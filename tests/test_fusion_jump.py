
import unittest
from unittest.mock import MagicMock, patch
import time
import numpy as np
from hf_timestd.core.multi_broadcast_fusion import MultiBroadcastFusion, BroadcastMeasurement

class TestFusionJump(unittest.TestCase):
    def setUp(self):
        # Initialize Fusion with mock paths
        self.fusion = MultiBroadcastFusion(
            data_root=MagicMock(),
            receiver_lat=40.0,
            receiver_lon=-105.0
        )
        # Disable VTEC integration for this test to isolate Kalman behavior
        self.fusion._read_gnss_vtec = MagicMock(return_value=None)
        
        # Disable file writing
        self.fusion._save_calibration = MagicMock()
        
    def create_measurements(self, d_clock_ms: float, timestamp: float) -> list:
        return [
            BroadcastMeasurement(
                timestamp=timestamp,
                station='WWV',
                frequency_mhz=10.0,
                d_clock_ms=d_clock_ms + np.random.normal(0, 0.05), # Very low noise for clean test
                propagation_delay_ms=5.0,
                propagation_mode='1F',
                confidence=1.0,
                snr_db=30.0,
                quality_grade='A',
                channel_name='WWV_10000_USB',
                uncertainty_ms=1.0
            ),
            BroadcastMeasurement(
                timestamp=timestamp,
                station='CHU',
                frequency_mhz=7.85,
                d_clock_ms=d_clock_ms + np.random.normal(0, 0.05),
                propagation_delay_ms=3.0,
                propagation_mode='1E',
                confidence=1.0,
                snr_db=30.0, # High SNR to minimize measurement uncertainy
                quality_grade='A',
                channel_name='CHU_7850_USB',
                uncertainty_ms=1.0
            )
        ]

    @patch('hf_timestd.core.multi_broadcast_fusion.MultiBroadcastFusion._read_latest_measurements')
    def test_steel_ruler_rejection(self, mock_read):
        """
        Verify that the new "Steel Ruler" configuration rejects a large 24ms jump
        after the system has converged.
        """
        start_time = time.time()
        
        print("\n=== Phase 1: Bootstrap & Convergence (150 updates) ===")
        # Run 150 updates with d_clock = 0.0
        # This allows the Kalman filter to initialize, converge, and set P to steady state
        # And critically, allows calibration to exit "bootstrap mode" (>100 updates)
        
        for i in range(150):
            t = start_time + i * 60
            mock_read.return_value = self.create_measurements(0.0, t)
            
            result = self.fusion.fuse()
            
            if i % 25 == 0:
                print(f"Update {i}: Fused={result.d_clock_fused_ms:.4f}ms, Uncert={result.uncertainty_ms:.4f}ms")

        # Verify we are converged at 0
        self.assertTrue(self.fusion.kalman_converged, "Filter should be converged")
        self.assertAlmostEqual(self.fusion.kalman_state[0], 0.0, delta=0.5, msg="Should be near 0 before jump")
        self.assertLess(self.fusion.kalman_P[0,0], 1.0, "Covariance should be low")

        print("\n=== Phase 2: The 24ms Jump (10 updates) ===")
        # Inject 24ms jump
        jump_val = 24.0
        
        for i in range(150, 160):
            t = start_time + i * 60
            mock_read.return_value = self.create_measurements(jump_val, t)
            
            result = self.fusion.fuse()
            
            print(f"Update {i} (Input={jump_val}ms): Fused={result.d_clock_fused_ms:.4f}ms, Raw={result.d_clock_raw_ms:.4f}ms")
            
            # CRITICAL ASSERTION:
            # The Fused result (Kalman State) should NOT jump to 24.
            # It should stay near 0 because Q is tiny.
            # We allow small movement (drift correction), e.g. < 0.1ms
            self.assertLess(abs(result.d_clock_fused_ms), 0.5, 
                f"Steel Ruler failed! Fused clock jumped to {result.d_clock_fused_ms}ms")
            
            # The Raw result SHOULD reflect the jump (science data)
            self.assertGreater(abs(result.d_clock_raw_ms), 20.0,
                "Raw result should reflect the measurement jump")

        print("\nSUCCESS: Filter ignored 24ms jump and maintained stability.")

if __name__ == '__main__':
    unittest.main()
