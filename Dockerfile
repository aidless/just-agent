# FlowGrid AML Retriever — Add/Search runtime image.
#
# Build:
#   docker build -t flowgrid-aml-retriever:local .
#
# Verify FTS5, unit tests, and the HTTP contract:
#   docker run --rm flowgrid-aml-retriever:local python -c \
#       "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('FTS5 OK')"
#   docker run --rm flowgrid-aml-retriever:local python -m unittest discover -s tests
#   docker run --rm flowgrid-aml-retriever:local python scripts/smoke_api.py
#
# Run with container-local storage:
#   docker run --rm -p 8080:8080 flowgrid-aml-retriever:local
#
# Run with persistent storage:
#   docker run --rm -p 8080:8080 -v "$PWD/data:/data" \
#       -e AML_DB_PATH=/data/aml.db flowgrid-aml-retriever:local
#
# Run with authentication:
#   docker run --rm -p 8080:8080 \
#       -e AML_AUTH_MODE=bearer -e AML_API_KEY=your-secret flowgrid-aml-retriever:local

FROM python:3.13-slim

# The default runtime uses only Python's standard library and SQLite FTS5.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    AML_HOST=0.0.0.0 \
    AML_PORT=8080 \
    AML_DB_PATH=/data/aml.db

WORKDIR /app

# Copy runtime and verification files only.
COPY aml_retriever/ /app/aml_retriever/
COPY scripts/ /app/scripts/
COPY tests/ /app/tests/
COPY config.example.json /app/config.example.json
COPY README.md /app/README.md
COPY docs/ /app/docs/

# Data remains container-local unless /data is mounted.
RUN mkdir -p /data && \
    python -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(x)');print('build-time FTS5 OK')"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 aml && chown -R aml:aml /app /data
USER aml

EXPOSE 8080

# Health remains unauthenticated for external orchestration.
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys;\
sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('AML_PORT','8080')+'/health', timeout=4).status < 300 else 1)"

CMD ["python", "-m", "aml_retriever.cli", "serve"]
