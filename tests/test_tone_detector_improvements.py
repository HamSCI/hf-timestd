#!/usr/bin/env python3
"""
Unit tests for Phase 4 tone detection improvements.

Tests three enhancements:
1. Robust noise floor estimation using MAD statistics
2. Adaptive search window calculation based on SNR and convergence state
3. Ionospheric propagation delay prediction using IRI-2020 model
"""

import pytest
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
import sys

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from hf_timestd.core.tone_detector import MultiStationToneDetector
from hf_timestd.core.phase2_temporal_engine import Phase2TemporalEngine


class TestRobustNoiseFloor:
    """Test robust noise floor estimation using MAD statistics."""
    
    def setup_method(self):
        """Initialize detector for testing."""
        self.detector = MultiStationToneDetector(
            channel_name='WWV_10_MHz',
            sample_rate=20000
        )
    
    def test_mad_calculation_with_gaussian_noise(self):
        """Test MAD calculation with pure Gaussian noise."""
        # Generate Gaussian noise
        np.random.seed(42)
        noise = np.random.normal(0, 1.0, 10000)
        
        # Create correlation-like array
        correlation = np.abs(noise)
        
        # Define search window (middle 20%)
        search_start = 4000
        search_end = 6000
        
        # Calculate noise floor
        noise_floor = self.detector._estimate_robust_noise_floor(
            correlation, search_start, search_end
        )
        
        # For Gaussian noise, median should be ~0.67σ
        # MAD should be ~0.67σ, so σ_eq = 1.4826 * 0.67σ ≈ σ
        # Noise floor = median + 3σ ≈ 0.67σ + 3σ = 3.67σ
        expected_floor = 3.67 * 1.0  # σ = 1.0
        
        # Allow 20% tolerance due to finite sample size
        assert abs(noise_floor - expected_floor) < 0.2 * expected_floor
    
    def test_robustness_to_outliers(self):
        """Test that MAD is robust to outliers in search region."""
        # Generate noise with outliers in search region
        np.random.seed(42)
        correlation = np.random.normal(0, 1.0, 10000)
        correlation = np.abs(correlation)
        
        # Add strong outliers in search region
        search_start = 4000
        search_end = 6000
        correlation[search_start:search_end] += 10.0  # Strong interference
        
        # Calculate noise floor (should ignore search region)
        noise_floor = self.detector._estimate_robust_noise_floor(
            correlation, search_start, search_end
        )
        
        # Noise floor should be based on clean noise, not contaminated by outliers
        # Should be similar to test without outliers
        expected_floor = 3.67 * 1.0
        assert abs(noise_floor - expected_floor) < 0.3 * expected_floor
    
    def test_fallback_for_short_buffers(self):
        """Test fallback to percentile method for short buffers."""
        # Create short correlation array
        correlation = np.random.normal(0, 1.0, 150)
        correlation = np.abs(correlation)
        
        # Search window takes most of the buffer
        search_start = 50
        search_end = 100
        
        # Should fall back to percentile method
        noise_floor = self.detector._estimate_robust_noise_floor(
            correlation, search_start, search_end
        )
        
        # Should return a reasonable value (not NaN or inf)
        assert np.isfinite(noise_floor)
        assert noise_floor > 0


class TestAdaptiveSearchWindow:
    """Test adaptive search window calculation."""
    
    def setup_method(self):
        """Initialize detector for testing."""
        self.detector = MultiStationToneDetector(
            channel_name='WWV_10_MHz',
            sample_rate=20000
        )
    
    def test_locked_high_snr_narrow_window(self):
        """Test that LOCKED + high SNR gives very tight window."""
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=25.0,
            convergence_state='LOCKED'
        )
        
        assert window_ms == 5.0, "LOCKED + high SNR should give ±5ms window"
    
    def test_locked_good_snr_tight_window(self):
        """Test that LOCKED + good SNR gives tight window."""
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=17.0,
            convergence_state='LOCKED'
        )
        
        assert window_ms == 15.0, "LOCKED + good SNR should give ±15ms window"
    
    def test_locked_medium_snr_moderate_window(self):
        """Test that LOCKED + medium SNR gives moderate window."""
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=12.0,
            convergence_state='LOCKED'
        )
        
        assert window_ms == 50.0, "LOCKED + medium SNR should give ±50ms window"
    
    def test_converging_state_adaptive(self):
        """Test that CONVERGING state adapts to SNR."""
        # High SNR
        window_high = self.detector._calculate_adaptive_search_window(
            recent_snr_db=18.0,
            convergence_state='CONVERGING'
        )
        assert window_high == 15.0
        
        # Medium SNR
        window_med = self.detector._calculate_adaptive_search_window(
            recent_snr_db=12.0,
            convergence_state='CONVERGING'
        )
        assert window_med == 50.0
    
    def test_acquiring_wide_window(self):
        """Test that ACQUIRING always gives wide window."""
        # Even with high SNR, ACQUIRING should use wide window
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=25.0,
            convergence_state='ACQUIRING'
        )
        
        assert window_ms == 500.0, "ACQUIRING should always use ±500ms window"
    
    def test_low_snr_wide_window(self):
        """Test that low SNR always gives wide window."""
        # LOCKED but low SNR should use wide window
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=5.0,
            convergence_state='LOCKED'
        )
        
        assert window_ms == 500.0, "Low SNR should use ±500ms window"
    
    def test_no_snr_data_wide_window(self):
        """Test that missing SNR data gives wide window."""
        window_ms = self.detector._calculate_adaptive_search_window(
            recent_snr_db=None,
            convergence_state='LOCKED'
        )
        
        assert window_ms == 500.0, "No SNR data should use ±500ms window"


