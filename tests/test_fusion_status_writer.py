#!/usr/bin/env python3
"""Unit tests for FusionStatusWriter (METROLOGY.md §4.5 contract)."""

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hf_timestd.core.fusion_status_writer import (
    SCHEMA_VERSION,
    FusionStatusWriter,
)


@dataclass
class _StubResult:
    """Minimal stand-in for FusedResult with only the fields the writer reads."""
    d_clock_fused_ms: float = 0.812
    uncertainty_ms: float = 0.94
    n_broadcasts: int = 24
    n_stations: int = 2
    wwv_count: int = 12
    wwvh_count: int = 12
    bpm_count: int = 0
    single_station_mode: bool = False
    kalman_state: Optional[str] = "LOCKED"
    quality_grade: str = "A"
    consistency_flag: str = "OK"
    calibration_applied: bool = True
    d_clock_uncalibrated_ms: Optional[float] = 4.2
    uncalibrated_uncertainty_ms: Optional[float] = 1.3


class TestFusionStatusWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "fusion_status.json"
        self.writer = FusionStatusWriter(self.path, cycle_interval_sec=8.0)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self) -> dict:
        with self.path.open() as f:
            return json.load(f)

    def test_schema_version_and_core_envelope(self) -> None:
        self.writer.update(result=_StubResult(), chrony_fed=True, skip_reasons=[])
        payload = self._read()
        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        self.assertEqual(payload["cycle_interval_sec"], 8.0)
        self.assertTrue(payload["utc_published"].endswith("Z"))
        # Should parse as a datetime-like iso8601 string
        self.assertRegex(
            payload["utc_published"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
        )

    def test_fusion_block_when_result_present(self) -> None:
        self.writer.update(result=_StubResult(), chrony_fed=True, skip_reasons=[])
        fusion = self._read()["fusion"]
        self.assertTrue(fusion["available"])
        self.assertAlmostEqual(fusion["d_clock_fused_ms"], 0.812)
        self.assertAlmostEqual(fusion["uncertainty_ms"], 0.94)
        self.assertEqual(fusion["n_broadcasts"], 24)
        self.assertEqual(fusion["n_stations"], 2)
        # Only stations with count > 0 should be listed, in fixed order
        self.assertEqual(fusion["stations_used"], ["WWV", "WWVH"])
        self.assertEqual(fusion["kalman_state"], "LOCKED")
        self.assertEqual(fusion["quality_grade"], "A")

    def test_fused_value_is_labelled_calibrated_with_raw_beside_it(self) -> None:
        """Step 2 of the ruled order (review section 15): the fused value
        says plainly that it is calibrated, and the value before the
        calibration offsets rides beside it with its own sigma."""
        self.writer.update(result=_StubResult(), chrony_fed=False, skip_reasons=[])
        fusion = self._read()["fusion"]
        self.assertIs(fusion["d_clock_fused_calibrated"], True)
        self.assertIn("calibrated", fusion["d_clock_fused_note"])
        self.assertIn("not evidence about the clock", fusion["d_clock_fused_note"])
        self.assertAlmostEqual(fusion["d_clock_uncalibrated_ms"], 4.2)
        self.assertAlmostEqual(fusion["uncalibrated_uncertainty_ms"], 1.3)
        # The calibrated value and its sigma stay where they were.
        self.assertAlmostEqual(fusion["d_clock_fused_ms"], 0.812)
        self.assertAlmostEqual(fusion["uncertainty_ms"], 0.94)

    def test_missing_raw_value_publishes_null_not_zero(self) -> None:
        """A result without the uncalibrated value must not read as a
        clock that runs right."""
        result = _StubResult(d_clock_uncalibrated_ms=None,
                             uncalibrated_uncertainty_ms=None)
        self.writer.update(result=result, chrony_fed=False, skip_reasons=[])
        fusion = self._read()["fusion"]
        self.assertIsNone(fusion["d_clock_uncalibrated_ms"])
        self.assertIsNone(fusion["uncalibrated_uncertainty_ms"])
        self.assertIs(fusion["d_clock_fused_calibrated"], True)

    def test_stations_used_order_and_filtering(self) -> None:
        result = _StubResult(
            wwv_count=0, wwvh_count=5, bpm_count=1,
            n_stations=2, n_broadcasts=6,
        )
        self.writer.update(result=result, chrony_fed=True, skip_reasons=[])
        self.assertEqual(
            self._read()["fusion"]["stations_used"],
            ["WWVH", "BPM"],
        )

    def test_result_none_marks_fusion_unavailable(self) -> None:
        self.writer.update(result=None, chrony_fed=False, skip_reasons=[])
        payload = self._read()
        self.assertFalse(payload["fusion"]["available"])
        self.assertIn("reason", payload["fusion"])
        # utc_published must still be fresh — the writer being alive is itself
        # the signal that hf-timestd is running even when fusion isn't.
        self.assertIn("utc_published", payload)

    def test_chrony_gate_skip_reasons_passed_through(self) -> None:
        self.writer.update(
            result=_StubResult(),
            chrony_fed=False,
            skip_reasons=["quality(grade=D,unc=12ms)", "discontinuity"],
        )
        gate = self._read()["chrony_gate"]
        self.assertFalse(gate["last_fed"])
        self.assertEqual(
            gate["skip_reasons"],
            ["quality(grade=D,unc=12ms)", "discontinuity"],
        )

    def test_unknown_kalman_state_falls_back_to_literal(self) -> None:
        result = _StubResult(kalman_state=None)
        self.writer.update(result=result, chrony_fed=True, skip_reasons=[])
        self.assertEqual(self._read()["fusion"]["kalman_state"], "UNKNOWN")

    def test_atomic_write_leaves_no_temp_files_on_success(self) -> None:
        self.writer.update(result=_StubResult(), chrony_fed=True, skip_reasons=[])
        leftovers = [p for p in self.tmp.iterdir() if p.name != "fusion_status.json"]
        self.assertEqual(leftovers, [], f"unexpected temp files: {leftovers}")

    def test_repeated_updates_replace_not_append(self) -> None:
        self.writer.update(result=_StubResult(), chrony_fed=True, skip_reasons=[])
        first = self._read()
        # Second call with different state
        result2 = _StubResult(quality_grade="C", uncertainty_ms=1.8)
        self.writer.update(result=result2, chrony_fed=False, skip_reasons=["quality"])
        second = self._read()
        self.assertEqual(second["fusion"]["quality_grade"], "C")
        self.assertAlmostEqual(second["fusion"]["uncertainty_ms"], 1.8)
        self.assertFalse(second["chrony_gate"]["last_fed"])
        # utc_published must advance monotonically between cycles
        self.assertGreaterEqual(second["utc_published"], first["utc_published"])


if __name__ == "__main__":
    unittest.main()


class ReadFeedRegimeRobustnessTests(unittest.TestCase):
    """``read_feed_regime`` answers None, never raises, for a missing,
    unparsable, pre-regime or foreign-schema status file.  (Moved from the
    retired gate's test module on 2026-09-11; the reader outlived the gate.)"""

    def test_unreadable_status_reads_as_no_regime(self):
        import json, tempfile
        from pathlib import Path
        from hf_timestd.core.fusion_status_writer import read_feed_regime
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.assertIsNone(read_feed_regime(tmp / "missing.json"))
            bad = tmp / "bad.json"; bad.write_text("{not json")
            self.assertIsNone(read_feed_regime(bad))
            old = tmp / "old.json"
            old.write_text(json.dumps({"schema": "v1", "chrony_gate": {"last_fed": True}}))
            self.assertIsNone(read_feed_regime(old))
            wrong = tmp / "wrong.json"
            wrong.write_text(json.dumps({"schema": "v99", "chrony_gate": {"feed_regime": "anchor"}}))
            self.assertIsNone(read_feed_regime(wrong))
