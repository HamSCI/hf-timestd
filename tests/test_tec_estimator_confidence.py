"""Regression tests for P-H1/H2/H5/H6 in tec_estimator.py.

P-H2 — `confidence` was r² (fit-to-a-line), which sits near 1 even when the
fitted 1/f² slope is pure noise. It is now slope detectability,
`1 − σ_slope/slope`, and `TECResult` carries `tec_uncertainty_tecu`.

P-H6 — `frequency_hz` is now validated (finite, in the HF band) before it
reaches the 1/f² term; a 0/NaN frequency used to poison the whole polyfit.

P-H5 — a negative slope is retained with `confidence=0` (already fixed per
contract decision CR-2); guarded here so it cannot regress to returning None.

(P-H1 is a docstring honesty section — not unit-testable.)
"""

import unittest

import numpy as np

from hf_timestd.core.tec_estimator import (
    K_IONOSPHERE,
    TECU_SCALE,
    TECEstimator,
)

_FREQS = [2.5e6, 5e6, 10e6, 15e6, 20e6, 25e6]


def _measurements(tec_u, freqs=_FREQS, noise_ms=0.0, u_ms=0.1, seed=1):
    """Synthetic D_clock measurements lying on a 1/f² line for `tec_u`,
    optionally with Gaussian timing noise."""
    rng = np.random.default_rng(seed)
    slope = tec_u * TECU_SCALE * K_IONOSPHERE
    out = []
    for f in freqs:
        ideal_ms = (slope / f ** 2) * 1000.0
        noise = rng.normal(0.0, noise_ms) if noise_ms else 0.0
        out.append({'frequency_hz': f, 'toa_ms': 3.0 + ideal_ms + noise,
                    'uncertainty_ms': u_ms})
    return out


class TestConfidenceIsSlopeDetectability(unittest.TestCase):

    def test_clean_strong_signal_gives_high_confidence(self) -> None:
        r = TECEstimator().estimate_tec(
            _measurements(50.0, noise_ms=0.0, u_ms=0.05), "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertGreater(r.confidence, 0.9)
        self.assertAlmostEqual(r.tec_u, 50.0, places=3)

    def test_pure_noise_confidence_is_low_on_average(self) -> None:
        # No real slope — only timing noise. r²-based confidence (the P-H2
        # bug) would average ~1 here, since the noise still fits *a* line;
        # slope-detectability confidence averages near zero. Averaged over
        # many realisations because a single noise draw can, by chance, look
        # marginally detectable.
        confs = [
            TECEstimator().estimate_tec(
                _measurements(0.0, noise_ms=3.0, u_ms=3.0, seed=s),
                "WWV", 0.0).confidence
            for s in range(40)
        ]
        self.assertLess(float(np.mean(confs)), 0.2)

    def test_tec_uncertainty_is_populated(self) -> None:
        r = TECEstimator().estimate_tec(
            _measurements(20.0, noise_ms=0.5, u_ms=1.0, seed=3), "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertTrue(np.isfinite(r.tec_uncertainty_tecu))
        self.assertGreater(r.tec_uncertainty_tecu, 0.0)


class TestFrequencyValidation(unittest.TestCase):

    def test_invalid_frequency_is_skipped(self) -> None:
        good = _measurements(20.0, freqs=[5e6, 10e6, 15e6], noise_ms=0.0)
        meas = [{'frequency_hz': 0.0, 'toa_ms': 3.0, 'uncertainty_ms': 1.0}] + good
        r = TECEstimator().estimate_tec(meas, "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.n_frequencies, 3)   # the freq=0 measurement dropped

    def test_all_invalid_frequencies_returns_none(self) -> None:
        meas = [{'frequency_hz': float('nan'), 'toa_ms': 3.0,
                 'uncertainty_ms': 1.0}] * 3
        self.assertIsNone(TECEstimator().estimate_tec(meas, "WWV", 0.0))


class TestOutlierRejection(unittest.TestCase):
    """P-M1: MAD outlier rejection must not degenerate at small N."""

    def test_n3_with_outlier_does_not_collapse(self) -> None:
        # 3 points, one a gross outlier. Outlier rejection is skipped at
        # N <= 3 — a 2-parameter line fit there has <= 1 residual DOF, so MAD
        # cannot tell an outlier from scatter, and rejecting could drop the
        # fit below 2 points. The estimate must still be returned.
        meas = _measurements(20.0, freqs=[5e6, 10e6, 15e6], noise_ms=0.0)
        meas[1]['toa_ms'] += 5.0  # gross outlier
        r = TECEstimator().estimate_tec(meas, "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.n_rejected, 0)      # no rejection attempted at N=3
        self.assertEqual(r.n_frequencies, 3)

    def test_outlier_rejected_one_per_pass(self) -> None:
        # 6 points, one a gross outlier with a large stated uncertainty so the
        # weighted fit is not dragged toward it (which would mask it). Exactly
        # one point is rejected — the worst — and the re-fit on the clean
        # remainder finds no more.
        meas = _measurements(20.0, noise_ms=0.0)
        meas[3]['toa_ms'] += 5.0
        meas[3]['uncertainty_ms'] = 5.0   # low weight → does not drag the fit
        r = TECEstimator().estimate_tec(meas, "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.n_rejected, 1)
        self.assertEqual(r.n_frequencies, 5)

    def test_measurement_noise_scatter_not_rejected(self) -> None:
        # P-M1 σ-floor: scatter at the measurement-uncertainty level is not
        # evidence of an outlier — nothing should be rejected.
        meas = _measurements(20.0, freqs=_FREQS[:5], noise_ms=0.8, u_ms=1.0,
                             seed=4)
        r = TECEstimator().estimate_tec(meas, "WWV", 0.0)
        self.assertIsNotNone(r)
        self.assertEqual(r.n_rejected, 0)


class TestNegativeSlopeRetained(unittest.TestCase):

    def test_negative_slope_retained_with_zero_confidence(self) -> None:
        # ToA decreasing with 1/f² ⇒ negative fitted slope ⇒ negative TEC.
        meas = [{'frequency_hz': f, 'toa_ms': 3.0 - (1e10 / f ** 2) * 1000.0,
                 'uncertainty_ms': 1.0} for f in _FREQS]
        r = TECEstimator().estimate_tec(meas, "WWV", 0.0)
        self.assertIsNotNone(r)          # retained, not discarded (CR-2 / P-H5)
        self.assertLess(r.tec_u, 0.0)
        self.assertEqual(r.confidence, 0.0)


if __name__ == '__main__':
    unittest.main()
