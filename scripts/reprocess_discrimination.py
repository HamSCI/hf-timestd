#!/usr/bin/env python3
"""
Reprocess discrimination data with coherent integration

Reprocesses existing NPZ archive files to regenerate discrimination CSV
with the new coherent integration tick detection.

This is useful after:
- Algorithm updates (like adding coherent integration)
- Bug fixes in discrimination logic
- Changes to coherence quality metrics

Usage:
    # Reprocess specific date
    python3 scripts/reprocess_discrimination.py --date 20251127 --channel "WWV 10 MHz"
    
    # Reprocess specific hours (fast testing - ~5 min for 2 hours)
    python3 scripts/reprocess_discrimination.py --date 20251127 --channel "WWV 10 MHz" --start-hour 9 --end-hour 11
    
    # Reprocess date range
    python3 scripts/reprocess_discrimination.py --start-date 20251118 --end-date 20251119 --channel "WWV 10 MHz"
    
    # Reprocess all available data
    python3 scripts/reprocess_discrimination.py --all --channel "WWV 10 MHz"
    
    # Keep existing data and append (useful for filling gaps)
    python3 scripts/reprocess_discrimination.py --date 20251127 --channel "WWV 10 MHz" --start-hour 6 --end-hour 12 --keep-existing
"""

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from grape_recorder.grape.analytics_service import NPZArchive
from grape_recorder.grape.tone_detector import MultiStationToneDetector
from grape_recorder.grape.wwvh_discrimination import WWVHDiscriminator

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def find_npz_files(archive_dir: Path, start_date: str, end_date: str,
                   start_hour: int = 0, end_hour: int = 23) -> List[Path]:
    """
    Find all NPZ files in date range and optional hour range
    
    Args:
        archive_dir: Path to archives/{channel}
        start_date: YYYYMMDD format
        end_date: YYYYMMDD format
        start_hour: Start hour (0-23), default 0
        end_hour: End hour (0-23), default 23
        
    Returns:
        List of NPZ file paths sorted by timestamp
    """
    npz_files = []
    
    # Convert dates
    start_dt = datetime.strptime(start_date, '%Y%m%d')
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    
    # Iterate through date range
    current_dt = start_dt
    while current_dt <= end_dt:
        date_str = current_dt.strftime('%Y%m%d')
        # Pattern: YYYYMMDDTHHMMSSZ_frequency_iq.npz
        date_pattern = f'{date_str}T*_iq.npz'
        
        # Find files for this date
        matching_files = list(archive_dir.glob(date_pattern))
        
        # Filter by hour if specified
        if start_hour > 0 or end_hour < 23:
            filtered_files = []
            for f in matching_files:
                # Extract hour from filename: YYYYMMDDTHHMMSSZ_...
                filename = f.stem
                if 'T' in filename:
                    time_part = filename.split('T')[1][:2]  # HH
                    try:
                        hour = int(time_part)
                        if start_hour <= hour <= end_hour:
                            filtered_files.append(f)
                    except ValueError:
                        pass
            matching_files = filtered_files
        
        npz_files.extend(matching_files)
        
        hour_info = f" (hours {start_hour:02d}-{end_hour:02d})" if (start_hour > 0 or end_hour < 23) else ""
        logger.info(f"Date {date_str}: Found {len(matching_files)} NPZ files{hour_info}")
        current_dt += timedelta(days=1)
    
    # Sort by filename (which includes timestamp)
    npz_files.sort()
    
    return npz_files


