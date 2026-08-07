# 提交就绪清单（SUBMISSION READINESS）

> 本文件是面向「报名 / 提交」的**静态就绪清单**与**人工确认门**集合。
> 它**不替代**官方页面，也**不执行任何外部动作**（见 §5）。
> 仓库内所有指标均来自**自造合成数据**，用于档位间的相对比较，不是官方榜单成绩。
> 相关细节以 [`README.md`](../README.md)、[`docs/API_CONTRACT.md`](API_CONTRACT.md)、
> [`docs/EVAL.md`](EVAL.md)、[`docs/DATA_LIFECYCLE.md`](DATA_LIFECYCLE.md) 为准。

---

## 0. 一句话定位

AML Retriever 只做两件事：**Add**（写入记忆）与 **Search**（返回原始证据）。
**不生成答案、不做评测**——那是平台的职责。每条返回都是原始消息证据或可回指原文的聚合视图。
纯 Python 标准库 + SQLite/FTS5，**零第三方依赖、不联网**；全部增强都带本地合成评测证据，没有证据的一律默认关闭。

---

## 1. 版本、根目录、启动与 API 入口

| 项 | 值 |
| --- | --- |
| 版本 | `1.0.0`（`aml_retriever/__init__.py::__version__`；HTTP `Server` 头 `aml-retriever/1.0`） |
| 仓库根 | `aml-retriever/`（本目录即服务根；部署时把整个目录作为服务根） |
| 公开候选仓库 | `https://github.com/dlxeva/flowgrid-aml-retriever` |
| 启动（脚本） | `./scripts/serve.sh`（默认 `127.0.0.1:8080`） |
| 启动（手动） | `python3 -m aml_retriever.cli serve --db ./aml.db --port 8080` |
| 本地总验收 | `./scripts/run_tests.sh`（环境探测 + 单测 + CLI 自检 + HTTP 契约 smoke） |
| 契约文档 | [`docs/API_CONTRACT.md`](API_CONTRACT.md)（字段与错误码严格对齐官方 api-guide，抓取核对于 2026-08-06） |

**API 入口（官方固定格式，不随 URL 路径变化）**

| 方法 / 路径 | 用途 | 必填字段 |
| --- | --- | --- |
| `POST /add` | 写入记忆（同步落库，返回前已可检索） | `request_id`、`user_id`、`session_id`、`messages[].{role,content}` |
| `POST /search` | 检索原始证据 | `query`、`user_id`、`top_k`（真整数，无服务端默认值） |
| `GET /health` | 存活探针，2xx 即正常，无需鉴权 | — |
| `GET /stats` | 行数统计，不含任何记忆正文 | — |
| `POST /admin/delete_user` | 按 `user_id` 删除全部数据 | `user_id` |

错误体统一为 `{"detail":{"reason":"..."}}`，字段校验失败返回 **422**。
完整判定矩阵、每条取舍理由、Add 的 first-write-wins 幂等语义见 [`docs/API_CONTRACT.md` §5.1](API_CONTRACT.md)。

---

## 2. 本地已通过项（提交前自检）

| 检查 | 结果 | 命令 / 依据 |
| --- | --- | --- |
| 单元与契约测试 | **142 项全绿** | `python3 -m unittest discover -s tests` |
| HTTP 官方契约 smoke | **31 项全绿** | `python3 scripts/smoke_api.py` |
| CLI 端到端自检 | 通过 | `python3 -m aml_retriever.cli selfcheck` |
| 合成评测可复现 | 通过 | `python3 scripts/run_eval.py --scale medium --difficulty mixed --seed 20260806 --top-k 100`（同 seed 同指标） |
| 跨 seed 稳定性 | 已补充 | `python3 scripts/run_eval.py --scale medium --difficulty <plain|paraphrase|mixed> --seeds 20260806,20260807,20260808 --top-k 100`；结论见 [`docs/EVAL.md` 附录 C](EVAL.md) |

> 可用 `python3 scripts/check_submission_materials.py` 一键静态核对本文件列出的关键产物是否齐备。

---

## 3. 证据状态表

状态分级定义：

| 状态 | 含义 |
| --- | --- |
| **confirmed** | 有测试 / 文档 / 代码双重覆盖，确定性成立 |
| **observed** | 本机跑出、可复现，但仅限于本合成集，不外推官方 |
| **inferred** | 未经验证的假设，仅作相对比较用 |
| **unknown** | 本环境无法验证（无依赖 / 无授权数据 / 规则禁止） |
| **unverified** | 静态编写但未实际执行验证（如 Docker 构建） |
| **blocked** | 需主办方口径或人工确认，本工程不自动执行 |

代表性事项（完整逐条见 [`README.md` §8](../README.md)）：

