#!/usr/bin/env python3
"""
WWV/WWVH Discrimination Testing Script

Tests and validates all 6 discrimination methods:
1. Timing Tones (1000/1200 Hz power ratio)
2. Tick Windows (5ms coherent integration)
3. 440 Hz Station ID (ground truth at minutes 1 & 2)
4. Test Signal (ground truth at minutes 8 & 44)
5. BCD Correlation (100 Hz cross-correlation)
6. Weighted Voting (final determination)

Usage:
    python3 scripts/test_discrimination.py [--minute N] [--all]
    
    --minute N   Process specific minute archive (e.g., --minute 8 for test signal)
    --all        Process all available archives
"""

import sys
import os
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
import argparse
import logging

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator, DiscriminationResult
from grape_recorder.grape.wwv_geographic_predictor import WWVGeographicPredictor

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)


def load_npz_archive(npz_path: Path) -> tuple:
    """Load IQ samples from NPZ archive"""
    try:
        data = np.load(npz_path)
        
        # Support both 'iq' and 'iq_samples' keys
        if 'iq' in data:
            iq_samples = data['iq']
        elif 'iq_samples' in data:
            iq_samples = data['iq_samples']
        else:
            raise ValueError(f"No IQ data found. Keys: {list(data.keys())}")
        
        sample_rate = int(data.get('sample_rate', 16000))
        
        # Try to get timestamp from archive
        if 'unix_timestamp' in data:
            timestamp = float(data['unix_timestamp'])
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            # Fall back to filename
            filename = npz_path.stem
            timestamp_str = filename.split('_')[0]
            dt = datetime.strptime(timestamp_str, '%Y%m%dT%H%M%SZ')
            dt = dt.replace(tzinfo=timezone.utc)
            timestamp = dt.timestamp()
        
        return iq_samples, sample_rate, timestamp, dt
    except Exception as e:
        logger.error(f"Failed to load {npz_path}: {e}")
        return None, None, None, None