def reprocess_discrimination(
    channel_name: str,
    npz_files: List[Path],
    discrimination_dir: Path,
    clear_existing: bool = True
) -> int:
    """
    Reprocess NPZ files to generate new discrimination data
    
    Args:
        channel_name: Channel name (e.g., "WWV 10 MHz")
        npz_files: List of NPZ archive files to process
        discrimination_dir: Output directory for CSV files
        clear_existing: If True, delete existing CSV files first
        
    Returns:
        Number of files successfully processed
    """
    # Initialize detectors
    tone_detector = MultiStationToneDetector(channel_name=channel_name)
    
    # Check if this channel has WWV/WWVH discrimination
    # Handle both space and underscore formats: "WWV 10 MHz", "WWV_10_MHz"
    import re
    wwvh_frequencies = [2.5, 5.0, 10.0, 15.0]
    channel_freq = None
    
    # Extract frequency from channel name using regex
    freq_match = re.search(r'(\d+(?:\.\d+)?)[_\s]*MHz', channel_name, re.IGNORECASE)
    if freq_match:
        extracted_freq = float(freq_match.group(1))
        if extracted_freq in wwvh_frequencies:
            channel_freq = extracted_freq
    
    if channel_freq is None:
        logger.error(f"Channel {channel_name} does not support WWV/WWVH discrimination "
                    f"(extracted freq: {freq_match.group(1) if freq_match else 'None'})")
        return 0
    
    discriminator = WWVHDiscriminator(channel_name=channel_name)
    logger.info(f"✅ Initialized WWV/WWVH discriminator for {channel_name}")
    
    # Track which dates we're processing
    dates_to_process = set()
    for npz_file in npz_files:
        # Extract date from filename: YYYYMMDDTHHMMSSZ_frequency_iq.npz
        filename = npz_file.stem
        if 'T' in filename:
            date_part = filename.split('T')[0]  # YYYYMMDD
            dates_to_process.add(date_part)
    
    # Clear existing CSV files if requested
    if clear_existing:
        for date_str in dates_to_process:
            csv_file = discrimination_dir / f'{channel_name.replace(" ", "_")}_discrimination_{date_str}.csv'
            if csv_file.exists():
                logger.info(f"🗑️  Deleting existing: {csv_file.name}")
                csv_file.unlink()
    
    # Process each NPZ file
    successful = 0
    failed = 0
    total = len(npz_files)
    
    # Progress reporting every 10%
    report_interval = max(1, total // 10)
    
    for i, npz_file in enumerate(npz_files, 1):
        try:
            # Progress update
            if i == 1 or i % report_interval == 0 or i == total:
                print(f"[{i}/{total}] Processing {npz_file.name}...", flush=True)
            
            # Load archive
            archive = NPZArchive.load(npz_file)
            
            # Get minute timestamp
            minute_timestamp = archive.unix_timestamp
            
            # Run tone detection
            detections = tone_detector.process_samples(
                timestamp=minute_timestamp,
                samples=archive.iq_samples,
                rtp_timestamp=archive.rtp_timestamp
            )
            
            # Handle None detections (convert to empty list for discrimination)
            if detections is None:
                detections = []
            
            # Run discrimination analysis (includes 440 Hz and tick detection)
            result = discriminator.analyze_minute_with_440hz(
                iq_samples=archive.iq_samples,
                sample_rate=archive.sample_rate,
                minute_timestamp=minute_timestamp,
                detections=detections
            )
            
            if result is None:
                logger.warning(f"  No discrimination result for {npz_file.name}")
                failed += 1
                continue
            
            # Write to CSV
            try:
                from datetime import timezone as tz
                dt = datetime.fromtimestamp(result.minute_timestamp, tz.utc)
                date_str = dt.strftime('%Y%m%d')
                
                csv_file = discrimination_dir / f'{channel_name.replace(" ", "_")}_discrimination_{date_str}.csv'
                
                # Write header if new file
                write_header = not csv_file.exists()
                
                with open(csv_file, 'a') as f:
                    if write_header:
                        f.write('timestamp_utc,minute_timestamp,minute_number,'
                               'wwv_detected,wwvh_detected,'
                               'wwv_power_db,wwvh_power_db,power_ratio_db,'
                               'differential_delay_ms,'
                               'tone_440hz_wwv_detected,tone_440hz_wwv_power_db,'
                               'tone_440hz_wwvh_detected,tone_440hz_wwvh_power_db,'
                               'dominant_station,confidence,tick_windows_10sec,'
                               'bcd_wwv_amplitude,bcd_wwvh_amplitude,bcd_differential_delay_ms,bcd_correlation_quality,bcd_windows,'
                               'tone_500_600_detected,tone_500_600_power_db,tone_500_600_freq_hz,tone_500_600_ground_truth_station,'
                               'harmonic_ratio_500_1000,harmonic_ratio_600_1200,'
                               'bcd_minute_validated,bcd_correlation_peak_quality,'
                               'inter_method_agreements,inter_method_disagreements\n')
                        write_header = False
                    # Format data
                    minute_number = dt.minute
                    
                    # Format optional float fields with safety checks
                    wwv_power_str = f'{result.wwv_power_db:.2f}' if result.wwv_power_db is not None else ''
                    wwvh_power_str = f'{result.wwvh_power_db:.2f}' if result.wwvh_power_db is not None else ''
                    power_ratio_str = f'{result.power_ratio_db:.2f}' if result.power_ratio_db is not None else ''
                    diff_delay_str = f'{result.differential_delay_ms:.2f}' if result.differential_delay_ms is not None else ''
                    tone_440_wwv_power_str = f'{result.tone_440hz_wwv_power_db:.2f}' if result.tone_440hz_wwv_power_db is not None else ''
                    tone_440_wwvh_power_str = f'{result.tone_440hz_wwvh_power_db:.2f}' if result.tone_440hz_wwvh_power_db is not None else ''
                    
                    # Serialize tick windows to JSON
                    import json
                    tick_windows_str = ''
                    if result.tick_windows_10sec:
                        tick_windows_str = json.dumps(result.tick_windows_10sec).replace('"', '""')  # Escape quotes for CSV
                    
                    # Format BCD fields with safety checks
                    bcd_wwv_str = f'{result.bcd_wwv_amplitude:.2f}' if result.bcd_wwv_amplitude is not None else ''
                    bcd_wwvh_str = f'{result.bcd_wwvh_amplitude:.2f}' if result.bcd_wwvh_amplitude is not None else ''
                    bcd_delay_str = f'{result.bcd_differential_delay_ms:.2f}' if result.bcd_differential_delay_ms is not None else ''
                    bcd_quality_str = f'{result.bcd_correlation_quality:.2f}' if result.bcd_correlation_quality is not None else ''
                    
                    # Serialize BCD windows to JSON
                    bcd_windows_str = ''
                    if result.bcd_windows:
                        bcd_windows_str = json.dumps(result.bcd_windows).replace('"', '""')  # Escape quotes for CSV
                    
                    # Format new 500/600 Hz tone fields
                    tone_500_600_power_str = f'{result.tone_500_600_power_db:.2f}' if result.tone_500_600_power_db is not None else ''
                    tone_500_600_freq_str = str(result.tone_500_600_freq_hz) if result.tone_500_600_freq_hz is not None else ''
                    tone_500_600_gt_str = result.tone_500_600_ground_truth_station if result.tone_500_600_ground_truth_station else ''
                    
                    # Format harmonic ratio fields
                    harmonic_500_1000_str = f'{result.harmonic_ratio_500_1000:.2f}' if result.harmonic_ratio_500_1000 is not None else ''
                    harmonic_600_1200_str = f'{result.harmonic_ratio_600_1200:.2f}' if result.harmonic_ratio_600_1200 is not None else ''
                    
                    # Format BCD validation fields
                    bcd_peak_quality_str = f'{result.bcd_correlation_peak_quality:.2f}' if result.bcd_correlation_peak_quality is not None else ''
                    
                    # Serialize inter-method validation lists
                    agreements_str = ''
                    if result.inter_method_agreements:
                        agreements_str = json.dumps(result.inter_method_agreements).replace('"', '""')
                    disagreements_str = ''
                    if result.inter_method_disagreements:
                        disagreements_str = json.dumps(result.inter_method_disagreements).replace('"', '""')
                    
                    f.write(f'{dt.isoformat()},{result.minute_timestamp},{minute_number},'
                           f'{int(result.wwv_detected)},{int(result.wwvh_detected)},'
                           f'{wwv_power_str},{wwvh_power_str},{power_ratio_str},{diff_delay_str},'
                           f'{int(result.tone_440hz_wwv_detected)},{tone_440_wwv_power_str},'
                           f'{int(result.tone_440hz_wwvh_detected)},{tone_440_wwvh_power_str},'
                           f'{result.dominant_station if result.dominant_station else ""},'
                           f'{result.confidence if result.confidence else "low"},"{tick_windows_str}",'
                           f'{bcd_wwv_str},{bcd_wwvh_str},{bcd_delay_str},{bcd_quality_str},"{bcd_windows_str}",'
                           f'{int(result.tone_500_600_detected)},{tone_500_600_power_str},{tone_500_600_freq_str},{tone_500_600_gt_str},'
                           f'{harmonic_500_1000_str},{harmonic_600_1200_str},'
                           f'{int(result.bcd_minute_validated)},{bcd_peak_quality_str},'
                           f'"{agreements_str}","{disagreements_str}"\n')
            except Exception as e:
                logger.error(f"  Failed to write CSV for {npz_file.name}: {e}")
                failed += 1
                continue
            
            successful += 1
            
            # Log progress every 10 files
            if i % 10 == 0:
                logger.info(f"  Progress: {successful} successful, {failed} failed")
                
        except Exception as e:
            import traceback
            logger.error(f"  Failed to process {npz_file.name}: {e}")
            logger.error(f"  Traceback:\n{traceback.format_exc()}")
            failed += 1
    
    return successful


def main():
    parser = argparse.ArgumentParser(
        description='Reprocess discrimination data with coherent integration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument('--data-root', type=str, default='/tmp/timestd-test',
                       help='Data root directory (default: /tmp/timestd-test)')
    parser.add_argument('--channel', type=str, required=True,
                       help='Channel name (e.g., "WWV 10 MHz")')
    parser.add_argument('--date', type=str,
                       help='Single date to process (YYYYMMDD format)')
    parser.add_argument('--start-date', type=str,
                       help='Start date for range (YYYYMMDD format)')
    parser.add_argument('--end-date', type=str,
                       help='End date for range (YYYYMMDD format)')
    parser.add_argument('--all', action='store_true',
                       help='Process all available data')
    parser.add_argument('--keep-existing', action='store_true',
                       help='Keep existing CSV data (append mode)')
    parser.add_argument('--start-hour', type=int, default=0,
                       help='Start hour UTC (0-23, default: 0)')
    parser.add_argument('--end-hour', type=int, default=23,
                       help='End hour UTC (0-23, default: 23)')
    parser.add_argument('--limit', type=int, default=0,
                       help='Limit number of files to process (0=all)')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not (args.date or (args.start_date and args.end_date) or args.all):
        parser.error("Must specify --date, --start-date/--end-date, or --all")
    
    # Setup paths using GRAPEPaths API
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
        start_date = end_date = args.date
    elif args.all:
        # Find earliest and latest NPZ files
        all_npz = list(archive_dir.glob('*_iq.npz'))
        if not all_npz:
            logger.error(f"No NPZ files found in {archive_dir}")
            return 1
        
        # Extract dates from filenames: YYYYMMDDTHHMMSSZ_frequency_iq.npz
        dates = []
        for f in all_npz:
            filename = f.stem
            if 'T' in filename:
                date_part = filename.split('T')[0]  # YYYYMMDD
                dates.append(date_part)
        
        if not dates:
            logger.error("Could not extract dates from NPZ filenames")
            return 1
        
        start_date = min(dates)
        end_date = max(dates)
        logger.info(f"Found data from {start_date} to {end_date}")
    else:
        start_date = args.start_date
        end_date = args.end_date
    
    logger.info("=" * 60)
    logger.info("Reprocessing Discrimination Data with Coherent Integration")
    logger.info("=" * 60)
    logger.info(f"Channel: {args.channel}")
    hour_range = ""
    if args.start_hour > 0 or args.end_hour < 23:
        hour_range = f" (hours {args.start_hour:02d}:00-{args.end_hour:02d}:59 UTC)"
    logger.info(f"Date range: {start_date} to {end_date}{hour_range}")
    logger.info(f"Archive directory: {archive_dir}")
    logger.info(f"Output directory: {discrimination_dir}")
    logger.info(f"Clear existing: {not args.keep_existing}")
    logger.info("")
    
    # Find NPZ files
    logger.info("🔍 Searching for NPZ files...")
    npz_files = find_npz_files(archive_dir, start_date, end_date,
                                args.start_hour, args.end_hour)
    
    if not npz_files:
        logger.error("No NPZ files found in date range")
        return 1
    
    logger.info(f"✅ Found {len(npz_files)} NPZ files to process")
    
    # Apply limit if specified
    if args.limit > 0:
        npz_files = npz_files[:args.limit]
        logger.info(f"⚠️  Limited to first {args.limit} files")
    
    logger.info("")
    
    # Reprocess
    logger.info("🔄 Starting reprocessing...")
    successful = reprocess_discrimination(
        channel_name=args.channel,
        npz_files=npz_files,
        discrimination_dir=discrimination_dir,
        clear_existing=not args.keep_existing
    )
    
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"✅ Complete! Processed {successful}/{len(npz_files)} files")
    logger.info("=" * 60)
    logger.info("")
    logger.info("View results:")
    logger.info(f"  CSV files: {discrimination_dir}")
    logger.info(f"  Web UI: http://localhost:3000/discrimination.html")
    logger.info("")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
