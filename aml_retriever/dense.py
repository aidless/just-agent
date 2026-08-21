"""dense.py — 可选的 per-user 稠密检索通道（默认关闭、可回退、可消融）。

设计约束（见 docs 与 README 的零依赖承诺）：
  - 仅在 flags["dense"] 开启且后端可用时参与检索；后端不可用/超时 → 静默回退
    纯确定性路径，并记录 dense_unavailable 诊断事件。
  - 向量按 user_id 隔离；每个文档向量只绑定原始 message/view ID，绝不保存
    “压缩记忆”作为独立证据。
  - 权重、候选数、模型名全部配置化，支持单因素消融与 Docker 构建期烘焙。

后端：fastembed（onnxruntime，CPU），模型默认 BAAI/bge-small-en-v1.5（与 InvMem
公开实现同款，便于把“模型差异”与“架构差异”分开解释）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

DENSE_MODEL_DEFAULT = "BAAI/bge-small-en-v1.5"
DENSE_DIM_DEFAULT = 384

_embedder = None
_embedder_lock = threading.Lock()
_embedder_error: str | None = None


def _np():
    """惰性导入 numpy：保持默认路径零第三方依赖（提交镜像无 numpy）。"""
    import numpy  # noqa: PLC0415

    return numpy


def backend_available() -> tuple[bool, str]:
    """检查稠密后端是否可用（导入 + 模型）。只检查一次。"""
    global _embedder, _embedder_error
    if _embedder is not None:
        return True, ""
    if _embedder_error is not None:
        return False, _embedder_error
    with _embedder_lock:
        if _embedder is not None:
            return True, ""
        try:
            _np()  # numpy 缺失 → dense 不可用（回退纯确定性路径）
            from fastembed import TextEmbedding  # noqa: PLC0415

            model = os.environ.get("AML_DENSE_MODEL", DENSE_MODEL_DEFAULT)
            threads = int(os.environ.get("AML_DENSE_THREADS", "4") or "4")
            _embedder = TextEmbedding(model_name=model, threads=threads)
            return True, ""
        except Exception as exc:  # 网络失败 / 模型损坏 / 依赖缺失
            _embedder_error = f"dense backend unavailable: {exc}"
            return False, _embedder_error


def embed_texts(texts: list[str]):
    np = _np()
    ok, _ = backend_available()
    if not ok:
        return None
    try:
        vectors = list(_embedder.embed(texts, batch_size=64))
        return np.asarray(vectors, dtype=np.float32)
    except Exception:
        return None


class DenseIndex:
    """per-user 稠密索引。写时持久化到 SQLite（BLOB），读时按 user 惰性加载。

    线程安全：写入走 _write 路径（单写者）；读取在只读连接上进行，
    向量矩阵加载后不可变，numpy 只读访问天然线程安全。
    """

    def __init__(self, db: "RetrieverDB"):
        self.db = db
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[np.ndarray, list[str]]] = {}
        self._ensure_table()

    def _ensure_table(self) -> None:
        def _do(con):
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS dense_vectors(
                    user_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    vec BLOB NOT NULL,
                    PRIMARY KEY(user_id, doc_id)
                )
                """
            )

        self.db._write(_do)

    def add_docs(self, user_id: str, doc_ids: list[str], contents: list[str]) -> None:
        """为一批新文档嵌入并持久化。向量嵌入失败时静默跳过（保持可回退）。"""
        if not doc_ids or not contents:
            return
        vecs = embed_texts(contents)
        if vecs is None:
            return
        with self._lock:
            self._cache.pop(user_id, None)  # 使旧缓存失效

        def _do(con):
            con.executemany(
                "INSERT OR REPLACE INTO dense_vectors(user_id, doc_id, vec) VALUES(?,?,?)",
                [(user_id, did, vecs[i].tobytes()) for i, did in enumerate(doc_ids)],
            )

        self.db._write(_do)

    def _load(self, user_id: str):
        np = _np()
        with self._lock:
            if user_id in self._cache:
                return self._cache[user_id]
        with self.db.connection() as con:
            rows = con.execute(
                "SELECT doc_id, vec FROM dense_vectors WHERE user_id=? ORDER BY doc_id",
                (user_id,),
            ).fetchall()
        if not rows:
            return None
        ids = [r["doc_id"] for r in rows]
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
        payload = (mat, ids)
        with self._lock:
            self._cache[user_id] = payload
        return payload

    def top_n(self, user_id: str, query: str, n: int) -> list[tuple[str, float]]:
        """余弦相似度 Top-N（0~1 分，越大越相关）。"""
        np = _np()
        payload = self._load(user_id)
        if payload is None:
            return []
        mat, ids = payload
        qv = embed_texts([query])
        if qv is None:
            return []
        q = qv[0]
        norm = np.linalg.norm(mat, axis=1)
        qn = np.linalg.norm(q)
        if qn == 0 or norm.size == 0:
            return []
        scores = (mat @ q) / (norm * qn + 1e-9)
        top = int(min(n, len(ids)))
        if top <= 0:
            return []
        idx = np.argpartition(-scores, top - 1)[:top]
        order = idx[np.argsort(-scores[idx])]
        return [(ids[i], float(scores[i])) for i in order]

    def delete_user(self, user_id: str) -> None:
        def _do(con):
            con.execute("DELETE FROM dense_vectors WHERE user_id=?", (user_id,))

        self.db._write(_do)
        with self._lock:
            self._cache.pop(user_id, None)

    def purge_all(self) -> None:
        def _do(con):
            con.execute("DELETE FROM dense_vectors")

        self.db._write(_do)
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict:
        with self.db.connection() as con:
            n = con.execute("SELECT COUNT(*) FROM dense_vectors").fetchone()[0]
        return {"dense_rows": n, "backend": "fastembed" if backend_available()[0] else "unavailable"}


