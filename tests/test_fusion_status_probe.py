#!/usr/bin/env python3
"""Unit tests for FusionStatusProbe — the T3 probe that reads
/run/hf-timestd/fusion_status.json.

Since 2026-10-22 (review-fusion-kalman-20261022.md section 15, step 5)
the probe always reports T3 UNAVAILABLE, with one constant reason: the
fused D_clock is calibrated, zeroed against the host, and so says
nothing about the clock.  It still reads and validates the file, and
reports what it found in ``detail["fusion_status"]`` ("ok" or the old
refusal), and the offset the file would give in the ruled convention
(rtp_to_utc = -d_clock).  A constant reason means the authority manager's
demotion line goes out once, not on every change of fusion's state."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hf_timestd.core.fusion_status_probe import (
    T3_CALIBRATED_REASON,
    FusionStatusProbe,
)


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

    def _refused(self, r, status_substring: str) -> None:
        """Unavailable, with the ruling's reason, and the file's own
        state carried in detail."""
        self.assertFalse(r.available)
        self.assertEqual(r.reason, T3_CALIBRATED_REASON)
        self.assertIn(status_substring, r.detail.get("fusion_status", ""))
        self.assertFalse(r.witness_only)

    def _probe(self, **kwargs) -> FusionStatusProbe:
        return FusionStatusProbe(
            status_path=self.path,
            now_fn=lambda: self.now,
            **kwargs,
        )

    # ----- happy path -----

    def test_good_fusion_is_unavailable_by_ruling(self) -> None:
        """A good file no longer makes T3 available (ruling 2026-10-22),
        and the offset it carries follows the ruled convention:
        rtp_to_utc = -d_clock."""
        self._write(_good_fusion())
        r = self._probe().poll()
        self._refused(r, "ok")
        self.assertAlmostEqual(r.offset_ms, -0.812)
        self.assertAlmostEqual(r.sigma_ms, 0.94)
        self.assertEqual(r.detail["d_clock_fused_ms"], 0.812)
        self.assertEqual(r.detail["stations_used"], ["WWV", "CHU"])
        self.assertEqual(r.detail["kalman_state"], "LOCKED")
        self.assertEqual(r.frame, "rtp")

    def test_the_reason_names_the_ruling(self) -> None:
        self.assertIn("calibrated", T3_CALIBRATED_REASON)
        self.assertIn("not evidence about the clock", T3_CALIBRATED_REASON)
        self.assertIn("2026-10-22", T3_CALIBRATED_REASON)

    def test_acquiring_kalman_state_is_read_as_ok(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["kalman_state"] = "ACQUIRING"
        self._write(payload)
        self._refused(self._probe().poll(), "ok")

    # ----- failure modes -----

    def test_missing_file(self) -> None:
        r = self._probe().poll()
        self._refused(r, "missing")

    def test_corrupt_json(self) -> None:
        self.path.write_text("{not valid json")
        r = self._probe().poll()
        self._refused(r, "read error")

    def test_unknown_schema_rejected(self) -> None:
        self._write(_good_fusion(schema="v2"))
        r = self._probe().poll()
        self._refused(r, "unsupported schema")

    def test_stale_publication_rejected(self) -> None:
        # Good file, but now is 120 s later than publication.
        self.now = self.now + timedelta(seconds=120)
        self._write(_good_fusion())
        r = self._probe(freshness_sec=60).poll()
        self._refused(r, "stale")

    def test_fusion_unavailable_reason_rides_in_detail(self) -> None:
        payload = _good_fusion()
        payload["fusion"] = {"available": False, "reason": "no fusion result this cycle"}
        self._write(payload)
        r = self._probe().poll()
        self._refused(r, "no fusion result")

    def test_single_station_rejected_by_default(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["n_stations"] = 1
        self._write(payload)
        r = self._probe().poll()
        self._refused(r, "n_stations=1")

    def test_single_station_reads_ok_when_min_configured_down(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["n_stations"] = 1
        self._write(payload)
        r = self._probe(min_stations=1).poll()
        self._refused(r, "ok")

    def test_reacquiring_kalman_rejected(self) -> None:
        payload = _good_fusion()
        payload["fusion"]["kalman_state"] = "REACQUIRING"
        self._write(payload)
        r = self._probe().poll()
        self._refused(r, "kalman_state=REACQUIRING")

    def test_missing_offset_fields_rejected(self) -> None:
        payload = _good_fusion()
        del payload["fusion"]["d_clock_fused_ms"]
        self._write(payload)
        r = self._probe().poll()
        self._refused(r, "missing/invalid offset")
        self.assertIsNone(r.offset_ms)


if __name__ == "__main__":
    unittest.main()
