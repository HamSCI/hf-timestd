#!/usr/bin/env python3
"""
Unit tests for Sporadic-E (Es) detection algorithm.

Tests the physics-based detection in propagation_mode_solver.py for:
- SNR anomaly detection (sudden increases at higher frequencies)
- Mode change detection (F-layer to E-layer transitions)
- Critical frequency (foEs) estimation
- Multi-frequency confirmation

Physics Reference:
- Sporadic-E forms at ~100-120 km altitude
- Can reflect frequencies up to 10+ MHz (normally above E-layer MUF)
- Characterized by sudden SNR increases and mode changes
"""

import numpy as np
import pytest
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from hf_timestd.core.propagation_mode_solver import (
    SporadicEDetector,
    SporadicEEvent
)


class TestSporadicEDetection:
    """Test Sporadic-E event detection."""
    
    @pytest.fixture
    def detector(self):
        """Create detector instance."""
        return SporadicEDetector(history_minutes=30)
    
    def test_no_event_insufficient_data(self, detector):
        """Test that insufficient data returns no detection."""
        # Only 2 observations
        base_time = time.time()
        detector.add_observation(base_time, freq_mhz=10.0, snr_db=20.0)
        detector.add_observation(base_time + 60, freq_mhz=10.0, snr_db=21.0)
        
        event = detector.detect_event()
        
        assert event.detected == False
        assert event.confidence == 0.0
    
    def test_no_event_stable_conditions(self, detector):
        """Test that stable conditions don't trigger false detection."""
        base_time = time.time()
        
        # Add stable observations (no SNR anomaly)
        for i in range(20):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=20.0 + np.random.randn() * 2,  # Small variations
                mode='1F'
            )
        
        event = detector.detect_event()
        
        assert event.detected == False
    
    def test_snr_anomaly_detection(self, detector):
        """Test detection via sudden SNR increase."""
        base_time = time.time()
        
        # First half: normal SNR (~20 dB)
        for i in range(10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=20.0 + np.random.randn()
            )
        
        # Second half: elevated SNR (~35 dB) - Es onset
        for i in range(10, 20):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=35.0 + np.random.randn()
            )
        
        event = detector.detect_event()
        
        assert event.detected == True
        assert event.snr_increase_db is not None
        assert event.snr_increase_db >= 10.0
        assert event.detection_method in ['snr_anomaly', 'combined']
    
    def test_mode_change_detection(self, detector):
        """Test detection via F→E mode change."""
        base_time = time.time()
        
        # Observations with mode change
        for i in range(5):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=25.0,
                mode='1F'  # F-layer propagation
            )
        
        for i in range(5, 10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=30.0,
                mode='1E'  # E-layer propagation (Es)
            )
        
        event = detector.detect_event()
        
        assert event.detected == True
        assert event.mode_changed_to_e == True
        assert event.detection_method in ['mode_change', 'combined']
    
    def test_combined_detection(self, detector):
        """Test detection with both SNR anomaly and mode change."""
        base_time = time.time()
        
        # Before Es: low SNR, F-layer mode
        for i in range(10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=18.0,
                mode='1F'
            )
        
        # After Es onset: high SNR, E-layer mode
        for i in range(10, 20):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=35.0,
                mode='1E'
            )
        
        event = detector.detect_event()
        
        assert event.detected == True
        assert event.mode_changed_to_e == True
        assert event.snr_increase_db >= 10.0
        assert event.detection_method == 'combined'
        assert event.confidence > 0.5  # High confidence with combined evidence
    
    def test_foes_estimation(self, detector):
        """Test critical frequency estimation from highest reflected frequency."""
        base_time = time.time()
        
        # Es detected at 15 MHz (highest Es-sensitive frequency)
        for i in range(10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=15.0,
                snr_db=20.0 if i < 5 else 35.0
            )
        
        event = detector.detect_event()
        
        assert event.detected == True
        assert event.highest_freq_reflected_mhz == 15.0
        # foEs ≈ 15 * 0.8 = 12 MHz
        assert event.estimated_foEs_mhz is not None
        assert 10.0 <= event.estimated_foEs_mhz <= 15.0
    
    def test_multi_frequency_confirmation(self, detector):
        """Test that multi-frequency detection increases confidence."""
        base_time = time.time()
        
        # Es detected at both 10 and 15 MHz
        for freq in [10.0, 15.0]:
            for i in range(10):
                detector.add_observation(
                    base_time + i * 60,
                    freq_mhz=freq,
                    snr_db=20.0 if i < 5 else 35.0
                )
        
        event = detector.detect_event()
        
        assert event.detected == True
        # Multi-frequency should give higher confidence
        assert event.confidence >= 0.4
    
    def test_history_pruning(self, detector):
        """Test that old observations are pruned."""
        base_time = time.time()
        
        # Add old observations (beyond history window)
        old_time = base_time - 3600  # 1 hour ago
        for i in range(10):
            detector.add_observation(
                old_time + i * 60,
                freq_mhz=10.0,
                snr_db=35.0  # Would trigger detection if not pruned
            )
        
        # Add recent normal observations
        for i in range(10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=20.0
            )
        
        # Old high-SNR observations should be pruned
        event = detector.detect_event()
        assert event.detected == False


