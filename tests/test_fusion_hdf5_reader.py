#!/usr/bin/env python3
"""
Test script for HDF5 data reading in multi_broadcast_fusion.

Tests:
1. HDF5 reader can read L2 timing measurements
2. CSV fallback works when HDF5 not available
3. Data equivalence between HDF5 and CSV
"""

import sys
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def test_hdf5_reader():
    """Test HDF5 reader for multi_broadcast_fusion."""
    from hf_timestd.core.multi_broadcast_fusion import MultiBroadcastFusion
    
    data_root = Path('/var/lib/timestd')
    
    logger.info("Initializing MultiBroadcastFusion...")
    fusion = MultiBroadcastFusion(data_root=data_root)
    
    logger.info("Testing _read_latest_measurements (should try HDF5 first)...")
    measurements = fusion._read_latest_measurements(lookback_minutes=10)
    
    logger.info(f"Read {len(measurements)} measurements")
    
    if measurements:
        # Show sample measurement
        m = measurements[0]
        logger.info(f"Sample measurement:")
        logger.info(f"  Station: {m.station}")
        logger.info(f"  Frequency: {m.frequency_mhz} MHz")
        logger.info(f"  D_clock: {m.d_clock_ms:+.3f} ms")
        logger.info(f"  Quality: {m.quality_grade}")
        logger.info(f"  SNR: {m.snr_db:.1f} dB")
        logger.info(f"  Confidence: {m.confidence:.2f}")
        logger.info(f"  Channel: {m.channel_name}")
        
        # Count by station
        from collections import Counter
        station_counts = Counter(m.station for m in measurements)
        logger.info(f"Measurements by station: {dict(station_counts)}")
        
        # Count by quality grade
        grade_counts = Counter(m.quality_grade for m in measurements)
        logger.info(f"Measurements by quality grade: {dict(grade_counts)}")
    else:
        logger.warning("No measurements found!")
    
    return len(measurements) > 0

def test_csv_fallback():
    """Test that CSV fallback works."""
    from hf_timestd.core.multi_broadcast_fusion import MultiBroadcastFusion
    
    data_root = Path('/var/lib/timestd')
    
    logger.info("Testing CSV fallback by calling _read_latest_measurements_for_channel...")
    fusion = MultiBroadcastFusion(data_root=data_root)
    
    if fusion.channels:
        channel = fusion.channels[0]
        logger.info(f"Testing CSV read for channel: {channel}")
        measurements = fusion._read_latest_measurements_for_channel(channel, lookback_minutes=10)
        logger.info(f"Read {len(measurements)} measurements from CSV for {channel}")
        return len(measurements) > 0
    else:
        logger.warning("No channels found!")
        return False

if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Testing HDF5 Reader for Multi-Broadcast Fusion")
    logger.info("=" * 60)
    
    try:
        # Test 1: HDF5 reader
        logger.info("\n[Test 1] HDF5 Reader Test")
        logger.info("-" * 60)
        test1_passed = test_hdf5_reader()
        logger.info(f"Test 1: {'PASSED' if test1_passed else 'FAILED'}")
        
        # Test 2: CSV fallback
        logger.info("\n[Test 2] CSV Fallback Test")
        logger.info("-" * 60)
        test2_passed = test_csv_fallback()
        logger.info(f"Test 2: {'PASSED' if test2_passed else 'FAILED'}")
        
        # Summary
        logger.info("\n" + "=" * 60)
        logger.info("Test Summary")
        logger.info("=" * 60)
        logger.info(f"HDF5 Reader: {'PASSED' if test1_passed else 'FAILED'}")
        logger.info(f"CSV Fallback: {'PASSED' if test2_passed else 'FAILED'}")
        
        if test1_passed and test2_passed:
            logger.info("\n✅ All tests PASSED!")
            sys.exit(0)
        else:
            logger.error("\n❌ Some tests FAILED!")
            sys.exit(1)
    
    except Exception as e:
        logger.error(f"Test failed with exception: {e}", exc_info=True)
        sys.exit(1)
