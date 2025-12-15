#!/usr/bin/env python3
"""
Verify Standard Time Signal Generator
"""
import sys
import os
import logging
import numpy as np
from datetime import datetime, timedelta

# Add src to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from hf_timestd.core.standard_signal_generator import StandardTimeSignalGenerator
from hf_timestd.core.chu_fsk_decoder import CHUFSKDecoder

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger('hf_timestd.core.chu_fsk_decoder').setLevel(logging.DEBUG)
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

def verify_chu_afsk_loopback():
    logger.info("\n--- Verifying CHU AFSK Loopback ---")
    sr = 20000
    gen = StandardTimeSignalGenerator(sample_rate=sr)
    decoder = CHUFSKDecoder(sample_rate=sr)
    
    # Test Time: Day 100, 12:30:00 UTC
    year = 2024
    day = 100
    hour = 12
    minute = 30
    
    logger.info(f"Generating CHU Minute for {year}-{day:03d} {hour}:{minute} UTC")
    
    # Manually construct a minute buffer to save time (just seconds 31-39)
    # But generate_second_combined needs context.
    # We'll generate just the relevant seconds.
    
    seconds_to_test = range(31, 40)
    full_buffer = np.zeros(60 * sr) # Full minute for simplicity
    
    for sec in seconds_to_test:
        sec_audio = gen.generate_second_combined('CHU', sec, minute, hour, day, year)
        start = sec * sr
        full_buffer[start : start + len(sec_audio)] = sec_audio
        
    # Decode
    logger.info("Decoding...")
    # Timestamp needs to be correct for decoder? Decoder outputs decoded fields.
    # Calculate the Unix timestamp for the start of the minute
    minute_start_dt = datetime(year, 1, 1, hour, minute, 0) + timedelta(days=day - 1)
    minute_start_ts = minute_start_dt.timestamp()
    result = decoder.decode_minute(full_buffer, minute_start_ts, is_audio=True)
    # Timestamp doesn't effect decode of content
    
    logger.info(f"Detected: {result.detected}")
    logger.info(f"Frames Decoded: {result.frames_decoded}/9")
    logger.info(f"Decoded Time: Day {result.decoded_day} {result.decoded_hour}:{result.decoded_minute}")
    
    if result.detected:
        logger.info("✅ SUCCESS: CHU AFSK Loopback Passed")
    else:
        logger.error("❌ FAILURE: CHU AFSK Decode Failed")

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
    verify_chu_afsk_loopback()
    verify_test_signals()
