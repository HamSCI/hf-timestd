"""Tests for the LBE-1421 GPSDO NMEA T5 disambiguation-reference probe."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hf_timestd.core.lb1421_t5_probe import (
    Lb1421Reading,
    Lb1421T5Probe,
    parse_rmc,
)


# Live-captured sentence from bee1 LBE-1421 2026-05-23 10:41:24 UTC.
SAMPLE_GNRMC = (
    "$GNRMC,104124.20,A,3855.12204,N,09207.65825,W,0.000,,230526,,,D*77"
)
SAMPLE_GPRMC = (
    "$GPRMC,123456.78,A,3855.12204,N,09207.65825,W,0.000,,150301,,,A*62"
)
SAMPLE_RMC_NO_FIX = (
    "$GNRMC,104124.20,V,,,,,0.000,,230526,,,N*4D"
)


# -- parse_rmc --------------------------------------------------------------


class TestParseRmc:
    def test_valid_gnrmc(self):
        r = parse_rmc(SAMPLE_GNRMC)
        assert r is not None
        # 2026-05-23 10:41:24 UTC
        expected = int(
            datetime(2026, 5, 23, 10, 41, 24, tzinfo=timezone.utc).timestamp()
        )
        assert r.pps_utc_sec == expected
        assert r.valid_fix is True

    def test_subsecond_truncated_to_integer(self):
        # NMEA time field 104124.20 must round-DOWN to the integer
        # second; the fractional part reflects sentence-emission
        # latency, not the PPS edge itself.
        r = parse_rmc(SAMPLE_GNRMC)
        assert r is not None
        # Confirm we're using the integer seconds without rounding up.
        dt = datetime.fromtimestamp(r.pps_utc_sec, tz=timezone.utc)
        assert dt.hour == 10
        assert dt.minute == 41
        assert dt.second == 24

    def test_void_fix_status_recorded(self):
        r = parse_rmc(SAMPLE_RMC_NO_FIX)
        assert r is not None
        assert r.valid_fix is False

    def test_gprmc_also_accepted(self):
        # The probe must accept both $GP (GPS-only) and $GN (multi-GNSS)
        # variants; the LBE-1421 uses GN, but older GPS modules use GP
        # and the same parsing logic applies.
        r = parse_rmc(SAMPLE_GPRMC)
        assert r is not None
        # 2001-03-15 12:34:56 UTC
        expected = int(
            datetime(2001, 3, 15, 12, 34, 56, tzinfo=timezone.utc).timestamp()
        )
        assert r.pps_utc_sec == expected

    def test_bad_checksum_rejected(self):
        # Flip last hex digit
        bad = SAMPLE_GNRMC[:-1] + "0"
        assert parse_rmc(bad) is None

    def test_missing_checksum_accepted(self):
        # Some emitters don't append the *CS checksum.  The parser is
        # permissive about this — only outright malformed lines are
        # rejected.
        nochk = SAMPLE_GNRMC.split("*")[0]
        r = parse_rmc(nochk)
        assert r is not None

    def test_wrong_sentence_type_rejected(self):
        # Non-RMC sentences should return None (we only consume RMC
        # because it carries both time and date).
        gga = "$GNGGA,104124.20,3855.1,N,09207.6,W,2,11,0.49,269.6,M,-30,M,,0*55"
        assert parse_rmc(gga) is None

    def test_empty_time_field_rejected(self):
        # If the LB-1421 emits RMC during boot before time-fix, the time
        # field may be empty — we must not crash, just skip the reading.
        empty = "$GNRMC,,V,,,,,0.000,,,,,N*53"
        assert parse_rmc(empty) is None

    def test_malformed_line_rejected(self):
        for bad in [
            "",
            "not nmea",
            "$",
            "$GNRMC",
            "$GNRMC,abc,A,,,,,,,,,,,N*55",
        ]:
            assert parse_rmc(bad) is None, f"should reject: {bad!r}"

    def test_invalid_checksum_hex_rejected(self):
        bad = SAMPLE_GNRMC[:-2] + "ZZ"
        assert parse_rmc(bad) is None

    def test_year_2000_pivot(self):
        # 2-digit year 00 → 2000 (not 1900).  This is the unambiguous
        # convention for NMEA in any post-2000 deployment.
        sentence = "$GNRMC,000000.00,A,0.0,N,0.0,W,0.000,,010100,,,A*72"
        r = parse_rmc(sentence)
        # If parse succeeds, year must be 2000.  (Checksum may be wrong
        # for this synthetic; we don't care here.)
        # Force parse by stripping checksum:
        r = parse_rmc(sentence.split("*")[0])
        assert r is not None
        dt = datetime.fromtimestamp(r.pps_utc_sec, tz=timezone.utc)
        assert dt.year == 2000

    def test_reading_records_host_monotonic(self):
        before = time.monotonic()
        r = parse_rmc(SAMPLE_GNRMC)
        after = time.monotonic()
        assert r is not None
        assert before <= r.host_monotonic_at_read <= after


# -- Lb1421T5Probe (background reader) --------------------------------------


class TestProbeReader:
    """Tests for the background reader, using a FIFO as the device file."""

    @pytest.fixture
    def fifo_path(self, tmp_path: Path):
        path = tmp_path / "fake-nmea-fifo"
        os.mkfifo(path)
        yield path
        # Cleanup: best-effort
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def test_initial_get_latest_returns_none(self):
        probe = Lb1421T5Probe(device=Path("/nonexistent"))
        assert probe.get_latest() is None

    def test_reader_thread_picks_up_sentence(self, fifo_path):
        probe = Lb1421T5Probe(
            device=fifo_path, fallback_device=Path("/nonexistent")
        )
        probe.start()
        try:
            # Open writer side; this unblocks the reader's open().
            # mkfifo open() blocks until both sides connect, so the
            # writer-side open must happen for the reader to start.
            with open(fifo_path, "wb", buffering=0) as w:
                w.write(SAMPLE_GNRMC.encode("ascii") + b"\n")
                # Give the reader thread a moment to process.
                for _ in range(50):
                    r = probe.get_latest(max_age_s=10.0)
                    if r is not None:
                        break
                    time.sleep(0.05)
                assert r is not None
                assert r.valid_fix is True
        finally:
            probe.stop()

    def test_get_latest_respects_freshness(self, fifo_path):
        probe = Lb1421T5Probe(
            device=fifo_path, fallback_device=Path("/nonexistent")
        )
        probe.start()
        try:
            with open(fifo_path, "wb", buffering=0) as w:
                w.write(SAMPLE_GNRMC.encode("ascii") + b"\n")
                for _ in range(50):
                    r = probe.get_latest(max_age_s=10.0)
                    if r is not None:
                        break
                    time.sleep(0.05)
                assert r is not None
            # No more writes; wait long enough for the reading to age out.
            # Use a short max_age_s so we don't need a real second to pass.
            time.sleep(0.2)
            stale = probe.get_latest(max_age_s=0.1)
            assert stale is None
        finally:
            probe.stop()

    def test_get_latest_require_valid_fix(self, fifo_path):
        probe = Lb1421T5Probe(
            device=fifo_path, fallback_device=Path("/nonexistent")
        )
        probe.start()
        try:
            with open(fifo_path, "wb", buffering=0) as w:
                w.write(SAMPLE_RMC_NO_FIX.encode("ascii") + b"\n")
                for _ in range(50):
                    r = probe.get_latest(max_age_s=10.0, require_valid_fix=False)
                    if r is not None:
                        break
                    time.sleep(0.05)
                assert r is not None
                # The same reading must be filtered out when require_valid_fix
                # is True (the default for production disambig use).
                strict = probe.get_latest(max_age_s=10.0, require_valid_fix=True)
                assert strict is None
        finally:
            probe.stop()

    def test_garbage_lines_skipped(self, fifo_path):
        probe = Lb1421T5Probe(
            device=fifo_path, fallback_device=Path("/nonexistent")
        )
        probe.start()
        try:
            with open(fifo_path, "wb", buffering=0) as w:
                # Mix of noise lines + one real sentence.  The noise
                # must not crash the reader; the real one must be
                # picked up.
                w.write(b"garbage1\n")
                w.write(b"\n")
                w.write(b"$GPGSV,1,1,01,01,01,000,01*4F\n")  # non-RMC
                w.write(SAMPLE_GNRMC.encode("ascii") + b"\n")
                for _ in range(50):
                    r = probe.get_latest(max_age_s=10.0)
                    if r is not None:
                        break
                    time.sleep(0.05)
                assert r is not None
        finally:
            probe.stop()

    def test_start_is_idempotent(self):
        probe = Lb1421T5Probe(
            device=Path("/nonexistent-1"),
            fallback_device=Path("/nonexistent-2"),
        )
        probe.start()
        # Calling start() again must NOT spawn a second thread.
        t1 = probe._thread
        probe.start()
        t2 = probe._thread
        assert t1 is t2
        probe.stop()
