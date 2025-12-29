
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from hf_timestd.core import (
    Phase2TemporalEngine, 
    ChannelCharacterization,
    TransmissionTimeSolution
)

class TestPipelineIntegration(unittest.TestCase):
    def setUp(self):
        self.output_dir = '/tmp/phase2_test_out'
        
    def test_dependency_injection(self):
        """Verify that we can inject mocks for discriminator and solver."""
        mock_discriminator = MagicMock()
        mock_solver = MagicMock()
        
        engine = Phase2TemporalEngine(
            raw_buffer_dir='/tmp/raw',
            output_dir=self.output_dir,
            channel_name='WWV_10MHz',
            frequency_hz=10000000,
            receiver_grid='EM38ww',
            discriminator=mock_discriminator,
            solver=mock_solver
        )
        
        self.assertEqual(engine.discriminator, mock_discriminator)
        self.assertEqual(engine.solver, mock_solver)
        
        # Verify internal initialization didn't start real components
        # (This assumes side effects of real components would fail or be slow if they ran, 
        # but primarily we check identity)
        
    def test_uncertainty_scaling(self):
        """Verify that uncertainty scales logically with SNR."""
        engine = Phase2TemporalEngine(
            raw_buffer_dir='/tmp/raw',
            output_dir=self.output_dir,
            channel_name='WWV_10MHz',
            frequency_hz=10000000,
            receiver_grid='EM38ww'
        )
        
        # Case 1: High SNR (30 dB)
        channel_high = ChannelCharacterization()
        channel_high.snr_db = 30.0
        channel_high.delay_spread_ms = 1.0
        channel_high.doppler_wwv_std_hz = 0.1
        
        unc_high, _ = engine._calculate_physics_based_uncertainty(channel_high)
        
        # Case 2: Low SNR (10 dB)
        channel_low = ChannelCharacterization()
        channel_low.snr_db = 10.0
        channel_low.delay_spread_ms = 1.0
        channel_low.doppler_wwv_std_hz = 0.1
        
        unc_low, _ = engine._calculate_physics_based_uncertainty(channel_low)
        
        print(f"\nUncertainty Scaling Test:")
        print(f"  High SNR (30dB) Uncertainty: {unc_high:.2f} ms")
        print(f"  Low SNR (10dB) Uncertainty:  {unc_low:.2f} ms")
        
        self.assertLess(unc_high, unc_low, "Uncertainty should increase as SNR decreases")
        
        # Case 3: High Multipath (Delay Spread)
        channel_bad_prop = ChannelCharacterization()
        channel_bad_prop.snr_db = 30.0
        channel_bad_prop.delay_spread_ms = 5.0 # Bad multipath
        channel_bad_prop.doppler_wwv_std_hz = 0.1

        unc_bad_prop, _ = engine._calculate_physics_based_uncertainty(channel_bad_prop)
        print(f"  High SNR, Bad Prop (5ms) Uncertainty: {unc_bad_prop:.2f} ms")
        
        self.assertLess(unc_high, unc_bad_prop, "Uncertainty should increase with delay spread")

if __name__ == '__main__':
    unittest.main()
