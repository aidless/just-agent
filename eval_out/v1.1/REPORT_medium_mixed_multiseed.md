# AML Retriever 跨 seed 聚合评测报告（多随机种子稳定性）

- 评测运行时间：2026-08-13T01:54:08+0800（报告重渲染于 2026-08-13T02:05:06+0800，指标未重算）
- 数据集：**纯合成**，suite=`classic`，scale=`medium`，difficulty=`mixed`
- 随机种子：[20260806, 20260807, 20260808]（共 3 个）
- top_k：100（官方正式评测口径）
- 运行环境：Python 3.9.6 / SQLite 3.51.0 / FTS5=True

> 本报告的目的**不是**刷分，而是确认指标在合成数据随机种子之间是**稳定的**——即单 seed 上的结论（尤其 temporal×paraphrase 短板、L9 guarded supersession 是否缓解）并非某个 seed 的偶然。所有数字均为本机纯合成、零依赖、不联网。

## 1. 跨 seed 聚合总览（各指标 mean / min / max）

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| `L5_plus_weighted_rrf` | 0.9948 (min 0.9870 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.6728 (min 0.6631 / max 0.6791) | 0.5035 (min 0.4792 / max 0.5312) | 12.80 (min 12.60 / max 12.95) | 18.19 (min 17.99 / max 18.48) |
| `L8_supersession_ctrl` | 0.9939 (min 0.9844 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.6780 (min 0.6680 / max 0.6834) | 0.4965 (min 0.4792 / max 0.5208) | 13.59 (min 13.48 / max 13.67) | 18.42 (min 18.16 / max 18.74) |
| `L9_guarded_supersession` **(v1.1 代码默认)** | 0.9948 (min 0.9870 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.6948 (min 0.6854 / max 0.7004) | 0.5035 (min 0.4792 / max 0.5312) | 13.57 (min 13.20 / max 13.82) | 18.95 (min 18.14 / max 20.34) |

## 2. 短板聚焦：temporal|paraphrase 交叉格

> 系统最弱的一环是「时间限定 + 查询被改写」：纯词法 + 确定性特征抓不到时间锚点（见 docs/EVAL.md 附录 B）。该弱点在 **kind×difficulty 交叉表** 的 `temporal|paraphrase` 单元格才暴露；看 `temporal` 整体会被其他难度稀释。

| 档位 | 角色 | MRR (mean/min/max) | Recall@20 (mean/min/max) | 旧值泄漏@10 |
| --- | --- | --- | --- | --- |
| `L9_guarded_supersession` | v1.1 代码默认 | 0.2685 (min 0.2569 / max 0.2743) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |
| `L5_plus_weighted_rrf` | v1.0 基线 | 0.0929 (min 0.0784 / max 0.1042) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |
| `L8_supersession_ctrl` | 无保护安全对照 | 0.1782 (min 0.1739 / max 0.1810) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |

> **v1.1 说明**：`L8_supersession_ctrl` 只看话题重合与时间，作为无保护安全对照；`L9_guarded_supersession` 进一步要求显式更新语义，并使用保守 4/1 权重。两者都只做软重排，不安装依赖、不做 confirmed-only 过滤，也不删除旧证据。
> 只有 `L9_guarded_supersession` 在跨 seed 上同时守住召回门并提升 MRR，才可标记为 v1.1 默认；官方数据上的效果仍必须写为 unknown，不能用本合成代理集代替官方验证。

## 3. 逐 seed 稳定性（v1.1 代码默认档位 L9_guarded_supersession）

| seed | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| 20260806 | 1.0000 | 1.0000 | 0.6854 | 0.5312 | 13.69 | 18.36 |
| 20260807 | 0.9870 | 1.0000 | 0.6985 | 0.4792 | 13.82 | 20.34 |
| 20260808 | 0.9974 | 1.0000 | 0.7004 | 0.5000 | 13.20 | 18.14 |

> 若上表各 seed 的 MRR / Recall@20 接近，则结论在 seed 间稳定；差异较大则说明该结论对合成数据随机性敏感，需谨慎外推到官方数据（属 `unknown`）。

## 4. 指标口径与复现

- `Recall@k` = 前 k 条覆盖到的 gold 消息数 / gold 总数，按查询取平均；结果为原始消息或聚合视图皆可，只要 `source_message_ids` 覆盖 gold 即算命中。
- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。
- `旧值泄漏@10` = 前 10 条出现「已被覆写旧值」的比例，越低越好；系统不删旧值，只降权与冲突标注，故不为 0 属预期。
- `p50/p95 (ms)` = 单次 search 的端到端耗时分位数（本机、冷缓存、单进程），跨 seed 聚合的是各 seed 自身的分位数再取 mean/min/max，**不是**把所有 seed 的原始延迟合池后取分位。
- 跨 seed 聚合：`mean` 为各 seed 算术平均，`min/max` 为各 seed 极值，`n` 为参与聚合的 seed 数。
- 产物只含指标数字，**不落任何语料原文**；逐 seed 明细见同目录 `ablation_medium_mixed_multiseed_per_seed.csv`。临时索引库跑完即删。

```bash
cd /Users/hu/Projects/Oh-My-Project/flowgrid-aml-retriever-v11
python3 scripts/run_eval.py --scale medium --difficulty mixed --suite classic --seeds 20260806,20260807,20260808 --top-k 100
```

> 数据集完全由 `seed` 决定，同一组 seed 必然复现同一组指标（延迟数除外）。
