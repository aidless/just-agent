from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB
from aml_retriever.temporal_fallback import resolve_stored_month_day

# 同一 (month, day, anchor_day) 是纯函数缓存键；重复调用必须稳定。
r1 = resolve_stored_month_day(8, 28, 1786665600.0)  # 2026-08-14 UTC
r2 = resolve_stored_month_day(8, 28, 1786665600.0)
assert r1 and r2 and r1.epoch == r2.epoch and r1.source == "stored_month_day_anchor"

cfg = RetrieverConfig(db_path=":memory:", top_k_max=20)
cfg.flags.update({"temporal_fallback": True, "views": False})
db = RetrieverDB(cfg)
db.add(request_id="monthday", user_id="u", session_id="s", messages=[
    {"role": "user", "timestamp": 1786665600000, "content": "今天是 2026-08-14，发布会计划待确认。"},
    {"role": "user", "content": "发布会改为 8月28日。"},
])
with db.connection() as con:
    row = con.execute("SELECT partial_month, partial_day, partial_expression FROM messages WHERE seq=1").fetchone()
    assert tuple(row) == (8, 28, "8月28日"), tuple(row)
result = db.search(user_id="u", query="发布会日期是什么？", top_k=20)
assert any("stored_month_day_anchor" in f for e in result.results for f in e.evidence_flags), result
print("month-day anchor cache integration OK")
