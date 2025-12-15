#!/usr/bin/env python3
"""
Parallel reprocessing of discrimination data - uses all CPU cores

This is a parallelized version of reprocess_discrimination.py that processes
multiple NPZ files simultaneously using multiprocessing.

Usage:
    # Reprocess specific date (use all cores)
    python3 scripts/reprocess_discrimination_parallel.py --date 20251119 --channel "WWV 10 MHz"
    
    # Specify number of worker processes
    python3 scripts/reprocess_discrimination_parallel.py --date 20251119 --channel "WWV 10 MHz" --workers 8
"""

import argparse
import logging
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional
from multiprocessing import Pool, cpu_count
from functools import partial

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.analytics_service import NPZArchive
from grape_recorder.grape.tone_detector import MultiStationToneDetector
from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator, DiscriminationResult

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def process_single_npz(npz_file: Path, channel_name: str) -> Optional[Tuple[Path, DiscriminationResult]]:
    """
    Process a single NPZ file (runs in worker process)
    
    Returns:
        Tuple of (npz_file, result) or None if processing failed
    """
    try:
        # Create detector and discriminator in worker process
        tone_detector = MultiStationToneDetector(channel_name=channel_name)
        discriminator = WWVHDiscriminator(channel_name=channel_name)
        
        # Load archive
        archive = NPZArchive.load(npz_file)
        minute_timestamp = archive.unix_timestamp
        
        # Run tone detection
        detections = tone_detector.process_samples(
            timestamp=minute_timestamp,
            samples=archive.iq_samples,
            rtp_timestamp=archive.rtp_timestamp
        )
        
        if detections is None:
            detections = []
        
        # Run discrimination analysis (includes 440 Hz, tick, and BCD detection)
        result = discriminator.analyze_minute_with_440hz(
            iq_samples=archive.iq_samples,
            sample_rate=archive.sample_rate,
            minute_timestamp=minute_timestamp,
            detections=detections
        )
        
        if result is None:
            return None
        
        return (npz_file, result)
        
    except Exception as e:
        logger.error(f"Error processing {npz_file.name}: {e}")
        return None


def find_npz_files(archive_dir: Path, start_date: str, end_date: str) -> List[Path]:
    """Find all NPZ files in date range"""
    start_dt = datetime.strptime(start_date, '%Y%m%d')
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    
    npz_files = []
    current_dt = start_dt
    
    while current_dt <= end_dt:
        date_str = current_dt.strftime('%Y%m%d')
        pattern = f'{date_str}T*.npz'
        day_files = sorted(archive_dir.glob(pattern))
        npz_files.extend(day_files)
        current_dt += timedelta(days=1)
    
    return npz_files


