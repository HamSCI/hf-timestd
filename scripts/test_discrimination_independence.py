#!/usr/bin/env python3
"""
Test Discrimination Method Independence

Verifies that all 5 discrimination methods can be called independently
from archived 16 kHz IQ data.

Usage:
    python3 scripts/test_discrimination_independence.py --npz-file /path/to/archive.npz
"""

import argparse
import sys
import logging
from pathlib import Path
import numpy as np

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_method_independence(npz_file: Path):
    """
    Test that all 5 discrimination methods can be called independently
    
    Args:
        npz_file: Path to 16 kHz archive NPZ file
    """
    logger.info(f"Loading NPZ file: {npz_file}")
    
    # Load archived IQ data
    data = np.load(npz_file)
    iq_samples = data['iq']
    sample_rate = int(data['sample_rate'])
    minute_timestamp = float(data['unix_timestamp'])
    
    logger.info(f"Sample rate: {sample_rate} Hz")
    logger.info(f"IQ samples: {len(iq_samples)}")
    logger.info(f"Duration: {len(iq_samples) / sample_rate:.1f} seconds")
    
    dt = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
    logger.info(f"Timestamp: {dt.isoformat()}")
    minute_number = dt.minute
    logger.info(f"Minute of hour: {minute_number}")
    
    # Initialize discriminator
    discriminator = WWVHDiscriminator(channel_name="TEST")
    
    print("\n" + "="*70)
    print("TESTING METHOD INDEPENDENCE")
    print("="*70)
    
    # METHOD 1: Timing Tones (800ms WWV/WWVH)
    print("\n[1/5] Testing detect_timing_tones()...")
    try:
        wwv_power, wwvh_power, diff_delay, detections = discriminator.detect_timing_tones(
            iq_samples, sample_rate, minute_timestamp
        )
        print(f"  ✅ SUCCESS")
        print(f"     WWV power: {wwv_power:.2f} dB" if wwv_power else "     WWV power: None")
        print(f"     WWVH power: {wwvh_power:.2f} dB" if wwvh_power else "     WWVH power: None")
        print(f"     Differential delay: {diff_delay:.2f} ms" if diff_delay else "     Differential delay: None")
        print(f"     Detections: {len(detections)}")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    # METHOD 2: Tick Windows (5ms ticks)
    print("\n[2/5] Testing detect_tick_windows()...")
    try:
        tick_windows = discriminator.detect_tick_windows(iq_samples, sample_rate)
        print(f"  ✅ SUCCESS")
        print(f"     Windows: {len(tick_windows)}")
        if tick_windows:
            good_windows = [w for w in tick_windows if w['wwv_snr_db'] > 0 or w['wwvh_snr_db'] > 0]
            print(f"     Valid windows: {len(good_windows)}/{len(tick_windows)}")
            if good_windows:
                avg_ratio = np.mean([w['ratio_db'] for w in good_windows])
                print(f"     Average ratio: {avg_ratio:+.1f} dB")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    # METHOD 3: 440 Hz Station ID
    print(f"\n[3/5] Testing detect_440hz_tone() (minute {minute_number})...")
    try:
        detected, power_db = discriminator.detect_440hz_tone(iq_samples, sample_rate, minute_number)
        print(f"  ✅ SUCCESS")
        expected_station = "WWVH" if minute_number == 1 else "WWV" if minute_number == 2 else "None"
        print(f"     Expected 440 Hz: {expected_station}")
        print(f"     Detected: {detected}")
        if detected and power_db:
            print(f"     Power: {power_db:.2f} dB")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    # METHOD 4: BCD Discrimination (100 Hz)
    print("\n[4/5] Testing detect_bcd_discrimination()...")
    try:
        wwv_amp, wwvh_amp, delay, quality, windows = discriminator.detect_bcd_discrimination(
            iq_samples, sample_rate, minute_timestamp
        )
        print(f"  ✅ SUCCESS")
        if wwv_amp and wwvh_amp:
            ratio_db = 20 * np.log10(wwv_amp / wwvh_amp) if wwv_amp > 0 and wwvh_amp > 0 else 0
            print(f"     WWV amplitude: {wwv_amp:.4f}")
            print(f"     WWVH amplitude: {wwvh_amp:.4f}")
            print(f"     Amplitude ratio: {ratio_db:+.2f} dB")
        if windows:
            print(f"     Windows: {len(windows)}")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    # METHOD 5: Full Analysis (all methods combined)
    print("\n[5/5] Testing analyze_minute_with_440hz() (full pipeline)...")
    try:
        result = discriminator.analyze_minute_with_440hz(
            iq_samples=iq_samples,
            sample_rate=sample_rate,
            minute_timestamp=minute_timestamp,
            detections=None  # Test independent mode (no external detections)
        )
        print(f"  ✅ SUCCESS")
        if result:
            print(f"     Dominant station: {result.dominant_station}")
            print(f"     Confidence: {result.confidence}")
            print(f"     WWV power: {result.wwv_power_db:.2f} dB" if result.wwv_power_db else "     WWV power: None")
            print(f"     WWVH power: {result.wwvh_power_db:.2f} dB" if result.wwvh_power_db else "     WWVH power: None")
    except Exception as e:
        print(f"  ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "="*70)
    print("INDEPENDENCE TEST COMPLETE")
    print("="*70)


def main():
    parser = argparse.ArgumentParser(description='Test discrimination method independence')
    parser.add_argument('--npz-file', type=str, required=False,
                       help='Path to 16 kHz archive NPZ file')
    parser.add_argument('--auto', action='store_true',
                       help='Auto-find a recent NPZ file from archives')
    
    args = parser.parse_args()
    
    if args.auto:
        # Find a recent NPZ file automatically
        archive_dir = Path('/tmp/timestd-test/archives/WWV_10_MHz')
        npz_files = sorted(archive_dir.glob('*.npz'))
        if npz_files:
            npz_file = npz_files[-1]  # Most recent
            logger.info(f"Auto-selected: {npz_file}")
        else:
            logger.error("No NPZ files found in /tmp/timestd-test/archives/WWV_10_MHz")
            return 1
    elif args.npz_file:
        npz_file = Path(args.npz_file)
        if not npz_file.exists():
            logger.error(f"NPZ file not found: {npz_file}")
            return 1
    else:
        parser.print_help()
        return 1
    
    test_method_independence(npz_file)
    return 0


if __name__ == '__main__':
    sys.exit(main())
