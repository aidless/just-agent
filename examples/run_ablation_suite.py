#!/usr/bin/env python3
"""一键运行 FlowGrid vNext 四配置消融并生成 JSON、Markdown 与 PNG 图表。

用于同一机器上的相对对比，采用交错顺序以减少“总是最后运行的配置更慢/更快”偏差。
不将本地微基准结果表述为官方榜单性能或生产 SLA。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import examples.benchmark_vnext_overhead as bench

CASES = {
    "baseline": (False, False),
    "entity_only": (True, False),
    "temporal_only": (False, True),
    "both": (True, True),
}
ORDERS = [
    ["baseline", "entity_only", "temporal_only", "both"],
    ["both", "temporal_only", "entity_only", "baseline"],
    ["entity_only", "baseline", "both", "temporal_only"],
]
METRICS = ["p50_ms", "p95_ms", "p99_ms", "mean_ms", "qps"]


def aggregate(rows: list[dict]) -> dict:
    result = {"config": rows[0]["config"], "runs": len(rows), "workers": rows[0]["workers"],
              "requests_per_run": rows[0]["requests"]}
    for metric in METRICS:
        values = [float(r[metric]) for r in rows]
        result[metric] = round(statistics.mean(values), 3)
        result[f"{metric}_stdev"] = round(statistics.stdev(values), 3) if len(values) > 1 else 0.0
    return result


def delta(value: float, baseline: float) -> float:
    return round((value - baseline) / baseline * 100.0, 2) if baseline else 0.0


def write_chart(summary: list[dict], out: Path) -> None:
    names = [r["config"] for r in summary]
    p50 = [r["p50_ms"] for r in summary]
    p95 = [r["p95_ms"] for r in summary]
    qps = [r["qps"] for r in summary]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), dpi=160)
    x = range(len(names))
    axes[0].bar([i - 0.18 for i in x], p50, width=0.36, label="P50", color="#2563eb")
    axes[0].bar([i + 0.18 for i in x], p95, width=0.36, label="P95", color="#f59e0b")
    axes[0].set_title("Search latency (lower is better)")
    axes[0].set_ylabel("milliseconds")
    axes[0].set_xticks(list(x), names, rotation=20)
    axes[0].legend()
    axes[0].grid(axis="y", alpha=0.25)
    axes[1].bar(list(x), qps, color="#059669")
    axes[1].set_title("Throughput (higher is better)")
    axes[1].set_ylabel("QPS")
    axes[1].set_xticks(list(x), names, rotation=20)
    axes[1].grid(axis="y", alpha=0.25)
    for i, value in enumerate(qps):
        axes[1].text(i, value, f"{value:.1f}", ha="center", va="bottom", fontsize=8)
    fig.suptitle("FlowGrid vNext local ablation microbenchmark", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)


def write_markdown(summary: list[dict], out: Path) -> None:
    base = next(r for r in summary if r["config"] == "baseline")
    lines = [
        "# FlowGrid vNext 自动化消融结果", "",
        "> 本地固定工作负载的相对微基准；不是官方评测成绩或生产 SLA。", "",
        "| 配置 | P50 ms | P95 ms | P99 ms | QPS | P50 相对基线 | QPS 相对基线 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in summary:
        lines.append(
            f"| {r['config']} | {r['p50_ms']:.3f} ± {r['p50_ms_stdev']:.3f} | "
            f"{r['p95_ms']:.3f} | {r['p99_ms']:.3f} | {r['qps']:.2f} | "
            f"{delta(r['p50_ms'], base['p50_ms']):+.2f}% | {delta(r['qps'], base['qps']):+.2f}% |"
        )
    lines += ["", "## 判读", "", "若 `temporal_only` 或 `both` 的 P50 相对基线高于 5%，优先启用有 `ts_ms` 候选的快速路径、缓存正文绝对日期解析结果，并限制完整兜底的候选数。"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--requests", type=int, default=320, help="每配置、每轮请求数")
    parser.add_argument("--out-dir", default="examples/ablation_artifacts")
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    # 复用已验证的固定语料和 32 并发 Search 实现；减少每轮数量以便多次重跑。
    bench.REQUESTS = args.requests
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw: list[dict] = []
    for round_index in range(args.repeats):
        order = ORDERS[round_index % len(ORDERS)]
        for name in order:
            entity, temporal = CASES[name]
            print(f"round={round_index + 1}/{args.repeats} config={name}", flush=True)
            row = bench.run_case(name, entity=entity, temporal=temporal)
            row["round"] = round_index + 1
            raw.append(row)

    summary = [aggregate([r for r in raw if r["config"] == name]) for name in CASES]
    (out_dir / "ablation_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "ablation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(summary, out_dir / "ablation_summary.md")
    write_chart(summary, out_dir / "ablation_chart.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"artifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
