# FlowGrid AML Retriever

Deterministic, evidence-first memory retrieval for the
[Agent Memory Leaderboard](https://agentmemories.ai/leaderboard/).

[中文说明](README.zh-CN.md) · [API contract](docs/API_CONTRACT.md) ·
[Evaluation methodology](docs/EVAL.md) · [Data lifecycle](docs/DATA_LIFECYCLE.md)

FlowGrid AML Retriever implements the two operations required from a memory
system: synchronous `Add` and user-isolated `Search`. It stores every original
message, builds traceable retrieval views, and returns ranked source evidence.
It does not generate final answers.

## Leaderboard result

| Track | Entry | Rank | Score | Evaluated version |
| --- | --- | ---: | ---: | --- |
| Text Memory · Open-source / Academic Methods, first public snapshot | `FlowGrid_AML_Retriever` | **#8** | **43.98** | v1.0 |

The top score in that snapshot was 45.06, a difference of 1.08 points. The
current repository is v1.4. Compared with v1.1 it adds guarded,
evidence-backed changes on top of the same deterministic core: temporal
fallback for messages without timestamps, a day-level event-time prefix on
returned evidence that is applied only when the query has temporal context
(see [docs/EVAL.md](docs/EVAL.md)), a query-type-aware recency weight
(plain non-temporal queries get 2.0 instead of 8.0), and a time-anchor
fallback fix so aggregate views correctly fall back to message timestamps
when no in-body time expression exists (v1.4; local paired end-to-end
reproduction +5.3pp on the same slice). These changes have local synthetic,
retrieval-proxy, and end-to-end evidence but have not been assigned a new
official score.

## What it does

- Synchronously persists each `Add` request before returning success.
- Makes newly added memories immediately searchable.
- Enforces strict retrieval isolation by `user_id`.
- Replays duplicate `(request_id, user_id)` writes idempotently.
- Preserves every original message and links derived views back to source IDs.
- Combines SQLite FTS5, Chinese character n-grams, exact entities, numbers,
  dates, temporal signals, adjacency, reciprocal-rank fusion, and deduplication.
- Applies guarded temporal supersession as a soft ranking signal; older evidence
  remains stored and retrievable.
- Falls back to content/anchor-derived event times when a message has no
  timestamp (`temporal_fallback`), and prefixes returned evidence with a
  day-level event date when the query has temporal context
  (`content_timestamp_prefix`, gated by `has_temporal_context`) so the answer
  model can use it on temporal questions without altering stored text.
- Runs with Python's standard library and SQLite FTS5, with no third-party
  Python package in the default path.

## Architecture

```text
HTTP Add/Search
      │
      ▼
Contract validation and field mapping
      │
      ▼
Memory service
  ├─ user isolation
  ├─ idempotency
  └─ deletion lifecycle
      │
      ▼
Evidence retriever
  ├─ original messages
  ├─ sliding windows
  ├─ session segments
  ├─ FTS5 and deterministic features
  └─ fusion, temporal reranking, provenance, deduplication
      │
      ▼
Ranked source evidence — no answer generation
```

The HTTP wrapper is intentionally separate from the retrieval engine. Protocol
changes remain in `api.py` and `server.py`; storage and ranking stay independently
testable.

## Quick start

Requirements:

- Python 3.11 or newer
- Python's `sqlite3` linked with FTS5

```bash
git clone https://github.com/dlxeva/flowgrid-aml-retriever.git
cd flowgrid-aml-retriever

# Environment check, 150 unit tests, CLI self-check, and 31 HTTP smoke checks
./scripts/run_tests.sh

# Start on 127.0.0.1:8080 with ./aml.db
./scripts/serve.sh
```

No `pip install` step is required.

### Add a memory

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
        "content": "The launch date moved to August 14, 2026."
      }
    ]
  }'