# ============================================================================
# nomic-embed-text 稠密通道（ollama 本地 API）
#
# 与 fastembed DenseIndex 并列的独立通道：
#   - 后端：本地 ollama 的 nomic-embed-text（768 维，1.82s/批，区分力 0.93）
#   - 存储：独立 nomic_vectors 表（不与 dense_vectors 混用，向量维度不同）
#   - 融合：作为第 3 路 RRF 通道与词法/特征路并列
#   - 回退：ollama 不可达 / 模型缺失 / 超时 → 静默回退纯确定性路径
# ============================================================================

NOMIC_DIM_DEFAULT = 768

_nomic_available: bool | None = None
_nomic_error: str | None = None
_nomic_lock = threading.Lock()


def _nomic_http_post(url: str, payload: dict, timeout: float):
    """零依赖 HTTP POST（urllib.request），返回解析后的 JSON dict 或 None。"""
    import urllib.request  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except Exception:
        return None


def nomic_backend_available(ollama_url: str = "", model: str = "") -> tuple[bool, str]:
    """检查 ollama nomic-embed-text 后端是否可用（只检查一次）。"""
    global _nomic_available, _nomic_error
    if _nomic_available is True:
        return True, ""
    if _nomic_available is False and _nomic_error is not None:
        return False, _nomic_error
    with _nomic_lock:
        if _nomic_available is not None:
            return _nomic_available, _nomic_error or ""
        base = ollama_url or os.environ.get(
            "AML_NOMIC_OLLAMA_URL", "http://127.0.0.1:11434"
        )
        mdl = model or os.environ.get("AML_NOMIC_MODEL", "nomic-embed-text")
        # 探测：发一条空文本嵌入请求，确认模型可达
        result = _nomic_http_post(
            f"{base.rstrip('/')}/api/embed",
            {"model": mdl, "input": ["ping"]},
            timeout=10.0,
        )
        if result is None or "embeddings" not in result:
            _nomic_available = False
            _nomic_error = "nomic backend unavailable: ollama /api/embed not reachable or no embeddings"
            return False, _nomic_error
        _nomic_available = True
        return True, ""


def nomic_embed_texts(texts: list[str], ollama_url: str = "", model: str = "",
                      timeout: float = 30.0, batch_size: int = 64):
    """调用 ollama /api/embed 批量嵌入文本，返回 numpy float32 矩阵或 None。"""
    np = _np()
    base = ollama_url or os.environ.get(
        "AML_NOMIC_OLLAMA_URL", "http://127.0.0.1:11434"
    )
    mdl = model or os.environ.get("AML_NOMIC_MODEL", "nomic-embed-text")
    all_vecs: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        result = _nomic_http_post(
            f"{base.rstrip('/')}/api/embed",
            {"model": mdl, "input": chunk},
            timeout=timeout,
        )
        if result is None or "embeddings" not in result:
            return None
        embs = result["embeddings"]
        if len(embs) != len(chunk):
            return None
        all_vecs.extend(embs)
    return np.asarray(all_vecs, dtype=np.float32)


def nomic_reset_availability_cache() -> None:
    """重置后端可用性缓存（测试用：ollama 启停后需重新探测）。"""
    global _nomic_available, _nomic_error
    with _nomic_lock:
        _nomic_available = None
        _nomic_error = None


