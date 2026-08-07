# AML Retriever 离线消融评测报告

- 生成时间：2026-08-07T07:00:30+0800
- 数据集：**纯合成**，scale=`medium`，difficulty=`mixed`，seed=`20260806`
- 规模：48 users / 144 sessions / 5184 messages / 240 queries（计分 192 条）
- 查询构成：{'absent': 48, 'knowledge_update': 48, 'multi_session': 48, 'single_hop': 48, 'temporal': 48}；难度分布：{'paraphrase': 120, 'plain': 120}
- top_k：100（官方正式评测口径）
- 运行环境：Python 3.13.12 / SQLite 3.50.4 / FTS5=True
- 向量后端：不可用（no local vector dependency (numpy/sentence_transformers/faiss) importable）

## 1. 消融梯度总览

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50(ms) | p95(ms) | 建库(s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `L0_lexical_baseline` | 0.8750 | 1.0000 | 0.5686 | 1.0000 | 3.15 | 5.51 | 1.96 |
| `L1_plus_views` | 0.8724 | 1.0000 | 0.6006 | 1.0000 | 9.16 | 14.38 | 9.55 |
| `L2_plus_exact` | 0.8724 | 1.0000 | 0.6006 | 1.0000 | 8.66 | 13.49 | 9.47 |
| `L3_plus_context` | 0.9948 | 1.0000 | 0.6553 | 0.4375 | 9.32 | 14.20 | 9.72 |
| `L4_plus_dedup` | 1.0000 | 1.0000 | 0.6576 | 0.5104 | 9.89 | 14.89 | 9.59 |
| `L5_plus_weighted_rrf` **(线上默认)** | 1.0000 | 1.0000 | 0.6631 | 0.5312 | 9.44 | 15.31 | 10.17 |
| `L6_temporal_intent_ctrl` | 0.9922 | 1.0000 | 0.6561 | 0.3958 | 9.46 | 14.55 | 9.42 |
| `L7_plus_vector` | 跳过 | 跳过 | 跳过 | 跳过 | — | — | — |

> `L5_plus_weighted_rrf` 即 `DEFAULT_FLAGS` 的实际配置（含 rrf_weight_lexical=0.1）。`L6_temporal_intent_ctrl` 是**对照组不是推荐档**：它量化「在相对新近度之上再加一层时间意图放大」的代价，实测整体 MRR 与 Recall@20 双双下降，结论已固化为 `DEFAULT_FLAGS['temporal_intent'] = False`（见 docs/EVAL.md 附录 B）。

## 2. 难度分档对比（各档位 MRR）

| 档位 | MRR(paraphrase) | MRR(plain) |
| --- | --- | --- |
| `L0_lexical_baseline` | 0.4124 | 0.7248 |
| `L1_plus_views` | 0.4764 | 0.7248 |
| `L2_plus_exact` | 0.4764 | 0.7248 |
| `L3_plus_context` | 0.5607 | 0.7499 |
| `L4_plus_dedup` | 0.5648 | 0.7504 |
| `L5_plus_weighted_rrf` | 0.5663 | 0.7599 |
| `L6_temporal_intent_ctrl` | 0.5514 | 0.7609 |

## 3. 分查询类型明细（线上默认档位）

档位：`L5_plus_weighted_rrf`（**线上默认 / production**）

| 查询类型 | 条数 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 |
| --- | --- | --- | --- | --- | --- |
| absent | 48 | — | — | — | — |  <!-- gold 为空，不计入 Recall/MRR -->
| knowledge_update | 48 | 1.0000 | 1.0000 | 0.9163 | 0.5625 |
| multi_session | 48 | 1.0000 | 1.0000 | 0.6281 | — |
| single_hop | 48 | 1.0000 | 1.0000 | 0.5687 | — |
| temporal | 48 | 1.0000 | 1.0000 | 0.5392 | 0.5000 |

> **档位角色**：`L5_plus_weighted_rrf` = 线上默认（production，`DEFAULT_FLAGS` 实配）；`L6_temporal_intent_ctrl` = 对照组（control），仅用于量化时间意图放大的代价，默认**关闭**，不参与线上返回。
> 对照组同口径整体指标：MRR=0.6561、Recall@20=0.9922（对比线上默认 MRR=0.6631、Recall@20=1.0000）。

## 4. 指标口径

- 结果可能是原始消息或聚合视图；只要某条结果的 `source_message_ids` 覆盖 gold 消息即算召回。
- `Recall@k` = 前 k 条结果覆盖到的 gold 消息数 / gold 总数，按查询取平均。
- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。
- `旧值泄漏@10` = 前 10 条中出现「已被覆写旧值」的比例，**越低越好**；系统不做删除，只做降权与冲突标注，故不为 0 属预期。
- `absent` 类查询 gold 为空，不计入 Recall/MRR，仅用于观察系统是否硬凑证据。
- 延迟为单进程串行 Search 的端到端墙钟时间（不含 HTTP 开销）。

## 5. 复现命令

```bash
cd /Users/hu/WorkBuddy/2026-08-06-16-02-06/aml-retriever
python3 scripts/run_eval.py --scale medium --difficulty mixed --seed 20260806 --top-k 100
```

> 数据集完全由 `seed` 决定，同一 seed 必然复现同一份数据与同一组指标（延迟数除外）。
