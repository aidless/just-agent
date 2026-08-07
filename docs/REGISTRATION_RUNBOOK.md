# Agent Memory Challenge 报名与提交操作手册

状态：代码候选仓库已公开；本文只记录可复现步骤和人工确认门，不执行报名、申请 Key、部署或正式评测。

最后核对日期：2026-08-07

## 1. 当前状态

已完成的本地证据：

- `python3 -m unittest discover -s tests`：142 tests，全部通过。
- `python3 -m aml_retriever.cli selfcheck`：8/8 通过。
- `python3 scripts/smoke_api.py`：31/31 通过。
- `python3 scripts/check_submission_materials.py`：25/25 通过。
- 评测指标全部来自合成或手写 fixture，不代表官方榜单成绩。

仍需人工或环境确认：

- Docker 镜像构建与容器内 API 验证。已在本机 Docker Desktop 4.85.0 / Engine 29.6.2 完成构建、FTS5、142 tests、31/31 smoke、容器 health/Add/Search 验证。
- 官方 Full 评测清单要求 Add/Search 使用 `gpt-4o-mini`；当前零 LLM 实现因此处于提交阻塞。
- 真实作者、许可证、第三方来源和原创性披露。
- 报名、Eval Key、可访问部署和正式评测。

## 2. 先确认模型边界

公开评测仓库披露的 `gpt-4o-mini` 属于平台 Answer 阶段。[官方当前参赛说明](https://agentmemories.ai/rules)又在 Full 清单中要求提交系统的 Add/Search 也使用 `gpt-4o-mini`。这两条要求分别影响平台流程和参赛资格，必须分开记录。

本实现的 Add/Search 是确定性的零 LLM 路线，因此当前不能勾选 Full 评测中的模型合规项。可行路径只有主办方书面确认例外，或用户授权后补充真实的 `gpt-4o-mini` Add/Search 适配层。不要添加占位调用，也不要伪造调用记录。

建议发送给主办方的英文问题：

```text
We plan to submit a deterministic, zero-LLM Add/Search retriever.
The current Full evaluation checklist says that the submitted memory system
must use gpt-4o-mini during both Add and Search. Is there an exception for a
deterministic lexical retriever in the textual memory and open methods tracks?
If not, what exact Add/Search integration, credential, data-handling, and
reproducibility requirements should we follow?
```

收到答复后，人工把原文、日期和结论填入 `SUBMISSION_READINESS.md` §6。没有例外答复或真实适配层时，状态保持 `blocked`。

## 3. Docker 验证

Docker Desktop 应用已安装到 `/Users/hu/Applications/Docker.app`，人工接受条款后引擎已稳定运行。本机验证结果为 `confirmed`，目标评测环境仍需按相同清单复核。

安装并启动 Docker Desktop 后，在项目目录执行：

```bash
cd /Users/hu/WorkBuddy/2026-08-06-16-02-06/aml-retriever

docker version
docker build -t aml-retriever:local .

docker run --rm aml-retriever:local \
  python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('FTS5 OK')"

docker run --rm aml-retriever:local \
  python -m unittest discover -s tests

docker run --rm aml-retriever:local \
  python scripts/smoke_api.py

docker run --rm -d --name aml-retriever-local \
  -p 18080:8080 aml-retriever:local

curl -fsS http://127.0.0.1:18080/health

docker stop aml-retriever-local
```

验收条件：

1. `docker build` 成功，构建阶段显示 `build-time FTS5 OK`。
2. 容器内 FTS5 检查输出 `FTS5 OK`。
3. 容器内单测和 smoke 全部通过。
4. 独立容器的 `/health` 返回 2xx。
5. 停止容器后没有遗留名为 `aml-retriever-local` 的容器。

首次构建若本机没有 `python:3.13-slim`，Docker 需要从镜像仓库拉取基础镜像。该网络动作由人工在本机执行，凭据不得写入仓库或聊天。

## 4. 报名前准备包

报名页打开前，准备以下内容：

- 项目名称：`FlowGrid AML Retriever`，若赛事要求独立产品名，再由用户确认最终名称。
- 赛道意向：文本记忆榜、开源方法榜。
- 一句话描述：A deterministic, user-isolated Add/Search memory retriever with multi-view lexical evidence retrieval.
- API 入口说明：`POST /add`、`POST /search`、`GET /health`。
- Docker 启动命令：见 `Dockerfile` 顶部和本文 §3。
- 复现说明：见 `README.md`、`docs/API_CONTRACT.md`、`docs/EVAL.md`。
- 证据边界：本地指标来自合成 fixture，没有官方数据、官方成绩或真实用户数据。
- 隐私承诺：评测数据只用于当前任务，任务结束后按赛事要求删除；项目不把赛事数据用于训练、分析或传播。

需要用户本人填写或确认：

- 作者或团队署名。
- 许可证。
- 第三方代码、论文、模型和数据来源。
- 公开 GitHub 仓库名称与可见性。
- 可接受的部署方式、费用和公开网络暴露范围。

## 5. 报名执行顺序

每一步都先保存页面截图或文字记录，再进入下一步：

1. 打开[赛事页](https://agentmemories.ai/competition/)和[规则页](https://agentmemories.ai/rules)，确认当前截止时间、资格、赛道和模型门槛。
2. 记录主办方对零 LLM Add/Search 的答复。
3. 完成 Docker 验证，并把结果写入 `SUBMISSION_READINESS.md`。
4. 用户确认作者、许可证、来源披露和公开仓库策略。
5. 公开候选仓库：`https://github.com/dlxeva/flowgrid-aml-retriever`。后续变更仍需用户确认。
6. 按当前报名表填写项目说明、仓库地址、Docker 启动方法和 API wrapper 说明。
7. 若平台发放 Eval Key 或 API Key，凭据只保存在环境变量或本地密钥管理器中，不写入代码、README、日志或 outbox。
8. 先用合成数据对公开 endpoint 做健康检查、Add/Search 同步性、幂等和 user 隔离 smoke。
9. 用户确认后，再启动正式评测或提交表单。

表单字段、截止时间和 Key 申请方式以当日官方页面为准。本文不代填、不代提交，也不把本地评测数字写成官方成绩。

## 6. 人工门记录模板

```text
官方页面复核日期：
规则版本或页面链接：
首期截止时间：
赛道选择：文本记忆榜 / 开源方法榜 / 其他：
主办方对零 LLM Add/Search 的答复：
Docker 构建结果：
作者与许可证确认：
第三方来源披露确认：
公开仓库确认：
Eval Key 获取确认：
部署地址与健康检查：
正式评测确认：
```

任何一项没有证据，都保持 `unknown`、`unverified` 或 `blocked`，不向外部描述为已完成。
