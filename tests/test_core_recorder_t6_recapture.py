"""Tests for the T6 Layer 3 re-capture logic.

Layer 3 consumes the Layer 2 flags (anchor_discontinuity, sustained_breach)
and re-runs the settled-capture gate + a fresh ``discover_channels`` to
replace both anchors atomically.

Coverage:
  * Signal A (anchor_discontinuity) bypasses hysteresis and recaptures
    on the first poll tick.
  * Signal B (sustained_breach) within cooldown is suppressed; after
    cooldown it recaptures.
  * Per-hour cap blocks sustained-breach recaptures after N within
    the rolling 60-min window.  Anchor-discontinuity still bypasses
    the cap.
  * Re-capture failure paths (chrony unsettled, discovery error,
    missing fresh ChannelInfo, missing gps_time/rtp_timesnap) leave
    state untouched.
  * Successful re-capture clears both flags, advances both anchor
    sets, bumps the count, and the cooldown window engages.
"""
from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _channel_info(*, ssrc: int = 0x1234, gps_time: int = 100_000_000_000,
                  rtp_timesnap: int = 500_000, frequency: float = 5_000_000.0):
    """Build a stand-in ChannelInfo with the fields rtp_to_wallclock + the
    drift/recapture machinery read.  Real ka9q.ChannelInfo is a dataclass;
    SimpleNamespace mimics the attribute access without dragging in the
    full ka9q import surface for unit tests.
    """
    return SimpleNamespace(
        ssrc=ssrc, gps_time=gps_time, rtp_timesnap=rtp_timesnap,
        frequency=frequency, sample_rate=24000,
    )


def _bare_recorder(*, sample_rate: int = 24000, ssrc: int = 0x1234,
                   anchor_gps: int = 100_000_000_000, anchor_rtp: int = 500_000):
    """Build a CoreRecorderV2 with Layer 2 + Layer 3 state populated.

    Bypasses __init__ (per the established pattern in
    test_core_recorder_t6_step_recovery.py) and stubs out
    _wait_for_chrony_settled + discover_channels so each test can
    pin those behaviours.
    """
    import threading
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_calibrator = MagicMock(sample_rate=sample_rate)
    cr._t6_channel_info = _channel_info(
        ssrc=ssrc, gps_time=anchor_gps, rtp_timesnap=anchor_rtp,
    )
    cr._t6_timing_lock = threading.Lock()
    cr._t6_latest_gps_time_ns = anchor_gps
    cr._t6_latest_rtp_timesnap = anchor_rtp
    cr._t6_drift_first_breach_wall = None
    cr._t6_drift_flag_sustained = False
    cr._t6_drift_flag_anchor_discontinuity = False
    cr._t6_drift_anchor_residual_samples = None
    cr._t6_drift_last_check_wall = None
    cr._t6_drift_anchor_gps_ns = anchor_gps
    cr._t6_drift_anchor_rtp_timesnap = anchor_rtp
    cr._t6_recapture_count = 0
    cr._t6_last_recapture_wall = None
    cr._t6_last_recapture_reason = None
    cr._t6_recapture_wall_history = deque(maxlen=cr.T6_RECAPTURE_MAX_PER_HOUR + 1)
    cr.status_address = "test-status.local"
    # Default: chrony settles immediately; discovery returns a fresh
    # ChannelInfo with timestamps advanced by 10 s of elapsed time
    # (proper N×sample_rate progression so a follow-up Signal A check
    # against the new anchor would compute residual = 0).
    cr._wait_for_chrony_settled = MagicMock(return_value=True)
    return cr


def _patched_discover(fresh_gps_ns: int, fresh_rtp_snap: int, *, ssrc: int = 0x1234):
    """Return a patch.object context manager that makes
    ``hf_timestd.core.core_recorder_v2.discover_channels`` return a
    single ChannelInfo with the supplied timestamps."""
    fresh_ci = _channel_info(ssrc=ssrc, gps_time=fresh_gps_ns,
                              rtp_timesnap=fresh_rtp_snap)
    return patch(
        'hf_timestd.core.core_recorder_v2.discover_channels',
        return_value={ssrc: fresh_ci},
    )


