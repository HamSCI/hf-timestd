"""Tests for the T6 wrap-rejection step-recovery path.

The wrap-rejector locks in the first stable disambiguated chain_delay and
rejects any later raw value that lands more than ``WRAP_THRESHOLD_NS`` away —
that protects against the BPSK calibrator's known half-second wrap glitch.
But if the underlying raw chain_delay genuinely steps to a new operating
point, the original logic rejected forever and TSL3 silently lost reach (as
observed on bee1, 2026-05-07).  These tests pin the step-recovery rule:

* When ``T6_STEP_RECOVERY_WINDOW`` consecutive rejected raws cluster within
  ``T6_STEP_RECOVERY_TIGHT_NS`` of each other, the lock is reset so the next
  cycle hits the initial-accept branch and re-disambiguates.
* When rejected raws are scattered (genuine noise wrap, not a real step),
  recovery does NOT trigger and the locked value persists.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _make_recorder_at_locked_state(locked_ns: int, disambig_ns: int = 0):
    """Build a CoreRecorderV2 sitting at a stable lock, ready to receive
    further calibrator samples through ``_t6_on_samples``."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._use_shared_multistream = True
    cr._t6_first_sample_logged = True
    cr._t6_calibrator = MagicMock()
    cr._t6_last_chain_delay_ns = locked_ns
    cr._t6_disambiguation_ns = disambig_ns
    cr._t6_wrap_rejections = 0
    cr._t6_recent_raw = __import__('collections').deque(
        maxlen=CoreRecorderV2.T6_STEP_RECOVERY_WINDOW
    )
    cr._t6_last_locked_wall = None
    cr._t6_shm = None
    cr._t6_channel_info = None
    cr.recorders = {}
    return cr


def _calibrator_result(chain_delay_ns: int):
    return SimpleNamespace(
        locked=True,
        chain_delay_ns=chain_delay_ns,
        chain_delay_samples=0.0,
        pps_consecutive=0,
        pps_ok=0,
        pps_noise=0,
    )


def _samples_quality():
    return MagicMock(), MagicMock(last_rtp_timestamp=0)


class TestT6StepRecovery(unittest.TestCase):

    def test_consistent_rejected_raws_trigger_step_recovery(self):
        # Stable lock at 32 ms (post-disambiguation), zero disambiguation
        # offset to keep the math clear in the test.
        locked = 32_000_000
        cr = _make_recorder_at_locked_state(locked_ns=locked, disambig_ns=0)
        # New raw at 418 ms — far outside WRAP_THRESHOLD_NS (10 ms),
        # mimicking the bee1 incident.  Vary by a few hundred ns so we
        # exercise the cluster-spread check, not just identical values.
        new_raw_base = 418_000_000

        for i in range(CoreRecorderV2.T6_STEP_RECOVERY_WINDOW):
            jitter_ns = (i % 5) * 50  # 0..200 ns spread
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                new_raw_base + jitter_ns
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)

        # Recovery has fired: lock state is cleared, deque drained, and
        # the disambiguation offset reset so the next cycle re-runs the
        # full initial-accept path.
        self.assertIsNone(cr._t6_last_chain_delay_ns)
        self.assertEqual(cr._t6_disambiguation_ns, 0)
        self.assertEqual(cr._t6_wrap_rejections, 0)
        self.assertEqual(len(cr._t6_recent_raw), 0)

    def test_scattered_rejected_raws_do_not_trigger_recovery(self):
        # Same locked state, but the rejected raws are scattered way
        # beyond T6_STEP_RECOVERY_TIGHT_NS (1 ms) — genuine half-second
        # wrap noise, not a stable new operating point.
        locked = 32_000_000
        cr = _make_recorder_at_locked_state(locked_ns=locked, disambig_ns=0)
        # Alternate between two values 200 ms apart so spread > 1 ms.
        candidates = [418_000_000, 218_000_000]

        for i in range(CoreRecorderV2.T6_STEP_RECOVERY_WINDOW * 2):
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                candidates[i % len(candidates)]
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)

        # Recovery did NOT fire: lock is still in place, rejection
        # counter still climbing.
        self.assertEqual(cr._t6_last_chain_delay_ns, locked)
        self.assertGreater(cr._t6_wrap_rejections, 0)

    def test_within_tolerance_value_clears_step_recovery_buffer(self):
        # If rejections accumulate but then a within-tolerance value
        # comes in, recent_raw must be cleared — otherwise stale rejected
        # raws could combine with a future tightly-clustered burst and
        # trip recovery against the wrong baseline.
        locked = 32_000_000
        cr = _make_recorder_at_locked_state(locked_ns=locked, disambig_ns=0)
        # Push a few rejections (but not enough to trigger recovery).
        for _ in range(10):
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                418_000_000
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)
        self.assertEqual(len(cr._t6_recent_raw), 10)

        # Now a within-tolerance sample arrives.
        cr._t6_calibrator.process_samples.return_value = _calibrator_result(
            locked + 500_000  # 500 us drift, well inside 10 ms
        )
        samples, quality = _samples_quality()
        cr._t6_on_samples(samples, quality)

        self.assertEqual(len(cr._t6_recent_raw), 0)
        self.assertEqual(cr._t6_wrap_rejections, 0)