def write_results_to_csv(
    results: List[Tuple[Path, DiscriminationResult]],
    discrimination_dir: Path,
    channel_name: str
) -> int:
    """
    Write all results to CSV files (one per date)
    
    Returns:
        Number of results successfully written
    """
    # Group results by date
    results_by_date = {}
    
    for npz_file, result in results:
        from datetime import timezone as tz
        dt = datetime.fromtimestamp(result.minute_timestamp, tz.utc)
        date_str = dt.strftime('%Y%m%d')
        
        if date_str not in results_by_date:
            results_by_date[date_str] = []
        
        results_by_date[date_str].append(result)
    
    # Write each date's CSV
    successful = 0
    
    for date_str, date_results in results_by_date.items():
        # Sort by timestamp
        date_results.sort(key=lambda r: r.minute_timestamp)
        
        csv_file = discrimination_dir / f'{channel_name.replace(" ", "_")}_discrimination_{date_str}.csv'
        
        try:
            with open(csv_file, 'w') as f:
                # Write header
                f.write('timestamp_utc,minute_timestamp,minute_number,'
                       'wwv_detected,wwvh_detected,'
                       'wwv_power_db,wwvh_power_db,power_ratio_db,'
                       'differential_delay_ms,'
                       'tone_440hz_wwv_detected,tone_440hz_wwv_power_db,'
                       'tone_440hz_wwvh_detected,tone_440hz_wwvh_power_db,'
                       'dominant_station,confidence,tick_windows_10sec,'
                       'bcd_wwv_amplitude,bcd_wwvh_amplitude,bcd_differential_delay_ms,bcd_correlation_quality,bcd_windows\n')
                
                # Write data rows
                for result in date_results:
                    from datetime import timezone as tz
                    dt = datetime.fromtimestamp(result.minute_timestamp, tz.utc)
                    minute_number = dt.minute
                    
                    # Format optional float fields
                    wwv_power_str = f'{result.wwv_power_db:.2f}' if result.wwv_power_db is not None else ''
                    wwvh_power_str = f'{result.wwvh_power_db:.2f}' if result.wwvh_power_db is not None else ''
                    power_ratio_str = f'{result.power_ratio_db:.2f}' if result.power_ratio_db is not None else ''
                    delay_str = f'{result.differential_delay_ms:.2f}' if result.differential_delay_ms is not None else ''
                    tone_440_wwv_power_str = f'{result.tone_440hz_wwv_power_db:.2f}' if result.tone_440hz_wwv_power_db is not None else ''
                    tone_440_wwvh_power_str = f'{result.tone_440hz_wwvh_power_db:.2f}' if result.tone_440hz_wwvh_power_db is not None else ''
                    
                    # Serialize tick windows
                    tick_windows_str = ''
                    if result.tick_windows_10sec:
                        tick_windows_str = json.dumps(result.tick_windows_10sec).replace('"', '""')
                    
                    # Format BCD fields
                    bcd_wwv_str = f'{result.bcd_wwv_amplitude:.2f}' if result.bcd_wwv_amplitude is not None else ''
                    bcd_wwvh_str = f'{result.bcd_wwvh_amplitude:.2f}' if result.bcd_wwvh_amplitude is not None else ''
                    bcd_delay_str = f'{result.bcd_differential_delay_ms:.2f}' if result.bcd_differential_delay_ms is not None else ''
                    bcd_quality_str = f'{result.bcd_correlation_quality:.2f}' if result.bcd_correlation_quality is not None else ''
                    
                    # Serialize BCD windows
                    bcd_windows_str = ''
                    if result.bcd_windows:
                        bcd_windows_str = json.dumps(result.bcd_windows).replace('"', '""')
                    
                    f.write(f'{dt.isoformat()},{result.minute_timestamp},{minute_number},'
                           f'{int(result.wwv_detected)},{int(result.wwvh_detected)},'
                           f'{wwv_power_str},{wwvh_power_str},{power_ratio_str},{delay_str},'
                           f'{int(result.tone_440hz_wwv_detected)},{tone_440_wwv_power_str},'
                           f'{int(result.tone_440hz_wwvh_detected)},{tone_440_wwvh_power_str},'
                           f'{result.dominant_station if result.dominant_station else ""},'
                           f'{result.confidence if result.confidence else "low"},"{tick_windows_str}",'
                           f'{bcd_wwv_str},{bcd_wwvh_str},{bcd_delay_str},{bcd_quality_str},"{bcd_windows_str}"\n')
                    
                    successful += 1
            
            logger.info(f"✅ Wrote {len(date_results)} results to {csv_file.name}")
            
        except Exception as e:
            logger.error(f"Failed to write CSV for {date_str}: {e}")
    
    return successful