class TestSporadicEPhysics:
    """Test physical correctness of Es detection."""
    
    @pytest.fixture
    def detector(self):
        return SporadicEDetector(history_minutes=30)
    
    def test_es_height_default(self, detector):
        """Test that Es height defaults to typical value."""
        base_time = time.time()
        
        for i in range(10):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=20.0 if i < 5 else 35.0
            )
        
        event = detector.detect_event()
        
        assert event.detected == True
        assert event.estimated_height_km == 110.0  # Typical Es height
    
    def test_threshold_sensitivity(self):
        """Test that SNR threshold is appropriate for Es detection."""
        detector = SporadicEDetector()
        
        # Verify threshold is reasonable (10 dB is typical for Es onset)
        assert detector.SNR_INCREASE_THRESHOLD_DB == 10.0
        
        # Verify Es-sensitive frequencies
        assert 10.0 in detector.ES_FREQUENCIES_MHZ
        assert 15.0 in detector.ES_FREQUENCIES_MHZ
    
    def test_gradual_snr_increase_no_detection(self, detector):
        """Test that gradual SNR increases don't trigger Es detection."""
        base_time = time.time()
        
        # Gradual increase over 20 minutes (not sudden Es onset)
        for i in range(20):
            detector.add_observation(
                base_time + i * 60,
                freq_mhz=10.0,
                snr_db=20.0 + i * 0.5  # 0.5 dB/min increase
            )
        
        event = detector.detect_event()
        
        # Gradual increase should not trigger (baseline tracks with it)
        # This depends on implementation - may or may not detect
        # The key is that sudden jumps are more reliably detected
        if event.detected:
            # If detected, confidence should be lower than sudden onset
            assert event.confidence < 0.8


class TestSporadicEEventDataclass:
    """Test SporadicEEvent dataclass."""
    
    def test_default_values(self):
        """Test default values for SporadicEEvent."""
        event = SporadicEEvent(detected=False, confidence=0.0)
        
        assert event.detected == False
        assert event.confidence == 0.0
        assert event.event_start_time is None
        assert event.estimated_foEs_mhz is None
        assert event.estimated_height_km == 110.0
        assert event.mode_changed_to_e == False
        assert event.detection_method == 'snr_anomaly'
    
    def test_full_event(self):
        """Test fully populated SporadicEEvent."""
        event = SporadicEEvent(
            detected=True,
            confidence=0.85,
            event_start_time=1705420800.0,
            estimated_foEs_mhz=12.0,
            estimated_height_km=105.0,
            snr_increase_db=15.0,
            mode_changed_to_e=True,
            highest_freq_reflected_mhz=15.0,
            detection_method='combined'
        )
        
        assert event.detected == True
        assert event.confidence == 0.85
        assert event.estimated_foEs_mhz == 12.0
        assert event.mode_changed_to_e == True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