class TestT6StepRecoveryT5Sanity(unittest.TestCase):
    """The 2026-05-23 phantom-step incident: a `Lost packet recovery` gap
    of 11520 samples produced a tight 60-edge cluster ~216 ms away from
    the working lock — the matched filter's boxcar sidelobe at ±0.5 s.
    Without the T5 sanity check step-recovery accepted it as a real
    operating-point change, re-disambiguated against T4, and walked HPPS
    out to +216 ms / chrony-falseticker.

    With the T5 probe wired we cross-check the candidate against GPS
    truth (LB-1421 NMEA): if the candidate's implied effective
    chain_delay disagrees with the existing lock by more than
    ``T6_STEP_RECOVERY_T5_SANITY_NS`` (5 ms), the step is rejected and
    the lock is held.
    """

    def _make_recorder_with_t5(
        self,
        *,
        locked_ns: int,
        t5_implied_ns: float,
    ):
        """Build a step-recovery test recorder with the T5 helper stubbed
        to return ``t5_implied_ns``.  No real probe involved."""
        cr = _make_recorder_at_locked_state(locked_ns=locked_ns)
        cr._t5_implied_effective_chain_delay = MagicMock(
            return_value=t5_implied_ns
        )
        return cr

    def test_t5_lets_same_delay_step_proceed_after_rtp_realignment(self):
        # The "stream restart re-lock" case: after a radiod restart the
        # matched filter re-locks at a *different RTP-grid position*
        # but the *physical chain_delay is unchanged* (it's an RF-path
        # constant).  T5's implied effective for the new raw is the
        # same as the old lock to within sub-ms.  In this case the
        # step-recovery should PROCEED — the calibrator's raw moved
        # but the underlying truth didn't, so the disambig needs to
        # re-run to bind the new raw to the same effective.
        locked = 32_000_000
        # T5 confirms 50 µs off — well within the 5 ms sanity threshold.
        cr = self._make_recorder_with_t5(
            locked_ns=locked, t5_implied_ns=32_050_000.0
        )
        for i in range(CoreRecorderV2.T6_STEP_RECOVERY_WINDOW):
            jitter_ns = (i % 5) * 50
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                418_000_000 + jitter_ns
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)

        # Step-recovery PROCEEDED: lock cleared so initial-accept
        # re-disambiguates against T5 on the next cycle.
        self.assertIsNone(cr._t6_last_chain_delay_ns)
        self.assertEqual(cr._t6_disambiguation_ns, 0)
        self.assertEqual(len(cr._t6_recent_raw), 0)

    def test_t5_phantom_detection_holds_lock_through_packet_loss(self):
        # The 2026-05-23 bee1 incident exactly: 60-edge cluster ~216 ms
        # away from old lock, T5 (had it been available) would have
        # computed implied ~248 ms (the phantom edge's wall-clock
        # position relative to NMEA's true PPS).  Difference from old
        # locked = 216 ms >> 5 ms → REJECT.  HPPS stays at +1 ns
        # instead of walking to +216 ms.
        locked = 32_000_000
        # Mirror the bee1 incident's effective_chain_delay = 249.367 ms:
        cr = self._make_recorder_with_t5(
            locked_ns=locked, t5_implied_ns=249_367_024.0
        )
        for i in range(CoreRecorderV2.T6_STEP_RECOVERY_WINDOW):
            jitter_ns = (i % 5) * 50
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                574_919_107 + jitter_ns  # also the bee1 incident raw
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)

        # Lock retained — phantom step refused.
        self.assertEqual(cr._t6_last_chain_delay_ns, locked)
        self.assertEqual(len(cr._t6_recent_raw), 0)

    def test_t5_unavailable_falls_through_to_old_behaviour(self):
        # If T5 isn't wired (helper returns None), step-recovery must
        # behave as before — the 2026-05-23 fix is opt-in via T5,
        # never a regression.
        locked = 32_000_000
        cr = _make_recorder_at_locked_state(locked_ns=locked)
        cr._t5_implied_effective_chain_delay = MagicMock(return_value=None)

        for i in range(CoreRecorderV2.T6_STEP_RECOVERY_WINDOW):
            jitter_ns = (i % 5) * 50
            cr._t6_calibrator.process_samples.return_value = _calibrator_result(
                418_000_000 + jitter_ns
            )
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)

        self.assertIsNone(cr._t6_last_chain_delay_ns)


