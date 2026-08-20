"""Tests for the LLM fact extraction pipeline (scnet Kimi-K2.5).

Verifies:
  1. Flag is OFF by default — zero behavior change.
  2. Flag ON but SC_API_KEY missing — graceful fallback to lexical-only.
  3. Facts table + fts_facts schema created on init.
  4. FactExtractor stub returns [] (NotImplementedError caught internally).
  5. Manually inserted facts boost matching evidence at Search time.
  6. Fact-matched messages get "fact_match" evidence flag.
  7. Fact-matched messages missed by lexical search are surfaced as extra candidates.
  8. Non-fact-relevant queries do NOT trigger fact search.
  9. delete_user cleans up facts.
"""
from __future__ import annotations

import json
import os
import unittest

from aml_retriever.config import DEFAULT_FLAGS, RetrieverConfig
from aml_retriever.retriever import RetrieverDB
from aml_retriever.fact_extraction import (
    ExtractedFact,
    FactExtractionConfig,
    FactExtractor,
    build_fact_id,
)


# ---------------------------------------------------------------------------
# Config / flag tests
# ---------------------------------------------------------------------------

class TestFactExtractionFlag(unittest.TestCase):
    def test_flag_default_false(self):
        self.assertIn("fact_extraction", DEFAULT_FLAGS)
        self.assertFalse(DEFAULT_FLAGS["fact_extraction"])

    def test_flag_can_be_enabled(self):
        cfg = RetrieverConfig()
        cfg.flags["fact_extraction"] = True
        self.assertTrue(cfg.flags["fact_extraction"])

    def test_env_override(self):
        os.environ["AML_FLAG_FACT_EXTRACTION"] = "1"
        try:
            cfg = RetrieverConfig.from_env()
            self.assertTrue(cfg.flags["fact_extraction"])
        finally:
            del os.environ["AML_FLAG_FACT_EXTRACTION"]

    def test_config_params_exist(self):
        cfg = RetrieverConfig()
        self.assertEqual(cfg.fact_boost_weight, 25.0)
        self.assertEqual(cfg.fact_extraction_model, "kimi-k2.5")
        self.assertEqual(cfg.fact_extraction_timeout, 5.0)
        self.assertEqual(cfg.fact_max_per_message, 8)
        self.assertEqual(cfg.fact_extra_candidates, 10)


# ---------------------------------------------------------------------------
# FactExtractor unit tests
# ---------------------------------------------------------------------------

class TestFactExtractor(unittest.TestCase):
    def test_no_api_key_unavailable(self):
        ext = FactExtractor(FactExtractionConfig(api_key="", api_base="https://x"))
        ok, reason = ext.available()
        self.assertFalse(ok)
        self.assertIn("SC_API_KEY", reason)

    def test_no_api_key_returns_empty(self):
        ext = FactExtractor(FactExtractionConfig(api_key="", api_base="https://x"))
        facts = ext.extract_facts("Alice's budget is 5000.")
        self.assertEqual(facts, [])

    def test_stub_returns_empty(self):
        """The API call is a stub (NotImplementedError); extract must return []."""
        ext = FactExtractor(FactExtractionConfig(
            api_key="test-key", api_base="https://api.scnet.cn/v1"
        ))
        ok, _ = ext.available()
        self.assertTrue(ok)
        facts = ext.extract_facts("Alice's budget is 5000.")
        self.assertEqual(facts, [])

    def test_empty_content_returns_empty(self):
        ext = FactExtractor(FactExtractionConfig(api_key="k", api_base="https://x"))
        self.assertEqual(ext.extract_facts(""), [])
        self.assertEqual(ext.extract_facts("   "), [])

    def test_parse_facts_valid(self):
        ext = FactExtractor(FactExtractionConfig(api_key="k"))
        raw = {
            "choices": [{
                "message": {
                    "content": json.dumps({"facts": [
                        {"subject": "budget", "predicate": "is", "object": "5000", "time": "March 2024"},
                        {"subject": "Alice", "predicate": "prefers", "object": "Python", "time": None},
                    ]})
                }
            }]
        }
        facts = ext._parse_facts(raw, "test")
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts[0].subject, "budget")
        self.assertEqual(facts[0].predicate, "is")
        self.assertEqual(facts[0].object, "5000")
        self.assertEqual(facts[0].time_value, "March 2024")
        self.assertIsNotNone(facts[0].time_epoch)  # parsed from "March 2024"
        self.assertEqual(facts[1].time_value, "")  # null → empty

    def test_parse_facts_malformed(self):
        ext = FactExtractor(FactExtractionConfig(api_key="k"))
        self.assertEqual(ext._parse_facts({}, "x"), [])
        self.assertEqual(ext._parse_facts({"choices": []}, "x"), [])
        self.assertEqual(ext._parse_facts({"choices": [{"message": {}}]}, "x"), [])
        self.assertEqual(ext._parse_facts("not a dict", "x"), [])

    def test_parse_facts_no_facts_key(self):
        ext = FactExtractor(FactExtractionConfig(api_key="k"))
        raw = {"choices": [{"message": {"content": '{"result": "ok"}'}}]}
        self.assertEqual(ext._parse_facts(raw, "x"), [])

    def test_build_fact_id_deterministic(self):
        fact = ExtractedFact(subject="s", predicate="p", object="o")
        id1 = build_fact_id("msg1", fact)
        id2 = build_fact_id("msg1", fact)
        self.assertEqual(id1, id2)
        id3 = build_fact_id("msg2", fact)
        self.assertNotEqual(id1, id3)

    def test_fact_text(self):
        fact = ExtractedFact(subject="budget", predicate="is", object="5000", time_value="2024")
        self.assertEqual(fact.fact_text(), "budget is 5000 2024")
        fact2 = ExtractedFact(subject="s", predicate="p", object="o")
        self.assertEqual(fact2.fact_text(), "s p o")

    def test_from_config_uses_env(self):
        os.environ["SC_API_KEY"] = "env-key-123"
        os.environ["SC_API_BASE"] = "https://custom.example.com/v1"
        try:
            cfg = RetrieverConfig()
            ext = FactExtractor.from_config(cfg)
            self.assertEqual(ext.cfg.api_key, "env-key-123")
            self.assertEqual(ext.cfg.api_base, "https://custom.example.com/v1")
            self.assertEqual(ext.cfg.model, "kimi-k2.5")
        finally:
            del os.environ["SC_API_KEY"]
            del os.environ["SC_API_BASE"]


