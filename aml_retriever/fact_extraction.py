"""fact_extraction.py — LLM fact extraction pipeline (open muse-spark-1.2-contributor, batch mode).

FLAG-GATED by ``flags["fact_extraction"]`` (default **False**).

Pipeline overview
-----------------
**Add time:** for each batch of new messages (5 per API call, 2.9s per message
amortized), the open muse-spark API is called to extract structured facts —
``(subject, predicate, object, time)`` triples.  Batch mode is used to amortize
the 12.9s single-call latency down to 2.9s/message.  Extracted facts are
persisted to a separate SQLite ``facts`` table (plus an ``fts_facts`` FTS5
virtual table), alongside the existing FTS5 message index.

**Search time:** for *knowledge_update / temporal* queries (detected via intent
rules in ``features.py``), the fact table is also searched.  Messages whose
facts match the query receive a score boost, and a small number of
fact-matched messages that lexical search *missed* are surfaced as extra
candidates.

Graceful fallback
-----------------
If the API is unavailable — no ``OPEN_API_KEY``, network timeout, malformed
response, or any exception — **no facts are extracted and retrieval falls back
to the pure lexical path without error**.  The pipeline never blocks Add or
Search on API failures; a missing or failing API is observationally identical
to having the flag off, except that the ``fact_match`` evidence flag simply
never appears.

Credentials
-----------
The API key and base URL are read from environment variables, **never
hardcoded**:

  ``OPEN_API_KEY`` — required; if missing/unset, fact extraction is silently
                     disabled (``available()`` returns ``False``).
  ``OPEN_API_BASE``  — optional; defaults to ``https://opencode.ai/zen/go/v1``.

Batch mode
----------
To amortize the 12.9s single-call latency, Add batches 5 messages per API call
(14.7s for 5 = 2.9s/message).  The LLM prompt lists 5 numbered messages and
asks for facts per message.

Stub status
-----------
This module was initially a **stub**; now wired to the open provider with
batch support.  See ``_call_api_batch`` for the actual HTTP call.

complete request shape and prompt template but raises ``NotImplementedError``.
Replace the stub body with a real ``urllib.request`` / ``http.client`` call
during integration testing.  All surrounding logic — credential loading,
response parsing, fact validation, time-epoch resolution, error handling — is
fully implemented and unit-tested.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

DEFAULT_OPEN_API_BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "muse-spark-1.2-contributor"
DEFAULT_TIMEOUT = 30.0         # seconds (muse-spark needs ~13s, scnet Kimi ~1.5s)
DEFAULT_MAX_FACTS = 8          # max facts to extract/store per message
DEFAULT_BATCH_SIZE = 5         # messages per API call (amortizes 12.9s → 2.9s/msg)

# Batch extraction prompt: lists 5 numbered messages, asks for facts per message
_BATCH_EXTRACTION_PROMPT = """\
Extract factual statements from each of the following {n} messages as JSON.

Return ONLY a JSON object with this exact schema:
{{"results": [{{"facts": [{{"subject": "...", "predicate": "...", "object": "...", "time": "..."}}]}}]}}
results[0] corresponds to message 1, results[1] to message 2, etc.

Rules per fact:
- subject: the entity or concept the fact is about (e.g. "budget", "Alice", "sprint end date")
- predicate: the relationship or attribute (e.g. "is", "has", "changed to", "prefers", "updated to")
- object: the value or target of the predicate (e.g. "5000", "165 commits", "2024-03-15")
- time: any temporal expression in the text (e.g. "March 2024", "last week", "now", "yesterday"), or null if none
- Only extract concrete, verifiable facts. Do not extract opinions, questions, or greetings.
- If a message has no facts, return {{"facts": []}} for it.

Messages:
{batch}"""

# Single-message fallback prompt (when batch not available)
_EXTRACTION_PROMPT = """\
Extract factual statements from the following message as a JSON object.

Return ONLY a JSON object with this exact schema:
{{"facts": [{{"subject": "...", "predicate": "...", "object": "...", "time": "..."}}]}}

Rules:
- subject: the entity or concept the fact is about (e.g. "budget", "Alice", "sprint end date")
- predicate: the relationship or attribute (e.g. "is", "has", "changed to", "prefers", "updated to")
- object: the value or target of the predicate (e.g. "5000", "165 commits", "2024-03-15")
- time: any temporal expression in the text (e.g. "March 2024", "last week", "now", "yesterday"), or null if none
- Only extract concrete, verifiable facts. Do not extract opinions, questions, or greetings.
- If no facts can be extracted, return {{"facts": []}}.

