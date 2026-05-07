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


if __name__ == '__main__':
    unittest.main()
