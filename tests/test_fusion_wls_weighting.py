"""Regression tests for Increment 3 (M-H12): inverse-variance fusion weights.

Before this fix, `_calculate_weights` claimed inverse-variance weighting but
`base_weight = 1/σ²` was constant (`uncertainty_ms` was degenerate), while SNR
was counted up to three times (snr_scale, an SNR-boosted confidence, and the
per-broadcast Kalman). The genuine per-broadcast `kalman_uncertainty_ms` went
unused.

Increment 3 makes the weight `w_i = trust_i / σ_i²` with
`σ_i = kalman_uncertainty_ms`, and the LOCKED-cycle WLS uncertainty
`max(√(1/Σw), weighted_scatter)`.
"""

import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from hf_timestd.core.multi_broadcast_fusion import (
    BroadcastMeasurement,
    MultiBroadcastFusion,
)


def _measurement(station: str, freq_mhz: float, d_clock_ms: float,
                  kalman_uncertainty_ms=1.0, snr_db: float = 25.0,
                  quality_grade: str = 'A', mode: str = '1F',
                  uncertainty_ms=None) -> BroadcastMeasurement:
    return BroadcastMeasurement(
        timestamp=time.time(),
        station=station,
        frequency_mhz=freq_mhz,
        d_clock_ms=d_clock_ms,
        propagation_delay_ms=10.0,
        propagation_mode=mode,
        confidence=0.9,
        snr_db=snr_db,
        quality_grade=quality_grade,
        channel_name=f'{station}_{freq_mhz}',
        raw_arrival_time_ms=100.0,
        uncertainty_ms=uncertainty_ms,
        kalman_uncertainty_ms=kalman_uncertainty_ms,
    )


