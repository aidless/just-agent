"""temporal_query_cache 的正确性测试（vNext 模块，默认关闭路径外的辅助）。

覆盖：strict 读取在 debounce 窗口内绝不返回旧值；非 strict 读取允许有限陈旧；
版本合并（debounce）与 single-flight。
"""
from __future__ import annotations

import threading
import time
import unittest

from aml_retriever.temporal_query_cache import TemporalQueryCache


class TestTemporalQueryCache(unittest.TestCase):
    def _cache(self, **kw):
        defaults = {"debounce_ms": 5, "max_stale_ms": 5}
        defaults.update(kw)
        return TemporalQueryCache(**defaults)

    def _wait_visible(self, cache, user_id: str, generation: int) -> None:
        deadline = time.monotonic() + 1.0
        while cache._generation(user_id) != generation and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_strict_never_returns_old_during_debounce_window(self):
        """pending 无效化未提升前，strict 读取必须重算，而不是返回旧 generation 条目。"""
        cache = self._cache()
        calls = [0]

        def compute():
            calls[0] += 1
            return f"result-{calls[0]}"

        self.assertEqual(cache.get_or_compute("u", "q", compute), "result-1")
        cache.invalidate("u", 1)
        # 不等待 debounce：pending 仍在，strict 读取必须触发重算。
        self.assertEqual(cache.get_or_compute("u", "q", compute, strict=True), "result-2")
        self.assertEqual(calls[0], 2)

    def test_non_strict_may_serve_stale_within_window(self):
        """非 strict 读取在 max_stale_ms 内允许返回旧值，保护 P99。"""
        cache = self._cache(max_stale_ms=500)
        calls = [0]

        def compute():
            calls[0] += 1
            return f"result-{calls[0]}"

        self.assertEqual(cache.get_or_compute("u", "q", compute), "result-1")
        cache.invalidate("u", 1)
        # pending 未提升：非 strict 允许旧值（max_stale 内）。
        self.assertEqual(cache.get_or_compute("u", "q", compute, strict=False), "result-1")
        self.assertEqual(calls[0], 1)

    def test_debounce_merges_revisions_and_computes_once(self):
        """连续多个 revision 合并为一个可见 generation；提升后只重算一次。"""
        cache = self._cache(debounce_ms=5)
        calls = [0]

        def compute():
            calls[0] += 1
            return f"result-{calls[0]}"

        self.assertEqual(cache.get_or_compute("u", "q", compute), "result-1")
        cache.invalidate("u", 1)
        cache.invalidate("u", 2)
        cache.invalidate("u", 3)
        self._wait_visible(cache, "u", 3)
        self.assertEqual(cache.get_or_compute("u", "q", compute, strict=True), "result-2")
        self.assertEqual(cache.get_or_compute("u", "q", compute, strict=True), "result-2")
        self.assertEqual(calls[0], 2)

    def test_single_flight_shares_compute_between_readers(self):
        """同 (user, query, generation) 的并发读取共享一次计算。"""
        cache = self._cache()
        calls = [0]
        barrier = threading.Event()

        def compute():
            calls[0] += 1
            barrier.wait(0.5)
            return f"result-{calls[0]}"

        results = [None, None]

        def reader(idx):
            results[idx] = cache.get_or_compute("u", "q", compute, strict=True)

        threads = [threading.Thread(target=reader, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        barrier.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(results, ["result-1", "result-1"])
        self.assertEqual(calls[0], 1)


if __name__ == "__main__":
    unittest.main()