| 事项 | 状态 | 依据 |
| --- | --- | --- |
| Add/Search 字段、错误码、同步语义符合官方文档 | **confirmed** | `docs/API_CONTRACT.md` + `tests/test_api_contract.py` + `scripts/smoke_api.py` |
| 写后立即可搜、幂等、`user_id` 隔离、`top_k` 上限 | **confirmed** | 单测 + HTTP smoke 双重覆盖 |
| 三类视图确定性、可配置、可回指 `message_id` | **confirmed** | `tests/test_views.py`（含增量 vs 全量一致性） |
| 删除彻底性、日志不含正文 | **confirmed** | `tests/test_privacy.py` 逐表扫描 + 哨兵断言 |
| 合成集上的 Recall/MRR/延迟与消融排序 | **observed** | `eval_out/` 产物，同 seed 可复现 |
| 64/32 workers 并发下的一致性 | **observed** | 本机线程池模拟，非评测环境分布式压力 |
| `rrf_weight_lexical=0.1` 是 Pareto 安全点 | **observed（有规模边界）** | `docs/EVAL.md` 附录 A/C；**仅 medium 及以上成立**，small×paraphrase 上反降 0.10 |
| `temporal_intent` 应关闭 | **observed** | `docs/EVAL.md` 附录 B（仅限本合成集） |
| 跨 seed 稳定性（3 seed × 3 difficulty） | **observed** | `docs/EVAL.md` 附录 C（仅限本合成集） |
| `supersession` 对 temporal×paraphrase 有增益 | **observed** | 附录 D：目标格 MRR +0.11，跨 3 seed 区间不重叠 |
| `supersession` 不应默认启用 | **observed** | 附录 D：总 MRR 在 2 难度 × 3 seed 上一致 −0.03~−0.04，未过启用门槛 |
| 合成集难度分布与官方数据可比 | **inferred** | 未经验证的假设 |
| 官方数据规模落在哪一档（small/medium/large） | **unknown** | 直接影响上面 RRF 结论是否适用 |
| 向量检索能否带来增益 | **unknown** | 本机无可用依赖，规则禁止为此安装 |
| 在官方数据 / 榜单上的真实表现 | **unknown** | 从未接触官方数据，无任何官方成绩 |
| Dockerfile 能否构建成功 | **confirmed（本机）** | Docker Desktop 4.85.0 / Engine 29.6.2，构建、FTS5、142 tests、31/31 smoke、容器 health/Add/Search 均通过 |
| 「Add/Search 须用 gpt-4o-mini」门控 | **blocked** | 官方当前 Full 评测清单明确要求；本实现没有 LLM 路径，不能勾选 |
| 公开 GitHub 仓库 | **confirmed（候选仓库）** | `https://github.com/dlxeva/flowgrid-aml-retriever`；公开版本仍需用户确认作者、许可证和来源披露 |
| 报名 / Eval Key / 部署 / 正式提交 | **blocked** | 保留人工确认门，本工程不自动执行 |

---

## 4. 官方材料清单（提交时建议携带）

**必带（本仓库均已提供）**

- `README.md` — 快速开始、官方契约、架构、证据状态表、未做的事
- `docs/API_CONTRACT.md` — 字段与错误码核对记录
- `docs/EVAL.md` — 评测方法与全部证据（含 §5/§6 跨 seed 附录）
- `docs/DATA_LIFECYCLE.md` — 数据、隐私与删除说明
- `docs/SUBMISSION_READINESS.md` — 本文件
- `Dockerfile` + `config.example.json` — 部署与配置样例
- `aml_retriever/`、`scripts/`、`tests/` — 完整源码与测试

**可选（视官方要求）**

- `eval_out/` — 聚合指标与报告（**只含指标，不含任何语料正文**，可公开）

**不建议携带**

- 任何真实用户数据或赛事数据——**本仓库当前不含此类数据**，请勿在提交前混入。

---

## 5. 未执行的外部动作（保留人工确认门）

以下动作**本工程默认不执行**，均保留为人工确认门。理由不是"做不到"，而是"不应由自动化越权代行"：

| 动作 | 状态 | 说明 |
| --- | --- | --- |
| Git 提交 / 推送 / 建公开仓库 | **已执行（候选仓库）** | 目标为 `dlxeva/flowgrid-aml-retriever`；后续版本变更仍需用户确认 |
| 报名赛事 | **不执行** | 需人工在官方页面操作 |
| 申请 / 填写 Eval Key、API Key | **不执行** | 凭据不应写入代码或日志 |
| 部署到任何环境 | **不执行** | 仅本地可运行，部署由人工决定 |
| 跑正式 / 官方评测 | **不执行** | 不接触官方数据，不消耗官方配额 |
| 发邮件 / 联系主办方 | **不执行** | 通信由人工负责 |
| 产生任何费用 | **不执行** | 零依赖、不联网、不调 LLM |
| 联网下载模型 / 依赖 | **不执行** | 规则禁止为向量检索等安装依赖；`vector_backend_available()` 只探测不安装 |
| 接触真实用户数据或赛事数据 | **不执行** | 全程纯合成 |

---

## 6. gpt-4o-mini 门控（BLOCKED）

