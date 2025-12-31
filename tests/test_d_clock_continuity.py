#!/usr/bin/env python3
"""
Unit Tests for D_clock Continuity Validation

Tests the continuity validation that detects physically impossible timing jumps:
- CHU frame slips (33ms jumps)
- Multipath mode hopping
- Decoder errors

Author: HF Time Standard Team
Date: 2025-12-31
"""

import pytest
import numpy as np
from datetime import datetime, timezone
from hf_timestd.core.phase2_temporal_engine import Phase2TemporalEngine
from pathlib import Path


class TestDClockContinuityValidation:
    """Test D_clock continuity validation logic"""
    
    def setup_method(self):
        """Initialize engine for each test"""
        # Create minimal engine instance for testing
        # We only need the validation method, not full initialization
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir=Path('/tmp/test_raw_buffer'),
            output_dir=Path('/tmp/test_output'),
            channel_name='TEST_10MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww',
            sample_rate=20000
        )
    
    def test_first_measurement_accepted(self):
        """Test that first measurement (no previous) is always accepted"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=10.5,
            previous_d_clock_ms=None,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        assert is_valid is True
        assert reason == "First measurement"
    
    def test_small_change_accepted(self):
        """Test that small changes (<2ms) are accepted"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=10.5,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        assert is_valid is True
        assert reason == "Continuity OK"
    
    def test_gradual_drift_accepted(self):
        """Test that gradual drift (0.1ms/min) is accepted"""
        # Over 10 minutes, 1ms drift is acceptable
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=11.0,
            previous_d_clock_ms=10.0,
            dt_seconds=600.0,  # 10 minutes
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 10 = 3.0ms
        # Actual change = 1.0ms
        assert is_valid is True
    
    def test_chu_frame_slip_rejected(self):
        """Test that CHU frame slip (33ms jump) is rejected"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=43.0,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='CHU_7850'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 33.0ms
        assert is_valid is False
        assert "D_clock jump: 33.00ms" in reason
    
    def test_large_jump_rejected(self):
        """Test that large jumps (>2ms baseline) are rejected"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=15.0,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 5.0ms
        assert is_valid is False
        assert "D_clock jump: 5.00ms" in reason
    
    def test_negative_jump_rejected(self):
        """Test that negative jumps are also caught"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=5.0,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 5.0ms (absolute value)
        assert is_valid is False
        assert "D_clock jump: 5.00ms" in reason
    
    def test_multipath_mode_hop_rejected(self):
        """Test that multipath mode hopping (sudden 10ms change) is rejected"""
        # Multipath mode change (1F -> 2F) could cause ~10ms jump
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=20.0,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='WWV_10MHz'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 10.0ms
        assert is_valid is False
        assert "D_clock jump: 10.00ms" in reason
    
    def test_longer_interval_allows_more_drift(self):
        """Test that longer intervals allow proportionally more drift"""
        # Over 30 minutes, 5ms drift should be acceptable
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=15.0,
            previous_d_clock_ms=10.0,
            dt_seconds=1800.0,  # 30 minutes
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 30 = 5.0ms
        # Actual change = 5.0ms
        assert is_valid is True
    
    def test_ionospheric_storm_drift_allowed(self):
        """Test that gradual ionospheric changes are allowed"""
        # During ionospheric storm, layer height might change 100km over 1 hour
        # This translates to ~0.3ms/min propagation delay change
        # Our threshold of 0.1ms/min is conservative
        
        # Over 60 minutes, 8ms total drift
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=18.0,
            previous_d_clock_ms=10.0,
            dt_seconds=3600.0,  # 60 minutes
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 60 = 8.0ms
        # Actual change = 8.0ms
        assert is_valid is True


class TestDClockContinuityEdgeCases:
    """Test edge cases and boundary conditions"""
    
    def setup_method(self):
        """Initialize engine for each test"""
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir=Path('/tmp/test_raw_buffer'),
            output_dir=Path('/tmp/test_output'),
            channel_name='TEST_10MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww',
            sample_rate=20000
        )
    
    def test_exactly_at_threshold_accepted(self):
        """Test that change exactly at threshold is accepted"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=12.1,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 2.1ms (exactly at threshold)
        assert is_valid is True
    
    def test_just_over_threshold_rejected(self):
        """Test that change just over threshold is rejected"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=12.11,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 1 = 2.1ms
        # Actual change = 2.11ms (just over threshold)
        assert is_valid is False
    
    def test_zero_time_interval(self):
        """Test handling of zero time interval"""
        # This shouldn't happen in practice, but test robustness
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=10.5,
            previous_d_clock_ms=10.0,
            dt_seconds=0.0,
            channel_name='TEST'
        )
        
        # Max allowed = 2.0 + 0.1 * 0 = 2.0ms
        # Actual change = 0.5ms
        assert is_valid is True
    
    def test_very_large_d_clock_values(self):
        """Test with very large D_clock values (e.g., unsynchronized GPSDO)"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=1000.5,
            previous_d_clock_ms=1000.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Should still validate based on delta, not absolute value
        assert is_valid is True
    
    def test_sign_change_small_magnitude(self):
        """Test sign change with small magnitude"""
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=-0.5,
            previous_d_clock_ms=0.5,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # Actual change = 1.0ms (absolute value)
        assert is_valid is True


class TestDClockContinuityPhysics:
    """Test physical constraints and assumptions"""
    
    def setup_method(self):
        """Initialize engine for each test"""
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir=Path('/tmp/test_raw_buffer'),
            output_dir=Path('/tmp/test_output'),
            channel_name='TEST_10MHz',
            frequency_hz=10e6,
            receiver_grid='EM38ww',
            sample_rate=20000
        )
    
    def test_ionospheric_layer_height_change_rate(self):
        """Test that realistic ionospheric layer height changes are allowed"""
        # F2 layer diurnal variation: ~50 km over 12 hours = 0.07 km/min
        # Path length change: ~0.14 km/min (2x for reflection)
        # Propagation delay change: ~0.14 km/min / 300 km/ms = 0.00047 ms/min
        # Our threshold of 0.1 ms/min is very conservative
        
        # Simulate 1 hour of gradual change
        # 0.00047 ms/min * 60 min = 0.028 ms total
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=10.028,
            previous_d_clock_ms=10.0,
            dt_seconds=3600.0,
            channel_name='TEST'
        )
        
        assert is_valid is True
    
    def test_gpsdo_drift_rate(self):
        """Test that GPSDO drift is well within limits"""
        # Typical GPSDO: <1e-12 frequency stability
        # Over 1 minute: <60 ns = 0.00006 ms
        # Our threshold of 2ms baseline is very conservative
        
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=10.00006,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        assert is_valid is True
    
    def test_measurement_noise_tolerance(self):
        """Test that typical measurement noise is tolerated"""
        # Typical measurement noise: ±1-2ms
        # Our 2ms baseline allows for this
        
        is_valid, reason = self.engine._validate_d_clock_continuity(
            current_d_clock_ms=11.9,
            previous_d_clock_ms=10.0,
            dt_seconds=60.0,
            channel_name='TEST'
        )
        
        # 1.9ms change is within 2ms baseline
        assert is_valid is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