def format_discrimination_result(result: DiscriminationResult, minute_number: int) -> str:
    """Format discrimination result for display"""
    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"MINUTE {minute_number:02d} DISCRIMINATION RESULTS")
    lines.append(f"{'='*70}")
    
    # Ground truth indicator
    ground_truth_minutes = {1: "WWVH 440Hz", 2: "WWV 440Hz", 8: "WWV Test Signal", 44: "WWVH Test Signal"}
    if minute_number in ground_truth_minutes:
        lines.append(f"⚡ GROUND TRUTH MINUTE: {ground_truth_minutes[minute_number]}")
    
    lines.append(f"\n📊 FINAL DETERMINATION:")
    lines.append(f"   Dominant Station: {result.dominant_station or 'NONE'}")
    lines.append(f"   Confidence: {result.confidence}")
    
    # Method 1: Timing Tones
    lines.append(f"\n📈 METHOD 1: Timing Tones (1000/1200 Hz)")
    lines.append(f"   WWV (1000 Hz):  {'DETECTED' if result.wwv_detected else 'not detected'}")
    if result.wwv_power_db is not None:
        lines.append(f"                   Power: {result.wwv_power_db:.1f} dB")
    lines.append(f"   WWVH (1200 Hz): {'DETECTED' if result.wwvh_detected else 'not detected'}")
    if result.wwvh_power_db is not None:
        lines.append(f"                   Power: {result.wwvh_power_db:.1f} dB")
    if result.power_ratio_db is not None:
        lines.append(f"   Power Ratio (WWV-WWVH): {result.power_ratio_db:+.1f} dB")
    if result.differential_delay_ms is not None:
        lines.append(f"   Differential Delay: {result.differential_delay_ms:+.2f} ms")
    
    # Method 2: Tick Windows
    lines.append(f"\n📈 METHOD 2: Tick Windows (5ms coherent integration)")
    if result.tick_windows_10sec:
        for i, tw in enumerate(result.tick_windows_10sec):
            method = tw.get('integration_method', 'unknown')
            wwv_snr = tw.get('wwv_snr_db', -100)
            wwvh_snr = tw.get('wwvh_snr_db', -100)
            ratio = tw.get('ratio_db', 0)
            ticks = tw.get('tick_count', 0)
            lines.append(f"   Window {i}: {method.upper()} - WWV={wwv_snr:.1f}dB, WWVH={wwvh_snr:.1f}dB, Ratio={ratio:+.1f}dB ({ticks} ticks)")
    else:
        lines.append(f"   No tick windows detected")
    
    # Method 3: 440 Hz Station ID
    lines.append(f"\n📈 METHOD 3: 440 Hz Station ID (Ground Truth)")
    if minute_number == 1:
        lines.append(f"   WWVH 440 Hz: {'✅ DETECTED' if result.tone_440hz_wwvh_detected else '❌ not detected'}")
        if result.tone_440hz_wwvh_power_db is not None:
            lines.append(f"                Power: {result.tone_440hz_wwvh_power_db:.1f} dB")
    elif minute_number == 2:
        lines.append(f"   WWV 440 Hz:  {'✅ DETECTED' if result.tone_440hz_wwv_detected else '❌ not detected'}")
        if result.tone_440hz_wwv_power_db is not None:
            lines.append(f"                Power: {result.tone_440hz_wwv_power_db:.1f} dB")
    else:
        lines.append(f"   (440 Hz only broadcast in minutes 1 and 2)")
    
    # Method 4: Test Signal
    lines.append(f"\n📈 METHOD 4: Test Signal (Ground Truth)")
    if minute_number in [8, 44]:
        expected = 'WWV' if minute_number == 8 else 'WWVH'
        lines.append(f"   Expected Station: {expected} (minute {minute_number})")
        lines.append(f"   Detected: {'✅ YES' if result.test_signal_detected else '❌ NO'}")
        if result.test_signal_detected:
            lines.append(f"   Station (from schedule): {result.test_signal_station}")
            if result.test_signal_confidence is not None:
                lines.append(f"   Confidence: {result.test_signal_confidence:.3f}")
            if result.test_signal_multitone_score is not None:
                lines.append(f"   Multi-tone Score: {result.test_signal_multitone_score:.3f}")
            if result.test_signal_chirp_score is not None:
                lines.append(f"   Chirp Score: {result.test_signal_chirp_score:.3f}")
            if result.test_signal_snr_db is not None:
                lines.append(f"   SNR: {result.test_signal_snr_db:.1f} dB")
            if result.test_signal_toa_offset_ms is not None:
                lines.append(f"   ToA Offset: {result.test_signal_toa_offset_ms:+.2f} ms")
    else:
        lines.append(f"   (Test signal only broadcast in minutes 8 and 44)")
    
    # Method 5: BCD Correlation
    lines.append(f"\n📈 METHOD 5: BCD Correlation (100 Hz subcarrier)")
    if result.bcd_wwv_amplitude is not None:
        # Use scientific notation for very small values
        if result.bcd_wwv_amplitude < 0.001:
            lines.append(f"   WWV Amplitude:  {result.bcd_wwv_amplitude:.2e}")
        else:
            lines.append(f"   WWV Amplitude:  {result.bcd_wwv_amplitude:.4f}")
    if result.bcd_wwvh_amplitude is not None:
        if result.bcd_wwvh_amplitude < 0.001:
            lines.append(f"   WWVH Amplitude: {result.bcd_wwvh_amplitude:.2e}")
        else:
            lines.append(f"   WWVH Amplitude: {result.bcd_wwvh_amplitude:.4f}")
    if result.bcd_wwv_amplitude is not None and result.bcd_wwvh_amplitude is not None:
        wwv_amp = max(result.bcd_wwv_amplitude, 1e-10)
        wwvh_amp = max(result.bcd_wwvh_amplitude, 1e-10)
        ratio_db = 20 * np.log10(wwv_amp / wwvh_amp)
        lines.append(f"   Amplitude Ratio: {ratio_db:+.1f} dB")
    if result.bcd_differential_delay_ms is not None:
        lines.append(f"   Differential Delay: {result.bcd_differential_delay_ms:.2f} ms")
    if result.bcd_correlation_quality is not None:
        lines.append(f"   Correlation Quality: {result.bcd_correlation_quality:.2f}")
    if result.bcd_windows:
        dual_count = sum(1 for w in result.bcd_windows if w.get('detection_type') == 'dual_peak')
        single_wwv = sum(1 for w in result.bcd_windows if 'single_peak_wwv' in w.get('detection_type', ''))
        single_wwvh = sum(1 for w in result.bcd_windows if 'single_peak_wwvh' in w.get('detection_type', ''))
        lines.append(f"   Windows: {len(result.bcd_windows)} total (dual:{dual_count}, wwv:{single_wwv}, wwvh:{single_wwvh})")
    
    # Doppler info
    if result.doppler_wwv_hz is not None or result.doppler_wwvh_hz is not None:
        lines.append(f"\n📈 DOPPLER ESTIMATION:")
        if result.doppler_wwv_hz is not None:
            lines.append(f"   WWV Doppler:  {result.doppler_wwv_hz:+.4f} Hz")
        if result.doppler_wwvh_hz is not None:
            lines.append(f"   WWVH Doppler: {result.doppler_wwvh_hz:+.4f} Hz")
        if result.doppler_max_coherent_window_sec is not None:
            lines.append(f"   Max Coherent Window: {result.doppler_max_coherent_window_sec:.1f} s")
        if result.doppler_quality is not None:
            lines.append(f"   Quality: {result.doppler_quality:.2f}")
    
    return '\n'.join(lines)


