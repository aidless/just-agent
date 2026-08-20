# Hosted Deployment Guide

This document explains how to stand up the just-agent Add/Search endpoint for
the [Agent Memory Leaderboard](https://agentmemories.ai/competition/) hosted
evaluation, and how to fill in the **Evaluation access form** the platform
requires.

It covers **scaffolding only** — bringing the service up locally (or on a host
you control) behind Docker Compose. It does not provision a cloud account,
register a domain, or issue a TLS certificate; the last section lists what to
add when exposing the endpoint to the evaluation environment.

The wire contract implemented by the service is documented in
[docs/API_CONTRACT.md](docs/API_CONTRACT.md). Read it before reporting URLs.

---

## 1. Prerequisites

- Docker Engine 24+ and the Compose v2 plugin (`docker compose`).
- The repository checked out at the deployment host.
- A long random string for `AML_API_KEY` (the shared auth secret).
- *(Optional, for the LLM fact-extraction pipeline)* a `scnet.cn` API key.
  The pipeline is **off by default**; see §6.

Verify the host Python ships FTS5 (the container does this at build time, but
checking the host is useful for debugging):

```bash
python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('FTS5 OK')"
```

---

## 2. Configure secrets

Copy the template and fill in values. **Never commit `.env`.**

```bash
cp .env.example .env
$EDITOR .env
```

At minimum set:

| Variable | Purpose | Example |
| --- | --- | --- |
| `AML_API_KEY` | Shared secret the leaderboard sends to authenticate Add/Search. | `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `AML_AUTH_MODE` | Auth scheme: `bearer` / `token` / `x-api-key`. Must match what you report on the access form. | `bearer` |
| `AML_HOST_PORT` | Host port mapped to the container's 8080. | `8080` |

Optional fact-extraction variables (§6):

| Variable | Purpose | Default |
| --- | --- | --- |
| `SC_API_KEY` | scnet.cn Kimi-K2.5 API key. Ignored unless the flag is on. | *(empty)* |
| `SC_API_BASE` | scnet.cn API base URL. | `https://api.scnet.cn/v1` |
| `AML_FLAG_FACT_EXTRACTION` | `1` enables the fact-extraction pipeline; `0` (default) keeps lexical-only retrieval. | `0` |

All variables are interpolated by `docker compose` from `.env`; none are baked
into the image.

---

## 3. Build and start

```bash
docker compose up -d --build
```

This builds the `Dockerfile` (Python 3.13-slim, stdlib + SQLite FTS5 only),
starts the service as a non-root user, and attaches a persistent named volume
to `/data` so memories survive container recreation.

Confirm the container is healthy (compose runs the unauthenticated `/health`
probe every 15 s):

```bash
docker compose ps
# STATUS should read "Up (healthy)"

curl -fsS http://127.0.0.1:${AML_HOST_PORT:-8080}/health
```

A healthy `/health` returns HTTP 200 with a small JSON body and **never**
includes memory content (see `server.py`).

---

## 4. Smoke-test the contract

Run the bundled HTTP smoke suite against your live instance. It exercises the
full Add → immediate-Search → user isolation → idempotency → 422 validation
matrix from [docs/API_CONTRACT.md](docs/API_CONTRACT.md):

```bash
docker compose exec aml-retriever python scripts/smoke_api.py \
  --base-url http://127.0.0.1:8080 --api-key "$AML_API_KEY"
```

Or, from the host, against the mapped port:

```bash
python3 scripts/smoke_api.py --base-url http://127.0.0.1:${AML_HOST_PORT:-8080} \
  --api-key "$(grep ^AML_API_KEY= .env | cut -d= -f2-)"
```

Exit code 0 means every contract check passed. A quick manual check:

```bash
BASE=http://127.0.0.1:${AML_HOST_PORT:-8080}
KEY="$(grep ^AML_API_KEY= .env | cut -d= -f2-)"

# Add (synchronous — returns only after the memory is durable & searchable)
curl -sS "$BASE/add" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEY" -d '{
    "request_id":"smoke-1","user_id":"smoke-user","session_id":"smoke-sess",
    "messages":[{"role":"user","timestamp":1785945600000,
      "content":"The launch date moved to August 14, 2026."}]
  }'

# Search (must find the just-added memory, scoped to that user_id)
curl -sS "$BASE/search" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $KEY" -d '{
    "query":"When is the launch?","user_id":"smoke-user","top_k":10
  }'
```

---

## 5. Fill in the Evaluation access form

The leaderboard's access form asks for the reachable URLs and credentials it
will use to call your endpoints. The official contract states the
request/response **shape is fixed and path-independent**, so the form records
the full URL of each endpoint.

Using the defaults from `.env.example` (`AML_AUTH_MODE=bearer`, paths
`/add` `/search` `/health`, host port `8080`), and assuming your public host
is `aml.example.com` behind HTTPS (see §7):

| Form field | Value to report | Notes |
| --- | --- | --- |
| **Add URL** | `https://aml.example.com/add` | `POST`, synchronous, returns `success:true` only after the memory is durable. |
| **Search URL** | `https://aml.example.com/search` | `POST`, returns `{data:[...]}`; platform preserves your order and reads up to `top_k` (official eval = 100). |
| **Health URL** | `https://aml.example.com/health` | `GET`, **unauthenticated**, any 2xx = healthy. If the form has no separate health field, the platform checks the same-origin `/health`. |
| **Auth type** | `Bearer` (matches `AML_AUTH_MODE=bearer`) | Or `Token` / `X-Api-Key` — must equal `AML_AUTH_MODE`. |
| **Auth key / secret** | the value of `AML_API_KEY` | Do **not** put it in the URL — the contract forbids credentials in the URL. Send it in the header. |

Rules baked into the contract (double-check before submitting):

- The endpoint must be **reachable from the evaluation environment** (no
  private/loopback/link-local addresses).
- **No credentials in the URL.**
- Add is **synchronous** — do not return 202 / a task ID; the platform
  treats anything other than `success:true` + matching IDs as immediate
  failure.
- Health is **unauthenticated** and returns any 2xx.
- The platform retries 5xx/408/429 with backoff; 422 (format error) is
  **terminal** — validate inputs strictly (the service already does; see the
  validation matrix in API_CONTRACT.md §5.1).

---

## 6. The fact-extraction pipeline (flag-gated, default OFF)

A complementary task adds an LLM fact-extraction pipeline that calls the
scnet.cn Kimi-K2.5 API at Add time to extract structured facts
(subject/predicate/object/time) stored alongside the FTS5 index, and boosts
fact-matched evidence at Search time for knowledge-update / temporal queries.

It is controlled by the **`fact_extraction`** ablation flag (default `False`),
exposed via the standard `AML_FLAG_*` env convention:

- `AML_FLAG_FACT_EXTRACTION=0` (default) — deterministic lexical-only path.
  `SC_API_KEY` / `SC_API_BASE` are ignored.
- `AML_FLAG_FACT_EXTRACTION=1` — enables the pipeline. The module reads
  `SC_API_KEY` / `SC_API_BASE` from the environment. If the key is absent or
  the API is unreachable, it **falls back to lexical-only** retrieval
  gracefully (no hard failure).

> **Flag name:** `fact_extraction`
> **Env override:** `AML_FLAG_FACT_EXTRACTION` (1/0)
> **Default:** `0` (OFF)

Until the `fact_extraction` flag is added to `DEFAULT_FLAGS` in
`aml_retriever/config.py` by the pipeline task, setting
`AML_FLAG_FACT_EXTRACTION=1` is a silent no-op (the config loader only applies
`AML_FLAG_*` overrides for flags that already exist). The compose file and
`.env.example` are forward-compatible: flipping the flag on later requires no
further changes here — just set `AML_FLAG_FACT_EXTRACTION=1` and provide
`SC_API_KEY`.

The API key is **never hardcoded**. It is read from the `SC_API_KEY`
environment variable at runtime; `SC_API_BASE` defaults to
`https://api.scnet.cn/v1` and is also overridable via env.

---

## 7. Exposing to the evaluation environment

`docker compose up` binds the service to your **host** port (default 8080) on
plain HTTP. The official contract recommends **HTTPS** for production, so
place a reverse proxy (Caddy, nginx, Traefik) in front that terminates TLS
and forwards to the container:

```text
evaluation harness  --HTTPS-->  reverse proxy  --HTTP-->  container:8080
```

A minimal Caddyfile that auto-issues a Let's Encrypt certificate:

```caddy
aml.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

With nginx, terminate TLS and `proxy_pass http://127.0.0.1:8080;`. Ensure:

- The proxy **does not log request/response bodies** (memory privacy — the
  service itself never logs bodies; extend that discipline to the proxy).
- Timeouts are generous: the official Add/Search HTTP timeout is **1200 s**.
  Set `proxy_read_timeout 1200s;` (or equivalent) so long batches are not cut.
- The `/health` path passes through unauthenticated.

If you cannot use TLS, the platform can still reach a plain-HTTP endpoint, but
the secret travels in cleartext — use TLS whenever feasible.

---

## 8. Operations

**View logs** (metadata only — never memory content):

```bash
docker compose logs -f aml-retriever
```

**Stats** (row counts, no memory content; auth-gated):

```bash
curl -sS http://127.0.0.1:${AML_HOST_PORT:-8080}/stats \
  -H "Authorization: Bearer $AML_API_KEY"
```

**Delete one user** (messages, views, FTS rows, request records, cursors):

```bash
curl -sS -X POST http://127.0.0.1:${AML_HOST_PORT:-8080}/admin/delete_user \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $AML_API_KEY" \
  -d '{"user_id":"the-user-id"}'
```

**Teardown** (keeps the named volume; data persists across restarts):

```bash
docker compose down          # stop, keep data
docker compose down -v       # stop AND delete the volume (irreversible)
```

For file-backed SQLite, also remove the `-wal` and `-shm` sidecar files when
destroying an instance — see [docs/DATA_LIFECYCLE.md](docs/DATA_LIFECYCLE.md).

Per the official privacy obligations, delete evaluation data and derived
copies within **30 days** of task completion.

---

## 9. Environment variable reference

| Variable | Source | Default | Meaning |
| --- | --- | --- | --- |
| `AML_API_KEY` | `.env` (required) | — | Shared auth secret. |
| `AML_AUTH_MODE` | `.env` | `bearer` | `bearer` / `token` / `x-api-key` / `none`. |
| `AML_HOST_PORT` | `.env` | `8080` | Host port mapped to container 8080. |
| `AML_ADD_PATH` | `.env` | `/add` | Add endpoint path. |
| `AML_SEARCH_PATH` | `.env` | `/search` | Search endpoint path. |
| `AML_HEALTH_PATH` | `.env` | `/health` | Unauthenticated health path. |
| `AML_POOL_SIZE` | `.env` | `24` | SQLite read-connection pool. |
| `AML_BUSY_TIMEOUT_MS` | `.env` | `10000` | SQLite busy timeout (ms). |
| `SC_API_KEY` | `.env` | *(empty)* | scnet.cn API key (fact-extraction only). |
| `SC_API_BASE` | `.env` | `https://api.scnet.cn/v1` | scnet.cn API base URL. |
| `AML_FLAG_FACT_EXTRACTION` | `.env` | `0` | `1` enables the fact-extraction pipeline. |
| `AML_FLAG_*` | env | per `DEFAULT_FLAGS` | Any ablation flag, e.g. `AML_FLAG_SUPERSESSION=1`. |
| `AML_CONFIG` | env | — | Path to a JSON config file (alternative to env). |

Container-internal vars fixed by the `Dockerfile` (`AML_HOST=0.0.0.0`,
`AML_PORT=8080`, `AML_DB_PATH=/data/aml.db`, `PYTHONPATH=/app`) are overridden
by compose only where noted; leave them unless you know why you're changing
them.
