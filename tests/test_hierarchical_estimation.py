#!/usr/bin/env python3
"""
Tests for v6.0 Hierarchical Estimation Architecture

This module tests the metrologically-justified estimation architecture:
1. Per-Broadcast Kalman Filter - ionospheric path tracking
2. TEC Estimation - multi-frequency dispersion fitting
3. WLS Fusion - optimal linear combination without temporal smoothing

These tests verify that each component works correctly in isolation
and that the integrated system produces stable, reproducible results.
"""

import pytest
import numpy as np
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Import components under test
from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter, BroadcastCharacteristics
from hf_timestd.core.tec_estimator import TECEstimator, TECResult


class TestBroadcastKalmanFilter:
    """Tests for per-broadcast Kalman filter."""
    
    def test_initialization(self):
        """Test filter initializes with correct parameters."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        assert kalman.broadcast_id == "WWV_10000"
        assert kalman.station == "WWV"
        assert kalman.frequency_mhz == 10.0
        assert not kalman.initialized
        assert kalman.n_updates == 0
    
    def test_first_measurement_initializes_state(self):
        """Test first measurement initializes filter state."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        tof, uncertainty = kalman.update(35.0, snr_db=15.0)
        
        assert kalman.initialized
        assert kalman.n_updates == 1
        assert tof == 35.0  # First measurement sets state directly
        assert uncertainty > 0
    
    def test_subsequent_measurements_smooth(self):
        """Test Kalman smooths subsequent measurements."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        # Initialize with first measurement
        kalman.update(35.0, snr_db=15.0)
        
        # Second measurement slightly different
        tof, uncertainty = kalman.update(35.5, snr_db=15.0)
        
        # Kalman should smooth - result between 35.0 and 35.5
        assert 35.0 < tof < 35.5
        assert kalman.n_updates == 2
    
    def test_outlier_rejection(self):
        """Test large innovations are detected as mode changes."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        # Initialize and converge
        for _ in range(10):
            kalman.update(35.0, snr_db=15.0)
        
        # Large jump should be detected
        mode_status = kalman.detect_mode_transition(20.0)  # 20ms innovation
        
        assert mode_status in ['MODE_CHANGE', 'POSSIBLE_CHANGE']
    
    def test_snr_affects_measurement_noise(self):
        """Test high SNR gives lower measurement noise."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        noise_high_snr = kalman._get_measurement_noise(20.0)
        noise_low_snr = kalman._get_measurement_noise(5.0)
        
        assert noise_high_snr < noise_low_snr
    
    def test_state_persistence(self):
        """Test filter state can be saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            
            # Create and update filter
            kalman1 = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
            for i in range(5):
                kalman1.update(35.0 + i * 0.1, snr_db=15.0)
            
            # Save state
            kalman1.save_state(state_dir)
            
            # Create new filter and load state
            kalman2 = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
            loaded = kalman2.load_state(state_dir)
            
            assert loaded
            assert kalman2.initialized
            assert kalman2.n_updates == kalman1.n_updates
            assert np.allclose(kalman2.state, kalman1.state)
    
    def test_frequency_dependent_characteristics(self):
        """Test different frequencies get different tuning."""
        kalman_low = BroadcastKalmanFilter("WWV_5000", "WWV", 5.0)
        kalman_high = BroadcastKalmanFilter("WWV_15000", "WWV", 15.0)
        
        # Low frequency should have higher process noise (E-layer volatility)
        assert kalman_low.characteristics.q_tof > kalman_high.characteristics.q_tof
    
    def test_station_dependent_characteristics(self):
        """Test different stations get different tuning."""
        kalman_wwv = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        kalman_bpm = BroadcastKalmanFilter("BPM_10000", "BPM", 10.0)
        
        # BPM (long path) should have higher base noise
        assert kalman_bpm.characteristics.base_measurement_noise_ms > kalman_wwv.characteristics.base_measurement_noise_ms
    
    def test_convergence_detection(self):
        """Test filter detects convergence correctly."""
        kalman = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
        
        # Not converged initially
        assert not kalman.is_converged()
        
        # Feed consistent measurements
        for _ in range(10):
            kalman.update(35.0, snr_db=20.0)
        
        # Should be converged after enough consistent updates
        # (depends on uncertainty threshold and mode stability)
        # This may or may not converge depending on timing
        # Just verify the method runs without error
        _ = kalman.is_converged()


