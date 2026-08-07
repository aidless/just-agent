"""AML Retriever — 确定性特征提取（零依赖）。

提供 CJK n-gram、拉丁词、数字、日期、引用短语、实体式 token 的确定性提取。
所有函数均为纯函数，结果可复现，不依赖任何外部资源或模型。
"""
from __future__ import annotations

import re

# 中日韩统一表意文字（含扩展 A）与兼容表意文字
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")
_LAT_RE = re.compile(r"[a-z0-9]+")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_DATE_RE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    r"|\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"
)
_PHRASE_RE = re.compile(r"[\u201c\"]([^\u201d\"]{1,64})[\u201d\"]")
# 实体式拉丁 token：大写开头的词、全大写缩写、带连字符的专名
_ENTITY_LAT_RE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9]{1,}|[A-Z]{2,})\b")

# 检索价值极低的高频停用词（只用于查询侧降噪，索引侧不丢弃）
_STOP = {
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for", "is", "are",
    "was", "were", "be", "been", "it", "this", "that", "with", "as", "by", "from",
    "what", "which", "who", "when", "where", "how", "did", "do", "does", "his",
    "her", "their", "they", "he", "she", "you", "i", "we", "best", "matches",
    "answer", "question", "memory",
    "\u7684", "\u4e86", "\u662f", "\u5728", "\u548c", "\u6709", "\u4ec0\u4e48",
}

# 时间意图标记：出现这些词，说明用户问的是「当前状态」而非「历史上说过什么」。
# 此时同一属性的多条记录里，最新的那条才是答案，需要显著抬高新近度权重。
_TEMPORAL_CN = (
    "现在", "目前", "当前", "如今", "最近", "最新", "眼下", "这个月", "这周",
    "今天", "近期", "改成", "换成", "还是", "已经",
)
_TEMPORAL_EN = (
    "now", "current", "currently", "latest", "recent", "recently", "today",
    "these days", "nowadays", "at present", "up to date", "still",
)


def has_temporal_intent(query: str) -> bool:
    """判断查询是否在问「当前状态」。纯确定性规则，无模型依赖。"""
    if not query:
        return False
    lowered = query.lower()
    if any(marker in query for marker in _TEMPORAL_CN):
        return True
    return any(marker in lowered for marker in _TEMPORAL_EN)


def cjk_ngrams(text: str, n: int) -> list[str]:
    out: list[str] = []
    for match in _CJK_RE.finditer(text or ""):
        run = match.group(0)
        if n == 1:
            out.extend(list(run))
        else:
            for i in range(len(run) - n + 1):
                out.append(run[i : i + n])
    return out


def tokenize(text: str, max_ngram: int = 3) -> list[str]:
    """索引侧分词：CJK 1~3-gram + 拉丁词 + 数字 + 日期，统一小写。"""
    text = (text or "").lower()
    tokens: list[str] = []
    for n in range(1, max(1, max_ngram) + 1):
        tokens.extend(cjk_ngrams(text, n))
    tokens.extend(_LAT_RE.findall(text))
    tokens.extend(t.replace(",", "") for t in _NUM_RE.findall(text))
    tokens.extend(_DATE_RE.findall(text))
    return tokens


def index_text(text: str) -> str:
    """生成写入 FTS5 的索引串（去重以压缩体积，检索语义不变）。"""
    seen: set[str] = set()
    out: list[str] = []
    for tok in tokenize(text):
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return " ".join(out)


def query_tokens(text: str, cap: int = 64) -> list[str]:
    """查询侧 token：去停用词、去重，并按"长者优先"确定性截断。

    截断是为了界定 FTS5 OR 查询的最坏代价；长 token（trigram / 日期 / 长词）
    区分度更高，优先保留。同长度按字典序，保证完全确定性。
    """
    raw = tokenize(text)
    uniq = {t for t in raw if t and t not in _STOP}
    ordered = sorted(uniq, key=lambda t: (-len(t), t))
    return ordered[: max(1, cap)]


def extract_numbers(text: str) -> list[str]:
    return [t.replace(",", "") for t in _NUM_RE.findall(text or "")]


def extract_dates(text: str) -> list[str]:
    return _DATE_RE.findall(text or "")


def extract_phrases(text: str) -> list[str]:
    """提取被引号包裹的精确短语（中文 \u201c \u201d 或英文 " "）。"""
    return [m.group(1) for m in _PHRASE_RE.finditer(text or "")]


def extract_entities(text: str) -> list[str]:
    """确定性"实体式" token：拉丁专名/缩写 + CJK 2~4gram（作为中文实体近似）。"""
    out: list[str] = []
    out.extend(m.group(0) for m in _ENTITY_LAT_RE.finditer(text or ""))
    for n in (2, 3, 4):
        out.extend(cjk_ngrams(text or "", n))
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in out:
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            uniq.append(tok)
    return uniq


def normalize(text: str) -> str:
    return (text or "").lower()


__all__ = [
    "tokenize",
    "index_text",
    "query_tokens",
    "cjk_ngrams",
    "extract_numbers",
    "extract_dates",
    "extract_phrases",
    "extract_entities",
    "normalize",
    "has_temporal_intent",
]
