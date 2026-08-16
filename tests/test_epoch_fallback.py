#!/usr/bin/env python3
"""test_epoch_fallback.py — _temporal_epoch=None 时 _epoch_of 必须兜底 created_at。"""
import unittest

from aml_retriever.retriever import RetrieverDB


class TestEpochFallback(unittest.TestCase):

    def test_temporal_epoch_none_falls_back_to_created_at(self):
        rec = {"ts_ms": None, "created_at": "2023-05-25T13:14:00+00:00",
               "_temporal_epoch": None}
        epoch = RetrieverDB._epoch_of(rec)
        self.assertIsNotNone(epoch, "_temporal_epoch=None 时必须用 created_at 兜底")
        self.assertAlmostEqual(epoch, 1685020440.0, delta=1.0)

    def test_temporal_epoch_missing_falls_back(self):
        rec = {"ts_ms": None, "created_at": "2023-05-25T13:14:00+00:00"}
        self.assertIsNotNone(RetrieverDB._epoch_of(rec))

    def test_temporal_epoch_value_wins(self):
        rec = {"ts_ms": None, "created_at": "2023-05-25T13:14:00+00:00",
               "_temporal_epoch": 12345.0}
        self.assertEqual(RetrieverDB._epoch_of(rec), 12345.0)

    def test_event_date_prefix_view_record(self):
        rec = {"content": "x", "created_at": "2023-05-25T13:14:00+00:00",
               "abs_expression": None, "_temporal_epoch": None}
        self.assertEqual(RetrieverDB._event_date_prefix(rec), "2023-05-25")


if __name__ == "__main__":
    unittest.main()
