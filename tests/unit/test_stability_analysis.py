"""
Unit tests for hf_timestd.core.stability_analysis

IEEE 1139-2008 Allan deviation helpers for oscillator-stability analysis.
Tests cover:
- compute_phase_adev: empty / under-3-samples guards, default octave taus,
  explicit taus, overlapping vs non-overlapping estimators, known closed-form
  cases (constant phase → ADEV 0, linear ramp → frequency-error signature)
- compute_frequency_adev: under-2-samples guard, default and explicit taus,
  overlapping/non-overlapping, constant frequency → 0
- identify_noise_type: classification ladder for white-phase, white-freq,
  random-walk slopes; insufficient-data guard
- compute_stability_at_tau: empty arrays, in-range vs out-of-range,
  log-log interpolation
- compute_stability_metrics: dict shape, includes ADEV at standard taus
"""

import numpy as np
import pytest

from hf_timestd.core.stability_analysis import (
    compute_frequency_adev,
    compute_phase_adev,
    compute_stability_at_tau,
    compute_stability_metrics,
    identify_noise_type,
)


# =============================================================================
# compute_phase_adev
# =============================================================================


class TestComputePhaseADEV:
    def test_under_three_samples_returns_empty(self):
        taus, adev = compute_phase_adev(np.array([0.0, 1.0]), tau0=1.0)
        assert taus.size == 0
        assert adev.size == 0

    def test_constant_phase_yields_zero_adev(self):
        # Constant phase data → second differences are all zero → ADEV = 0
        phase = np.full(64, 1.0)
        taus, adev = compute_phase_adev(phase, tau0=1.0)
        assert taus.size > 0
        assert all(a == 0.0 for a in adev)

    def test_linear_ramp_phase_yields_zero_adev(self):
        # Linear ramp = constant frequency → second differences = 0 → ADEV = 0
        phase = np.arange(64, dtype=float) * 1e-9
        taus, adev = compute_phase_adev(phase, tau0=1.0)
        assert all(a == pytest.approx(0.0, abs=1e-15) for a in adev)

    def test_explicit_tau_array(self):
        phase = np.cumsum(np.random.RandomState(0).randn(256) * 1e-9)
        taus_in = np.array([1.0, 2.0, 4.0, 8.0])
        taus_out, adev = compute_phase_adev(phase, tau0=1.0, taus=taus_in)
        assert taus_out.shape == taus_in.shape
        # All ADEV values are non-negative
        assert all(a >= 0 for a in adev)

    def test_default_taus_use_octave_spacing(self):
        phase = np.cumsum(np.random.RandomState(0).randn(128) * 1e-9)
        taus, _ = compute_phase_adev(phase, tau0=1.0)
        # Octave spacing → consecutive ratio = 2.0
        ratios = taus[1:] / taus[:-1]
        assert all(r == pytest.approx(2.0) for r in ratios)

    def test_overlapping_vs_non_overlapping(self):
        np.random.seed(42)
        phase = np.cumsum(np.random.randn(128) * 1e-9)
        taus_in = np.array([1.0, 2.0, 4.0])
        _, adev_overlap = compute_phase_adev(phase, tau0=1.0, taus=taus_in,
                                              overlapping=True)
        _, adev_nonoverlap = compute_phase_adev(phase, tau0=1.0, taus=taus_in,
                                                 overlapping=False)
        # Both produce values of the same length and same order of magnitude
        assert len(adev_overlap) == len(adev_nonoverlap) == 3
        # They should be roughly the same magnitude (within 5x)
        for a, b in zip(adev_overlap, adev_nonoverlap):
            if a > 0 and b > 0:
                assert 0.2 < a / b < 5.0

    def test_tau_too_large_skipped(self):
        # tau > N/2 of phase samples is rejected silently
        phase = np.zeros(20)
        taus_in = np.array([1.0, 100.0])  # 100 way too large
        taus_out, adev = compute_phase_adev(phase, tau0=1.0, taus=taus_in)
        # Only the valid tau remains
        assert len(taus_out) == 1
        assert taus_out[0] == 1.0


# =============================================================================
# compute_frequency_adev
# =============================================================================


class TestComputeFrequencyADEV:
    def test_under_two_samples_returns_empty(self):
        taus, adev = compute_frequency_adev(np.array([1.0]), tau0=1.0)
        assert taus.size == 0
        assert adev.size == 0

    def test_constant_frequency_yields_zero_adev(self):
        freq = np.full(64, 1e-10)
        taus, adev = compute_frequency_adev(freq, tau0=1.0)
        assert taus.size > 0
        assert all(a == pytest.approx(0.0, abs=1e-20) for a in adev)

    def test_explicit_tau_array(self):
        np.random.seed(0)
        freq = np.random.randn(128) * 1e-10
        taus_in = np.array([1.0, 2.0, 4.0])
        taus_out, adev = compute_frequency_adev(freq, tau0=1.0, taus=taus_in)
        assert taus_out.shape == taus_in.shape
        assert all(a >= 0 for a in adev)

    def test_overlapping_and_non_overlapping_produce_results(self):
        np.random.seed(1)
        freq = np.random.randn(128) * 1e-10
        taus_in = np.array([1.0, 2.0])
        _, adev_overlap = compute_frequency_adev(freq, tau0=1.0,
                                                  taus=taus_in,
                                                  overlapping=True)
        _, adev_nonoverlap = compute_frequency_adev(freq, tau0=1.0,
                                                     taus=taus_in,
                                                     overlapping=False)
        assert len(adev_overlap) == len(adev_nonoverlap) == 2