def validate_ground_truth(result: DiscriminationResult, minute_number: int) -> dict:
    """Validate discrimination against ground truth for special minutes"""
    validation = {
        'minute': minute_number,
        'is_ground_truth': minute_number in [1, 2, 8, 44],
        'passed': None,
        'expected': None,
        'actual': None,
        'method': None
    }
    
    if minute_number == 1:
        validation['expected'] = 'WWVH 440Hz detected'
        validation['actual'] = 'detected' if result.tone_440hz_wwvh_detected else 'not detected'
        validation['passed'] = result.tone_440hz_wwvh_detected
        validation['method'] = '440Hz Station ID'
    
    elif minute_number == 2:
        validation['expected'] = 'WWV 440Hz detected'
        validation['actual'] = 'detected' if result.tone_440hz_wwv_detected else 'not detected'
        validation['passed'] = result.tone_440hz_wwv_detected
        validation['method'] = '440Hz Station ID'
    
    elif minute_number == 8:
        validation['expected'] = 'WWV Test Signal detected'
        validation['actual'] = f"detected={result.test_signal_detected}, station={result.test_signal_station}"
        validation['passed'] = result.test_signal_detected and result.test_signal_station == 'WWV'
        validation['method'] = 'Test Signal'
    
    elif minute_number == 44:
        validation['expected'] = 'WWVH Test Signal detected'
        validation['actual'] = f"detected={result.test_signal_detected}, station={result.test_signal_station}"
        validation['passed'] = result.test_signal_detected and result.test_signal_station == 'WWVH'
        validation['method'] = 'Test Signal'
    
    return validation


def find_archives(archive_dir: Path, target_minute: int = None) -> list:
    """Find NPZ archives, optionally filtering by minute"""
    if not archive_dir.exists():
        return []
    
    archives = sorted(archive_dir.glob("*.npz"))
    
    if target_minute is not None:
        # Filter to specific minute
        filtered = []
        for f in archives:
            try:
                # Extract minute from filename (YYYYMMDDTHHMMSSZ_...)
                ts_str = f.stem.split('_')[0]
                minute = int(ts_str[11:13])  # MM part of HHMMSS
                if minute == target_minute:
                    filtered.append(f)
            except (ValueError, IndexError):
                pass
        return filtered
    
    return archives


