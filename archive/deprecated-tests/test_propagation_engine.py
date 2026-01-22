#!/usr/bin/env python3
import unittest
import numpy as np
from hf_timestd.core.propagation_engine import PropagationEngine, PropagationMode

class TestPropagationEngine(unittest.TestCase):
    def setUp(self):
        # Coordinates for WWV (Fort Collins, CO) to a receiver (e.g. Austin, TX)
        # WWV: 40.6782° N, 105.0402° W
        # Austin: 30.2672° N, 97.7431° W
        # Distance approx 1400 km
        self.engine = PropagationEngine()
        self.tx_loc = (40.6782, -105.0402)
        self.rx_loc = (30.2672, -97.7431)
        self.freq_mhz = 10.0

    def test_geometric_model_sanity(self):
        """Verify geometric model produces reasonable results."""
        # Great circle approx 1400 km
        # Speed of light ~300 km/ms
        # Ground wave (min delay): 1400 / 300 = 4.67 ms
        # 1-hop F (300km height): path longer
        
        delay, uncertainty = self.engine.estimate_delay(
            self.tx_loc, self.rx_loc, self.freq_mhz, 
            mode=PropagationMode.F_LAYER_1HOP
        )
        
        print(f"1-hop delay: {delay:.2f} ms")
        self.assertGreater(delay, 4.6)
        self.assertLess(delay, 10.0) # Should be around 5-6 ms
        self.assertGreater(uncertainty, 0.0)

    def test_mode_differences(self):
        """Verify multi-hop takes longer than single-hop."""
        delay_1, _ = self.engine.estimate_delay(
            self.tx_loc, self.rx_loc, self.freq_mhz, mode=PropagationMode.F_LAYER_1HOP
        )
        delay_2, _ = self.engine.estimate_delay(
            self.tx_loc, self.rx_loc, self.freq_mhz, mode=PropagationMode.F_LAYER_2HOP
        )
        
        print(f"1-hop: {delay_1:.2f} ms, 2-hop: {delay_2:.2f} ms")
        self.assertGreater(delay_2, delay_1)
        
    def test_fallback_behavior(self):
        """Verify fallback when IRI is not available (which is the case here)."""
        # We manually disable IRI if it was somehow enabled
        self.engine.use_iri = False
        
        delay, unc = self.engine.estimate_delay(
            self.tx_loc, self.rx_loc, self.freq_mhz
        )
        # Should use parameter lookup or geometric
        self.assertGreater(delay, 0)
        
if __name__ == '__main__':
    unittest.main()