class TestSignalABypassesHysteresis(unittest.TestCase):
    """anchor_discontinuity should fire on the first poll tick without
    waiting for any cooldown — namespace changes are binary."""

    def test_first_discontinuity_recaptures(self):
        cr = _bare_recorder()
        cr._t6_drift_flag_anchor_discontinuity = True
        fresh_gps, fresh_rtp = 200_000_000_000, 800_000
        with _patched_discover(fresh_gps, fresh_rtp):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 1)
        self.assertEqual(cr._t6_last_recapture_reason, 'anchor_discontinuity')
        self.assertEqual(cr._t6_drift_anchor_gps_ns, fresh_gps)
        self.assertEqual(cr._t6_drift_anchor_rtp_timesnap, fresh_rtp)
        self.assertEqual(cr._t6_channel_info.gps_time, fresh_gps)
        self.assertEqual(cr._t6_channel_info.rtp_timesnap, fresh_rtp)
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)

    def test_discontinuity_clears_both_signals_in_one_pass(self):
        """If both flags are set (e.g. radiod restart + Δ spike), the
        single discontinuity-driven re-capture clears both."""
        cr = _bare_recorder()
        cr._t6_drift_flag_anchor_discontinuity = True
        cr._t6_drift_flag_sustained = True
        cr._t6_drift_first_breach_wall = 999.0
        with _patched_discover(200_000_000_000, 800_000):
            cr._t6_react_to_flags()
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)
        self.assertFalse(cr._t6_drift_flag_sustained)
        self.assertIsNone(cr._t6_drift_first_breach_wall)

    def test_discontinuity_bypasses_per_hour_cap(self):
        """Even after the per-hour cap is hit, a fresh discontinuity
        must still trigger re-capture — namespace changes can't be
        rate-limited away."""
        cr = _bare_recorder()
        # Saturate the history (last 60 min — use monotonic-recent values).
        now = 10_000.0
        cr._t6_recapture_wall_history.extend(
            [now - 100.0, now - 80.0, now - 60.0, now - 40.0, now - 20.0]
        )
        cr._t6_drift_flag_anchor_discontinuity = True
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=now), \
             _patched_discover(200_000_000_000, 800_000):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 1)


class TestSignalBHonoursHysteresis(unittest.TestCase):

    def test_sustained_breach_first_time_recaptures(self):
        cr = _bare_recorder()
        cr._t6_drift_flag_sustained = True
        with _patched_discover(200_000_000_000, 800_000):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 1)
        self.assertEqual(cr._t6_last_recapture_reason, 'sustained_breach')

    def test_cooldown_suppresses_immediate_retrigger(self):
        cr = _bare_recorder()
        # Prior re-capture 60s ago — well inside the 300s cooldown.
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1060.0):
            cr._t6_last_recapture_wall = 1000.0
            cr._t6_recapture_count = 1
            cr._t6_drift_flag_sustained = True
            with _patched_discover(200_000_000_000, 800_000) as disc:
                cr._t6_react_to_flags()
                disc.assert_not_called()
        self.assertEqual(cr._t6_recapture_count, 1)
        # Flag stays set so the next poll re-checks.
        self.assertTrue(cr._t6_drift_flag_sustained)

    def test_recapture_after_cooldown_elapses(self):
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1400.0):
            cr._t6_last_recapture_wall = 1000.0  # 400 s ago — past 300 s
            cr._t6_recapture_count = 1
            cr._t6_drift_flag_sustained = True
            with _patched_discover(200_000_000_000, 800_000):
                cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 2)
        self.assertFalse(cr._t6_drift_flag_sustained)

    def test_per_hour_cap_engages_for_sustained_breach(self):
        cr = _bare_recorder()
        now = 10_000.0
        # 5 recaptures already within last 60 min — at the cap.
        cr._t6_recapture_wall_history.extend(
            [now - 3000.0, now - 2500.0, now - 2000.0, now - 1500.0, now - 1000.0]
        )
        cr._t6_recapture_count = 5
        cr._t6_last_recapture_wall = now - 1000.0   # past cooldown
        cr._t6_drift_flag_sustained = True
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=now), \
             _patched_discover(200_000_000_000, 800_000) as disc:
            cr._t6_react_to_flags()
            disc.assert_not_called()
        self.assertEqual(cr._t6_recapture_count, 5)