# =============================================================================
# identify_noise_type
# =============================================================================


class TestIdentifyNoiseType:
    def test_insufficient_taus_returns_message(self):
        assert "Insufficient" in identify_noise_type(np.array([1.0]),
                                                      np.array([1e-10]))

    def test_white_phase_noise_slope(self):
        # ADEV ∝ τ^-1 → slope = -1.0 in log-log → "White Phase Noise"
        taus = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        adev = 1e-9 / taus  # slope = -1
        assert identify_noise_type(taus, adev) == "White Phase Noise"

    def test_white_frequency_noise_slope(self):
        # ADEV ∝ τ^0 → flat → slope = 0 → "White Frequency Noise"
        taus = np.array([1.0, 2.0, 4.0, 8.0])
        adev = np.full_like(taus, 1e-10)
        assert identify_noise_type(taus, adev) == "White Frequency Noise"

    def test_random_walk_frequency_slope(self):
        # ADEV ∝ τ^+1 → slope = +1 → "Random Walk Frequency"
        taus = np.array([1.0, 2.0, 4.0, 8.0])
        adev = 1e-12 * taus
        assert identify_noise_type(taus, adev) == "Random Walk Frequency"

    def test_filters_invalid_values_below_three_returns_insufficient(self):
        # Three of five points are invalid (NaN, 0, -1). The remaining two
        # are below the source's required-minimum of 3 → "Insufficient
        # valid data".
        taus = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        adev = np.array([1e-9, np.nan, 0.0, -1.0, 1e-10 / 16.0])
        result = identify_noise_type(taus, adev)
        assert "Insufficient" in result

    def test_filters_invalid_values_with_enough_clean_points(self):
        # Five tau points, three valid → classification proceeds
        taus = np.array([1.0, 2.0, 4.0, 8.0, 16.0])
        adev = np.array([1e-9, np.nan, 2.5e-10, 1.25e-10, 6.25e-11])
        result = identify_noise_type(taus, adev)
        # Slope between adev[0]=1e-9 at τ=1 and adev[4]=6.25e-11 at τ=16 is
        # log10(0.0625)/log10(16) = -1.0 → White Phase Noise
        assert result == "White Phase Noise"


# =============================================================================
# compute_stability_at_tau
# =============================================================================


class TestComputeStabilityAtTau:
    def test_empty_returns_none(self):
        assert compute_stability_at_tau(np.array([]), np.array([]), 1.0) is None

    def test_out_of_range_returns_none(self):
        taus = np.array([10.0, 20.0, 40.0])
        adev = np.array([1e-9, 1e-10, 1e-11])
        # Far below smallest tau
        assert compute_stability_at_tau(taus, adev, 1.0) is None
        # Far above largest tau
        assert compute_stability_at_tau(taus, adev, 1000.0) is None

    def test_close_match_returned_directly(self):
        taus = np.array([1.0, 10.0, 100.0])
        adev = np.array([1e-9, 1e-10, 1e-11])
        # Target near 1.0 → returns adev[0]
        assert compute_stability_at_tau(taus, adev, 1.0) == 1e-9

    def test_log_log_interpolation(self):
        # ADEV ∝ 1/τ → at τ = 5, expect 1e-9 / 5 = 2e-10
        taus = np.array([1.0, 10.0])
        adev = np.array([1e-9, 1e-10])
        result = compute_stability_at_tau(taus, adev, 5.0)
        # Log-log interpolation for slope=-1 → exact match
        assert result == pytest.approx(2e-10, rel=0.01)


# =============================================================================
# compute_stability_metrics
# =============================================================================


class TestComputeStabilityMetrics:
    def test_empty_phase_data(self):
        result = compute_stability_metrics(np.array([]), sample_interval=1.0)
        assert result['tau_seconds'] == []
        assert result['adev'] == []
        assert result['dominant_noise'] == 'Insufficient data'
        assert result['n_points'] == 0

    def test_full_metrics_dict_shape(self):
        np.random.seed(0)
        phase = np.cumsum(np.random.randn(2048) * 1e-9)
        result = compute_stability_metrics(phase, sample_interval=1.0)
        for key in ('tau_seconds', 'adev', 'dominant_noise', 'n_points',
                    'sample_interval', 'adev_1s', 'adev_10s', 'adev_60s',
                    'adev_100s', 'adev_1000s', 'adev_10000s'):
            assert key in result
        assert result['n_points'] == 2048
        assert result['sample_interval'] == 1.0
        assert isinstance(result['tau_seconds'], list)
        assert isinstance(result['adev'], list)

    def test_short_phase_data_does_not_crash(self):
        # 5 samples is enough for at least one tau (m=1)
        phase = np.cumsum(np.random.RandomState(0).randn(5) * 1e-9)
        result = compute_stability_metrics(phase, sample_interval=1.0)
        # Either insufficient or partial — must not raise
        assert isinstance(result, dict)
