#!/usr/bin/env python3
"""
Gap detection and backfill for analytics service.

Compares NPZ archive coverage vs discrimination CSV coverage to identify
and reprocess gaps in the analytical output data.
"""

import csv
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Tuple, Set
import logging

logger = logging.getLogger(__name__)


def extract_timestamp_from_npz_filename(filename: str) -> datetime:
    """
    Extract timestamp from NPZ filename.
    
    Format: YYYYMMDDTHHMMSSZ_freq_iq.npz
    Example: 20251116T153000Z_10000000_iq.npz
    
    Returns:
        datetime object in UTC
    """
    # Extract timestamp part: YYYYMMDDTHHMMSSZ
    timestamp_str = filename.split('_')[0]
    
    # Parse: 20251116T153000Z
    dt = datetime.strptime(timestamp_str, '%Y%m%dT%H%M%SZ')
    return dt.replace(tzinfo=timezone.utc)


def get_npz_minute_timestamps(archive_dir: Path) -> Set[datetime]:
    """
    Get set of all minute timestamps covered by NPZ archives.
    
    Args:
        archive_dir: Directory containing NPZ files
        
    Returns:
        Set of datetime objects (minute precision, UTC)
    """
    timestamps = set()
    
    for npz_file in archive_dir.glob('*_iq.npz'):
        try:
            dt = extract_timestamp_from_npz_filename(npz_file.name)
            # Round to minute precision
            minute_timestamp = dt.replace(second=0, microsecond=0)
            timestamps.add(minute_timestamp)
        except Exception as e:
            logger.warning(f"Failed to parse timestamp from {npz_file.name}: {e}")
            continue
    
    return timestamps


def get_csv_minute_timestamps(csv_file: Path) -> Set[datetime]:
    """
    Get set of all minute timestamps in discrimination CSV.
    
    Args:
        csv_file: Path to discrimination CSV
        
    Returns:
        Set of datetime objects (minute precision, UTC)
    """
    timestamps = set()
    
    if not csv_file.exists():
        return timestamps
    
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Parse timestamp_utc column: 2025-11-16T15:30:00+00:00
                    dt = datetime.fromisoformat(row['timestamp_utc'].replace('Z', '+00:00'))
                    # Round to minute precision
                    minute_timestamp = dt.replace(second=0, microsecond=0)
                    timestamps.add(minute_timestamp)
                except Exception as e:
                    continue
    except Exception as e:
        logger.error(f"Failed to read CSV {csv_file}: {e}")
    
    return timestamps


def find_gaps(archive_dir: Path, discrimination_csv: Path) -> List[Tuple[datetime, Path]]:
    """
    Find gaps: NPZ files that exist but have no discrimination CSV entry.
    
    Args:
        archive_dir: Directory containing NPZ archives
        discrimination_csv: Path to discrimination CSV file
        
    Returns:
        List of (timestamp, npz_file_path) tuples for missing data,
        sorted chronologically
    """
    logger.info("Analyzing coverage gaps...")
    
    # Get coverage from both sources
    npz_timestamps = get_npz_minute_timestamps(archive_dir)
    csv_timestamps = get_csv_minute_timestamps(discrimination_csv)
    
    logger.info(f"  NPZ archives: {len(npz_timestamps)} unique minutes")
    logger.info(f"  CSV entries:  {len(csv_timestamps)} unique minutes")
    
    # Find gaps
    gap_timestamps = npz_timestamps - csv_timestamps
    
    if not gap_timestamps:
        logger.info("  ✅ No gaps found - CSV coverage matches NPZ archives")
        return []
    
    logger.info(f"  ⚠️  Found {len(gap_timestamps)} minutes with missing discrimination data")
    
    # Map gaps back to NPZ files
    gaps = []
    for gap_time in sorted(gap_timestamps):
        # Find NPZ file for this timestamp
        # Format: YYYYMMDDTHHMMSSZ
        timestamp_pattern = gap_time.strftime('%Y%m%dT%H%M')
        
        matching_files = list(archive_dir.glob(f'{timestamp_pattern}*_iq.npz'))
        if matching_files:
            # Should be exactly one, but take first if multiple
            gaps.append((gap_time, matching_files[0]))
    
    return gaps


def format_gap_summary(gaps: List[Tuple[datetime, Path]]) -> str:
    """
    Format gap summary for logging/display.
    
    Args:
        gaps: List of (timestamp, file_path) tuples
        
    Returns:
        Formatted string
    """
    if not gaps:
        return "No gaps detected"
    
    # Group by hour for summary
    hourly = {}
    for gap_time, _ in gaps:
        hour_key = gap_time.strftime('%Y-%m-%d %H:00')
        hourly[hour_key] = hourly.get(hour_key, 0) + 1
    
    summary = [f"Gap Summary ({len(gaps)} minutes total):"]
    for hour, count in sorted(hourly.items()):
        summary.append(f"  {hour} UTC: {count} minutes")
    
    return '\n'.join(summary)


def backfill_gaps(gaps: List[Tuple[datetime, Path]], 
                  process_func,
                  max_backfill: int = None) -> int:
    """
    Backfill gaps by reprocessing NPZ files.
    
    Args:
        gaps: List of (timestamp, file_path) tuples
        process_func: Function to call for each NPZ file
        max_backfill: Maximum number of files to backfill (None = unlimited)
        
    Returns:
        Number of files successfully backfilled
    """
    if not gaps:
        return 0
    
    backfill_count = len(gaps) if max_backfill is None else min(len(gaps), max_backfill)
    
    logger.info(f"Starting backfill of {backfill_count} gaps...")
    
    successful = 0
    failed = 0
    
    for i, (gap_time, npz_file) in enumerate(gaps[:backfill_count]):
        try:
            logger.info(f"  [{i+1}/{backfill_count}] Backfilling {gap_time.strftime('%Y-%m-%d %H:%M')} UTC")
            process_func(npz_file)
            successful += 1
        except Exception as e:
            logger.error(f"  Failed to backfill {npz_file.name}: {e}")
            failed += 1
    
    logger.info(f"Backfill complete: {successful} successful, {failed} failed")
    
    return successful


if __name__ == '__main__':
    # Quick test/diagnostic tool
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='Detect gaps in discrimination data')
    parser.add_argument('--archive-dir', required=True, type=Path,
                        help='NPZ archive directory')
    parser.add_argument('--discrimination-csv', required=True, type=Path,
                        help='Discrimination CSV file')
    
    args = parser.parse_args()
    
    logging.basicConfig(level=logging.INFO, 
                       format='%(levelname)s: %(message)s')
    
    gaps = find_gaps(args.archive_dir, args.discrimination_csv)
    
    print()
    print(format_gap_summary(gaps))
    print()
    
    if gaps:
        print("Gap details:")
        for gap_time, npz_file in gaps[:20]:  # Show first 20
            print(f"  {gap_time.strftime('%Y-%m-%d %H:%M')} UTC → {npz_file.name}")
        
        if len(gaps) > 20:
            print(f"  ... and {len(gaps) - 20} more")
        
        sys.exit(1)  # Non-zero exit if gaps found
    else:
        sys.exit(0)
