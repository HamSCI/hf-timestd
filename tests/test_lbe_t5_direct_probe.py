"""Tests for LbeT5DirectProbe.

Mirrors test_core_recorder_t6_drift_monitor.py shape: status-file
fixtures, dependency-injected now_fn, and exhaustive coverage of
every short-circuit branch the probe takes before declaring
T5 available.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


# Mirrors the t5_lbe1421 block core_recorder writes to
# /var/lib/timestd/status/core-recorder-status.json.
def _make_status(
    *,
    timestamp: str = "2026-05-24T21:30:00Z",
    t5_block=None,
    omit_t5: bool = False,
):
    status = {
        "timestamp": timestamp,
        "l6_pps": {"enabled": False},
    }
    if not omit_t5:
        if t5_block is None:
            t5_block = {
                "enabled": True,
                "valid_fix": True,
                "pps_utc_sec": 1716501000,
                "age_sec": 0.5,
                "device": "/dev/lb1421-nmea",
            }
        status["t5_lbe1421"] = t5_block
    return status


def _write(status, tmpdir):
    p = Path(tmpdir) / "core-recorder-status.json"
    p.write_text(json.dumps(status))
    return p


# Reference "now" used by tests — matches the fixture timestamps so
# the status file is 0 s old by default.
NOW = datetime(2026, 5, 24, 21, 30, 0, tzinfo=timezone.utc)


class LbeT5DirectProbeAvailableTests(unittest.TestCase):
    """The happy path: every gate passes, T5 reports available."""

    def test_normal_fresh_reading_yields_available(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(), d)
            probe = LbeT5DirectProbe(
                status_path=p, now_fn=lambda: NOW,
                sigma_floor_ms=5.0,
            )
            r = probe.poll()
        self.assertTrue(r.available)
        self.assertEqual(r.t_level, "T5")
        self.assertEqual(r.offset_ms, 0.0)        # Phase 2A: trust-tier
        self.assertEqual(r.sigma_ms, 5.0)
        # Detail surfaces what an operator needs to see at a glance.
        self.assertTrue(r.detail["valid_fix"])
        self.assertEqual(r.detail["pps_utc_sec"], 1716501000)
        self.assertAlmostEqual(r.detail["nmea_age_sec"], 0.5)
        self.assertEqual(r.detail["device"], "/dev/lb1421-nmea")
        self.assertEqual(r.detail["sigma_floor_ms"], 5.0)

    def test_sigma_floor_is_configurable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(), d)
            probe = LbeT5DirectProbe(
                status_path=p, now_fn=lambda: NOW,
                sigma_floor_ms=2.0,
            )
            r = probe.poll()
        self.assertEqual(r.sigma_ms, 2.0)


class LbeT5DirectProbeUnavailableTests(unittest.TestCase):
    """Every short-circuit branch maps to an operator-readable reason."""

    def test_status_file_missing_unavailable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        probe = LbeT5DirectProbe(
            status_path=Path("/nonexistent/x.json"), now_fn=lambda: NOW,
        )
        r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("missing", r.reason)

    def test_unparseable_json_unavailable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "core-recorder-status.json"
            p.write_text("{ not json")
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("read error", r.reason)

    def test_status_timestamp_missing_unavailable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write({"l6_pps": {}, "t5_lbe1421": {"enabled": True,
                       "valid_fix": True, "age_sec": 0.5}}, d)
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("timestamp missing", r.reason)

    def test_stale_status_file_unavailable(self):
        """Status file older than freshness_sec → unavailable."""
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        # File is 5 minutes old vs freshness=60s.
        old = "2026-05-24T21:25:00Z"
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(timestamp=old), d)
            probe = LbeT5DirectProbe(
                status_path=p, now_fn=lambda: NOW, freshness_sec=60.0,
            )
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("stale", r.reason)

    def test_missing_t5_block_unavailable_with_clear_reason(self):
        """When core_recorder didn't write the t5_lbe1421 block (e.g.,
        no probe was attached), the reason must call that out so
        operators can wire the device."""
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(omit_t5=True), d)
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("not attached", r.reason)

    def test_t5_disabled_unavailable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(
                t5_block={"enabled": False, "valid_fix": False},
            ), d)
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("disabled", r.reason)

    def test_no_fix_unavailable(self):
        """LBE-1421 sees no satellites — T5 cannot vouch for UTC."""
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(
                t5_block={"enabled": True, "valid_fix": False,
                          "age_sec": 0.5, "reason": "no reading yet"},
            ), d)
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("no valid fix", r.reason)
        # The producer-side hint should ride through.
        self.assertIn("no reading yet", r.reason)

    def test_stale_nmea_reading_unavailable(self):
        """Status file is fresh but NMEA reading inside it is old —
        the device went silent.  Distinct failure mode from a stale
        status file (which means core_recorder itself stalled)."""
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(t5_block={
                "enabled": True, "valid_fix": True,
                "pps_utc_sec": 1716501000, "age_sec": 10.0,
            }), d)
            probe = LbeT5DirectProbe(
                status_path=p, now_fn=lambda: NOW, max_nmea_age_sec=2.0,
            )
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("NMEA stale", r.reason)

    def test_missing_age_unavailable(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        with tempfile.TemporaryDirectory() as d:
            p = _write(_make_status(t5_block={
                "enabled": True, "valid_fix": True,
                "pps_utc_sec": 1716501000,
            }), d)
            probe = LbeT5DirectProbe(status_path=p, now_fn=lambda: NOW)
            r = probe.poll()
        self.assertFalse(r.available)
        self.assertIn("age missing", r.reason)


class LbeT5DirectProbeT_LevelTests(unittest.TestCase):
    """Constants the AuthorityManager + snapshot store depend on."""

    def test_t_level_is_T5(self):
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        self.assertEqual(LbeT5DirectProbe.t_level, "T5")


if __name__ == "__main__":
    unittest.main()
