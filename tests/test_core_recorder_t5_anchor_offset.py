"""Tests for the T5 anchor-disagreement offset (Phase 2B).

``_compute_t5_anchor_offset`` extrapolates the RTP anchor's UTC view
forward from its capture instant (host monotonic) to the moment of
the latest NMEA reading, then compares to NMEA's pps_utc_sec.  The
result lands in core-recorder-status.json's ``t5_lbe1421`` block as
``anchor_offset_ns``; ``LbeT5DirectProbe`` forwards it as the T5
probe's ``offset_ms`` and ``AuthorityManager._build_state`` publishes
it as ``rtp_to_utc_offset_ns`` when the manager elects T5 active.

These tests pin the math and the freshness gates; the live-system
behaviour (LbeT5DirectProbe consuming the field, manager honoring
the rtp_anchor_grounded marker) is covered by the probe/manager
unit tests separately.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


# Anchor inputs are conveyed in GPS-time nanoseconds; the conversion
# to Unix-UTC seconds inside the helper is:
#   anchor_utc_at_set_sec = (gps_time_ns + BILLION*(GPS_UTC_OFFSET - GPS_LEAP_SECONDS)) / BILLION
# with GPS_UTC_OFFSET=315964800 and GPS_LEAP_SECONDS=18.
GPS_UTC_BIAS_SEC = 315964800 - 18  # 315964782


def _gps_time_ns_for_utc(utc_sec: float) -> int:
    """Pick a gps_time_ns whose conversion produces the given UTC."""
    return int(round((utc_sec - GPS_UTC_BIAS_SEC) * 1_000_000_000))


class _FakeReading:
    """Minimal stand-in for hf_timestd.core.lb1421_t5_probe.Lb1421Reading
    so we don't construct the real frozen dataclass with all fields."""

    def __init__(self, *, pps_utc_sec, host_monotonic_at_read, valid_fix=True):
        self.pps_utc_sec = pps_utc_sec
        self.host_monotonic_at_read = host_monotonic_at_read
        self.valid_fix = valid_fix


def _bare_recorder() -> CoreRecorderV2:
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_timing_lock = threading.Lock()
    cr._t6_latest_gps_time_ns = None
    cr._t6_latest_rtp_timesnap = None
    cr._t6_latest_anchor_monotonic = None
    return cr


class T5AnchorOffsetGuardTests(unittest.TestCase):
    """Short-circuit branches that must yield (None, None) without
    computing — invalid input, unset anchor."""

    def test_no_reading_returns_none(self):
        cr = _bare_recorder()
        # Anchor IS set so the only short-circuit is the reading.
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0)
        cr._t6_latest_anchor_monotonic = 100.0
        offset_ns, age = cr._compute_t5_anchor_offset(None)
        self.assertIsNone(offset_ns)
        self.assertIsNone(age)

    def test_invalid_fix_returns_none(self):
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0)
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.5,
            valid_fix=False,
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertIsNone(offset_ns)
        self.assertIsNone(age)

    def test_anchor_unset_returns_none(self):
        cr = _bare_recorder()
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.5,
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertIsNone(offset_ns)
        self.assertIsNone(age)


class T5AnchorOffsetFreshnessTests(unittest.TestCase):
    """Stale anchors are skipped — the extrapolation diverges from
    truth as anchor age grows.  We bound by 1.2× the timing poll
    cadence (T6_T5_OFFSET_MAX_ANCHOR_AGE_SEC)."""

    def test_age_above_cap_returns_none_offset_with_age_surfaced(self):
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0)
        cr._t6_latest_anchor_monotonic = 100.0
        too_old_age = cr.T6_T5_OFFSET_MAX_ANCHOR_AGE_SEC + 1.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.0 + too_old_age,
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertIsNone(offset_ns)
        # The age IS surfaced so operators can correlate the missing
        # offset with anchor staleness without grepping logs.
        self.assertAlmostEqual(age, too_old_age)

    def test_negative_age_returns_none(self):
        """NMEA reading captured BEFORE the latest anchor → the
        elapsed-time bookkeeping is broken; refuse to extrapolate."""
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0)
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=99.5,  # before the anchor
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertIsNone(offset_ns)
        self.assertLess(age, 0.0)

    def test_age_at_cap_is_still_computed(self):
        """The cap is inclusive — equal-to-cap is honored, only
        strictly-greater is rejected.  This avoids flapping at the
        boundary in steady state."""
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0)
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.0 + cr.T6_T5_OFFSET_MAX_ANCHOR_AGE_SEC,
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertIsNotNone(offset_ns)


class T5AnchorOffsetMathTests(unittest.TestCase):
    """The arithmetic: anchor extrapolated forward through monotonic
    elapsed, compared to NMEA's integer-second truth."""

    def test_zero_disagreement_when_extrapolation_matches_pps(self):
        """Anchor set 500 ms before the NMEA read; the anchor's UTC
        view at set was pps_utc_sec - 0.5.  Extrapolated forward by
        the 500 ms elapsed → exactly pps_utc_sec.  Offset = 0."""
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(1_700_000_000.0 - 0.5)
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.5,
        )
        offset_ns, age = cr._compute_t5_anchor_offset(reading)
        self.assertEqual(offset_ns, 0)
        self.assertAlmostEqual(age, 0.5)

    def test_positive_drift_anchor_ahead_of_truth(self):
        """Anchor predicts UTC = pps_utc_sec + 0.200 at NMEA-read
        time → anchor reads 200 ms too high → offset = +200_000_000 ns."""
        cr = _bare_recorder()
        # set anchor 200 ms ahead of the (anchor_mono - 0.5)-implied truth
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(
            1_700_000_000.0 - 0.5 + 0.200
        )
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.5,
        )
        offset_ns, _ = cr._compute_t5_anchor_offset(reading)
        # Allow integer rounding wiggle from the gps_time_ns int cast.
        self.assertAlmostEqual(offset_ns / 1e9, 0.200, places=6)

    def test_negative_drift_anchor_behind_truth(self):
        """Anchor predicts UTC = pps_utc_sec - 0.150 at NMEA-read
        time → anchor reads 150 ms too low → offset = -150_000_000 ns."""
        cr = _bare_recorder()
        cr._t6_latest_gps_time_ns = _gps_time_ns_for_utc(
            1_700_000_000.0 - 0.5 - 0.150
        )
        cr._t6_latest_anchor_monotonic = 100.0
        reading = _FakeReading(
            pps_utc_sec=1_700_000_000,
            host_monotonic_at_read=100.5,
        )
        offset_ns, _ = cr._compute_t5_anchor_offset(reading)
        self.assertAlmostEqual(offset_ns / 1e9, -0.150, places=6)


if __name__ == "__main__":
    unittest.main()
