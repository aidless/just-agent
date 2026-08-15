"""temporal_query_cache.py — 连续撤回下的抗雪崩查询缓存。

设计：写入仍立即递增真实 ledger revision；缓存层把短时间内同一 user 的多次
无效化合并为一个可见 generation。读请求以 single-flight 合并重建，非 strict
读取可在 max_stale_ms 内返回旧条目，避免撤回风暴时所有线程阻塞。
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    generation: int
    value: T
    expires_at: float


class TemporalQueryCache(Generic[T]):
    def __init__(self, *, ttl_s: float = 20.0, jitter_ratio: float = 0.15,
                 debounce_ms: int = 250, max_stale_ms: int = 200):
        self.ttl_s = ttl_s
        self.jitter_ratio = jitter_ratio
        self.debounce_s = debounce_ms / 1000.0
        self.max_stale_s = max_stale_ms / 1000.0
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], _Entry[T]] = {}
        self._visible_gen: dict[str, int] = {}
        self._pending_gen: dict[str, int] = {}
        self._dirty_since: dict[str, float] = {}
        self._flights: dict[tuple[str, str, int], threading.Event] = {}

    def invalidate(self, user_id: str, ledger_revision: int) -> None:
        """写路径调用：合并短时间连续撤回/修改，不删除旧条目。"""
        with self._lock:
            self._pending_gen[user_id] = max(ledger_revision, self._pending_gen.get(user_id, 0))
            self._dirty_since.setdefault(user_id, time.monotonic())

    def _generation(self, user_id: str) -> int:
        now = time.monotonic()
        pending = self._pending_gen.get(user_id)
        if pending is not None and now - self._dirty_since.get(user_id, now) >= self.debounce_s:
            self._visible_gen[user_id] = pending
            self._pending_gen.pop(user_id, None)
            self._dirty_since.pop(user_id, None)
        return self._visible_gen.get(user_id, 0)

    def get_or_compute(self, user_id: str, query: str, compute: Callable[[], T], *, strict: bool = False) -> T:
        """读路径：相同 (user, query, generation) 只允许一条线程重算。

        strict=True：任何 pending invalidation 都不会返回旧值；适用于用户刚刚
        撤回后主动追问“当前日期”。strict=False：最多返回 max_stale_ms 的旧值，
        适用于后台预取和非关键 UI，优先保护 P99。
        """
        key = (user_id, query)
        while True:
            with self._lock:
                generation = self._generation(user_id)
                entry = self._entries.get(key)
                now = time.monotonic()
                pending = user_id in self._pending_gen
                # fresh 必须同时满足“无 pending 无效化”：debounce 窗口内
                # pending 尚未提升为 visible generation，此时即使 entry 的
                # generation 与 visible 相同，也不能把它当作新鲜值返回——
                # 否则 strict 读取会违反“绝不返回旧值”的承诺。
                fresh = (entry and not pending and entry.generation == generation
                         and now < entry.expires_at)
                stale_ok = (entry and not strict and pending and
                            now < entry.expires_at + self.max_stale_s)
                if fresh or stale_ok:
                    return entry.value
                flight_key = (user_id, query, generation)
                flight = self._flights.get(flight_key)
                if flight is None:
                    flight = threading.Event()
                    self._flights[flight_key] = flight
                    leader = True
                else:
                    leader = False
            if not leader:
                flight.wait(timeout=5.0)
                continue
            try:
                value = compute()
                with self._lock:
                    # 若计算过程中又发生撤回，不将旧 generation 的结果当作新鲜缓存。
                    if self._generation(user_id) == generation and user_id not in self._pending_gen:
                        jitter = self.ttl_s * random.uniform(-self.jitter_ratio, self.jitter_ratio)
                        self._entries[key] = _Entry(generation, value, time.monotonic() + self.ttl_s + jitter)
                return value
            finally:
                with self._lock:
                    self._flights.pop(flight_key, None)
                    flight.set()


if __name__ == "__main__":
    cache: TemporalQueryCache[str] = TemporalQueryCache(debounce_ms=5, max_stale_ms=5)
    calls = [0]
    def compute() -> str:
        calls[0] += 1
        return f"result-{calls[0]}"
    assert cache.get_or_compute("u", "q", compute) == "result-1"
    cache.invalidate("u", 1); cache.invalidate("u", 2); cache.invalidate("u", 3)
    # Windows 计时器粒度（默认 ~15.6ms）下 sleep(0.01) 可能不足 debounce(5ms) 即返回，
    # 导致提升未发生、strict 读到旧值。改为轮询等待可见 generation 到达 3，
    # 使自检与计时器分辨率无关。
    deadline = time.monotonic() + 1.0
    while cache._generation("u") != 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache.get_or_compute("u", "q", compute, strict=True) == "result-2"
    assert calls[0] == 2
    print("temporal query cache coalescing OK")