class TestIonosphericPrediction:
    """Test ionospheric propagation delay prediction."""
    
    def setup_method(self):
        """Initialize temporal engine for testing."""
        # Create minimal temporal engine for testing
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir=Path('/tmp/test_raw'),
            output_dir=Path('/tmp/test_output'),
            channel_name='WWV_10_MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww',
            precise_lat=38.0,
            precise_lon=-90.0
        )
    
    def test_wwv_delay_prediction(self):
        """Test propagation delay prediction for WWV."""
        timestamp = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        
        delay_ms, uncertainty_ms = self.engine._predict_propagation_delay(
            station='WWV',
            timestamp=timestamp
        )
        
        # WWV is ~1500 km from central US
        # Expected delay: ~5-10 ms for 1-hop F-layer
        assert 3.0 < delay_ms < 15.0, f"WWV delay {delay_ms}ms outside expected range"
        assert uncertainty_ms > 0, "Uncertainty should be positive"
        assert uncertainty_ms < delay_ms, "Uncertainty should be less than delay"
    
    def test_wwvh_delay_prediction(self):
        """Test propagation delay prediction for WWVH."""
        timestamp = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        
        delay_ms, uncertainty_ms = self.engine._predict_propagation_delay(
            station='WWVH',
            timestamp=timestamp
        )
        
        # WWVH is ~6000 km from central US
        # Expected delay: ~20-30 ms for 1-hop F-layer
        assert 15.0 < delay_ms < 40.0, f"WWVH delay {delay_ms}ms outside expected range"
        assert uncertainty_ms > 0, "Uncertainty should be positive"
    
    def test_chu_delay_prediction(self):
        """Test propagation delay prediction for CHU."""
        timestamp = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        
        delay_ms, uncertainty_ms = self.engine._predict_propagation_delay(
            station='CHU',
            timestamp=timestamp
        )
        
        # CHU is ~1200 km from central US
        # Expected delay: ~4-8 ms for 1-hop F-layer
        assert 2.0 < delay_ms < 12.0, f"CHU delay {delay_ms}ms outside expected range"
        assert uncertainty_ms > 0, "Uncertainty should be positive"
    
    def test_day_night_variation(self):
        """Test that predictions vary between day and night."""
        # Daytime (higher F-layer)
        day_time = datetime(2025, 12, 31, 18, 0, 0, tzinfo=timezone.utc)
        day_delay, _ = self.engine._predict_propagation_delay('WWV', day_time)
        
        # Nighttime (lower F-layer)
        night_time = datetime(2025, 12, 31, 6, 0, 0, tzinfo=timezone.utc)
        night_delay, _ = self.engine._predict_propagation_delay('WWV', night_time)
        
        # Delays should differ (F-layer height varies)
        # Night F-layer is typically higher, so delay should be slightly longer
        assert abs(day_delay - night_delay) > 0.1, "Day/night delays should differ"
    
    def test_unknown_station_fallback(self):
        """Test fallback for unknown station."""
        timestamp = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        
        # Should not crash, should use default distance
        delay_ms, uncertainty_ms = self.engine._predict_propagation_delay(
            station='UNKNOWN',
            timestamp=timestamp
        )
        
        assert delay_ms > 0, "Should return positive delay"
        assert uncertainty_ms > 0, "Should return positive uncertainty"


class TestIntegration:
    """Integration tests for all improvements working together."""
    
    def test_detector_has_all_methods(self):
        """Verify all new methods are present in detector."""
        detector = MultiStationToneDetector(
            channel_name='WWV_10_MHz',
            sample_rate=20000
        )
        
        assert hasattr(detector, '_estimate_robust_noise_floor')
        assert hasattr(detector, '_calculate_adaptive_search_window')
        assert callable(detector._estimate_robust_noise_floor)
        assert callable(detector._calculate_adaptive_search_window)
    
    def test_temporal_engine_has_prediction(self):
        """Verify temporal engine has prediction method."""
        engine = Phase2TemporalEngine(
            raw_buffer_dir=Path('/tmp/test_raw'),
            output_dir=Path('/tmp/test_output'),
            channel_name='WWV_10_MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww'
        )
        
        assert hasattr(engine, '_predict_propagation_delay')
        assert callable(engine._predict_propagation_delay)


if __name__ == '__main__':
    # Run tests with pytest
    pytest.main([__file__, '-v', '--tb=short'])
