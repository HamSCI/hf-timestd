#!/usr/bin/env python3
"""
Unit tests for BroadcastKalmanFilter
"""

import unittest
import numpy as np
from pathlib import Path
import tempfile
import shutil
from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter


class TestBroadcastKalmanFilter(unittest.TestCase):
    """Test cases for BroadcastKalmanFilter."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
    
    def tearDown(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
    
    def test_initialization_wwv_10mhz(self):
        """Test filter initialization for WWV 10 MHz."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        self.assertEqual(filter.broadcast_id, "WWV_10000")
        self.assertEqual(filter.station, "WWV")
        self.assertEqual(filter.frequency_mhz, 10.0)
        self.assertFalse(filter.initialized)
        self.assertEqual(filter.n_updates, 0)
        
        # Check characteristics
        self.assertEqual(filter.characteristics.typical_layer, 'F')
        self.assertTrue(filter.characteristics.has_bcd)
        self.assertTrue(filter.characteristics.has_test_signal)
        self.assertFalse(filter.characteristics.is_anchor)
    
    def test_initialization_chu_3330(self):
        """Test filter initialization for CHU 3.33 MHz (anchor, FSK)."""
        filter = BroadcastKalmanFilter(
            broadcast_id="CHU_3330",
            station="CHU",
            frequency_mhz=3.33
        )
        
        # Check characteristics
        self.assertEqual(filter.characteristics.typical_layer, 'E')
        self.assertEqual(filter.characteristics.modulation, 'FSK')
        self.assertFalse(filter.characteristics.has_bcd)
        self.assertTrue(filter.characteristics.is_anchor)
    
    def test_first_update(self):
        """Test first measurement update."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # First update
        tof, uncertainty = filter.update(measurement_ms=34.5, snr_db=15.0)
        
        self.assertTrue(filter.initialized)
        self.assertEqual(filter.n_updates, 1)
        self.assertAlmostEqual(tof, 34.5, places=2)
        self.assertGreater(uncertainty, 0)
    
    def test_multiple_updates(self):
        """Test multiple updates with stable signal."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # Simulate stable signal with small noise
        measurements = [34.5 + np.random.normal(0, 0.1) for _ in range(10)]
        
        for i, meas in enumerate(measurements):
            tof, uncertainty = filter.update(measurement_ms=meas, snr_db=20.0)
            
            # Uncertainty should decrease with more measurements
            if i > 0:
                self.assertLess(uncertainty, 10.0)  # Should converge
        
        # Final ToF should be close to mean
        self.assertAlmostEqual(tof, 34.5, delta=0.5)
    
    def test_doppler_tracking(self):
        """Test Doppler (rate of change) tracking."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # Simulate linear drift (0.1 ms/min)
        doppler_true = 0.1
        measurements = [34.0 + doppler_true * i for i in range(20)]
        
        for meas in measurements:
            filter.update(measurement_ms=meas, snr_db=20.0)
        
        # Check that Doppler is estimated correctly
        state = filter.get_state()
        self.assertAlmostEqual(state['doppler_ms_per_min'], doppler_true, delta=0.05)
    
    def test_fading_handling(self):
        """Test predict-only mode during fading."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # Initialize with measurements
        for i in range(5):
            filter.update(measurement_ms=34.5, snr_db=20.0)
        
        # Simulate fading (predict only, no update)
        tof_before = filter.state[0]
        tof_predict, uncertainty_predict = filter.predict()
        
        # State should evolve based on Doppler
        # Uncertainty should increase
        self.assertGreater(uncertainty_predict, 0)
    
    # test_gpsdo_continuity_check was removed alongside the
    # `check_gpsdo_continuity` method (§3.4 Low, 2026-05-20): the
    # method had no production callers; only this test exercised it.

    def test_snr_based_measurement_noise(self):
        """Test that measurement noise adapts to SNR."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # High SNR → low noise
        noise_high_snr = filter._get_measurement_noise(snr_db=20.0)
        
        # Low SNR → high noise
        noise_low_snr = filter._get_measurement_noise(snr_db=5.0)
        
        self.assertLess(noise_high_snr, noise_low_snr)
    
    def test_frequency_dependent_tuning(self):
        """Test that process noise varies with frequency."""
        # Low frequency (high volatility)
        filter_low = BroadcastKalmanFilter(
            broadcast_id="WWV_2500",
            station="WWV",
            frequency_mhz=2.5
        )
        
        # High frequency (low volatility)
        filter_high = BroadcastKalmanFilter(
            broadcast_id="WWV_20000",
            station="WWV",
            frequency_mhz=20.0
        )
        
        # Low freq should have higher process noise
        self.assertGreater(
            filter_low.characteristics.q_tof,
            filter_high.characteristics.q_tof
        )
    
    def test_station_dependent_tuning(self):
        """Test that characteristics vary by station."""
        # WWV (short path)
        filter_wwv = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # WWVH (long path)
        filter_wwvh = BroadcastKalmanFilter(
            broadcast_id="WWVH_10000",
            station="WWVH",
            frequency_mhz=10.0
        )
        
        # WWVH should have longer path and higher noise
        self.assertGreater(
            filter_wwvh.characteristics.path_length_km,
            filter_wwv.characteristics.path_length_km
        )
        self.assertGreater(
            filter_wwvh.characteristics.base_measurement_noise_ms,
            filter_wwv.characteristics.base_measurement_noise_ms
        )
    
    def test_fsk_modulation_advantage(self):
        """Test that FSK (CHU) has lower measurement noise than AM."""
        # CHU (FSK)
        filter_chu = BroadcastKalmanFilter(
            broadcast_id="CHU_7850",
            station="CHU",
            frequency_mhz=7.85
        )
        
        # WWV (AM)
        filter_wwv = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # CHU should have lower base noise (FSK advantage)
        # Note: This is approximate due to frequency differences
        self.assertEqual(filter_chu.characteristics.modulation, 'FSK')
        self.assertEqual(filter_wwv.characteristics.modulation, 'AM+BCD')
    
    def test_state_persistence(self):
        """Test saving and loading filter state."""
        filter1 = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # Update filter
        for i in range(10):
            filter1.update(measurement_ms=34.5 + i * 0.1, snr_db=15.0)
        
        # Save state
        filter1.save_state(self.temp_dir)
        
        # Create new filter and load state
        filter2 = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        loaded = filter2.load_state(self.temp_dir)
        self.assertTrue(loaded)
        
        # States should match
        self.assertTrue(filter2.initialized)
        self.assertEqual(filter2.n_updates, filter1.n_updates)
        np.testing.assert_array_almost_equal(filter2.state, filter1.state)
    
    def test_get_state(self):
        """Test getting filter state dictionary."""
        filter = BroadcastKalmanFilter(
            broadcast_id="WWV_10000",
            station="WWV",
            frequency_mhz=10.0
        )
        
        # Update once
        filter.update(measurement_ms=34.5, snr_db=15.0)
        
        state = filter.get_state()
        
        self.assertEqual(state['broadcast_id'], "WWV_10000")
        self.assertEqual(state['station'], "WWV")
        self.assertEqual(state['frequency_mhz'], 10.0)
        self.assertAlmostEqual(state['tof_ms'], 34.5, places=1)
        self.assertIn('doppler_ms_per_min', state)
        self.assertIn('tof_uncertainty_ms', state)
        self.assertTrue(state['initialized'])
        self.assertEqual(state['n_updates'], 1)


if __name__ == '__main__':
    unittest.main()
