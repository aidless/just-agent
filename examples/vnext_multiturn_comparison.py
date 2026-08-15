#!/usr/bin/env python3
"""实体消歧 + 时间兜底在 FlowGrid 检索管道中的多轮案例对比。

本脚本直接调用 RetrieverDB 的内部候选、评分与 RRF 融合步骤，目的是
显式显示：基线分数、vNext 实体提升后的特征分、最终融合分、时间来源与 flags。
它是解释性示例，不替代官方全量评测。
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aml_retriever.config import RetrieverConfig
from aml_retriever.entity_disambiguate import apply_entity_boost_v2
from aml_retriever.retriever import RetrieverDB


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() * 1000)


USER_ID = "demo-user"
SESSION_ID = "launch-planning"
QUERY = "林涛确认的 Apple Q3 发布会更新后日期是什么？"

# 多轮对话：m1 缺少 timestamp，只能依赖“3 天前”与 m0 的会话锚点推算。
MESSAGES = [
    {
        "role": "user",
        "timestamp": ms("2026-08-14T00:00:00Z"),
        "content": "今天是 2026-08-14。Apple Q3 发布会原定日期是 2026-08-21。",
    },
    {
        "role": "user",
        # 故意缺省 timestamp：验证相对时间 + 会话锚点（m0）兜底。
        "content": "三天前，林涛确认 Apple Q3 发布会更新后的日期是 8月28日。",
    },
    {
        "role": "user",
        "timestamp": ms("2026-08-20T00:00:00Z"),
        "content": "Q3 财务审计材料今天送审，和 Apple 发布会日期无关。",
    },
    {
        "role": "user",
        "timestamp": ms("2026-08-22T00:00:00Z"),
        "content": "苹果水果采购预算已更新，供应商将于 2026-08-25 交付。",
    },
]


def make_config(*, enhanced: bool) -> RetrieverConfig:
    cfg = RetrieverConfig(db_path=":memory:", top_k_default=10, top_k_max=10)
    cfg.flags.update(
        {
            # 为突出消息排序，案例中关闭聚合视图；真实线上应单独做 views 消融。
            "views": False,
            "rrf": True,
            "dedup": True,
            "supersession": False,
            "temporal_intent": False,
            "entity_boost_v2": enhanced,
            "temporal_fallback": enhanced,
        }
    )
    return cfg


def run_once(*, enhanced: bool) -> list[dict]:
    db = RetrieverDB(config=make_config(enhanced=enhanced))
    db.add(
        request_id=f"case-{'vnext' if enhanced else 'baseline'}",
        user_id=USER_ID,
        session_id=SESSION_ID,
        messages=MESSAGES,
    )
    tokens = db._match_expr  # 仅用于保持下行代码阅读简洁
    del tokens
    query_tokens = __import__("aml_retriever.features", fromlist=["query_tokens"]).query_tokens(
        QUERY, db.config.max_query_tokens
    )

    with db.connection() as con:
        raw = db._fts_candidates(con, USER_ID, query_tokens)
        records = db._load_records(con, USER_ID, raw)

    fts_order = [doc_id for doc_id, _doc_type, _rank in raw]
    rank_map = {doc_id: rank for doc_id, _doc_type, rank in raw}
    scored = db._score(records, QUERY, query_tokens, rank_map)

    # 抓取 _score 结束时的特征分（RRF 覆盖 score 前）。
    feature_score = {rec["id"]: round(float(rec["score"]), 4) for rec in scored}

    if enhanced:
        apply_entity_boost_v2(
            scored,
            query=QUERY,
            base_weight=db.config.entity_disambiguation_weight,
            cooccurrence_weight=db.config.entity_cooccurrence_weight,
            config=db.config,
        )
    boosted_score = {rec["id"]: round(float(rec["score"]), 4) for rec in scored}

    ordered = db._fuse_and_order(scored, fts_order)
    output: list[dict] = []
    for rank, rec in enumerate(ordered, start=1):
        output.append(
            {
                "id": rec["id"],
                "rank": rank,
                "content": rec["content"],
                "feature_score": feature_score[rec["id"]],
                "post_entity_score": boosted_score[rec["id"]],
                "rrf_score": round(float(rec["score"]), 6),
                "temporal_source": rec.get("_temporal_source", "legacy"),
                "temporal_confidence": getattr(rec.get("_temporal_confidence"), "name", "N/A"),
                "temporal_granularity": rec.get("_temporal_granularity", "N/A"),
                "flags": rec["flags"],
            }
        )
    return output


def print_table(baseline: list[dict], enhanced: list[dict]) -> None:
    by_id_base = {r["id"]: r for r in baseline}
    by_id_new = {r["id"]: r for r in enhanced}
    ids = [r["id"] for r in enhanced]

    print("\n查询：", QUERY)
    print("\n消息与含义：")
    for index, msg in enumerate(MESSAGES):
        print(f"  m{index}: {msg['content']}")

    print("\n逐候选对比（feature_score 为 RRF 之前的特征总分）：")
    print("| 候选 | 基线排名 | vNext排名 | 基线特征分 | vNext实体后分 | 基线RRF | vNext RRF | 时间来源 / 置信度 |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    for rid in ids:
        b, n = by_id_base.get(rid), by_id_new[rid]
        print(
            f"| {rid[-6:]} | {b['rank'] if b else '-'} | {n['rank']} | "
            f"{b['feature_score'] if b else '-'} | {n['post_entity_score']} | "
            f"{b['rrf_score'] if b else '-'} | {n['rrf_score']} | "
            f"{n['temporal_source']} / {n['temporal_confidence']} |"
        )

    print("\n增强版证据标记：")
    for row in enhanced:
        print(f"  #{row['rank']} {row['id'][-6:]}  {row['flags']}")
        print(f"      {row['content']}")


def main() -> int:
    baseline = run_once(enhanced=False)
    enhanced = run_once(enhanced=True)
    print_table(baseline, enhanced)

    report = {"query": QUERY, "messages": MESSAGES, "baseline": baseline, "vnext": enhanced}
    out = Path(__file__).with_name("vnext_multiturn_comparison.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
