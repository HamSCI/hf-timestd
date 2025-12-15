#!/usr/bin/env python3
"""
Reprocess ONLY BCD Discrimination Analysis

Runs BCD 100 Hz subcarrier analysis on archived NPZ files without
running any other discrimination methods. Useful for:
- Testing improved BCD algorithms
- Selective reprocessing after parameter tuning
- Validating BCD-specific fixes

Usage:
    python3 scripts/reprocess_bcd_only.py --date 20251115 --channel "WWV 10 MHz"
    python3 scripts/reprocess_bcd_only.py --date 20251115 --channel "WWV 10 MHz" --hour 12
"""

import argparse
import sys
import logging
import numpy as np
import csv
from pathlib import Path
from datetime import datetime, timezone

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator
from grape_recorder.paths import GRAPEPaths, channel_name_to_dir

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def reprocess_bcd_only(date_str: str, channel_name: str, hour: int = None, data_root: str = '/tmp/timestd-test'):
    """
    Reprocess ONLY BCD discrimination for a specific date/channel
    
    Args:
        date_str: Date in YYYYMMDD format
        channel_name: Channel name (e.g., "WWV 10 MHz")
        hour: Optional specific hour (0-23), or None for full day
        data_root: Root data directory
    """
    # Setup paths using GRAPEPaths API
    paths = GRAPEPaths(data_root)
    archive_dir = paths.get_archive_dir(channel_name)
    output_dir = paths.get_discrimination_dir(channel_name)
    channel_dir = channel_name_to_dir(channel_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Output CSV file
    if hour is not None:
        output_file = output_dir / f"{channel_dir}_bcd_{date_str}_{hour:02d}.csv"
        logger.info(f"Processing {channel_name} on {date_str} hour {hour:02d} (BCD only)")
    else:
        output_file = output_dir / f"{channel_dir}_bcd_{date_str}.csv"
        logger.info(f"Processing {channel_name} on {date_str} full day (BCD only)")
    
    logger.info(f"Archive dir: {archive_dir}")
    logger.info(f"Output file: {output_file}")
    
    # Initialize discriminator
    discriminator = WWVHDiscriminator(channel_name=channel_name)
    
    # Find NPZ files
    if hour is not None:
        pattern = f"{date_str}T{hour:02d}*.npz"
    else:
        pattern = f"{date_str}T*.npz"
    
    npz_files = sorted(archive_dir.glob(pattern))
    logger.info(f"Found {len(npz_files)} NPZ files")
    
    if not npz_files:
        logger.error("No NPZ files found!")
        return 1
    
    # CSV fieldnames
    fieldnames = ['timestamp_utc', 'window_start_sec', 'wwv_amplitude', 'wwvh_amplitude',
                 'differential_delay_ms', 'correlation_quality', 'amplitude_ratio_db']
    
    # Open output CSV
    csv_file = open(output_file, 'w', newline='')
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()
    
    # Process each NPZ file
    windows_processed = 0
    files_processed = 0
    
    for i, npz_file in enumerate(npz_files):
        if (i + 1) % 10 == 0:
            logger.info(f"Progress: {i+1}/{len(npz_files)} files, {windows_processed} BCD windows")
        
        try:
            # Load NPZ
            data = np.load(npz_file)
            iq_samples = data['iq']
            sample_rate = int(data['sample_rate'])
            unix_timestamp = float(data['unix_timestamp'])
            
            # Skip incomplete files
            if len(iq_samples) < 100000:
                continue
            
            # Run ONLY BCD discrimination
            wwv_amp, wwvh_amp, delay, quality, windows = discriminator.detect_bcd_discrimination(
                iq_samples, sample_rate, unix_timestamp
            )
            
            if windows:
                # Get timestamp in ISO format
                dt = datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)
                timestamp_utc = dt.isoformat()
                
                # Write each window to CSV
                for window in windows:
                    wwv_a = window.get('wwv_amplitude', 0)
                    wwvh_a = window.get('wwvh_amplitude', 0)
                    
                    # Calculate amplitude ratio (avoid log(0))
                    if wwv_a > 0 and wwvh_a > 0:
                        ratio_db = 20 * np.log10(wwv_a / wwvh_a)
                    else:
                        ratio_db = 0.0
                    
                    writer.writerow({
                        'timestamp_utc': timestamp_utc,
                        'window_start_sec': f"{window.get('window_start', 0.0):.1f}",
                        'wwv_amplitude': f"{wwv_a:.6f}",
                        'wwvh_amplitude': f"{wwvh_a:.6f}",
                        'differential_delay_ms': f"{window.get('delay', 0.0):.2f}",
                        'correlation_quality': f"{window.get('quality', 0.0):.3f}",
                        'amplitude_ratio_db': f"{ratio_db:.2f}"
                    })
                    windows_processed += 1
                
                files_processed += 1
            
        except Exception as e:
            logger.warning(f"Failed to process {npz_file.name}: {e}")
    
    csv_file.close()
    
    logger.info(f"\n{'='*70}")
    logger.info(f"BCD Reprocessing Complete")
    logger.info(f"{'='*70}")
    logger.info(f"Files processed: {files_processed}/{len(npz_files)}")
    logger.info(f"BCD windows extracted: {windows_processed}")
    logger.info(f"Output: {output_file}")
    logger.info(f"Average windows/file: {windows_processed/files_processed:.1f}" if files_processed > 0 else "No files processed")
    
    # Print sample statistics
    if windows_processed > 0:
        logger.info(f"\nReopen file to compute statistics...")
        
        with open(output_file, 'r') as f:
            reader = csv.DictReader(f)
            ratios = []
            for row in reader:
                ratio = float(row['amplitude_ratio_db'])
                if abs(ratio) < 100:  # Sanity check
                    ratios.append(ratio)
        
        if ratios:
            logger.info(f"BCD Amplitude Ratio Statistics:")
            logger.info(f"  Count: {len(ratios)}")
            logger.info(f"  Mean: {np.mean(ratios):+.2f} dB")
            logger.info(f"  Std Dev: {np.std(ratios):.2f} dB")
            logger.info(f"  Min: {min(ratios):+.2f} dB")
            logger.info(f"  Max: {max(ratios):+.2f} dB")
            logger.info(f"  Significant (>3 dB): {sum(1 for r in ratios if abs(r) >= 3)} ({100*sum(1 for r in ratios if abs(r) >= 3)/len(ratios):.1f}%)")
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Reprocess ONLY BCD discrimination analysis from archived NPZ files'
    )
    parser.add_argument('--date', type=str, required=True,
                       help='Date in YYYYMMDD format')
    parser.add_argument('--channel', type=str, required=True,
                       help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--hour', type=int, default=None,
                       help='Optional specific hour (0-23)')
    parser.add_argument('--data-root', type=str, default='/tmp/timestd-test',
                       help='Root data directory (default: /tmp/timestd-test)')
    
    args = parser.parse_args()
    
    # Validate date format
    try:
        datetime.strptime(args.date, '%Y%m%d')
    except ValueError:
        logger.error(f"Invalid date format: {args.date}. Use YYYYMMDD")
        return 1
    
    # Validate hour if provided
    if args.hour is not None and not (0 <= args.hour <= 23):
        logger.error(f"Invalid hour: {args.hour}. Must be 0-23")
        return 1
    
    return reprocess_bcd_only(args.date, args.channel, args.hour, args.data_root)


if __name__ == '__main__':
    sys.exit(main())
