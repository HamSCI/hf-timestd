"""Regression tests for P-H3/H4/H7 in carrier_tec.py.

P-H3 — the "phase unwrapping quality check" comment claimed the check could
detect wrong-branch unwrapping. It cannot: once the phase is sampled, a true
inter-tick step >π aliases to a small wrapped diff, so the failure is invisible.
`unwrap_quality` / `n_phase_jumps` are a RISK score — post-unwrap steps whose
magnitude approaches the π Nyquist boundary — not proof of a bad unwrap. The
comment is fixed; the computation is unchanged and is exercised here.

P-H4 — cycle slips were frozen (doppler→0 through the slip) but never counted,
so a slip-contaminated series looked clean. `CarrierTECResult` now carries
`n_cycle_slips` for consumers to gate on.

P-H7 — integrated dTEC is a random-walk cumulative sum; its 1σ grows as √N
with the number of integration steps. `_estimate_noise_floor` used to return
0.0 ("perfect") when it could not estimate the floor — now NaN ("unknown") —
and `sigma_dtec_tecu` is that per-tick floor propagated to the
end-of-integration uncertainty (floor·√N), NaN when the floor is unknown.
"""

import math
import unittest

import numpy as np

from hf_timestd.core.carrier_tec import CarrierTECEstimator, CarrierTECResult


class TestUnwrapRiskIndicator(unittest.TestCase):
    """P-H3: unwrap_quality / n_phase_jumps as a risk score."""

    def test_clean_phase_has_unit_unwrap_quality(self) -> None:
        epochs = np.arange(120, dtype=float)
        phase = 0.005 * epochs  # gentle ramp, every step well below π/2
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.n_phase_jumps, 0)
        self.assertEqual(result.unwrap_quality, 1.0)

    def test_large_steps_flag_unwrap_risk(self) -> None:
        """Steps near the π boundary raise n_phase_jumps and drop the score
        into [0, 1) — flagging risk, not confirming a bad unwrap."""
        epochs = np.arange(30, dtype=float)
        phase = np.zeros(30)
        for i in (5, 10, 15, 20, 25):  # ten ±2.0-rad raw steps (|·| > π/2)
            phase[i] = 2.0
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.n_phase_jumps, 10)
        self.assertAlmostEqual(result.unwrap_quality, 1.0 - 10 / 29, places=9)
        self.assertGreaterEqual(result.unwrap_quality, 0.0)
        self.assertLess(result.unwrap_quality, 1.0)


class TestCycleSlipCount(unittest.TestCase):
    """P-H4: cycle slips are counted onto the result."""

    def test_clean_series_reports_zero_slips(self) -> None:
        epochs = np.arange(120, dtype=float)
        phase = 0.005 * epochs
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertIsInstance(result.n_cycle_slips, int)
        self.assertEqual(result.n_cycle_slips, 0)

    def test_phase_rate_spike_is_counted_as_a_slip(self) -> None:
        """A fast-cadence zig-zag drives the phase-acceleration past the
        5 Hz/s slip threshold; the slip must be counted, not silently frozen."""
        epochs = np.arange(50, dtype=float) * 0.1  # 10 Hz cadence
        phase = np.zeros(50)
        phase[25] = 3.0  # single-tick excursion -> |Δdoppler| ≈ 9.5 Hz/s
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result.n_cycle_slips, 1)

    def test_field_default_is_zero(self) -> None:
        result = CarrierTECResult(
            station='WWV', channel='ch', frequency_mhz=10.0,
            start_epoch=0.0, end_epoch=1.0)
        self.assertEqual(result.n_cycle_slips, 0)


class TestDtecUncertainty(unittest.TestCase):
    """P-H7: sigma_dtec_tecu is the noise floor propagated by √N; the floor
    is NaN, never 0.0, when it cannot be estimated."""

    def test_noise_floor_returns_nan_when_indeterminate(self) -> None:
        floor = CarrierTECEstimator._estimate_noise_floor

        # Too few points.
        self.assertTrue(math.isnan(floor(np.arange(5, dtype=float), np.zeros(5))))
        # Non-positive cadence (duplicate epochs -> median dt 0).
        self.assertTrue(math.isnan(floor(np.zeros(20), np.zeros(20))))
        # Enough points and cadence, but no window fits the 60 s span.
        short_span = np.arange(20, dtype=float) * 0.1
        self.assertTrue(math.isnan(floor(short_span, np.zeros(20))))

    def test_sigma_is_nan_for_short_series(self) -> None:
        """A series too short for a noise-floor estimate yields an unknown
        (NaN) uncertainty — never a spurious, confident 0.0."""
        epochs = np.arange(8, dtype=float)
        phase = 0.01 * epochs
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertTrue(math.isnan(result.sigma_dtec_tecu))

    def test_sigma_is_floor_propagated_by_sqrt_n(self) -> None:
        """sigma_dtec_tecu == per-tick floor · √N (random-walk growth)."""
        rng = np.random.default_rng(42)
        n = 400
        epochs = np.arange(n, dtype=float)
        phase = 0.002 * epochs + rng.normal(0.0, 0.05, n)
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)

        floor = CarrierTECEstimator._estimate_noise_floor(
            np.asarray(result.epochs), np.asarray(result.dtec_tecu))
        self.assertTrue(math.isfinite(floor))
        self.assertGreater(floor, 0.0)
        self.assertAlmostEqual(result.sigma_dtec_tecu,
                               floor * math.sqrt(result.n_points), places=9)

    def test_sigma_grows_with_integration_length(self) -> None:
        """For one noise process, a longer integration carries a larger 1σ."""
        rng = np.random.default_rng(7)
        n = 800
        epochs = np.arange(n, dtype=float)
        phase = 0.001 * epochs + rng.normal(0.0, 0.04, n)
        est = CarrierTECEstimator()
        short = est.compute_dtec_from_phase(epochs[:120], phase[:120], 10.0)
        long = est.compute_dtec_from_phase(epochs[:480], phase[:480], 10.0)
        self.assertIsNotNone(short)
        self.assertIsNotNone(long)
        self.assertTrue(math.isfinite(short.sigma_dtec_tecu))
        self.assertTrue(math.isfinite(long.sigma_dtec_tecu))
        self.assertGreater(long.sigma_dtec_tecu, short.sigma_dtec_tecu)


if __name__ == '__main__':
    unittest.main()