class NomicDenseIndex:
    """per-user nomic 稠密索引（ollama nomic-embed-text）。

    与 DenseIndex (fastembed) 结构一致但独立运作：
      - 独立 nomic_vectors 表（768 维，不与 384 维 dense_vectors 混用）
      - ollama API 批量嵌入（/api/embed，支持多文本单请求）
      - 余弦相似度 Top-N，结果作为 RRF 第 3 路通道
      - 后端不可用/超时 → 静默回退，不影响词法路径
    """

    def __init__(self, db: "RetrieverDB"):
        self.db = db
        self._lock = threading.RLock()
        self._cache: dict[str, tuple["np.ndarray", list[str]]] = {}
        self._ensure_table()

    def _ensure_table(self) -> None:
        def _do(con):
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS nomic_vectors(
                    user_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    vec BLOB NOT NULL,
                    PRIMARY KEY(user_id, doc_id)
                )
                """
            )

        self.db._write(_do)

    def add_docs(self, user_id: str, doc_ids: list[str], contents: list[str]) -> None:
        """为一批新文档嵌入并持久化。嵌入失败时静默跳过（保持可回退）。"""
        if not doc_ids or not contents:
            return
        cfg = getattr(self.db, "config", None)
        ollama_url = getattr(cfg, "nomic_ollama_url", "") if cfg else ""
        model = getattr(cfg, "nomic_model", "") if cfg else ""
        timeout = float(getattr(cfg, "nomic_timeout", 30.0)) if cfg else 30.0
        vecs = nomic_embed_texts(contents, ollama_url=ollama_url, model=model, timeout=timeout)
        if vecs is None:
            return
        with self._lock:
            self._cache.pop(user_id, None)

        def _do(con):
            con.executemany(
                "INSERT OR REPLACE INTO nomic_vectors(user_id, doc_id, vec) VALUES(?,?,?)",
                [(user_id, did, vecs[i].tobytes()) for i, did in enumerate(doc_ids)],
            )

        self.db._write(_do)

    def _load(self, user_id: str):
        np = _np()
        with self._lock:
            if user_id in self._cache:
                return self._cache[user_id]
        with self.db.connection() as con:
            rows = con.execute(
                "SELECT doc_id, vec FROM nomic_vectors WHERE user_id=? ORDER BY doc_id",
                (user_id,),
            ).fetchall()
        if not rows:
            return None
        ids = [r["doc_id"] for r in rows]
        mat = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
        payload = (mat, ids)
        with self._lock:
            self._cache[user_id] = payload
        return payload

    def top_n(self, user_id: str, query: str, n: int) -> list[tuple[str, float]]:
        """余弦相似度 Top-N（0~1 分，越大越相关）。"""
        np = _np()
        payload = self._load(user_id)
        if payload is None:
            return []
        mat, ids = payload
        cfg = getattr(self.db, "config", None)
        ollama_url = getattr(cfg, "nomic_ollama_url", "") if cfg else ""
        model = getattr(cfg, "nomic_model", "") if cfg else ""
        timeout = float(getattr(cfg, "nomic_timeout", 30.0)) if cfg else 30.0
        qv = nomic_embed_texts([query], ollama_url=ollama_url, model=model, timeout=timeout)
        if qv is None:
            return []
        q = qv[0]
        norm = np.linalg.norm(mat, axis=1)
        qn = np.linalg.norm(q)
        if qn == 0 or norm.size == 0:
            return []
        scores = (mat @ q) / (norm * qn + 1e-9)
        top = int(min(n, len(ids)))
        if top <= 0:
            return []
        idx = np.argpartition(-scores, top - 1)[:top]
        order = idx[np.argsort(-scores[idx])]
        return [(ids[i], float(scores[i])) for i in order]

    def delete_user(self, user_id: str) -> None:
        def _do(con):
            con.execute("DELETE FROM nomic_vectors WHERE user_id=?", (user_id,))

        self.db._write(_do)
        with self._lock:
            self._cache.pop(user_id, None)

    def purge_all(self) -> None:
        def _do(con):
            con.execute("DELETE FROM nomic_vectors")

        self.db._write(_do)
        with self._lock:
            self._cache.clear()

    def stats(self) -> dict:
        with self.db.connection() as con:
            n = con.execute("SELECT COUNT(*) FROM nomic_vectors").fetchone()[0]
        return {"nomic_rows": n, "backend": "nomic-embed-text" if nomic_backend_available()[0] else "unavailable"}
