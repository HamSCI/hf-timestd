"""Tests for the gap_samples per-minute attribution.

Recorder writes one ``gap_samples`` value covering the whole chunk
file.  Reader returns the chunk's full metadata with each minute
slice.  Before this fix, ``decimation_pipeline.py`` treated the
chunk-wide value as the gap for *each* minute it processed from
that chunk, inflating ``total_gap_samples`` and skewing
``completeness_pct`` by up to chunk_minutes×.

The fix divides the chunk-wide value by the chunk's minute count so
aggregates remain exact.  These tests pin down both the per-minute
math and the aggregate-correctness invariant.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from hf_timestd.grape.decimation_pipeline import _per_minute_gap  # noqa: E402


class PerMinuteGapTests(unittest.TestCase):
    def test_legacy_60s_chunk_passes_through(self):
        # A 60-second chunk = 1 minute, so chunk-wide and per-minute
        # are the same.  Returning anything else would break
        # legacy 1-minute archives.
        self.assertEqual(
            _per_minute_gap({"gap_samples": 12000, "file_duration_sec": 60}),
            12000,
        )

    def test_600s_chunk_divides_by_ten(self):
        # The production chunk shape — 10 minutes per file.
        # 12000 chunk-wide → 1200 per minute → 10×1200 = 12000 sum.
        self.assertEqual(
            _per_minute_gap({"gap_samples": 12000, "file_duration_sec": 600}),
            1200,
        )

    def test_1200s_chunk_divides_by_twenty(self):
        # A future / non-standard duration.  Old "guess from
        # (600,300,900,3600)" code wouldn't even find this chunk;
        # this test just confirms the gap math also handles it.
        self.assertEqual(
            _per_minute_gap({"gap_samples": 12000, "file_duration_sec": 1200}),
            600,
        )

    def test_missing_file_duration_assumed_legacy_60s(self):
        # Sidecars from older recorders may not carry
        # file_duration_sec.  Assume 60s (one-minute chunk).
        self.assertEqual(
            _per_minute_gap({"gap_samples": 12000}),
            12000,
        )

    def test_zero_gap_returns_zero(self):
        self.assertEqual(
            _per_minute_gap({"gap_samples": 0, "file_duration_sec": 600}),
            0,
        )

    def test_negative_or_garbage_returns_zero(self):
        # Don't propagate corrupt sidecar values into the decimator;
        # treat as no recorded gap.
        self.assertEqual(
            _per_minute_gap({"gap_samples": -1, "file_duration_sec": 600}),
            0,
        )

    def test_no_metadata_returns_zero(self):
        self.assertEqual(_per_minute_gap(None), 0)
        self.assertEqual(_per_minute_gap({}), 0)

    def test_short_chunk_below_one_minute_treated_as_minute(self):
        # file_duration_sec=30 / 60 == 0; max(1, 0) keeps us safe.
        self.assertEqual(
            _per_minute_gap({"gap_samples": 1000, "file_duration_sec": 30}),
            1000,
        )


class AggregateInvariantTests(unittest.TestCase):
    """The whole point of the fix: per-minute values, summed across the
    chunk's minutes, must equal the chunk-wide value (modulo integer
    division — at most chunk_minutes-1 raw samples lost across the
    chunk, vanishingly small compared to the 12k+ samples a 0.5s gap
    represents at 24 kHz)."""

    def _aggregate(self, chunk_gap: int, dur_sec: int) -> int:
        meta = {"gap_samples": chunk_gap, "file_duration_sec": dur_sec}
        per_min = _per_minute_gap(meta)
        n_minutes = max(1, dur_sec // 60)
        return per_min * n_minutes

    def test_aggregate_matches_within_rounding_for_600s(self):
        for chunk_gap in (0, 1200, 12000, 720_000, 1_440_000):
            with self.subTest(chunk_gap=chunk_gap):
                agg = self._aggregate(chunk_gap, 600)
                self.assertLessEqual(chunk_gap - agg, 9)  # at most n_min-1
                self.assertGreaterEqual(agg, 0)

    def test_aggregate_matches_within_rounding_for_1200s(self):
        for chunk_gap in (0, 24000, 240000, 2_880_000):
            with self.subTest(chunk_gap=chunk_gap):
                agg = self._aggregate(chunk_gap, 1200)
                self.assertLessEqual(chunk_gap - agg, 19)
                self.assertGreaterEqual(agg, 0)

    def test_old_behavior_inflated_by_chunk_minutes(self):
        # Document what the bug looked like, so a regression jumps out.
        # Before: each of 10 minutes reported the chunk-wide value.
        # After:  each of 10 minutes reports chunk_wide / 10.
        chunk_gap = 12000
        meta = {"gap_samples": chunk_gap, "file_duration_sec": 600}
        old_per_minute = chunk_gap                       # the bug
        new_per_minute = _per_minute_gap(meta)           # the fix
        old_sum_over_chunk = old_per_minute * 10
        new_sum_over_chunk = new_per_minute * 10
        self.assertEqual(old_sum_over_chunk, 10 * chunk_gap)
        self.assertEqual(new_sum_over_chunk, chunk_gap)


if __name__ == "__main__":
    unittest.main()