# ---------------------------------------------------------------------------
# RetrieverDB integration tests
# ---------------------------------------------------------------------------

class TestFactExtractionSchema(unittest.TestCase):
    def test_facts_table_created(self):
        db = RetrieverDB(RetrieverConfig(db_path=":memory:"))
        with db.connection() as con:
            # facts table exists
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='facts'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            # fts_facts virtual table exists
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_facts'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            # indexes exist
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_facts_subj'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
        db.close()


class TestFactExtractionFlagOff(unittest.TestCase):
    """When flag is OFF, fact extraction must not run and behavior is unchanged."""

    def setUp(self):
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["fact_extraction"] = False
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()

    def test_add_no_facts_extracted(self):
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "My budget is 5000."}],
        )
        with self.db.connection() as con:
            n = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(n, 0)

    def test_search_no_fact_match_flag(self):
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "My budget is 5000."}],
        )
        result = self.db.search(user_id="u1", query="what is my current budget?", top_k=10)
        for ev in result.results:
            self.assertNotIn("fact_match", ev.evidence_flags)


class TestFactExtractionGracefulFallback(unittest.TestCase):
    """Flag ON but SC_API_KEY missing — graceful fallback to lexical-only."""

    def setUp(self):
        # Ensure no SC_API_KEY in environment
        self._saved_key = os.environ.pop("SC_API_KEY", None)
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["fact_extraction"] = True
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()
        if self._saved_key is not None:
            os.environ["SC_API_KEY"] = self._saved_key

    def test_add_succeeds_without_api_key(self):
        result = self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "My budget is 5000."}],
        )
        self.assertTrue(result.message_ids)
        # No facts stored (API unavailable)
        with self.db.connection() as con:
            n = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertEqual(n, 0)

    def test_search_works_without_facts(self):
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "My budget is 5000."}],
        )
        result = self.db.search(user_id="u1", query="what is my current budget?", top_k=10)
        self.assertGreater(len(result.results), 0)
        # Lexical match still works
        self.assertIn("5000", result.results[0].content)


