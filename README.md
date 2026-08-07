# AML Retriever

面向 Agent Memory Challenge「文本记忆榜 / 开源方法榜」的**本地可运行**记忆检索实现。

系统只做两件事：**Add**（写入记忆）与 **Search**（返回原始证据）。
**不生成答案、不做评测** —— 那是平台的职责。返回的每一条都是原始消息证据或可回指原文的聚合视图。

- 纯 Python **标准库** + SQLite/FTS5，零第三方依赖，不联网
- 一次 `Add` 同步落库并**立即可检索**；`request_id` 幂等；`user_id` 强隔离
- 检索为**完全确定性**的多视图混合证据路线，可逐项消融
- 全部增强都有本地合成评测证据；没有证据的一律默认关闭

> ⚠️ **重要边界**：本仓库的一切指标均来自**自造合成数据**，用于档位之间的**相对比较**。
> 它**不是**官方榜单成绩，也未在任何官方数据上验证过。详见 [证据状态表](#8-证据状态表)。

当前版本是学术代码路线的候选提交材料：公开仓库、Docker 启动、API 封装和复现说明均随仓库提供。
官方 Full 评测清单中的 `gpt-4o-mini` Add/Search 门控仍处于 `blocked`，因此本仓库不能宣称已经具备 Full 榜单资格。

---

## 1. 快速开始

```bash
cd aml-retriever

# 一键总验收：环境探测 + 142 项单测 + CLI 自检 + 31 项 HTTP 契约 smoke
./scripts/run_tests.sh

# 起服务（默认 127.0.0.1:8080）
./scripts/serve.sh

# 另开一个终端
curl -s localhost:8080/health
curl -s -X POST localhost:8080/add -H 'Content-Type: application/json' -d '{
  "request_id": "req-1",
  "user_id": "u1",
  "session_id": "s1",
  "messages": [{"role":"user","timestamp":1704067200000,"content":"发布日期定在 2026-08-14，负责人林涛。"}]
}'
curl -s -X POST localhost:8080/search -H 'Content-Type: application/json' -d '{
  "query": "发布日期是什么时候", "user_id": "u1", "top_k": 10
}'
```

只需要 Python 3.11+（自带 FTS5 的 sqlite3）。没有 `pip install` 这一步——**本项目不需要**。

## 2. 官方 Add / Search 契约

字段与错误码严格对齐 [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md)
（2026-08-06 抓取官方 api-guide 与公开评测仓库核对而成）。**报名或提交前须人工复核官方页面。**

### `POST /add`

```jsonc
// 请求
{
  "request_id": "eval:run_abc:conv-0:chunk-0",   // 必填，成功时原样回显
  "user_id":    "eval:run_abc:conv-0",           // 必填，唯一检索范围
  "session_id": "eval:run_abc:sample:0",         // 必填，仅用于组织，不作检索筛选
  "messages": [{ "role": "user", "timestamp": 1704067200000, "content": "..." }]
}
// 响应 200（写入完成且已可检索后才返回）
{ "success": true, "request_id": "...", "user_id": "...", "session_id": "..." }
```

**同步语义**：接口返回时数据已落库并建好索引，下一毫秒的 Search 就能查到。
不返回 202、不返回任务 ID、不提供状态查询。

### `POST /search`

```jsonc
// 请求
{ "query": "...", "options": ["A. ...", "B. ..."], "user_id": "...", "top_k": 100 }
// 响应 200
{ "data": [ { "id": "...", "content": "原始证据原文", "score": 12.34, "created_at": "..." } ] }
```

`data` 是**平铺数组**：不加 `items` 包装层，也不直接返回顶层数组；无结果返回 `[]`。
**平台保留接口返回的顺序**，所以排序在返回前就已完成。

响应里额外带 `view` / `source_message_ids` / `evidence_flags` 三个字段用于本地审计与可追溯性
（官方声明未声明字段会被忽略）。设 `AML_INCLUDE_PROVENANCE=0` 可关闭。

### 其他端点

| 端点 | 用途 |
| --- | --- |
| `GET /health` | 无需鉴权，2xx 即正常 |
| `GET /stats` | 行数统计，不含任何记忆内容 |
| `POST /admin/delete_user` | 按 `user_id` 删除全部数据 |

错误体统一为 `{"detail":{"reason":"..."}}`，字段校验失败返回 **422**。

### 校验口径：严进宽容

官方声明的必填字段一律强校验、**不做任何隐式转换**；官方未声明的字段一律忽略而不报错。

| 规则 | 说明 |
| --- | --- |
| `role` **必填**且非空 | 与 `content` 同级必填；取值不限于 user/assistant |
| `top_k` **必填**且必须是真整数 | 缺失、`"100"`、`100.0`、`10.5`、`true` 全部 422；**没有服务端默认值** |
| `top_k > 上限` 只钳制不拒绝 | 少返回几条好过整批失败 |
| `timestamp` 可选，给了就必须是整数毫秒 | `10.5` 这类小数**拒绝而不截断**；`1704067200000.0` 无损接受 |
| 未声明字段一律忽略 | 顶层与 `messages[]` 内都不报错 |

之所以在"缺 `top_k`"上选择报错而不是回落默认值：评测端一旦漏传，静默返回 10 条会让
线上跑着一个我们自己编出来的参数却毫无提示，排查成本极高——**宁可 422 也不要静默"正确"**。
完整判定矩阵、每条取舍的理由，以及 Add 的 first-write-wins 幂等语义，见
[`docs/API_CONTRACT.md` §5.1](docs/API_CONTRACT.md)。

## 3. 架构分层

```
server.py          HTTP 传输层（标准库 ThreadingHTTPServer，日志不含正文）
   ↑
api.py             官方契约 wrapper：字段映射 + 校验，不参与打分
   ↑  MemoryService  领域服务：隔离 / 幂等 / 生命周期
   ↑
retriever.py       核心引擎：存储 + 候选召回 + 打分 + 重排（不认识 HTTP，也不认识官方字段名）
   ├── views.py    三类视图的确定性构造
   ├── features.py 实体 / 数字 / 日期 / 短语等确定性特征
   └── config.py   全部增强以 flag 暴露，便于消融
```

换榜单或换协议时只改 `api.py` + `server.py`，核心引擎不动。

## 4. 记忆表示：原始消息 + 三类视图

原始消息**全量保存**，视图是叠加的检索入口而**非替代品**。

| 视图 | 构造 | 用途 |
| --- | --- | --- |
| `message` | 每条消息一个文档 | 精确定位单条事实 |
| `window` | 滑动窗口（默认 3 条、重叠 1 条） | 跨轮次的上下文连贯 |
| `session-segment` | 按消息数（12）或时间间隔（1800s）切分 | 会话级主题聚合 |

- 每条视图都带 `source_message_ids`，**可回指原始 message_id**；
- 窗口大小、重叠、切分阈值全部可配置且确定性；
- 视图为**增量构造**：新消息只触发受影响的窗口重建，
  `tests/test_views.py` 断言增量结果与全量重扫**逐字节一致**。

## 5. 检索路线

1. **候选召回**：FTS5 词法检索（中文 unigram/bigram + 拉丁/数字/日期切分），
   可选把 `options` 一并纳入召回；候选上限 `max_candidates` 界定最坏代价。
2. **确定性特征打分**：精确子串、token 覆盖率、实体式短语、数字与日期精确匹配。
3. **重排**：相对新近度、邻接上下文、provenance、当前/历史状态冲突提示。
   `knowledge_update` 类查询靠这一步把**被覆写的旧值降权**（但不删除）。
4. **融合与去重**：BM25 名次与特征分两路加权 RRF；同内容 / 被完全覆盖的视图去重。
5. **稳定输出**：同分时按确定性 tiebreak，保证同输入必然同顺序。

**不做的事**：不生成答案、不改写证据、不跨 `user_id`、不硬编码题目、
不用 confirmed-only 门过滤原始记忆（差异化层只做重排与标注）。

## 6. 消融结论（合成数据，medium/mixed，seed=20260806，top_k=100）

| 档位 | Recall@20 | Recall@100 | MRR | 旧值泄漏@10 | p95(ms) |
| --- | --- | --- | --- | --- | --- |
| `L0_lexical_baseline` | 0.8750 | 1.0000 | 0.5686 | 1.0000 | 3.74 |
| `L1_plus_views` | 0.8724 | 1.0000 | 0.6006 | 1.0000 | 10.88 |
| `L2_plus_exact` | 0.8724 | 1.0000 | 0.6006 | 1.0000 | 11.00 |
| `L3_plus_context` | 0.9948 | 1.0000 | 0.6553 | 0.4375 | 11.33 |
| `L4_plus_dedup` | 1.0000 | 1.0000 | 0.6576 | 0.5104 | 11.54 |
| **`L5_plus_weighted_rrf`（线上默认）** | **1.0000** | **1.0000** | **0.6631** | 0.5312 | 11.53 |
| `L6_temporal_intent_ctrl`（对照组） | 0.9922 | 1.0000 | 0.6561 | 0.3958 | 11.63 |
| `L7_plus_vector` | 跳过（本机无向量依赖） | | | | |

诚实记账：

- **`L2` 在本合成集上无独立增益**，词面重叠的收益已被 `L1` 的视图聚合吃掉。保留但标注无证据。
- **`L6` 是对照组不是推荐档**：时间意图放大即便在它专门针对的 `temporal` 类上也只有 +0.0015，
  整体 MRR 与 Recall@20 却双双下降 → 固化为**关闭**。
- **`temporal` × `paraphrase` 的 MRR 只有 0.077**，是当前最弱环节，也是向量检索最可能补上的地方。

完整方法、参数扫描与**一处已更正的错误结论**见 [`docs/EVAL.md`](docs/EVAL.md)。

## 7. 并发、隐私与删除

**并发**（`tests/test_concurrency.py`，按官方披露的 Add 64 / Search 32 workers 本机模拟）：
SQLite WAL + `busy_timeout` + 写锁重试 + 读连接池。已验证并发下无丢写、无重复写、
幂等仍成立、Search 结果稳定且不跨 user，读写混合不报 `database is locked`。

**隐私**：记忆正文绝不进日志（有回归测试用哨兵文本断言）；错误响应不外泄内部细节；
`api_key` 在配置导出时脱敏。

**删除**：

```bash
python3 -m aml_retriever.cli delete-user --db ./aml.db --user u1   # 删单个 user
python3 -m aml_retriever.cli purge       --db ./aml.db --yes       # 清空整库
rm -f ./aml.db ./aml.db-wal ./aml.db-shm                           # 连 WAL 一并销毁
```

删除会清掉原始消息、视图、FTS 索引、幂等记录与会话游标，并有逐表扫描的残留断言。
完整说明见 [`docs/DATA_LIFECYCLE.md`](docs/DATA_LIFECYCLE.md)。

**当前仓库不含任何赛事数据**，全部为合成或手写示例。

## 8. 证据状态表

| 事项 | 状态 | 依据 |
| --- | --- | --- |
| Add/Search 字段、错误码、同步语义符合官方文档 | **confirmed** | `docs/API_CONTRACT.md` 抓取核对 + `tests/test_api_contract.py` + `scripts/smoke_api.py` 31/31 |
| 写后立即可搜、幂等、`user_id` 隔离、`top_k` 上限 | **confirmed** | 单测 + HTTP smoke 双重覆盖 |
| 三类视图确定性、可配置、可回指 message_id | **confirmed** | `tests/test_views.py`（含增量 vs 全量一致性） |
| 删除彻底性、日志不含正文 | **confirmed** | `tests/test_privacy.py` 逐表扫描 + 哨兵断言 |
| 合成集上的 Recall/MRR/延迟与消融排序 | **observed** | `eval_out/` 产物，同 seed 可复现 |
| 64/32 workers 并发下的一致性 | **observed** | 本机线程池模拟，非真实评测环境的分布式压力 |
| `rrf_weight_lexical=0.1` 是 Pareto 安全点 | **observed** | `docs/EVAL.md` 附录 A（仅限本合成集） |
| `temporal_intent` 应关闭 | **observed** | `docs/EVAL.md` 附录 B（仅限本合成集） |
| 合成集难度分布与官方数据可比 | **inferred** | 未经验证的假设，仅作相对比较用 |
| 向量检索能否带来增益 | **unknown** | 本机无可用依赖，规则禁止为此安装 → 未测 |
| 在官方数据/榜单上的真实表现 | **unknown** | 从未接触官方数据，无任何官方成绩 |
| Dockerfile 能否构建成功 | **confirmed（本机）** | Docker Desktop 4.85.0 / Engine 29.6.2；构建、FTS5、142 tests、31/31 smoke、容器 health/Add/Search 均通过 |
| 「Add/Search 须用 gpt-4o-mini」门控 | **blocked** | 官方当前 Full 清单明确要求；本实现全程不调 LLM，不能勾选 |
| 报名 / Eval Key / 部署 / 正式提交 | **blocked** | 保留人工确认门，本工程不自动执行 |

> 面向「报名 / 提交」的就绪清单、人工确认门（`gpt-4o-mini` 门控、未执行的外部动作、原创性与来源披露模板）见 [`docs/SUBMISSION_READINESS.md`](docs/SUBMISSION_READINESS.md)；可用 `python3 scripts/check_submission_materials.py` 一键静态核对。
>
> 逐步报名、Docker 验证和人工门记录见 [`docs/REGISTRATION_RUNBOOK.md`](docs/REGISTRATION_RUNBOOK.md)。

## 9. 已知限制

1. **改写类时间查询是短板**（`temporal`×`paraphrase` MRR 0.077）。纯确定性词法在查询被改写后抓不到时间锚点。
2. **旧值泄漏不为 0**（默认档 0.53）。原始消息必须全量保留，系统只降权与标注，不删除旧值。
3. **`L2` 无独立增益**，在真实语料上是否有效未知。
4. **中文分词是 unigram+bigram 近似**，非语义分词；长实体可能被切碎。
5. **视图构建拖慢写入**：建库耗时从 1.43s 升到约 7.3s（5184 条消息），换来 MRR +0.03~0.09。
6. **单机 SQLite**，无横向扩展；官方 1200s HTTP 超时下本机延迟余量充足，但未在评测环境验证。
7. **目标评测环境与本机不同**：本机 Docker 已验证，平台环境仍需按同一清单复核。

## 10. 仓库结构

```
aml_retriever/
  api.py          官方契约 wrapper + 领域服务
  server.py       HTTP 传输层
  retriever.py    核心引擎（存储 / 召回 / 打分 / 重排）
  views.py        三类视图构造
  features.py     确定性特征抽取
  config.py       配置与消融开关
  store.py        P0 词法基线（保留作对照，独立可用）
  cli.py          serve / add / search / delete-user / purge / stats / selfcheck
  evaluation/     合成数据集、指标、消融执行器
scripts/
  serve.sh        启动
  run_tests.sh    本地总验收
  smoke_api.py    HTTP 官方契约 smoke
  run_eval.py     消融评测
  run_scan.py     参数扫描
docs/
  API_CONTRACT.md 官方字段核对记录
  EVAL.md         评测方法与全部证据
  DATA_LIFECYCLE.md 数据、隐私与删除
  SUBMISSION_READINESS.md 提交就绪清单与人工确认门
Dockerfile        本机已构建与运行验证（Docker Desktop 4.85.0）
config.example.json
eval_out/         评测产物（只有聚合指标，无语料正文）
```

## 11. 未做的事

不申请 Key、不部署、不提交正式评测、不发邮件、不产生任何费用、不接触真实用户数据或赛事数据。
当前仓库定位为代码路线候选提交材料，不能当作官方评测结果；报名与最终 Full 评测仍需人工确认。

首期截止（用户提供的规划信息）：**2026-08-07 23:59 UTC+8** —— 以官方页面实时口径为准。
