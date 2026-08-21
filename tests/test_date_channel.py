#!/usr/bin/env python3
"""date_channel + recency_sort 候选单测。

date_channel: 日期窗口检索通道——查询含显式日期时，额外检索 event_time
落在该日期窗口内的消息（不依赖词法匹配），作为独立 RRF 通道融合。

recency_sort: 对 current-value / temporal 查询，按 event_time DESC 硬排序。

两个 flag 均默认关闭，关闭时与基线完全等价（零回归）。
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


class TestDateChannel(unittest.TestCase):
    """日期窗口检索通道。"""

    def test_off_by_default(self):
        """date_channel 默认关闭 → 不出现 date_channel 标记。"""
        cfg = RetrieverConfig(db_path=":memory:")  # 默认 flags
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Server restarted on 2026-08-14.",
                 "timestamp": _ts(2026, 8, 14, 10)},
            ])
            res = db.search(user_id="u1", query="What happened on 2026-08-14?", top_k=10)
            flagged = [e for e in res.results if "date_channel" in e.evidence_flags]
            self.assertEqual(len(flagged), 0, "date_channel 关闭时不应有标记")
        finally:
            db.close()

    def test_finds_message_by_date_window(self):
        """date_channel 开启 → 找到 FTS 漏召回但日期匹配的消息。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            date_channel=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                # 目标消息：时间在 2026-08-14，但内容与查询无词法重合
                {"role": "user", "content": "Server maintenance completed successfully.",
                 "timestamp": _ts(2026, 8, 14, 10)},
                # 干扰消息：时间在 2026-08-20，不应被日期窗口命中
                {"role": "user", "content": "New feature deployed to production.",
                 "timestamp": _ts(2026, 8, 20, 10)},
            ])
            res = db.search(user_id="u1", query="What happened on 2026-08-14?", top_k=10)
            date_flagged = [e for e in res.results if "date_channel" in e.evidence_flags]
            self.assertGreater(len(date_flagged), 0,
                               "date_channel 应找到日期匹配的消息")
            contents = " ".join(_raw(e.content) for e in date_flagged)
            self.assertIn("maintenance", contents,
                          "应找到 2026-08-14 的服务器维护消息")
            # 2026-08-20 的消息不应出现在 date_channel 命集中
            for e in date_flagged:
                self.assertNotIn("deployed", _raw(e.content),
                                 "日期窗口应排除 2026-08-20 的消息")
        finally:
            db.close()

    def test_no_dates_in_query_no_effect(self):
        """date_channel 开启但查询无显式日期 → 不激活，零行为变化。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            date_channel=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "My favorite color is blue.",
                 "timestamp": _ts(2026, 8, 14, 10)},
            ])
            res = db.search(user_id="u1", query="What is my favorite color?", top_k=10)
            date_flagged = [e for e in res.results if "date_channel" in e.evidence_flags]
            self.assertEqual(len(date_flagged), 0,
                             "查询无显式日期 → date_channel 不应激活")
        finally:
            db.close()

    def test_finds_via_abs_epoch_not_only_ts_ms(self):
        """date_channel 也通过 abs_epoch（正文绝对日期）找到消息，不限于 ts_ms。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            date_channel=True,
        )
        db = RetrieverDB(cfg)
        try:
            # 消息无 ts_ms，但正文含绝对日期 "2026-08-14."（句点结尾，
            # parse_absolute_temporal 的 day 正则要求日期后非 T/空格/数字）
            # Add 阶段会解析 abs_epoch 并持久化
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "The deployment was on 2026-08-14."},
            ])
            res = db.search(user_id="u1", query="What happened on 2026-08-14?", top_k=10)
            date_flagged = [e for e in res.results if "date_channel" in e.evidence_flags]
            self.assertGreater(len(date_flagged), 0,
                               "date_channel 应通过 abs_epoch 找到含正文日期的消息")
        finally:
            db.close()


