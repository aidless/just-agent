"""AML Retriever — 多视图混合证据检索引擎（零依赖、可消融、线程安全）。

设计要点
--------
存储：SQLite + FTS5。原始消息**全量保存且永远独立可检索**；聚合视图只作为
额外证据通道，带 source_message_ids 回指，绝不替换原始消息。

一致性：
  - WAL + busy_timeout + 进程内写锁 + 有限重试，支持 Add 高并发写。
  - Add 在返回前完成 commit，**同步写后立即可 Search**（官方硬性要求）。
  - 每个线程持有独立 sqlite3 连接（sqlite3 连接非线程安全）。

检索（每条增强都是可独立关闭的 flag，用于离线消融）：
  lexical(基线) / views / exact / datenum / entity / rerank / rrf / dedup / vector(可选)

隔离：所有读写都以 user_id 为范围；session_id 只用于组织记忆，不作为检索筛选条件。
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import features
from .config import RetrieverConfig
from .dense import DenseIndex, backend_available
from .entity_disambiguate import apply_entity_boost_v2
from .temporal_fallback import (
    TimeConfidence, confidence_to_recency_weight, extract_partial_month_day,
    has_temporal_expression, parse_absolute_temporal, resolve_stored_month_day, resolve_temporal,
)
from .views import (
    affected_window_starts,
    join_content,
    scope_key,
    segment_view_id,
    window_view_id,
)

# ------------------------------------------------------------------ 打分权重
W_LEXICAL = 1.0        # FTS5 bm25 归一化后的词法基线权重
W_COVERAGE = 40.0      # 查询 token 覆盖率
W_EXACT_FULL = 120.0   # 整句精确子串命中
W_PHRASE = 60.0        # 引号精确短语命中
W_DATE = 45.0          # 日期精确命中
W_NUMBER = 25.0        # 数字精确命中
W_ENTITY = 12.0        # 实体式 token 命中（按命中数封顶）
W_VIEW_CONTEXT = 4.0   # 聚合视图的邻接上下文加成
W_ADJACENT = 6.0       # 候选在会话中相邻（证据簇）
# 新近度用「候选集内相对位置」而非绝对年龄：语料整体偏老时，
# 绝对年龄项会退化成常数，完全失去区分力（离线证据见 docs/EVAL.md）。
W_RECENCY = 8.0        # 相对新近度基础权重（同分时更偏向新证据）
W_RECENCY_INTENT = 55.0  # 查询含时间意图时的相对新近度权重
W_CONFLICT_LATEST = 10.0  # 疑似状态冲突时，更偏向较新的那条
RRF_K = 60


@dataclass
class AddResult:
    request_id: str
    user_id: str
    session_id: str | None
    message_ids: list[str] = field(default_factory=list)
    idempotent: bool = False
    status: str = "ok"


@dataclass
class Evidence:
    id: str
    user_id: str
    view: str
    content: str
    created_at: str
    score: float
    source_message_ids: list[str] = field(default_factory=list)
    provenance: str = ""
    evidence_flags: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    request_id: str | None
    total: int
    results: list[Evidence] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_ms(ts_ms) -> str | None:
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _msg_id(user_id: str, session_id: str | None, seq: int) -> str:
    raw = f"{user_id}\x00{session_id or ''}\x00{seq}".encode("utf-8")
    return "m_" + hashlib.sha1(raw).hexdigest()[:16]


def _dedup_impl(ordered: list[dict], *, drop_views_by_sources: bool) -> list[dict]:
    """候选去重：内容完全相同的记录只保留一条。

    drop_views_by_sources=True 时，聚合视图若其全部来源消息都已被保留，
    则丢弃该视图（原文证据优先于冗余视图）。召回安全性：gold 按
    source_message_ids 判定，来源消息在列表中就命中，因此这不降低 Recall@K。
    """
    seen_hash: set[str] = set()
    kept: list[dict] = []
    covered: list[set] = []
    kept_msg_ids: set[str] = set()
    for rec in ordered:
        digest = hashlib.sha1((rec["content"] or "").encode("utf-8")).hexdigest()
        if digest in seen_hash:
            continue
        sources = set(rec["source_ids"])
        # 聚合视图若已被某条保留视图完全覆盖，则丢弃（原始消息永不丢弃）；
        # consolidation 摘要视同原始消息保护——其来源已被归档，不可被覆盖丢弃
        if rec["view"] not in ("message", "consolidation") and any(sources <= c for c in covered):
            continue
        # 优化开关：视图的所有来源消息都已保留时，视图是纯冗余
        if (
            drop_views_by_sources
            and rec["view"] not in ("message", "consolidation")
            and sources
            and sources <= kept_msg_ids
        ):
            continue
        seen_hash.add(digest)
        if rec["view"] not in ("message", "consolidation"):
            covered.append(sources)
        else:
            kept_msg_ids.add(rec["id"])
        kept.append(rec)
    return kept


def _consolidation_view_id(user_id: str, source_ids: list[str]) -> str:
    """合并摘要视图的确定性 ID：以 (user_id, 排序后的全部来源消息 ID) 为指纹。

    相同来源集合 → 相同 view_id，保证合并幂等（重复请求/批内二次触发不会产生重复摘要）。
    """
    raw = f"{user_id}\x00cons\x00{','.join(source_ids)}".encode("utf-8")
    return "c_" + hashlib.sha1(raw).hexdigest()[:16]


class RetrieverDB:
    """存储 + 检索引擎。线程安全：每线程独立连接，写操作串行化。"""

    def __init__(self, config: RetrieverConfig | None = None, db_path: str | None = None):
        self.config = config or RetrieverConfig()
        if db_path is not None:
            self.config.db_path = db_path
        self.db_path = self.config.db_path
        self.flags = dict(self.config.flags)

        self._write_lock = threading.RLock()
        # 有界连接池：ThreadingHTTPServer 每请求一个线程，thread-local 连接会持续泄漏 fd，
        # 因此改为借还式连接池，归还时超出容量的连接直接关闭。
        self._pool: queue.LifoQueue = queue.LifoQueue(maxsize=max(1, self.config.pool_size))
        # 内存库必须共享同一连接，否则各线程看到不同的空库；共享连接需串行访问。
        self._mem_lock = threading.RLock()
        self._shared_mem_con: sqlite3.Connection | None = None
        if self.db_path == ":memory:":
            self._shared_mem_con = self._new_connection()

        self._dense: DenseIndex | None = None
        self._fact_ext = None  # 惰性初始化 LLM 事实抽取器（仅 flags["fact_extraction"] 开启时）

        self._init_schema()

    # ------------------------------------------------------------- connection
    def _dense_index(self) -> DenseIndex | None:
        """惰性初始化稠密索引（仅 flags["dense"] 开启时）。"""
        if not self.flags.get("dense", False):
            return None
        ok, _ = backend_available()
        if not ok:
            return None
        if self._dense is None:
            self._dense = DenseIndex(self)
        return self._dense

    def _new_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=self.config.busy_timeout_ms / 1000.0,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        try:
            if self.db_path != ":memory:":
                con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"PRAGMA busy_timeout={int(self.config.busy_timeout_ms)}")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA temp_store=MEMORY")
        except sqlite3.Error:
            pass
        return con

    @contextmanager
    def connection(self):
        """借出一个连接，用完归还。内存库退化为串行访问共享连接。"""
        if self._shared_mem_con is not None:
            with self._mem_lock:
                yield self._shared_mem_con
            return
        try:
            con = self._pool.get_nowait()
        except queue.Empty:
            con = self._new_connection()
        try:
            yield con
        finally:
            try:
                self._pool.put_nowait(con)
            except queue.Full:
                try:
                    con.close()
                except sqlite3.Error:
                    pass

    def _write(self, fn, *args, **kwargs):
        """串行化写入 + 有限重试（应对 SQLITE_BUSY）。"""
        last_error: Exception | None = None
        for attempt in range(max(1, self.config.write_retries)):
            try:
                with self._write_lock, self.connection() as con:
                    try:
                        result = fn(con, *args, **kwargs)
                        con.commit()
                        return result
                    except Exception:
                        con.rollback()
                        raise
            except sqlite3.OperationalError as exc:  # database is locked / busy
                last_error = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.02 * (2**attempt))
        raise last_error if last_error else RuntimeError("write failed")

    # ----------------------------------------------------------------- schema
    def _init_schema(self) -> None:
        def _do(con: sqlite3.Connection):
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    seq INTEGER NOT NULL,
                    role TEXT,
                    content TEXT NOT NULL,
                    ts_ms INTEGER,
                    abs_epoch REAL,
                    abs_granularity TEXT,
                    abs_expression TEXT,
                    partial_month INTEGER,
                    partial_day INTEGER,
                    partial_expression TEXT,
                    has_temporal_expr INTEGER,
                    created_at TEXT NOT NULL,
                    request_id TEXT,
                    added_at TEXT NOT NULL,
                    consolidated INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_msg_scope ON messages(user_id, session_id, seq);
                CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id);

                CREATE TABLE IF NOT EXISTS views(
                    view_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    view_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    start_seq INTEGER NOT NULL,
                    end_seq INTEGER NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_views_user ON views(user_id);

                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    message_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS sessions(
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    msg_count INTEGER NOT NULL DEFAULT 0,
                    last_ts_ms INTEGER,
                    seg_index INTEGER NOT NULL DEFAULT 0,
                    seg_start INTEGER NOT NULL DEFAULT 0,
                    seg_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, session_id)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                    text,
                    doc_id UNINDEXED,
                    user_id UNINDEXED,
                    doc_type UNINDEXED
                );

                -- LLM 事实抽取：独立事实表（与现有 FTS5 并列）。
                -- 每条事实是一条 (subject, predicate, object, time) 三元组，回指源消息。
                -- flags["fact_extraction"] 关闭时表为空、不影响检索；开启时由 Add 阶段填充。
                CREATE TABLE IF NOT EXISTS facts(
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    time_value TEXT,
                    time_epoch REAL,
                    created_at TEXT NOT NULL,
                    added_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_facts_user ON facts(user_id);
                CREATE INDEX IF NOT EXISTS idx_facts_msg ON facts(message_id);
                CREATE INDEX IF NOT EXISTS idx_facts_subj ON facts(user_id, subject);

                CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
                    fact_text,
                    fact_id UNINDEXED,
                    user_id UNINDEXED
                );
                """
            )
            # 兼容已有库：SQLite 没有 ADD COLUMN IF NOT EXISTS，故按 schema 自检后迁移。
            existing = {row["name"] for row in con.execute("PRAGMA table_info(messages)").fetchall()}
            for column, ddl in (
                ("abs_epoch", "REAL"),
                ("abs_granularity", "TEXT"),
                ("abs_expression", "TEXT"),
                ("partial_month", "INTEGER"),
                ("partial_day", "INTEGER"),
                ("partial_expression", "TEXT"),
                ("has_temporal_expr", "INTEGER"),
                ("consolidated", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    con.execute(f"ALTER TABLE messages ADD COLUMN {column} {ddl}")

        self._write(_do)

    # -------------------------------------------------------------------- Add
    def add(
        self,
        *,
        request_id: str,
        user_id: str,
        messages: list[dict],
        session_id: str | None = None,
    ) -> AddResult:
        """写入一批原始消息，同步建索引与聚合视图。返回后立即可 Search。"""
        if not request_id:
            raise ValueError("request_id is required")
        if not user_id:
            raise ValueError("user_id is required")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        normalized: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("each message must be an object")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("each message requires a non-empty content string")
            ts = item.get("timestamp", item.get("ts_ms"))
            if ts is not None:
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    raise ValueError("timestamp must be an integer (unix milliseconds)")
            # P1：仅对缺失原始 timestamp 的消息做一次绝对日期解析；该结果随内容
            # 一起持久化，后续 Search 不再重复扫描相同正文。
            absolute = parse_absolute_temporal(content) if ts is None else None
            partial = extract_partial_month_day(content) if ts is None and absolute is None else None
            normalized.append({
                "role": item.get("role") or "", "content": content, "ts_ms": ts,
                "abs_epoch": absolute.epoch if absolute else None,
                "abs_granularity": absolute.granularity if absolute else None,
                "abs_expression": absolute.original_expression if absolute else None,
                "partial_month": partial[0] if partial else None,
                "partial_day": partial[1] if partial else None,
                "partial_expression": partial[2] if partial else None,
                # 旧迁移数据为 NULL；Search 会保守地将 NULL 当作“尚未预筛”。
                "has_temporal_expr": int(has_temporal_expression(content)) if ts is None and absolute is None and partial is None else 0,
            })

        result = self._write(self._add_locked, request_id, user_id, session_id, normalized)
        # 幂等命中时 _add_locked 返回 (AddResult, None)；新写入返回 (AddResult, dense_docs)
        if isinstance(result, tuple) and len(result) == 2:
            add_result, dense_docs = result
            self._embed_dense_after_add(user_id, dense_docs)
            # LLM 事实抽取：事务提交后异步抽取（网络调用，不占用写锁）。
            # dense_docs = [(doc_id, content), ...]，复用已有的新消息内容列表。
            self._extract_facts_after_add(user_id, session_id, dense_docs)
            return add_result
        return result

    def _embed_dense_after_add(self, user_id: str, dense_docs: list[tuple[str, str]]) -> None:
        """事务提交后为新增文档嵌入向量（CPU 密集，避免占用写锁）。"""
        if not dense_docs:
            return
        dense = self._dense_index()
        if dense is None:
            return
        try:
            dense.add_docs(user_id, [d for d, _ in dense_docs], [c for _, c in dense_docs])
        except Exception:
            # 稠密通道失败不回滚已提交的写入；下次 Search 自动退回词法路径
            pass

    # -------------------------------------------------------- fact extraction
    def _fact_extractor(self):
        """惰性初始化 LLM 事实抽取器（仅 flags["fact_extraction"] 开启时）。"""
        if not self.flags.get("fact_extraction", False):
            return None
        if self._fact_ext is None:
            from .fact_extraction import FactExtractor
            self._fact_ext = FactExtractor.from_config(self.config)
        return self._fact_ext

    def _extract_facts_after_add(
        self, user_id: str, session_id: str | None, dense_docs: list[tuple[str, str]]
    ) -> None:
        """事务提交后为新增消息抽取 LLM 事实（网络调用，不占用写锁）。

        失败静默处理——API 不可用/超时/异常时无事实存入，检索自动回退纯词法路径。
        dense_docs = [(doc_id, content), ...]，复用 Add 已构建的新消息列表。
        """
        if not self.flags.get("fact_extraction", False):
            return
        if not dense_docs:
            return
        extractor = self._fact_extractor()
        if extractor is None:
            return  # API 未配置（SC_API_KEY 缺失等），优雅跳过
        facts_batch: list[tuple[str, str, str | None, object]] = []
        for doc_id, content in dense_docs:
            try:
                facts = extractor.extract_facts(content)
            except Exception:
                facts = []  # 任何失败 → 该消息无事实，不影响其他消息或整体 Add
            for fact in facts:
                facts_batch.append((doc_id, user_id, session_id, fact))
        if facts_batch:
            self._store_facts(facts_batch)

    def _store_facts(self, facts_batch: list[tuple[str, str, str | None, object]]) -> None:
        """持久化抽取的事实到 facts 表 + fts_facts。走写锁，保证线程安全。"""
        from .fact_extraction import build_fact_id

        def _do(con: sqlite3.Connection):
            now = _now_iso()
            for doc_id, user_id, session_id, fact in facts_batch:
                fact_id = build_fact_id(doc_id, fact)
                fact_text = fact.fact_text()
                con.execute(
                    "INSERT OR REPLACE INTO facts"
                    "(id, message_id, user_id, session_id, subject, predicate, object, "
                    "time_value, time_epoch, created_at, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (fact_id, doc_id, user_id, session_id,
                     fact.subject, fact.predicate, fact.object,
                     fact.time_value, fact.time_epoch, now, now),
                )
                con.execute(
                    "INSERT OR REPLACE INTO fts_facts(fact_text, fact_id, user_id) VALUES(?,?,?)",
                    (features.index_text(fact_text), fact_id, user_id),
                )

        self._write(_do)

    def _add_locked(self, con, request_id, user_id, session_id, normalized):
        row = con.execute(
            "SELECT message_ids FROM requests WHERE request_id=? AND user_id=?",
            (request_id, user_id),
        ).fetchone()
        if row is not None:  # 幂等：同 (request_id, user_id) 不重复落库
            return (AddResult(
                request_id=request_id,
                user_id=user_id,
                session_id=session_id,
                message_ids=json.loads(row["message_ids"]),
                idempotent=True,
            ), None)

        state = con.execute(
            "SELECT msg_count, last_ts_ms, seg_index, seg_start, seg_count "
            "FROM sessions WHERE user_id=? AND session_id IS ?",
            (user_id, session_id),
        ).fetchone()
        old_count = state["msg_count"] if state else 0
        last_ts = state["last_ts_ms"] if state else None
        seg_index = state["seg_index"] if state else 0
        seg_start = state["seg_start"] if state else 0
        seg_count = state["seg_count"] if state else 0

        now = _now_iso()
        gap_ms = max(0, int(self.config.segment_max_gap_seconds)) * 1000
        max_seg = max(1, int(self.config.segment_max_messages))
        touched_segments: list[tuple[int, int]] = []
        new_ids: list[str] = []
        dense_docs: list[tuple[str, str]] = []

        for offset, item in enumerate(normalized):
            seq = old_count + offset
            mid = _msg_id(user_id, session_id, seq)
            created_at = _iso_from_ms(item["ts_ms"]) or now
            con.execute(
                "INSERT OR REPLACE INTO messages"
                "(id,user_id,session_id,seq,role,content,ts_ms,abs_epoch,abs_granularity,abs_expression,partial_month,partial_day,partial_expression,has_temporal_expr,created_at,request_id,added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, user_id, session_id, seq, item["role"], item["content"],
                 item["ts_ms"], item["abs_epoch"], item["abs_granularity"], item["abs_expression"],
                 item["partial_month"], item["partial_day"], item["partial_expression"], item["has_temporal_expr"],
                 created_at, request_id, now),
            )
            self._index_doc(con, mid, user_id, "message", item["content"])
            new_ids.append(mid)
            dense_docs.append((mid, item["content"]))

            # 段边界：从左到右、确定性，与 views.segment_boundaries 全量扫描等价
            if seg_count > 0:
                gap_break = (
                    gap_ms > 0
                    and last_ts is not None
                    and item["ts_ms"] is not None
                    and (int(item["ts_ms"]) - int(last_ts)) > gap_ms
                )
                if seg_count >= max_seg or gap_break:
                    seg_index += 1
                    seg_start = seq
                    seg_count = 0
            seg_count += 1
            if item["ts_ms"] is not None:
                last_ts = int(item["ts_ms"])
            if not touched_segments or touched_segments[-1][0] != seg_index:
                touched_segments.append((seg_index, seg_start))

        new_count = old_count + len(normalized)

        con.execute(
            "INSERT INTO requests(request_id,user_id,session_id,message_ids,created_at) "
            "VALUES(?,?,?,?,?)",
            (request_id, user_id, session_id, json.dumps(new_ids), now),
        )
        con.execute(
            "INSERT INTO sessions(user_id,session_id,msg_count,last_ts_ms,seg_index,seg_start,seg_count) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,session_id) DO UPDATE SET "
            "msg_count=excluded.msg_count, last_ts_ms=excluded.last_ts_ms, "
            "seg_index=excluded.seg_index, seg_start=excluded.seg_start, seg_count=excluded.seg_count",
            (user_id, session_id, new_count, last_ts, seg_index, seg_start, seg_count),
        )

        if self.flags.get("views", True):
            view_docs = self._rebuild_incremental_views(
                con, user_id, session_id, old_count, new_count, touched_segments
            )
            # 稠密索引默认只嵌入原始消息：视图是派生的（segment 可含 12 条消息
            # 的长拼接），其内容全部蕴含在消息里，嵌入它们会显著拖慢 Add 且
            # 收益边际。需要时用 dense_index_views 打开。
            if self.flags.get("dense_index_views", False):
                dense_docs.extend(view_docs)

        # Consolidation N->1：归档冗余同主题消息并产出摘要视图（默认关闭）。
        # 必须在消息/视图落库后、commit 前执行：它读取刚写入的活跃消息、判定合并、
        # 归档原始行并新建摘要。整个 pass 在同一事务内，失败整体回滚。
        if self.flags.get("consolidation_dedup", False):
            self._consolidate_after_add(con, user_id, session_id, new_ids, normalized, now)

        return (AddResult(
            request_id=request_id,
            user_id=user_id,
            session_id=session_id,
            message_ids=new_ids,
            idempotent=False,
        ), dense_docs)

    # -------------------------------------------------------- consolidation
    def _consolidate_after_add(self, con, user_id, session_id, new_ids, normalized, now):
        """Consolidation N->1 deterministic dedup（仅 flags["consolidation_dedup"] 开启时调用）。

        对每条新消息：计算其话题指纹（长度≥2 的 token 集合，与 supersession 同一基建），
        在该用户已有活跃消息（consolidated=0）中找 containment ≥ 阈值且事件时间在窗口内的
        匹配。当匹配数 ≥ N（consolidation_min_cluster）时，把「新消息 + 全部匹配」合并为
        一条摘要视图：
          - content   = 簇内事件时间最新的那条消息原文（keep the latest content）；
          - source_ids= 簇内全部消息 ID 的并集（gold 按 source_message_ids 判定仍命中）；
          - view_type = "consolidation"，view_id 由 (user_id, 排序后来源集合) 哈希确定（幂等）。
        随后归档簇内全部原始消息：标记 consolidated=1 并从 FTS 删除，使其不再作为独立候选；
        原始行仍保留于 messages 表供审计/回溯/source_ids 回指。

        确定性：指纹、containment、时间窗、排序键均为纯函数，相同输入产出完全一致的
        摘要 view_id 与 source_ids。批内已归档的新消息不再触发二次合并。
        """
        min_cluster = max(1, int(getattr(self.config, "consolidation_min_cluster", 3)))
        window_s = max(0, int(getattr(self.config, "consolidation_time_window_seconds", 604800)))
        min_overlap = float(getattr(self.config, "consolidation_min_overlap", 0.5))
        max_scan = max(1, int(getattr(self.config, "consolidation_max_scan", 1000)))
        archived_in_batch: set[str] = set()

        for offset, mid in enumerate(new_ids):
            if mid in archived_in_batch:
                continue  # 已被批内更早的新消息合并归档，不重复触发
            item = normalized[offset]
            sig_new = self._supersession_signature(item["content"])
            if not sig_new:
                continue
            # 新消息事件时间：ts_ms 优先，缺失时退化为 created_at（= now，即「刚写入」）
            new_epoch = RetrieverDB._epoch_of({
                "ts_ms": item["ts_ms"],
                "created_at": _iso_from_ms(item["ts_ms"]) or now,
            })
            if new_epoch is None:
                continue  # 无任何时间信息无法判窗，保守跳过

            rows = con.execute(
                "SELECT id, content, ts_ms, created_at, session_id FROM messages "
                "WHERE user_id=? AND consolidated=0 AND id<>? "
                "ORDER BY added_at DESC, seq DESC LIMIT ?",
                (user_id, mid, max_scan),
            ).fetchall()

            matches: list[tuple] = []
            for r in rows:
                sig = self._supersession_signature(r["content"])
                if not sig:
                    continue
                inter = len(sig_new & sig)
                if not inter:
                    continue
                # containment（交集 / 较短一方）而非 Jaccard：与 supersession 一致，
                # 真实近重复陈述长度差异不影响判定。
                if inter / float(min(len(sig_new), len(sig))) < min_overlap:
                    continue
                ep = RetrieverDB._epoch_of(dict(r))
                if ep is None:
                    continue
                if window_s > 0 and abs(ep - new_epoch) > window_s:
                    continue
                matches.append((ep, r["id"], r["content"], r["created_at"], r["session_id"]))

            if len(matches) < min_cluster:
                continue

            # 簇 = 新消息 + 匹配的已有消息；最新内容 = 事件时间最大者（同时间按 id 稳定排序）
            cluster = matches + [
                (new_epoch, mid, item["content"], _iso_from_ms(item["ts_ms"]) or now, session_id)
            ]
            cluster.sort(key=lambda x: (x[0], x[1]))
            latest = cluster[-1]
            all_ids = sorted(c[1] for c in cluster)
            view_id = _consolidation_view_id(user_id, all_ids)
            # 幂等：同源集合的摘要已存在则跳过（防重复请求 / 批内二次触发）
            if con.execute("SELECT 1 FROM views WHERE view_id=?", (view_id,)).fetchone() is not None:
                continue
            digest = hashlib.sha1((latest[2] or "").encode("utf-8")).hexdigest()
            con.execute(
                "INSERT OR REPLACE INTO views"
                "(view_id,user_id,session_id,view_type,content,created_at,source_ids,start_seq,end_seq,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (view_id, user_id, latest[4], "consolidation", latest[2],
                 latest[3] or now, json.dumps(all_ids, ensure_ascii=False), -1, -1, digest),
            )
            self._index_doc(con, view_id, user_id, "consolidation", latest[2])
            # 归档簇内全部原始消息：标记 + 从 FTS 移除（原始行保留于 messages 表）
            for cid in all_ids:
                con.execute("UPDATE messages SET consolidated=1 WHERE id=?", (cid,))
                con.execute("DELETE FROM fts WHERE doc_id=?", (cid,))
                archived_in_batch.add(cid)

    # ------------------------------------------------------------------ views
    def _session_rows(self, con, user_id, session_id, start, end) -> list[dict]:
        rows = con.execute(
            "SELECT id, role, content, created_at, seq FROM messages "
            "WHERE user_id=? AND session_id IS ? AND seq BETWEEN ? AND ? ORDER BY seq",
            (user_id, session_id, start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def _rebuild_incremental_views(
        self, con, user_id, session_id, old_count, new_count, touched_segments
    ) -> list[tuple[str, str]]:
        """重建受影响视图；返回本次实际 upsert 的 (view_id, content) 列表。"""
        size = max(1, int(self.config.window_size))
        overlap = max(0, int(self.config.window_overlap))
        view_docs: list[tuple[str, str]] = []

        for start in affected_window_starts(old_count, new_count, size, overlap):
            chunk = self._session_rows(con, user_id, session_id, start, start + size - 1)
            if not chunk:
                continue
            doc = self._upsert_view(
                con,
                view_id=window_view_id(user_id, session_id, start),
                user_id=user_id,
                session_id=session_id,
                view_type="window",
                chunk=chunk,
                start_seq=start,
                end_seq=start + len(chunk) - 1,
            )
            if doc:
                view_docs.append(doc)

        for idx, (seg_idx, seg_start) in enumerate(touched_segments):
            seg_end = (
                touched_segments[idx + 1][1] - 1
                if idx + 1 < len(touched_segments)
                else new_count - 1
            )
            chunk = self._session_rows(con, user_id, session_id, seg_start, seg_end)
            if not chunk:
                continue
            doc = self._upsert_view(
                con,
                view_id=segment_view_id(user_id, session_id, seg_idx),
                user_id=user_id,
                session_id=session_id,
                view_type="session-segment",
                chunk=chunk,
                start_seq=seg_start,
                end_seq=seg_end,
            )
            if doc:
                view_docs.append(doc)
        return view_docs

    def _upsert_view(
        self, con, *, view_id, user_id, session_id, view_type, chunk, start_seq, end_seq
    ) -> tuple[str, str] | None:
        content = join_content(chunk)
        digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        existing = con.execute(
            "SELECT content_hash FROM views WHERE view_id=?", (view_id,)
        ).fetchone()
        if existing is not None and existing["content_hash"] == digest:
            return None  # 内容未变，避免无谓的 FTS 抖动
        con.execute(
            "INSERT OR REPLACE INTO views"
            "(view_id,user_id,session_id,view_type,content,created_at,source_ids,start_seq,end_seq,content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                view_id, user_id, session_id, view_type, content,
                chunk[0].get("created_at") or "",
                json.dumps([m["id"] for m in chunk], ensure_ascii=False),
                start_seq, end_seq, digest,
            ),
        )
        self._index_doc(con, view_id, user_id, view_type, content)
        return (view_id, content)

    def _index_doc(self, con, doc_id, user_id, doc_type, content) -> None:
        con.execute("DELETE FROM fts WHERE doc_id=?", (doc_id,))
        con.execute(
            "INSERT INTO fts(text, doc_id, user_id, doc_type) VALUES(?,?,?,?)",
            (features.index_text(content), doc_id, user_id, doc_type),
        )

    # ----------------------------------------------------------------- Search
    def _match_expr(self, tokens: list[str]) -> str | None:
        if not tokens:
            return None
        return " OR ".join('"%s"' % t.replace('"', '""') for t in tokens)

    def _fts_candidates(self, con, user_id: str, tokens: list[str]) -> list[tuple[str, str, float]]:
        expr = self._match_expr(tokens)
        if not expr:
            return []
        sql = (
            "SELECT doc_id, doc_type, rank FROM fts "
            "WHERE fts MATCH ? AND user_id=? "
            + (
                ""
                if self.flags.get("views", True)
                # views=False 时仍需召回 consolidation 摘要：它替代了已归档的原始消息，
                # 排除它会导致该主题内容在 FTS 中彻底消失（归档消息已从 FTS 删除）。
                else "AND doc_type IN ('message', 'consolidation') "
            )
            + "ORDER BY rank LIMIT ?"
        )
        try:
            rows = con.execute(sql, (expr, user_id, int(self.config.max_candidates))).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(r["doc_id"], r["doc_type"], float(r["rank"] or 0.0)) for r in rows]

    def _like_fallback(self, con, user_id: str, query: str) -> list[tuple[str, str, float]]:
        needle = f"%{(query or '').strip()}%"
        if len(needle) <= 2:
            return []
        rows = con.execute(
            "SELECT id AS doc_id, 'message' AS doc_type FROM messages "
            "WHERE user_id=? AND consolidated=0 AND content LIKE ? LIMIT ?",
            (user_id, needle, int(self.config.max_candidates)),
        ).fetchall()
        return [(r["doc_id"], r["doc_type"], 0.0) for r in rows]

    # ---------------------------------------------------- fact-table search
    @staticmethod
    def _has_fact_relevant_intent(query: str) -> bool:
        """判断查询是否适合检索事实表（knowledge_update / temporal 类）。

        事实表对「问当前状态 / 问更新后的值 / 问某属性当前取值 / 问何时发生」
        的查询最有增益：这些查询的答案取决于结构化事实的匹配，而非词面重合。
        非目标查询不检索事实表，避免无谓的 FTS 开销。
        """
        return (
            features.has_temporal_intent(query)
            or features.has_current_value_intent(query)
            or features.has_date_value_intent(query)
            or features.has_update_cue(query)
            or features.has_numeric_value_intent(query)
        )

    def _search_facts(
        self, con: sqlite3.Connection, user_id: str, tokens: list[str]
    ) -> dict[str, list[dict]]:
        """检索事实表，返回 message_id -> [fact_dict, ...] 映射。

        用查询 token 在 fts_facts 上做 OR 匹配，再回查 facts 表获取结构化字段。
        事实表为空时返回空 dict——与纯词法路径完全等价。
        """
        expr = self._match_expr(tokens)
        if not expr:
            return {}
        try:
            rows = con.execute(
                "SELECT fact_id FROM fts_facts WHERE fts_facts MATCH ? AND user_id=? LIMIT ?",
                (expr, user_id, int(self.config.max_candidates)),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        if not rows:
            return {}
        fact_ids = [r["fact_id"] for r in rows]
        # 分块回查事实详情（避免 IN 子句过长）
        matches: dict[str, list[dict]] = {}
        chunk = 400
        for i in range(0, len(fact_ids), chunk):
            part = fact_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            fact_rows = con.execute(
                f"SELECT message_id, subject, predicate, object, time_value, time_epoch "
                f"FROM facts WHERE id IN ({ph}) AND user_id=?",
                (*part, user_id),
            ).fetchall()
            for r in fact_rows:
                matches.setdefault(r["message_id"], []).append({
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "time_value": r["time_value"],
                    "time_epoch": r["time_epoch"],
                })
        return matches

    def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int | None = None,
        options: list[str] | None = None,
        request_id: str | None = None,
    ) -> SearchResult:
        if not user_id:
            raise ValueError("user_id is required")
        if query is None:
            raise ValueError("query is required")
        limit = self.config.top_k_default if top_k is None else int(top_k)
        limit = max(0, min(limit, int(self.config.top_k_max)))
        if limit == 0:
            return SearchResult(request_id=request_id, total=0, results=[])

        recall_text = query
        if options and self.flags.get("use_options", True):
            recall_text = query + " " + " ".join(str(o) for o in options)
        tokens = features.query_tokens(recall_text, self.config.max_query_tokens)

        with self.connection() as con:
            raw_candidates = self._fts_candidates(con, user_id, tokens)
            if not raw_candidates and self.flags.get("exact_scan", False):
                raw_candidates = self._like_fallback(con, user_id, query)
            dense_candidates: list[tuple[str, float]] = []
            dense = self._dense_index()
            if dense is not None:
                try:
                    dense_candidates = dense.top_n(
                        user_id, recall_text, int(self.config.dense_top_n)
                    )
                except Exception:
                    dense_candidates = []  # 稠密失败 → 纯词法路径，不改变语义

            # LLM 事实抽取：对 knowledge_update/temporal 查询额外检索事实表。
            # 事实表为空（flag 关闭 / API 不可用 / 无事实）时 fact_matches 为空，
            # 不改变任何检索行为——与纯词法路径完全等价。
            fact_matches: dict[str, list[dict]] = {}
            if self.flags.get("fact_extraction", False) and self._has_fact_relevant_intent(query):
                fact_matches = self._search_facts(con, user_id, tokens)

            if not raw_candidates and not dense_candidates and not fact_matches:
                return SearchResult(request_id=request_id, total=0, results=[])
            # 稠密候选与词法候选按 doc_id 合并去重（稠密补充词法漏掉的文档）
            merged: dict[str, tuple[str, str, float]] = {}
            for doc_id, doc_type, rank in raw_candidates:
                merged.setdefault(doc_id, (doc_id, doc_type, rank))
            for doc_id, _score in dense_candidates:
                merged.setdefault(doc_id, (doc_id, "dense", 0.0))
            raw_candidates = list(merged.values())
            records = self._load_records(con, user_id, raw_candidates)

            # 事实匹配补充：词法召回遗漏但事实表命中的消息作为额外候选。
            # 这些消息无 FTS rank（rank_map 中为 0.0），仅靠 fact_boost 提升进入排序。
            if fact_matches:
                existing_ids = {r["id"] for r in records}
                extra_limit = int(getattr(self.config, "fact_extra_candidates", 10))
                extra_ids = [mid for mid in fact_matches if mid not in existing_ids][:extra_limit]
                if extra_ids:
                    extra_raw = [(mid, "message", 0.0) for mid in extra_ids]
                    extra_records = self._load_records(con, user_id, extra_raw)
                    records.extend(extra_records)
                    raw_candidates.extend(extra_raw)

        fts_order = [doc_id for doc_id, _t, _r in raw_candidates if _t != "dense"]
        dense_order = [doc_id for doc_id, _score in dense_candidates]
        rank_map = {doc_id: rank for doc_id, _t, rank in raw_candidates}
        if not records:
            return SearchResult(request_id=request_id, total=0, results=[])

        scored = self._score(records, query, tokens, rank_map, fact_matches=fact_matches)

        # vNext：实体消歧后的软重排必须发生在 RRF 融合前，
        # 这样 feature 路的排序会反映其增益；它从不删除任何候选。
        if self.flags.get("entity_boost_v2", False):
            scored = apply_entity_boost_v2(
                scored,
                query=query,
                base_weight=float(getattr(self.config, "entity_disambiguation_weight", 35.0)),
                cooccurrence_weight=float(getattr(self.config, "entity_cooccurrence_weight", 20.0)),
                config=self.config,
            )

        ordered = self._fuse_and_order(scored, fts_order, dense_order=dense_order)
        if self.flags.get("dedup", True):
            ordered = self._dedup(ordered)
        final = self._apply_slot_guarantee(ordered, limit)
        # 塑形在槽位保证之后：它只重排/加前缀，不改变"哪些记录入选"
        final = self._apply_ordering_shaping(final, query)
        # 视图过滤（message_view_only，默认关闭）：只返回原始消息视图，丢弃
        # window/session-segment 聚合块。动机（LoCoMo e2e 诊断）：聚合视图在
        # top-k 中占据前位但无时间锚（ts_prefix 只对 message 视图生效），导致
        # 答案模型读到无绝对日期的相对时间文本；message 原文自带 [日期] 前缀。
        # 证据仍在（消息全部保留），只是不返回冗余聚合块。
        if self.flags.get("message_view_only", False):
            final = [r for r in final if r.get("view") == "message"]

        # v1.4 H 修复（low_confidence_abstain）：低置信弃权。若返回结果与查询
        # 无任何 token 重合（无相关证据），返回空证据集，让答案模型对无证据
        # 问题弃权而非编造。对齐合规冲榜路线"对没有证据的问题返回无法确定所需
        # 的空证据状态"。只在确实零重合时触发，不误伤正常召回。
        if self.flags.get("low_confidence_abstain", False) and final and tokens:
            token_set = set(tokens)
            has_overlap = any(
                token_set & set(features.tokenize(r.get("content") or ""))
                for r in final
            )
            if not has_overlap:
                return SearchResult(request_id=request_id, total=len(ordered), results=[])

        results = [
            Evidence(
                id=r["id"],
                user_id=user_id,
                view=r["view"],
                content=self._content_for_response(r, query),
                created_at=r["created_at"] or "",
                score=round(float(r["score"]), 6),
                source_message_ids=r["source_ids"],
                provenance=f"{r['view']}:{r['id']}<-[{','.join(r['source_ids'])}]",
                evidence_flags=r["flags"],
            )
            for r in final
        ]
        return SearchResult(request_id=request_id, total=len(ordered), results=results)

    def _apply_ordering_shaping(self, ordered: list[dict], query: str = "") -> list[dict]:
        """事件序号/时间线塑形（默认关闭，仅目标意图查询生效，不改原文）。

        - ``ordering_prefix``：顺序意图查询（has_ordering_intent）时，message 视图
          按事件时间升序重排并注入 [事件N] 序号前缀（N 为时间序）。
        - ``chrono_ordering``：时间上下文查询（has_temporal_context）时，结果按
          事件时间升序重排（不加前缀）。
        两者共用时间锚（_epoch_of）；无可排序时间的记录保持在原相对位置之后。
        非目标查询与关闭时完全原样返回（检索代理零回归的保证）。
        """
        ordering = bool(self.flags.get("ordering_prefix", False)) and features.has_ordering_intent(query or "")
        chrono = (not ordering) and bool(self.flags.get("chrono_ordering", False)) and features.has_temporal_context(query or "")
        if not (ordering or chrono):
            return ordered
        # 时间锚：_epoch_of 优先，缺失的排最后（稳定排序保持同锚原序）
        def anchor(rec: dict):
            epoch = self._epoch_of(rec)
            return (0, epoch) if epoch is not None else (1, 0)
        # 只对 message 视图重排（聚合视图时间语义模糊，保持原序）；稳定排序
        messages = [r for r in ordered if r.get("view") == "message"]
        others = [r for r in ordered if r.get("view") != "message"]
        messages.sort(key=anchor)
        if ordering:
            for idx, rec in enumerate(messages, start=1):
                content = rec.get("content") or ""
                rec["content"] = f"[事件{idx}] {content}"
        return messages + others

    def _content_for_response(self, rec: dict, query: str = "") -> str:
        """内容塑造：按配置给返回内容附加事件时间元数据（不改原文，不改排序）。

        官方答案指令允许“memory timestamp 明确时把相对时间转成日期”，而
        Search 响应只把 content 喂给答案模型；因此把事件时间以
        ``[<时间>]`` 前缀注入 content，可让答案模型在时间问题上使用它。
        时间取原始粒度保守表达（仅日期），避免粒度升级；关闭时原样返回。

        v1.2.1：默认**仅对含时间/日期意图的查询**加前缀（消融证据：无条件前缀
        在非时间类问题上小幅回退，cat4 −0.008 / cat5 −0.030）；设置
        ``content_timestamp_prefix_unconditional`` 可恢复 v1.2 的无条件行为。
        """
        content = rec.get("content") or ""
        if not self.flags.get("content_timestamp_prefix", False):
            return content
        if not self.flags.get("content_timestamp_prefix_unconditional", False):
            if not features.has_temporal_context(query or ""):
                return content
        ts = self._event_date_prefix(rec)
        if not ts:
            return content
        return f"[{ts}] {content}"

    @staticmethod
    def _event_date_prefix(rec: dict) -> str:
        """事件时间的日期级表达：优先原文自带的绝对日期（保持其粒度），
        其次 ts_ms/created_at 的日期部分。粒度不高于 day，绝不伪造精度。"""
        from datetime import datetime, timezone as _tz

        abs_expr = rec.get("abs_expression")
        if abs_expr:
            return str(abs_expr)
        epoch = RetrieverDB._epoch_of(rec)
        if epoch is None:
            return ""
        try:
            return datetime.fromtimestamp(epoch, tz=_tz.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            return ""

    def _load_records(self, con, user_id, raw_candidates) -> list[dict]:
        msg_ids = [d for d, t, _ in raw_candidates if t == "message"]
        view_ids = [d for d, t, _ in raw_candidates if t not in ("message", "dense")]
        # 稠密通道的候选 doc_id 类型未知：先尝试按消息加载，剩余的按视图加载
        unknown_ids = [d for d, t, _ in raw_candidates if t == "dense"]
        if unknown_ids:
            found_msg: set[str] = set()
            chunk_u = 400
            for i in range(0, len(unknown_ids), chunk_u):
                part = unknown_ids[i : i + chunk_u]
                ph = ",".join("?" * len(part))
                for r in con.execute(
                    f"SELECT id FROM messages WHERE id IN ({ph}) AND user_id=? AND consolidated=0",
                    (*part, user_id),
                ).fetchall():
                    found_msg.add(r["id"])
            msg_ids.extend(sorted(found_msg))
            view_ids.extend(sorted(set(unknown_ids) - found_msg))
        out: list[dict] = []
        chunk = 400
        for i in range(0, len(msg_ids), chunk):
            part = msg_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            for r in con.execute(
                f"SELECT id, 'message' AS view, role, content, created_at, ts_ms, abs_epoch, abs_granularity, abs_expression, partial_month, partial_day, partial_expression, has_temporal_expr, session_id, seq "
                f"FROM messages WHERE id IN ({ph}) AND user_id=? AND consolidated=0",
                (*part, user_id),
            ).fetchall():
                rec = dict(r)
                rec["source_ids"] = [rec["id"]]
                out.append(rec)
        for i in range(0, len(view_ids), chunk):
            part = view_ids[i : i + chunk]
            ph = ",".join("?" * len(part))
            for r in con.execute(
                f"SELECT view_id AS id, view_type AS view, NULL AS role, content, created_at, "
                f"session_id, start_seq AS seq, source_ids, NULL AS ts_ms, NULL AS abs_epoch, "
                f"NULL AS abs_granularity, NULL AS abs_expression, NULL AS partial_month, "
                f"NULL AS partial_day, NULL AS partial_expression, NULL AS has_temporal_expr "
                f"FROM views WHERE view_id IN ({ph}) AND user_id=?",
                (*part, user_id),
            ).fetchall():
                rec = dict(r)
                try:
                    rec["source_ids"] = json.loads(rec["source_ids"])
                except (TypeError, ValueError):
                    rec["source_ids"] = [rec["id"]]
                out.append(rec)
        return out

    def _score(self, records, query, tokens, rank_map, fact_matches=None) -> list[dict]:
        q_lower = (query or "").lower()
        numbers = features.extract_numbers(query)
        dates = features.extract_dates(query)
        phrases = features.extract_phrases(query)
        entities = [e.lower() for e in features.extract_entities(query)][:32]
        token_set = [t for t in tokens if t]

        # bm25 归一化：fts5 rank 为负值，越小越相关
        ranks = [v for v in rank_map.values() if v]
        worst = max((abs(v) for v in ranks), default=1.0) or 1.0

        msg_seqs: dict[tuple, set[int]] = {}
        for rec in records:
            if rec["view"] == "message":
                msg_seqs.setdefault((rec.get("session_id"),), set()).add(int(rec.get("seq") or 0))

        now_ts = time.time()

        # 相对新近度：把候选集内的时间戳线性归一到 [0, 1]。
        # 绝对年龄在"整批语料都很老"时会退化成常数，无法区分新旧证据。
        temporal_intent = (
            self.flags.get("temporal_intent", False) and features.has_temporal_intent(query)
        )
        # v1.3：新近度权重按查询类型分档。
        # - has_temporal_intent（现在/当前/latest 等"当前状态"标记）→ 高权重，
        #   因为当前状态问题的答案确实是最新陈述。
        # - 其余（含"when did X happen"历史问题、普通事实问题）→ 低权重：
        #   BEAM 实证，多月经度对话中 recency=8 会把 2026 年的无关消息顶到
        #   2024 年的事实答案之上（"When does my first sprint end?" 答案排 rank 11）。
        current_state = features.has_temporal_intent(query)
        # v1.4 D 修复：current-value 查询（问某属性当前取值）抬高新近度，使最新版本
        # 排前。仅在 flag 开启且非 temporal_intent/current_state 时生效（后两者已有
        # 更高权重）。针对 BEAM knowledge_update 34% 失败（返回旧版本）。
        current_value = (
            self.flags.get("current_value_recency", False)
            and not current_state
            and features.has_current_value_intent(query)
        )
        recency_weight = (
            float(getattr(self.config, "recency_weight_intent", W_RECENCY_INTENT))
            if temporal_intent
            else (
                float(getattr(self.config, "recency_weight", W_RECENCY))
                if current_state
                else (
                    float(getattr(self.config, "recency_weight_current_value", 5.0))
                    if current_value
                    else float(getattr(self.config, "recency_weight_plain", 2.0))
                )
            )
        )
        # vNext：仅在开关打开时用分级时间解析给每条候选附加事件时间。
        # 关闭时保持原有 _epoch_of(ts_ms → created_at) 行为，便于消融比较。
        if self.flags.get("temporal_fallback", False):
            self._annotate_temporal(records, rank_map, query)

        stamps = [self._epoch_of(rec) for rec in records]
        known = [s for s in stamps if s is not None]
        ts_min = min(known) if known else None
        ts_max = max(known) if known else None
        ts_span = (ts_max - ts_min) if known else 0.0
        for rec in records:
            content_lower = (rec["content"] or "").lower()
            score = 0.0
            flags: list[str] = []

            rank = rank_map.get(rec["id"])
            if rank:
                score += W_LEXICAL * (abs(float(rank)) / worst) * 10.0
                flags.append("lexical")

            if token_set:
                hit = sum(1 for t in token_set if t in content_lower)
                coverage = hit / float(len(token_set))
                score += W_COVERAGE * coverage
                if coverage >= 0.6:
                    flags.append("high_coverage")

            if self.flags.get("exact", True):
                if q_lower and len(q_lower) >= 4 and q_lower in content_lower:
                    score += W_EXACT_FULL
                    flags.append("exact_substring")

            if self.flags.get("entity", True):
                if phrases and any(p.lower() in content_lower for p in phrases):
                    score += W_PHRASE
                    flags.append("phrase")
                ent_hits = sum(1 for e in entities if e and e in content_lower)
                if ent_hits:
                    score += min(W_ENTITY * ent_hits, W_ENTITY * 5)
                    flags.append("entity")

            if self.flags.get("datenum", True):
                if dates and any(d in content_lower for d in dates):
                    score += W_DATE
                    flags.append("date")
                if numbers and any(n in content_lower for n in numbers):
                    score += W_NUMBER
                    flags.append("number")

            if self.flags.get("rerank", True):
                if rec["view"] in ("window", "session-segment"):
                    score += W_VIEW_CONTEXT
                    flags.append("view_context")
                if rec["view"] == "message":
                    peers = msg_seqs.get((rec.get("session_id"),), set())
                    seq = int(rec.get("seq") or 0)
                    if (seq - 1) in peers or (seq + 1) in peers:
                        score += W_ADJACENT
                        flags.append("adjacent_evidence")
                epoch = self._epoch_of(rec)
                if epoch is not None:
                    if self.flags.get("ebbinghaus_decay", False):
                        # Ebbinghaus 指数遗忘：recency = exp(-λ·Δt)，Δt 为该事实距候选集
                        # 内最新事件的天数（以最新事件为「上次访问」参考点）。锚定候选集而非
                        # 墙钟 now_ts：评测不注入查询时刻，绝对墙钟会让整批老语料的衰减值
                        # 塌缩到 ~0 而丧失区分力，且结果随运行日期漂移不可复现。候选集相对
                        # 锚定保持确定性，且与线性基线共享「最新=1.0」锚点，A/B 仅隔离曲线
                        # 形状（线性 vs 指数）。
                        lam = float(getattr(self.config, "decay_lambda", 0.1))
                        days_since = max(0.0, (ts_max - epoch) / 86400.0)
                        relative = math.exp(-lam * days_since)
                    elif ts_span > 0:
                        relative = (epoch - ts_min) / ts_span
                    else:
                        relative = 1.0
                    effective_recency_weight = recency_weight
                    if self.flags.get("temporal_fallback", False):
                        confidence = rec.get("_temporal_confidence")
                        if confidence is not None:
                            effective_recency_weight = confidence_to_recency_weight(
                                recency_weight, confidence
                            )
                        source = rec.get("_temporal_source", "legacy")
                        confidence_name = getattr(confidence, "name", "UNKNOWN").lower()
                        flags.append(f"temporal:{source}:{confidence_name}")
                    score += effective_recency_weight * relative
                    if self.flags.get("ebbinghaus_decay", False):
                        flags.append("ebbinghaus_decay")
                    flags.append("recency_intent" if temporal_intent else "recency")
                elif self.flags.get("temporal_fallback", False):
                    # 已启用新兜底但仍无可排序的时间信息：显式不加新近度，
                    # 防止把不确定记忆伪装成“较新”。
                    flags.append("temporal:unknown:no_recency_boost")
                elif self._age_days(rec.get("created_at"), now_ts) is not None:
                    # 旧行为：仅在 vNext 时间兜底关闭时保留 created_at 弱信号。
                    age_days = self._age_days(rec.get("created_at"), now_ts)
                    score += W_RECENCY / (1.0 + math.log1p(max(0.0, age_days)))
                    flags.append("recency")

            rec["score"] = score
            rec["flags"] = flags

        # LLM 事实匹配提升：对事实表命中的消息做分数提升（在 RRF 融合前生效，
        # 使 feature 路排序反映事实增益）。只提升、不删除、不改写原文。
        # fact_matches 为空（flag 关闭 / 无事实 / 非目标查询）时零行为变化。
        if fact_matches:
            weight = float(getattr(self.config, "fact_boost_weight", 25.0))
            for rec in records:
                if rec["id"] in fact_matches:
                    rec["score"] += weight
                    if "fact_match" not in rec["flags"]:
                        rec["flags"].append("fact_match")

        if (
            self.flags.get("preference_role_boost", False)
            and features.has_preference_intent(query)
        ):
            weight = float(getattr(self.config, "preference_role_weight", 14.0))
            for rec in records:
                if (
                    rec["view"] == "message"
                    and str(rec.get("role") or "").lower() == "user"
                    and features.has_direct_preference_statement(rec.get("content") or "")
                ):
                    rec["score"] += weight
                    if "direct_user_preference" not in rec["flags"]:
                        rec["flags"].append("direct_user_preference")

        if self.flags.get("rerank", True):
            self._mark_conflicts(records)
        if self.flags.get("conflict_pair_return", False):
            # v1.4 D 修复：冲突成对返回（同话题相反极性成对提升，确保成对返回）。
            self._mark_polarity_conflicts(records)
        if self.flags.get("supersession", False):
            # 用「原始」时间意图判定，不受 flags["temporal_intent"] 影响：
            # 覆写检测与新近度放大是两个独立机制，前者不应被后者的开关连带关掉。
            self._mark_supersession(
                records,
                features.has_temporal_intent(query),
                query=query,
                require_update_cue=self.flags.get("supersession_update_guard", False),
            )
        return records

    def _annotate_temporal(self, records: list[dict], rank_map: dict[str, float] | None = None, query: str = "") -> None:
        """为候选附加分级时间结果，不改写数据库原始字段。

        先按 (session_id, seq) 建立会话锚点：有原始 ts_ms 或正文绝对日期的
        消息会更新锚点；后续“昨天 / 三天前 / 下周”等相对表达据此推算。
        created_at 只作为 LOW 级最后兜底，因此永远不会替代显式事件时间。
        """
        anchors: dict[object, float] = {}
        # P2：预筛字段先排除确定“不含相对/模糊时间”的新消息；旧消息（NULL）保守保留。
        # 再按 query 类型给解析预算：日期问题 > 当前状态问题 > 普通问题。
        if features.has_date_value_intent(query):
            limit = max(0, int(getattr(self.config, "temporal_fallback_top_n", 80)))
        elif features.has_temporal_intent(query):
            limit = max(0, int(getattr(self.config, "temporal_fallback_top_n_temporal", 40)))
        else:
            limit = max(0, int(getattr(self.config, "temporal_fallback_top_n_other", 8)))
        unresolved_all = [r for r in records if r.get("ts_ms") is None and r.get("abs_epoch") is None and r.get("partial_month") is None]
        prefilter_ids = {r.get("id") for r in unresolved_all if r.get("has_temporal_expr") == 0}
        unresolved = [r for r in unresolved_all if r.get("id") not in prefilter_ids]
        unresolved.sort(key=lambda r: (abs(float((rank_map or {}).get(r.get("id"), 1e18))), r.get("id", "")))
        deferred_ids = {r.get("id") for r in unresolved[limit:]} if limit else set()
        ordered = sorted(
            records,
            key=lambda r: (str(r.get("session_id") or ""), int(r.get("seq") or 0), r.get("id", "")),
        )
        for rec in ordered:
            session_key = rec.get("session_id")
            ts_ms = rec.get("ts_ms")
            # 热路径：大多数生产消息已有 ts_ms；避免为它们构造结果对象、扫描正则
            # 或解析 created_at。只有真正缺少原始时间戳的候选才进入完整兜底。
            if ts_ms is not None:
                try:
                    epoch = float(ts_ms) / 1000.0
                    rec["_temporal_epoch"] = epoch
                    rec["_temporal_confidence"] = TimeConfidence.HIGH
                    rec["_temporal_source"] = "ts_ms"
                    rec["_temporal_granularity"] = "millisecond"
                    rec["_temporal_expression"] = str(ts_ms)
                    anchors[session_key] = epoch
                    continue
                except (TypeError, ValueError):
                    pass
            # P1：绝对日期已在 Add 阶段提取并持久化，不再扫描 content。
            if rec.get("abs_epoch") is not None:
                rec["_temporal_epoch"] = float(rec["abs_epoch"])
                rec["_temporal_confidence"] = TimeConfidence.HIGH
                rec["_temporal_source"] = "stored_content_absolute"
                rec["_temporal_granularity"] = rec.get("abs_granularity") or "day"
                rec["_temporal_expression"] = rec.get("abs_expression") or ""
                anchors[session_key] = float(rec["abs_epoch"])
                continue
            # 月日无年份已在 Add 持久化；只依赖当前会话锚点的纯函数已按锚点日缓存。
            partial = resolve_stored_month_day(
                rec.get("partial_month"), rec.get("partial_day"), anchors.get(session_key)
            )
            if partial is not None:
                rec["_temporal_epoch"] = partial.epoch
                rec["_temporal_confidence"] = partial.confidence
                rec["_temporal_source"] = partial.source
                rec["_temporal_granularity"] = partial.granularity
                rec["_temporal_expression"] = rec.get("partial_expression") or partial.original_expression
                anchors[session_key] = partial.epoch
                continue
            if rec.get("id") in prefilter_ids:
                rec["_temporal_epoch"] = None
                rec["_temporal_confidence"] = TimeConfidence.UNKNOWN
                rec["_temporal_source"] = "prefilter_no_time_expression"
                rec["_temporal_granularity"] = "unknown"
                rec["_temporal_expression"] = ""
                continue
            if rec.get("id") in deferred_ids:
                rec["_temporal_epoch"] = None
                rec["_temporal_confidence"] = TimeConfidence.UNKNOWN
                rec["_temporal_source"] = "deferred_top_n"
                rec["_temporal_granularity"] = "unknown"
                rec["_temporal_expression"] = ""
                continue
            result = resolve_temporal(
                ts_ms=None,
                content=rec.get("content") or "",
                created_at=rec.get("created_at"),
                session_anchor_epoch=anchors.get(session_key),
            )
            rec["_temporal_epoch"] = result.epoch
            rec["_temporal_confidence"] = result.confidence
            rec["_temporal_source"] = result.source
            rec["_temporal_granularity"] = result.granularity
            rec["_temporal_expression"] = result.original_expression
            # 只让 HIGH/MEDIUM 级事件时间成为下一条消息的会话锚点，
            # 防止 created_at 或插值结果累积放大误差。
            if result.epoch is not None and result.confidence.name in {"HIGH", "MEDIUM"}:
                anchors[session_key] = result.epoch

    @staticmethod
    def _epoch_of(rec: dict) -> float | None:
        """候选记录的事件时间（秒）。优先原始 ts_ms，其次解析 created_at。

        聚合视图的 created_at 取自其首条源消息，因此与原始消息在同一时间轴上，
        可以直接参与相对新近度归一。
        """
        # 注意：_temporal_epoch 键可能被 _annotate_temporal 显式置 None（视图/正文
        # 无时间表达时）；此时必须继续走 ts_ms/created_at 兜底，而不是短路返回
        # None——否则开启 temporal_fallback 后视图记录的时间锚全部丢失，
        # ts_prefix 无法给视图附加 [日期] 前缀（LoCoMo e2e 时间类失败根因）。
        if "_temporal_epoch" in rec and rec.get("_temporal_epoch") is not None:
            return rec.get("_temporal_epoch")
        ts_ms = rec.get("ts_ms")
        if ts_ms is not None:
            try:
                return float(ts_ms) / 1000.0
            except (TypeError, ValueError):
                pass
        created_at = rec.get("created_at")
        if not created_at:
            return None
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    @staticmethod
    def _age_days(created_at: str | None, now_ts: float) -> float | None:
        if not created_at:
            return None
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now_ts - dt.timestamp()) / 86400.0)

    def _mark_conflicts(self, records) -> None:
        """疑似状态冲突：同一实体上出现不同数字/日期。只做标注与轻微偏好，绝不过滤。"""
        buckets: dict[str, list[dict]] = {}
        for rec in records:
            if rec["view"] != "message":
                continue
            ents = features.extract_entities(rec["content"] or "")[:1]
            if not ents:
                continue
            buckets.setdefault(ents[0].lower(), []).append(rec)
        for group in buckets.values():
            if len(group) < 2:
                continue
            values = {
                tuple(features.extract_numbers(r["content"]) + features.extract_dates(r["content"]))
                for r in group
            }
            if len(values) < 2:
                continue
            newest = max(group, key=lambda r: (r.get("created_at") or "", r["id"]))
            for rec in group:
                if "possible_conflict" not in rec["flags"]:
                    rec["flags"].append("possible_conflict")
            newest["score"] += W_CONFLICT_LATEST
            if "latest_state" not in newest["flags"]:
                newest["flags"].append("latest_state")

    def _mark_polarity_conflicts(self, records) -> None:
        """v1.4 D 修复（conflict_pair_return）：检测同话题、相反极性的消息对。

        对齐合规冲榜路线「无法确定是更新还是矛盾时，保留 conflicted 并把两个
        证据一起返回」。检测「话题指纹高度重合 + 一肯定一否定」的消息对，
        检测到即**同时提升两条**，确保成对进入 top-k，让答案模型看到矛盾
        而非只取其一（针对 BEAM contradiction_resolution 本地实测 0.000：
        模型只见否定侧、答 "No."）。

        只做提升，不过滤、不删除、不改写原文。判定是纯结构性的
        （token 重合 + 否定极性），不依赖评测集硬编码。
        """
        pool = [r for r in records if r["view"] == "message"]
        if len(pool) < 2:
            return
        pool.sort(key=lambda r: (-float(r.get("score") or 0.0), r["id"]))
        pool = pool[:40]
        sigs = {r["id"]: self._supersession_signature(r["content"]) for r in pool}
        neg = {r["id"]: features.has_negation(r["content"]) for r in pool}
        weight = float(getattr(self.config, "conflict_pair_weight", 3.0))
        min_overlap = float(getattr(self.config, "conflict_pair_min_overlap", 0.4))
        paired: set[str] = set()
        for i in range(len(pool)):
            a = pool[i]
            sig_a = sigs[a["id"]]
            if not sig_a:
                continue
            for j in range(i + 1, len(pool)):
                b = pool[j]
                sig_b = sigs[b["id"]]
                if not sig_b or sig_a == sig_b:
                    continue
                if neg[a["id"]] == neg[b["id"]]:
                    continue  # 同极性，非矛盾
                inter = len(sig_a & sig_b)
                if not inter:
                    continue
                if inter / float(min(len(sig_a), len(sig_b))) < min_overlap:
                    continue
                paired.add(a["id"])
                paired.add(b["id"])
        if not paired:
            return
        for rec in records:
            if rec["id"] in paired:
                rec["score"] += weight
                if "polarity_conflict_pair" not in rec["flags"]:
                    rec["flags"].append("polarity_conflict_pair")

    def _supersession_signature(self, text: str) -> frozenset:
        """消息的「话题指纹」：长度≥2 的 n-gram 集合。

        只取多字 token：单字 CJK 与停用词在任意两条中文句子间都会重合，
        会把不相关的消息也判成同一话题。
        """
        return frozenset(t for t in features.tokenize(text or "") if len(t) >= 2)

    def _mark_supersession(
        self,
        records,
        temporal_intent: bool,
        *,
        query: str = "",
        require_update_cue: bool = False,
    ) -> None:
        """覆写检测：话题高度重合的两条消息中，较晚的一条视为覆写较早的一条。

        与 `_mark_conflicts` 的区别：后者要求出现**不同的数字/日期**，
        因此只能覆盖"预算从 A 改成 B"这类数值型更新；本方法基于**成对内容冗余**，
        也能覆盖"早餐从 A 改成 B"这类纯文本型更新。

        与 `temporal_intent` 的区别：不做全局新近度放大。只有在确实找到
        「更早的近重复陈述」时才生效，所以不会把「问当前状态、但答案本身较旧」
        的查询整体推向最新消息（那正是 temporal_intent 在 multi_session 上失分的原因）。

        判定是纯结构性的（token 重合 + 时间先后），不依赖任何同义词表、
        人名/项目名清单或与评测集有关的硬编码。

        **只在查询含时间意图时生效**。否则「同模板、不同属性」的近重复消息
        （如"X的工牌号是…"/"X的车位编号是…"）会互相判定覆写并被整体抬高，
        把与时间无关的查询的正确答案挤下去（实测 single_hop|paraphrase
        MRR 1.0 → 0.21）。

        ⚠️ 仅靠结构判断、使用历史 18/6 权重的 L8 版本默认 **disabled**。跨 3 seed 消融显示它
        在合成集上净负：temporal|paraphrase 稳定 +0.10，但整体 MRR −0.026~−0.034。
        失败根因是**本方法的能力上限**，不是参数没调好：仅凭「话题重合 + 时间更晚」
        无法区分"真正的后续更新"与"对同一话题的无关后续提及"。合成集里存在
        与 gold 同项目名、但时间更晚的普通陈述，本方法会把它判成覆写者并抬到 gold 之上
        （knowledge_update|paraphrase −0.21）。
        v1.1 的 ``require_update_cue`` 会进一步要求较新消息带通用更新语义，
        并阻止数值更新干扰非数值型查询。该保护不包含评测实体或答案值；
        保守 4/1 权重在本地三 seed 过门后与 supersession 组合启用，
        但在官方数据上的表现仍是 **unknown**。
        """
        if not temporal_intent:
            return
        cfg = self.config
        min_overlap = float(getattr(cfg, "supersession_min_overlap", 0.5))
        weight = float(getattr(cfg, "supersession_weight", 4.0))
        penalty = float(getattr(cfg, "supersession_penalty", 1.0))
        max_pairs = int(getattr(cfg, "supersession_max_pairs", 40))

        # 只比较原始消息：聚合视图天然与其成员高度重合，会产生虚假覆写对。
        pool = [r for r in records if r["view"] == "message" and self._epoch_of(r) is not None]
        if len(pool) < 2:
            return
        # 确定性截断：按当前特征分降序取前 N，界定两两比较的最坏代价。
        pool.sort(key=lambda r: (-float(r.get("score") or 0.0), r["id"]))
        pool = pool[: max(2, max_pairs)]

        sigs = {r["id"]: self._supersession_signature(r["content"]) for r in pool}
        epochs = {r["id"]: self._epoch_of(r) for r in pool}

        superseded: set[str] = set()
        supersedes: set[str] = set()
        explicit_updates: set[str] = set()
        for i in range(len(pool)):
            a = pool[i]
            sig_a = sigs[a["id"]]
            if not sig_a:
                continue
            for j in range(i + 1, len(pool)):
                b = pool[j]
                sig_b = sigs[b["id"]]
                if not sig_b or sig_a == sig_b:
                    continue
                inter = len(sig_a & sig_b)
                if not inter:
                    continue
                # containment（交集 / 较短一方）而非 Jaccard：真实覆写陈述通常
                # 比原陈述更长（"早餐改成了 B，不再吃 A" vs "早餐一直吃 A"），
                # Jaccard 会因长度差异把这类真实覆写对判负（该对 Jaccard < 0.5、
                # containment ≈ 0.57）。
                # 也试过 IDF 加权 containment（想压掉同模板干扰项的共享模板词），
                # 实测更差：temporal|paraphrase 增益从 +0.11 掉到 +0.06，
                # 而 knowledge_update 的失分一点没救回来（根因见下方 docstring）。
                if inter / float(min(len(sig_a), len(sig_b))) < min_overlap:
                    continue
                ea, eb = epochs[a["id"]], epochs[b["id"]]
                if ea == eb:
                    continue
                newer, older = (b, a) if eb > ea else (a, b)
                if require_update_cue and not features.has_update_cue(newer["content"]):
                    continue
                # 数值/日期更新不能仅凭共享项目名干扰“谁负责/偏好什么”之类查询。
                # 这只是答案类型保护：不删除候选，也不包含任何评测实体或答案值。
                structured_types: set[str] = set()
                if features.extract_non_date_numbers(newer["content"]):
                    structured_types.add("numeric")
                if features.extract_dates(newer["content"]):
                    structured_types.add("date")
                query_types: set[str] = set()
                if features.has_numeric_value_intent(query):
                    query_types.add("numeric")
                if features.has_date_value_intent(query):
                    query_types.add("date")
                if (
                    require_update_cue
                    and structured_types
                    and structured_types.isdisjoint(query_types)
                ):
                    continue
                supersedes.add(newer["id"])
                superseded.add(older["id"])
                if require_update_cue:
                    explicit_updates.add(newer["id"])

        # 同时被判为「覆写者」和「被覆写者」的记录处于更新链中间，不加不减，
        # 避免链式更新时中间态被误抬。
        for rec in records:
            rid = rec["id"]
            is_new, is_old = rid in supersedes, rid in superseded
            if is_new and not is_old:
                rec["score"] += weight
                if "supersedes_earlier" not in rec["flags"]:
                    rec["flags"].append("supersedes_earlier")
                if rid in explicit_updates and "explicit_update_cue" not in rec["flags"]:
                    rec["flags"].append("explicit_update_cue")
            elif is_old and not is_new:
                rec["score"] -= penalty
                if "superseded" not in rec["flags"]:
                    rec["flags"].append("superseded")

    def _fuse_and_order(self, records, fts_order, dense_order=None) -> list[dict]:
        feat_order = [
            r["id"]
            for r in sorted(records, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))
        ]
        if self.flags.get("rrf", True):
            # 加权 RRF：特征路与词法路权重可配（稠密通道开启时加入第三路）。
            # 实测（docs/EVAL.md 附录 A，合成集 medium）：提高词法权重会**持续抬高 MRR**
            # 同时**持续压低 Recall@20**（Recall@100 恒为 1.0，说明 gold 没丢、只是被挤出前 20）。
            # 默认 w_lex=0.1 是扫描点中唯一 Pareto 安全的取值：三种难度 MRR 均正增益且
            # Recall@20 全部保持 1.0000。取 0.1 的理由是"不牺牲 Recall@20"，
            # 不是早期注释里写的"防止等权抹平特征分"——那个说法已被扫描证伪。
            k = int(getattr(self.config, "rrf_k", RRF_K) or RRF_K)
            w_feat = float(getattr(self.config, "rrf_weight_feature", 1.0))
            w_lex = float(getattr(self.config, "rrf_weight_lexical", 0.25))
            fused: dict[str, float] = {}
            channels = [(feat_order, w_feat), (fts_order, w_lex)]
            if dense_order:
                w_dense = float(getattr(self.config, "dense_rrf_weight", 0.5))
                if w_dense != 0.0:
                    channels.append((dense_order, w_dense))
            for order, weight in channels:
                if weight == 0.0:
                    continue
                for pos, doc_id in enumerate(order):
                    fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + pos + 1)
            for rec in records:
                rec["score"] = fused.get(rec["id"], 0.0)
                if "rrf" not in rec["flags"]:
                    rec["flags"].append("rrf")
        # 确定性排序：分值降序 -> 内容短优先 -> id 升序
        return sorted(records, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))

    def _dedup(self, ordered: list[dict]) -> list[dict]:
        if self.flags.get("dedup_views_by_sources", False):
            return _dedup_impl(ordered, drop_views_by_sources=True)
        return _dedup_impl(ordered, drop_views_by_sources=False)

    def _apply_slot_guarantee(self, ordered: list[dict], limit: int) -> list[dict]:
        """保证 top_k 中原始消息占比不低于配置比例（聚合视图不得挤掉原始证据）。

        view_max_ratio<1 时，进一步限制聚合视图在 top_k 中的占比；
        被挤出的位置按原排序补入原始消息（仅当有足够消息可补）。
        """
        head = ordered[:limit]
        ratio = float(self.config.message_slot_ratio or 0.0)
        view_cap = int(limit * (1.0 - float(self.config.view_max_ratio or 1.0)))
        if ratio <= 0 and view_cap <= 0:
            return head
        if len(ordered) <= limit:
            return head
        # 先按 message_slot_ratio 保底消息
        need = int(limit * ratio)
        have = sum(1 for r in head if r["view"] == "message")
        keep = [r for r in head if r["view"] == "message"]
        fillers = [r for r in head if r["view"] != "message"]
        extra = [r for r in ordered[limit:] if r["view"] == "message"]
        if have < need and extra:
            drop = min(need - have, len(fillers), len(extra))
            merged = keep + extra[:drop] + fillers[: max(0, len(fillers) - drop)]
        else:
            merged = head
        # 视图占比上限：超出的视图从尾部挤出，用后续消息补位
        if 0 < view_cap < limit:
            msgs = [r for r in merged if r["view"] == "message"]
            views = [r for r in merged if r["view"] != "message"]
            if len(views) > view_cap:
                overflow = views[view_cap:]
                spare = [r for r in ordered[limit:] if r["view"] == "message"
                         and r not in msgs][: len(overflow)]
                merged = sorted(
                    msgs + spare + views[:view_cap],
                    key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]),
                )
        return sorted(merged, key=lambda x: (-x["score"], len(x["content"] or ""), x["id"]))[:limit]

    # -------------------------------------------------- 隐私与生命周期（删除）
    def delete_user(self, user_id: str) -> dict:
        def _do(con):
            n_msg = con.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            n_view = con.execute(
                "SELECT COUNT(*) FROM views WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            con.execute("DELETE FROM fts WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM views WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM requests WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM fts_facts WHERE user_id=?", (user_id,))
            con.execute("DELETE FROM facts WHERE user_id=?", (user_id,))
            try:  # dense_vectors 表惰性创建；未启用稠密通道时可能不存在
                con.execute("DELETE FROM dense_vectors WHERE user_id=?", (user_id,))
            except sqlite3.OperationalError:
                pass
            return {"user_id": user_id, "deleted_messages": n_msg, "deleted_views": n_view}

        result = self._write(_do)
        if self._dense is not None:
            with self._dense._lock:
                self._dense._cache.pop(user_id, None)
        return result

    def purge_all(self) -> dict:
        def _do(con):
            n = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            try:  # dense_vectors 表惰性创建；未启用稠密通道时可能不存在
                con.executescript(
                    "DELETE FROM fts; DELETE FROM messages; DELETE FROM views; "
                    "DELETE FROM requests; DELETE FROM sessions; DELETE FROM dense_vectors; "
                    "DELETE FROM fts_facts; DELETE FROM facts;"
                )
            except sqlite3.OperationalError:
                con.executescript(
                    "DELETE FROM fts; DELETE FROM messages; DELETE FROM views; "
                    "DELETE FROM requests; DELETE FROM sessions; "
                    "DELETE FROM fts_facts; DELETE FROM facts;"
                )
            return {"deleted_messages": n}

        result = self._write(_do)
        if self._dense is not None:
            with self._dense._lock:
                self._dense._cache.clear()
        return result

    # --------------------------------------------------------------- 只读辅助
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """只读查询辅助（审计 / 测试用）。写操作必须走 _write。"""
        with self.connection() as con:
            return con.execute(sql, params).fetchall()

    def count(self, user_id: str | None = None) -> int:
        with self.connection() as con:
            if user_id:
                return con.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_id=?", (user_id,)
                ).fetchone()[0]
            return con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def stats(self) -> dict:
        with self.connection() as con:
            return {
                "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "views": con.execute("SELECT COUNT(*) FROM views").fetchone()[0],
                "facts": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                "users": con.execute("SELECT COUNT(DISTINCT user_id) FROM messages").fetchone()[0],
                "sessions": con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "db_path": self.db_path,
            }

    def close(self) -> None:
        if self._shared_mem_con is not None:
            try:
                self._shared_mem_con.commit()
                self._shared_mem_con.close()
            except sqlite3.Error:
                pass
            self._shared_mem_con = None
        while True:
            try:
                con = self._pool.get_nowait()
            except queue.Empty:
                break
            try:
                con.commit()
                con.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ["RetrieverDB", "AddResult", "SearchResult", "Evidence"]