def main():
    parser = argparse.ArgumentParser(description='Test WWV/WWVH Discrimination Methods')
    parser.add_argument('--minute', type=int, help='Process specific minute (0-59)')
    parser.add_argument('--all', action='store_true', help='Process all available archives')
    parser.add_argument('--channel', default='WWV_10_MHz', help='Channel to test (default: WWV_10_MHz)')
    parser.add_argument('--archive-dir', help='Override archive directory')
    parser.add_argument('--grid', default='EM38ww', help='Receiver grid square (default: EM38ww)')
    parser.add_argument('--limit', type=int, default=5, help='Max archives to process (default: 5)')
    args = parser.parse_args()
    
    # Determine archive directory
    if args.archive_dir:
        archive_dir = Path(args.archive_dir)
    else:
        archive_dir = Path(f"/tmp/timestd-test/archives/{args.channel}")
    
    if not archive_dir.exists():
        logger.error(f"Archive directory not found: {archive_dir}")
        sys.exit(1)
    
    logger.info(f"Testing discrimination for channel: {args.channel}")
    logger.info(f"Archive directory: {archive_dir}")
    logger.info(f"Receiver grid: {args.grid}")
    
    # Find archives
    if args.minute is not None:
        logger.info(f"Filtering for minute {args.minute}")
        archives = find_archives(archive_dir, args.minute)
    else:
        archives = find_archives(archive_dir)
    
    if not archives:
        logger.error("No archives found!")
        sys.exit(1)
    
    # Limit number of archives
    if not args.all:
        archives = archives[-args.limit:]
    
    logger.info(f"Found {len(archives)} archives to process")
    
    # Initialize discriminator
    discriminator = WWVHDiscriminator(
        channel_name=args.channel,
        receiver_grid=args.grid
    )
    
    # Extract frequency from channel name
    freq_mhz = None
    try:
        # Format: WWV_10_MHz or WWV_2.5_MHz
        parts = args.channel.split('_')
        freq_str = parts[1]
        freq_mhz = float(freq_str)
    except (IndexError, ValueError):
        logger.warning(f"Could not parse frequency from channel name: {args.channel}")
    
    # Process archives
    results = []
    validations = []
    
    for npz_path in archives:
        logger.info(f"\n{'='*70}")
        logger.info(f"Processing: {npz_path.name}")
        
        iq_samples, sample_rate, timestamp, dt = load_npz_archive(npz_path)
        
        if iq_samples is None:
            continue
        
        minute_number = dt.minute
        logger.info(f"Timestamp: {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC (minute {minute_number})")
        logger.info(f"Samples: {len(iq_samples)}, Sample rate: {sample_rate} Hz, Duration: {len(iq_samples)/sample_rate:.1f}s")
        
        # Run full discrimination
        result = discriminator.analyze_minute_with_440hz(
            iq_samples=iq_samples,
            sample_rate=sample_rate,
            minute_timestamp=timestamp,
            frequency_mhz=freq_mhz
        )
        
        if result:
            results.append((minute_number, result))
            
            # Display formatted results
            print(format_discrimination_result(result, minute_number))
            
            # Validate ground truth minutes
            validation = validate_ground_truth(result, minute_number)
            if validation['is_ground_truth']:
                validations.append(validation)
                status = "✅ PASS" if validation['passed'] else "❌ FAIL"
                print(f"\n🎯 GROUND TRUTH VALIDATION: {status}")
                print(f"   Method: {validation['method']}")
                print(f"   Expected: {validation['expected']}")
                print(f"   Actual: {validation['actual']}")
    
    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"Processed: {len(results)} minutes")
    
    if validations:
        passed = sum(1 for v in validations if v['passed'])
        total = len(validations)
        print(f"\nGround Truth Validations: {passed}/{total}")
        for v in validations:
            status = "✅" if v['passed'] else "❌"
            print(f"  {status} Minute {v['minute']:02d}: {v['method']} - {v['expected']}")
    
    # Station distribution
    if results:
        wwv_count = sum(1 for _, r in results if r.dominant_station == 'WWV')
        wwvh_count = sum(1 for _, r in results if r.dominant_station == 'WWVH')
        balanced_count = sum(1 for _, r in results if r.dominant_station == 'BALANCED')
        none_count = sum(1 for _, r in results if r.dominant_station in ['NONE', None])
        
        print(f"\nStation Distribution:")
        print(f"  WWV:      {wwv_count}")
        print(f"  WWVH:     {wwvh_count}")
        print(f"  BALANCED: {balanced_count}")
        print(f"  NONE:     {none_count}")
    
    print(f"\n{'='*70}")


if __name__ == '__main__':
    main()
