# 数据生命周期、隐私与删除

## 1. 当前仓库里有什么数据

**没有任何赛事数据。** 仓库内出现的全部文本都属于以下两类之一：

1. `aml_retriever/evaluation/dataset.py` 由固定 seed **程序化生成**的合成语料；
2. 测试与文档里**人工手写**的示例句子。

`eval_out/` 下的产物只包含**聚合指标**（Recall / MRR / 延迟 / 行数），
不含任何语料正文。评测过程用的临时 SQLite 库建在 `eval_out/_work/`，跑完自动删除
（若进程被中断可能残留，直接删掉该目录即可）。

## 2. 运行时数据落在哪

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 原始消息 | SQLite `messages` 表 | 全量保留，是检索返回的证据本体 |
| 聚合视图 | SQLite `views` 表 | 滑窗 / 会话片段，含回指的 `source_ids` |
| 检索索引 | SQLite `fts` 虚表 | 分词后的 token |
| 幂等记录 | SQLite `requests` 表 | `(request_id, user_id)` → message_ids |
| 会话游标 | SQLite `sessions` 表 | 增量构窗用的边界状态 |

库文件路径由 `AML_DB_PATH` / `--db` / 配置文件决定，默认 `:memory:`（进程退出即消失）。
使用 WAL 模式时，同目录还会出现 `<db>-wal` 与 `<db>-shm`。

## 3. 日志里有什么（以及没有什么）

**记忆正文绝不进日志。** 具体做法：

- `server.py` 覆写了 `BaseHTTPRequestHandler.log_message`，访问日志只有方法、路径、状态码、耗时；
- 500 错误统一返回 `{"detail":{"reason":"internal error"}}`，不外泄内部异常细节；
- `GET /health` 与 `GET /stats` 只返回状态与行数，不返回任何内容；
- `RetrieverConfig.to_dict()` 会把 `api_key` 脱敏成 `***`。

回归保护：`tests/test_privacy.py::TestNoContentInLogs` 会真实起服务、灌入
一段哨兵文本、捕获 stdout/stderr，断言哨兵**未出现**在日志里。

## 4. 怎么删

### 删单个 user（推荐，最小爆炸半径）

```bash
python3 -m aml_retriever.cli delete-user --db ./aml.db --user "eval:run_abc:conv-0"
```

或走 HTTP：

```bash
curl -s -X POST http://127.0.0.1:8080/admin/delete_user \
     -H 'Content-Type: application/json' \
     -d '{"user_id":"eval:run_abc:conv-0"}'
```

一次删除该 user 的**原始消息 + 聚合视图 + FTS 索引行 + 幂等记录 + 会话游标**，
返回实际删除的行数。删除后同一 user 的 Search 立即返回空数组。

回归保护：`tests/test_privacy.py::TestDeleteLifecycle` 断言删除后
**在存储层逐表扫描**都搜不到任何残留（不只是接口查不到）。

### 清空整库

```bash
python3 -m aml_retriever.cli purge --db ./aml.db --yes   # 必须显式 --yes
```

### 彻底销毁（含 WAL 副本）

只删主库文件是不够的，WAL/SHM 里可能还有尚未 checkpoint 的页：

```bash
rm -f ./aml.db ./aml.db-wal ./aml.db-shm
```

容器场景下，不挂卷运行（默认 `/data` 在容器内）即可做到"容器销毁 = 数据销毁"。

## 5. 若将来获授权接入赛事数据（当前未发生）

以下是**尚未执行**的约束记录，不是已完成事项：

- 只能用于完成当前评测任务；**禁止**用于训练、微调、产品分析、数据集重建或对外传播；
- 只向必要人员开放；避免记录不必要的请求正文（本系统默认已不记录）；
- 任务完成后 **30 天内删除**，延长保留需事先书面同意；
- 删除时使用上面第 4 节的命令，并连同 WAL 副本一并销毁；
- 禁止跨 `user_id` 返回记忆；`session_id` 只用于组织来源会话，不作为检索筛选条件。

上述条款转录自 `docs/API_CONTRACT.md` 第 7 节（2026-08-06 抓取的官方原文要点）。
**以官方页面实时口径为准**；正式接入前须人工复核。