```

Successful `Add` responses are returned only after the memory is durable and
searchable:

```json
{
  "success": true,
  "request_id": "req-1",
  "user_id": "user-1",
  "session_id": "session-1"
}
```

### Search for evidence

```bash
curl -sS http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "When is the launch?",
    "user_id": "user-1",
    "top_k": 10
  }'
```

```json
{
  "data": [
    {
      "id": "...",
      "content": "The launch date moved to August 14, 2026.",
      "score": 12.34,
      "created_at": "2026-08-05T16:00:00Z",
      "view": "message",
      "source_message_ids": ["..."],
      "evidence_flags": ["lexical"]
    }
  ]
}
```

`view`, `source_message_ids`, and `evidence_flags` provide optional provenance.
Set `AML_INCLUDE_PROVENANCE=0` to omit them.

## Docker

```bash
docker build -t flowgrid-aml-retriever:local .

# Verify FTS5 and the full test suite inside the image
docker run --rm flowgrid-aml-retriever:local \
  python -c "import sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('FTS5 OK')"
docker run --rm flowgrid-aml-retriever:local \
  python -m unittest discover -s tests
docker run --rm flowgrid-aml-retriever:local \
  python scripts/smoke_api.py

# Persistent service
docker run --rm -p 8080:8080 \
  -v "$PWD/data:/data" \
  -e AML_DB_PATH=/data/aml.db \
  flowgrid-aml-retriever:local
```

The container runs as a non-root user and exposes an unauthenticated `/health`
endpoint. Bearer, Token, and `X-Api-Key` authentication are available for Add,
Search, statistics, and deletion endpoints.

## Retrieval pipeline

1. **Persist original evidence.** Messages are stored unchanged, with stable
   IDs and request-level idempotency.
2. **Build multiple views.** Message, sliding-window, and session-segment views
   provide different recall surfaces without replacing the source messages.
3. **Generate candidates.** FTS5 retrieval uses Latin tokens, numbers, dates,
   and Chinese unigrams/bigrams; optional answer choices can contribute query
   terms.
4. **Score deterministic signals.** Exact substrings, token coverage, entities,
   numbers, dates, recency, and neighboring context contribute independent
   evidence.
5. **Handle updates conservatively.** A newer near-duplicate receives a soft
   boost only when the query is temporal and explicit update language is
   present. The earlier evidence is demoted, never deleted.
6. **Fuse and deduplicate.** Weighted reciprocal-rank fusion combines lexical
   and feature ranks before deterministic tie-breaking.

All ranking controls live in `RetrieverConfig` and can be disabled independently
for ablation.

## Evaluation

The repository includes a deterministic synthetic evaluation harness for
regression testing and ablation. These metrics are development evidence, not a
substitute for the official leaderboard score.

`classic`, medium, mixed difficulty, seeds 20260806–20260808, `top_k=100`:

| Configuration | Recall@20 mean (range) | Recall@100 | MRR mean (range) |
| --- | ---: | ---: | ---: |
| v1.0 baseline (`L5_plus_weighted_rrf`) | 0.9948 (0.9870–1.0000) | 1.0000 | 0.6728 (0.6631–0.6791) |
| v1.1 guarded supersession (`L9_guarded_supersession`) | 0.9948 (0.9870–1.0000) | 1.0000 | 0.6948 (0.6854–0.7004) |
| v1.2 production (`L11_v12_production`) | 0.9983 (0.9948–1.0000) | 1.0000 | 0.7785 (0.7427–0.8020) |
| v1.3 production (`L12_v13_production`) | 0.9983 (0.9948–1.0000) | 1.0000 | 0.7785 (0.7427–0.8020) |

The v1.2 default adds `temporal_fallback` and `content_timestamp_prefix` on top
of v1.1 (the prefix is gated by query temporal context since v1.2.1): Recall@20
is preserved across all three seeds while MRR improves on each seed. On real
LoCoMo-style data (locomo10.json, 1977 queries, `top_k=100`) the v1.2 default
raises Recall@20 from 0.8958 to 0.9014, Recall@100 from 0.9540 to 0.9580, and
MRR from 0.6186 to 0.6203. An end-to-end local reproduction (DeepSeek official
`deepseek-v4-flash` answers judged by `deepseek-v4-pro`, 297-item stratified
sample) scores 0.6229 vs 0.5724 for the v1.1 default; the query-gated variant
(v1.2.1) is neutral-to-positive overall (+1.35 pp paired) and removes the
non-temporal regression (multi-hop category +0.06).

v1.3 adds a **query-type-aware recency weight** at the code level (plain
non-temporal queries use 2.0 instead of 8.0; temporal-intent queries keep 8.0):
locomo10 MRR rises from 0.6203 to 0.6324 (R@20 0.9074 / R@100 0.9565), and the
synthetic L9 MRR rises from 0.6854 to 0.7281 with Recall unchanged. The flag set
is identical to v1.2.1: `preference_role_boost` was briefly enabled during v1.3
development but reverted after paired same-sample gates (slices 50/100/200,
bit-identical with the flag off) showed the apparent gain was sample variance.
These are development evidence;
only an official run can establish a new official score.

Reproduce a run with:

```bash
python3 scripts/run_eval.py \
  --scale medium \
  --difficulty mixed \
  --suite classic \
  --seeds 20260806,20260807,20260808 \
  --top-k 100