class TestFailureModesPreserveState(unittest.TestCase):

    def test_chrony_unsettled_leaves_state_unchanged(self):
        cr = _bare_recorder()
        cr._wait_for_chrony_settled = MagicMock(return_value=False)
        cr._t6_drift_flag_anchor_discontinuity = True
        old_anchor = cr._t6_drift_anchor_gps_ns
        with _patched_discover(200_000_000_000, 800_000) as disc:
            cr._t6_react_to_flags()
            disc.assert_not_called()
        self.assertEqual(cr._t6_recapture_count, 0)
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_gps_ns, old_anchor)

    def test_discovery_failure_leaves_state_unchanged(self):
        cr = _bare_recorder()
        cr._t6_drift_flag_anchor_discontinuity = True
        old_anchor = cr._t6_drift_anchor_gps_ns
        with patch('hf_timestd.core.core_recorder_v2.discover_channels',
                   side_effect=OSError("no radiod")):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 0)
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_gps_ns, old_anchor)

    def test_ssrc_missing_in_discovery_leaves_state_unchanged(self):
        cr = _bare_recorder(ssrc=0x1234)
        cr._t6_drift_flag_anchor_discontinuity = True
        old_anchor = cr._t6_drift_anchor_gps_ns
        # discover_channels returns a different SSRC; freq mismatch too.
        other_ci = _channel_info(ssrc=0x9999, frequency=999_000_000.0)
        with patch('hf_timestd.core.core_recorder_v2.discover_channels',
                   return_value={0x9999: other_ci}):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 0)
        self.assertEqual(cr._t6_drift_anchor_gps_ns, old_anchor)

    def test_fresh_channelinfo_missing_timestamps(self):
        cr = _bare_recorder()
        cr._t6_drift_flag_anchor_discontinuity = True
        old_anchor = cr._t6_drift_anchor_gps_ns
        bad_ci = SimpleNamespace(ssrc=0x1234, gps_time=None, rtp_timesnap=None,
                                  frequency=5_000_000.0, sample_rate=24000)
        with patch('hf_timestd.core.core_recorder_v2.discover_channels',
                   return_value={0x1234: bad_ci}):
            cr._t6_react_to_flags()
        self.assertEqual(cr._t6_recapture_count, 0)
        self.assertEqual(cr._t6_drift_anchor_gps_ns, old_anchor)


class TestAnchorSwapAtomic(unittest.TestCase):

    def test_channel_info_reference_replaced_not_mutated(self):
        """The atomic-swap invariant: re-capture must NOT mutate the
        original ChannelInfo, only swap the reference.  A reader
        holding the old object should see consistent old values."""
        cr = _bare_recorder()
        original = cr._t6_channel_info
        cr._t6_drift_flag_anchor_discontinuity = True
        with _patched_discover(200_000_000_000, 800_000):
            cr._t6_react_to_flags()
        # Original object retains old values.
        self.assertEqual(original.gps_time, 100_000_000_000)
        self.assertEqual(original.rtp_timesnap, 500_000)
        # Recorder's reference now points at a different object.
        self.assertIsNot(cr._t6_channel_info, original)
        self.assertEqual(cr._t6_channel_info.gps_time, 200_000_000_000)


class TestCooldownRemainingHelper(unittest.TestCase):

    def test_returns_none_before_first_recapture(self):
        cr = _bare_recorder()
        self.assertIsNone(cr._t6_recapture_cooldown_remaining_sec())

    def test_returns_positive_within_cooldown(self):
        cr = _bare_recorder()
        cr._t6_last_recapture_wall = 1000.0
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1100.0):  # 100 s elapsed of 300 s cooldown
            self.assertAlmostEqual(
                cr._t6_recapture_cooldown_remaining_sec(), 200.0, places=1,
            )

    def test_returns_zero_after_cooldown_elapsed(self):
        cr = _bare_recorder()
        cr._t6_last_recapture_wall = 1000.0
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=2000.0):  # well past cooldown
            self.assertEqual(cr._t6_recapture_cooldown_remaining_sec(), 0.0)


if __name__ == '__main__':
    unittest.main()
