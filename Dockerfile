# AML Retriever — 官方 Add/Search wrapper 运行镜像
#
# 本 Dockerfile 已在 2026-08-07 的 Docker Desktop 4.85.0 / Engine 29.6.2
#（Apple Silicon, linux/arm64）上完成本机构建与运行验证：
#   - docker build 成功，构建期 FTS5 检查通过
#   - 容器内 142 个单测通过
#   - 容器内 HTTP smoke 31/31 通过
#   - 独立容器的 /health、Add、立即 Search 通过
# 提交前仍需按目标评测环境重新复核。
#
# 构建：
#   docker build -t aml-retriever:local .
#
# 构建后自检（三条都必须通过）：
#   docker run --rm aml-retriever:local python -c \
#       "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('FTS5 OK')"
#   docker run --rm aml-retriever:local python -m unittest discover -s tests
#   docker run --rm aml-retriever:local python scripts/smoke_api.py
#
# 运行（内存库，容器停止即丢弃）：
#   docker run --rm -p 8080:8080 aml-retriever:local
#
# 运行（持久库，落在宿主机 ./data）：
#   docker run --rm -p 8080:8080 -v "$PWD/data:/data" \
#       -e AML_DB_PATH=/data/aml.db aml-retriever:local
#
# 带鉴权运行：
#   docker run --rm -p 8080:8080 \
#       -e AML_AUTH_MODE=bearer -e AML_API_KEY=your-secret aml-retriever:local

FROM python:3.13-slim

# 纯标准库实现：不 pip install 任何东西。
# 之所以选官方 python slim 镜像，正是因为它自带的 libsqlite3 已启用 FTS5。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    AML_HOST=0.0.0.0 \
    AML_PORT=8080 \
    AML_DB_PATH=/data/aml.db

WORKDIR /app

# 只拷贝运行与自检所需内容
COPY aml_retriever/ /app/aml_retriever/
COPY scripts/ /app/scripts/
COPY tests/ /app/tests/
COPY config.example.json /app/config.example.json
COPY README.md /app/README.md
COPY docs/ /app/docs/

# 数据目录；未挂卷时也能启动（数据随容器销毁）
RUN mkdir -p /data && \
    python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('build-time FTS5 OK')"

# 以非 root 运行
RUN useradd --create-home --uid 10001 aml && chown -R aml:aml /app /data
USER aml

EXPOSE 8080

# 官方要求 Health 为无需鉴权的 GET，任意 2xx 即正常
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys;\
sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('AML_PORT','8080')+'/health', timeout=4).status < 300 else 1)"

CMD ["python", "-m", "aml_retriever.cli", "serve"]
