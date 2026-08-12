# FlowGrid AML Retriever

面向 [Agent Memory Leaderboard](https://agentmemories.ai/leaderboard/) 的确定性、
证据优先记忆检索系统。

[English](README.md) · [API 契约](docs/API_CONTRACT.md) ·
[评测方法](docs/EVAL.md) · [数据生命周期](docs/DATA_LIFECYCLE.md)

FlowGrid AML Retriever 实现记忆系统需要提供的两个操作：同步 `Add` 与按用户隔离的
`Search`。系统保存每一条原始消息，建立可追溯的检索视图，并返回按相关性排序的来源
证据；它不生成最终答案。

## 榜单成绩

| 榜单 | 系统名 | 排名 | 综合分 | 参评版本 |
| --- | --- | ---: | ---: | --- |
| 文本记忆 · 开源 / 学术方法，首期公开快照 | `FlowGrid_AML_Retriever` | **第 8 名** | **43.98** | v1.0 |

该快照第一名为 45.06，分差 1.08。当前仓库版本为 v1.1.0；新增的受保护时间证据
重排已经过本地合成消融验证，但尚未获得新的官方分数。

## 核心能力

- `Add` 成功返回前同步持久化，写入后立即可检索；
- 以 `user_id` 为严格检索边界；
- `(request_id, user_id)` 重复写入保持幂等；
- 原始消息完整保留，派生视图可回指来源消息 ID；
- 融合 SQLite FTS5、中文字符 n-gram、实体/数字/日期精确特征、时间与邻接信号、
  RRF 和去重；
- 只将受保护的事实更新作为软重排信号，旧证据不删除；
- 默认路径只使用 Python 标准库与 SQLite FTS5，不依赖第三方 Python 包。

## 架构

```text
HTTP Add/Search
      │
      ▼
契约校验与字段映射
      │
      ▼
Memory Service
  ├─ 用户隔离
  ├─ 幂等
  └─ 删除生命周期
      │
      ▼
Evidence Retriever
  ├─ 原始消息
  ├─ 滑动窗口
  ├─ 会话片段
  ├─ FTS5 与确定性特征
  └─ 融合、时间重排、来源追踪、去重
      │
      ▼
按相关性排序的原始证据，不生成答案
```

HTTP wrapper 与检索引擎分层：协议变化集中在 `api.py` 和 `server.py`，存储与排序逻辑
可以独立测试。

## 快速开始

环境要求：Python 3.11+，且 Python 的 `sqlite3` 已启用 FTS5。

```bash
git clone https://github.com/dlxeva/flowgrid-aml-retriever.git
cd flowgrid-aml-retriever

# 环境检查、146 项单测、CLI 自检、31 项 HTTP smoke
./scripts/run_tests.sh

# 默认启动在 127.0.0.1:8080，数据库为 ./aml.db
./scripts/serve.sh
```

不需要执行 `pip install`。

### Add

```bash
curl -sS http://127.0.0.1:8080/add \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-1",
    "user_id": "user-1",
    "session_id": "session-1",
    "messages": [
      {
        "role": "user",
        "timestamp": 1785945600000,
        "content": "发布日期调整为 2026 年 8 月 14 日。"
      }
    ]
  }'
```

只有在数据已经落库并可检索后，接口才返回成功：

```json
{
  "success": true,
  "request_id": "req-1",
  "user_id": "user-1",
  "session_id": "session-1"
}
```

### Search

```bash
curl -sS http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "发布日期是什么时候？",
    "user_id": "user-1",
    "top_k": 10
  }'
```

```json
{
  "data": [
    {
      "id": "...",
      "content": "发布日期调整为 2026 年 8 月 14 日。",
      "score": 12.34,
      "created_at": "2026-08-05T16:00:00Z",
      "view": "message",
      "source_message_ids": ["..."],
      "evidence_flags": ["lexical"]
    }
  ]
}
```

`view`、`source_message_ids` 与 `evidence_flags` 是可选的来源追踪字段；设置
`AML_INCLUDE_PROVENANCE=0` 可关闭。

## Docker

```bash
docker build -t flowgrid-aml-retriever:local .

docker run --rm flowgrid-aml-retriever:local \
  python -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('FTS5 OK')"
docker run --rm flowgrid-aml-retriever:local \
  python -m unittest discover -s tests
docker run --rm flowgrid-aml-retriever:local \
  python scripts/smoke_api.py

docker run --rm -p 8080:8080 \
  -v "$PWD/data:/data" \
  -e AML_DB_PATH=/data/aml.db \
  flowgrid-aml-retriever:local
```

容器以非 root 用户运行，并提供无需鉴权的 `/health`。Add、Search、统计和删除端点
支持 Bearer、Token 与 `X-Api-Key` 鉴权。

## 检索流程

1. **保存原始证据**：消息不改写，使用稳定 ID，并在请求级保证幂等。
2. **建立多种视图**：单消息、滑动窗口和会话片段提供不同的召回入口，但不替代原文。
3. **候选召回**：FTS5 使用拉丁词、数字、日期以及中文 unigram/bigram；选择题选项可参与查询。
4. **确定性特征打分**：精确子串、token 覆盖率、实体、数字、日期、新近度和邻接上下文分别计分。
5. **保守处理更新**：只有同时满足时间查询、近重复事实与显式更新语义时，才软提升新证据、
   降低旧证据；历史证据始终保留。
6. **融合与去重**：加权 RRF 融合词法与特征名次，再以确定性规则处理同分与重复结果。

全部增强开关都位于 `RetrieverConfig`，可以单独关闭做消融。

## 评测

仓库内置确定性的合成评测台，用于回归和消融。以下数字属于开发证据，不能替代官方榜单成绩。

`classic` / medium / mixed / seeds 20260806–20260808 / `top_k=100`：

| 配置 | Recall@20 mean（范围） | Recall@100 | MRR mean（范围） |
| --- | ---: | ---: | ---: |
| v1.0 基线（`L5_plus_weighted_rrf`） | 0.9948（0.9870–1.0000） | 1.0000 | 0.6728（0.6631–0.6791） |
| v1.1 受保护覆写（`L9_guarded_supersession`） | 0.9948（0.9870–1.0000） | 1.0000 | 0.6948（0.6854–0.7004） |

v1.1 默认配置在三个 seed 上保持 Recall@20，同时逐 seed 提升 MRR。更激进的时间权重
虽然继续提高 MRR，但会让一个 seed 的 Recall@20 下降，因此没有成为默认值。

```bash
python3 scripts/run_eval.py \
  --scale medium \
  --difficulty mixed \
  --suite classic \
  --seeds 20260806,20260807,20260808 \
  --top-k 100
```

报告会生成到已忽略的 `eval_out/`。完整指标定义、对照组、回退案例和消融历史见
[docs/EVAL.md](docs/EVAL.md)。

## 隐私与删除

- 服务日志不记录记忆正文；
- Search 只能访问一个明确的 `user_id`；
- `/health` 与 `/stats` 不返回记忆内容；
- 配置输出会隐藏鉴权密钥；
- `delete-user` 会清除该用户的消息、视图、FTS 行、请求记录与会话游标；
- `purge --yes` 会清空整个数据库。

```bash
python3 -m aml_retriever.cli delete-user --db ./aml.db --user user-1
python3 -m aml_retriever.cli purge --db ./aml.db --yes
```

销毁文件型 SQLite 实例时，还应同时删除同名 `-wal` 与 `-shm` 文件。详见
[docs/DATA_LIFECYCLE.md](docs/DATA_LIFECYCLE.md)。

本仓库只含合成 fixture 与手写示例，不含排行榜评测数据。

## 已知限制

- 默认路线是确定性词法检索，语义距离较远的改写，尤其时间类改写，仍是主要短板；
- 受保护覆写是排序信号，不是真值裁决，冲突与历史证据会按设计继续保留；
- 中文分词采用字符 unigram/bigram，不是语言模型或完整形态学分词；
- SQLite 是单机后端。WAL、重试和读连接池可处理已验证的并发形态，但系统不支持横向分布式扩展；
- 合成评测只适合受控比较，新版本的正式分数只能由官方评测确定。

## 仓库结构

```text
aml_retriever/
  api.py          Add/Search 契约与领域服务
  server.py       HTTP 传输层
  retriever.py    存储、召回、打分与重排
  views.py        单消息、滑窗与会话片段视图
  features.py     确定性特征抽取
  config.py       配置与消融开关
  store.py        用于回归的词法基线
  cli.py          服务、数据与自检命令
  evaluation/     合成数据、指标与消融评测台
scripts/
  serve.sh        本地服务启动
  run_tests.sh    完整本地验收
  smoke_api.py    HTTP 契约 smoke
  run_eval.py     可复现消融
  run_scan.py     参数扫描
docs/
  API_CONTRACT.md
  EVAL.md
  DATA_LIFECYCLE.md
Dockerfile
config.example.json
```

## 项目边界

FlowGrid AML Retriever 是独立的排行榜实现。它吸收了 FlowGrid 的来源追踪、时间状态、
冲突保留和用户隔离思想，但不是 FlowGrid Core 产品；排行榜成绩也不应被表述为
FlowGrid Core 的产品验证。

## 许可证

本项目采用 [MIT License](LICENSE)。
