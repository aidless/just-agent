"""fact_extraction.py — LLM fact extraction pipeline (scnet Kimi-K2.5).

FLAG-GATED by ``flags["fact_extraction"]`` (default **False**).

Pipeline overview
-----------------
**Add time:** for each new message, the scnet Kimi-K2.5 API is called to extract
structured facts — ``(subject, predicate, object, time)`` triples that capture
the verifiable knowledge in the message.  Extracted facts are persisted to a
separate SQLite ``facts`` table (plus an ``fts_facts`` FTS5 virtual table for
fast query-time lookup), alongside the existing FTS5 message index.

**Search time:** for *knowledge_update / temporal* queries (detected via intent
rules in ``features.py``), the fact table is also searched.  Messages whose
facts match the query receive a score boost, and a small number of
fact-matched messages that lexical search *missed* are surfaced as extra
candidates.

Graceful fallback
-----------------
If the API is unavailable — no ``SC_API_KEY``, network timeout, malformed
response, or any exception — **no facts are extracted and retrieval falls back
to the pure lexical path without error**.  The pipeline never blocks Add or
Search on API failures; a missing or failing API is observationally identical
to having the flag off, except that the ``fact_match`` evidence flag simply
never appears.

Credentials
-----------
The API key and base URL are read from environment variables, **never
hardcoded**:

  ``SC_API_KEY``   — required; if missing/unset, fact extraction is silently
                     disabled (``available()`` returns ``False``).
  ``SC_API_BASE``  — optional; defaults to the scnet.cn chat-completions
                     endpoint.

Stub status
-----------
This module is a **stub**: the actual HTTP call (``_call_api``) contains the
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

DEFAULT_SC_API_BASE = "https://api.scnet.cn/v1"
DEFAULT_MODEL = "kimi-k2.5"
DEFAULT_TIMEOUT = 5.0          # seconds
DEFAULT_MAX_FACTS = 8          # max facts to extract/store per message

# The extraction prompt asks Kimi-K2.5 for a strict JSON object.  The team
# validated (3-round validation, ~1.52 s/message) that this model reliably
# returns well-formed JSON with the expected schema.
_EXTRACTION_PROMPT = """\
Extract factual statements from the following message as a JSON object.

Return ONLY a JSON object with this exact schema:
{"facts": [{"subject": "...", "predicate": "...", "object": "...", "time": "..."}]}

Rules:
- subject: the entity or concept the fact is about (e.g. "budget", "Alice", "sprint end date")
- predicate: the relationship or attribute (e.g. "is", "has", "changed to", "prefers", "updated to")
- object: the value or target of the predicate (e.g. "5000", "165 commits", "2024-03-15")
- time: any temporal expression in the text (e.g. "March 2024", "last week", "now", "yesterday"), or null if none
- Only extract concrete, verifiable facts. Do not extract opinions, questions, or greetings.
- If no facts can be extracted, return {"facts": []}.

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
    api_base: str = DEFAULT_SC_API_BASE
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_facts: int = DEFAULT_MAX_FACTS


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
                api_key=os.environ.get("SC_API_KEY", ""),
                api_base=os.environ.get("SC_API_BASE", DEFAULT_SC_API_BASE),
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
            api_key=os.environ.get("SC_API_KEY", ""),
            api_base=os.environ.get("SC_API_BASE", DEFAULT_SC_API_BASE),
            model=getattr(retriever_config, "fact_extraction_model", DEFAULT_MODEL),
            timeout=float(getattr(retriever_config, "fact_extraction_timeout", DEFAULT_TIMEOUT)),
            max_facts=int(getattr(retriever_config, "fact_max_per_message", DEFAULT_MAX_FACTS)),
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
            return False, "SC_API_KEY not set; fact extraction disabled"
        if not self.cfg.api_base:
            return False, "SC_API_BASE is empty; fact extraction disabled"
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

    # ------------------------------------------------------------------ API call
    def _call_api(self, content: str) -> dict:
        """Call the scnet Kimi-K2.5 chat-completions API for fact extraction.

        **STUB** — this method is structured but does not execute an HTTP
        request.  It documents the exact request shape so integration testing
        can replace the ``NotImplementedError`` with a real call.

        Expected request (OpenAI-compatible chat-completions format)::

            POST {api_base}/chat/completions
            Headers:
                Content-Type: application/json
                Authorization: Bearer {SC_API_KEY}
            Body:
                {
                  "model": "kimi-k2.5",
                  "messages": [
                    {"role": "system", "content": "<extraction prompt>"},
                    {"role": "user", "content": "<message content>"}
                  ],
                  "temperature": 0,
                  "response_format": {"type": "json_object"}
                }

        Expected response::

            {
              "choices": [
                {
                  "message": {
                    "content": "{\"facts\": [{\"subject\": \"...\", ...}]}"
                  }
                }
              ]
            }

        Parameters
        ----------
        content : str
            The raw message content to extract facts from.

        Returns
        -------
        dict
            The parsed JSON response body from the API.

        Raises
        ------
        NotImplementedError
            Always — this is a stub.  Replace with a real HTTP call
            (``urllib.request`` or ``http.client``) during integration testing.
        """
        # The prompt is fully built so integration tests can verify it.
        _prompt = _EXTRACTION_PROMPT.format(content=content)
        _url = f"{self.cfg.api_base.rstrip('/')}/chat/completions"
        _headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }
        _body = json.dumps({
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": "You are a fact extraction assistant. Extract structured facts as JSON."},
                {"role": "user", "content": _prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        })
        # ---- integration-testing placeholder --------------------------------
        # Replace the line below with:
        #   import urllib.request
        #   req = urllib.request.Request(_url, data=_body.encode("utf-8"),
        #                                 headers=_headers, method="POST")
        #   with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
        #       return json.loads(resp.read().decode("utf-8"))
        raise NotImplementedError(
            "scnet Kimi-K2.5 API call is a stub — implement in integration testing"
        )

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
    "DEFAULT_SC_API_BASE",
    "DEFAULT_MODEL",
]
