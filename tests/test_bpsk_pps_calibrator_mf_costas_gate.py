"""Tests for the Costas lock-quality gate in BpskPpsCalibratorMF.

Layer A of the TSL3 Costas-drift fix (docs/TSL3_COSTAS_DRIFT_2026-05-18.md).
The BPSK calibrator's carrier-recovery loop makes intermittent ~10-15 s
phase excursions; during one the matched filter throws strong phantom
peaks that, unguarded, walk the edge reference and re-lock TSL3 biased.

The fix adds a ``costas_locked`` quality signal and, once acquired, gates
edge acceptance on it: an excursion makes the calibrator *coast* on the
last-good chain delay (a brief holdover) instead of re-locking against a
phantom.

These tests pin three things:
* the detector — ``_update_costas_lock`` flips ``costas_locked`` from the
  phase motion (|Δφ| EMA) and band (|φ − φ_ema|) tests, with a debounce;
* the gate — once acquired, an unlocked Costas loop coasts: no edge is
  accepted, no lock state moves, the last-good result keeps flowing;
* the wiring — a real swept-carrier signal through ``process_samples``
  unlocks the loop and TSL3 holds rather than dropping out.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.bpsk_pps_calibrator_mf import (  # noqa: E402
    BpskPpsCalibratorMF,
    COSTAS_DPHASE_MAX_RAD,
)


SR = 96_000


# --------------------------------------------------------------------------
# Detector — white-box tests on _update_costas_lock
# --------------------------------------------------------------------------

def _detector_calibrator(batch_dt: float = 0.02) -> BpskPpsCalibratorMF:
    """A calibrator with the Costas-lock coefficients pinned for a known
    per-batch dt, so the detector can be driven directly with synthetic
    phase increments (no IQ synthesis needed)."""
    cal = BpskPpsCalibratorMF(sample_rate=SR)
    cal._alpha = 1.0
    cal._costas_phase_ema_alpha = float(1.0 - np.exp(-batch_dt / 10.0))
    cal._costas_dphase_ema_alpha = float(1.0 - np.exp(-batch_dt / 0.5))
    cal._costas_relock_batches = max(1, int(np.ceil(0.5 / batch_dt)))
    cal._phase_initialized = True
    return cal


def _drive_detector(cal: BpskPpsCalibratorMF, increment: float, n: int) -> None:
    """Advance the Costas phase by ``increment`` per batch for ``n``
    batches, feeding each increment through the lock-quality detector."""
    for _ in range(n):
        cal._phase += increment
        cal._update_costas_lock(increment)


class TestCostasLockDetector(unittest.TestCase):

    def test_quiescent_loop_locks_after_debounce(self):
        cal = _detector_calibrator()
        self.assertFalse(cal.costas_locked)
        # One batch short of the debounce — not yet locked.
        _drive_detector(cal, 0.0, cal._costas_relock_batches - 1)
        self.assertFalse(cal.costas_locked)
        # The debounce-completing batch flips it.
        _drive_detector(cal, 0.0, 1)
        self.assertTrue(cal.costas_locked)

    def test_excursion_unlocks(self):
        cal = _detector_calibrator()
        _drive_detector(cal, 0.0, cal._costas_relock_batches)
        self.assertTrue(cal.costas_locked)
        # A sustained per-batch phase slew — the motion test trips.
        _drive_detector(cal, 0.15, 5)
        self.assertFalse(cal.costas_locked)

    def test_band_test_holds_unlocked_through_a_plateau(self):
        """A phase that has wandered far must not re-validate just
        because the loop momentarily stops moving — the band test keeps
        it unlocked even after the motion test has recovered."""
        cal = _detector_calibrator()
        _drive_detector(cal, 0.0, cal._costas_relock_batches)
        # Excursion: φ slews ~4 rad away, freezing the band EMA near home.
        _drive_detector(cal, 0.1, 40)
        self.assertFalse(cal.costas_locked)
        # Plateau: the loop is now stationary, but at the far-off phase.
        _drive_detector(cal, 0.0, 250)
        # Motion test has recovered ...
        self.assertLessEqual(cal._dphase_ema, COSTAS_DPHASE_MAX_RAD)
        # ... but the band test still (correctly) holds it unlocked.
        self.assertFalse(cal.costas_locked)

    def test_relocks_after_phase_returns_home(self):
        cal = _detector_calibrator()
        _drive_detector(cal, 0.0, cal._costas_relock_batches)
        _drive_detector(cal, 0.1, 30)      # excursion out
        self.assertFalse(cal.costas_locked)
        _drive_detector(cal, -0.1, 30)     # phase ramps back home
        # Still unlocked while the loop settles.
        self.assertFalse(cal.costas_locked)
        _drive_detector(cal, 0.0, 300)     # quiescent at home
        self.assertTrue(cal.costas_locked)


# --------------------------------------------------------------------------
# Gate — tests on _detect_and_record_peaks
# --------------------------------------------------------------------------

def _make_acquired_calibrator(costas_locked: bool) -> BpskPpsCalibratorMF:
    """A calibrator primed into the acquired/TRACKING state with a known
    edge reference, and with the Costas loop locked or unlocked."""
    cal = BpskPpsCalibratorMF(
        sample_rate=SR,
        consecutive_required=10,
        edge_tolerance_samples=30,
        cascade_tolerance_ms=3.0,
    )
    cal._last_edge_rtp = 1_000_000
    cal.pps_consecutive = cal.consecutive_required
    cal.pps_ok = 42
    cal._acquired = True
    cal._peak_running = 100.0
    cal._chain_delay_samples = float(1_000_000 % SR)
    cal._costas_locked = costas_locked
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


class TestCostasEdgeGate(unittest.TestCase):

    def test_locked_loop_accepts_a_good_edge(self):
        """Baseline: with the Costas loop locked, an on-time edge is
        accepted exactly as before the fix."""
        cal = _make_acquired_calibrator(costas_locked=True)
        good_rtp = cal._last_edge_rtp + SR  # exactly one second later
        _drive_one_peak(cal, rtp_at_peak=good_rtp)
        self.assertEqual(cal._last_edge_rtp, good_rtp)
        self.assertEqual(cal.pps_consecutive, cal.consecutive_required + 1)
        self.assertEqual(cal.pps_ok, 43)

    def test_unlocked_loop_ignores_a_good_edge_and_coasts(self):
        """With the Costas loop unlocked the calibrator coasts: even a
        perfectly on-time edge is not accepted, and no lock state moves."""
        cal = _make_acquired_calibrator(costas_locked=False)
        ref0, consec0 = cal._last_edge_rtp, cal.pps_consecutive
        ok0, noise0 = cal.pps_ok, cal.pps_noise
        delay0 = cal._chain_delay_samples
        good_rtp = cal._last_edge_rtp + SR
        _drive_one_peak(cal, rtp_at_peak=good_rtp)
        self.assertEqual(cal._last_edge_rtp, ref0)
        self.assertEqual(cal.pps_consecutive, consec0)
        self.assertEqual(cal.pps_ok, ok0)
        self.assertEqual(cal.pps_noise, noise0)
        self.assertEqual(cal._chain_delay_samples, delay0)

    def test_unlocked_loop_does_not_let_a_phantom_walk_the_reference(self):
        """The core failure mode: a phantom peak during an excursion must
        not walk _last_edge_rtp (which is what re-locked TSL3 biased)."""
        cal = _make_acquired_calibrator(costas_locked=False)
        ref0 = cal._last_edge_rtp
        # Phantom ~1.1 ms off — inside cascade_tolerance, so without the
        # Costas gate it would walk the reference.
        phantom_rtp = cal._last_edge_rtp + SR + 106
        _drive_one_peak(cal, rtp_at_peak=phantom_rtp)
        self.assertEqual(cal._last_edge_rtp, ref0)
        self.assertEqual(cal.pps_noise, 0)
        self.assertEqual(cal.pps_consecutive, cal.consecutive_required)

    def test_coast_keeps_the_last_good_result_flowing(self):
        """While coasting, process_samples' result path still reports the
        last-good chain delay — TSL3 holds instead of dropping out."""
        cal = _make_acquired_calibrator(costas_locked=False)
        before = cal._maybe_result()
        self.assertIsNotNone(before)
        self.assertTrue(before.locked)
        _drive_one_peak(cal, rtp_at_peak=cal._last_edge_rtp + SR + 9_600)
        after = cal._maybe_result()
        self.assertIsNotNone(after)
        self.assertTrue(after.locked)
        self.assertEqual(after.chain_delay_samples, before.chain_delay_samples)

    def test_gate_is_inert_during_acquisition(self):
        """Before acquisition the gate must not fire — the bootstrap has
        to be free to walk the reference even with the loop unsettled."""
        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=10, edge_tolerance_samples=30,
        )
        cal._last_edge_rtp = 1_000_000
        cal._peak_running = 100.0
        cal._costas_locked = False
        self.assertFalse(cal._acquired)
        walked_rtp = cal._last_edge_rtp + int(1.05 * SR) + 9_600
        _drive_one_peak(cal, rtp_at_peak=walked_rtp)
        # Acquiring bootstrap still walked the reference.
        self.assertEqual(cal._last_edge_rtp, walked_rtp)


# --------------------------------------------------------------------------
# Wiring — integration through process_samples with a swept carrier
# --------------------------------------------------------------------------

def _make_swept_bpsk(carrier_phase: np.ndarray,
                     edge_offset_samples: float = 10.0) -> np.ndarray:
    """Polarity-flip BPSK at DC with a per-sample carrier phase, so the
    Costas loop can be driven through a carrier-recovery excursion."""
    n = len(carrier_phase)
    t = np.arange(n)
    nearest_k = np.round((t - edge_offset_samples) / SR).astype(np.int64)
    nearest_edge = nearest_k * SR + edge_offset_samples
    sign = np.where(nearest_k % 2 == 0, +1.0, -1.0)
    polarity = sign * np.tanh((t - nearest_edge) / 2.0)
    return (polarity * np.exp(1j * carrier_phase)).astype(np.complex64)


class TestCostasGateIntegration(unittest.TestCase):

    def test_swept_carrier_unlocks_loop_and_tsl3_holds(self):
        # 16 s: 8 s steady (lock + acquire), 4 s carrier-phase excursion,
        # 4 s steady (recover).
        n = 16 * SR
        phase = np.full(n, 0.5)
        exc = np.arange(8 * SR, 12 * SR)
        # Smooth 4-rad bump — peak slew ~2 rad/s, well past the detector's
        # ~0.8 rad/s motion floor at this batch size.
        phase[exc] = 0.5 + 4.0 * np.sin(np.linspace(0, np.pi, len(exc))) ** 2
        signal = _make_swept_bpsk(phase)

        cal = BpskPpsCalibratorMF(
            sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
        )
        batch = 480
        seen = []  # (sample_index, costas_locked, result)
        rtp = 0
        for i in range(0, len(signal), batch):
            r = cal.process_samples(signal[i:i + batch], rtp)
            seen.append((i, cal.costas_locked, r))
            rtp = (rtp + batch) & 0xFFFFFFFF

        def window(lo_s, hi_s):
            return [s for s in seen if lo_s * SR <= s[0] < hi_s * SR]

        # Acquired and Costas-locked during the first steady stretch.
        pre = window(6, 8)
        self.assertTrue(all(s[1] for s in pre),
                        "Costas loop should be locked before the excursion")
        pre_locked = [s[2] for s in pre if s[2] is not None]
        self.assertTrue(pre_locked and all(r.locked for r in pre_locked))
        chain_delay = pre_locked[-1].chain_delay_samples

        # The excursion unlocks the loop.
        self.assertTrue(any(not s[1] for s in window(8, 12)),
                        "carrier excursion should unlock the Costas loop")

        # Through the excursion TSL3 coasts: every result still locked,
        # and the chain delay never moves off its pre-excursion value.
        for _, _, r in seen:
            if r is not None:
                self.assertTrue(r.locked, "TSL3 must not drop out — coast")
                self.assertAlmostEqual(r.chain_delay_samples, chain_delay,
                                       delta=1.0)

        # The loop re-locks once the carrier settles again.
        self.assertTrue(seen[-1][1],
                        "Costas loop should re-lock after recovery")


if __name__ == '__main__':
    unittest.main()
