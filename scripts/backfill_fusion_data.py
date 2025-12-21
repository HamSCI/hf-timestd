#!/usr/bin/env python3
"""
Backfill Fusion Data Script

This script re-processes existing raw buffer data to generate the missing
_utc_nist_ CSV files required for the Fusion data visualization.

It utilizes the Phase2AnalyticsService to replay the analysis pipeline.
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timezone
import argparse

# Ensure src is in path to import hf_timestd modules
src_path = Path(__file__).resolve().parent.parent / 'src'
sys.path.append(str(src_path))

from hf_timestd.core.phase2_analytics_service import Phase2AnalyticsService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger("backfill_fusion")

def process_channel(channel_dir, target_date_str):
    """
    Process all minutes for a specific channel and date.
    
    Args:
        channel_dir: Path to raw_buffer/{CHANNEL} directory
        target_date_str: Date string YYYYMMDD to process
    """
    channel_name = channel_dir.name
    logger.info(f"Processing channel: {channel_name}")
    
    # Heuristic to determine frequency from channel name (e.g. SHARED_2500)
    parts = channel_name.split('_')
    if len(parts) >= 2 and parts[-1].isdigit():
        freq_khz = int(parts[-1])
        frequency_hz = freq_khz * 1000.0
    else:
        # Fallback for known stations
        if 'WWV' in channel_name: frequency_hz = 10000000.0
        elif 'WWVH' in channel_name: frequency_hz = 10000000.0
        elif 'CHU' in channel_name: frequency_hz = 7850000.0
        else:
            logger.warning(f"Could not determine frequency for {channel_name}, skipping")
            return

    # Define paths
    # Note: Phase2AnalyticsService expects archive_dir to be raw_buffer/{CHANNEL}
    archive_dir = channel_dir
    
    # Output to standard Phase 2 location
    # data_root is assumed to be parent of raw_buffer
    data_root = archive_dir.parent.parent
    output_dir = data_root / 'phase2' / channel_name
    
    logger.info(f"  Archive: {archive_dir}")
    logger.info(f"  Output: {output_dir}")
    
    # Initialize Service
    # We use a dummy grid square as it's not critical for backfilling if not strictly known,
    # but EM38ww is a safe default for testing/dev environments if unknown.
    service = Phase2AnalyticsService(
        archive_dir=archive_dir,
        output_dir=output_dir,
        channel_name=channel_name,
        frequency_hz=frequency_hz,
        receiver_grid='EM38ww',
        sample_rate=20000,
        use_tiered_storage=False # Force reading from disk
    )
    
    # Find files for the target date
    date_dir = archive_dir / target_date_str
    if not date_dir.exists():
        logger.warning(f"  No data found for date {target_date_str} in {channel_dir}")
        return
        
    bin_files = sorted(date_dir.glob('*.bin'))
    if not bin_files:
        logger.warning(f"  No .bin files found in {date_dir}")
        return
        
    logger.info(f"  Found {len(bin_files)} minute files to process")
    
    count = 0
    for bf in bin_files:
        try:
            timestamp = int(bf.stem)
            
            # Check if output already exists? 
            # Actually, we want to OVERWRITE or APPEND to the _utc_nist_ csv.
            # The service appends.
            
            # Running process_minute will re-run analysis and write all CSVs
            # This is robust but computationally expensive.
            # Given we have < 1440 minutes, it should be fast enough.
            service.process_minute(timestamp)
            count += 1
            
            if count % 10 == 0:
                print(f"  Processed {count}/{len(bin_files)} minutes...", end='\r')
                
        except ValueError:
            continue
        except Exception as e:
            logger.error(f"  Error processing {bf}: {e}")
            
    print(f"  Completed {count} minutes for {channel_name}")

def main():
    parser = argparse.ArgumentParser(description="Backfill Fusion Data")
    parser.add_argument('--date', type=str, default='20251219', help='Date to process (YYYYMMDD)')
    parser.add_argument('--buffers', type=str, default='/var/lib/timestd/raw_buffer', help='Path to raw_buffer root')
    args = parser.parse_args()
    
    raw_buffer_root = Path(args.buffers)
    
    if not raw_buffer_root.exists():
        logger.error(f"Raw buffer root not found: {raw_buffer_root}")
        sys.exit(1)
        
    # Iterate over all channels
    for channel_dir in sorted(raw_buffer_root.iterdir()):
        if channel_dir.is_dir():
            process_channel(channel_dir, args.date)

if __name__ == "__main__":
    main()