```

Generated reports are written to the ignored `eval_out/` directory. See
[docs/EVAL.md](docs/EVAL.md) for metric definitions, controls, known regressions,
and the full ablation history.

## Privacy and deletion

- Memory content is never written to service logs.
- Searches are scoped to exactly one `user_id`.
- `/health` and `/stats` never return memory content.
- Configuration rendering redacts authentication secrets.
- `delete-user` removes messages, views, FTS rows, request records, and session
  cursors for one user.
- `purge --yes` clears the complete database.

```bash
python3 -m aml_retriever.cli delete-user --db ./aml.db --user user-1
python3 -m aml_retriever.cli purge --db ./aml.db --yes
```

For file-backed SQLite, remove the database together with its `-wal` and `-shm`
files when destroying an instance. See [docs/DATA_LIFECYCLE.md](docs/DATA_LIFECYCLE.md).

The repository contains synthetic fixtures and handwritten examples only; it
does not contain leaderboard evaluation data.

## Known limitations

- The default retrieval path is lexical and deterministic. Semantically distant
  paraphrases, especially temporal paraphrases, remain the main weakness.
- Guarded supersession is a ranking signal, not truth resolution. Conflicting
  and historical evidence remains available by design.
- Chinese tokenization uses character unigrams and bigrams rather than a
  language model or full morphological segmenter.
- SQLite is a single-node backend. WAL mode, retries, and a read pool handle the
  evaluated concurrency shape, but the implementation is not horizontally
  distributed.
- Synthetic evaluation supports controlled comparison; only an official run can
  establish an official score for a new version.

## Repository layout

```text
aml_retriever/
  api.py          Add/Search contract and memory service
  server.py       HTTP transport
  retriever.py    storage, candidate generation, scoring, and reranking
  views.py        message, window, and session-segment views
  features.py     deterministic feature extraction
  config.py       configuration and ablation flags
  store.py        lexical baseline retained for regression testing
  cli.py          service, data, and self-check commands
  evaluation/     synthetic datasets, metrics, and ablation harness
scripts/
  serve.sh        local service launcher
  run_tests.sh    complete local verification
  smoke_api.py    HTTP contract smoke suite
  run_eval.py     reproducible ablation runner
  run_scan.py     parameter scan runner
docs/
  API_CONTRACT.md
  EVAL.md
  DATA_LIFECYCLE.md
Dockerfile
config.example.json
```

## Project boundary

FlowGrid AML Retriever is an independent leaderboard implementation. It applies
FlowGrid's ideas of provenance, temporal state, conflict preservation, and user
isolation, but it is not the FlowGrid Core product and its leaderboard results
should not be interpreted as validation of FlowGrid Core.

## License

Licensed under the [MIT License](LICENSE).
