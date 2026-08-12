"""AML Retriever 离线评测套件（纯合成数据、零依赖）。

该包只用于本地可复现的离线评测，不接触任何真实数据、不联网。
"""
from .dataset import Dataset, Query, make_dataset, SCALES, SUITES
from .metrics import recall_at_k, reciprocal_rank, percentile, summarize_latency
from .harness import (ABLATION_LADDER, CONTROL_STAGE, MAINLINE_STAGES,
                      PRODUCTION_STAGE, StageResult, run_stage, run_ladder)

__all__ = [
    "Dataset",
    "Query",
    "make_dataset",
    "SCALES",
    "SUITES",
    "recall_at_k",
    "reciprocal_rank",
    "percentile",
    "summarize_latency",
    "ABLATION_LADDER",
    "CONTROL_STAGE",
    "MAINLINE_STAGES",
    "PRODUCTION_STAGE",
    "StageResult",
    "run_stage",
    "run_ladder",
]
