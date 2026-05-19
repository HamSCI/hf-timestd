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

P-H7 / P-M3 — `_estimate_noise_floor` returns NaN ("unknown"), never 0.0
("perfect"), when it cannot estimate the floor. P-M3 then changed the dTEC
computation to be DIRECT from unwrapped phase rather than a re-integrated
Doppler rate, so the series is no longer a random-walk cumulative sum:
`sigma_dtec_tecu` is now the per-sample noise floor itself, constant — there
is no √N growth to propagate.
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
    """P-H7 / P-M3: sigma_dtec_tecu is the per-sample dTEC noise floor —
    constant, not √N-propagated — and NaN, never 0.0, when indeterminate."""

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

    def test_sigma_is_the_per_sample_noise_floor(self) -> None:
        """P-M3: dTEC is direct from phase, not a re-integrated random walk —
        sigma_dtec_tecu is the per-sample noise floor itself, with no √N
        propagation."""
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
        self.assertAlmostEqual(result.sigma_dtec_tecu, floor, places=9)

    def test_sigma_does_not_grow_with_integration_length(self) -> None:
        """P-M3: per-sample dTEC noise is a property of the measurement, not
        the record length — a longer record does NOT inflate it. The old
        re-integration model grew σ as √N (√4 = 2× over this 4× span)."""
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
        ratio = long.sigma_dtec_tecu / short.sigma_dtec_tecu
        self.assertLess(ratio, 1.5)     # nowhere near the old √4 = 2×
        self.assertGreater(ratio, 0.67)


class TestDirectFromPhase(unittest.TestCase):
    """P-M3: relative TEC is computed directly from unwrapped phase."""

    def test_dtec_series_on_the_sample_grid(self) -> None:
        # Output is on the carrier-phase sample grid (length N), not the
        # N-1 midpoint grid the old Doppler-integration produced.
        epochs = np.arange(60, dtype=float)
        phase = 0.01 * epochs  # clean ramp, no slips, no gaps
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.epochs), 60)
        self.assertEqual(len(result.dtec_tecu), 60)
        self.assertEqual(len(result.dtec_rate_tecu_per_s), 60)
        self.assertEqual(result.n_points, 60)
        self.assertEqual(result.n_gaps, 0)
        # Relative TEC is anchored to 0 at the first sample and, for a clean
        # monotonic phase ramp, is itself monotonic.
        dtec = np.asarray(result.dtec_tecu)
        self.assertAlmostEqual(dtec[0], 0.0, places=12)
        self.assertTrue(np.all(np.diff(dtec) < 0) or np.all(np.diff(dtec) > 0))

    def test_gap_is_coasted_and_counted(self) -> None:
        # A >120 s data gap with a large phase step across it: the step is
        # not a real ionospheric change, so dTEC must coast flat across the
        # gap and the gap must be counted.
        epochs = np.concatenate([np.arange(30, dtype=float),
                                 200.0 + np.arange(30, dtype=float)])
        phase = 0.01 * np.arange(60, dtype=float)
        phase[30:] += 50.0  # carrier wound unobserved during the gap
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.n_gaps, 1)
        dtec = np.asarray(result.dtec_tecu)
        # The 50-rad jump across the gap was removed — dTEC steps across the
        # gap by ~one ordinary sample, not by the 50-rad winding.
        ordinary_step = abs(dtec[1] - dtec[0])
        self.assertLess(abs(dtec[30] - dtec[29]), 5.0 * ordinary_step + 1e-9)


class TestDifferentialDtec(unittest.TestCase):
    """P-M4: differential dTEC mean-removes each series first."""

    def test_constant_offset_removed_before_differencing(self) -> None:
        # Two relative dTEC series with identical variation but different
        # arbitrary offsets. After mean-removal the differential is ~0 —
        # without it the series would carry the 5 TECU offset difference.
        epochs = list(range(20))
        signal = [math.sin(i / 3.0) for i in range(20)]
        r1 = CarrierTECResult(
            station='WWV', channel='a', frequency_mhz=10.0,
            start_epoch=0.0, end_epoch=19.0, epochs=list(map(float, epochs)),
            dtec_tecu=[s + 5.0 for s in signal], n_points=20)
        r2 = CarrierTECResult(
            station='WWV', channel='b', frequency_mhz=15.0,
            start_epoch=0.0, end_epoch=19.0, epochs=list(map(float, epochs)),
            dtec_tecu=list(signal), n_points=20)
        out = CarrierTECEstimator().compute_differential_dtec(r1, r2)
        self.assertIsNotNone(out)
        self.assertLess(max(abs(v) for v in out['dtec_diff_tecu']), 1e-9)


class TestAnchoring(unittest.TestCase):
    """P-M5: anchor epoch-tolerance check and uncertainty propagation."""

    def _phase(self, n=60):
        epochs = np.arange(n, dtype=float)
        return epochs, 0.01 * epochs

    def test_anchor_within_tolerance_is_applied(self) -> None:
        epochs, phase = self._phase()
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0,
            anchor_tec_tecu=30.0, anchor_epoch=30.0)
        self.assertIsNotNone(result)
        self.assertTrue(result.is_anchored)
        # The sample nearest the anchor epoch carries the anchor TEC.
        self.assertAlmostEqual(result.dtec_tecu[30], 30.0, places=6)

    def test_stale_anchor_is_rejected(self) -> None:
        # Anchor epoch hours away from the data — argmin would still find a
        # nearest sample; the tolerance check must reject it instead.
        epochs, phase = self._phase()
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0,
            anchor_tec_tecu=30.0, anchor_epoch=10000.0)
        self.assertIsNotNone(result)
        self.assertFalse(result.is_anchored)

    def test_anchor_uncertainty_is_stored(self) -> None:
        epochs, phase = self._phase()
        result = CarrierTECEstimator().compute_dtec_from_phase(
            epochs, phase, frequency_mhz=10.0,
            anchor_tec_tecu=30.0, anchor_epoch=30.0,
            anchor_uncertainty_tecu=2.5)
        self.assertIsNotNone(result)
        self.assertTrue(result.is_anchored)
        self.assertAlmostEqual(result.anchor_uncertainty_tecu, 2.5, places=9)


if __name__ == '__main__':
    unittest.main()
