"""AML Retriever — 运行配置（零依赖）。

配置来源优先级：显式参数 > 环境变量 > JSON 配置文件 > 内置默认值。
所有检索增强都以 flag 形式暴露，便于离线消融（ablation）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

# 检索增强开关。每一项都可独立关闭，用于消融实验。
# 默认值只反映"当前有离线证据支持"的配置，无证据的一律默认关闭。
DEFAULT_FLAGS: dict[str, bool] = {
    "views": True,       # 聚合视图（window / session-segment）参与检索
    "exact": True,       # 精确子串 / token 覆盖率加权
    "datenum": True,     # 数字与日期精确匹配加权
    "entity": True,      # 引用短语 / 实体式 token 加权
    "rerank": True,      # 时间、邻接上下文、provenance、冲突提示软重排
    # 时间意图：查询含「现在 / 最近 / latest」等标记时额外抬高相对新近度权重。
    # 默认关闭。证据见 docs/EVAL.md 附录 B（medium/seed=20260806，2026-08-06 实测）：
    # 它对目标 temporal 类的增益微乎其微（mixed 该类 MRR 0.5392 → 0.5407，+0.0015），
    # 却拉低整体表现（mixed 总 MRR 0.6631 → 0.6561，Recall@20 1.0 → 0.9922；
    # paraphrase 总 MRR 0.5687 → 0.5591）。收益来自「相对新近度」本身，
    # 而非在其上再叠一层意图放大，故保持 disabled。
    "temporal_intent": False,
    # 加权 RRF 默认开启，并配合**低词法权重**（rrf_weight_lexical=0.1）。
    # 证据见 docs/EVAL.md 附录 A（medium/seed=20260806，2026-08-06 实测）：
    # w_lex=0.1 是扫描点中唯一的 Pareto 安全点——三种难度 MRR 均为正增益
    # （+0.0009 ~ +0.0060），且 Recall@20 保持 1.0000、延迟无实质变化。
    # 更大的权重能换来更高 MRR（mixed w=1.0 时 +0.0325），但要付出 Recall@20 代价
    # （mixed 0.8750、paraphrase 0.7708），属于用召回换排序，未作为默认。
    "rrf": True,         # FTS rank 与特征分值两路加权 RRF 融合
    "dedup": True,       # 候选去重（同内容 / 完全被覆盖的聚合视图）
    "use_options": True, # Search 的 options 字段参与候选召回
    "exact_scan": False, # FTS 无命中时的兜底 LIKE 全扫（代价高，默认关闭）
    "vector": False,     # 可选向量检索：环境无可用依赖时恒为 False
    # 覆写检测（supersession）：在候选集内找「话题高度重合但更晚」的消息对，
    # 判定后者覆写前者，抬高后者、轻降前者。与 temporal_intent 的区别是
    # 它不做全局新近度放大，只在**成对冗余**成立时局部生效，因此不会误伤
    # 「问当前状态但答案本身较旧」的查询。默认值由 Phase D 跨 seed 消融决定，
    # v1.1 只启用“覆写 + 显式更新保护”的组合；无保护的覆写仍是不安全对照。
    # medium/mixed 三 seed 下，保守 4/1 权重保持 Recall@20 不退、Recall@100=1，
    # MRR 每个 seed 均提升。官方数据上的影响仍 unknown，见 docs/EVAL.md 附录 E。
    "supersession": True,
    # 显式更新保护：仅允许带通用更新语义（如“更新为 / no longer”）的较新消息
    # 覆写旧消息。它只在 supersession 同时开启时生效；v1.1 将两者组合启用。
    "supersession_update_guard": True,
    # 个性化证据来源（v1.3 回退默认关闭）：偏好类查询中轻度抬高用户本人直接陈述。
    # 0eeca8b 曾默认启用，声称 PersonaMem-v2 MRR +0.261——同样本配对（50/100/200）
    # 证明该增益是样本方差伪信号，7fe9b4b 回退；代码保留可消融（L10 对照组）。
    "preference_role_boost": False,
    # 事件序号塑形（S2，默认关闭）：顺序意图查询（has_ordering_intent）时，
    # 返回的 message 视图按事件时间升序重排并加 [事件N] 序号前缀，帮助答案模型
    # 直接按时间线组织事件。只影响该意图查询的响应内容与顺序，不改原文与排序
    # 特征；探针证据：BEAM event_ordering 上避免拒答并列出事件（见 DECISIONS.md）。
    "ordering_prefix": False,
    # 时间线排序（S4，默认关闭）：时间上下文查询（has_temporal_context）时，
    # 返回结果按事件时间升序重排（不加前缀），多跳时间推理的输入顺序优化。
    # 与 ordering_prefix 共用排序基建；两者同时开启时以前者为准。
    "chrono_ordering": False,
    # 视图过滤（v1.4 候选，默认关闭）：Search 只返回 message 原始消息视图，
    # 丢弃 window/session-segment 聚合块。动机（LoCoMo-Refined e2e 诊断）：
    # 聚合视图占据 top-k 前位但无时间锚，ts_prefix 只对 message 视图生效，
    # 导致答案模型读到相对时间文本；message 原文带 [日期] 前缀且更短。
    # 证据集合不变（消息全保留），仅去除冗余聚合；locomo10 检索 R@100 不变。
    "message_view_only": False,
    # vNext：实体消歧后的软提升。默认关闭，先通过离线消融确认净收益。
    "entity_boost_v2": False,
    # 时间兜底（v1.2 默认启用）：当 ts_ms 缺失时，按正文时间表达／会话锚点／
    # created_at 逐层兜底。locomo10 全量门禁：R@20 +0.006 / R@100 +0.004 /
    # MRR +0.001；端到端（DeepSeek 官网 flash+pro）0.5758 vs 基线 0.5724。
    "temporal_fallback": True,
    # 优化（消融中）：当某条聚合视图的全部来源消息都已出现在保留结果中时，
    # 丢弃该视图（原文证据优先级高于冗余视图）。端到端实测 −0.0067，不启用。
    "dedup_views_by_sources": False,
    # 优化（消融中）：per-user 稠密检索通道（fastembed + bge-small-en-v1.5）。
    # 默认关闭；后端不可用/超时自动回退纯确定性路径。仅存 message/view ID，
    # 按 user_id 隔离，绝不把不同用户的向量合并进共享候选池。全权重网格门禁
    # 均劣于基线，冻结关闭。
    "dense": False,
    # 稠密索引是否包含聚合视图（默认 False：只嵌原始消息，视图内容蕴含在
    # 消息里，嵌入长拼接视图会显著拖慢 Add）。只影响索引内容，不影响语义。
    "dense_index_views": False,
    # nomic-embed-text 稠密检索通道（ollama 本地，默认关闭）。与 dense (fastembed
    # bge-small) 不同：使用本地 ollama 的 nomic-embed-text 模型（768 维，1.82s/批，
    # 区分力 0.93 vs bge-small 0.39）。后端不可用时静默回退纯确定性路径。作为第 3
    # 路 RRF 通道与词法/特征路融合。向量按 user_id 隔离，存于独立 nomic_vectors 表。
    "dense_nomic": False,
    # 返回 content 前给证据加 [事件时间] 前缀（v1.2 默认启用；日期级，不改
    # 原文、不改排序）。官方答案指令允许“memory timestamp 明确时把相对时间
    # 转成日期”，而 Search 只把 content 喂给答案模型。端到端（DeepSeek 官网
    # flash+pro，297 项）实测 0.6229 vs 基线 0.5724（+0.0505），时间类
    # 问题（cat1 +0.116 / cat2 +0.271）大幅提升。粒度不高于 day，绝不伪造精度。
    "content_timestamp_prefix": True,
    # v1.2.1：前缀默认**仅对含时间/日期意图的查询**生效（消融证据：无条件
    # 前缀在非时间类问题小幅回退，cat4 −0.008 / cat5 −0.030）。设 True 恢复
    # v1.2 的无条件行为（供对照消融）。
    "content_timestamp_prefix_unconditional": False,
    # v1.4 D 修复（默认关闭，消融中）：冲突成对返回。检测同话题、相反极性
    # （一肯定一否定）的消息对，检测到即同时提升两条，确保成对返回，让答案模型
    # 看到矛盾而非只取其一。对齐合规冲榜路线"无法确定更新还是矛盾时保留 conflicted
    # 并把两个证据一起返回"。针对 BEAM contradiction_resolution（本地实测 0.000）。
    "conflict_pair_return": False,
    # v1.4 H 修复（默认关闭，消融中）：低置信弃权。当检索结果与查询无任何 token
    # 重合（无相关证据）时返回空证据集，让答案模型对无证据问题弃权而非编造。
    # 对齐合规冲榜路线"对没有证据的问题返回无法确定所需的空证据状态"。
    "low_confidence_abstain": False,
    # v1.4 D 修复（默认关闭，消融中）：current-value 查询新近度。对"问某属性当前
    # 取值"的查询（how many / what is my / when is my，且无过去时间标记）抬高新近度
    # 权重，使最新版本排前。针对 BEAM knowledge_update 失败诊断：34% 失败是检索返回
    # 旧版本（gold 165 commits 只返回 150）。v1.3 把 plain 查询弱化到 recency=2.0，
    # 此类查询被误归 plain 档。预期增益小（折算总分 <1 分），需门禁防回归。
    "current_value_recency": False,
    # Ebbinghaus 指数遗忘曲线（授权数据门禁通过，已提升为默认）：把相对新近度从候选集内线性归一
    # (epoch-ts_min)/ts_span 换成指数衰减 exp(-λ·Δt)，Δt 为该事实距候选集内最新事件
    # 的天数（以最新事件为「上次访问」参考点），使陈旧事实自然衰减。锚定候选集而非
    # 墙钟：评测不注入查询时刻，绝对墙钟会让整批老语料的衰减值塌缩到 ~0 而丧失区分力，
    # 且结果随运行日期漂移不可复现。授权数据门禁：LoCoMo +0.0026 MRR / BEAM +0.0144 MRR。
    "ebbinghaus_decay": True,
    # Consolidation N->1 deterministic dedup（授权数据门禁通过，已提升为默认）。
    # Add 阶段：当新消息的话题指纹与 ≥N 条已有活跃消息匹配（同一时间窗内）时，合并为
    # 一条摘要视图（保留最新内容 + 全部来源 ID 并集），归档原始消息（标记 consolidated=1
    # 并从 FTS 移除，不再作为独立候选）。判定纯结构性（token containment + 时间窗），
    # 完全确定性、可复现。动机：同主题冗余事实堆积时用一条摘要替代 N 条噪声，降低 top-k
    # 冗余。召回安全：摘要 source_ids 含全部原始消息 ID，gold 按 source_message_ids
    # 判定仍命中；原始行保留于 messages 表供审计/回溯。授权数据门禁：BEAM +10.7% R@20。
    "consolidation_dedup": True,
    # LLM 事实抽取（scnet Kimi-K2.5，默认关闭）。Add 阶段调用 scnet API 从消息中抽取
    # 结构化事实三元组（subject/predicate/object/time），存入独立 facts 表（与现有 FTS5
    # 并列）。Search 阶段对 knowledge_update/temporal 查询额外检索事实表，对事实命中的
    # 证据做分数提升，并补充词法召回遗漏的事实匹配消息。API 不可用时（无 SC_API_KEY、
    # 超时、响应异常）静默回退纯词法路径——与关闭 flag 观察等价，只是不会出现 fact_match
    # 证据标记。凭据从环境变量 SC_API_KEY / SC_API_BASE 读取，绝不硬编码。
    "fact_extraction": False,
    # v1.4 候选：实体边图 BFS 扩展（默认关闭）。Add 阶段用 features.extract_entities 提取
    # 每条消息的实体，在 entity_edges 表中建立共享实体的邻接边（src_id, dst_id, shared_entity,
    # weight）。Search 阶段若查询含实体，从已召回的消息种子出发做 BFS（深度 graph_max_depth），
    # 收集未召回的邻居消息 ID 作为额外候选并加分提升。动机：词法召回只命中直接匹配查询
    # token 的消息，而通过共享实体的图邻居（如"Alice 工作在 Project Alpha"→"Project Alpha
    # 用 Python"）可能携带答案但未被 FTS 召回。纯确定性、零新依赖（复用现有实体提取）。
    "entity_graph": False,
    # 知识图谱多跳桥接（kg_graph，默认关闭，消融中）。与 entity_graph（消息-消息邻接边
    # BFS 扩展）不同：kg_graph 在 entity-entity 层建图——Add 阶段对每条消息用
    # features.extract_entities 提取实体，在同一消息内共现的实体对之间建立无向边，存入
    # kg_edges(entity_a, entity_b, message_id, user_id)。Search 阶段若查询含 ≥2 个实体，
    # 找出「桥接实体」——即与 ≥2 个查询实体分别在不同消息中共现的实体（HippoRAG PPR /
    # YourMemory entity graph 的简化版），把这些桥接实体所连接的消息作为额外候选召回并
    # 加分提升。动机：多跳问题（"Alice 和 Bob 通过什么项目联系？"）的答案散落在多条
    # 消息里，词法召回只命中直接匹配查询 token 的消息，而桥接实体（如共享的 Project X）
    # 把分散证据串成推理链。纯确定性、零新依赖（复用 features.extract_entities）。
    "kg_graph": False,
    # Entity-level functional contradiction resolution（默认关闭，消融中）。
    # Search 阶段：按主实体分组候选消息，检测同实体同谓词的值矛盾（不同数值/日期）
    # 或极性矛盾（一肯定一否定 via has_negation），标记较早的消息为 superseded。
    # 对 current-value 查询（has_current_value_intent / has_temporal_intent）从结果中
    # 过滤 superseded 消息，使答案模型只看到最新值。与 supersession 的区别：
    # supersession 基于话题指纹重合 + 时间先后，只在 temporal_intent 查询时做软提升/轻降；
    # 本方法基于实体级谓词匹配（排除实体/值 token 后的谓词签名），更精确地定位功能矛盾，
    # 且对 current-value 查询做硬过滤（删除而非降权）。
    "entity_contradiction": False,
    # entity_contradiction_filter（默认关闭，消融中）：实体级极性矛盾过滤。
    # Search 阶段：按主实体分组候选消息，检测同实体同谓词（排除实体/值 token 后的
    # 谓词签名重合 ≥ entity_contradiction_min_overlap）且极性相反（一肯定一否定
    # via has_negation）的消息对，标记较早的消息为 superseded。对 current-value
    # 查询（has_current_value_intent）从结果中硬过滤 superseded 消息，使答案模型
    # 只看到最新值；对非 current-value 查询仅提升较新消息分数（不过滤）。
    # 与 entity_contradiction 的区别：后者是更宽泛的占位（含数值/日期矛盾），
    # 本 flag 专注于极性矛盾（has_negation）的精确检测与硬过滤。复用
    # entity_contradiction_min_overlap / entity_contradiction_weight 参数。
    "entity_contradiction_filter": False,
    # date_channel（默认关闭，消融中）：日期窗口检索通道。当查询含显式日期时，
    # 额外检索 event_time 落在该日期窗口内的消息（不依赖词法匹配），作为独立的
    # RRF 通道与词法/特征路融合。针对词法漏召回但时间匹配的日期类问题。
    "date_channel": False,
    # recency_sort（默认关闭，消融中）：对 current-value / temporal 查询，
    # 将证据按 event_time DESC 硬排序后取 head。当前状态问题的答案是最新的
    # 陈述，硬排序比软权重更直接地保证最新证据进入 top-k 前位。
    "recency_sort": False,
}


@dataclass
class RetrieverConfig:
    # 存储
    db_path: str = ":memory:"

    # 视图构造（确定性、可配置）
    window_size: int = 3
    window_overlap: int = 1
    segment_max_messages: int = 12
    segment_max_gap_seconds: int = 1800

    # 检索
    top_k_default: int = 10
    top_k_max: int = 100          # 官方正式评测固定 100
    max_candidates: int = 400     # FTS 候选上限，界定单次 Search 的最坏代价
    max_query_tokens: int = 64    # 查询 token 上限，界定 FTS 查询规模
    message_slot_ratio: float = 0.5  # top_k 中保底留给原始消息的比例
    # 聚合视图（window/session-segment）在最终 top_k 中的最大占比。
    # 1.0 表示不额外限制（v1.1 行为，只靠 message_slot_ratio 保底 50% 消息）；
    # 调低会把更多原始消息挤进前 top_k，代价是视图的邻接上下文变少。
    # 该值只在 dedup_views_by_sources 关闭时仍生效于未被去重的视图。
    view_max_ratio: float = 1.0

    # 稠密通道参数（flags["dense"] 开启时生效）
    dense_top_n: int = 80          # 稠密通道候选数
    dense_rrf_weight: float = 0.5  # 三路 RRF 中稠密通道的权重（词法 0.1 / 特征 1.0）
    dense_model: str = "BAAI/bge-small-en-v1.5"

    # nomic 稠密通道参数（仅在 flags["dense_nomic"] 开启时生效）
    # nomic_model       : ollama 模型名（本地 nomic-embed-text，768 维，1.82s/批）
    # nomic_ollama_url  : ollama API 基址（/api/embed 端点，支持批量输入）
    # nomic_rrf_weight  : RRF 融合中 nomic 通道的权重（与词法/特征/稠密/日期路并列）
    # nomic_top_n       : nomic 通道返回的候选数（界定 Search 热路径成本）
    # nomic_timeout     : ollama API 超时秒数；超时静默回退，不阻塞 Add/Search
    nomic_model: str = "nomic-embed-text"
    nomic_ollama_url: str = "http://127.0.0.1:11434"
    nomic_rrf_weight: float = 0.5
    nomic_top_n: int = 80
    nomic_timeout: float = 30.0

    # 加权 RRF 融合参数（仅在 flags["rrf"] 开启时生效）。
    # 权重扫描（docs/EVAL.md 附录 A）显示：w_lexical 越大 MRR 越高、Recall@20 越低。
    # 默认取 0.1 —— 唯一「MRR 有增益且 Recall@20 不退化」的取值；
    # 若下游更看重排序而非 20 条内召回，可上调至 0.25/0.5（已实测，非默认）。
    rrf_k: int = 60
    rrf_weight_feature: float = 1.0
    rrf_weight_lexical: float = 0.1

    # 相对新近度权重。把候选集内时间戳归一到 [0,1] 后加权，
    # 取代原先的绝对年龄项（语料整体偏老时绝对年龄会退化成常数）。
    # intent 档仅在 flags["temporal_intent"] 开启时生效，实测收益为负，故默认不启用。
    recency_weight: float = 8.0
    recency_weight_intent: float = 12.0
    # v1.3：非时间上下文查询的新近度权重（BEAM 实证：长对话中 recency=8 会把
    # 无关的最新消息顶到相关旧消息之上，如 "When does my first sprint end?" 返回
    # 一堆 Flask 无关消息；普通查询改用低权重，时间查询保持 recency_weight）。
    recency_weight_plain: float = 2.0
    # v1.4 D 修复：current-value 查询的新近度权重（current_value_recency 开启时生效）。
    # 介于 plain(2.0) 与 temporal(8.0) 之间：既要抬最新值，又不至于像 temporal 那样
    # 把无关最新消息顶过相关旧证据。具体取值需跨 seed 门禁确定。
    recency_weight_current_value: float = 5.0

    # Ebbinghaus 指数遗忘的衰减常数 λ（仅在 flags["ebbinghaus_decay"] 开启时生效）。
    # recency = exp(-λ · Δt_days)，Δt 为事实距候选集最新事件的天数。λ 越大陈旧事实
    # 衰减越快：0.1/天 时约 7 天衰减到 ~0.50、30 天到 ~0.05。需跨 seed 门禁确定。
    decay_lambda: float = 0.1

    # Consolidation N->1 参数（仅在 flags["consolidation_dedup"] 开启时生效）。
    # consolidation_min_cluster: 触发合并所需的最少已有匹配消息数 N（新消息 + N 已有 → 1 摘要）。
    # consolidation_time_window_seconds: 只有事件时间在该窗口内（|Δt| ≤ 窗口）的已有消息才参与匹配。
    #   0 表示不限时间窗（任意时间均可合并，慎用——会把跨期同主题消息误并）。
    # consolidation_min_overlap: 话题指纹重合阈值（containment = 交集 / 较短一方 token 数，
    #   token 取长度≥2 的 n-gram，与 supersession 同一指纹基建）。
    # consolidation_max_scan: 每条新消息扫描的已有活跃消息上限（界定 Add 热路径最坏代价）。
    consolidation_min_cluster: int = 3
    consolidation_time_window_seconds: int = 604800  # 7 天
    consolidation_min_overlap: float = 0.5
    consolidation_max_scan: int = 1000

    # LLM 事实抽取参数（仅在 flags["fact_extraction"] 开启时生效）。
    # fact_boost_weight         : 事实命中的证据分数提升量（在 RRF 融合前的特征路生效）。
    # fact_extraction_model     : scnet API 模型名（团队验证 Kimi-K2.5，1.52s/消息）。
    # fact_extraction_timeout   : API 超时秒数；超时静默回退，不阻塞 Add。
    # fact_max_per_message      : 每条消息最多抽取/存储的事实数（控制 Add 延迟与表体积）。
    # fact_extra_candidates     : Search 时从事实表补充的、词法召回遗漏的最大消息数。
    # 凭据 OPEN_API_KEY / OPEN_API_BASE 从环境变量读取，不在此处配置。
    fact_boost_weight: float = 25.0
    fact_extraction_model: str = "muse-spark-1.2-contributor"
    fact_extraction_timeout: float = 30.0
    fact_max_per_message: int = 8
    fact_extra_candidates: int = 10

    # 实体边图 BFS 扩展参数（仅在 flags["entity_graph"] 开启时生效）。
    # graph_max_depth     : BFS 最大深度（1=只找直接邻居，2=两跳邻居）。
    # graph_max_expansion : BFS 最多补充的额外候选数（界定 Search 热路径成本）。
    # graph_boost_weight  : 图扩展候选的分数提升量（深度越深提升越小：weight/depth）。
    graph_max_depth: int = 2
    graph_max_expansion: int = 5
    graph_boost_weight: float = 15.0

    # 知识图谱多跳桥接参数（仅在 flags["kg_graph"] 开启时生效）。
    # kg_max_entities_per_message : 每条消息参与建边的实体上限（取 extract_entities 前 N 个，
    #   控制全配对边数 O(N^2) 的最坏体积；CJK n-gram 噪声多，截断避免边表爆炸）。
    # kg_max_bridge_messages      : 桥接实体连接的消息作为额外候选召回的最多条数（界定
    #   Search 热路径成本；仅在 FTS 未召回时才补充）。
    # kg_bridge_boost_weight      : 桥接消息的分数提升量（在 RRF 融合前的特征路生效）。
    #   连接的查询实体越多，提升越大（weight × min(connected_query_entities, 3) / 3）。
    kg_max_entities_per_message: int = 16
    kg_max_bridge_messages: int = 10
    kg_bridge_boost_weight: float = 15.0

    # 覆写检测参数（仅在 flags["supersession"] 开启时生效）。
    # min_overlap 是两条消息「谈的是同一件事」的判定阈值，按 containment
    # （交集 / 较短一方的 token 数，token 取长度≥2 的 n-gram）计算。
    # max_pairs 限制参与两两比较的候选数，界定 O(n^2) 的最坏代价。
    # v1.1 三 seed 参数扫描的 Pareto 安全点：4/1 保持 Recall@20 不退化，
    # 同时稳定提升 MRR；14/4 与 18/6 的增益更大但会在一个 seed 上损伤 Recall@20。
    supersession_weight: float = 4.0
    supersession_penalty: float = 1.0
    supersession_min_overlap: float = 0.5
    supersession_max_pairs: int = 40

    # 偏好类查询中，“role=user + 第一人称偏好陈述”的软加权。
    # 只改变排序，不过滤任何候选，且在 RRF 前进入特征路排序。
    preference_role_weight: float = 14.0

    # v1.4 D 修复：冲突成对返回的提升权重与话题重合阈值（conflict_pair_return 开启时生效）。
    conflict_pair_weight: float = 3.0
    conflict_pair_min_overlap: float = 0.4

    # v1.4 H 修复：低置信弃权阈值（low_confidence_abstain 开启时生效）。
    # 最佳候选的查询 token 覆盖率低于该值时返回空证据集（弃权）。
    abstain_min_coverage: float = 0.0

    # vNext：实体消歧后的软提升。仅在 flags["entity_boost_v2"] 开启时生效。
    entity_disambiguation_weight: float = 35.0
    entity_cooccurrence_weight: float = 20.0

    # Entity-level contradiction 参数（仅在 flags["entity_contradiction"] 开启时生效）。
    # entity_contradiction_min_overlap: 同实体下两条消息的谓词签名重合阈值
    #   （containment = 交集 / 较短一方 token 数；token 取长度≥2 且排除实体与值 token）。
    #   低于此阈值视为不同谓词（如 budget vs phone number），不判矛盾。
    # entity_contradiction_weight: 矛盾检测中较新消息（winner）的分数提升量，
    #   使其在非 current-value 查询中也能排前；superseded 消息对 current-value
    #   查询被硬过滤，不需要额外降权。
    entity_contradiction_min_overlap: float = 0.4
    entity_contradiction_weight: float = 4.0

    # date_channel 参数（仅在 flags["date_channel"] 开启时生效）。
    # date_channel_rrf_weight    : RRF 融合中日期通道的权重（与词法/特征/稠密路并列）。
    # date_window_padding_days   : 日期窗口两侧扩展的天数（0 = 精确到当天 [00:00, 次日00:00)）。
    # date_channel_max_candidates: 日期通道返回的最大候选数（界定 SQL 扫描成本）。
    date_channel_rrf_weight: float = 0.5
    date_window_padding_days: int = 0
    date_channel_max_candidates: int = 50

    # vNext P2：仅对基础特征排名靠前的无 ts_ms 候选执行完整相对时间兜底。
    # 0 表示不限制；默认 80，保持候选覆盖同时控制 Search 热路径成本。
    temporal_fallback_top_n: int = 80  # 兼容旧配置：日期值查询的默认上限
    temporal_fallback_top_n_temporal: int = 40  # “当前/最新”但未必索要日期
    temporal_fallback_top_n_other: int = 8      # 普通查询仅保留极小的安全预算

    # 一致性 / 并发
    busy_timeout_ms: int = 10000
    write_retries: int = 5
    pool_size: int = 24  # 读连接池容量（官方 Search 并发默认 32，留出借还余量）

    # HTTP wrapper
    host: str = "127.0.0.1"
    port: int = 8080
    auth_mode: str = "none"       # none | bearer | token | x-api-key
    api_key: str = ""
    add_path: str = "/add"
    search_path: str = "/search"
    health_path: str = "/health"

    flags: dict = field(default_factory=lambda: dict(DEFAULT_FLAGS))

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, path: str | None = None) -> "RetrieverConfig":
        cfg = cls()
        path = path or os.environ.get("AML_CONFIG") or ""
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                cfg = cfg.merge(json.load(fh))

        env_map = {
            "AML_DB_PATH": ("db_path", str),
            "AML_WINDOW_SIZE": ("window_size", int),
            "AML_WINDOW_OVERLAP": ("window_overlap", int),
            "AML_SEGMENT_MAX_MESSAGES": ("segment_max_messages", int),
            "AML_SEGMENT_MAX_GAP_SECONDS": ("segment_max_gap_seconds", int),
            "AML_TOP_K_DEFAULT": ("top_k_default", int),
            "AML_TOP_K_MAX": ("top_k_max", int),
            "AML_MAX_CANDIDATES": ("max_candidates", int),
            "AML_RRF_K": ("rrf_k", int),
            "AML_RRF_W_FEATURE": ("rrf_weight_feature", float),
            "AML_RRF_W_LEXICAL": ("rrf_weight_lexical", float),
            "AML_RECENCY_W": ("recency_weight", float),
            "AML_RECENCY_W_INTENT": ("recency_weight_intent", float),
            "AML_DECAY_LAMBDA": ("decay_lambda", float),
            "AML_CONSOLIDATION_MIN_CLUSTER": ("consolidation_min_cluster", int),
            "AML_CONSOLIDATION_TIME_WINDOW": ("consolidation_time_window_seconds", int),
            "AML_CONSOLIDATION_MIN_OVERLAP": ("consolidation_min_overlap", float),
            "AML_CONSOLIDATION_MAX_SCAN": ("consolidation_max_scan", int),
            "AML_FACT_BOOST_WEIGHT": ("fact_boost_weight", float),
            "AML_FACT_EXTRACTION_MODEL": ("fact_extraction_model", str),
            "AML_FACT_EXTRACTION_TIMEOUT": ("fact_extraction_timeout", float),
            "AML_FACT_MAX_PER_MESSAGE": ("fact_max_per_message", int),
            "AML_FACT_EXTRA_CANDIDATES": ("fact_extra_candidates", int),
            "AML_GRAPH_MAX_DEPTH": ("graph_max_depth", int),
            "AML_GRAPH_MAX_EXPANSION": ("graph_max_expansion", int),
            "AML_GRAPH_BOOST_WEIGHT": ("graph_boost_weight", float),
            "AML_KG_MAX_ENTITIES_PER_MESSAGE": ("kg_max_entities_per_message", int),
            "AML_KG_MAX_BRIDGE_MESSAGES": ("kg_max_bridge_messages", int),
            "AML_KG_BRIDGE_BOOST_WEIGHT": ("kg_bridge_boost_weight", float),
            "AML_ENTITY_DISAMBIGUATION_W": ("entity_disambiguation_weight", float),
            "AML_ENTITY_COOCCURRENCE_W": ("entity_cooccurrence_weight", float),
            "AML_ENTITY_CONTRADICTION_MIN_OVERLAP": ("entity_contradiction_min_overlap", float),
            "AML_ENTITY_CONTRADICTION_WEIGHT": ("entity_contradiction_weight", float),
            "AML_DATE_CHANNEL_RRF_WEIGHT": ("date_channel_rrf_weight", float),
            "AML_DATE_WINDOW_PADDING_DAYS": ("date_window_padding_days", int),
            "AML_DATE_CHANNEL_MAX_CANDIDATES": ("date_channel_max_candidates", int),
            "AML_NOMIC_MODEL": ("nomic_model", str),
            "AML_NOMIC_OLLAMA_URL": ("nomic_ollama_url", str),
            "AML_NOMIC_RRF_WEIGHT": ("nomic_rrf_weight", float),
            "AML_NOMIC_TOP_N": ("nomic_top_n", int),
            "AML_NOMIC_TIMEOUT": ("nomic_timeout", float),
            "AML_TEMPORAL_FALLBACK_TOP_N": ("temporal_fallback_top_n", int),
            "AML_TEMPORAL_FALLBACK_TOP_N_TEMPORAL": ("temporal_fallback_top_n_temporal", int),
            "AML_TEMPORAL_FALLBACK_TOP_N_OTHER": ("temporal_fallback_top_n_other", int),
            "AML_HOST": ("host", str),
            "AML_PORT": ("port", int),
            "AML_AUTH_MODE": ("auth_mode", str),
            "AML_API_KEY": ("api_key", str),
            "AML_ADD_PATH": ("add_path", str),
            "AML_SEARCH_PATH": ("search_path", str),
            "AML_HEALTH_PATH": ("health_path", str),
        }
        for env_key, (attr, caster) in env_map.items():
            raw = os.environ.get(env_key)
            if raw is not None and raw != "":
                try:
                    setattr(cfg, attr, caster(raw))
                except (TypeError, ValueError):
                    pass

        # AML_FLAG_<NAME>=0/1 覆盖单个开关
        for key, value in os.environ.items():
            if key.startswith("AML_FLAG_"):
                name = key[len("AML_FLAG_") :].lower()
                if name in cfg.flags:
                    cfg.flags[name] = value.strip().lower() in ("1", "true", "yes", "on")
        return cfg

    def merge(self, data: dict) -> "RetrieverConfig":
        out = RetrieverConfig(**{**asdict(self)})
        out.flags = dict(self.flags)
        for key, value in (data or {}).items():
            if key == "flags" and isinstance(value, dict):
                for flag_name, flag_value in value.items():
                    if flag_name in out.flags:
                        out.flags[flag_name] = bool(flag_value)
            elif hasattr(out, key):
                setattr(out, key, value)
        return out

    def with_flags(self, **overrides) -> "RetrieverConfig":
        """返回一个只改动 flag 的副本（消融实验用）。"""
        out = RetrieverConfig(**{**asdict(self)})
        out.flags = dict(self.flags)
        for name, value in overrides.items():
            out.flags[name] = bool(value)
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["api_key"] = "***" if self.api_key else ""  # 不外泄密钥
        return data


def vector_backend_available() -> tuple[bool, str]:
    """探测运行环境是否已有可用向量依赖。

    严格只做探测，绝不安装依赖、绝不联网下载模型。
    返回 (available, reason)。
    """
    for module in ("numpy", "sentence_transformers", "faiss"):
        try:
            __import__(module)
        except Exception:
            continue
        else:
            return True, f"{module} importable"
    return False, "no local vector dependency (numpy/sentence_transformers/faiss) importable"


__all__ = ["RetrieverConfig", "DEFAULT_FLAGS", "vector_backend_available"]
