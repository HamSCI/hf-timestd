
import unittest
import logging
from unittest.mock import MagicMock, patch
from hf_timestd.core.tec_estimator import TECEstimator

class TestTECEstimatorDiagnostics(unittest.TestCase):
    def setUp(self):
        self.estimator = TECEstimator()
        
    def test_logs_flat_data(self):
        """Verify that suspicious 'flat' data (0.0 TEC) triggers detailed logging."""
        
        # Create "flat" data: same ToA for all frequencies (no dispersion)
        # T_obs = 1000.0 ms for all frequencies
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 15e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
        ]
        
        station = "TEST_STATION"
        timestamp = 1234567890.0
        
        with self.assertLogs('hf_timestd.core.tec_estimator', level='WARNING') as cm:
            result = self.estimator.estimate_tec(measurements, station, timestamp)
            
            # Check result properties
            self.assertIsNotNone(result)
            self.assertAlmostEqual(result.tec_electrons_m2, 0.0, delta=1e-3)
            # R2 is 0 for flat line (no variance to explain)
            self.assertAlmostEqual(result.confidence, 0.0)
            
            # Check logs
            # We expect a warning about suspicious TEC result
            warning_found = False
            for log_record in cm.output:
                if "Suspicious TEC result" in log_record and "Inputs (Freq MHz -> ToA ms)" in log_record:
                    warning_found = True
                    # Verify input dump
                    self.assertIn("5.0->1000.000", log_record)
                    self.assertIn("10.0->1000.000", log_record)
            
            self.assertTrue(warning_found, "Detailed diagnostic log not found for flat data")

    def test_logs_negative_tec(self):
        """Verify that negative TEC triggers debug/info logs."""
        # Create negative TEC data: Higher freq arrives LATER than Lower freq
        # This is opposite of ionospheric physics (Group delay ~ 1/f^2)
        # 5 MHz: 1000ms
        # 10 MHz: 1010ms (should be faster/smaller delay)
        
        measurements = [
            {'frequency_hz': 5e6, 'toa_ms': 1000.0, 'uncertainty_ms': 0.1},
            {'frequency_hz': 10e6, 'toa_ms': 1010.0, 'uncertainty_ms': 0.1},
        ]
        
        station = "TEST_STATION"
        timestamp = 1234567890.0
        
        with self.assertLogs('hf_timestd.core.tec_estimator', level='DEBUG') as cm:
            result = self.estimator.estimate_tec(measurements, station, timestamp)
            
            # Check result properties
            self.assertIsNotNone(result)
            self.assertLess(result.tec_electrons_m2, 0.0)
            
            # Check for specific log message
            log_found = False
            for log_record in cm.output:
                if "Negative TEC detected" in log_record:
                    log_found = True
            
            self.assertTrue(log_found, "Negative TEC warning not found")

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
