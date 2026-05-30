"""Tests for the LBE-1421 GPSDO T5 disambiguation probe.

The probe is a JSON-file poller that consumes gpsdo-monitor's published
per-device files under `/run/gpsdo/<serial>.json` (Schema v1).  Tests
synthesise those files in a tmp_path to exercise the probe end-to-end
without a real serial device or daemon.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hf_timestd.core.lb1421_t5_probe import (
    DEFAULT_RUN_DIR,
    Lb1421Reading,
    Lb1421T5Probe,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


_UNSET = object()


def _device_doc(
    *,
    serial: str = "1421-TEST",
    written_utc: str | None = None,
    pps_utc_sec: Any = _UNSET,  # default: current second - 1; pass None to omit
    fix_age_sec: float | None = 0.4,
    probe_interval_sec: int = 10,
) -> dict[str, Any]:
    # Default to a fresh integer-UTC pair so the probe's host-vs-GPS
    # consistency check passes — tests using a frozen past timestamp
    # would otherwise see valid_fix=False.
    if pps_utc_sec is _UNSET:
        pps_utc_sec = int(time.time()) - 1
    """Build a minimal Schema-v1-compatible device report dict."""
    return {
        "schema": "v1",
        "written_utc": written_utc or _now_iso(),
        "probe_interval_sec": probe_interval_sec,
        "host": "test",
        "device": {
            "model": "LBE-1421",
            "pid": "0001",
            "serial": serial,
            "hid_path": "n/a",
        },
        "governs": [],
        "health": {
            "pll_locked": True,
            "outputs_enabled": True,
            "gps_fix": "3D",
            "sats_used": 12,
            "fix_age_sec": fix_age_sec,
            "pps_utc_sec": pps_utc_sec,
            "nmea_host_monotonic_at_read": 12345.6,
        },
        "outputs": {},
        "pps_study": {"enabled": False, "window_sec": 0, "edges": 0},
        "a_level_hint": "A1",
        "a_level_reason": "test",
    }


def _write(path: Path, doc: dict[str, Any]) -> None:
    path.write_text(json.dumps(doc))


# -- get_latest semantics (without the background thread) ------------------


class TestReadOnce:
    """Drive `_read_once()` directly — deterministic, no thread."""

    def test_no_run_dir_returns_none(self, tmp_path: Path):
        probe = Lb1421T5Probe(run_dir=tmp_path / "missing")
        assert probe._read_once() is None

    def test_no_files_returns_none(self, tmp_path: Path):
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_valid_file_yields_reading(self, tmp_path: Path):
        expected_pps = int(time.time()) - 1
        _write(tmp_path / "1421-A.json",
               _device_doc(serial="1421-A", pps_utc_sec=expected_pps))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        r = probe._read_once()
        assert r is not None
        # _read_once returns the raw integer second from the JSON; the
        # consumer-time projection happens in get_latest().
        assert r.pps_utc_sec == expected_pps
        assert r.valid_fix is True

    def test_serial_filter_picks_matching_file(self, tmp_path: Path):
        _write(tmp_path / "1421-A.json", _device_doc(
            serial="1421-A", pps_utc_sec=111))
        _write(tmp_path / "1421-B.json", _device_doc(
            serial="1421-B", pps_utc_sec=222))
        probe = Lb1421T5Probe(run_dir=tmp_path, serial="1421-B")
        r = probe._read_once()
        assert r is not None and r.pps_utc_sec == 222

    def test_serial_filter_missing_file_returns_none(self, tmp_path: Path):
        _write(tmp_path / "1421-A.json", _device_doc(serial="1421-A"))
        probe = Lb1421T5Probe(run_dir=tmp_path, serial="1421-MISSING")
        assert probe._read_once() is None

    def test_skips_index_json(self, tmp_path: Path):
        _write(tmp_path / "index.json", {"schema": "v1", "devices": []})
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_stale_file_rejected(self, tmp_path: Path):
        # written_utc 10 min ago, file_max_age_s default 30 s.
        old_iso = datetime.fromtimestamp(
            time.time() - 600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _write(tmp_path / "1421-A.json", _device_doc(written_utc=old_iso))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_missing_pps_utc_sec_returns_none(self, tmp_path: Path):
        _write(tmp_path / "1421-A.json", _device_doc(pps_utc_sec=None))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_stale_fix_marks_valid_false(self, tmp_path: Path):
        # NMEA was valid once but fix has been stale for 5 sec; default
        # nmea_max_age_s is 2.0, so valid_fix must come back False.
        _write(tmp_path / "1421-A.json", _device_doc(fix_age_sec=5.0))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        r = probe._read_once()
        assert r is not None
        assert r.valid_fix is False

    def test_missing_fix_age_returns_none(self, tmp_path: Path):
        # Without fix_age_sec the probe cannot compute effective freshness
        # OR run the host/GPS consistency check, so it returns None
        # rather than a not-fresh reading.
        _write(tmp_path / "1421-A.json", _device_doc(fix_age_sec=None))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_host_gps_inconsistent_marks_valid_false(self, tmp_path: Path):
        # pps_utc_sec a year in the past — host clock and NMEA truth
        # disagree by far more than 1 sec → demote T5.
        _write(tmp_path / "1421-A.json", _device_doc(
            pps_utc_sec=int(time.time()) - 365 * 86400,
        ))
        probe = Lb1421T5Probe(run_dir=tmp_path)
        r = probe._read_once()
        assert r is not None
        assert r.valid_fix is False

    def test_wrong_schema_version_rejected(self, tmp_path: Path):
        doc = _device_doc()
        doc["schema"] = "v0"
        _write(tmp_path / "1421-A.json", doc)
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_malformed_json_logged_and_skipped(self, tmp_path: Path):
        (tmp_path / "1421-A.json").write_text("{not json")
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None

    def test_bad_written_utc_rejected(self, tmp_path: Path):
        doc = _device_doc(written_utc="not-a-timestamp")
        _write(tmp_path / "1421-A.json", doc)
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe._read_once() is None


# -- get_latest age-out and require_valid_fix gates -------------------------


class TestGetLatest:
    def test_initial_get_latest_returns_none(self, tmp_path: Path):
        probe = Lb1421T5Probe(run_dir=tmp_path)
        assert probe.get_latest() is None

    def test_freshness_window_respected(self, tmp_path: Path):
        # Manually seed _latest with an old monotonic timestamp.
        probe = Lb1421T5Probe(run_dir=tmp_path)
        probe._latest = Lb1421Reading(
            pps_utc_sec=1,
            host_monotonic_at_read=time.monotonic() - 5.0,
            valid_fix=True,
        )
        assert probe.get_latest(max_age_s=2.0) is None
        assert probe.get_latest(max_age_s=10.0) is not None

    def test_require_valid_fix_filters(self, tmp_path: Path):
        probe = Lb1421T5Probe(run_dir=tmp_path)
        probe._latest = Lb1421Reading(
            pps_utc_sec=1,
            host_monotonic_at_read=time.monotonic(),
            valid_fix=False,
        )
        assert probe.get_latest(require_valid_fix=True) is None
        assert probe.get_latest(require_valid_fix=False) is not None

    def test_get_latest_projects_pps_to_consumer_time(self, tmp_path: Path):
        # Seed a stale raw pps_utc_sec; get_latest should return
        # floor(time.time()), not the raw value — this is what keeps
        # the ±0.5s disambig guard in
        # _t6_disambiguate_via_t5_lb1421 within reach even when the
        # gpsdo-monitor JSON is 5-10 s stale (probe_interval cadence).
        probe = Lb1421T5Probe(run_dir=tmp_path)
        stale_raw = int(time.time()) - 7  # 7 sec stale
        probe._latest = Lb1421Reading(
            pps_utc_sec=stale_raw,
            host_monotonic_at_read=time.monotonic(),
            valid_fix=True,
        )
        r = probe.get_latest()
        assert r is not None
        assert r.pps_utc_sec == int(time.time())
        assert r.pps_utc_sec != stale_raw


# -- background thread end-to-end ------------------------------------------


class TestBackgroundReader:
    def test_thread_picks_up_seeded_file(self, tmp_path: Path):
        _write(tmp_path / "1421-A.json", _device_doc())
        probe = Lb1421T5Probe(run_dir=tmp_path, poll_interval_s=0.05)
        probe.start()
        try:
            for _ in range(40):  # up to 2 s
                r = probe.get_latest(max_age_s=10.0)
                if r is not None:
                    break
                time.sleep(0.05)
            assert r is not None
            # get_latest projects pps_utc_sec to consumer time, so the
            # returned value is floor(time.time()), not the raw value.
            assert r.pps_utc_sec == int(time.time())
            assert r.valid_fix is True
        finally:
            probe.stop()

    def test_start_is_idempotent(self, tmp_path: Path):
        probe = Lb1421T5Probe(run_dir=tmp_path, poll_interval_s=0.05)
        probe.start()
        t1 = probe._thread
        probe.start()
        t2 = probe._thread
        assert t1 is t2
        probe.stop()


# -- module-level constants ------------------------------------------------


def test_default_run_dir_is_run_gpsdo():
    assert DEFAULT_RUN_DIR == Path("/run/gpsdo")
