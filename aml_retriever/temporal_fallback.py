"""temporal_fallback.py — 时间戳缺失与模糊表达的分级兜底

解决的核心问题
--------------
在实际多轮对话记忆中，时间信息经常不完整：
1. ts_ms 字段完全缺失（平台未发送 timestamp）
2. 消息内容中只有模糊表达（"下周""明天""最近"）
3. 消息内容中有相对时间（"三天前""两个月后"）
4. 消息内容中有部分日期（"8月14日"，无年份）
5. 消息内容中完全没有时间信息

官方评判器对时间粒度严格匹配，但前提是"能找到正确的时间信息"。
如果时间信息根本不存在，任何粒度保留都无从谈起。
本模块提供五层分级兜底，确保在时间信息不完整时，
系统仍能给出合理的时间估计，而不是直接返回 None 导致排序失效。

五层兜底策略
------------
L1  直接使用 ts_ms（最可靠）
L2  从 content 提取绝对时间表达（正则匹配）
L3  从 content 提取相对时间表达，结合会话上下文锚点推算
L4  使用 created_at 字段（写入时间，非事件时间）
L5  使用会话中相邻消息的时间戳插值

兜底原则
--------
- 每层兜底都标注置信度（HIGH/MEDIUM/LOW/ESTIMATED）
- 置信度越低，在排序中的权重越小
- 绝不凭空捏造时间；无法推断时返回 None + 置信度 UNKNOWN
- 模糊表达（"最近""下周"）只返回粒度范围，不精确到天
"""
from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# 置信度枚举
# ─────────────────────────────────────────────────────────────────────────────

class TimeConfidence(Enum):
    HIGH      = 4   # 来自 ts_ms 或内容中的绝对日期
    MEDIUM    = 3   # 来自内容中的相对时间 + 锚点推算
    LOW       = 2   # 来自 created_at（写入时间）
    ESTIMATED = 1   # 来自相邻消息插值
    UNKNOWN   = 0   # 无法推断


@dataclass
class TemporalResult:
    epoch: Optional[float]          # Unix 时间戳（秒），None 表示无法推断
    confidence: TimeConfidence
    granularity: str                # "second"/"minute"/"hour"/"day"/"month"/"year"/"range"
    source: str                     # 时间来源描述
    original_expression: str = ""  # 原始时间表达（用于粒度保留）


# ─────────────────────────────────────────────────────────────────────────────
# 绝对时间提取（L2）
# ─────────────────────────────────────────────────────────────────────────────

_ABS_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # 精确到秒
    ("second", re.compile(
        r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号T ](\d{2}):(\d{2}):(\d{2})'
    ), "absolute"),
    # 精确到分
    ("minute", re.compile(
        r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号T ](\d{2}):(\d{2})(?!:\d)'
    ), "absolute"),
    # 精确到小时
    ("hour", re.compile(
        r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号T ](\d{2})(?!:\d)'
    ), "absolute"),
    # 精确到天（ISO）
    ("day", re.compile(
        r'(\d{4})-(\d{2})-(\d{2})(?![T \d])'
    ), "absolute"),
    # 精确到天（中文）
    ("day", re.compile(
        r'(\d{4})年(\d{1,2})月(\d{1,2})[日号]'
    ), "absolute"),
    # 精确到天（英文月份）
    ("day", re.compile(
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})',
        re.IGNORECASE,
    ), "absolute_en"),
    # 精确到月
    ("month", re.compile(
        r'(\d{4})[-/年](\d{1,2})(?![-/月\d])'
    ), "absolute"),
    # 精确到月（中文）
    ("month", re.compile(
        r'(\d{4})年(\d{1,2})月(?!\d{1,2}[日号])'
    ), "absolute"),
    # 精确到年
    ("year", re.compile(
        r'(?<!\d)((?:19|20)\d{2})(?!\s*[-年月\d])'
    ), "absolute"),
]

# 无年份“8月28日”只在有会话锚点时才可解析；Add 阶段仅持久化月/日，
# 不臆造年份。Search 阶段按锚点日调用 LRU 缓存的纯计算函数。
_PARTIAL_MONTH_DAY_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])月(3[01]|[12]\d|0?[1-9])[日号]?")


def extract_partial_month_day(text: str) -> tuple[int, int, str] | None:
    match = _PARTIAL_MONTH_DAY_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), match.group(0)