class TestRecencySort(unittest.TestCase):
    """recency_sort：按 event_time DESC 硬排序。"""

    def test_off_by_default(self):
        """recency_sort 默认关闭 → 不改变正常排序。"""
        cfg = RetrieverConfig(db_path=":memory:")  # 默认 flags
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "There are 150 commits in main.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "Now there are 165 commits in main.",
                 "timestamp": 1_600_001_000_000},
            ])
            res = db.search(user_id="u1",
                            query="How many commits are in main?", top_k=10)
            self.assertGreater(len(res.results), 0, "应有结果返回")
            # 默认关闭时不强制最新在前（取决于评分，不做硬断言）
        finally:
            db.close()

    def test_sorts_newest_first_for_current_value(self):
        """recency_sort 开启 + current-value 查询 → 最新证据排第一。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            recency_sort=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "There are 150 commits in the main branch.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "There are 165 commits in the main branch.",
                 "timestamp": 1_600_001_000_000},
                {"role": "user", "content": "There are 200 commits in the main branch.",
                 "timestamp": 1_600_002_000_000},
            ])
            res = db.search(user_id="u1",
                            query="How many commits are in the main branch?", top_k=10)
            self.assertGreater(len(res.results), 0)
            top = _raw(res.results[0].content)
            self.assertIn("200", top,
                          "recency_sort 应将最新证据（200 commits）排到第一位")
        finally:
            db.close()

    def test_not_triggered_for_non_temporal_query(self):
        """recency_sort 开启但非 current-value/temporal 查询 → 不硬排序。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            recency_sort=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "Alice works on the backend.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "Bob works on the frontend.",
                 "timestamp": 1_600_002_000_000},
            ])
            # "Who works on the backend?" 不是 current-value 或 temporal 查询
            res = db.search(user_id="u1", query="Who works on the backend?", top_k=10)
            self.assertGreater(len(res.results), 0)
            # recency_sort 不应改变顺序（按特征分排序，Alice 应在前）
            top = _raw(res.results[0].content)
            self.assertIn("Alice", top,
                          "非 temporal 查询不应触发 recency_sort 硬排序")
        finally:
            db.close()

    def test_temporal_intent_triggers_sort(self):
        """recency_sort 开启 + temporal_intent 查询（含"now"）→ 最新排前。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            recency_sort=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "My office is on floor 3.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "My office is now on floor 5.",
                 "timestamp": 1_600_002_000_000},
            ])
            # "now" 触发 has_temporal_intent
            res = db.search(user_id="u1", query="Where is my office now?", top_k=10)
            self.assertGreater(len(res.results), 0)
            top = _raw(res.results[0].content)
            self.assertIn("floor 5", top,
                          "temporal_intent 查询应触发 recency_sort，最新值排前")
        finally:
            db.close()


class TestNoRegression(unittest.TestCase):
    """两个 flag 均关闭时与基线完全等价。"""

    def test_both_off_identical_to_baseline(self):
        """date_channel=False + recency_sort=False → 无 date_channel 标记，正常返回。"""
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=True, dedup=False, supersession=False,
            ebbinghaus_decay=False, consolidation_dedup=False,
            content_timestamp_prefix=False,
            date_channel=False, recency_sort=False,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "There are 150 commits in the main branch.",
                 "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "Now there are 165 commits in the main branch.",
                 "timestamp": 1_600_001_000_000},
                {"role": "user", "content": "Server maintenance on 2026-08-14.",
                 "timestamp": _ts(2026, 8, 14, 10)},
            ])
            # current-value 查询
            res1 = db.search(user_id="u1",
                             query="How many commits are in the main branch?", top_k=10)
            self.assertGreater(len(res1.results), 0)
            for e in res1.results:
                self.assertNotIn("date_channel", e.evidence_flags)

            # 日期查询
            res2 = db.search(user_id="u1", query="What happened on 2026-08-14?", top_k=10)
            for e in res2.results:
                self.assertNotIn("date_channel", e.evidence_flags)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
