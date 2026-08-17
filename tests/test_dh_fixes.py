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


if __name__ == "__main__":
    unittest.main()
