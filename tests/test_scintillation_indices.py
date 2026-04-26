#!/usr/bin/env python3
"""
Unit tests for scintillation indices (S4, σ_φ) calculation.

Tests the physics calculations in advanced_signal_analysis.py for:
- S4 amplitude scintillation index
- σ_φ phase scintillation index
- Severity classification
- Event detection

Physics Reference:
- S4 = sqrt(var(I) / mean(I)²) where I is intensity
- σ_φ = std(φ_detrended) where Doppler trend is removed
"""

import numpy as np
import pytest
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hf_timestd.core.advanced_signal_analysis import (
    AdvancedSignalAnalyzer,
    ScintillationResult
)


class TestScintillationIndices:
    """Test scintillation index calculations."""
    
    @pytest.fixture
    def analyzer(self):
        """Create analyzer instance."""
        return AdvancedSignalAnalyzer(sample_rate=20000)
    
    def test_s4_weak_scintillation(self, analyzer):
        """Test S4 calculation for weak scintillation (stable signal)."""
        # Stable amplitude with small variations (S4 < 0.3)
        np.random.seed(42)
        n_samples = 60
        mean_amp = 1.0
        # Small variance: std = 0.1 * mean → S4 ≈ 0.1
        amplitudes = mean_amp + 0.1 * mean_amp * np.random.randn(n_samples)
        amplitudes = np.abs(amplitudes)  # Ensure positive
        
        # Stable phase with small fluctuations
        phases = np.linspace(0, 2*np.pi, n_samples) + 0.05 * np.random.randn(n_samples)
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases)
        
        assert isinstance(result, ScintillationResult)
        assert result.s4_index < 0.3, f"Expected weak S4 < 0.3, got {result.s4_index}"
        assert result.s4_severity == 'weak'
        assert result.scintillation_event == False
        assert result.valid_samples == n_samples
    
    def test_s4_moderate_scintillation(self, analyzer):
        """Test S4 calculation for moderate scintillation."""
        # Moderate amplitude variations (0.3 ≤ S4 < 0.6).
        # S4 = sqrt(var(I)) / mean(I) where I = A². For A ~ N(1, σ),
        # E[I] = 1 + σ², Var(I) ≈ 4σ² + 2σ⁴ (Isserlis), so S4 ≈ 2σ for small σ.
        # Targeting S4 ≈ 0.4 → σ ≈ 0.2 in the amplitude domain.
        np.random.seed(42)
        n_samples = 60
        mean_amp = 1.0
        amplitudes = mean_amp + 0.2 * mean_amp * np.random.randn(n_samples)
        amplitudes = np.abs(amplitudes)

        phases = np.linspace(0, 2*np.pi, n_samples)

        result = analyzer.calculate_scintillation_indices(amplitudes, phases)

        assert 0.3 <= result.s4_index < 0.6, f"Expected moderate S4, got {result.s4_index}"
        assert result.s4_severity == 'moderate'
        assert result.scintillation_event == True
        assert result.event_severity in ['moderate', 'strong']
    
    def test_s4_strong_scintillation(self, analyzer):
        """Test S4 calculation for strong scintillation."""
        # Strong amplitude variations (S4 ≥ 0.6)
        np.random.seed(42)
        n_samples = 60
        mean_amp = 1.0
        # High variance: std = 0.8 * mean → S4 ≈ 0.8
        amplitudes = mean_amp + 0.8 * mean_amp * np.random.randn(n_samples)
        amplitudes = np.abs(amplitudes)
        
        phases = np.linspace(0, 2*np.pi, n_samples)
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases)
        
        assert result.s4_index >= 0.6, f"Expected strong S4 >= 0.6, got {result.s4_index}"
        assert result.s4_severity == 'strong'
        assert result.scintillation_event == True
        assert result.event_severity == 'strong'
    
    def test_sigma_phi_weak(self, analyzer):
        """Test σ_φ calculation for weak phase scintillation."""
        np.random.seed(42)
        n_samples = 60
        amplitudes = np.ones(n_samples)

        # Linear phase (Doppler) with small fluctuations. doppler_hz must be
        # well below the np.unwrap Nyquist (per-sample Δφ < π) so the linear
        # detrend does not race with the unwrap heuristic.
        doppler_hz = 0.05
        times = np.arange(n_samples, dtype=float)
        phases = 2 * np.pi * doppler_hz * times + 0.05 * np.random.randn(n_samples)

        result = analyzer.calculate_scintillation_indices(amplitudes, phases, times)

        assert result.sigma_phi_rad < 0.2, f"Expected weak σ_φ < 0.2, got {result.sigma_phi_rad}"
        assert result.sigma_phi_severity == 'weak'
        # Doppler should be approximately recovered
        assert abs(result.doppler_removed_hz - doppler_hz) < 0.05
    
    def test_sigma_phi_moderate(self, analyzer):
        """Test σ_φ calculation for moderate phase scintillation."""
        n_samples = 60
        amplitudes = np.ones(n_samples)
        
        # Phase with moderate fluctuations (0.2 ≤ σ_φ < 0.5)
        times = np.arange(n_samples)
        phases = 0.3 * np.random.randn(n_samples)  # ~0.3 rad std
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases, times)
        
        assert 0.2 <= result.sigma_phi_rad < 0.5, f"Expected moderate σ_φ, got {result.sigma_phi_rad}"
        assert result.sigma_phi_severity == 'moderate'
    
    def test_sigma_phi_strong(self, analyzer):
        """Test σ_φ calculation for strong phase scintillation."""
        n_samples = 60
        amplitudes = np.ones(n_samples)
        
        # Phase with large fluctuations (σ_φ ≥ 0.5)
        times = np.arange(n_samples)
        phases = 0.7 * np.random.randn(n_samples)  # ~0.7 rad std
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases, times)
        
        assert result.sigma_phi_rad >= 0.5, f"Expected strong σ_φ >= 0.5, got {result.sigma_phi_rad}"
        assert result.sigma_phi_severity == 'strong'
    
    def test_insufficient_samples(self, analyzer):
        """Test handling of insufficient samples."""
        amplitudes = np.array([1.0, 1.1, 1.0])  # Only 3 samples
        phases = np.array([0.0, 0.1, 0.2])
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases, min_samples=10)
        
        assert result.confidence == 0.0
        assert result.s4_severity == 'unknown'
        assert result.sigma_phi_severity == 'unknown'
        assert result.valid_samples == 3
    
    def test_doppler_removal(self, analyzer):
        """Test that Doppler trend is properly removed from phase."""
        n_samples = 60
        amplitudes = np.ones(n_samples)
        times = np.arange(n_samples, dtype=float)

        # Pure Doppler (linear phase) with known rate. Keep doppler_hz below
        # the np.unwrap Nyquist (per-sample Δφ < π) — see test_sigma_phi_weak.
        doppler_hz = 0.1
        phases = 2 * np.pi * doppler_hz * times

        result = analyzer.calculate_scintillation_indices(amplitudes, phases, times)

        # After removing Doppler, phase variance should be near zero
        assert result.sigma_phi_rad < 0.01, "Doppler should be fully removed"
        assert abs(result.doppler_removed_hz - doppler_hz) < 0.01
    
    def test_s4_formula_correctness(self, analyzer):
        """Verify S4 formula: S4 = sqrt(var(I) / mean(I)²)."""
        # Known values for verification
        amplitudes = np.array([1.0, 2.0, 1.5, 0.5, 1.0])
        phases = np.zeros(5)
        
        # Manual calculation
        intensity = amplitudes ** 2
        mean_I = np.mean(intensity)
        var_I = np.var(intensity)
        expected_s4 = np.sqrt(var_I) / mean_I
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases, min_samples=5)
        
        assert abs(result.s4_index - expected_s4) < 0.001, \
            f"S4 formula mismatch: expected {expected_s4}, got {result.s4_index}"
    
    def test_from_ticks_convenience(self, analyzer):
        """Test calculate_scintillation_from_ticks convenience method."""
        # Simulate tick data: (second, phase, snr, amplitude)
        tick_data = [
            (i, 0.1 * i, 15.0, 1.0 + 0.1 * np.sin(i * 0.5))
            for i in range(30)
        ]
        
        result = analyzer.calculate_scintillation_from_ticks(tick_data, min_snr_db=10.0)
        
        assert result is not None
        assert result.valid_samples == 30
        assert result.s4_severity in ['weak', 'moderate', 'strong']
    
    def test_from_ticks_snr_filtering(self, analyzer):
        """Test that low-SNR ticks are filtered out."""
        # Mix of high and low SNR ticks
        tick_data = [
            (i, 0.1 * i, 20.0 if i % 2 == 0 else 3.0, 1.0)
            for i in range(30)
        ]
        
        result = analyzer.calculate_scintillation_from_ticks(tick_data, min_snr_db=10.0)
        
        assert result is not None
        assert result.valid_samples == 15  # Only even indices pass SNR filter


