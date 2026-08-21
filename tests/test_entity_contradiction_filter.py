#!/usr/bin/env python3
"""entity_contradiction_filter 候选单测。

entity_contradiction_filter: 实体级极性矛盾过滤——按主实体分组候选消息，
检测同实体同谓词且极性相反（一肯定一否定 via has_negation）的消息对，标记
较早的消息为 superseded。对 current-value 查询（has_current_value_intent）
从结果中硬过滤 superseded 消息；对非 current-value 查询仅提升较新消息分数。

flag 默认关闭，关闭时与基线完全等价（零回归）。
"""
import re
import unittest

from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig
from aml_retriever import features


def _raw(content: str) -> str:
    """Strip timestamp / event-number prefixes added by shaping flags."""
    return re.sub(r"^(\[事件\d+\] )?(\[\S+\] )?", "", content)


class TestEntityContradictionFilter(unittest.TestCase):
    """实体级极性矛盾过滤。"""

    # 两条同实体、同谓词、相反极性的消息（Alice + backend project）
    _MSG_OLD_AFFIRM = {
        "role": "user",
        "content": "Alice works on the backend project.",
        "timestamp": 1_600_000_000_000,
    }
    _MSG_NEW_NEG = {
        "role": "user",
        "content": "Alice does not work on the backend project anymore.",
        "timestamp": 1_600_002_000_000,
    }
    _QUERY_CURRENT = "What is the backend project Alice works on?"
    _QUERY_NON_CURRENT = "Does Alice work on the backend?"

    def _make_db(self, **flag_overrides):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            entity_contradiction_filter=True,
            **flag_overrides,
        )
        return RetrieverDB(cfg)

    def test_off_by_default(self):
        """entity_contradiction_filter 默认关闭 → 无矛盾标记，两条消息均在。"""
        cfg = RetrieverConfig(db_path=":memory:")  # 默认 flags
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                self._MSG_OLD_AFFIRM, self._MSG_NEW_NEG,
            ])
            res = db.search(user_id="u1", query=self._QUERY_CURRENT, top_k=10)
            # 两条消息都应返回（无过滤）
            self.assertGreaterEqual(len(res.results), 2,
                                    "flag 关闭时两条消息都应返回")
            # 无 entity_contradiction_* 标记
            for e in res.results:
                self.assertNotIn("entity_contradiction_superseded", e.evidence_flags)
                self.assertNotIn("entity_contradiction_winner", e.evidence_flags)
        finally:
            db.close()

    def test_filters_superseded_for_current_value(self):
        """flag 开启 + current-value 查询 → 较早的肯定消息被过滤，仅留较新的否定消息。"""
        db = self._make_db()
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                self._MSG_OLD_AFFIRM, self._MSG_NEW_NEG,
            ])
            # 确认查询确实触发 has_current_value_intent
            self.assertTrue(features.has_current_value_intent(self._QUERY_CURRENT),
                            "测试查询应触发 has_current_value_intent")
            res = db.search(user_id="u1", query=self._QUERY_CURRENT, top_k=10)
            contents = [_raw(e.content) for e in res.results]
            # 较早的肯定消息（"works on the backend project." 无 "does not"）应被过滤
            affirm = [c for c in contents if "does not" not in c and "anymore" not in c
                      and "backend" in c]
            self.assertEqual(len(affirm), 0,
                             "较早的肯定消息应被 entity_contradiction_filter 过滤")
            # 较新的否定消息应保留
            neg = [c for c in contents if "does not" in c]
            self.assertGreater(len(neg), 0,
                               "较新的否定消息应保留在结果中")
            # 较新消息应有 entity_contradiction_winner 标记
            winners = [e for e in res.results
                       if "entity_contradiction_winner" in e.evidence_flags]
            self.assertGreater(len(winners), 0,
                               "较新消息应标记 entity_contradiction_winner")
        finally:
            db.close()

    def test_keeps_both_for_non_current_value(self):
        """flag 开启但非 current-value 查询 → 两条消息都保留，较新消息有 winner 标记。"""
        db = self._make_db()
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                self._MSG_OLD_AFFIRM, self._MSG_NEW_NEG,
            ])
            # 确认查询不触发 has_current_value_intent
            self.assertFalse(features.has_current_value_intent(self._QUERY_NON_CURRENT),
                             "测试查询不应触发 has_current_value_intent")
            res = db.search(user_id="u1", query=self._QUERY_NON_CURRENT, top_k=10)
            contents = [_raw(e.content) for e in res.results]
            # 两条消息都应保留（非 current-value 查询不过滤）
            self.assertTrue(any("does not" in c for c in contents),
                            "较新的否定消息应保留")
            self.assertTrue(any("works on the backend" in c and "does not" not in c
                                for c in contents),
                            "较早的肯定消息也应保留（非 current-value 不过滤）")
            # 较新消息有 winner 标记
            winners = [e for e in res.results
                       if "entity_contradiction_winner" in e.evidence_flags]
            self.assertGreater(len(winners), 0,
                               "较新消息应标记 entity_contradiction_winner")
            # 较早消息有 superseded 标记（标记但不过滤）
            superseded = [e for e in res.results
                          if "entity_contradiction_superseded" in e.evidence_flags]
            self.assertGreater(len(superseded), 0,
                               "较早消息应标记 entity_contradiction_superseded")
        finally:
            db.close()

    def test_no_contradiction_no_effect(self):
        """flag 开启，同实体同谓词但同极性（均肯定）→ 无矛盾标记，无过滤。"""
        db = self._make_db()
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user",
                 "content": "Alice works on the backend project.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user",
                 "content": "Alice works on the frontend project.",
                 "timestamp": 1_600_002_000_000},
            ])
            res = db.search(user_id="u1", query=self._QUERY_CURRENT, top_k=10)
            # 两条消息都应返回（同极性 → 无矛盾 → 无过滤）
            self.assertGreaterEqual(len(res.results), 2,
                                    "同极性消息不应被过滤")
            # 无 entity_contradiction_* 标记
            for e in res.results:
                self.assertNotIn("entity_contradiction_superseded", e.evidence_flags)
                self.assertNotIn("entity_contradiction_winner", e.evidence_flags)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
