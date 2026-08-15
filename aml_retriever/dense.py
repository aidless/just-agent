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

import numpy as np

DENSE_MODEL_DEFAULT = "BAAI/bge-small-en-v1.5"
DENSE_DIM_DEFAULT = 384

_embedder = None
_embedder_lock = threading.Lock()
_embedder_error: str | None = None


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
            from fastembed import TextEmbedding  # noqa: PLC0415

            model = os.environ.get("AML_DENSE_MODEL", DENSE_MODEL_DEFAULT)
            threads = int(os.environ.get("AML_DENSE_THREADS", "4") or "4")
            _embedder = TextEmbedding(model_name=model, threads=threads)
            return True, ""
        except Exception as exc:  # 网络失败 / 模型损坏 / 依赖缺失
            _embedder_error = f"dense backend unavailable: {exc}"
            return False, _embedder_error


def embed_texts(texts: list[str]) -> np.ndarray | None:
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

    def _load(self, user_id: str) -> tuple[np.ndarray, list[str]] | None:
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
