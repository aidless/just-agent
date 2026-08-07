# AML Retriever 跨 seed 聚合评测报告（多随机种子稳定性）

- 评测运行时间：2026-08-07T07:13:24+0800（报告重渲染于 2026-08-07T07:27:13+0800，指标未重算）
- 数据集：**纯合成**，scale=`small`，difficulty=`paraphrase`
- 随机种子：[20260806, 20260807, 20260808]（共 3 个）
- top_k：100（官方正式评测口径）
- 运行环境：Python 3.13.12 / SQLite 3.50.4 / FTS5=True

> 本报告的目的**不是**刷分，而是确认指标在合成数据随机种子之间是**稳定的**——即单 seed 上的结论（尤其 temporal×paraphrase 短板、L8 supersession 是否缓解）并非某个 seed 的偶然。所有数字均为本机纯合成、零依赖、不联网。

## 1. 跨 seed 聚合总览（各指标 mean / min / max）

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| `L0_lexical_baseline` | 0.7500 (min 0.7500 / max 0.7500) | 1.0000 (min 1.0000 / max 1.0000) | 0.4354 (min 0.4235 / max 0.4555) | 1.0000 (min 1.0000 / max 1.0000) | 1.22 (min 1.05 / max 1.35) | 1.79 (min 1.31 / max 2.33) |
| `L1_plus_views` | 0.7500 (min 0.7500 / max 0.7500) | 1.0000 (min 1.0000 / max 1.0000) | 0.4542 (min 0.4463 / max 0.4629) | 1.0000 (min 1.0000 / max 1.0000) | 2.76 (min 2.67 / max 2.93) | 7.36 (min 3.26 / max 15.45) |
| `L2_plus_exact` | 0.7500 (min 0.7500 / max 0.7500) | 1.0000 (min 1.0000 / max 1.0000) | 0.4542 (min 0.4463 / max 0.4629) | 1.0000 (min 1.0000 / max 1.0000) | 2.77 (min 2.63 / max 2.95) | 4.04 (min 3.34 / max 5.37) |
| `L3_plus_context` | 0.9792 (min 0.9688 / max 0.9922) | 1.0000 (min 1.0000 / max 1.0000) | 0.7699 (min 0.7602 / max 0.7785) | 0.1458 (min 0.0625 / max 0.2188) | 3.21 (min 3.03 / max 3.34) | 3.86 (min 3.77 / max 3.93) |
| `L4_plus_dedup` | 0.9922 (min 0.9922 / max 0.9922) | 1.0000 (min 1.0000 / max 1.0000) | 0.7719 (min 0.7644 / max 0.7787) | 0.1458 (min 0.0625 / max 0.2188) | 3.21 (min 3.14 / max 3.29) | 3.96 (min 3.79 / max 4.11) |
| `L5_plus_weighted_rrf` **(线上默认)** | 0.9922 (min 0.9922 / max 0.9922) | 1.0000 (min 1.0000 / max 1.0000) | 0.6710 (min 0.6571 / max 0.6787) | 0.1458 (min 0.0625 / max 0.2188) | 3.25 (min 3.24 / max 3.26) | 3.97 (min 3.90 / max 4.05) |
| `L6_temporal_intent_ctrl` | 0.9870 (min 0.9844 / max 0.9922) | 1.0000 (min 1.0000 / max 1.0000) | 0.6605 (min 0.6554 / max 0.6653) | 0.1458 (min 0.0625 / max 0.2188) | 3.25 (min 3.16 / max 3.29) | 4.02 (min 3.96 / max 4.11) |
| `L7_plus_vector` | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 | 跳过 |
| `L8_supersession_ctrl` | 0.9922 (min 0.9922 / max 0.9922) | 1.0000 (min 1.0000 / max 1.0000) | 0.6399 (min 0.6372 / max 0.6414) | 0.1250 (min 0.0312 / max 0.1875) | 3.39 (min 3.33 / max 3.45) | 4.59 (min 4.41 / max 4.95) |

## 2. 短板聚焦：temporal|paraphrase 交叉格

> 系统最弱的一环是「时间限定 + 查询被改写」：纯词法 + 确定性特征抓不到时间锚点（见 docs/EVAL.md 附录 B）。该弱点在 **kind×difficulty 交叉表** 的 `temporal|paraphrase` 单元格才暴露；看 `temporal` 整体会被其他难度稀释。

| 档位 | 角色 | MRR (mean/min/max) | Recall@20 (mean/min/max) | 旧值泄漏@10 |
| --- | --- | --- | --- | --- |
| `L5_plus_weighted_rrf` | 线上默认 | 0.5903 (min 0.5625 / max 0.6146) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |
| `L6_temporal_intent_ctrl` | 对照组 | 0.5903 (min 0.5625 / max 0.6146) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |
| `L8_supersession_ctrl` | 候选(可消融) | 0.6666 (min 0.6562 / max 0.6875) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |

> **可消融安全优化说明**：`L8_supersession_ctrl` 是成对覆写检测（检测同话题的旧→新消息，局部抬高新值、轻降旧值），**不猜测官方语义、不安装依赖、不做 confirmed-only 过滤**。
> 它针对的正是 `temporal`/`knowledge_update` 类的覆写盲区。上表对比 L5（线上默认）与 L8，若 L8 在该交叉格上 MRR 不降（或上升）且泄漏不升，则它是对短板的**安全可消融增强**，是否纳入默认交由人工决定；若 L8 反而掉点，则保持 `DEFAULT_FLAGS['supersession'] = False`。

## 3. 逐 seed 稳定性（线上默认档位 L5_plus_weighted_rrf）

| seed | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| 20260806 | 0.9922 | 1.0000 | 0.6787 | 0.2188 | 3.24 | 3.90 |
| 20260807 | 0.9922 | 1.0000 | 0.6771 | 0.1562 | 3.25 | 3.97 |
| 20260808 | 0.9922 | 1.0000 | 0.6571 | 0.0625 | 3.26 | 4.05 |

> 若上表各 seed 的 MRR / Recall@20 接近，则结论在 seed 间稳定；差异较大则说明该结论对合成数据随机性敏感，需谨慎外推到官方数据（属 `unknown`）。

## 4. 指标口径与复现

- `Recall@k` = 前 k 条覆盖到的 gold 消息数 / gold 总数，按查询取平均；结果为原始消息或聚合视图皆可，只要 `source_message_ids` 覆盖 gold 即算命中。
- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。
- `旧值泄漏@10` = 前 10 条出现「已被覆写旧值」的比例，越低越好；系统不删旧值，只降权与冲突标注，故不为 0 属预期。
- `p50/p95 (ms)` = 单次 search 的端到端耗时分位数（本机、冷缓存、单进程），跨 seed 聚合的是各 seed 自身的分位数再取 mean/min/max，**不是**把所有 seed 的原始延迟合池后取分位。
- 跨 seed 聚合：`mean` 为各 seed 算术平均，`min/max` 为各 seed 极值，`n` 为参与聚合的 seed 数。
- 产物只含指标数字，**不落任何语料原文**；逐 seed 明细见同目录 `ablation_small_paraphrase_multiseed_per_seed.csv`。临时索引库跑完即删。

```bash
cd /Users/hu/WorkBuddy/2026-08-06-16-02-06/aml-retriever
python3 scripts/run_eval.py --scale small --difficulty paraphrase --seeds 20260806,20260807,20260808 --top-k 100
```

> 数据集完全由 `seed` 决定，同一组 seed 必然复现同一组指标（延迟数除外）。
