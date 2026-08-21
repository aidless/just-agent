#!/usr/bin/env python3
"""entity_graph BFS 扩展候选单测。

entity_graph: 实体边图 BFS 扩展——Add 阶段为每条消息提取实体（features.extract_entities）
并建立共享实体的邻接边（entity_edges 表: src_id, dst_id, shared_entity），Search 阶段
若查询含实体，从已召回的消息种子出发做 BFS（深度 graph_max_depth=2），收集未召回的
图邻居消息 ID 作为额外候选并加分提升（boost = graph_boost_weight / depth）。

flag 默认关闭，关闭时与基线完全等价（零回归）。
"""
import re
import unittest
from datetime import datetime, timezone

from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig


def _raw(content: str) -> str:
    """Strip timestamp / event-number prefixes added by shaping flags."""
    return re.sub(r"^(\[事件\d+\] )?(\[\S+\] )?", "", content)


def _ts(year, month, day, hour=0):
    """UTC datetime → milliseconds (the Add API timestamp format)."""
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class TestEntityGraph(unittest.TestCase):
    """实体边图 BFS 扩展。"""

    def test_off_by_default(self):
        """entity_graph 默认关闭 → 不出现 entity_graph 标记，图邻居不被召回。"""
        cfg = RetrieverConfig(db_path=":memory:")  # 默认 flags
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice works on Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                # B 与 A 共享 "Project Alpha" 实体，但 FTS 查询不命中 B
                {"role": "user", "content": "Project Alpha backend uses Django.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            res = db.search(user_id="u1", query="What does Alice work on?", top_k=10)
            for e in res.results:
                self.assertNotIn("entity_graph", e.evidence_flags,
                                 "entity_graph 关闭时不应有标记")
        finally:
            db.close()

    def test_finds_neighbor_via_shared_entity(self):
        """entity_graph 开启 → BFS 找到 FTS 漏召回但共享实体的图邻居。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            entity_graph=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                # A: 被 FTS 召回（含 "alice" 和 "work" token）
                {"role": "user", "content": "Alice works on Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                # B: 不被 FTS 召回（不含 "alice"/"work"），但与 A 共享 "Project"/"Alpha" 实体
                {"role": "user",
                 "content": "Project Alpha backend is built with Django and PostgreSQL.",
                 "timestamp": _ts(2026, 1, 2)},
                # C: 无关消息，不与 A/B 共享实体（"The"/"Friday" 仅 C 自身有）
                {"role": "user", "content": "The cafeteria serves pizza every Friday.",
                 "timestamp": _ts(2026, 1, 3)},
            ])
            res = db.search(user_id="u1", query="What does Alice work on?", top_k=10)
            # B 应通过 entity_graph BFS 被发现并标记
            graph_flagged = [e for e in res.results if "entity_graph" in e.evidence_flags]
            self.assertGreater(len(graph_flagged), 0,
                               "entity_graph 应通过共享实体找到 FTS 漏召回的邻居")
            contents = " ".join(_raw(e.content) for e in graph_flagged)
            self.assertIn("Django", contents,
                          "应找到含 Django 的图邻居消息")
            # C 不应出现在结果中（不与任何召回消息共享实体）
            for e in res.results:
                self.assertNotIn("pizza", _raw(e.content),
                                 "无关消息不应通过图扩展进入结果")
        finally:
            db.close()

    def test_no_entities_in_query_no_effect(self):
        """entity_graph 开启但查询无实体 → BFS 不激活，零行为变化。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            entity_graph=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                # A 与 B 共享 "Project Alpha" 实体 → entity_edges 已建边
                {"role": "user", "content": "Alice works on Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Project Alpha backend uses Django.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            # "who is alice?" → extract_entities 返回空（全小写，无 CJK）
            # FTS 仍召回 A（含 "alice" token），B 不被 FTS 召回
            res = db.search(user_id="u1", query="who is alice?", top_k=10)
            for e in res.results:
                self.assertNotIn("entity_graph", e.evidence_flags,
                                 "查询无实体 → entity_graph 不应激活")
            # B 不应出现（FTS 未召回 + BFS 未激活）
            for e in res.results:
                self.assertNotIn("Django", _raw(e.content),
                                 "查询无实体时图邻居不应被召回")
        finally:
            db.close()

    def test_no_regression_when_off(self):
        """entity_graph=False → 图邻居不被召回，与基线完全等价。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            entity_graph=False,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice works on Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user",
                 "content": "Project Alpha backend is built with Django and PostgreSQL.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            res = db.search(user_id="u1", query="What does Alice work on?", top_k=10)
            # entity_graph 关闭时，B 不应被召回（FTS 无命中 + 无图扩展）
            for e in res.results:
                self.assertNotIn("entity_graph", e.evidence_flags)
                self.assertNotIn("Django", _raw(e.content),
                                 "entity_graph 关闭时图邻居不应出现")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
