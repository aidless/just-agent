"""entity_disambiguate.py — 实体消歧与噪声过滤

解决的核心问题
--------------
1. 多义词噪声：查询中的"苹果"可能指公司也可能指水果；"春节"既是节日也可能是人名。
   纯子串匹配会把所有包含"苹果"的记忆都提升，包括完全不相关的记忆。

2. 同义词指代：查询说"林总"，记忆里写的是"林涛"；查询说"Q3"，记忆里写的是"第三季度"。
   纯子串匹配会错过这些相关记忆。

3. 短实体过度匹配：2 字 CJK n-gram（如"发布"）会命中几乎所有记忆，失去区分力。

设计原则
--------
- 零外部依赖：不使用 NLP 模型、词典或知识图谱，只用确定性规则。
- 可消融：每个过滤层都有独立开关，可以单独关闭以定位效果来源。
- 保守：宁可少过滤（保留噪声），也不过度过滤（丢失相关记忆）。
  在没有外部语义资源的情况下，消歧的上限是"减少最明显的噪声"，
  而不是"完全解决多义性"。

四个过滤层
----------
L1  最小长度过滤：丢弃过短的实体候选（CJK < 2 字，拉丁 < 3 字符）
L2  停用实体过滤：丢弃高频通用词（"项目""工作""问题"等）
L3  上下文锚定：只保留在查询中与其他词共现的实体（减少孤立多义词）
L4  共现验证：对高权重实体，要求其在记忆中与查询的另一个 token 共现

集成方式
--------
在 entity_boost.py 的 apply_entity_boost() 中，将 _extract_entities_from_query()
替换为本模块的 extract_disambiguated_entities()。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# 停用实体词表（高频通用词，单独出现时无区分力）
# ─────────────────────────────────────────────────────────────────────────────

# 原则：只列出"单独出现时几乎不携带实体语义"的词。
# 不列入专有名词（即使常见）；不列入数字、日期类词。
_STOP_ENTITIES_CN: frozenset[str] = frozenset({
    # 极高频通用名词
    "项目", "工作", "问题", "情况", "内容", "方案", "结果", "进度", "计划",
    "会议", "文件", "报告", "邮件", "消息", "通知", "记录", "数据", "信息",
    "系统", "平台", "功能", "模块", "版本", "接口", "服务", "流程", "规则",
    "用户", "客户", "团队", "成员", "负责", "完成", "确认", "更新", "发布",
    "时间", "日期", "今天", "明天", "昨天", "本周", "下周", "上周", "本月",
    # 代词和指示词
    "这个", "那个", "这些", "那些", "我们", "他们", "你们",
    # 动词性词语（被 n-gram 切出来的）
    "需要", "可以", "应该", "已经", "正在", "开始", "结束", "继续",
})

_STOP_ENTITIES_EN: frozenset[str] = frozenset({
    "the", "this", "that", "these", "those", "with", "from", "about",
    "project", "task", "issue", "problem", "meeting", "report", "update",
    "system", "service", "team", "user", "client", "version", "module",
    "data", "info", "information", "content", "result", "plan", "process",
    "need", "should", "would", "could", "will", "have", "been", "done",
})


# ─────────────────────────────────────────────────────────────────────────────
# 同义词映射（可扩展的轻量词典）
# ─────────────────────────────────────────────────────────────────────────────

# 格式：{规范形式: [同义词列表]}
# 查询中出现同义词时，展开为规范形式 + 同义词，同时在记忆中匹配两者。
# 这里只列举最常见的通用缩写和称谓模式；项目专有名词应由用户扩展。
_SYNONYM_MAP: dict[str, list[str]] = {
    # 季度缩写
    "第一季度": ["q1", "Q1", "一季度"],
    "第二季度": ["q2", "Q2", "二季度"],
    "第三季度": ["q3", "Q3", "三季度"],
    "第四季度": ["q4", "Q4", "四季度"],
    # 常见职位称谓（"林总" → "林涛" 这类需要用户自定义，此处只处理通用模式）
    # 通用缩写
    "人工智能": ["ai", "AI"],
    "应用程序接口": ["api", "API"],
    "用户界面": ["ui", "UI"],
    "用户体验": ["ux", "UX"],
    "机器学习": ["ml", "ML"],
    "大语言模型": ["llm", "LLM"],
    "检索增强生成": ["rag", "RAG"],
}

# 反向索引：同义词 → 规范形式
_REVERSE_SYNONYM: dict[str, str] = {}
for canonical, synonyms in _SYNONYM_MAP.items():
    for syn in synonyms:
        _REVERSE_SYNONYM[syn.lower()] = canonical
        _REVERSE_SYNONYM[canonical.lower()] = canonical


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EntityCandidate:
    text: str                          # 原始文本
    normalized: str                    # 小写规范化
    canonical: str                     # 同义词规范形式（若无映射则等于 normalized）
    length: int                        # 字符长度
    is_cjk: bool                       # 是否为 CJK 实体
    context_tokens: list[str]          # 查询中与该实体共现的其他 token
    filter_reason: str = ""            # 被过滤的原因（空字符串表示保留）
    boost_multiplier: float = 1.0      # 权重乘数（多义词降权时 < 1.0）


# ─────────────────────────────────────────────────────────────────────────────
# 核心：消歧实体提取
# ─────────────────────────────────────────────────────────────────────────────

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_LAT_ENTITY_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]{1,}|[A-Z]{2,})\b")
_QUOTE_RE = re.compile(r'["\u201c]([^"\u201d]{1,64})["\u201d]')


def _split_query_tokens(query: str) -> list[str]:
    """将查询拆分为基本 token（空格分词 + CJK 单字）。"""
    tokens: list[str] = []
    for part in re.split(r"\s+", query):
        part = part.strip(".,!?;:\"'")
        if not part:
            continue
        if _CJK_RE.search(part):
            tokens.extend(list(part))  # CJK 拆为单字
        else:
            tokens.append(part.lower())
    return [t for t in tokens if t]


def extract_disambiguated_entities(
    query: str,
    *,
    min_cjk_len: int = 2,
    min_lat_len: int = 3,
    require_cooccurrence: bool = True,
    expand_synonyms: bool = True,
    cap: int = 16,
) -> list[EntityCandidate]:
    """从查询中提取经过消歧过滤的实体候选列表。

    参数
    ----
    min_cjk_len         : CJK 实体的最小字符长度（L1 过滤）
    min_lat_len         : 拉丁实体的最小字符长度（L1 过滤）
    require_cooccurrence: 是否要求实体与查询中其他 token 共现（L3 过滤）
    expand_synonyms     : 是否展开同义词（L4 增强）
    cap                 : 最多返回 N 个实体
    """
    if not query:
        return []

    query_tokens = _split_query_tokens(query)
    query_token_set = set(query_tokens)
    candidates: list[EntityCandidate] = []
    seen_normalized: set[str] = set()

    # ── 提取候选 ──────────────────────────────────────────────────────────────
    raw_entities: list[str] = []

    # 引号短语（最高优先级）
    for m in _QUOTE_RE.finditer(query):
        raw_entities.append(m.group(1))

    # 拉丁专名
    for m in _LAT_ENTITY_RE.finditer(query):
        raw_entities.append(m.group(0))

    # CJK n-gram（2~4 字）
    for m in _CJK_RE.finditer(query):
        run = m.group(0)
        for n in (4, 3, 2):  # 长者优先
            for i in range(len(run) - n + 1):
                raw_entities.append(run[i:i+n])

    # ── 过滤与消歧 ────────────────────────────────────────────────────────────
    for raw in raw_entities:
        normalized = raw.lower().strip()
        if not normalized or normalized in seen_normalized:
            continue
        seen_normalized.add(normalized)

        is_cjk = bool(_CJK_RE.search(raw))
        length = len(raw)

        # L1：最小长度过滤
        if is_cjk and length < min_cjk_len:
            candidates.append(EntityCandidate(
                text=raw, normalized=normalized, canonical=normalized,
                length=length, is_cjk=is_cjk, context_tokens=[],
                filter_reason=f"L1:too_short({length}<{min_cjk_len})",
            ))
            continue
        # 两字符季度缩写（Q1–Q4）是高区分度的有效实体，不受通用长度阈值限制。
        is_quarter_abbr = bool(re.fullmatch(r"q[1-4]", normalized, re.IGNORECASE))
        if not is_cjk and length < min_lat_len and not is_quarter_abbr:
            candidates.append(EntityCandidate(
                text=raw, normalized=normalized, canonical=normalized,
                length=length, is_cjk=is_cjk, context_tokens=[],
                filter_reason=f"L1:too_short({length}<{min_lat_len})",
            ))
            continue

        # L2：停用实体过滤
        if normalized in _STOP_ENTITIES_CN or normalized in _STOP_ENTITIES_EN:
            candidates.append(EntityCandidate(
                text=raw, normalized=normalized, canonical=normalized,
                length=length, is_cjk=is_cjk, context_tokens=[],
                filter_reason="L2:stop_entity",
            ))
            continue

        # 同义词规范化
        canonical = _REVERSE_SYNONYM.get(normalized, normalized)

        # L3：上下文锚定（要求与查询中其他 token 共现）
        context_tokens = [t for t in query_token_set
                          if t != normalized and t not in _STOP_ENTITIES_CN
                          and t not in _STOP_ENTITIES_EN and len(t) >= 2]
        if require_cooccurrence and not context_tokens:
            # 查询中只有这一个实体，无法锚定上下文 → 降权而非过滤
            candidates.append(EntityCandidate(
                text=raw, normalized=normalized, canonical=canonical,
                length=length, is_cjk=is_cjk, context_tokens=[],
                filter_reason="",  # 保留但降权
                boost_multiplier=0.5,
            ))
            continue

        # 通过所有过滤层
        candidates.append(EntityCandidate(
            text=raw, normalized=normalized, canonical=canonical,
            length=length, is_cjk=is_cjk, context_tokens=context_tokens,
            filter_reason="",
            boost_multiplier=1.0,
        ))

    # ── 同义词展开 ────────────────────────────────────────────────────────────
    if expand_synonyms:
        extra: list[EntityCandidate] = []
        for cand in candidates:
            if cand.filter_reason:
                continue
            # 若规范形式有同义词，添加同义词作为额外候选
            synonyms = _SYNONYM_MAP.get(cand.canonical, [])
            for syn in synonyms:
                syn_norm = syn.lower()
                if syn_norm not in seen_normalized:
                    seen_normalized.add(syn_norm)
                    extra.append(EntityCandidate(
                        text=syn, normalized=syn_norm, canonical=cand.canonical,
                        length=len(syn), is_cjk=bool(_CJK_RE.search(syn)),
                        context_tokens=cand.context_tokens,
                        filter_reason="",
                        boost_multiplier=0.8,  # 同义词展开略降权
                    ))
        candidates.extend(extra)

    # ── 截断（保留有效候选，按长度降序）────────────────────────────────────────
    valid = [c for c in candidates if not c.filter_reason]
    valid.sort(key=lambda c: (-c.length, c.normalized))
    return valid[:cap]


def apply_entity_boost_v2(
    records: list[dict],
    *,
    query: str,
    base_weight: float = 35.0,
    cooccurrence_weight: float = 20.0,
    max_entity_hits: int = 3,
    config=None,
) -> list[dict]:
    """带消歧过滤的实体优先排序（entity_boost.py 的升级版）。

    相比 v1 的改进
    --------------
    1. 使用 extract_disambiguated_entities() 过滤多义词和停用实体。
    2. 对同义词展开的实体使用较低权重（0.8 乘数）。
    3. 对孤立多义词（无上下文锚定）使用 0.5 乘数降权。
    4. 新增共现验证（L4）：对高权重实体，要求其在记忆中与查询的另一个 token 共现。
    5. 每条候选最多计入 max_entity_hits 个实体，防止重叠 CJK n-gram 重复累加。

    参数
    ----
    base_weight         : 实体命中的基础权重（每次命中）
    cooccurrence_weight : 共现验证通过时的额外加成
    max_entity_hits      : 每条候选最多计入的实体数（默认 3）
    """
    if not records:
        return records

    entities = extract_disambiguated_entities(query, cap=16)
    if not entities:
        return records

    # 构建查询 token 集合（用于共现验证）
    query_tokens = set(_split_query_tokens(query))

    for rec in records:
        content_lower = (rec.get("content") or "").lower()
        total_boost = 0.0
        hit_entities: list[str] = []

        for ent in entities:
            # 重叠 CJK n-gram 常描述同一语义片段；限制每条候选的有效命中数，
            # 以避免“发布会更新后”等相邻片段发生无界叠加。
            if len(hit_entities) >= max(1, int(max_entity_hits)):
                break
            # 主实体命中
            if ent.normalized not in content_lower and ent.canonical not in content_lower:
                continue

            hit_weight = base_weight * ent.boost_multiplier

            # L4：共现验证——要求记忆中同时出现查询的另一个 token
            cooccurrence_bonus = 0.0
            if ent.context_tokens:
                cooccurring = [t for t in ent.context_tokens if t in content_lower]
                if cooccurring:
                    cooccurrence_bonus = cooccurrence_weight * min(len(cooccurring), 3) / 3

            total_boost += hit_weight + cooccurrence_bonus
            hit_entities.append(f"{ent.text}({ent.boost_multiplier:.1f})")

        if total_boost > 0:
            rec["score"] = float(rec.get("score") or 0.0) + total_boost
            flag = f"entity_boost_v2:[{','.join(hit_entities[:3])}]"
            rec.setdefault("flags", []).append(flag)

    return records


# ─────────────────────────────────────────────────────────────────────────────
# 独立测试
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 测试 1：停用实体过滤 ===")
    entities = extract_disambiguated_entities("项目的发布日期是什么？")
    valid = [e for e in entities if not e.filter_reason]
    filtered = [e for e in entities if e.filter_reason]
    print(f"有效实体: {[e.text for e in valid]}")
    print(f"过滤实体: {[(e.text, e.filter_reason) for e in filtered]}")
    assert not any(e.text in ("项目", "发布", "日期") for e in valid), \
        "停用词不应出现在有效实体中"
    print("✓ 停用实体过滤通过\n")

    print("=== 测试 2：同义词展开 ===")
    entities = extract_disambiguated_entities("Q3 的目标完成了吗？", expand_synonyms=True)
    valid = [e for e in entities if not e.filter_reason]
    texts = [e.normalized for e in valid]
    print(f"有效实体（含同义词展开）: {texts}")
    assert "第三季度" in texts or "q3" in texts, "应包含 Q3 或其同义词"
    print("✓ 同义词展开通过\n")

    print("=== 测试 3：共现验证降权 ===")
    entities = extract_disambiguated_entities("苹果", require_cooccurrence=True)
    # 孤立实体应被降权（boost_multiplier=0.5）
    valid = [e for e in entities if not e.filter_reason]
    if valid:
        assert valid[0].boost_multiplier <= 0.5, "孤立多义词应降权"
        print(f"孤立实体 '苹果' 权重乘数: {valid[0].boost_multiplier}")
    print("✓ 孤立多义词降权通过\n")

    print("=== 测试 4：apply_entity_boost_v2 ===")
    records = [
        {"id": "m1", "content": "林涛确认 Q3 发布日期为 2026-08-21。", "score": 10.0, "flags": []},
        {"id": "m2", "content": "第三季度目标已完成。", "score": 8.0, "flags": []},
        {"id": "m3", "content": "会议记录已更新。", "score": 12.0, "flags": []},
    ]
    result = apply_entity_boost_v2(records, query="Q3 林涛 发布日期")
    result.sort(key=lambda r: -r["score"])
    print("排序结果：")
    for r in result:
        print(f"  [{r['id']}] score={r['score']:.1f}  flags={r['flags']}")
    assert result[0]["id"] == "m1", "m1 应排第一（命中 Q3/第三季度、林涛、发布日期）"
    print("✓ apply_entity_boost_v2 测试通过")
