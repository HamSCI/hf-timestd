#!/usr/bin/env python3
"""
Reprocess discrimination data for a specific time range

Usage:
    python3 scripts/reprocess_discrimination_timerange.py \
        --date 20251119 --channel "WWV 10 MHz" \
        --start-hour 12 --end-hour 16
"""

import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator
import numpy as np
import csv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def reprocess_timerange(date_str: str, channel_name: str, start_hour: int, end_hour: int, data_root: str):
    """Reprocess discrimination for a specific time range"""
    
    # Setup paths using GRAPEPaths API
    from grape_recorder.paths import GRAPEPaths, channel_name_to_dir
    paths = GRAPEPaths(data_root)
    archive_dir = paths.get_archive_dir(channel_name)
    output_dir = paths.get_discrimination_dir(channel_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # NOTE: This script creates time-range suffix files for partial reprocessing
    # These should be merged into daily files: {channel}_discrimination_{date}.csv
    channel_dir = channel_name_to_dir(channel_name)
    output_file = output_dir / f"{channel_dir}_discrimination_{date_str}_{start_hour:02d}-{end_hour:02d}.csv"
    
    logger.info(f"Processing {channel_name} on {date_str} from {start_hour:02d}:00 to {end_hour:02d}:00")
    logger.info(f"Archive dir: {archive_dir}")
    logger.info(f"Output file: {output_file}")
    
    # Initialize discriminator (tone detection now handled internally)
    discriminator = WWVHDiscriminator(channel_name=channel_name)
    
    # Find NPZ files in time range
    year = int(date_str[:4])
    month = int(date_str[4:6])
    day = int(date_str[6:8])
    
    npz_files = []
    for hour in range(start_hour, end_hour):
        pattern = f"{date_str}T{hour:02d}*.npz"
        hour_files = sorted(archive_dir.glob(pattern))
        npz_files.extend(hour_files)
    
    logger.info(f"Found {len(npz_files)} NPZ files in time range")
    
    if not npz_files:
        logger.error("No NPZ files found!")
        return
    
    # Process files
    results = []
    
    for i, npz_file in enumerate(npz_files):
        if (i + 1) % 10 == 0:
            logger.info(f"Progress: {i+1}/{len(npz_files)} files")
        
        try:
            # Load NPZ
            data = np.load(npz_file)
            iq_samples = data['iq']
            sample_rate = int(data['sample_rate'])
            unix_timestamp = float(data['unix_timestamp'])
            
            # Skip incomplete files
            if len(iq_samples) < 100000:
                continue
            
            # Analyze with ALL discrimination methods (fully independent)
            # Tone detection now happens automatically from IQ samples
            result = discriminator.analyze_minute_with_440hz(
                iq_samples=iq_samples,
                sample_rate=sample_rate,
                minute_timestamp=unix_timestamp
                # detections parameter omitted - will detect tones internally
            )
            
            if result:
                results.append(result)
                
        except Exception as e:
            logger.warning(f"Failed to process {npz_file.name}: {e}")
    
    logger.info(f"Processed {len(results)} minutes successfully")
    
    # Write CSV
    if results:
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            
            # Header
            writer.writerow([
                'timestamp_utc', 'minute_timestamp', 'minute_number',
                'wwv_detected', 'wwvh_detected',
                'wwv_power_db', 'wwvh_power_db', 'power_ratio_db', 'differential_delay_ms',
                'tone_440hz_wwv_detected', 'tone_440hz_wwv_power_db',
                'tone_440hz_wwvh_detected', 'tone_440hz_wwvh_power_db',
                'dominant_station', 'confidence', 'tick_windows_10sec',
                'bcd_wwv_amplitude', 'bcd_wwvh_amplitude',
                'bcd_differential_delay_ms', 'bcd_correlation_quality', 'bcd_windows'
            ])
            
            # Data rows
            for result in results:
                import json
                
                # Generate timestamp_utc from minute_timestamp
                dt = datetime.fromtimestamp(result.minute_timestamp, timezone.utc)
                timestamp_utc = dt.isoformat()
                minute_number = dt.minute
                
                # Serialize tick windows to JSON
                tick_json = ''
                if result.tick_windows_10sec:
                    tick_json = json.dumps(result.tick_windows_10sec)
                
                # Serialize BCD windows to JSON
                bcd_json = ''
                if result.bcd_windows:
                    bcd_json = json.dumps(result.bcd_windows)
                
                row = [
                    timestamp_utc,
                    result.minute_timestamp,
                    minute_number,
                    1 if result.wwv_detected else 0,
                    1 if result.wwvh_detected else 0,
                    f"{result.wwv_power_db:.2f}" if result.wwv_power_db is not None else '',
                    f"{result.wwvh_power_db:.2f}" if result.wwvh_power_db is not None else '',
                    f"{result.power_ratio_db:.2f}" if result.power_ratio_db is not None else '',
                    f"{result.differential_delay_ms:.2f}" if result.differential_delay_ms is not None else '',
                    1 if result.tone_440hz_wwv_detected else 0,
                    f"{result.tone_440hz_wwv_power_db:.2f}" if result.tone_440hz_wwv_power_db is not None else '',
                    1 if result.tone_440hz_wwvh_detected else 0,
                    f"{result.tone_440hz_wwvh_power_db:.2f}" if result.tone_440hz_wwvh_power_db is not None else '',
                    result.dominant_station if result.dominant_station else '',
                    result.confidence if result.confidence else 'low',
                    tick_json,
                    f"{result.bcd_wwv_amplitude:.2f}" if result.bcd_wwv_amplitude is not None else '',
                    f"{result.bcd_wwvh_amplitude:.2f}" if result.bcd_wwvh_amplitude is not None else '',
                    f"{result.bcd_differential_delay_ms:.2f}" if result.bcd_differential_delay_ms is not None else '',
                    f"{result.bcd_correlation_quality:.2f}" if result.bcd_correlation_quality is not None else '',
                    bcd_json
                ]
                writer.writerow(row)
        
        logger.info(f"✅ Wrote {len(results)} rows to {output_file}")
    else:
        logger.warning("No results to write!")


def main():
    parser = argparse.ArgumentParser(description='Reprocess discrimination for time range')
    parser.add_argument('--date', required=True, help='Date in YYYYMMDD format')
    parser.add_argument('--channel', required=True, help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--start-hour', type=int, required=True, help='Start hour (0-23)')
    parser.add_argument('--end-hour', type=int, required=True, help='End hour (1-24)')
    parser.add_argument('--data-root', default='/tmp/timestd-test', help='Data root directory')
    args = parser.parse_args()
    
    reprocess_timerange(args.date, args.channel, args.start_hour, args.end_hour, args.data_root)


if __name__ == '__main__':
    main()
