"""Tests for phantom-inert tracking and genuine-step detection in
BpskPpsCalibratorMF — Part 1 of the TSL3 displaced-reference fix
(docs/TSL3_COSTAS_DRIFT_2026-05-18.md).

The bug: under RF turbulence the calibrator hopped ±100 ms phantom-grid
cells.  An off-position (phantom) edge reset ``pps_consecutive``, the
calibrator dropped ``locked``, was reset, and re-acquired blind onto a
random grid cell.

The fix replaces the old cascade-tolerance gate.  Once acquired, the true
PPS edge is GPSDO-pinned to a fixed sample-of-second, so an edge more than
``edge_tolerance_samples`` off is a phantom and is held INERT — it does
not reset ``pps_consecutive`` and does not walk ``_last_edge_rtp``.  Only
a persistent run of ``STEP_CONFIRM_EDGES`` off-position edges agreeing on
one new position is a genuine chain-delay step, which re-homes the lock.

These tests pin:
* an acquired phantom edge is inert — lock, reference and chain delay held;
* a phantom burst (incl. multi-cell) never breaks or hops the lock;
* a phantom burst interleaved with real edges never confirms a step;
* a sustained run of consistent off-position edges IS adopted as a step;
* a good on-position edge clears a partial step candidate;
* during acquisition the bootstrap still walks freely.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.bpsk_pps_calibrator_mf import (  # noqa: E402
    BpskPpsCalibratorMF,
    STEP_CONFIRM_EDGES,
)

SR = 96_000


def _make_acquired() -> BpskPpsCalibratorMF:
    """A calibrator primed into the acquired/locked tracking state with a
    known edge reference and the Costas loop locked."""
    cal = BpskPpsCalibratorMF(
        sample_rate=SR, consecutive_required=10, edge_tolerance_samples=30,
    )
    cal._last_edge_rtp = 1_000_000
    cal.pps_consecutive = cal.consecutive_required
    cal.pps_ok = 50
    cal._acquired = True
    cal._costas_locked = True
    cal._peak_running = 100.0
    cal._chain_delay_samples = float(1_000_000 % SR)
    return cal


def _drive_one_peak(cal: BpskPpsCalibratorMF, rtp_at_peak: int,
                    magnitude: float = 120.0) -> None:
    """Synthesise a single-peak y/rtp window and run the edge detector
    directly, so no IQ samples are needed."""
    window_len = 1024
    pi = 100
    y = np.zeros(window_len, dtype=np.float64)
    for k in range(-3, 4):
        idx = pi + k
        if 0 <= idx < window_len:
            y[idx] = magnitude * (1.0 - 0.05 * abs(k))
    rtp = (np.arange(window_len, dtype=np.int64) - pi + rtp_at_peak) & 0xFFFFFFFF
    cal._detect_and_record_peaks(y, rtp)


class TestPhantomInert(unittest.TestCase):

    def test_acquired_phantom_edge_is_inert(self):
        """A single off-position edge while acquired must not touch the
        lock — it is a phantom, the calibrator coasts."""
        cal = _make_acquired()
        ref0, consec0 = cal._last_edge_rtp, cal.pps_consecutive
        delay0 = cal._chain_delay_samples
        # 1 s + 100 ms (9600 samples) past the reference — a phantom-grid cell.
        _drive_one_peak(cal, rtp_at_peak=ref0 + SR + 9_600)
        self.assertEqual(cal._last_edge_rtp, ref0, "reference must hold")
        self.assertEqual(cal.pps_consecutive, consec0, "lock must hold")
        self.assertEqual(cal._chain_delay_samples, delay0)
        self.assertEqual(cal.pps_phantom, 1)
        self.assertTrue(cal.locked)

    def test_multi_cell_phantom_burst_holds_the_lock(self):
        """A long burst of phantoms across several ±100 ms grid cells must
        never break the lock or hop the reference."""
        cal = _make_acquired()
        ref0, consec0 = cal._last_edge_rtp, cal.pps_consecutive
        offsets = [9_600, 19_200, 28_800]  # +100 / +200 / +300 ms
        for i in range(40):
            off = offsets[i % len(offsets)]
            _drive_one_peak(cal, rtp_at_peak=ref0 + (i + 1) * SR + off)
        self.assertEqual(cal._last_edge_rtp, ref0, "reference never moves")
        self.assertEqual(cal.pps_consecutive, consec0, "lock never resets")
        self.assertTrue(cal.locked)
        self.assertEqual(cal.pps_phantom, 40)

    def test_good_edges_still_accepted_while_locked(self):
        """Sanity: an on-time edge is still accepted normally."""
        cal = _make_acquired()
        good_rtp = cal._last_edge_rtp + SR
        _drive_one_peak(cal, rtp_at_peak=good_rtp)
        self.assertEqual(cal._last_edge_rtp, good_rtp)
        self.assertEqual(cal.pps_consecutive, cal.consecutive_required + 1)
        self.assertEqual(cal.pps_phantom, 0)


class TestGenuineStepDetection(unittest.TestCase):

    def test_transient_burst_interrupted_by_real_edges_never_steps(self):
        """Phantoms interleaved with real edges (a phantom burst, not a
        step) must never accumulate into a confirmed step — each real
        edge clears the candidate."""
        cal = _make_acquired()
        for _ in range(STEP_CONFIRM_EDGES * 2):
            # phantom 100 ms off — reference does not move
            _drive_one_peak(cal, rtp_at_peak=cal._last_edge_rtp + SR + 9_600)
            # real on-position edge — advances and clears the candidate
            _drive_one_peak(cal, rtp_at_peak=cal._last_edge_rtp + SR)
        self.assertLess(cal._step_candidate_count, STEP_CONFIRM_EDGES)
        self.assertTrue(cal.locked)

    def test_good_edge_clears_a_partial_step_candidate(self):
        cal = _make_acquired()
        ref0 = cal._last_edge_rtp
        for i in range(10):
            _drive_one_peak(cal, rtp_at_peak=ref0 + (i + 1) * SR + 5_000)
        self.assertEqual(cal._step_candidate_count, 10)
        _drive_one_peak(cal, rtp_at_peak=ref0 + SR)  # on-position
        self.assertEqual(cal._step_candidate_count, 0)
        self.assertIsNone(cal._step_candidate_rtp)

    def test_persistent_offset_run_is_adopted_as_a_step(self):
        """A genuine chain-delay step — the old edge gone, every edge now
        at one consistent new position — is adopted after
        STEP_CONFIRM_EDGES edges, re-homing the lock."""
        cal = _make_acquired()
        ref0 = cal._last_edge_rtp
        step_off = 5_000  # new within-second position, +5000 samples
        for i in range(STEP_CONFIRM_EDGES):
            _drive_one_peak(cal, rtp_at_peak=ref0 + (i + 1) * SR + step_off)
        # The run was adopted: the lock re-homed to the new position.
        self.assertEqual(cal._last_edge_rtp % SR, (ref0 + step_off) % SR)
        self.assertAlmostEqual(
            cal._chain_delay_samples, (ref0 + step_off) % SR, delta=1.0,
        )
        self.assertTrue(cal.locked, "lock is held across a genuine step")
        self.assertEqual(cal._step_candidate_count, 0, "candidate cleared")

    def test_step_not_adopted_one_edge_early(self):
        """The run must be exactly STEP_CONFIRM_EDGES long — one edge
        short, the lock has not moved yet."""
        cal = _make_acquired()
        ref0 = cal._last_edge_rtp
        for i in range(STEP_CONFIRM_EDGES - 1):
            _drive_one_peak(cal, rtp_at_peak=ref0 + (i + 1) * SR + 5_000)
        self.assertEqual(cal._last_edge_rtp, ref0, "not adopted yet")
        self.assertEqual(cal._step_candidate_count, STEP_CONFIRM_EDGES - 1)


class TestAcquisitionUnchanged(unittest.TestCase):

    def test_acquiring_state_walks_on_off_edge(self):
        """Before acquisition the bootstrap must still walk the reference
        freely toward whatever offset it finds — the phantom-inert rule
        applies only once acquired."""
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=10, edge_tolerance_samples=30,
        )
        cal._last_edge_rtp = 1_000_000
        cal._peak_running = 100.0
        cal._costas_locked = True
        self.assertFalse(cal._acquired)
        off_rtp = cal._last_edge_rtp + SR + 9_600
        _drive_one_peak(cal, rtp_at_peak=off_rtp)
        self.assertEqual(cal._last_edge_rtp, off_rtp, "bootstrap walks")
        self.assertEqual(cal.pps_consecutive, 0)
        self.assertGreaterEqual(cal.pps_noise, 1)
        self.assertEqual(cal.pps_phantom, 0, "phantom counter is acquired-only")


if __name__ == '__main__':
    unittest.main()
