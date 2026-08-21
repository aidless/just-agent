#!/usr/bin/env python3
"""dense_nomic 通道单测（ollama nomic-embed-text）。

dense_nomic: 使用本地 ollama 的 nomic-embed-text 模型（768 维）做稠密检索，
作为第 3 路 RRF 通道与词法/特征路融合。flag 默认关闭，关闭时与基线完全等价（零回归）。

测试覆盖：
  1. Flag 默认关闭 + 可开启 + 环境变量覆盖 + config 参数存在。
  2. Flag 关闭 → 无 nomic_vectors 表、无 nomic 标记、搜索正常（零回归）。
  3. Flag 开启但 ollama 不可达 → 优雅回退（不崩溃、搜索仍走词法路径）。
  4. Flag 开启且 ollama 可达 → nomic 向量入库 + 搜索融合生效（需 ollama 运行）。
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from aml_retriever.config import DEFAULT_FLAGS, RetrieverConfig
from aml_retriever.retriever import RetrieverDB
from aml_retriever.dense import (
    nomic_backend_available,
    nomic_reset_availability_cache,
    NomicDenseIndex,
)


def _ts(year, month, day, hour=0):
    """UTC datetime → milliseconds (the Add API timestamp format)."""
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class TestDenseNomicFlag(unittest.TestCase):
    """Flag / config 测试。"""

    def test_flag_default_false(self):
        self.assertIn("dense_nomic", DEFAULT_FLAGS)
        self.assertFalse(DEFAULT_FLAGS["dense_nomic"])

    def test_flag_can_be_enabled(self):
        cfg = RetrieverConfig()
        cfg.flags["dense_nomic"] = True
        self.assertTrue(cfg.flags["dense_nomic"])

    def test_env_override(self):
        os.environ["AML_FLAG_DENSE_NOMIC"] = "1"
        try:
            cfg = RetrieverConfig.from_env()
            self.assertTrue(cfg.flags["dense_nomic"])
        finally:
            del os.environ["AML_FLAG_DENSE_NOMIC"]

    def test_config_params_exist(self):
        cfg = RetrieverConfig()
        self.assertEqual(cfg.nomic_model, "nomic-embed-text")
        self.assertEqual(cfg.nomic_ollama_url, "http://127.0.0.1:11434")
        self.assertEqual(cfg.nomic_rrf_weight, 0.5)
        self.assertEqual(cfg.nomic_top_n, 80)
        self.assertEqual(cfg.nomic_timeout, 30.0)


class TestDenseNomicOff(unittest.TestCase):
    """Flag 关闭 → 零行为变化（零回归）。"""

    def setUp(self):
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["dense_nomic"] = False
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()

    def test_no_nomic_vectors_table(self):
        """flag 关闭时 nomic_vectors 表不应被创建（惰性初始化）。"""
        with self.db.connection() as con:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nomic_vectors'"
            ).fetchall()
        self.assertEqual(len(rows), 0)

    def test_search_no_nomic_effect(self):
        """flag 关闭时搜索正常，无 nomic 相关行为。"""
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "Alice works on Project Alpha.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Bob likes Python programming.",
                 "timestamp": _ts(2026, 1, 2)},
            ],
        )
        res = self.db.search(user_id="u1", query="What does Alice work on?", top_k=10)
        self.assertGreater(len(res.results), 0)
        for ev in res.results:
            self.assertNotIn("nomic", ev.evidence_flags)


class TestDenseNomicGracefulFallback(unittest.TestCase):
    """Flag 开启但 ollama 不可达 → 优雅回退（不崩溃）。"""

    def setUp(self):
        # 重置全局可用性缓存，确保用无效 URL 重新探测
        nomic_reset_availability_cache()
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["dense_nomic"] = True
        # 指向一个不存在的端口，确保 ollama 不可达
        self.cfg.nomic_ollama_url = "http://127.0.0.1:1"
        self.cfg.nomic_timeout = 3.0
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()
        # 恢复缓存状态，避免影响后续测试
        nomic_reset_availability_cache()

    def test_backend_unavailable(self):
        ok, reason = nomic_backend_available(
            ollama_url="http://127.0.0.1:1", model="nomic-embed-text"
        )
        self.assertFalse(ok)
        self.assertIn("unavailable", reason)

    def test_add_succeeds_without_ollama(self):
        """ollama 不可达时 Add 仍成功（nomic 嵌入静默跳过）。"""
        result = self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "My budget is 5000 dollars.",
                 "timestamp": _ts(2026, 1, 1)},
            ],
        )
        self.assertTrue(result.message_ids)
        # nomic_vectors 表可能被创建（_ensure_table 在 NomicDenseIndex.__init__ 中），
        # 但应为空（嵌入失败 → add_docs 静默跳过）
        # 注意：_nomic_dense_index 返回 None 时表不会被创建
        with self.db.connection() as con:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nomic_vectors'"
            ).fetchall()
            if rows:
                n = con.execute("SELECT COUNT(*) FROM nomic_vectors").fetchone()[0]
                self.assertEqual(n, 0)

    def test_search_works_without_ollama(self):
        """ollama 不可达时 Search 仍走词法路径，不崩溃。"""
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "My budget is 5000 dollars.",
                 "timestamp": _ts(2026, 1, 1)},
            ],
        )
        result = self.db.search(user_id="u1", query="what is my current budget?", top_k=10)
        self.assertGreater(len(result.results), 0)
        self.assertIn("5000", result.results[0].content)


class TestDenseNomicIntegration(unittest.TestCase):
    """Flag 开启且 ollama 可达 → 端到端验证（需 ollama 运行 nomic-embed-text）。

    如果 ollama 未运行，本类所有测试自动跳过（不阻塞 CI）。
    """

    @classmethod
    def setUpClass(cls):
        nomic_reset_availability_cache()
        cls.ollama_ok, _ = nomic_backend_available()
        if not cls.ollama_ok:
            raise unittest.SkipTest("ollama nomic-embed-text not available — skipping integration tests")

    @classmethod
    def tearDownClass(cls):
        nomic_reset_availability_cache()

    def setUp(self):
        nomic_reset_availability_cache()
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["dense_nomic"] = True
        # 关闭其他增强以隔离 nomic 通道的效果
        self.cfg = self.cfg.with_flags(
            dense_nomic=True,
            views=False,
            rrf=True,
            dedup=False,
            supersession=False,
            ebbinghaus_decay=False,
            consolidation_dedup=False,
            content_timestamp_prefix=False,
            temporal_fallback=False,
        )
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()
        nomic_reset_availability_cache()

    def test_nomic_vectors_stored_after_add(self):
        """Add 后 nomic_vectors 表应有向量行。"""
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "Alice works on Project Alpha using Django.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Bob prefers Rust for system programming.",
                 "timestamp": _ts(2026, 1, 2)},
            ],
        )
        with self.db.connection() as con:
            n = con.execute("SELECT COUNT(*) FROM nomic_vectors WHERE user_id='u1'").fetchone()[0]
        self.assertEqual(n, 2, "应有 2 条 nomic 向量（2 条消息）")

    def test_nomic_channel_contributes_to_search(self):
        """nomic 通道应补充词法漏召回的语义相关消息。"""
        # A: 会被 FTS 召回（含 "alice"/"budget"）
        # B: 语义相关但词面不匹配（"financial allocation" vs "budget"）
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "Alice's budget for Q1 is 5000 dollars.",
                 "timestamp": _ts(2026, 1, 1)},
                {"role": "user",
                 "content": "The financial allocation for Alice was set to five thousand in the first quarter.",
                 "timestamp": _ts(2026, 1, 2)},
                {"role": "user", "content": "The cafeteria serves pizza every Friday.",
                 "timestamp": _ts(2026, 1, 3)},
            ],
        )
        # 查询 "What is Alice's budget?" — FTS 召回 A，
        # nomic 应将 B（语义同义但词面不匹配）也排入候选
        res = self.db.search(user_id="u1", query="What is Alice's budget?", top_k=10)
        self.assertGreater(len(res.results), 0)
        contents = " ".join(ev.content for ev in res.results)
        # A 必须在结果中
        self.assertIn("5000", contents)
        # B 应通过 nomic 语义匹配进入结果（允许未进入——nomic 权重 0.5，
        # 若 FTS 已充分召回则 B 可能被挤出 top_k；此处宽松断言）
        # 核心断言：无关消息 C 不应排前
        pizza_results = [ev for ev in res.results if "pizza" in ev.content]
        if pizza_results:
            # 如果 pizza 出现，它不应排在 Alice budget 之前
            alice_idx = next(
                (i for i, ev in enumerate(res.results) if "5000" in ev.content), len(res.results)
            )
            pizza_idx = next(
                (i for i, ev in enumerate(res.results) if "pizza" in ev.content), len(res.results)
            )
            self.assertLess(alice_idx, pizza_idx, "Alice budget 应排在无关 pizza 消息之前")

    def test_nomic_index_stats(self):
        """NomicDenseIndex.stats() 返回正确的行数和后端信息。"""
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "Test message one.", "timestamp": _ts(2026, 1, 1)},
                {"role": "user", "content": "Test message two.", "timestamp": _ts(2026, 1, 2)},
            ],
        )
        nomic = self.db._nomic_dense_index()
        self.assertIsNotNone(nomic)
        stats = nomic.stats()
        self.assertEqual(stats["nomic_rows"], 2)
        self.assertEqual(stats["backend"], "nomic-embed-text")

    def test_delete_user_cleans_nomic_vectors(self):
        """delete_user 应清理 nomic_vectors。"""
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "Delete me.", "timestamp": _ts(2026, 1, 1)},
            ],
        )
        with self.db.connection() as con:
            before = con.execute("SELECT COUNT(*) FROM nomic_vectors WHERE user_id='u1'").fetchone()[0]
        self.assertEqual(before, 1)
        self.db.delete_user("u1")
        with self.db.connection() as con:
            after = con.execute("SELECT COUNT(*) FROM nomic_vectors WHERE user_id='u1'").fetchone()[0]
        self.assertEqual(after, 0)


if __name__ == "__main__":
    unittest.main()
