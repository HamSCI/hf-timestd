#!/usr/bin/env python3
"""
Unit tests for scintillation indices (S4, σ_φ) in advanced_signal_analysis.

The S4/σ_φ kernel is delegated to hamsci_dsp.propagation.compute_scintillation;
these tests verify (a) the *math* against synthetic injection with known ground
truth, and (b) that severity is *convention-relative* — a label is meaningful
only with its (threshold, detrend order, window) triple, and hf-timestd's HF
data defaults to the HF-oblique convention.

They deliberately do NOT freeze absolute severity numbers to one convention
(the old ITU-R-pinned tests did, which is what blocked the HF recalibration).
"""

import numpy as np
import pytest

from hf_timestd.core.advanced_signal_analysis import (
    AdvancedSignalAnalyzer,
    ScintillationResult,
)
from hamsci_dsp.propagation import HF_OBLIQUE, ITU_R_LBAND


def _two_level_amplitudes(s4: float, n: int = 60) -> np.ndarray:
    """Amplitudes whose intensity has an EXACT coefficient of variation = s4.

    Intensity alternates {1-s4, 1+s4}: mean 1, std s4, so S4 = std(I)/mean(I) =
    s4 exactly. Symmetric and bounded, so the kernel's MAD outlier filter never
    trims it — recovery is exact and deterministic (valid for s4 < 1)."""
    intensity = np.array([1.0 - s4, 1.0 + s4] * (n // 2))
    return np.sqrt(intensity)


class TestScintillationMath:
    """Kernel math, verified by injection — convention-independent."""

    @pytest.fixture
    def analyzer(self):
        return AdvancedSignalAnalyzer(sample_rate=20000)

    @pytest.mark.parametrize("s4_true", [0.2, 0.4, 0.7])
    def test_s4_recovers_injected_coefficient_of_variation(self, analyzer, s4_true):
        amplitudes = _two_level_amplitudes(s4_true)
        phases = np.zeros(len(amplitudes))
        r = analyzer.calculate_scintillation_indices(amplitudes, phases)
        assert isinstance(r, ScintillationResult)
        assert r.s4_index == pytest.approx(s4_true, rel=1e-6)

    def test_s4_formula_matches_definition(self, analyzer):
        amplitudes = np.array([1.0, 2.0, 1.5, 0.5, 1.0])
        phases = np.zeros(5)
        intensity = amplitudes ** 2
        expected = np.sqrt(np.var(intensity)) / np.mean(intensity)
        r = analyzer.calculate_scintillation_indices(amplitudes, phases, min_samples=5)
        assert r.s4_index == pytest.approx(expected, abs=1e-3)

    def test_periodic_fading_known_s4(self, analyzer):
        n = 100
        t = np.arange(n, dtype=float)
        d = 0.25  # 25% modulation depth
        amplitudes = 1.0 * (1 + d * np.sin(2 * np.pi * t / 20))
        phases = np.zeros(n)
        r = analyzer.calculate_scintillation_indices(amplitudes, phases, t)
        # S4 for A = 1 + d·sin: sqrt(2d^2 + d^4/8) / (1 + d^2/2)
        expected = np.sqrt(2 * d**2 + d**4 / 8) / (1 + d**2 / 2)
        assert r.s4_index == pytest.approx(expected, abs=0.05)

    def test_sigma_phi_recovers_injected_fluctuation_under_curvature(self, analyzer):
        # constant amplitude; phase = Doppler + quadratic TEC curvature + white
        # fluctuation. HF default uses a quadratic detrend, which removes the
        # curvature and recovers the injected fluctuation std.
        n = 60
        t = np.arange(n, dtype=float)
        rng = np.random.default_rng(7)
        sigma_inject = 0.30
        phases = 0.05 * t + 0.002 * t**2 + rng.normal(0.0, sigma_inject, n)
        r = analyzer.calculate_scintillation_indices(np.ones(n), phases, t)
        assert r.sigma_phi_rad == pytest.approx(sigma_inject, rel=0.30)

    def test_pure_doppler_is_removed(self, analyzer):
        n = 60
        t = np.arange(n, dtype=float)
        doppler_hz = 0.1
        phases = 2 * np.pi * doppler_hz * t
        r = analyzer.calculate_scintillation_indices(np.ones(n), phases, t)
        assert r.sigma_phi_rad < 0.01           # linear trend fully removed
        assert abs(r.doppler_removed_hz - doppler_hz) < 0.01

    def test_constant_signal_zero_scintillation(self, analyzer):
        n = 60
        r = analyzer.calculate_scintillation_indices(np.ones(n), np.zeros(n))
        assert r.s4_index < 1e-3
        assert r.sigma_phi_rad < 1e-3


class TestConventionRelativeSeverity:
    """Severity is meaningful only relative to its convention.

    Same raw data, two conventions → different label; the raw index is
    identical. hf-timestd defaults to the HF-oblique convention, where the HF
    Rayleigh-fading baseline (quiet-day S4 ~0.7-1.0) is correctly 'weak', not
    the 'strong' that the L-band ITU-R thresholds would (mis)report.
    """

    @pytest.fixture
    def analyzer(self):
        return AdvancedSignalAnalyzer(sample_rate=20000)

    def test_default_convention_is_hf_oblique(self, analyzer):
        r = analyzer.calculate_scintillation_indices(np.ones(30), np.zeros(30))
        assert r.convention_name == HF_OBLIQUE.name

    def test_moderate_s4_is_weak_under_hf_but_moderate_under_itur(self, analyzer):
        amplitudes = _two_level_amplitudes(0.45)      # S4 = 0.45
        phases = np.zeros(len(amplitudes))
        hf = analyzer.calculate_scintillation_indices(amplitudes, phases,
                                                      convention=HF_OBLIQUE)
        itu = analyzer.calculate_scintillation_indices(amplitudes, phases,
                                                       convention=ITU_R_LBAND)
        assert hf.s4_index == pytest.approx(itu.s4_index)   # same raw observable
        assert hf.s4_severity == 'weak'                     # 0.45 < 1.0 (HF)
        assert itu.s4_severity == 'moderate'                # 0.3 <= 0.45 < 0.6 (ITU-R)
        assert hf.convention_name != itu.convention_name

    def test_hf_fading_baseline_is_not_an_event_under_hf(self, analyzer):
        amplitudes = _two_level_amplitudes(0.8)       # quiet-day HF baseline
        phases = np.zeros(len(amplitudes))
        hf = analyzer.calculate_scintillation_indices(amplitudes, phases,
                                                      convention=HF_OBLIQUE)
        itu = analyzer.calculate_scintillation_indices(amplitudes, phases,
                                                       convention=ITU_R_LBAND)
        assert hf.s4_severity == 'weak' and hf.scintillation_event is False
        assert itu.s4_severity == 'strong' and itu.scintillation_event is True


class TestQualityGatingAndPlumbing:

    @pytest.fixture
    def analyzer(self):
        return AdvancedSignalAnalyzer(sample_rate=20000)

    def test_insufficient_samples_is_unknown(self, analyzer):
        r = analyzer.calculate_scintillation_indices(
            np.array([1.0, 1.1, 1.0]), np.array([0.0, 0.1, 0.2]), min_samples=10)
        assert r.confidence == 0.0
        assert r.s4_severity == 'unknown'
        assert r.sigma_phi_severity == 'unknown'
        assert r.valid_samples == 3
        assert r.convention_name == HF_OBLIQUE.name

    def test_from_ticks_convenience(self, analyzer):
        # mild amplitude variation → clean MAD scale, nothing trimmed
        tick_data = [(i, 0.1 * i, 15.0, 1.0 + 0.1 * np.sin(i * 0.5))
                     for i in range(30)]
        r = analyzer.calculate_scintillation_from_ticks(tick_data, min_snr_db=10.0)
        assert r is not None
        assert r.valid_samples == 30
        assert r.s4_severity in ('weak', 'moderate', 'strong')

    def test_from_ticks_snr_filtering(self, analyzer):
        # even ticks pass the SNR gate (15 of 30); mild amplitude variation so
        # the kernel's MAD filter keeps all of them.
        tick_data = [(i, 0.1 * i, 20.0 if i % 2 == 0 else 3.0, 1.0 + 0.02 * i)
                     for i in range(30)]
        r = analyzer.calculate_scintillation_from_ticks(tick_data, min_snr_db=10.0)
        assert r is not None
        assert r.valid_samples == 15


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
