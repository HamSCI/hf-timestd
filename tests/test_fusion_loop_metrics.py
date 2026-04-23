#!/usr/bin/env python3
"""Tests for FusionLoopMetrics — the per-cycle instrumentation that
feeds the fusion parsimony/stability audit's measurement phase.

We exercise accumulation semantics (phase timing, event marking,
re-entrant phase names summing), the atomic JSON write, the
payload schema, and failure modes (writer path denied → log-and-
continue, no_phases case, watchdog clamp)."""

import json
import logging
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from hf_timestd.core.fusion_loop_metrics import (
    FusionLoopMetrics,
    SCHEMA_VERSION,
)


class TestFusionLoopMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "fusion_metrics.json"
        self.m = FusionLoopMetrics(watchdog_sec=120.0, path=self.path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self) -> dict:
        with self.path.open() as f:
            return json.load(f)

    def test_one_cycle_happy_path(self) -> None:
        self.m.start_cycle()
        with self.m.phase("fuse_l1"):
            time.sleep(0.005)
        with self.m.phase("fuse_l2"):
            time.sleep(0.005)
        self.m.mark_event("kalman_lock")
        payload = self.m.finalize_and_emit()

        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        self.assertEqual(payload["cycle_index"], 0)
        self.assertTrue(payload["first_cycle"])
        self.assertGreater(payload["loop_duration_sec"], 0.009)
        self.assertAlmostEqual(payload["watchdog_budget_sec"], 120.0)
        self.assertLess(payload["watchdog_consumed_pct"], 100.0)
        self.assertGreater(payload["rss_kb"], 0)
        self.assertEqual(set(payload["phases"].keys()), {"fuse_l1", "fuse_l2"})
        self.assertEqual(payload["events"], ["kalman_lock"])

        # Disk state matches returned payload.
        self.assertEqual(self._read()["cycle_index"], 0)

    def test_cycle_index_increments_and_first_cycle_flips(self) -> None:
        self.m.start_cycle()
        self.m.finalize_and_emit()
        self.m.start_cycle()
        payload = self.m.finalize_and_emit()
        self.assertEqual(payload["cycle_index"], 1)
        self.assertFalse(payload["first_cycle"])

    def test_phase_reentry_sums(self) -> None:
        self.m.start_cycle()
        with self.m.phase("calibration_apply"):
            time.sleep(0.002)
        with self.m.phase("calibration_apply"):
            time.sleep(0.002)
        payload = self.m.finalize_and_emit()
        self.assertGreater(payload["phases"]["calibration_apply"], 0.0039)

    def test_record_phase_manual(self) -> None:
        """Large try/except blocks use record_phase to avoid reindent."""
        self.m.start_cycle()
        t0 = time.monotonic()
        time.sleep(0.003)
        self.m.record_phase("shm_write", time.monotonic() - t0)
        payload = self.m.finalize_and_emit()
        self.assertGreater(payload["phases"]["shm_write"], 0.002)

    def test_record_and_context_phase_sum_under_same_name(self) -> None:
        """Mixing the two APIs for the same phase name is additive."""
        self.m.start_cycle()
        with self.m.phase("fusion_status_write"):
            time.sleep(0.002)
        self.m.record_phase("fusion_status_write", 0.010)
        payload = self.m.finalize_and_emit()
        self.assertGreater(payload["phases"]["fusion_status_write"], 0.011)

    def test_start_cycle_resets_phases_and_events(self) -> None:
        self.m.start_cycle()
        with self.m.phase("fuse_l1"):
            pass
        self.m.mark_event("kalman_lock")
        self.m.finalize_and_emit()

        self.m.start_cycle()
        payload = self.m.finalize_and_emit()
        self.assertEqual(payload["phases"], {})
        self.assertEqual(payload["events"], [])

    def test_watchdog_consumed_pct_calculation(self) -> None:
        # watchdog_sec=1.0 so even a tiny cycle shows >0%.
        m = FusionLoopMetrics(watchdog_sec=1.0, path=self.path)
        m.start_cycle()
        time.sleep(0.01)
        payload = m.finalize_and_emit()
        self.assertGreater(payload["watchdog_consumed_pct"], 0.5)
        self.assertLess(payload["watchdog_consumed_pct"], 100.0)

    def test_watchdog_sec_zero_is_safe(self) -> None:
        """Edge case: zero/negative watchdog shouldn't divide-by-zero."""
        m = FusionLoopMetrics(watchdog_sec=0.0, path=self.path)
        m.start_cycle()
        payload = m.finalize_and_emit()
        self.assertEqual(payload["watchdog_consumed_pct"], 0.0)

    def test_atomic_write_survives_concurrent_reader(self) -> None:
        """Two back-to-back cycles: reader between them sees valid JSON
        from one or the other, never a torn write."""
        self.m.start_cycle()
        self.m.finalize_and_emit()
        p1 = self._read()
        self.m.start_cycle()
        self.m.finalize_and_emit()
        p2 = self._read()
        self.assertEqual(p1["cycle_index"], 0)
        self.assertEqual(p2["cycle_index"], 1)

    def test_writer_path_denied_is_logged_not_raised(self) -> None:
        """If the atomic write can't happen, emit() logs and continues."""
        m = FusionLoopMetrics(
            watchdog_sec=120.0,
            path=Path("/nonexistent/dir/fusion_metrics.json"),
        )
        m.start_cycle()
        with self.assertLogs(
            "hf_timestd.core.fusion_loop_metrics", level=logging.WARNING,
        ):
            m.finalize_and_emit()

    def test_structured_log_line_contains_key_fields(self) -> None:
        self.m.start_cycle()
        with self.m.phase("fuse_l1"):
            pass
        self.m.mark_event("shm_reconnect_l1")
        with self.assertLogs(
            "hf_timestd.core.fusion_loop_metrics", level=logging.INFO,
        ) as cm:
            self.m.finalize_and_emit()
        line = " ".join(cm.output)
        self.assertIn("fusion_metrics", line)
        self.assertIn("cycle=0", line)
        self.assertIn("fuse_l1=", line)
        self.assertIn("shm_reconnect_l1", line)

    def test_empty_cycle_still_emits(self) -> None:
        """A cycle with zero phases and zero events — still valid output."""
        self.m.start_cycle()
        payload = self.m.finalize_and_emit()
        self.assertEqual(payload["phases"], {})
        self.assertEqual(payload["events"], [])
        self.assertGreaterEqual(payload["loop_duration_sec"], 0.0)


if __name__ == "__main__":
    unittest.main()
