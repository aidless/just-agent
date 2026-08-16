#!/usr/bin/env python3
"""test_shaping.py — 内容塑形（ordering_prefix / chrono_ordering）单测。"""
import unittest

from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig


def _raw(content: str) -> str:
    # 去掉 [事件N] 与 [时间] 前缀，还原正文
    import re
    return re.sub(r"^(\[事件\d+\] )?(\[\S+\] )?", "", content)


class TestOrderingShaping(unittest.TestCase):

    def _db(self, flags: dict):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False,
            supersession=False, **flags,
        )
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
            {"role": "user", "content": "第一个事件：定会议。", "timestamp": 1_600_000_000_000},
            {"role": "user", "content": "第三个事件：写报告。", "timestamp": 1_600_000_120_000},
            {"role": "user", "content": "第二个事件：做调研。", "timestamp": 1_600_000_060_000},
        ])
        return db

    def test_ordering_prefix_sorts_and_numbers(self):
        db = self._db({"ordering_prefix": True})
        try:
            res = db.search(user_id="u1", query="按照先后顺序，我依次做了哪些事？", top_k=10)
            contents = [_raw(e.content) for e in res.results if e.view == "message"]
            self.assertEqual(contents, ["第一个事件：定会议。", "第二个事件：做调研。", "第三个事件：写报告。"])
            # 前缀存在
            raw = [e.content for e in res.results if e.view == "message"]
            self.assertTrue(raw[0].startswith("[事件1]"))
            self.assertTrue(raw[1].startswith("[事件2]"))
            self.assertTrue(raw[2].startswith("[事件3]"))
        finally:
            db.close()

    def test_ordering_prefix_off_by_default(self):
        db = self._db({})
        try:
            res = db.search(user_id="u1", query="按照先后顺序，我依次做了哪些事？", top_k=10)
            raw = [e.content for e in res.results if e.view == "message"]
            self.assertEqual(len(raw), 3)
            # 默认路径：无 [事件N] 前缀（顺序为分数序，合法）
            self.assertFalse(raw[0].startswith("[事件"))
            self.assertFalse(raw[1].startswith("[事件"))
        finally:
            db.close()

    def test_ordering_prefix_only_ordering_queries(self):
        # 非顺序意图查询：不加前缀、不改顺序（分数序）
        db = self._db({"ordering_prefix": True})
        try:
            res = db.search(user_id="u1", query="第二个事件是什么内容？", top_k=10)
            raw = [e.content for e in res.results if e.view == "message"]
            self.assertFalse(raw[0].startswith("[事件"))
        finally:
            db.close()

    def test_chrono_ordering_sorts_without_prefix(self):
        db = self._db({"chrono_ordering": True})
        try:
            res = db.search(user_id="u1", query="目前进行到哪个阶段了？", top_k=10)
            contents = [_raw(e.content) for e in res.results if e.view == "message"]
            self.assertEqual(contents, ["第一个事件：定会议。", "第二个事件：做调研。", "第三个事件：写报告。"])
            raw = [e.content for e in res.results if e.view == "message"]
            self.assertFalse(raw[0].startswith("[事件"))
        finally:
            db.close()

    def test_chrono_ordering_only_temporal_queries(self):
        db = self._db({"chrono_ordering": True})
        try:
            res = db.search(user_id="u1", query="事件的具体内容分别是什么？", top_k=10)
            raw = [e.content for e in res.results if e.view == "message"]
            self.assertGreater(len(raw), 0)
            # 非时间意图查询：不加前缀（保持分数序）
            self.assertFalse(raw[0].startswith("[事件"))
        finally:
            db.close()

    def test_missing_timestamp_kept_last(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False, supersession=False,
            ordering_prefix=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "无时间戳的事件记录。", "timestamp": None},
                {"role": "user", "content": "早期事件记录。", "timestamp": 1_500_000_000_000},
            ])
            res = db.search(user_id="u1", query="按顺序说明事件记录的情况？", top_k=10)
            contents = [_raw(e.content) for e in res.results if e.view == "message"]
            self.assertEqual(contents, ["早期事件记录。", "无时间戳的事件记录。"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
