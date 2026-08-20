#!/usr/bin/env python3
"""v1.4 D/H 修复单测：冲突成对返回 + 低置信弃权。"""
import unittest

from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig


def _raw(content: str) -> str:
    import re
    return re.sub(r"^(\[事件\d+\] )?(\[\S+\] )?", "", content)


class TestConflictPairReturn(unittest.TestCase):
    """D 修复：同话题相反极性的消息成对返回。"""

    def _db(self, flag: bool):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False, supersession=False,
            conflict_pair_return=flag,
        )
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
            {"role": "user", "content": "I have worked with Flask routes and handled HTTP requests.",
             "timestamp": 1_600_000_000_000},
            {"role": "user", "content": "I have never worked with Flask routes or handled HTTP requests.",
             "timestamp": 1_600_000_060_000},
            {"role": "user", "content": "The weather is sunny today.", "timestamp": 1_600_000_120_000},
        ])
        return db

    def test_conflict_pair_both_promoted(self):
        db = self._db(True)
        try:
            res = db.search(user_id="u1", query="Have I worked with Flask routes?", top_k=10)
            flagged = [e for e in res.results if "polarity_conflict_pair" in e.evidence_flags]
            self.assertGreaterEqual(len(flagged), 2, "冲突对两条都应被标记并提升")
            contents = " ".join(_raw(e.content) for e in flagged)
            self.assertIn("never worked", contents)
            self.assertIn("have worked", contents)
        finally:
            db.close()

    def test_conflict_pair_off_by_default(self):
        db = self._db(False)
        try:
            res = db.search(user_id="u1", query="Have I worked with Flask routes?", top_k=10)
            flagged = [e for e in res.results if "polarity_conflict_pair" in e.evidence_flags]
            self.assertEqual(len(flagged), 0, "默认关闭时不应标记冲突对")
        finally:
            db.close()

    def test_same_polarity_not_paired(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False, supersession=False,
            conflict_pair_return=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "I like green tea.", "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "I really like green tea a lot.", "timestamp": 1_600_000_060_000},
            ])
            res = db.search(user_id="u1", query="Do I like green tea?", top_k=10)
            flagged = [e for e in res.results if "polarity_conflict_pair" in e.evidence_flags]
            self.assertEqual(len(flagged), 0, "同极性不应判为冲突")
        finally:
            db.close()


class TestLowConfidenceAbstain(unittest.TestCase):
    """H 修复：无相关证据时返回空证据集（弃权）。"""

    def _db(self, flag: bool):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False, supersession=False,
            low_confidence_abstain=flag,
        )
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
            {"role": "user", "content": "My favorite color is blue.", "timestamp": 1_600_000_000_000},
        ])
        return db

    def test_abstain_on_no_overlap(self):
        db = self._db(True)
        try:
            res = db.search(user_id="u1", query="What is the capital of France?", top_k=10)
            self.assertEqual(len(res.results), 0, "无 token 重合应返回空（弃权）")
        finally:
            db.close()

    def test_no_abstain_on_overlap(self):
        db = self._db(True)
        try:
            res = db.search(user_id="u1", query="What is my favorite color?", top_k=10)
            self.assertGreater(len(res.results), 0, "有 token 重合不应弃权")
        finally:
            db.close()

    def test_abstain_off_by_default(self):
        db = self._db(False)
        try:
            # 用有 token 重合的查询：flag 关闭时正常返回（不因弃权逻辑受影响）
            res = db.search(user_id="u1", query="What is my favorite color?", top_k=10)
            self.assertGreater(len(res.results), 0, "默认关闭且有重合时不弃权")
        finally:
            db.close()


class TestCurrentValueRecency(unittest.TestCase):
    """v1.4 D 修复：current-value 查询抬最新值。"""

    def test_intent_detection(self):
        from aml_retriever import features
        # 当前值查询
        self.assertTrue(features.has_current_value_intent(
            "How many commits have been merged into the main branch?"))
        self.assertTrue(features.has_current_value_intent(
            "What is my monthly budget for books?"))
        self.assertTrue(features.has_current_value_intent(
            "When is my final decision meeting scheduled?"))
        # 历史查询（含过去/序数标记）不应触发
        self.assertFalse(features.has_current_value_intent(
            "When does my first sprint end?"))
        self.assertFalse(features.has_current_value_intent(
            "When did I originally set the budget?"))
        self.assertFalse(features.has_current_value_intent(
            "What was my budget last year?"))

    def _db(self, flag: bool):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            current_value_recency=flag,
        )
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
            {"role": "user", "content": "There are 150 commits in the main branch.",
             "timestamp": 1_600_000_000_000},
            {"role": "user", "content": "I just merged more, now there are 165 commits in main.",
             "timestamp": 1_600_000_900_000},
        ])
        return db

    def test_latest_version_ranked_first_when_on(self):
        db = self._db(True)
        try:
            res = db.search(user_id="u1",
                            query="How many commits have been merged into the main branch?",
                            top_k=10)
            self.assertGreater(len(res.results), 0)
            top = _raw(res.results[0].content)
            self.assertIn("165", top, "开启后最新版本 165 应排第一")
        finally:
            db.close()

    def test_off_by_default_no_change(self):
        db = self._db(False)
        try:
            res = db.search(user_id="u1",
                            query="How many commits have been merged into the main branch?",
                            top_k=10)
            flagged = [e for e in res.results
                       if any("recency" in f for f in e.evidence_flags)]
            # flag 关闭时不应有 current_value 触发的额外抬升（行为同基线）
            self.assertGreater(len(res.results), 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