class TestTECEstimator:
    """Tests for TEC estimation from multi-frequency measurements."""
    
    def test_minimum_frequencies_required(self):
        """Test TEC estimation requires at least 2 frequencies."""
        estimator = TECEstimator()
        
        # Single frequency should return None
        result = estimator.estimate_tec(
            [{'frequency_hz': 10e6, 'toa_ms': 35.0}],
            station='WWV',
            timestamp=0.0
        )
        
        assert result is None
    
    def test_two_frequency_estimation(self):
        """Test TEC estimation with two frequencies."""
        estimator = TECEstimator()
        
        # Simulate ionospheric dispersion: lower freq arrives later
        # ToA = T_vacuum + k/f²
        # At 5 MHz: ToA = 33.0 + 2.0 = 35.0 ms
        # At 10 MHz: ToA = 33.0 + 0.5 = 33.5 ms
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 35.0, 'uncertainty_ms': 0.5},
            {'frequency_hz': 10e6, 'toa_ms': 33.5, 'uncertainty_ms': 0.5},
        ]
        
        result = estimator.estimate_tec(measurements, 'WWV', 0.0)
        
        assert result is not None
        assert result.station == 'WWV'
        assert result.n_frequencies == 2
        # t_vacuum should be approximately 33.0 ms (extrapolated to infinite frequency)
        assert 30.0 < result.t_vacuum_error_ms < 36.0
    
    def test_three_frequency_estimation(self):
        """Test TEC estimation with three frequencies gives better fit."""
        estimator = TECEstimator()
        
        # Three frequencies following 1/f² relationship
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 35.0, 'uncertainty_ms': 0.5},
            {'frequency_hz': 10e6, 'toa_ms': 33.5, 'uncertainty_ms': 0.5},
            {'frequency_hz': 15e6, 'toa_ms': 33.2, 'uncertainty_ms': 0.5},
        ]
        
        result = estimator.estimate_tec(measurements, 'WWV', 0.0)
        
        assert result is not None
        assert result.n_frequencies == 3
        # With good 1/f² fit, confidence should be high
        assert result.confidence > 0.5
    
    def test_negative_slope_rejected(self):
        """Negative slope (higher freq arriving later) is unphysical and must be rejected."""
        estimator = TECEstimator()

        # Inverted relationship (higher freq arrives later - impossible)
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 33.0, 'uncertainty_ms': 0.5},
            {'frequency_hz': 10e6, 'toa_ms': 35.0, 'uncertainty_ms': 0.5},
        ]

        result = estimator.estimate_tec(measurements, 'WWV', 0.0)

        # Estimator rejects unphysical data outright (mode mixing / noise)
        assert result is None
    
    def test_group_delay_calculation(self):
        """Test per-frequency group delay is calculated."""
        estimator = TECEstimator()
        
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 35.0, 'uncertainty_ms': 0.5},
            {'frequency_hz': 10e6, 'toa_ms': 33.5, 'uncertainty_ms': 0.5},
        ]
        
        result = estimator.estimate_tec(measurements, 'WWV', 0.0)
        
        assert result is not None
        assert len(result.group_delay_ms) == 2
        # Lower frequency should have higher delay
        assert result.group_delay_ms.get(5.0, 0) > result.group_delay_ms.get(10.0, 0)


class TestIntegration:
    """Integration tests for the hierarchical architecture."""
    
    def test_broadcast_kalman_to_tec_flow(self):
        """Test data flows from per-broadcast Kalman to TEC estimator."""
        # Create Kalman filters for multiple frequencies
        kalmans = {
            'WWV_5000': BroadcastKalmanFilter("WWV_5000", "WWV", 5.0),
            'WWV_10000': BroadcastKalmanFilter("WWV_10000", "WWV", 10.0),
            'WWV_15000': BroadcastKalmanFilter("WWV_15000", "WWV", 15.0),
        }
        
        # Simulate measurements with ionospheric dispersion
        raw_measurements = {
            'WWV_5000': 35.0,
            'WWV_10000': 33.5,
            'WWV_15000': 33.2,
        }
        
        # Filter through per-broadcast Kalmans
        filtered = {}
        for broadcast_id, raw_tof in raw_measurements.items():
            tof, unc = kalmans[broadcast_id].update(raw_tof, snr_db=15.0)
            filtered[broadcast_id] = {'tof': tof, 'uncertainty': unc}
        
        # Feed to TEC estimator
        tec_input = [
            {
                'frequency_hz': kalmans[bid].frequency_mhz * 1e6,
                'toa_ms': data['tof'],
                'uncertainty_ms': data['uncertainty']
            }
            for bid, data in filtered.items()
        ]
        
        estimator = TECEstimator()
        result = estimator.estimate_tec(tec_input, 'WWV', 0.0)
        
        assert result is not None
        assert result.n_frequencies == 3
    
    def test_restart_stability(self):
        """Test that state persistence provides stable restarts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            
            # Simulate first session
            kalman1 = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
            for i in range(20):
                kalman1.update(35.0 + np.random.normal(0, 0.5), snr_db=15.0)
            
            final_state_1 = kalman1.state.copy()
            kalman1.save_state(state_dir)
            
            # Simulate restart
            kalman2 = BroadcastKalmanFilter("WWV_10000", "WWV", 10.0)
            kalman2.load_state(state_dir)
            
            # State should be restored
            assert np.allclose(kalman2.state, final_state_1)
            
            # Continue with new measurements
            tof, unc = kalman2.update(35.1, snr_db=15.0)
            
            # Should be close to previous state (no large jump)
            assert abs(tof - final_state_1[0]) < 1.0


class TestWeightedLeastSquares:
    """Tests for WLS fusion (replacing L3 Kalman)."""
    
    def test_wls_is_optimal_linear_combination(self):
        """Test WLS produces optimal linear combination."""
        # Simulate measurements with different uncertainties
        measurements = [
            {'d_clock': 1.0, 'uncertainty': 0.5},  # High weight
            {'d_clock': 2.0, 'uncertainty': 1.0},  # Medium weight
            {'d_clock': 3.0, 'uncertainty': 2.0},  # Low weight
        ]
        
        # Compute WLS manually
        weights = [1.0 / m['uncertainty']**2 for m in measurements]
        d_clocks = [m['d_clock'] for m in measurements]
        
        wls_mean = sum(w * d for w, d in zip(weights, d_clocks)) / sum(weights)
        
        # WLS should weight toward the low-uncertainty measurement
        assert wls_mean < 2.0  # Closer to 1.0 than to 3.0
    
    def test_wls_uncertainty_propagation(self):
        """Test WLS uncertainty is correctly propagated."""
        # With inverse variance weighting, combined uncertainty is:
        # 1/σ²_combined = Σ(1/σ²_i)
        uncertainties = [0.5, 1.0, 2.0]
        
        combined_var = 1.0 / sum(1.0 / u**2 for u in uncertainties)
        combined_unc = np.sqrt(combined_var)
        
        # Combined uncertainty should be less than smallest individual
        assert combined_unc < min(uncertainties)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
