#!/usr/bin/env python3
"""test_consolidation_dedup.py — Consolidation N->1 deterministic dedup 单测。

覆盖：默认关闭零回归、簇合并触发、摘要保留最新内容、source_ids 并集、
归档消息不再独立召回、阈值未达不合并、时间窗排除、幂等、gold 召回安全。
"""
import unittest

from aml_retriever.config import DEFAULT_FLAGS, RetrieverConfig
from aml_retriever.retriever import RetrieverDB

# 同主题、高 token 重合的英文消息（containment ≈ 0.83 ≥ 默认 0.5 阈值）
_BASE_TS = 1_600_000_000_000  # ms
_MSGS = [
    {"role": "user", "content": "The project budget is 100 dollars.",
     "timestamp": _BASE_TS},
    {"role": "user", "content": "The project budget is 200 dollars.",
     "timestamp": _BASE_TS + 60_000},
    {"role": "user", "content": "The project budget is 300 dollars.",
     "timestamp": _BASE_TS + 120_000},
    {"role": "user", "content": "The project budget is 400 dollars.",
     "timestamp": _BASE_TS + 180_000},
]


def _cfg(**flag_overrides):
    base = dict(views=False, rrf=False, supersession=False)
    base.update(flag_overrides)
    return RetrieverConfig(db_path=":memory:").with_flags(**base)


def _raw(content: str) -> str:
    """去掉 [时间] 前缀，返回原始证据文本。"""
    if content.startswith("[") and "] " in content[:64]:
        return content[content.index("] ") + 2:]
    return content


class TestConsolidationDefaults(unittest.TestCase):
    def test_flag_off_by_default(self):
        # consolidation_dedup 已通过授权数据门禁提升为默认 ON（见 config.DEFAULT_FLAGS）。
        self.assertTrue(DEFAULT_FLAGS["consolidation_dedup"])

    def test_off_no_consolidation_views(self):
        """关闭时：无 consolidation 视图、所有消息 consolidated=0、行为不变。"""
        db = RetrieverDB(_cfg(consolidation_dedup=False))
        try:
            for i, m in enumerate(_MSGS):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[m])
            rows = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(rows, 0)
            archived = db.query(
                "SELECT COUNT(*) AS n FROM messages WHERE user_id='u1' AND consolidated=1"
            )[0]["n"]
            self.assertEqual(archived, 0)
            # 原始消息仍可独立检索
            res = db.search(user_id="u1", query="project budget", top_k=20)
            msg_views = [e for e in res.results if e.view == "message"]
            self.assertGreater(len(msg_views), 0)
        finally:
            db.close()


class TestConsolidationMerge(unittest.TestCase):
    def _add_four(self, db):
        ids = []
        for i, m in enumerate(_MSGS):
            r = db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[m])
            ids.extend(r.message_ids)
        return ids

    def test_cluster_merges_into_one_summary(self):
        """4 条同主题消息（3 已有 + 1 新）→ 1 条 consolidation 摘要， originals 归档。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            ids = self._add_four(db)
            self.assertEqual(len(ids), 4)
            # 恰好一条 consolidation 摘要
            crows = db.query(
                "SELECT view_id, content, source_ids FROM views "
                "WHERE view_type='consolidation' AND user_id='u1'"
            )
            self.assertEqual(len(crows), 1, "应产出恰好一条合并摘要")
            # 全部原始消息已归档
            archived = db.query(
                "SELECT COUNT(*) AS n FROM messages WHERE user_id='u1' AND consolidated=1"
            )[0]["n"]
            self.assertEqual(archived, 4, "簇内全部原始消息应被归档")
            # 归档消息已从 FTS 移除
            left_fts = db.query(
                "SELECT COUNT(*) AS n FROM fts WHERE user_id='u1' AND doc_type='message'"
            )[0]["n"]
            self.assertEqual(left_fts, 0, "归档消息不应再留在 FTS")
        finally:
            db.close()

    def test_summary_keeps_latest_content(self):
        """摘要 content = 簇内事件时间最新的那条消息原文。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            self._add_four(db)
            row = db.query(
                "SELECT content FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]
            self.assertIn("400 dollars", row["content"], "摘要应保留最新内容（400）")
            self.assertNotIn("100 dollars", row["content"])
        finally:
            db.close()

    def test_summary_source_ids_is_union(self):
        """摘要 source_ids = 簇内全部原始消息 ID 的并集（gold 召回安全）。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            ids = self._add_four(db)
            row = db.query(
                "SELECT source_ids FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]
            import json
            source_ids = json.loads(row["source_ids"])
            self.assertEqual(sorted(source_ids), sorted(ids),
                             "source_ids 应为全部原始 ID 并集")
        finally:
            db.close()

    def test_archived_messages_not_independent_candidates(self):
        """归档消息不再作为独立 message 候选；搜索只返回 consolidation 摘要。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            self._add_four(db)
            res = db.search(user_id="u1", query="project budget dollars", top_k=20)
            views = {e.view for e in res.results}
            self.assertIn("consolidation", views, "摘要应出现在结果中")
            # views=False 时归档消息已从 FTS 移除，不应有独立 message 候选
            self.assertNotIn("message", views,
                             "归档消息不应作为独立 message 候选返回")
            # 摘要的 source_message_ids 含全部原始 ID（gold 召回）
            summary = next(e for e in res.results if e.view == "consolidation")
            self.assertEqual(len(summary.source_message_ids), 4)
        finally:
            db.close()

    def test_gold_recall_via_summary_source_ids(self):
        """模拟 gold 判定：gold 消息 ID 在摘要 source_message_ids 中即命中。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            ids = self._add_four(db)
            res = db.search(user_id="u1", query="project budget", top_k=10)
            all_sources = set()
            for e in res.results:
                all_sources.update(e.source_message_ids)
            # 全部原始消息 ID 都可通过返回证据的 source_message_ids 命中
            for mid in ids:
                self.assertIn(mid, all_sources, f"gold {mid} 未被召回")
        finally:
            db.close()


class TestConsolidationThreshold(unittest.TestCase):
    def test_below_threshold_no_merge(self):
        """已有匹配数 < N（默认 3）时不合并。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            # 只加 3 条：第 3 条新消息只找到 2 条已有匹配 < 3
            for i in range(3):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[_MSGS[i]])
            crows = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(crows, 0, "未达阈值不应合并")
            archived = db.query(
                "SELECT COUNT(*) AS n FROM messages WHERE user_id='u1' AND consolidated=1"
            )[0]["n"]
            self.assertEqual(archived, 0)
        finally:
            db.close()

    def test_min_cluster_configurable(self):
        """N=2 时 3 条消息（2 已有 + 1 新）即可触发合并。"""
        cfg = RetrieverConfig(db_path=":memory:", consolidation_min_cluster=2).with_flags(
            views=False, rrf=False, supersession=False, consolidation_dedup=True
        )
        db = RetrieverDB(cfg)
        try:
            for i in range(3):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[_MSGS[i]])
            crows = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(crows, 1, "N=2 时 3 条应触发合并")
        finally:
            db.close()