class TestT6StuckRecovery(unittest.TestCase):

    def test_unlocked_for_longer_than_timeout_resets_calibrator(self):
        # Set up a recorder previously locked, with last_locked_wall in
        # the past beyond T6_STUCK_TIMEOUT_SEC.
        from unittest.mock import patch
        cr = _make_recorder_at_locked_state(locked_ns=33_000_000)
        timeout = CoreRecorderV2.T6_STUCK_TIMEOUT_SEC

        # Calibrator returns None (or unlocked) — stuck state.
        cr._t6_calibrator.process_samples.return_value = None

        # Walk monotonic time forward past the timeout.  First call
        # initialises _t6_last_locked_wall to now; second call (after
        # timeout has elapsed) should trigger reset.
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic') as mock_clock:
            mock_clock.return_value = 1000.0
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)
            self.assertEqual(cr._t6_last_locked_wall, 1000.0)
            self.assertEqual(cr._t6_last_chain_delay_ns, 33_000_000)
            cr._t6_calibrator.reset.assert_not_called()

            # Jump forward past the timeout — stuck-recovery should fire.
            mock_clock.return_value = 1000.0 + timeout + 1.0
            cr._t6_on_samples(samples, quality)

        cr._t6_calibrator.reset.assert_called_once()
        self.assertIsNone(cr._t6_last_chain_delay_ns)
        self.assertEqual(cr._t6_disambiguation_ns, 0)
        self.assertEqual(cr._t6_wrap_rejections, 0)

    def test_locked_result_keeps_last_locked_wall_fresh(self):
        from unittest.mock import patch
        cr = _make_recorder_at_locked_state(locked_ns=33_000_000)
        cr._t6_calibrator.process_samples.return_value = _calibrator_result(
            33_500_000
        )
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic') as mock_clock:
            mock_clock.return_value = 2000.0
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)
            self.assertEqual(cr._t6_last_locked_wall, 2000.0)
            mock_clock.return_value = 2000.0 + CoreRecorderV2.T6_STUCK_TIMEOUT_SEC + 5.0
            # New locked sample arrives — last_locked_wall should advance,
            # NOT trigger reset.
            cr._t6_on_samples(samples, quality)
            self.assertEqual(
                cr._t6_last_locked_wall,
                2000.0 + CoreRecorderV2.T6_STUCK_TIMEOUT_SEC + 5.0,
            )
        cr._t6_calibrator.reset.assert_not_called()

    def test_no_stuck_reset_during_initial_acquisition(self):
        # Fresh recorder, never locked — _t6_last_chain_delay_ns is
        # None, so the stuck-recovery branch must NOT fire even after
        # the timeout elapses (we'd have nothing to recover from yet).
        from unittest.mock import patch
        cr = _make_recorder_at_locked_state(locked_ns=33_000_000)
        cr._t6_last_chain_delay_ns = None  # never locked
        cr._t6_calibrator.process_samples.return_value = None

        with patch('hf_timestd.core.core_recorder_v2.time.monotonic') as mock_clock:
            mock_clock.return_value = 5000.0
            samples, quality = _samples_quality()
            cr._t6_on_samples(samples, quality)
            mock_clock.return_value = 5000.0 + CoreRecorderV2.T6_STUCK_TIMEOUT_SEC + 1.0
            cr._t6_on_samples(samples, quality)

        cr._t6_calibrator.reset.assert_not_called()


if __name__ == '__main__':
    unittest.main()
