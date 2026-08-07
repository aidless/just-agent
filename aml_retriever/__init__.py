"""AML Retriever — Agent Memory Challenge 参赛用记忆系统（Add / Search）。

分层：
  store.py      P0 词法基线（保留，作为回归基准）
  retriever.py  多视图混合证据检索引擎
  api.py        领域服务 + 官方契约 wrapper
  server.py     官方 Add/Search HTTP 传输层
"""
from .store import Store, tokenize  # P0 基线（回归保护，签名不得变更）
from .config import RetrieverConfig, DEFAULT_FLAGS
from .retriever import RetrieverDB, AddResult, SearchResult, Evidence
from .api import MemoryService, ApiError

__version__ = "1.0.0"

__all__ = [
    "Store",
    "tokenize",
    "RetrieverConfig",
    "DEFAULT_FLAGS",
    "RetrieverDB",
    "AddResult",
    "SearchResult",
    "Evidence",
    "MemoryService",
    "ApiError",
    "__version__",
]
