#!/usr/bin/env python3
"""test_message_view_only.py — message_view_only 视图过滤单测。"""
import unittest

from aml_retriever.retriever import RetrieverDB
from aml_retriever.config import RetrieverConfig


class TestMessageViewOnly(unittest.TestCase):

    def _results(self, flag: bool):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=True, rrf=True, dedup=True, supersession=True,
            message_view_only=flag,
        )
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
            {"role": "user", "content": "猎户座预算口径是 100 元。", "timestamp": 1_600_000_000_000},
            {"role": "user", "content": "猎户座预算口径已更新为 200 元。", "timestamp": 1_600_000_060_000},
            {"role": "user", "content": "猎户座预算口径说明已整理完成。", "timestamp": 1_600_000_120_000},
        ])
        try:
            res = db.search(user_id="u1", query="猎户座目前的预算口径是多少？", top_k=100)
            return [(e.view, e.content) for e in res.results]
        finally:
            db.close()

    def test_default_includes_views(self):
        rows = self._results(False)
        views = {v for v, _ in rows}
        self.assertTrue(views & {"window", "session-segment"}, f"默认路径应含聚合视图: {views}")
        self.assertGreater(len(rows), 3)

    def test_message_view_only_filters_views(self):
        rows = self._results(True)
        views = {v for v, _ in rows}
        self.assertEqual(views, {"message"}, f"过滤后只能有 message 视图: {views}")
        self.assertGreater(len(rows), 0)
        # 关键证据消息仍在（含时间前缀——ts_prefix 对 message 生效）
        contents = " ".join(c for _, c in rows)
        self.assertIn("200 元", contents)
        self.assertIn("[2020-", contents)

    def test_message_view_only_zero_regression_off(self):
        """关闭时保持历史行为：含聚合视图且按分数排序（首条为消息或视图均可）。"""
        rows_off = self._results(False)
        rows_on = self._results(True)
        # 关闭路径包含聚合视图
        self.assertTrue(any(v != "message" for v, _ in rows_off))
        # 开启路径全是 message
        self.assertTrue(all(v == "message" for v, _ in rows_on))


if __name__ == "__main__":
    unittest.main()