class TestInverseVarianceWeighting(unittest.TestCase):
    """`_calculate_weights` weights by 1/σ², σ = kalman_uncertainty_ms."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def _fusion(self) -> MultiBroadcastFusion:
        fusion = MultiBroadcastFusion(data_root=Path(self._td.name))
        fusion.calibration_update_count = 200  # disable bootstrap boost
        fusion.physics_model = None            # disable mode-ambiguity probe
        return fusion

    def test_weight_scales_as_inverse_variance(self) -> None:
        fusion = self._fusion()
        # Two measurements identical but for σ — 0.5 ms vs 2.0 ms.
        precise = _measurement('WWV', 10.0, 2.0, kalman_uncertainty_ms=0.5)
        noisy = _measurement('WWV', 10.0, 2.0, kalman_uncertainty_ms=2.0)
        w_precise, w_noisy = fusion._calculate_weights([precise, noisy])
        # w ∝ 1/σ² → ratio = (2.0/0.5)² = 16.
        self.assertAlmostEqual(w_precise / w_noisy, 16.0, places=6)

    def test_snr_does_not_affect_weight(self) -> None:
        """SNR lives in σ_i now — it must not re-enter the weight directly."""
        fusion = self._fusion()
        low_snr = _measurement('WWV', 10.0, 2.0, snr_db=5.0)
        high_snr = _measurement('WWV', 10.0, 2.0, snr_db=30.0)
        w_low, w_high = fusion._calculate_weights([low_snr, high_snr])
        self.assertEqual(w_low, w_high)

    def test_falls_back_to_uncertainty_then_default(self) -> None:
        fusion = self._fusion()
        # No Kalman σ, but a declared uncertainty_ms → use that.
        from_uncertainty = _measurement('WWV', 10.0, 2.0,
                                         kalman_uncertainty_ms=None,
                                         uncertainty_ms=0.5)
        # No Kalman σ and no uncertainty_ms → 1.0 ms default.
        from_default = _measurement('WWV', 10.0, 2.0,
                                    kalman_uncertainty_ms=None,
                                    uncertainty_ms=None)
        reference = _measurement('WWV', 10.0, 2.0, kalman_uncertainty_ms=0.5)
        w_unc, w_def, w_ref = fusion._calculate_weights(
            [from_uncertainty, from_default, reference])
        self.assertAlmostEqual(w_unc, w_ref, places=9)   # fell back to 0.5 ms
        self.assertAlmostEqual(w_unc / w_def, (1.0 / 0.5) ** 2, places=6)


class TestWlsUncertaintyFormula(unittest.TestCase):
    """`_wls_uncertainty` = max(formal sqrt(1/Σw), weighted scatter)."""

    def test_agreement_yields_formal_precision(self) -> None:
        # Inputs agree exactly → scatter 0 → formal sqrt(1/Σw) wins.
        u = MultiBroadcastFusion._wls_uncertainty(
            np.array([1.0, 1.0, 1.0, 1.0]), np.array([2.0, 2.0, 2.0, 2.0]), 2.0)
        self.assertAlmostEqual(u, np.sqrt(1.0 / 4.0), places=9)

    def test_disagreement_yields_weighted_scatter(self) -> None:
        # Inputs spread 0..6 about fused 3.0 → scatter sqrt(20/4)=√5 wins.
        u = MultiBroadcastFusion._wls_uncertainty(
            np.array([1.0, 1.0, 1.0, 1.0]), np.array([0.0, 2.0, 4.0, 6.0]), 3.0)
        self.assertAlmostEqual(u, np.sqrt(5.0), places=9)
        self.assertGreater(u, np.sqrt(1.0 / 4.0))  # scatter beats formal

    def test_unequal_weights(self) -> None:
        # w=[4,1], x=[0,1], fused=(4·0+1·1)/5=0.2
        # formal  = sqrt(1/5)              ≈ 0.4472
        # scatter = sqrt((4·0.04+1·0.64)/5) = 0.4
        u = MultiBroadcastFusion._wls_uncertainty(
            np.array([4.0, 1.0]), np.array([0.0, 1.0]), 0.2)
        self.assertAlmostEqual(u, np.sqrt(1.0 / 5.0), places=9)

    def test_zero_weight_sum_returns_nan(self) -> None:
        u = MultiBroadcastFusion._wls_uncertainty(
            np.array([0.0, 0.0]), np.array([1.0, 2.0]), 1.5)
        self.assertTrue(np.isnan(u))


class TestWlsUncertaintyWiredIntoFuse(unittest.TestCase):
    """A LOCKED fuse() cycle records a finite, positive WLS uncertainty."""

    def test_locked_cycle_sets_finite_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._read_latest_measurements = lambda *a, **k: [
                _measurement('WWV', 10.0, 2.0),
                _measurement('WWV', 15.0, 2.2),
                _measurement('WWVH', 10.0, 1.8),
                _measurement('CHU', 7.85, 2.1),
            ]
            result = fusion.fuse(skip_write=True)
            self.assertIsNotNone(result)
            self.assertFalse(fusion.holdover_mode)
            self.assertTrue(np.isfinite(fusion.last_valid_fusion_uncertainty))
            self.assertGreater(fusion.last_valid_fusion_uncertainty, 0.0)


class TestConvergenceTiming(unittest.TestCase):
    """The lock gate now reflects genuine per-broadcast Kalman convergence.

    Pre-Increment-3 the convergence gate used the a-priori RSS budget, so a
    cold start LOCKED on cycle 1. Now it uses the real WLS uncertainty: a cold
    cycle 1 (fresh per-broadcast Kalmans, σ≈10 ms) is honestly REACQUIRING,
    and LOCK follows once those filters converge a cycle later.
    """

    def test_cold_start_acquires_then_locks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fusion = MultiBroadcastFusion(data_root=Path(td))
            fusion._read_latest_measurements = lambda *a, **k: [
                _measurement('WWV', 10.0, 2.0),
                _measurement('WWV', 15.0, 2.2),
                _measurement('WWVH', 10.0, 1.8),
                _measurement('CHU', 7.85, 2.1),
            ]
            # Cold cycle 1: per-broadcast Kalmans unconverged → not yet LOCKED.
            first = fusion.fuse(skip_write=True)
            self.assertFalse(fusion.kalman_converged)
            self.assertNotEqual(first.kalman_state, 'LOCKED')
            # Within a few cycles the per-broadcast Kalmans converge and the
            # WLS uncertainty drops below the lock gate.
            for _ in range(5):
                latest = fusion.fuse(skip_write=True)
            self.assertTrue(fusion.kalman_converged)
            self.assertEqual(latest.kalman_state, 'LOCKED')


if __name__ == '__main__':
    unittest.main()