官方当前[参赛说明](https://agentmemories.ai/rules)的 Full 评测清单明确写出：提交的记忆系统在 Add 和 Search 阶段都必须使用 `gpt-4o-mini`，平台会复现提交版本；复现分数出现实质差异时，榜单结果可能失效。

当前实现无法勾选该项：

- 本实现**全程不调用任何 LLM**：Add 是确定性写入，Search 是确定性多视图混合检索 + 重排，
  不生成、不改写、不调用大模型。
- 这意味着：无 token 费用、结果可逐项复现、不依赖外部推理服务可用性。
- 该门控当前属于**已确认的提交阻塞**，不再按“规则是否存在”处理。
- 可行路径只有两类：主办方书面确认存在适用于本项目的例外，或用户授权后补充真实的 `gpt-4o-mini` Add/Search 适配层，并承担 API Key、费用、延迟、复现和数据合规责任。
- 在得到授权和真实凭据前，不添加占位调用、不伪造调用记录、不勾选 Full 评测合规项。

> 本仓库没有任何代码路径会静默"假装"调用了 LLM。

---

## 7. 数据隐私与删除

- **当前仓库不含任何赛事数据或真实用户数据**，全部为合成或手写示例。
- **日志不含正文**：访问日志只记方法、路径、状态码、耗时；错误响应不外泄内部细节；`api_key` 配置导出时脱敏（有回归测试哨兵断言）。
- **删除**彻底清除原始消息、视图、FTS 索引、幂等记录与会话游标：

  ```bash
  python3 -m aml_retriever.cli delete-user --db ./aml.db --user u1   # 删单个 user
  python3 -m aml_retriever.cli purge       --db ./aml.db --yes       # 清空整库
  rm -f ./aml.db ./aml.db-wal ./aml.db-shm                           # 连 WAL 一并销毁
  ```

- 完整说明与残量断言见 [`docs/DATA_LIFECYCLE.md`](DATA_LIFECYCLE.md)。

---

## 8. 原创性与来源披露模板（切勿虚构）

> ⚠️ **硬性约束**：以下模板**只用于如实填写**。
> **不得虚构作者、论文、仓库、基准分数或第三方成果**。凡非本仓库原创的内容，必须标明真实来源或明确标注为「未知来源」。

```markdown
# 原创性与来源披露（提交时由人工填写）

## 本仓库原创部分
- （例如）确定性多视图混合检索 + 加权 RRF 的引擎设计（aml_retriever/retriever.py、views.py、features.py、config.py）
- （例如）官方 Add/Search 契约的校验与幂等实现（aml_retriever/api.py、server.py）
- （例如）纯合成评测数据集与消融执行器（aml_retriever/evaluation/）

## 来源于公开文档 / 官方材料的部分
- API 契约字段与错误码：对齐官方 api-guide（抓取核对于 2026-08-06，见 docs/API_CONTRACT.md）
- 评测结构参考：公开评测仓库的目录与字段形状（如有，填写真实仓库地址）
- 第三方算法 / 模型：无（本实现未使用任何第三方模型或预训练权重）

## 训练数据 / 权重来源
- 训练数据：无（不训练、不微调）
- 预训练权重：无
- 外部语料：无（评测数据为本地确定性合成，词表全虚构）

## 已知限制与未验证项
- （复制 docs/EVAL.md / README §9 的已知限制；gpt-4o-mini 门控状态见本文档 §6）

## 许可与署名
- 许可证：（人工填写，如未定则标注「待定」）
- 作者 / 团队：（人工填写真实署名）
```

---

## 9. 提交前最后核对（checklist）

- [ ] 人工复核官方页面**最新**口径（本仓库契约抓取于 **2026-08-06**，可能已过时）
- [ ] 运行 `python3 scripts/check_submission_materials.py` 全绿
- [ ] 确认 **§6 gpt-4o-mini 门控**处置方案（主办方确认 / 补充可选 LLM 层 / 明确标注差异）
- [ ] 确认仓库**不含**任何真实用户数据或赛事数据
- [ ] 确认 **§5** 列出的报名、Key、部署和正式评测仍未执行
- [ ] 确认 `README.md` 与本文档版本号一致（当前 `1.0.0`）
- [ ] 填写 **§8** 原创性与来源披露模板（不得留空虚构）

---

## 10. 相关文档

- [`README.md`](../README.md) — 总览、快速开始、官方契约、架构、证据状态表、未做的事
- [`docs/API_CONTRACT.md`](API_CONTRACT.md) — 字段与错误码核对（含 §5.1 校验矩阵）
- [`docs/EVAL.md`](EVAL.md) — 评测方法与全部证据（附录 A/B/C）
- [`docs/DATA_LIFECYCLE.md`](DATA_LIFECYCLE.md) — 数据、隐私与删除
- [`scripts/check_submission_materials.py`](../scripts/check_submission_materials.py) — 本清单的静态核对脚本
