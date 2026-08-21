#!/usr/bin/env python3
"""kg_graph 知识图谱多跳桥接单测。

kg_graph: 实体-实体共现知识图谱——Add 阶段对每条消息用 features.extract_entities
提取实体，在同一消息内共现的实体对之间建立无向边，存入 kg_edges(entity_a, entity_b,
message_id, user_id)。Search 阶段若查询含 ≥2 个实体，找出「桥接实体」——即与 ≥2 个
查询实体分别在不同消息中共现的实体（HippoRAG PPR / YourMemory entity graph 的简化版），
把这些桥接实体连接的消息作为额外候选召回并加分提升
（boost = kg_bridge_boost_weight × min(connected_query_entities, 3) / 3）。

与 entity_graph（消息-消息邻接边 BFS 扩展）正交：entity_graph 在消息层建图、按共享
实体做 BFS 扩展邻居消息；kg_graph 在实体层建图、找连接多个查询实体的桥接实体。

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


class TestKgGraph(unittest.TestCase):
    """知识图谱多跳桥接。"""

    def test_off_by_default(self):
        """kg_graph 默认关闭 → 不出现 kg_bridge 标记，桥接消息不被额外召回。"""
        cfg = RetrieverConfig(db_path=":memory:")  # 默认 flags
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice is assigned to Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Bob is also working on Project Alpha.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            res = db.search(user_id="u1", query="What connects Alice and Bob?", top_k=10)
            for e in res.results:
                self.assertNotIn("kg_bridge", e.evidence_flags,
                                 "kg_graph 关闭时不应有标记")
            # kg_edges 表应为空（flag 关闭时 Add 不建边）
            edges = db.query("SELECT COUNT(*) AS n FROM kg_edges WHERE user_id=?", ("u1",))
            self.assertEqual(edges[0]["n"], 0, "kg_graph 关闭时不应建任何边")
        finally:
            db.close()

    def test_finds_bridge_entity_multi_hop(self):
        """kg_graph 开启 → 桥接实体（Project/Alpha 连接 Alice 与 Bob）使其消息被提升。

        场景：m1=(Alice,Project,Alpha)、m2=(Bob,Project,Alpha) 共享 Project/Alpha。
        查询问 Alice 与 Bob 的联系时，Project/Alpha 是桥接实体（与两个查询实体分别
        在不同消息中共现），m1/m2 被标记 kg_bridge 并加分。m4 含 Alice 但与 Bob 无
        共享桥接实体 → 被 FTS 召回但不获 kg_bridge 提升，排在 m1/m2 之后。
        """
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            kg_graph=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                # m1: 含 Alice + Project + Alpha（与 m2 共享 Project/Alpha）
                {"role": "user", "content": "Alice is assigned to Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                # m2: 含 Bob + Project + Alpha（与 m1 共享 Project/Alpha）
                {"role": "user", "content": "Bob is also working on Project Alpha.",
                 "timestamp": _ts(2026, 1, 2)},
                # m3: 无关消息，与 Alice/Bob 无共享实体
                {"role": "user", "content": "Carol leads Project Beta.",
                 "timestamp": _ts(2026, 1, 3)},
                # m4: 含 Alice（被 FTS 召回）但单实体无共现边 → 非桥接
                {"role": "user", "content": "Alice enjoys hiking on weekends.",
                 "timestamp": _ts(2026, 1, 4)},
            ])
            # 直接验证 kg_edges 表已建边：m1 应有 (alice,project) 等共现边
            m1_edges = db.query(
                "SELECT entity_a, entity_b FROM kg_edges WHERE user_id=? AND entity_a='alice'",
                ("u1",),
            )
            self.assertGreater(len(m1_edges), 0, "kg_graph 开启时应为 m1 建实体共现边")

            res = db.search(user_id="u1", query="What connects Alice and Bob?", top_k=10)
            bridge = [e for e in res.results if "kg_bridge" in e.evidence_flags]
            self.assertGreater(len(bridge), 0,
                               "kg_graph 应找出连接 Alice 与 Bob 的桥接消息")
            bridge_contents = " ".join(_raw(e.content) for e in bridge)
            # 桥接消息应同时覆盖 m1(Alice) 与 m2(Bob)
            self.assertIn("Project Alpha", bridge_contents,
                          "桥接消息应含共享实体 Project Alpha")
            # m4 含 Alice 但无桥接 → 不应带 kg_bridge 标记
            for e in res.results:
                if "hiking" in _raw(e.content):
                    self.assertNotIn("kg_bridge", e.evidence_flags,
                                     "非桥接消息不应获 kg_bridge 提升")
            # m3（Carol/Beta）不与任何查询实体共享桥接 → 不应进入结果
            for e in res.results:
                self.assertNotIn("Carol", _raw(e.content),
                                 "无关消息不应通过桥接进入结果")
            # 桥接消息应排在非桥接的 m4 之前
            ranks = {e.id: i for i, e in enumerate(res.results)}
            bridge_ids = {e.id for e in bridge}
            hiking = next((e for e in res.results if "hiking" in _raw(e.content)), None)
            if hiking is not None:
                for bid in bridge_ids:
                    self.assertLess(ranks[bid], ranks[hiking.id],
                                    "桥接消息应排在非桥接消息之前")
        finally:
            db.close()

    def test_no_bridge_when_single_query_entity(self):
        """kg_graph 开启但查询实体 <2 → 不激活桥接，零行为变化。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            kg_graph=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice is assigned to Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Bob is also working on Project Alpha.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            # "who is alice?" 全小写 → extract_entities 返回空（无 CJK、无大写拉丁）
            # 查询实体 <2 → 桥接不激活；FTS 仍召回 m1（含 alice）
            res = db.search(user_id="u1", query="who is alice?", top_k=10)
            for e in res.results:
                self.assertNotIn("kg_bridge", e.evidence_flags,
                                 "查询实体 <2 时 kg_graph 不应激活")
        finally:
            db.close()

    def test_no_regression_when_off(self):
        """kg_graph=False → 桥接消息不被额外召回/提升，与基线完全等价。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            kg_graph=False,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice is assigned to Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Bob is also working on Project Alpha.",
                 "timestamp": _ts(2026, 1, 2)},
            ])
            res = db.search(user_id="u1", query="What connects Alice and Bob?", top_k=10)
            for e in res.results:
                self.assertNotIn("kg_bridge", e.evidence_flags)
            edges = db.query("SELECT COUNT(*) AS n FROM kg_edges WHERE user_id=?", ("u1",))
            self.assertEqual(edges[0]["n"], 0, "kg_graph 关闭时不应建任何边")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
