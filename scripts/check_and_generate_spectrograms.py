#!/usr/bin/env python3
"""
Check for missing spectrograms and generate them automatically.
Can run periodically to ensure all products exist for available data.
"""

import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Set

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def find_days_with_archives(data_root: Path) -> Set[str]:
    """Find all dates that have NPZ archive data"""
    archives_dir = data_root / 'archives'
    dates = set()
    
    if not archives_dir.exists():
        logger.warning(f"Archives directory not found: {archives_dir}")
        return dates
    
    # Scan all channel directories for dates
    for channel_dir in archives_dir.glob('*'):
        if not channel_dir.is_dir():
            continue
        
        # Find all NPZ files and extract dates
        for npz_file in channel_dir.glob('*T*Z_*_iq.npz'):
            # Filename format: YYYYMMDDTHHmmssZ_...
            date_str = npz_file.name[:8]
            if date_str.isdigit() and len(date_str) == 8:
                dates.add(date_str)
    
    return dates


def find_days_with_spectrograms(data_root: Path) -> Set[str]:
    """Find all dates that have complete spectrograms"""
    spectrograms_dir = data_root / 'spectrograms'
    dates = set()
    
    if not spectrograms_dir.exists():
        return dates
    
    # Expected channels
    expected_channels = [
        'WWV_2.5_MHz', 'WWV_5_MHz', 'WWV_10_MHz', 
        'WWV_15_MHz', 'WWV_20_MHz', 'WWV_25_MHz',
        'CHU_3.33_MHz', 'CHU_7.85_MHz', 'CHU_14.67_MHz'
    ]
    
    # Check each date directory
    for date_dir in spectrograms_dir.glob('*'):
        if not date_dir.is_dir():
            continue
        
        date_str = date_dir.name
        if not (date_str.isdigit() and len(date_str) == 8):
            continue
        
        # Count spectrograms for this date (now using 'decimated' type)
        spectrograms = list(date_dir.glob('*_decimated_spectrogram.png'))
        
        # Consider complete if we have most channels (some might not have data)
        if len(spectrograms) >= 6:  # At least 6 out of 9 channels
            dates.add(date_str)
            logger.debug(f"Date {date_str}: {len(spectrograms)}/9 spectrograms")
    
    return dates


def is_day_complete(date_str: str) -> bool:
    """Check if a day is complete (past midnight UTC)"""
    try:
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
        day_end = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
        
        # Day is complete if we're past midnight UTC
        return datetime.now(timezone.utc) > day_end
    except:
        return False


def is_current_day(date_str: str) -> bool:
    """Check if a date is the current UTC day"""
    try:
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        return date_str == today
    except:
        return False


def generate_spectrograms(date_str: str, data_root: Path) -> bool:
    """Generate spectrograms for a specific date from 10 Hz decimated NPZ files"""
    script_dir = Path(__file__).parent
    script = script_dir / 'generate_spectrograms_from_10hz.py'
    
    logger.info(f"📊 Generating spectrograms for {date_str}...")
    
    try:
        result = subprocess.run(
            ['python3', str(script), '--date', date_str, '--data-root', str(data_root)],
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            logger.info(f"✅ Successfully generated spectrograms for {date_str}")
            return True
        else:
            logger.error(f"❌ Failed to generate spectrograms for {date_str}")
            logger.error(f"   Exit code: {result.returncode}")
            if result.stderr:
                logger.error(f"   Error: {result.stderr[:500]}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"⏱️  Timeout generating spectrograms for {date_str}")
        return False
    except Exception as e:
        logger.error(f"❌ Exception generating spectrograms for {date_str}: {e}")
        return False


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Check for missing spectrograms and generate them'
    )
    parser.add_argument(
        '--data-root',
        default='/tmp/timestd-test',
        help='Data root directory'
    )
    parser.add_argument(
        '--max-age-days',
        type=int,
        default=7,
        help='Only check dates within this many days (default: 7)'
    )
    parser.add_argument(
        '--skip-incomplete',
        action='store_true',
        help='Skip days that are not yet complete (before midnight UTC)'
    )
    
    args = parser.parse_args()
    data_root = Path(args.data_root)
    
    logger.info("🔍 Checking for missing spectrograms...")
    logger.info(f"   Data root: {data_root}")
    logger.info(f"   Max age: {args.max_age_days} days")
    
    # Find all dates with archives
    archive_dates = find_days_with_archives(data_root)
    logger.info(f"   Found archives for {len(archive_dates)} dates")
    
    # Find dates with spectrograms
    spectrogram_dates = find_days_with_spectrograms(data_root)
    logger.info(f"   Found spectrograms for {len(spectrogram_dates)} dates")
    
    # Find missing spectrograms
    missing_dates = archive_dates - spectrogram_dates
    
    # Always include current day for refresh (spectrograms need updating as data arrives)
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    if today in archive_dates:
        if today in spectrogram_dates:
            logger.info(f"🔄 Adding current day {today} for spectrogram refresh")
        missing_dates.add(today)
    
    if not missing_dates:
        logger.info("✅ All dates have spectrograms!")
        return 0
    
    # Filter by age
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age_days)
    recent_missing = []
    
    for date_str in sorted(missing_dates):
        try:
            year = int(date_str[:4])
            month = int(date_str[4:6])
            day = int(date_str[6:8])
            date_dt = datetime(year, month, day, tzinfo=timezone.utc)
            
            if date_dt >= cutoff:
                # Check if day is complete (but always process current day for refresh)
                if args.skip_incomplete and not is_day_complete(date_str) and not is_current_day(date_str):
                    logger.info(f"⏭️  Skipping {date_str} (day not complete)")
                    continue
                
                recent_missing.append(date_str)
        except:
            continue
    
    if not recent_missing:
        logger.info(f"✅ No missing spectrograms within last {args.max_age_days} days")
        return 0
    
    logger.info(f"📋 Missing spectrograms for {len(recent_missing)} dates: {', '.join(recent_missing)}")
    
    # Generate missing spectrograms
    success_count = 0
    for date_str in recent_missing:
        if generate_spectrograms(date_str, data_root):
            success_count += 1
    
    logger.info(f"\n{'='*60}")
    logger.info(f"✅ Generated spectrograms for {success_count}/{len(recent_missing)} dates")
    logger.info(f"{'='*60}")
    
    return 0 if success_count == len(recent_missing) else 1


if __name__ == '__main__':
    sys.exit(main())
