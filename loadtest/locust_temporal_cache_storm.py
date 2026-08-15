"""FlowGrid 时间账本与缓存雪崩 Locust 压测。

前提：服务端除 AML /search 外，已暴露可选的时间账本 mutation endpoint，
用于将 update/retract 写入 TemporalLedger 并调用 TemporalQueryCache.invalidate()。
若当前部署尚无此 endpoint，设 ENABLE_MUTATIONS=0，只压测 read single-flight 路径。

示例：
  pip install locust
  FLOWGRID_URL=http://127.0.0.1:8080 ENABLE_MUTATIONS=1 \
    locust -f loadtest/locust_temporal_cache_storm.py --headless \
    -u 500 -r 50 -t 10m --html reports/cache-storm.html
"""
from __future__ import annotations

import os
import time
from itertools import cycle

from locust import HttpUser, between, task, events

USER_ID = os.getenv("FLOWGRID_USER_ID", "loadtest-temporal-user")
SEARCH_PATH = os.getenv("FLOWGRID_SEARCH_PATH", "/search")
MUTATION_PATH = os.getenv("TEMPORAL_MUTATION_PATH", "/temporal/claims")
ENABLE_MUTATIONS = os.getenv("ENABLE_MUTATIONS", "0").lower() in {"1", "true", "yes"}
STRICT_HEADER = os.getenv("STRICT_QUERY_HEADER", "X-Temporal-Consistency")
TOP_K = int(os.getenv("TOP_K", "20"))

QUERIES = [
    "Apple Q3 发布会当前日期是什么？",
    "林涛最近一次确认的发布时间是什么？",
    "Northstar 项目的有效截止日是什么？",
    "已撤回的日期不要作为当前事实，当前日期是什么？",
]


def _headers(*, strict: bool) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if strict:
        headers[STRICT_HEADER] = "strict"
    return headers


class TemporalCacheStormUser(HttpUser):
    """读写比例约 82% / 13% / 5%。

    普通读取允许 stale-while-revalidate；严格读取模拟“用户刚撤回后主动追问”；
    mutation 任务连续写同一 scope，以检查 debounce 与 single-flight 是否把重建压平。
    """
    host = os.getenv("FLOWGRID_URL", "http://127.0.0.1:8080")
    wait_time = between(0.0, 0.04)
    queries = cycle(QUERIES)

    def on_start(self):
        response = self.client.get("/health", name="health")
        if response.status_code >= 400:
            response.failure(f"health status={response.status_code}")

    @task(82)
    def normal_search(self):
        query = next(self.queries)
        with self.client.post(
            SEARCH_PATH,
            name="search.normal",
            headers=_headers(strict=False),
            json={"user_id": USER_ID, "query": query, "top_k": TOP_K},
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code}")
                return
            try:
                data = response.json()
                if not isinstance(data.get("results"), list):
                    response.failure("missing results list")
            except ValueError:
                response.failure("non-JSON response")

    @task(13)
    def strict_search_after_retract(self):
        query = next(self.queries)
        with self.client.post(
            SEARCH_PATH,
            name="search.strict",
            headers=_headers(strict=True),
            json={"user_id": USER_ID, "query": query, "top_k": TOP_K},
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"status={response.status_code}")

    @task(5)
    def mutation_storm(self):
        if not ENABLE_MUTATIONS:
            return
        # 同一 scope 连续 update/retract，刻意制造 generation 高频变化。
        # 服务端应把它写入 TemporalLedger，并只向缓存层发布合并后的 generation。
        sequence = int(time.time() * 1000)
        operation = "retract" if sequence % 2 else "update"
        payload = {
            "operation": operation,
            "user_id": USER_ID,
            "session_id": "loadtest-session",
            "scope": "project:apple-q3:release_date",
            "source_message_id": f"loadtest-{sequence}",
            "reason": "locust_cache_storm",
            # update 可用；retract 时服务端应在无显式 target 时撤回当前 active claim。
            "event_epoch": 1787270400 + (sequence % 7) * 86400,
            "granularity": "day",
            "confidence": "MEDIUM",
        }
        with self.client.post(
            MUTATION_PATH,
            name="temporal.mutation",
            headers={"Content-Type": "application/json"},
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 201, 202, 204):
                response.failure(f"status={response.status_code}; ensure mutation API is enabled")


@events.quitting.add_listener
def quality_gate(environment, **_kwargs):
    """默认门槛：错误率 <1%，P95 < 2s；请按部署 SLA 调整。"""
    stats = environment.stats.total
    error_ratio = (stats.num_failures / stats.num_requests) if stats.num_requests else 0.0
    if error_ratio > 0.01 or stats.get_response_time_percentile(0.95) > 2000:
        environment.process_exit_code = 1
