# AML Retriever 跨 seed 聚合评测报告（多随机种子稳定性）

- 评测运行时间：2026-08-15T12:45:15+0800
- 数据集：**纯合成**，suite=`classic`，scale=`medium`，difficulty=`mixed`
- 随机种子：[20260806]（共 1 个）
- top_k：100（官方正式评测口径）
- 运行环境：Python 3.12.7 / SQLite 3.45.3 / FTS5=True

> 本报告的目的**不是**刷分，而是确认指标在合成数据随机种子之间是**稳定的**——即单 seed 上的结论（尤其 temporal×paraphrase 短板、L9 guarded supersession 是否缓解）并非某个 seed 的偶然。所有数字均为本机纯合成、零依赖、不联网。

## 1. 跨 seed 聚合总览（各指标 mean / min / max）

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| `L5_plus_weighted_rrf` | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.6631 (min 0.6631 / max 0.6631) | 0.5312 (min 0.5312 / max 0.5312) | 31.05 (min 31.05 / max 31.05) | 51.02 (min 51.02 / max 51.02) |
| `L9_guarded_supersession` **(v1.1 代码默认)** | 1.0000 (min 1.0000 / max 1.0000) | 1.0000 (min 1.0000 / max 1.0000) | 0.6854 (min 0.6854 / max 0.6854) | 0.5312 (min 0.5312 / max 0.5312) | 32.13 (min 32.13 / max 32.13) | 49.41 (min 49.41 / max 49.41) |

## 2. 短板聚焦：temporal|paraphrase 交叉格

> 系统最弱的一环是「时间限定 + 查询被改写」：纯词法 + 确定性特征抓不到时间锚点（见 docs/EVAL.md 附录 B）。该弱点在 **kind×difficulty 交叉表** 的 `temporal|paraphrase` 单元格才暴露；看 `temporal` 整体会被其他难度稀释。

| 档位 | 角色 | MRR (mean/min/max) | Recall@20 (mean/min/max) | 旧值泄漏@10 |
| --- | --- | --- | --- | --- |
| `L9_guarded_supersession` | v1.1 代码默认 | 0.2569 (min 0.2569 / max 0.2569) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |
| `L5_plus_weighted_rrf` | v1.0 基线 | 0.0784 (min 0.0784 / max 0.0784) | 1.0000 (min 1.0000 / max 1.0000) | 0.0000 (min 0.0000 / max 0.0000) |

> **v1.1 说明**：`L8_supersession_ctrl` 只看话题重合与时间，作为无保护安全对照；`L9_guarded_supersession` 进一步要求显式更新语义，并使用保守 4/1 权重。两者都只做软重排，不安装依赖、不做 confirmed-only 过滤，也不删除旧证据。
> 只有 `L9_guarded_supersession` 在跨 seed 上同时守住召回门并提升 MRR，才可标记为 v1.1 默认；官方数据上的效果仍必须写为 unknown，不能用本合成代理集代替官方验证。

## 3. 逐 seed 稳定性（v1.1 代码默认档位 L9_guarded_supersession）

| seed | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p50 (ms) | p95 (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| 20260806 | 1.0000 | 1.0000 | 0.6854 | 0.5312 | 32.13 | 49.41 |

> 若上表各 seed 的 MRR / Recall@20 接近，则结论在 seed 间稳定；差异较大则说明该结论对合成数据随机性敏感，需谨慎外推到官方数据（属 `unknown`）。

## 4. 指标口径与复现

- `Recall@k` = 前 k 条覆盖到的 gold 消息数 / gold 总数，按查询取平均；结果为原始消息或聚合视图皆可，只要 `source_message_ids` 覆盖 gold 即算命中。
- `MRR` = 首个命中任一 gold 的结果排名倒数，未命中记 0。
- `旧值泄漏@10` = 前 10 条出现「已被覆写旧值」的比例，越低越好；系统不删旧值，只降权与冲突标注，故不为 0 属预期。
- `p50/p95 (ms)` = 单次 search 的端到端耗时分位数（本机、冷缓存、单进程），跨 seed 聚合的是各 seed 自身的分位数再取 mean/min/max，**不是**把所有 seed 的原始延迟合池后取分位。
- 跨 seed 聚合：`mean` 为各 seed 算术平均，`min/max` 为各 seed 极值，`n` 为参与聚合的 seed 数。
- 产物只含指标数字，**不落任何语料原文**；逐 seed 明细见同目录 `ablation_medium_mixed_multiseed_per_seed.csv`。临时索引库跑完即删。

```bash
cd flowgrid-aml-retriever
python3 scripts/run_eval.py --scale medium --difficulty mixed --suite classic --seeds 20260806 --top-k 100
```

> 数据集完全由 `seed` 决定，同一组 seed 必然复现同一组指标（延迟数除外）。
