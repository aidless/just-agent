#!/usr/bin/env python3
"""FlowGrid vNext 本地微基准：模块开关的相对开销。

不是官方榜单性能成绩，也不等同于生产 SLA；它用固定、非随机的多轮记忆负载，
在同一台机器上比较四个配置的相对延迟和吞吐。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB

WORKERS = 32
REQUESTS = 640
TOP_K = 20

QUERIES = [
    "林涛确认的 Apple Q3 发布会更新后日期是什么？",
    "Northstar 项目目前的预算是多少？",
    "王芳第三季度的偏好和计划是什么？",
    "最近修改后的发布流程是什么？",
    "Apple 发布会的原定日期和更新日期分别是什么？",
]


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def workload() -> list[dict]:
    """构造固定语料：包含显式时间、缺失时间、相对时间、同形词和项目实体。"""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    out: list[dict] = []
    for i in range(256):
        project = ("Apple Q3 发布会" if i % 4 == 0 else
                   "Northstar 项目" if i % 4 == 1 else
                   "第三季度客户计划" if i % 4 == 2 else "苹果水果采购")
        if i % 8 == 1:
            # 固定的缺 timestamp 记录，测试时间兜底本身。
            content = f"三天前，林涛确认 {project} 更新后的日期是 8月{(i % 20) + 1}日。"
            message = {"role": "user", "content": content}
        else:
            content = (
                f"2026-08-{(i % 27) + 1:02d}：{project} 的记录 {i}。"
                f"负责人{('林涛' if i % 3 == 0 else '王芳')}，预算为 {100 + i} 万元。"
            )
            message = {"role": "user", "content": content, "timestamp": ms(base + timedelta(hours=i))}
        out.append(message)
    return out


def make_db(*, entity: bool, temporal: bool, path: str) -> RetrieverDB:
    cfg = RetrieverConfig(db_path=path, top_k_default=TOP_K, top_k_max=TOP_K, max_candidates=400)
    cfg.flags.update({
        "views": False,
        "entity_boost_v2": entity,
        "temporal_fallback": temporal,
        "supersession": False,
        "temporal_intent": False,
    })
    db = RetrieverDB(cfg)
    messages = workload()
    # 分批写入，模拟真实多轮 session；每批 8 条。
    for start in range(0, len(messages), 8):
        db.add(
            request_id=f"bench-{start // 8}", user_id="bench-user",
            session_id=f"session-{start // 32}", messages=messages[start:start + 8],
        )
    return db


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    pos = min(len(values) - 1, max(0, int((len(values) - 1) * p)))
    return values[pos]


def run_case(name: str, *, entity: bool, temporal: bool) -> dict:
    fd, path = tempfile.mkstemp(prefix="flowgrid-bench-", suffix=".db")
    os.close(fd)
    try:
        db = make_db(entity=entity, temporal=temporal, path=path)
        # 预热：填充连接池、SQLite 页缓存和解释器路径。
        for query in QUERIES:
            db.search(user_id="bench-user", query=query, top_k=TOP_K)

        def one(index: int) -> float:
            query = QUERIES[index % len(QUERIES)]
            t0 = time.perf_counter()
            result = db.search(user_id="bench-user", query=query, top_k=TOP_K)
            elapsed = (time.perf_counter() - t0) * 1000
            if not result.results:
                raise RuntimeError("unexpected empty search result")
            return elapsed

        t_start = time.perf_counter()
        latencies: list[float] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(one, i) for i in range(REQUESTS)]
            for f in as_completed(futures):
                latencies.append(f.result())
        elapsed_s = time.perf_counter() - t_start
        return {
            "config": name,
            "workers": WORKERS,
            "requests": REQUESTS,
            "p50_ms": round(percentile(latencies, 0.50), 3),
            "p95_ms": round(percentile(latencies, 0.95), 3),
            "p99_ms": round(percentile(latencies, 0.99), 3),
            "mean_ms": round(statistics.mean(latencies), 3),
            "qps": round(REQUESTS / elapsed_s, 2),
            "wall_seconds": round(elapsed_s, 3),
        }
    finally:
        try:
            db.close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def main() -> int:
    cases = [
        ("baseline", False, False),
        ("entity_only", True, False),
        ("temporal_only", False, True),
        ("both", True, True),
    ]
    results = []
    for name, entity, temporal in cases:
        print(f"running {name} ...", flush=True)
        result = run_case(name, entity=entity, temporal=temporal)
        results.append(result)
        print(result, flush=True)

    out = Path(__file__).with_name("vnext_overhead_benchmark.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
