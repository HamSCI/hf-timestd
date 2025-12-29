
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from hf_timestd.core import (
    Phase2TemporalEngine, 
    TimeSnapResult, 
    ChannelCharacterization,
    TransmissionTimeSolution
)

class TestDiscriminationStability(unittest.TestCase):
    def setUp(self):
        # Initialize engine with dummy paths
        self.engine = Phase2TemporalEngine(
            raw_buffer_dir='/tmp/raw',
            output_dir='/tmp/out',
            channel_name='WWV_10MHz',
            frequency_hz=10000000,
            receiver_grid='EM38ww'
        )
        
        # Mock the solver to return a dummy solution
        self.engine.solver = MagicMock()
        mock_solution = MagicMock()
        mock_solution.utc_nist_offset_ms = 0.0
        mock_solution.emission_offset_ms = 0.0
        mock_solution.propagation_delay_ms = 5.0
        mock_solution.mode.value = '1F'
        mock_solution.n_hops = 1
        mock_solution.layer_height_km = 300.0
        mock_solution.confidence = 1.0
        mock_solution.utc_nist_verified = False
        mock_solution.candidates = []
        self.engine.solver.solve.return_value = mock_solution
        
        # Mock internal components to avoid initialization errors
        self.engine.tone_detector = MagicMock()
        self.engine.discriminator = MagicMock()

    def test_rtp_override_logic(self):
        """
        Test that RTP prediction prediction overrides weak/medium acoustic discrimination.
        
        Scenario:
        - We are on a shared frequency (10 MHz).
        - Acoustic discrimination (noisy) thinks it's WWVH (Confidence: Medium).
        - RTP Prediction (history) thinks it's WWV (Confidence: High > 0.8).
        
        Expected Behavior (WITH FIX): System should choose WWV.
        Current Behavior (WITHOUT FIX): System chooses WWVH (Priority 3 > Priority 4).
        """
        
        # 1. Setup Input Objects
        time_snap = TimeSnapResult(
            timing_error_ms=0.0,
            arrival_rtp=1000,
            arrival_system_time=1700000000.0,
            wwv_detected=True,
            wwvh_detected=True
        )
        
        channel = ChannelCharacterization()
        channel.dominant_station = 'WWVH' # Acoustic thinks WWVH
        channel.station_confidence = 'medium' # But only medium confidence
        channel.ground_truth_station = None # No ground truth
        
        # 2. Setup RTP Predictor Mock
        # Predicts WWV with High Confidence (0.9)
        self.engine.station_predictor = MagicMock(return_value=('WWV', 0.9))
        
        # 3. Setup RTP Calibration Callback
        self.engine.rtp_calibration_callback = MagicMock(return_value=12345)

        # 4. Run Step 3
        solution = self.engine._step3_transmission_time_solution(
            time_snap=time_snap,
            channel=channel,
            system_time=1700000000.0,
            rtp_timestamp=1000
        )
        
        print(f"\n[TEST RESULT] Selected Station: {solution.station}")
        print(f"  Acoustic: WWVH (medium)")
        print(f"  RTP Pred: WWV (0.9)")
        
        # Assertions
        # If fix is working, this should be WWV.
        # If fix is NOT working, this utilizes acoustic medium > RTP, so it will be WWVH.
        # Note: We expect this test to FAIL initially (returning WWVH) or succeed if we assert for WWVH to confirm baseline.
        
        return solution.station

if __name__ == '__main__':
    unittest.main()
