from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB

cfg = RetrieverConfig(db_path=":memory:", temporal_fallback_top_n=1, top_k_max=20)
cfg.flags["temporal_fallback"] = True
cfg.flags["views"] = False
db = RetrieverDB(cfg)
db.add(request_id="p1p2", user_id="u", session_id="s", messages=[
    {"role": "user", "content": "项目发布日期为 2026-08-14。"},
    {"role": "user", "content": "三天前确认了最新发布流程。"},
    {"role": "user", "content": "昨天撤回之前的时间表达。"},
])
with db.connection() as con:
    row = con.execute("SELECT abs_epoch, abs_granularity, abs_expression FROM messages WHERE seq=0").fetchone()
    assert row["abs_epoch"] is not None and row["abs_granularity"] == "day", dict(row)
result = db.search(user_id="u", query="发布日期和发布流程是什么？", top_k=20)
assert result.results
assert any("stored_content_absolute" in f for e in result.results for f in e.evidence_flags), result
print("P1/P2 integration OK")