class TestConsolidationTimeWindow(unittest.TestCase):
    def test_far_apart_messages_not_merged(self):
        """事件时间超出窗口的已有消息不参与匹配。"""
        cfg = RetrieverConfig(
            db_path=":memory:", consolidation_time_window_seconds=30
        ).with_flags(
            views=False, rrf=False, supersession=False, consolidation_dedup=True
        )
        db = RetrieverDB(cfg)
        try:
            # 消息间隔 60s > 窗口 30s：新消息与已有消息均超出窗口
            for i, m in enumerate(_MSGS):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[m])
            crows = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(crows, 0, "超出时间窗的消息不应合并")
        finally:
            db.close()

    def test_within_window_merged(self):
        """事件时间在窗口内时正常合并。"""
        cfg = RetrieverConfig(
            db_path=":memory:", consolidation_time_window_seconds=600
        ).with_flags(
            views=False, rrf=False, supersession=False, consolidation_dedup=True
        )
        db = RetrieverDB(cfg)
        try:
            for i, m in enumerate(_MSGS):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[m])
            crows = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(crows, 1, "窗口内应合并")
        finally:
            db.close()


class TestConsolidationIdempotent(unittest.TestCase):
    def test_replayed_request_no_duplicate_summary(self):
        """同 request_id 重放不重复落库（Add 幂等），不产生重复摘要。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            # 一次性加入 4 条（批内第 4 条触发合并）
            db.add(request_id="batch", user_id="u1", session_id="s1", messages=_MSGS)
            first = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            # 重放同一 request_id
            db.add(request_id="batch", user_id="u1", session_id="s1", messages=_MSGS)
            second = db.query(
                "SELECT COUNT(*) AS n FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]["n"]
            self.assertEqual(first, second, "幂等重放不应新增摘要")
        finally:
            db.close()

    def test_deterministic_view_id(self):
        """相同来源集合产出相同 view_id（确定性）。"""
        db = RetrieverDB(_cfg(consolidation_dedup=True))
        try:
            db.add(request_id="batch", user_id="u1", session_id="s1", messages=_MSGS)
            row = db.query(
                "SELECT view_id FROM views WHERE view_type='consolidation' AND user_id='u1'"
            )[0]
            self.assertTrue(row["view_id"].startswith("c_"))
        finally:
            db.close()


class TestConsolidationWithViewsOn(unittest.TestCase):
    """生产路径（views=True）下摘要与窗口视图共存。"""

    def test_summary_surfaces_alongside_window_views(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=True, rrf=True, supersession=True, consolidation_dedup=True
        )
        db = RetrieverDB(cfg)
        try:
            for i, m in enumerate(_MSGS):
                db.add(request_id=f"r{i}", user_id="u1", session_id="s1", messages=[m])
            res = db.search(user_id="u1", query="project budget dollars", top_k=20)
            views = {e.view for e in res.results}
            # consolidation 摘要应在结果中（不被 dedup 丢弃）
            self.assertIn("consolidation", views,
                          "摘要不应被 source-coverage dedup 丢弃")
            # 归档消息不作为独立 message 候选
            msg_results = [e for e in res.results if e.view == "message"]
            for e in msg_results:
                self.assertNotIn("100 dollars", _raw(e.content))
                self.assertNotIn("200 dollars", _raw(e.content))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
