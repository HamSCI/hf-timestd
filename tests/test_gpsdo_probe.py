#!/usr/bin/env python3
"""Unit tests for GpsdoProbe — reads /run/gpsdo/<serial>.json produced
by the HamSCI/gpsdo-monitor daemon and maps it to an A-level string
the authority manager can consume as a_level_provider."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hf_timestd.core.gpsdo_probe import GpsdoProbe


def _report(
    *,
    serial: str = "LBE1421-ABC123",
    a_level_hint: str = "A1",
    written_utc: str = "2026-04-24T12:00:00.000Z",
    probe_interval_sec: int = 10,
    schema: str = "v1",
    a_level_reason: str = "pll_locked && gps_fix=3D && antenna_ok && pps_present && fresh",
) -> dict:
    """Minimal valid gpsdo-monitor report per docs/SCHEMA-v1.md."""
    return {
        "schema": schema,
        "written_utc": written_utc,
        "probe_interval_sec": probe_interval_sec,
        "host": "bee1.local",
        "device": {
            "model": "lbe-1421", "pid": "0x2444", "serial": serial,
            "hid_path": "/dev/hidraw0", "firmware": None,
            "firmware_source": "unavailable",
        },
        "governs": ["radiod:main"],
        "health": {"pll_locked": True, "outputs_enabled": True, "gps_fix": "3D"},
        "outputs": {"out1_hz": 10_000_000, "pps_enabled": True},
        "pps_study": {"enabled": True, "window_sec": 60, "edges": 60},
        "a_level_hint": a_level_hint,
        "a_level_reason": a_level_reason,
    }


class TestGpsdoProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Pin "now" at the instant the default-good report was written.
        self.now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, serial: str, payload: dict) -> Path:
        p = self.tmp / f"{serial}.json"
        p.write_text(json.dumps(payload))
        return p

    def _probe(self, **kwargs) -> GpsdoProbe:
        return GpsdoProbe(run_dir=self.tmp, now_fn=lambda: self.now, **kwargs)

    # ----- empty / missing ------------------------------------------------

    def test_missing_run_dir_returns_a0(self) -> None:
        probe = GpsdoProbe(run_dir=self.tmp / "does-not-exist",
                           now_fn=lambda: self.now)
        self.assertEqual(probe.poll(), "A0")

    def test_empty_run_dir_returns_a0(self) -> None:
        self.assertEqual(self._probe().poll(), "A0")

    def test_only_index_json_returns_a0(self) -> None:
        # index.json must be ignored — it's an aggregate list, not a
        # per-device report.
        (self.tmp / "index.json").write_text(json.dumps({"schema": "v1"}))
        self.assertEqual(self._probe().poll(), "A0")

    # ----- happy path -----------------------------------------------------

    def test_single_fresh_a1_reports_a1(self) -> None:
        self._write("LBE1421-ABC123", _report())
        self.assertEqual(self._probe().poll(), "A1")

    def test_single_fresh_a0_reports_a0(self) -> None:
        self._write("LBE1421-ABC123", _report(
            a_level_hint="A0",
            a_level_reason="gps_fix=no_fix",
        ))
        self.assertEqual(self._probe().poll(), "A0")

    def test_a1_plus_a0_is_a1_optimistic(self) -> None:
        # Any fresh A1 is enough; authority manager can still override.
        self._write("DEV-A", _report(serial="DEV-A", a_level_hint="A1"))
        self._write("DEV-B", _report(serial="DEV-B", a_level_hint="A0"))
        self.assertEqual(self._probe().poll(), "A1")

    # ----- staleness / freshness -----------------------------------------

    def test_stale_file_is_not_counted_as_a1(self) -> None:
        # With probe_interval_sec=10, staleness threshold is max(30, 3*10)=30s.
        self._write("X", _report(
            written_utc="2026-04-24T11:59:00.000Z",  # 60 s old
            probe_interval_sec=10,
        ))
        self.assertEqual(self._probe().poll(), "A0")

    def test_threshold_floor_protects_fast_probes(self) -> None:
        # probe_interval_sec=1 would give 3*1=3 s; we floor at 30 so a
        # momentary scheduling hiccup doesn't flap A1 -> A0.
        self._write("X", _report(
            written_utc="2026-04-24T11:59:45.000Z",  # 15 s old
            probe_interval_sec=1,
        ))
        self.assertEqual(self._probe().poll(), "A1")

    def test_custom_staleness_factor_tightens_window(self) -> None:
        # Factor of 1x + floor still leaves 30 s window, which a 20-s-old
        # report sits inside: still A1.
        self._write("X", _report(
            written_utc="2026-04-23T23:59:30.000Z",  # ~12h old
            probe_interval_sec=10,
        ))
        self.assertEqual(self._probe(staleness_factor=1.0).poll(), "A0")

    def test_future_written_utc_is_treated_as_age_zero(self) -> None:
        # Clock skew can push written_utc slightly ahead of the authority
        # runner's "now". Age clamped to 0 so we don't reject the file.
        self._write("X", _report(written_utc="2026-04-24T12:00:05.000Z"))
        self.assertEqual(self._probe().poll(), "A1")

    # ----- malformed input ------------------------------------------------

    def test_wrong_schema_is_ignored(self) -> None:
        self._write("X", _report(schema="v2"))
        self.assertEqual(self._probe().poll(), "A0")

    def test_bad_json_is_ignored(self) -> None:
        (self.tmp / "corrupt.json").write_text("{ not valid json")
        self.assertEqual(self._probe().poll(), "A0")

    def test_missing_written_utc_is_treated_as_stale(self) -> None:
        r = _report()
        r.pop("written_utc", None)
        self._write("X", r)
        self.assertEqual(self._probe().poll(), "A0")

    def test_unparseable_written_utc_is_treated_as_stale(self) -> None:
        self._write("X", _report(written_utc="yesterday"))
        self.assertEqual(self._probe().poll(), "A0")

    def test_unknown_a_level_hint_is_ignored(self) -> None:
        self._write("X", _report(a_level_hint="maybe"))
        self.assertEqual(self._probe().poll(), "A0")

    # ----- serial filter --------------------------------------------------

    def test_serial_filter_selects_only_that_device(self) -> None:
        self._write("KEEP", _report(serial="KEEP", a_level_hint="A0"))
        self._write("OTHER", _report(serial="OTHER", a_level_hint="A1"))
        # Despite OTHER being A1, serial=KEEP restricts our view.
        self.assertEqual(self._probe(serial="KEEP").poll(), "A0")

    def test_serial_filter_missing_file_is_a0(self) -> None:
        self._write("EXISTS", _report(serial="EXISTS", a_level_hint="A1"))
        self.assertEqual(self._probe(serial="MISSING").poll(), "A0")

    # ----- diagnostics ----------------------------------------------------

    def test_poll_detail_reports_per_device_state(self) -> None:
        self._write("A", _report(serial="A", a_level_hint="A1"))
        self._write("B", _report(
            serial="B", a_level_hint="A0",
            a_level_reason="pll_unlocked",
        ))
        details = {s.serial: s for s in self._probe().poll_detail()}
        self.assertEqual(details["A"].a_level_hint, "A1")
        self.assertTrue(details["A"].used)
        self.assertEqual(details["B"].a_level_reason, "pll_unlocked")
        self.assertTrue(details["B"].used)  # "A0" still counts as "read"

    def test_poll_detail_flags_unused_stale_record(self) -> None:
        self._write("X", _report(
            written_utc="2026-04-24T11:59:00.000Z", probe_interval_sec=10,
        ))
        (sample,) = self._probe().poll_detail()
        self.assertFalse(sample.fresh)
        self.assertFalse(sample.used)
        self.assertEqual(sample.a_level_hint, "A1")     # field still reported


if __name__ == "__main__":
    unittest.main()
