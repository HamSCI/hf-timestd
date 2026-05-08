"""Tests for the cascade-tolerance gate in BpskPpsCalibratorMF.

The original MF calibrator updated ``_last_edge_rtp`` from every
detected edge (including ones rejected as noise via the
``edge_tolerance_samples`` test).  That reproduced the legacy
calibrator's pre-cascade-protection failure mode: a single noise edge
shifts the reference, then real PPS edges land outside the new
reference's tolerance and get rejected too — calibrator can re-lock
at the noise offset.

The 2026-05-08 diagnostic capture on bee1 showed this is driven by
~10-second Costas-loop phase excursions during which apparent peak
positions slide by 100-300 ms.  With the gate, those out-of-cascade
edges still reset ``pps_consecutive`` (so the calibrator reports
unlocked while phase recovers) but leave ``_last_edge_rtp`` intact,
so once Costas re-acquires the next correctly-positioned edge resumes
normal accept-flow.

These tests pin the rule:
* ACQUIRING (no acquisition yet): every edge updates the reference —
  bootstrap walking is preserved.
* TRACKING (after first acquisition): only edges within
  ``cascade_tolerance_samples`` of the reference may update it.  Far
  noise edges still increment ``pps_noise`` and reset
  ``pps_consecutive`` but cannot move the reference.
* ``_acquired`` is sticky across cascades; only ``reset()`` clears it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF


def _make_acquired_calibrator(sample_rate: int = 96000) -> BpskPpsCalibratorMF:
    """Build a calibrator that's already in TRACKING with a known
    reference, by directly priming the state.  Avoids having to
    synthesise a real BPSK signal in every test."""
    cal = BpskPpsCalibratorMF(
        sample_rate=sample_rate,
        consecutive_required=10,
        edge_tolerance_samples=30,
        cascade_tolerance_ms=3.0,
    )
    cal._last_edge_rtp = 1_000_000
    cal.pps_consecutive = cal.consecutive_required
    cal._acquired = True
    cal._peak_running = 100.0
    return cal


def _drive_one_peak(cal: BpskPpsCalibratorMF, peak_offset_in_window: int,
                    rtp_at_peak: int, magnitude: float = 100.0) -> None:
    """Synthesise a single y/rtp window with one local-max peak at the
    chosen offset and call ``_detect_and_record_peaks`` directly so
    we don't have to construct IQ samples."""
    window_len = 1024
    y = np.zeros(window_len, dtype=np.float64)
    # Triangular peak so parabolic interp gets a clean apex.
    pi = peak_offset_in_window
    for k in range(-3, 4):
        idx = pi + k
        if 0 <= idx < window_len:
            y[idx] = magnitude * (1.0 - 0.05 * abs(k))
    rtp = (np.arange(window_len, dtype=np.int64)
           - pi + rtp_at_peak) & 0xFFFFFFFF
    cal._detect_and_record_peaks(y, rtp)


class TestCascadeGate(unittest.TestCase):

    def test_far_noise_edge_does_not_move_reference_when_acquired(self):
        cal = _make_acquired_calibrator()
        original_ref = cal._last_edge_rtp
        # Drive a noise edge 100 ms (9_600 samples) away within the
        # second — way beyond cascade_tolerance (288 samples ≈ 3 ms).
        sr = cal.sample_rate
        # Place the noise edge 1.05 s after the reference so the
        # 0.99-s short_gap check passes, with a within-second offset
        # that's 100 ms off (9_600 samples).
        noise_rtp = original_ref + int(1.05 * sr) + 9_600
        _drive_one_peak(cal, peak_offset_in_window=100,
                        rtp_at_peak=noise_rtp, magnitude=120.0)
        # Edge was rejected (noise) and reference held.
        self.assertEqual(cal.pps_consecutive, 0)
        self.assertEqual(cal._last_edge_rtp, original_ref)
        self.assertGreaterEqual(cal.pps_noise, 1)

    def test_within_cascade_tolerance_edge_updates_reference(self):
        cal = _make_acquired_calibrator()
        original_ref = cal._last_edge_rtp
        sr = cal.sample_rate
        # Place a noise edge exactly 1 s + 50 samples after the
        # reference.  The 1 s × sr = 96_000 sample offset is divisible
        # by sample_rate, so the within-second-offset delta `d` is just
        # the +50 sample residual.  That's > edge_tolerance (30) but
        # ≤ cascade_tolerance (288 = 3 ms × 96 kHz).  Expect:
        # consecutive resets (the edge is rejected as noise) but
        # reference DOES update because the drift is small enough to
        # be legitimate post-acquisition tracking.
        noise_rtp = original_ref + sr + 50
        _drive_one_peak(cal, peak_offset_in_window=100,
                        rtp_at_peak=noise_rtp, magnitude=120.0)
        self.assertEqual(cal.pps_consecutive, 0)
        self.assertEqual(cal._last_edge_rtp, noise_rtp)

    def test_acquiring_state_walks_reference_freely(self):
        cal = BpskPpsCalibratorMF(
            sample_rate=96000,
            consecutive_required=10,
            edge_tolerance_samples=30,
            cascade_tolerance_ms=3.0,
        )
        cal._last_edge_rtp = 1_000_000
        cal._peak_running = 100.0
        # Not yet acquired (pps_consecutive < required).  A noise edge
        # 100 ms off SHOULD update the reference (acquisition bootstrap).
        sr = cal.sample_rate
        noise_rtp = cal._last_edge_rtp + int(1.05 * sr) + 9_600
        _drive_one_peak(cal, peak_offset_in_window=100,
                        rtp_at_peak=noise_rtp, magnitude=120.0)
        self.assertFalse(cal._acquired)
        self.assertEqual(cal._last_edge_rtp, noise_rtp)

    def test_acquired_flag_is_sticky_across_cascades(self):
        cal = _make_acquired_calibrator()
        sr = cal.sample_rate
        # Burst of far-out noise edges that would, in the old code,
        # reset the reference.  Acquired must remain True throughout.
        for k in range(20):
            noise_rtp = cal._last_edge_rtp + int(1.05 * sr) + 9_600 + k * 17
            _drive_one_peak(cal, peak_offset_in_window=100,
                            rtp_at_peak=noise_rtp, magnitude=120.0)
        self.assertTrue(cal._acquired)
        # Reference unchanged across the whole burst.
        self.assertEqual(cal._last_edge_rtp, 1_000_000)

    def test_reset_clears_acquired_flag(self):
        cal = _make_acquired_calibrator()
        self.assertTrue(cal._acquired)
        cal.reset()
        self.assertFalse(cal._acquired)
        self.assertIsNone(cal._last_edge_rtp)


if __name__ == '__main__':
    unittest.main()
