# AML Retriever 跨 seed 聚合评测报告（多随机种子稳定性）

- 评测运行时间：2026-08-07T07:18:21+0800（报告重渲染于 2026-08-07T07:30:22+0800，指标未重算）
- 数据集：**纯合成**，scale=`medium`，difficulty=`plain`
- 随机种子：[20260806, 20260807, 20260808]（共 3 个）
- top_k：100（官方正式评测口径）
- 运行环境：Python 3.13.12 / SQLite 3.50.4 / FTS5=True

> 本报告的目的**不是**刷分，而是确认指标在合成数据随机种子之间是**稳定的**——即单 seed 上的结论（尤其 temporal×paraphrase 短板、L8 supersession 是否缓解）并非某个 seed 的偶然。所有数字均为本机纯合成、零依赖、不联网。

## 1. 跨 seed 聚合总览（各指标 mean / min / max）

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| `L0_lexical_baseline` | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.8231 (min 0.7193 / max 0.8750) | 1.0000 (min 1.0000 / max 1.0000) | 3.01 (min 2.64 / max 3.36) | 5.59 (min 4.59 / max 7.18) |
| `L1_plus_views` | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.8231 (min 0.7193 / max 0.8750) | 1.0000 (min 1.0000 / max 1.0000) | 9.13 (min 8.42 / max 9.56) | 16.50 (min 14.14 / max 19.07) |
| `L2_plus_exact` | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.8231 (min 0.7193 / max 0.8750) | 1.0000 (min 1.0000 / max 1.0000) | 9.87 (min 8.39 / max 11.38) | 18.40 (min 13.78 / max 22.59) |
| `L3_plus_context` | 0.9939 (min 0.9870 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.7647 (min 0.7547 / max 0.7782) | 0.7431 (min 0.7292 / max 0.7500) | 10.01 (min 8.88 / max 11.04) | 17.49 (min 14.56 / max 19.95) |
| `L4_plus_dedup` | 0.9957 (min 0.9870 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.7650 (min 0.7553 / max 0.7783) | 0.7847 (min 0.7604 / max 0.8229) | 9.46 (min 8.78 / max 10.46) | 17.26 (min 14.86 / max 21.14) |
| `L5_plus_weighted_rrf` **(线上默认)** | 0.9974 (min 0.9922 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.7776 (min 0.7613 / max 0.7951) | 0.7986 (min 0.7708 / max 0.8333) | 9.63 (min 8.73 / max 11.16) | 16.84 (min 14.70 / max 20.75) |
| `L6_temporal_intent_ctrl` | 0.9965 (min 0.9896 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.7800 (min 0.7623 / max 0.7999) | 0.6805 (min 0.6354 / max 0.7083) | 9.63 (min 8.96 / max 10.86) | 17.52 (min 15.24 / max 21.56) |
| `L7_plus_vector` | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 |
| `L8_supersession_ctrl` | 0.9965 (min 0.9896 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.7496 (min 0.7364 / max 0.7566) | 0.7778 (min 0.7500 / max 0.8125) | 9.58 (min 9.00 / max 10.46) | 15.93 (min 14.95 / max 17.86) |

## 2. 短板聚焦：temporal|plain 交叉格

> **注意**：本次 difficulty=`plain`，数据集中不存在 `paraphrase` 难度，因此下表展示的是回退单元格 `temporal|plain`，**不能**用来判断 temporal×paraphrase 短板；要看该短板请跑 `--difficulty paraphrase` 或 `--difficulty mixed`。

| 档位 | 角色 | MRR (mean/min/max) | Recall@20 (mean/min/max) | 旧值泄漏@10 |
| --- | --- | --- | --- | --- |
| `L5_plus_weighted_rrf` | 线上默认 | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) |
| `L6_temporal_intent_ctrl` | 对照组 | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) |
| `L8_supersession_ctrl` | 候选(可消融) | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) |

> **可消融安全优化说明**：`L8_supersession_ctrl` 是成对覆写检测（检测同话题的旧→新消息，局部抬高新值、轻降旧值），**不猜测官方语义、不安装依赖、不做 confirmed-only 过滤**。
> 它针对的正是 `temporal`/`knowledge_update` 类的覆写盲区。上表对比 L5（线上默认）与 L8，若 L8 在该交叉格上 MRR 不降（或上升）且泄漏不升，则它是对短板的**安全可消融增强**，是否纳入默认交由人工决定；若 L8 反而掉点，则保持 `DEFAULT_FLAGS['supersession'] = False`。

## 3. 逐 seed 稳定性（线上默认档位 L5_plus_weighted_rrf）

| seed | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| 20260806 | 1.0000 | 1.0000 | 0.7613 | 0.7917 | 11.16 | 20.75 |
| 20260807 | 0.9922 | 1.0000 | 0.7765 | 0.8333 | 9.01 | 15.07 |
| 20260808 | 1.0000 | 1.0000 | 0.7951 | 0.7708 | 8.73 | 14.70 |

> 若上表各 seed 的 MRR / Recall@20 接近，则结论在 seed 间稳定；差异较大则说明该结论对合成数据随机性敏感，需谨慎外推到官方数据（属 `unknown`）。

## 4. 指标口径与复现

- `Recall@k` = 前 k 条覆盖到的 gold 消息数 / gold 总数，按查询取平均；结果为原始消息或聚合视图皆可，只要 `source_message_ids` 覆盖 gold 即算命中。
- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。
- `旧值泄漏@10` = 前 10 条出现「已被覆写旧值」的比例，越低越好；系统不删旧值，只降权与冲突标注，故不为 0 属预期。
- `p50/p95 (ms)` = 单次 search 的端到端耗时分位数（本机、冷缓存、单进程），跨 seed 聚合的是各 seed 自身的分位数再取 mean/min/max，**不是**把所有 seed 的原始延迟合池后取分位。
- 跨 seed 聚合：`mean` 为各 seed 算术平均，`min/max` 为各 seed 极值，`n` 为参与聚合的 seed 数。
- 产物只含指标数字，**不落任何语料原文**；逐 seed 明细见同目录 `ablation_medium_plain_multiseed_per_seed.csv`。临时索引库跑完即删。

```bash
cd /Users/hu/WorkBuddy/2026-08-06-16-02-06/aml-retriever
python3 scripts/run_eval.py --scale medium --difficulty plain --seeds 20260806,20260807,20260808 --top-k 100
```

> 数据集完全由 `seed` 决定，同一组 seed 必然复现同一组指标（延迟数除外）。
