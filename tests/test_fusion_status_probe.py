#!/usr/bin/env python3
"""Unit tests for FusionStatusProbe — the T3 probe that reads
/run/hf-timestd/fusion_status.json."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hf_timestd.core.fusion_status_probe import FusionStatusProbe


def _good_fusion(**overrides) -> dict:
    base = {
        "schema": "v1",
        "utc_published": "2026-04-23T12:00:00.000000Z",
        "cycle_interval_sec": 8.0,
        "fusion": {
            "available": True,
            "d_clock_fused_ms": 0.812,
            "uncertainty_ms": 0.94,
            "n_broadcasts": 24,
            "n_stations": 2,
            "stations_used": ["WWV", "CHU"],
            "single_station_mode": False,
            "kalman_state": "LOCKED",
            "quality_grade": "A",
            "consistency_flag": "OK",
            "calibration_applied": True,
        },
        "chrony_gate": {"last_fed": True, "skip_reasons": []},
    }
    base.update(overrides)
    return base


class TestFusionStatusProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "fusion_status.json"
        # Fix "now" at the moment the default good file was published.
        self.now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload: dict) -> None:
        with self.path.open("w") as f:
            json.dump(payload, f)

    def _probe(self, **kwargs) -> FusionStatusProbe:
        return FusionStatusProbe(
            status_path=self.path,
            now_fn=lambda: self.now,
            **kwargs,
        )

    # ----- happy path -----

    def test_good_fusion_reports_available(self) -> None:
        self._write(_good_fusion())
        r = self._probe().poll()
        self.assertTrue(r.available)
        self.assertAlmostEqual(r.offset_ms, 0.812)
        self.assertAlmostEqual(r.sigma_ms, 0.94)
        self.assertEqual(r.detail["stations_used"], ["WWV", "CHU"])
        self.assertEqual(r.detail["kalman_state"], "LOCKED")

    def test_acquiring_kalman_state_is_acceptable(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["kalman_state"] = "ACQUIRING"
        self._write(payload)
        r = self._probe().poll()
        self.assertTrue(r.available)

    # ----- failure modes -----

    def test_missing_file(self) -> None:
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("missing", r.reason or "")

    def test_corrupt_json(self) -> None:
        self.path.write_text("{not valid json")
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("read error", r.reason or "")

    def test_unknown_schema_rejected(self) -> None:
        self._write(_good_fusion(schema="v2"))
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("unsupported schema", r.reason or "")

    def test_stale_publication_rejected(self) -> None:
        # Good file, but now is 120 s later than publication.
        self.now = self.now + timedelta(seconds=120)
        self._write(_good_fusion())
        r = self._probe(freshness_sec=60).poll()
        self.assertFalse(r.available)
        self.assertIn("stale", r.reason or "")

    def test_fusion_unavailable_passes_reason_through(self) -> None:
        payload = _good_fusion()
        payload["fusion"] = {"available": False, "reason": "no fusion result this cycle"}
        self._write(payload)
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("no fusion result", r.reason or "")

    def test_single_station_rejected_by_default(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["n_stations"] = 1
        self._write(payload)
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("n_stations=1", r.reason or "")

    def test_single_station_allowed_when_min_configured_down(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["n_stations"] = 1
        self._write(payload)
        r = self._probe(min_stations=1).poll()
        self.assertTrue(r.available)

    def test_reacquiring_kalman_rejected(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["kalman_state"] = "REACQUIRING"
        self._write(payload)
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("kalman_state=REACQUIRING", r.reason or "")

    def test_missing_offset_fields_rejected(self) -> None:
        payload = _good_fusion()
        del payload["fusion"]["d_clock_fused_ms"]
        self._write(payload)
        r = self._probe().poll()
        self.assertFalse(r.available)
        self.assertIn("missing/invalid offset", r.reason or "")


if __name__ == "__main__":
    unittest.main()
