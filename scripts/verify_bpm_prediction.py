#!/usr/bin/env python3
"""
Verify BPM Station Integration
"""
import sys
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from hf_timestd.core.wwv_geographic_predictor import WWVGeographicPredictor
from hf_timestd.core.propagation_mode_solver import PropagationModeSolver

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def verify_bpm():
    receiver_grid = 'EM38ww'  # Midwest US
    logger.info(f"--- Verifying BPM Integration for Receiver {receiver_grid} ---")
    
    # 1. Geographic Predictor
    logger.info("\n1. Testing WWVGeographicPredictor")
    predictor = WWVGeographicPredictor(receiver_grid=receiver_grid)
    
    expected_delay = predictor.calculate_expected_delay_bpm(frequency_mhz=10.0)
    logger.info(f"BPM Expected Delay (10 MHz): {expected_delay:.2f} ms")
    
    # 2. Propagation Mode Solver
    logger.info("\n2. Testing PropagationModeSolver")
    solver = PropagationModeSolver(receiver_grid=receiver_grid)
    
    bpm_dist = solver.get_station_distance_km('BPM')
    logger.info(f"Distance to BPM: {bpm_dist:.1f} km")
    
    logger.info("Calculating modes for BPM (10 MHz):")
    modes = solver.calculate_modes('BPM', frequency_mhz=10.0, max_hops=6)
    
    found_valid = False
    for i, mode in enumerate(modes):
        logger.info(f"  Mode {i+1}: {mode}")
        if mode.viable:
            found_valid = True
            
    if not found_valid:
        logger.warning("No valid modes found (expected for very long path with default settings?)")
    else:
        logger.info("VALID modes found.")

    logger.info("\n--- Verification Complete ---")

if __name__ == "__main__":
    verify_bpm()
