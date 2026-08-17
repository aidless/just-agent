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
    # 扩充英文停用词（2026-08-15 消融，最终档）：仅纯人称/物主代词。
    # 实测：加上助动词（have/would/can 等）会导致 locomo R@100 −0.004~−0.007
    # 回归（这些词在完成时/情态查询里有语义）；仅代词档仍待全量确认，
    # 若无增益则整体回退到 v1.1 原始表。
    "me", "us", "him", "them", "its", "my", "your", "our", "their", "mine",
    "yours", "ours", "hers", "theirs", "myself", "yourself", "ourselves",
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

# 通用更新语义。只描述“旧状态被新状态替换”的语言现象，不包含评测实体、
# 数值或专有名词；用于可选的 supersession 防误判保护。
_UPDATE_CN = (
    "更新为", "改为", "改成", "换为", "换成", "调整为", "变更为", "转为",
    "已上调", "已下调", "上调为", "下调为", "最新口径", "新口径",
    "作废", "不再", "取代", "替换为", "迁移到", "搬到",
)
_UPDATE_EN = (
    "updated to", "changed to", "switched to", "revised to", "is now",
    "no longer", "replaced by", "supersedes", "deprecated", "new value",
    "effective from", "moved to", "migrated to",
)

# 偏好查询意图与第一人称直接陈述。后者比单纯 role=user 更严格，避免把
# “某某更喜欢……”这类用户转述误当成用户本人的偏好。
_PREFERENCE_CN = (
    "喜欢", "偏好", "首选", "最爱", "爱用", "常用", "习惯", "更愿意", "合我",
)
_PREFERENCE_EN = (
    "prefer", "preference", "favorite", "favourite", "go-to", "usually use",
    "tend to use", "like to use",
)
_NUMERIC_INTENT_CN = (
    "多少", "几个", "几号", "几点", "金额", "预算", "价格", "费用", "数量",
    "编号", "号码", "版本", "余额", "额度", "比例", "百分比",
)
_NUMERIC_INTENT_EN = (
    "how much", "how many", "amount", "budget", "price", "cost", "number",
    "version", "balance", "quota", "percentage", "ratio",
)
_DATE_INTENT_CN = (
    "什么时候", "何时", "哪天", "哪一天", "日期", "几号", "年月日",
)
_DATE_INTENT_EN = (
    "when", "what date", "which date", "release date", "start date", "end date",
)
_DIRECT_PREFERENCE_CN_RE = re.compile(
    r"(?:我|本人|我们)[^。！？!?]{0,16}(?:喜欢|偏好|首选|最爱|爱用|常用|习惯|更愿意)"
)
_DIRECT_PREFERENCE_EN_RE = re.compile(
    r"\b(?:i|we)\s+(?:(?:really|usually|generally)\s+)?"
    r"(?:prefer|like|favor|favour|use|tend\s+to\s+use)\b"
    r"|\b(?:my|our)\s+(?:favorite|favourite|preferred|go-to)\b",
    re.IGNORECASE,
)


def has_temporal_intent(query: str) -> bool:
    """判断查询是否在问「当前状态」。纯确定性规则，无模型依赖。"""
    if not query:
        return False
    lowered = query.lower()
    if any(marker in query for marker in _TEMPORAL_CN):
        return True
    return any(marker in lowered for marker in _TEMPORAL_EN)


def has_update_cue(text: str) -> bool:
    """判断陈述是否显式表达状态替换或旧值失效。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _UPDATE_CN):
        return True
    return any(marker in lowered for marker in _UPDATE_EN)


# 否定表达：用于 D 维度冲突成对返回（检测同话题相反极性）。
_NEGATION_EN = (
    " not ", " never ", " no ", "don't", "dont", "doesn't", "doesnt",
    "isn't", "isnt", "aren't", "arent", "haven't", "havent", "hasn't",
    "hasnt", "didn't", "didnt", "won't", "wont", "can't", "cant",
    "couldn't", "couldnt", "without", "rarely", "seldom", "hardly",
)
_NEGATION_CN = ("不", "没", "无", "未", "别", "莫", "非")


def has_negation(text: str) -> bool:
    """判断陈述是否含否定表达（用于相反极性冲突检测）。"""
    if not text:
        return False
    if any(marker in text for marker in _NEGATION_CN):
        return True
    padded = " " + text.lower() + " "
    return any(marker in padded for marker in _NEGATION_EN)


def has_preference_intent(text: str) -> bool:
    """判断查询是否在询问偏好或习惯。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _PREFERENCE_CN):
        return True
    return any(marker in lowered for marker in _PREFERENCE_EN)


def has_direct_preference_statement(text: str) -> bool:
    """判断文本是否为第一人称偏好陈述，而非对第三人的转述。"""
    if not text:
        return False
    return bool(
        _DIRECT_PREFERENCE_CN_RE.search(text)
        or _DIRECT_PREFERENCE_EN_RE.search(text)
    )