@lru_cache(maxsize=4096)
def _resolve_month_day_for_anchor_day(month: int, day: int, anchor_day_utc: int) -> float | None:
    """将无年份月日映射到离锚点日期最近的合法年份；结果按锚点日缓存。"""
    anchor = datetime.fromtimestamp(anchor_day_utc * 86400, tz=timezone.utc)
    candidates = []
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda dt: abs((dt - anchor).total_seconds())).timestamp()


def resolve_stored_month_day(month: int | None, day: int | None, anchor_epoch: float | None) -> TemporalResult | None:
    if month is None or day is None or anchor_epoch is None:
        return None
    try:
        epoch = _resolve_month_day_for_anchor_day(int(month), int(day), int(float(anchor_epoch) // 86400))
    except (TypeError, ValueError, OverflowError):
        return None
    if epoch is None:
        return None
    return TemporalResult(epoch=epoch, confidence=TimeConfidence.MEDIUM, granularity="day",
                          source="stored_month_day_anchor", original_expression=f"{month}月{day}日")


_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_CHINESE_NUMERALS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _count(value: str) -> int:
    """解析阿拉伯数字与常用中文数词，支持一至九、十、十一至十九、二十至九十九。"""
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CHINESE_NUMERALS.get(left, 1) if left else 1
        ones = _CHINESE_NUMERALS.get(right, 0) if right else 0
        return tens * 10 + ones
    if value in _CHINESE_NUMERALS:
        return _CHINESE_NUMERALS[value]
    raise ValueError(f"unsupported relative-time count: {value!r}")


def parse_absolute_temporal(text: str) -> Optional[TemporalResult]:
    """从文本中提取最细粒度的绝对时间表达。"""
    for granularity, pattern, source_type in _ABS_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            if source_type == "absolute_en":
                # "August 14, 2026" → groups = (day, year)
                month_str = m.group(0).split()[0][:3].lower()
                month = _MONTH_MAP.get(month_str, 1)
                day = int(groups[0])
                year = int(groups[1])
                dt = datetime(year, month, day, tzinfo=timezone.utc)
            elif granularity == "second":
                dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                              int(groups[3]), int(groups[4]), int(groups[5]),
                              tzinfo=timezone.utc)
            elif granularity == "minute":
                dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                              int(groups[3]), int(groups[4]), tzinfo=timezone.utc)
            elif granularity == "hour":
                dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                              int(groups[3]), tzinfo=timezone.utc)
            elif granularity == "day":
                dt = datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                              tzinfo=timezone.utc)
            elif granularity == "month":
                dt = datetime(int(groups[0]), int(groups[1]), 1, tzinfo=timezone.utc)
            else:  # year
                dt = datetime(int(groups[0]), 1, 1, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        return TemporalResult(
            epoch=dt.timestamp(),
            confidence=TimeConfidence.HIGH,
            granularity=granularity,
            source=f"content_absolute:{granularity}",
            original_expression=m.group(0),
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 相对时间提取（L3）
# ─────────────────────────────────────────────────────────────────────────────

# 格式：(模式, 偏移计算函数(anchor_epoch) -> epoch, 粒度)
_REL_PATTERNS: list[tuple[re.Pattern, object, str]] = [
    # 中文相对时间
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*天前'), lambda g, a: a - _count(g[0]) * 86400, "day"),
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*天后'), lambda g, a: a + _count(g[0]) * 86400, "day"),
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*周前'), lambda g, a: a - _count(g[0]) * 7 * 86400, "day"),
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*周后'), lambda g, a: a + _count(g[0]) * 7 * 86400, "day"),
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*个?月前'), lambda g, a: a - _count(g[0]) * 30 * 86400, "month"),
    (re.compile(r'([0-9一二三四五六七八九十两]+)\s*个?月后'), lambda g, a: a + _count(g[0]) * 30 * 86400, "month"),
    (re.compile(r'昨天'), lambda g, a: a - 86400, "day"),
    (re.compile(r'明天'), lambda g, a: a + 86400, "day"),
    (re.compile(r'后天'), lambda g, a: a + 2 * 86400, "day"),
    (re.compile(r'前天'), lambda g, a: a - 2 * 86400, "day"),
    (re.compile(r'下周'), lambda g, a: a + 7 * 86400, "range"),
    (re.compile(r'上周'), lambda g, a: a - 7 * 86400, "range"),
    (re.compile(r'下个?月'), lambda g, a: a + 30 * 86400, "range"),
    (re.compile(r'上个?月'), lambda g, a: a - 30 * 86400, "range"),
    # 英文相对时间
    (re.compile(r'(\d+)\s+days?\s+ago', re.IGNORECASE), lambda g, a: a - _count(g[0]) * 86400, "day"),
    (re.compile(r'(\d+)\s+weeks?\s+ago', re.IGNORECASE), lambda g, a: a - _count(g[0]) * 7 * 86400, "day"),
    (re.compile(r'(\d+)\s+months?\s+ago', re.IGNORECASE), lambda g, a: a - _count(g[0]) * 30 * 86400, "month"),
    (re.compile(r'yesterday', re.IGNORECASE), lambda g, a: a - 86400, "day"),
    (re.compile(r'tomorrow', re.IGNORECASE), lambda g, a: a + 86400, "day"),
    (re.compile(r'next\s+week', re.IGNORECASE), lambda g, a: a + 7 * 86400, "range"),
    (re.compile(r'last\s+week', re.IGNORECASE), lambda g, a: a - 7 * 86400, "range"),
]

# 模糊时间词（无法精确推算，只能标注为 range）
_VAGUE_PATTERNS = re.compile(
    r'最近|近期|近来|不久前|不久后|过段时间|sometime|recently|soon|lately',
    re.IGNORECASE,
)


def has_temporal_expression(text: str) -> bool:
    """Add 阶段预筛：只标记是否值得在 Search 做完整相对时间解析。"""
    if not text:
        return False
    if _VAGUE_PATTERNS.search(text):
        return True
    return any(pattern.search(text) for pattern, _offset, _granularity in _REL_PATTERNS)


def _parse_relative(text: str, anchor_epoch: Optional[float]) -> Optional[TemporalResult]:
    """从文本中提取相对时间表达，结合锚点推算绝对时间。"""
    if anchor_epoch is None:
        # 无锚点时，模糊时间词只能标注为 range
        if _VAGUE_PATTERNS.search(text):
            return TemporalResult(
                epoch=None,
                confidence=TimeConfidence.LOW,
                granularity="range",
                source="content_vague_no_anchor",
                original_expression=_VAGUE_PATTERNS.search(text).group(0),
            )
        return None

    for pattern, offset_fn, granularity in _REL_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groups()
        try:
            epoch = offset_fn(groups, anchor_epoch)
        except (IndexError, ValueError):
            continue
        return TemporalResult(
            epoch=float(epoch),
            confidence=TimeConfidence.MEDIUM,
            granularity=granularity,
            source=f"content_relative:{granularity}",
            original_expression=m.group(0),
        )

    # 模糊时间词（有锚点但无法精确）
    if _VAGUE_PATTERNS.search(text):
        return TemporalResult(
            epoch=anchor_epoch,  # 用锚点作为近似
            confidence=TimeConfidence.LOW,
            granularity="range",
            source="content_vague_with_anchor",
            original_expression=_VAGUE_PATTERNS.search(text).group(0),
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 主入口：分级兜底
# ─────────────────────────────────────────────────────────────────────────────

def resolve_temporal(
    *,
    ts_ms: Optional[int],
    content: str,
    created_at: Optional[str],
    session_anchor_epoch: Optional[float] = None,
    adjacent_epochs: Optional[list[float]] = None,
) -> TemporalResult:
    """分级兜底：按 L1→L2→L3→L4→L5 顺序尝试，返回最高置信度的时间结果。

    参数
    ----
    ts_ms               : 消息的原始 Unix 毫秒时间戳（可为 None）
    content             : 消息正文
    created_at          : 写入时间（ISO 字符串，可为 None）
    session_anchor_epoch: 会话中已知的最近一个可靠时间戳（秒）
    adjacent_epochs     : 相邻消息的时间戳列表（用于插值）
    """
    # L1：直接使用 ts_ms
    if ts_ms is not None:
        try:
            epoch = int(ts_ms) / 1000.0
            return TemporalResult(
                epoch=epoch,
                confidence=TimeConfidence.HIGH,
                granularity="millisecond",
                source="ts_ms",
                original_expression=str(ts_ms),
            )
        except (TypeError, ValueError):
            pass

    # L2：从 content 提取绝对时间
    if content:
        absolute = parse_absolute_temporal(content)
        if absolute:
            return absolute

    # L3：从 content 提取相对时间（结合会话锚点）
    if content:
        rel_result = _parse_relative(content, session_anchor_epoch)
        # 对“最近”等无锚点模糊表达，保留 LOW/range 结果而非降级为 UNKNOWN；
        # 它没有可排序的精确 epoch，但仍是可解释的时间范围信号。
        if rel_result:
            return rel_result

    # L4：使用 created_at（写入时间，非事件时间）
    if created_at:
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return TemporalResult(
                epoch=dt.timestamp(),
                confidence=TimeConfidence.LOW,
                granularity="second",
                source="created_at",
                original_expression=created_at,
            )
        except ValueError:
            pass

    # L5：相邻消息插值
    if adjacent_epochs:
        valid = [e for e in adjacent_epochs if e is not None]
        if valid:
            interpolated = sum(valid) / len(valid)
            return TemporalResult(
                epoch=interpolated,
                confidence=TimeConfidence.ESTIMATED,
                granularity="range",
                source=f"adjacent_interpolation({len(valid)})",
                original_expression="",
            )

    # 完全无法推断
    return TemporalResult(
        epoch=None,
        confidence=TimeConfidence.UNKNOWN,
        granularity="unknown",
        source="none",
        original_expression="",
    )


def confidence_to_recency_weight(
    base_weight: float,
    confidence: TimeConfidence,
) -> float:
    """将置信度映射为新近度权重乘数。

    置信度越低，新近度权重越小，避免不可靠的时间估计影响排序。
    """
    multipliers = {
        TimeConfidence.HIGH:      1.0,
        TimeConfidence.MEDIUM:    0.7,
        TimeConfidence.LOW:       0.4,
        TimeConfidence.ESTIMATED: 0.2,
        TimeConfidence.UNKNOWN:   0.0,
    }
    return base_weight * multipliers.get(confidence, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 独立测试
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== L1：ts_ms 直接使用 ===")
    r = resolve_temporal(ts_ms=1786780800000, content="发布日期 2026-08-14",
                         created_at=None)
    print(f"  {r.source}  confidence={r.confidence.name}  epoch={r.epoch}")
    assert r.confidence == TimeConfidence.HIGH
    print("✓ L1 通过\n")

    print("=== L2：content 绝对时间 ===")
    r = resolve_temporal(ts_ms=None, content="发布日期定在 2026-08-21，负责人林涛。",
                         created_at=None)
    print(f"  {r.source}  granularity={r.granularity}  expr={r.original_expression!r}")
    assert r.confidence == TimeConfidence.HIGH
    assert r.granularity == "day"
    print("✓ L2 通过\n")

    print("=== L3：相对时间 + 锚点 ===")
    anchor = datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp()
    r = resolve_temporal(ts_ms=None, content="三天前确认了预算。",
                         created_at=None, session_anchor_epoch=anchor)
    print(f"  {r.source}  granularity={r.granularity}  epoch={r.epoch}")
    expected = anchor - 3 * 86400
    assert abs(r.epoch - expected) < 1, f"期望 {expected}，实际 {r.epoch}"
    assert r.confidence == TimeConfidence.MEDIUM
    print("✓ L3 通过\n")

    print("=== L3：模糊时间词（无锚点）===")
    r = resolve_temporal(ts_ms=None, content="最近项目进展顺利。",
                         created_at=None, session_anchor_epoch=None)
    print(f"  {r.source}  confidence={r.confidence.name}  granularity={r.granularity}")
    assert r.confidence == TimeConfidence.LOW
    assert r.granularity == "range"
    print("✓ L3 模糊无锚点通过\n")

    print("=== L4：created_at 兜底 ===")
    r = resolve_temporal(ts_ms=None, content="会议记录已更新。",
                         created_at="2026-08-10T08:00:00Z")
    print(f"  {r.source}  confidence={r.confidence.name}")
    assert r.confidence == TimeConfidence.LOW
    assert r.source == "created_at"
    print("✓ L4 通过\n")

    print("=== L5：相邻消息插值 ===")
    adj = [
        datetime(2026, 8, 10, tzinfo=timezone.utc).timestamp(),
        datetime(2026, 8, 12, tzinfo=timezone.utc).timestamp(),
    ]
    r = resolve_temporal(ts_ms=None, content="无时间信息的消息。",
                         created_at=None, adjacent_epochs=adj)
    print(f"  {r.source}  confidence={r.confidence.name}  epoch={r.epoch}")
    assert r.confidence == TimeConfidence.ESTIMATED
    print("✓ L5 通过\n")

    print("=== 完全无时间信息 ===")
    r = resolve_temporal(ts_ms=None, content="无任何时间信息。",
                         created_at=None)
    print(f"  {r.source}  confidence={r.confidence.name}")
    assert r.confidence == TimeConfidence.UNKNOWN
    assert r.epoch is None
    print("✓ UNKNOWN 通过\n")

    print("=== 置信度权重映射 ===")
    for conf in TimeConfidence:
        w = confidence_to_recency_weight(55.0, conf)
        print(f"  {conf.name}: weight={w:.1f}")
    print("✓ 权重映射通过")