class TestScintillationPhysics:
    """Test physical correctness of scintillation calculations."""
    
    @pytest.fixture
    def analyzer(self):
        return AdvancedSignalAnalyzer(sample_rate=20000)
    
    def test_constant_signal_zero_scintillation(self, analyzer):
        """A perfectly constant signal should have zero scintillation."""
        n_samples = 60
        amplitudes = np.ones(n_samples)
        phases = np.zeros(n_samples)
        
        result = analyzer.calculate_scintillation_indices(amplitudes, phases)
        
        assert result.s4_index < 0.001, "Constant amplitude should give S4 ≈ 0"
        assert result.sigma_phi_rad < 0.001, "Constant phase should give σ_φ ≈ 0"
    
    def test_periodic_fading(self, analyzer):
        """Periodic fading should produce predictable S4."""
        n_samples = 100
        times = np.arange(n_samples, dtype=float)

        # Sinusoidal amplitude variation (Rayleigh-like fading)
        mean_amp = 1.0
        fade_depth = 0.25  # 25% modulation depth (small-fade regime)
        amplitudes = mean_amp * (1 + fade_depth * np.sin(2 * np.pi * times / 20))
        phases = np.zeros(n_samples)

        result = analyzer.calculate_scintillation_indices(amplitudes, phases, times)

        # S4 = sqrt(Var(I)) / mean(I) where I = A². For A = 1 + d·sin(ωt),
        #   mean(I) = 1 + d²/2,  Var(I) = 2d² + d⁴/8
        # → S4 = sqrt(2d² + d⁴/8) / (1 + d²/2). At d=0.25 this is ≈ 0.343.
        d = fade_depth
        expected_s4 = np.sqrt(2 * d**2 + d**4 / 8) / (1 + d**2 / 2)
        assert abs(result.s4_index - expected_s4) < 0.05, \
            f"Periodic fading S4 mismatch: expected ~{expected_s4:.3f}, got {result.s4_index:.3f}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