def has_numeric_value_intent(text: str) -> bool:
    """判断查询是否明确索要金额、数量、编号或版本类数值状态。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _NUMERIC_INTENT_CN):
        return True
    return any(marker in lowered for marker in _NUMERIC_INTENT_EN)


def has_date_value_intent(text: str) -> bool:
    """判断查询是否明确索要某个日期或发生时间。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _DATE_INTENT_CN):
        return True
    return any(marker in lowered for marker in _DATE_INTENT_EN)


# 英文月份名（含缩写）——查询中出现月份 = 时间上下文
_MONTH_NAME_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)

# 时长意图：问"多久/几个月/几年"这类时间跨度
_DURATION_INTENT_EN = (
    "how long", "how many months", "how many days", "how many weeks",
    "how many years", "how many hours", "how long ago", "lapsed between",
    "duration", "time span",
)
_DURATION_INTENT_CN = ("多久", "多长时间", "几个月", "几天", "几周", "几年", "间隔", "历时")


def has_explicit_date(text: str) -> bool:
    """查询文本是否含显式日期（数字/中文日期或英文月份名）。"""
    if not text:
        return False
    return bool(extract_dates(text) or _MONTH_NAME_RE.search(text))


def has_duration_intent(text: str) -> bool:
    """判断查询是否在问时间跨度（多久/几个月等）。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _DURATION_INTENT_CN):
        return True
    return any(marker in lowered for marker in _DURATION_INTENT_EN)


# 顺序/流程意图：问"先发生什么、按什么顺序、依次经历什么"
_ORDERING_INTENT_EN = (
    "in what order", "which order", "the order", "in order", "sequence",
    "chronological", "chronologically", "timeline", "step by step", "steps",
    "walk me through", "list the order", "what came first", "what came next",
    "first,", "then,", "after that", "before that",
)
_ORDERING_INTENT_CN = (
    "顺序", "先后", "依次", "按时间", "流程", "步骤", "排序", "先发生", "然后", "之后",
    "前因后果", "时间线",
)


def has_ordering_intent(text: str) -> bool:
    """判断查询是否在问事件发生的顺序/时间线。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _ORDERING_INTENT_CN):
        return True
    return any(marker in lowered for marker in _ORDERING_INTENT_EN)


# 建议/咨询意图（个性化建议类）：PersonaMem 式问题多为"给我建议/推荐"，
# 与 has_preference_intent 的"直接问偏好"互补。独立函数，不改动原意图。
_ADVICE_INTENT_EN = (
    "what are some", "what is the best", "what's the best", "how can i",
    "how do i", "how should i", "suggest", "recommend", "tips", "ideas",
    "ways to", "advice", "help me", "help me with", "i'm planning",
    "i am planning", "what should i", "options for", "guide me", "any ideas",
    "best way to", "good way to", "i want to",
)
_ADVICE_INTENT_CN = (
    "建议", "推荐", "怎么做", "怎么办", "有什么好", "帮我", "方法", "技巧",
    "规划", "打算", "想", "适合", "选择", "方案",
)


def has_advice_intent(text: str) -> bool:
    """判断查询是否在寻求建议/推荐/规划类回答。"""
    if not text:
        return False
    lowered = text.lower()
    if any(marker in text for marker in _ADVICE_INTENT_CN):
        return True
    return any(marker in lowered for marker in _ADVICE_INTENT_EN)


def has_temporal_context(text: str) -> bool:
    """v1.2.1 时间前缀的统一门控：时间/日期意图、显式日期、月份名或时长意图。

    消融证据（locomo10 297 项）：无条件前缀在非时间类问题回退（cat4 −0.008 /
    cat5 −0.030），而仅靠 has_temporal_intent/has_date_value_intent 会漏掉
    23% 的日期类问题（如 "What was Sam doing on December 4, 2023?" /
    "How many months lapsed between..."），导致 cat2 回退 −0.063。
    """
    if not text:
        return False
    return (
        has_temporal_intent(text)
        or has_date_value_intent(text)
        or has_explicit_date(text)
        or has_duration_intent(text)
    )


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


def extract_non_date_numbers(text: str) -> list[str]:
    """提取日期表达式之外的数字，供答案类型保护使用。"""
    return extract_numbers(_DATE_RE.sub(" ", text or ""))


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
    "extract_non_date_numbers",
    "extract_dates",
    "extract_phrases",
    "has_negation",
    "extract_entities",
    "normalize",
    "has_temporal_intent",
    "has_update_cue",
    "has_preference_intent",
    "has_direct_preference_statement",
    "has_numeric_value_intent",
    "has_date_value_intent",
    "has_explicit_date",
    "has_duration_intent",
    "has_temporal_context",
    "has_ordering_intent",
    "has_advice_intent",
]