class TestFactBoost(unittest.TestCase):
    """Manually insert facts and verify search-time boosting."""

    def setUp(self):
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["fact_extraction"] = True
        self.db = RetrieverDB(self.cfg)
        # Add messages
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[
                {"role": "user", "content": "My budget is 5000 dollars.", "timestamp": 1700000000000},
                {"role": "assistant", "content": "The project uses Python 3.12.", "timestamp": 1700000001000},
                {"role": "user", "content": "Budget updated to 8000.", "timestamp": 1700000002000},
            ],
        )

    def tearDown(self):
        self.db.close()

    def _insert_fact(self, message_id, subject, predicate, obj, time_value="", time_epoch=None):
        """Manually insert a fact (simulating what the API would produce)."""
        fact = ExtractedFact(subject=subject, predicate=predicate, object=obj,
                             time_value=time_value, time_epoch=time_epoch)
        self.db._store_facts([(message_id, "u1", "s1", fact)])

    def _get_message_ids(self):
        with self.db.connection() as con:
            rows = con.execute(
                "SELECT id, content FROM messages WHERE user_id='u1' ORDER BY seq"
            ).fetchall()
        return [(r["id"], r["content"]) for r in rows]

    def test_fact_match_boosts_evidence(self):
        ids = self._get_message_ids()
        # Insert a fact for the "budget updated to 8000" message
        budget_msg_id = [mid for mid, content in ids if "8000" in content][0]
        self._insert_fact(budget_msg_id, "budget", "updated to", "8000")

        # Search for "what is my current budget" — fact-relevant query
        result = self.db.search(user_id="u1", query="what is my current budget?", top_k=10)
        self.assertGreater(len(result.results), 0)

        # The fact-matched message should be in results
        matched = [ev for ev in result.results if "fact_match" in ev.evidence_flags]
        self.assertGreater(len(matched), 0)
        self.assertEqual(matched[0].id, budget_msg_id)

    def test_fact_match_surfaces_missed_message(self):
        """A message not found by lexical search but matched by facts is surfaced."""
        ids = self._get_message_ids()
        # Insert a fact for the "Python 3.12" message using a synonym-like fact
        python_msg_id = [mid for mid, content in ids if "Python" in content][0]
        self._insert_fact(python_msg_id, "project", "uses", "Python 3.12")

        # Search with a query that won't lexically match "Python 3.12" message
        # but is fact-relevant (current_value intent)
        result = self.db.search(user_id="u1", query="what is my current project language?", top_k=10)
        # The Python message should appear (surfaced via fact match)
        python_ev = [ev for ev in result.results if ev.id == python_msg_id]
        self.assertGreater(len(python_ev), 0)
        self.assertIn("fact_match", python_ev[0].evidence_flags)

    def test_non_fact_relevant_query_no_fact_search(self):
        """Queries without temporal/update/numeric intent do NOT trigger fact search."""
        ids = self._get_message_ids()
        budget_msg_id = [mid for mid, content in ids if "8000" in content][0]
        self._insert_fact(budget_msg_id, "budget", "updated to", "8000")

        # "Hello world" has no temporal/update/numeric intent
        result = self.db.search(user_id="u1", query="hello world", top_k=10)
        for ev in result.results:
            # No fact_match flag because fact search was not triggered
            self.assertNotIn("fact_match", ev.evidence_flags)

    def test_fact_boost_in_stats(self):
        ids = self._get_message_ids()
        self._insert_fact(ids[0][0], "budget", "is", "5000")
        stats = self.db.stats()
        self.assertEqual(stats["facts"], 1)


class TestFactExtractionDeleteUser(unittest.TestCase):
    """delete_user must clean up facts."""

    def setUp(self):
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["fact_extraction"] = True
        self.db = RetrieverDB(self.cfg)
        self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "My budget is 5000."}],
        )
        ids = []
        with self.db.connection() as con:
            rows = con.execute("SELECT id FROM messages WHERE user_id='u1'").fetchall()
            ids = [r["id"] for r in rows]
        if ids:
            fact = ExtractedFact(subject="budget", predicate="is", object="5000")
            self.db._store_facts([(ids[0], "u1", "s1", fact)])

    def tearDown(self):
        self.db.close()

    def test_delete_user_removes_facts(self):
        with self.db.connection() as con:
            before = con.execute("SELECT COUNT(*) FROM facts WHERE user_id='u1'").fetchone()[0]
        self.assertGreater(before, 0)
        self.db.delete_user("u1")
        with self.db.connection() as con:
            after = con.execute("SELECT COUNT(*) FROM facts WHERE user_id='u1'").fetchone()[0]
            fts_after = con.execute("SELECT COUNT(*) FROM fts_facts WHERE user_id='u1'").fetchone()[0]
        self.assertEqual(after, 0)
        self.assertEqual(fts_after, 0)


class TestFactExtractionIdempotent(unittest.TestCase):
    """Idempotent Add (same request_id) should not re-extract or duplicate facts."""

    def setUp(self):
        self._saved_key = os.environ.pop("SC_API_KEY", None)
        self.cfg = RetrieverConfig(db_path=":memory:")
        self.cfg.flags["fact_extraction"] = True
        self.db = RetrieverDB(self.cfg)

    def tearDown(self):
        self.db.close()
        if self._saved_key is not None:
            os.environ["SC_API_KEY"] = self._saved_key

    def test_idempotent_add_no_error(self):
        """Second add with same request_id returns idempotent=True without error."""
        r1 = self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "Budget is 5000."}],
        )
        self.assertFalse(r1.idempotent)
        r2 = self.db.add(
            request_id="r1", user_id="u1", session_id="s1",
            messages=[{"role": "user", "content": "Budget is 5000."}],
        )
        self.assertTrue(r2.idempotent)


if __name__ == "__main__":
    unittest.main()
