#!/usr/bin/env python3
"""
Verify Standard Time Signal Generator
"""
import sys
import os
import logging
import numpy as np

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from hf_timestd.core.standard_signal_generator import StandardTimeSignalGenerator

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger('hf_timestd.core.standard_signal_generator').setLevel(logging.INFO)

def verify_bpm_pulses():
    logger.info("--- Verifying BPM Pulses ---")
    gen = StandardTimeSignalGenerator(sample_rate=20000)
    
    # Check 10ms UTC Tick
    tick_utc = gen.generate_tick('BPM', 'standard')
    dur_utc = len(tick_utc) / 20000.0
    logger.info(f"BPM UTC Tick: {dur_utc:.3f}s (Expected 0.010)")
    assert np.isclose(dur_utc, 0.010, atol=0.001)
    
    # Check 100ms UT1 Tick
    tick_ut1 = gen.generate_tick('BPM', 'bpm_ut1')
    dur_ut1 = len(tick_ut1) / 20000.0
    logger.info(f"BPM UT1 Tick: {dur_ut1:.3f}s (Expected 0.100)")
    assert np.isclose(dur_ut1, 0.100, atol=0.001)
    
    # Check 300ms Minute Marker
    mark_min = gen.generate_tick('BPM', 'minute')
    dur_min = len(mark_min) / 20000.0
    logger.info(f"BPM Minute Marker: {dur_min:.3f}s (Expected 0.300)")
    assert np.isclose(dur_min, 0.300, atol=0.001)

def verify_test_signals():
    """Verify WWV Scientific Modulation Test Signal generation"""
    logger.info("\n--- Verifying WWV Test Signals ---")
    sr = 20000
    gen = StandardTimeSignalGenerator(sample_rate=sr)
    
    # Import Detector (assuming it exists in the codebase as reviewed)
    from hf_timestd.core.wwv_test_signal import WWVTestSignalDetector
    detector = WWVTestSignalDetector(sample_rate=sr)
    
    # Generate WWV Minute 8 (480 seconds into hour)
    logger.info("Generating WWV Minute 8 (Test Signal)...")
    minute_8_audio = gen.generate_minute('WWV', 8)
    
    # Verify length
    expected_len = 60 * sr
    if len(minute_8_audio) != expected_len:
        logger.error(f"Length mismatch: {len(minute_8_audio)} != {expected_len}")
        
    # Detect
    logger.info("Running Test Signal Detector...")
    # API is detect(iq_samples, minute_number, sample_rate)
    result = detector.detect(minute_8_audio, minute_number=8, sample_rate=sr)
    
    if result.detected:
        logger.info(f"✅ SUCCESS: Test Signal Detected! Confidence: {result.confidence:.2f}")
        logger.info(f"Tone Powers: {result.tone_powers_db}")
        logger.info(f"FSS: {result.frequency_selectivity_db} dB")
    else:
        logger.error("❌ FAILURE: Test Signal Not Detected")

if __name__ == "__main__":
    verify_bpm_pulses()
    verify_test_signals()