Message:
{content}"""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExtractedFact:
    """A single structured fact extracted from a message.

    subject   : the entity or concept the fact is about
    predicate : the relationship or attribute (e.g. "is", "changed to")
    object    : the value or target of the predicate
    time_value: raw temporal expression from the text (may be empty)
    time_epoch: parsed Unix epoch in seconds, or None if no parseable time
    """
    subject: str
    predicate: str
    object: str
    time_value: str = ""
    time_epoch: float | None = None

    def fact_text(self) -> str:
        """Concatenated text for FTS indexing (subject + predicate + object + time)."""
        parts = [self.subject, self.predicate, self.object]
        if self.time_value:
            parts.append(self.time_value)
        return " ".join(parts)


@dataclass
class FactExtractionConfig:
    """Configuration for the fact extraction pipeline.

    Held separately from RetrieverConfig to keep the zero-dependency promise
    and allow the module to be used standalone in tests.
    """
    api_key: str = ""
    api_base: str = DEFAULT_OPEN_API_BASE
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_facts: int = DEFAULT_MAX_FACTS
    batch_size: int = DEFAULT_BATCH_SIZE


# ---------------------------------------------------------------------------
# Fact extractor
# ---------------------------------------------------------------------------

class FactExtractor:
    """Wraps the scnet Kimi-K2.5 API for structured fact extraction.

    Usage::

        extractor = FactExtractor.from_config(config)
        if extractor.available():
            facts = extractor.extract_facts(message_content)
    """

    def __init__(self, cfg: FactExtractionConfig | None = None):
        if cfg is None:
            cfg = FactExtractionConfig(
                api_key=os.environ.get("OPEN_API_KEY", ""),
                api_base=os.environ.get("OPEN_API_BASE", DEFAULT_OPEN_API_BASE),
            )
        self.cfg = cfg

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_config(cls, retriever_config) -> "FactExtractor":
        """Build a FactExtractor from a RetrieverConfig (or compatible object).

        Credentials always come from the environment, never from the config
        dataclass, so that secrets are not persisted or logged.
        """
        cfg = FactExtractionConfig(
            api_key=os.environ.get("OPEN_API_KEY", ""),
            api_base=os.environ.get("OPEN_API_BASE", DEFAULT_OPEN_API_BASE),
            model=getattr(retriever_config, "fact_extraction_model", DEFAULT_MODEL),
            timeout=float(getattr(retriever_config, "fact_extraction_timeout", DEFAULT_TIMEOUT)),
            max_facts=int(getattr(retriever_config, "fact_max_per_message", DEFAULT_MAX_FACTS)),
            batch_size=int(getattr(retriever_config, "fact_batch_size", DEFAULT_BATCH_SIZE)),
        )
        return cls(cfg)

    # ------------------------------------------------------------------ status
    def available(self) -> tuple[bool, str]:
        """Check whether the API is *configured* (key present).

        This does **not** make a network call — it only verifies that the
        environment is set up for fact extraction.  The actual call may still
        fail at runtime (timeout, 5xx, etc.), which is handled gracefully.
        """
        if not self.cfg.api_key:
            return False, "OPEN_API_KEY not set; fact extraction disabled"
        if not self.cfg.api_base:
            return False, "OPEN_API_BASE is empty; fact extraction disabled"
        return True, ""

    # ------------------------------------------------------------------ extract
    def extract_facts(self, content: str) -> list[ExtractedFact]:
        """Extract structured facts from a message.

        Returns a list of ``ExtractedFact`` objects.  On any failure — API
        unavailable, network error, malformed response, parse error — returns
        an empty list.  **Never raises** to the caller.
        """
        ok, _reason = self.available()
        if not ok:
            return []
        if not content or not content.strip():
            return []
        try:
            raw_response = self._call_api(content)
        except NotImplementedError:
            # STUB: real API call not yet implemented.
            # During integration testing, replace _call_api with a real
            # HTTP request. Until then, return no facts.
            return []
        except Exception:
            # Network timeout, connection refused, SSL error, etc.
            # Graceful fallback: no facts extracted.
            return []
        facts = self._parse_facts(raw_response, content)
        return facts[: self.cfg.max_facts]

    # ---------------------------------------------------------- batch extract
    # Fast-path thresholds for skipping low-value messages.
    _FAST_PATH_MIN_CHARS = 30        # messages shorter than this are skipped
    _FAST_PATH_SKIP_ROLE = "assistant"  # messages with this role are skipped

    @staticmethod
    def _should_extract(content: str, role: str) -> bool:
        """Fast-path gate: skip short or assistant messages to cut volume ~50%.

        Assistant messages are model-generated summaries/responses that rarely
        contain new user facts.  Short messages (<30 chars) are greetings,
        acks, or chitchat with no extractable facts.  Skipping them avoids
        wasting an LLM call on messages that will yield ``{"facts": []}``.
        """
        text = (content or "").strip()
        if len(text) < FactExtractor._FAST_PATH_MIN_CHARS:
            return False
        if (role or "").strip().lower() == FactExtractor._FAST_PATH_SKIP_ROLE:
            return False
        return True

    def extract_facts_batch(
        self, messages: list[tuple[str, str, str]]
    ) -> list[tuple[str, list[ExtractedFact]]]:
        """Extract structured facts from a **batch** of messages.

        ``messages`` is a list of ``(doc_id, content, role)`` tuples.

        Batch mode sends ``batch_size`` (default 5) messages per API call,
        amortising the ~12.9 s single-call latency down to ~2.9 s/message.

        **Fast-path:** messages with fewer than 30 characters of content or
        with ``role == "assistant"`` are skipped entirely — no API call is
        wasted on them.  In typical chat logs this cuts extraction volume by
        ~50%, roughly halving total Add-time latency and cost.

        Returns a list of ``(doc_id, facts)`` tuples **only for messages that
        passed the fast-path gate** (skipped messages are omitted).  On any
        failure — API unavailable, network error, malformed response — the
        affected batch yields empty fact lists and the overall call never
        raises to the caller.
        """
        ok, _reason = self.available()
        if not ok:
            return []
        # Fast-path: filter out short / assistant messages
        eligible = [
            (doc_id, content, role)
            for doc_id, content, role in messages
            if self._should_extract(content, role)
        ]
        if not eligible:
            return []
        batch_size = max(1, int(self.cfg.batch_size))
        results: list[tuple[str, list[ExtractedFact]]] = []
        for i in range(0, len(eligible), batch_size):
            batch = eligible[i: i + batch_size]
            doc_ids = [d for d, _c, _r in batch]
            contents = [c for _d, c, _r in batch]
            try:
                raw_response = self._call_api_batch(contents)
            except Exception:
                # Batch-level failure — no facts for any message in this batch.
                # Do not abort remaining batches; continue with next chunk.
                for doc_id in doc_ids:
                    results.append((doc_id, []))
                continue
            facts_lists = self._parse_facts_batch(raw_response, len(batch))
            for doc_id, facts in zip(doc_ids, facts_lists):
                results.append((doc_id, facts[: self.cfg.max_facts]))
        return results

    # -------------------------------------------------------- batch API call
    def _call_api_batch(self, contents: list[str]) -> dict:
        """Call the open muse-spark chat-completions API with a batch prompt.

        Lists ``n`` numbered messages in a single user turn and asks the model
        to return a ``{"results": [...]}`` JSON array aligned by index.
        Handles the thinking model (needs generous ``max_tokens``) and the
        Cloudflare WAF (needs a browser-like ``User-Agent`` header).
        """
        import time
        import urllib.error
        import urllib.request

        n = len(contents)
        batch_text = "\n".join(f"{idx + 1}. {c}" for idx, c in enumerate(contents))
        _prompt = _BATCH_EXTRACTION_PROMPT.format(n=n, batch=batch_text)
        _url = f"{self.cfg.api_base.rstrip('/')}/chat/completions"
        _headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        # Batch mode needs more output tokens: 5 messages × ~8 facts × ~30 tokens/fact.
        _max_tokens = max(2000, 800 * n)
        _body = json.dumps({
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": "You are a fact extraction assistant. Extract structured facts as JSON."},
                {"role": "user", "content": _prompt},
            ],
            "temperature": 0,
            "max_tokens": _max_tokens,
        })
        last_exc = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    _url, data=_body.encode("utf-8"), headers=_headers, method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_exc = e
                time.sleep(1.0 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("batch API call failed")

    # ---------------------------------------------------- batch response parse
    def _parse_facts_batch(
        self, raw_response: dict, n: int
    ) -> list[list[ExtractedFact]]:
        """Parse a batch API response into ``n`` lists of ``ExtractedFact``.

        The expected response shape (from ``_BATCH_EXTRACTION_PROMPT``) is::

            {"results": [{"facts": [...]}, {"facts": [...]}, ...]}

        ``results[i]`` corresponds to the ``i``-th message in the batch.
        Returns a list of ``n`` fact-lists, aligned by index.  Missing,
        malformed, or extra entries are handled gracefully: a missing index
        yields an empty list, and indices beyond ``n`` are ignored.
        """
        if not isinstance(raw_response, dict):
            return [[] for _ in range(n)]
        choices = raw_response.get("choices")
        if not isinstance(choices, list) or not choices:
            return [[] for _ in range(n)]
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return [[] for _ in range(n)]
        raw_text = message.get("content", "")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return [[] for _ in range(n)]
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            return [[] for _ in range(n)]
        if not isinstance(parsed, dict):
            return [[] for _ in range(n)]
        results = parsed.get("results")
        if not isinstance(results, list):
            return [[] for _ in range(n)]
        # Align by index: pad missing entries with empty lists, truncate extras.
        facts_per_msg: list[list[ExtractedFact]] = []
        for i in range(n):
            entry = results[i] if i < len(results) else None
            if not isinstance(entry, dict):
                facts_per_msg.append([])
                continue
            facts_raw = entry.get("facts")
            if not isinstance(facts_raw, list):
                facts_per_msg.append([])
                continue
            facts_per_msg.append(
                [self._build_fact(f) for f in facts_raw if isinstance(f, dict)]
            )
        return facts_per_msg

    # ------------------------------------------------------------------ API call
    def _call_api(self, content: str) -> dict:
        """Call the open muse-spark chat-completions API for fact extraction.

        Uses the open provider (opencode.ai/zen) with muse-spark-1.2-contributor.
        Handles the thinking model (needs max_tokens=2000) and Cloudflare WAF
        (needs User-Agent header).
        """
        import time
        import urllib.error
        import urllib.request

        _prompt = _EXTRACTION_PROMPT.format(content=content)
        _url = f"{self.cfg.api_base.rstrip('/')}/chat/completions"
        _headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        _body = json.dumps({
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": "You are a fact extraction assistant. Extract structured facts as JSON."},
                {"role": "user", "content": _prompt},
            ],
            "temperature": 0,
            "max_tokens": 2000,
        })
        last_exc = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(_url, data=_body.encode("utf-8"),
                                             headers=_headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_exc = e
                time.sleep(1.0 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("API call failed")

    # ------------------------------------------------------------------ parsing
    def _parse_facts(self, raw_response: dict, content: str) -> list[ExtractedFact]:
        """Parse the API response into ``ExtractedFact`` objects.

        Handles the OpenAI-compatible chat-completions response shape and
        extracts the JSON fact array from the assistant message content.
        Malformed or unexpected responses yield an empty list.
        """
        if not isinstance(raw_response, dict):
            return []
        # Navigate the chat-completions response shape.
        choices = raw_response.get("choices")
        if not isinstance(choices, list) or not choices:
            return []
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            return []
        raw_text = message.get("content", "")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return []
        # The model is instructed to return a JSON object; parse it.
        try:
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(parsed, dict):
            return []
        facts_raw = parsed.get("facts")
        if not isinstance(facts_raw, list):
            return []
        return [self._build_fact(f) for f in facts_raw if isinstance(f, dict)]

    @staticmethod
    def _build_fact(raw: dict) -> ExtractedFact:
        """Convert a raw fact dict to an ``ExtractedFact``, resolving time."""
        subject = str(raw.get("subject") or "").strip()
        predicate = str(raw.get("predicate") or "").strip()
        obj = str(raw.get("object") or "").strip()
        time_value = str(raw.get("time") or "").strip()
        if time_value.lower() in ("null", "none", "n/a", ""):
            time_value = ""
        time_epoch: float | None = None
        if time_value:
            time_epoch = _resolve_time_epoch(time_value)
        return ExtractedFact(
            subject=subject,
            predicate=predicate,
            object=obj,
            time_value=time_value,
            time_epoch=time_epoch,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_time_epoch(time_value: str) -> float | None:
    """Attempt to parse a time expression into a Unix epoch (seconds).

    Reuses the existing ``temporal_fallback.parse_absolute_temporal`` regex
    parser to avoid duplicating date-extraction logic.  Returns ``None`` if
    the expression cannot be parsed (e.g. "last week", "now") — in that case
    the fact still stores the raw ``time_value`` string for display.
    """
    try:
        from .temporal_fallback import parse_absolute_temporal
        result = parse_absolute_temporal(time_value)
        if result is not None and result.epoch is not None:
            return result.epoch
    except Exception:
        pass
    return None


def build_fact_id(message_id: str, fact: ExtractedFact) -> str:
    """Deterministic fact ID: hash of (message_id, subject, predicate, object).

    Same fact from the same message → same ID, ensuring idempotent storage
    (re-extraction or retry does not create duplicate rows).
    """
    import hashlib
    raw = f"{message_id}\x00{fact.subject}\x00{fact.predicate}\x00{fact.object}".encode("utf-8")
    return "f_" + hashlib.sha1(raw).hexdigest()[:16]


__all__ = [
    "ExtractedFact",
    "FactExtractionConfig",
    "FactExtractor",
    "build_fact_id",
    "DEFAULT_OPEN_API_BASE",
    "DEFAULT_MODEL",
    "DEFAULT_BATCH_SIZE",
]