def reprocess_discrimination_parallel(
    channel_name: str,
    npz_files: List[Path],
    discrimination_dir: Path,
    num_workers: int = None
) -> int:
    """
    Reprocess discrimination with parallel workers
    
    Args:
        channel_name: Channel name
        npz_files: List of NPZ files to process
        discrimination_dir: Output directory
        num_workers: Number of worker processes (default: CPU count)
    
    Returns:
        Number of files successfully processed
    """
    if num_workers is None:
        num_workers = cpu_count()
    
    logger.info(f"🚀 Starting parallel processing with {num_workers} workers")
    logger.info(f"📦 Total files to process: {len(npz_files)}")
    
    # Create partial function with channel_name bound
    process_func = partial(process_single_npz, channel_name=channel_name)
    
    # Process in parallel with progress tracking
    results = []
    
    with Pool(processes=num_workers) as pool:
        # Use imap_unordered for better progress tracking
        for i, result in enumerate(pool.imap_unordered(process_func, npz_files), 1):
            if result is not None:
                results.append(result)
            
            # Log progress every 10 files
            if i % 10 == 0 or i == len(npz_files):
                logger.info(f"Progress: {i}/{len(npz_files)} files processed ({len(results)} successful)")
    
    logger.info(f"✅ Processing complete: {len(results)}/{len(npz_files)} successful")
    
    # Write all results to CSV
    logger.info("📝 Writing results to CSV files...")
    successful = write_results_to_csv(results, discrimination_dir, channel_name)
    
    return successful


def main():
    parser = argparse.ArgumentParser(description='Parallel reprocessing of discrimination data')
    parser.add_argument('--channel', type=str, required=True,
                       help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--date', type=str,
                       help='Single date to process (YYYYMMDD)')
    parser.add_argument('--start-date', type=str,
                       help='Start date (YYYYMMDD)')
    parser.add_argument('--end-date', type=str,
                       help='End date (YYYYMMDD)')
    parser.add_argument('--all', action='store_true',
                       help='Process all available data')
    parser.add_argument('--data-root', type=str, default='/tmp/timestd-test',
                       help='Root data directory')
    parser.add_argument('--workers', type=int,
                       help=f'Number of worker processes (default: {cpu_count()})')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not (args.date or (args.start_date and args.end_date) or args.all):
        logger.error("Must specify --date, --start-date/--end-date, or --all")
        return 1
    
    # Setup paths
    from grape_recorder.paths import GRAPEPaths
    paths = GRAPEPaths(args.data_root)
    archive_dir = paths.get_archive_dir(args.channel)
    discrimination_dir = paths.get_discrimination_dir(args.channel)
    
    if not archive_dir.exists():
        logger.error(f"Archive directory not found: {archive_dir}")
        return 1
    
    discrimination_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine date range
    if args.date:
        start_date = args.date
        end_date = args.date
    elif args.all:
        # Find earliest and latest files
        all_npz = sorted(archive_dir.glob('*.npz'))
        if not all_npz:
            logger.error(f"No NPZ files found in {archive_dir}")
            return 1
        start_date = all_npz[0].name[:8]
        end_date = all_npz[-1].name[:8]
    else:
        start_date = args.start_date
        end_date = args.end_date
    
    # Find NPZ files
    logger.info("=" * 60)
    logger.info("PARALLEL DISCRIMINATION REPROCESSING")
    logger.info("=" * 60)
    logger.info(f"Channel: {args.channel}")
    logger.info(f"Date range: {start_date} to {end_date}")
    logger.info(f"Archive directory: {archive_dir}")
    logger.info(f"Output directory: {discrimination_dir}")
    logger.info(f"Workers: {args.workers or cpu_count()}")
    logger.info("")
    
    npz_files = find_npz_files(archive_dir, start_date, end_date)
    
    if not npz_files:
        logger.error(f"No NPZ files found for date range {start_date} to {end_date}")
        return 1
    
    # Process files in parallel
    successful = reprocess_discrimination_parallel(
        channel_name=args.channel,
        npz_files=npz_files,
        discrimination_dir=discrimination_dir,
        num_workers=args.workers
    )
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ Reprocessing complete: {successful} minutes processed")
    logger.info("=" * 60)
    logger.info("")
    logger.info("View results:")
    logger.info(f"  CSV files: {discrimination_dir}")
    logger.info(f"  Web UI: http://localhost:3000/discrimination.html")
    logger.info("")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
