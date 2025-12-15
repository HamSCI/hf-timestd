#!/usr/bin/env python3
"""
Phase 3 Product Generator - CLI Script

Process Phase 1 raw archive + Phase 2 timing analysis into
decimated 10 Hz DRF products for PSWS upload.

Usage:
    # Process a single day for one channel
    python run_phase3_processor.py --data-root /tmp/timestd-test \
        --channel "WWV 10 MHz" --date 2025-12-04

    # Process all channels for yesterday
    python run_phase3_processor.py --data-root /tmp/timestd-test \
        --all-channels --yesterday

    # Real-time streaming mode
    python run_phase3_processor.py --data-root /tmp/timestd-test \
        --channel "WWV 10 MHz" --streaming

Output Structure:
    products/{CHANNEL}/
    ├── decimated/        # 10 Hz DRF files
    ├── gap_analysis/     # JSON gap reports
    └── timing_annotations/  # CSV timing quality
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

try:
    from grape_recorder.grape import (
        Phase3ProductEngine,
        Phase3Config,
        create_phase3_engine,
        process_channel_day
    )
    from grape_recorder.paths import GRAPEPaths
except ImportError as e:
    print(f"Import error: {e}")
    print("Make sure grape-recorder is installed or run from project root")
    sys.exit(1)


# Channel frequency mapping
CHANNEL_FREQUENCIES = {
    'WWV 2.5 MHz': 2.5e6,
    'WWV 5 MHz': 5e6,
    'WWV 10 MHz': 10e6,
    'WWV 15 MHz': 15e6,
    'WWV 20 MHz': 20e6,
    'WWV 25 MHz': 25e6,
    'CHU 3.33 MHz': 3.33e6,
    'CHU 7.85 MHz': 7.85e6,
    'CHU 14.67 MHz': 14.67e6,
}

# Default station config (override with --config)
DEFAULT_STATION_CONFIG = {
    'callsign': 'UNKNOWN',
    'grid_square': 'UNKNOWN',
    'receiver_name': 'GRAPE',
    'psws_station_id': 'UNKNOWN',
    'psws_instrument_id': '1'
}


def setup_logging(log_level: str, log_file: Optional[Path] = None):
    """Configure logging."""
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


def load_station_config(config_file: Optional[Path]) -> Dict[str, Any]:
    """Load station configuration from TOML file."""
    if config_file is None:
        return DEFAULT_STATION_CONFIG.copy()
    
    try:
        import toml
        with open(config_file, 'r') as f:
            config = toml.load(f)
        
        station = config.get('station', {})
        return {
            'callsign': station.get('callsign', DEFAULT_STATION_CONFIG['callsign']),
            'grid_square': station.get('grid_square', DEFAULT_STATION_CONFIG['grid_square']),
            'receiver_name': station.get('receiver_name', DEFAULT_STATION_CONFIG['receiver_name']),
            'psws_station_id': station.get('psws_station_id', DEFAULT_STATION_CONFIG['psws_station_id']),
            'psws_instrument_id': str(station.get('psws_instrument_id', DEFAULT_STATION_CONFIG['psws_instrument_id']))
        }
    except Exception as e:
        logging.warning(f"Could not load config file {config_file}: {e}")
        return DEFAULT_STATION_CONFIG.copy()


def discover_channels(data_root: Path) -> List[str]:
    """Discover channels with Phase 1 raw archive data."""
    paths = GRAPEPaths(data_root)
    return paths.discover_channels()


def process_channel(
    data_root: Path,
    channel_name: str,
    date_str: str,
    station_config: Dict[str, Any]
) -> Dict[str, Any]:
    """Process one channel for one day."""
    logger = logging.getLogger(__name__)
    
    # Get frequency for channel
    frequency_hz = CHANNEL_FREQUENCIES.get(channel_name)
    if frequency_hz is None:
        # Try to parse from channel name
        import re
        match = re.search(r'([\d.]+)\s*MHz', channel_name)
        if match:
            frequency_hz = float(match.group(1)) * 1e6
        else:
            logger.error(f"Unknown frequency for channel {channel_name}")
            return {'error': f'Unknown frequency for {channel_name}'}
    
    logger.info(f"Processing {channel_name} @ {frequency_hz/1e6:.2f} MHz for {date_str}")
    
    try:
        results = process_channel_day(
            data_root=data_root,
            channel_name=channel_name,
            frequency_hz=frequency_hz,
            station_config=station_config,
            date_str=date_str
        )
        return results
    except Exception as e:
        logger.error(f"Error processing {channel_name}: {e}", exc_info=True)
        return {'error': str(e)}


def run_batch(
    data_root: Path,
    channels: List[str],
    date_str: str,
    station_config: Dict[str, Any]
):
    """Process multiple channels for one day."""
    logger = logging.getLogger(__name__)
    
    all_results = {}
    total_samples = 0
    total_minutes = 0
    total_gaps = 0
    
    start_time = time.time()
    
    for channel in channels:
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {channel}")
        logger.info(f"{'='*60}")
        
        results = process_channel(
            data_root=data_root,
            channel_name=channel,
            date_str=date_str,
            station_config=station_config
        )
        
        all_results[channel] = results
        
        if 'error' not in results:
            total_samples += results.get('samples_written', 0)
            total_minutes += results.get('minutes_processed', 0)
            total_gaps += results.get('gaps_detected', 0)
    
    elapsed = time.time() - start_time
    
    # Print summary
    logger.info(f"\n{'='*60}")
    logger.info("BATCH PROCESSING SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Date: {date_str}")
    logger.info(f"Channels processed: {len(channels)}")
    logger.info(f"Total minutes: {total_minutes}")
    logger.info(f"Total samples: {total_samples:,}")
    logger.info(f"Total gaps detected: {total_gaps}")
    logger.info(f"Processing time: {elapsed:.1f}s")
    logger.info(f"{'='*60}")
    
    # Per-channel summary
    for channel, results in all_results.items():
        if 'error' in results:
            logger.error(f"  {channel}: ERROR - {results['error']}")
        else:
            grades = results.get('quality_grades', {})
            grade_str = ', '.join(f"{g}:{n}" for g, n in sorted(grades.items()))
            logger.info(f"  {channel}: {results.get('minutes_processed', 0)} min, "
                       f"grades=[{grade_str}]")
    
    return all_results


def main():
    parser = argparse.ArgumentParser(
        description='Phase 3 Product Generator - Create decimated 10 Hz DRF for PSWS upload',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    # Required arguments
    parser.add_argument('--data-root', type=Path, required=True,
                       help='Root data directory (e.g., /tmp/timestd-test)')
    
    # Channel selection
    channel_group = parser.add_mutually_exclusive_group(required=True)
    channel_group.add_argument('--channel', type=str,
                              help='Single channel to process (e.g., "WWV 10 MHz")')
    channel_group.add_argument('--all-channels', action='store_true',
                              help='Process all discovered channels')
    channel_group.add_argument('--channels', nargs='+',
                              help='List of channels to process')
    
    # Date selection
    date_group = parser.add_mutually_exclusive_group()
    date_group.add_argument('--date', type=str,
                           help='Date to process (YYYY-MM-DD or YYYYMMDD)')
    date_group.add_argument('--yesterday', action='store_true',
                           help='Process yesterday\'s data')
    date_group.add_argument('--today', action='store_true',
                           help='Process today\'s data (may be incomplete)')
    date_group.add_argument('--streaming', action='store_true',
                           help='Run in streaming mode (real-time processing)')
    
    # Configuration
    parser.add_argument('--config', type=Path,
                       help='Path to timestd-config.toml for station config')
    parser.add_argument('--callsign', type=str,
                       help='Station callsign (overrides config)')
    parser.add_argument('--grid', type=str,
                       help='Grid square (overrides config)')
    parser.add_argument('--psws-station-id', type=str,
                       help='PSWS station ID (overrides config)')
    parser.add_argument('--psws-instrument-id', type=str, default='1',
                       help='PSWS instrument ID (default: 1)')
    
    # Logging
    parser.add_argument('--log-level', type=str, default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Logging level')
    parser.add_argument('--log-file', type=Path,
                       help='Log file path')
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.log_level, args.log_file)
    logger = logging.getLogger(__name__)
    
    # Load station config
    station_config = load_station_config(args.config)
    
    # Apply overrides
    if args.callsign:
        station_config['callsign'] = args.callsign
    if args.grid:
        station_config['grid_square'] = args.grid
    if args.psws_station_id:
        station_config['psws_station_id'] = args.psws_station_id
    if args.psws_instrument_id:
        station_config['psws_instrument_id'] = args.psws_instrument_id
    
    logger.info("Phase 3 Product Generator")
    logger.info(f"  Data root: {args.data_root}")
    logger.info(f"  Station: {station_config['callsign']} @ {station_config['grid_square']}")
    
    # Determine channels to process
    if args.all_channels:
        channels = discover_channels(args.data_root)
        if not channels:
            logger.error("No channels found in data root")
            sys.exit(1)
        logger.info(f"  Discovered channels: {channels}")
    elif args.channels:
        channels = args.channels
    else:
        channels = [args.channel]
    
    # Determine date
    if args.streaming:
        logger.error("Streaming mode not yet implemented")
        sys.exit(1)
    elif args.yesterday:
        target_date = datetime.now(tz=timezone.utc).date() - timedelta(days=1)
        date_str = target_date.strftime('%Y-%m-%d')
    elif args.today:
        target_date = datetime.now(tz=timezone.utc).date()
        date_str = target_date.strftime('%Y-%m-%d')
    elif args.date:
        date_str = args.date
    else:
        # Default to yesterday
        target_date = datetime.now(tz=timezone.utc).date() - timedelta(days=1)
        date_str = target_date.strftime('%Y-%m-%d')
    
    logger.info(f"  Date: {date_str}")
    logger.info(f"  Channels: {channels}")
    
    # Run batch processing
    results = run_batch(
        data_root=args.data_root,
        channels=channels,
        date_str=date_str,
        station_config=station_config
    )
    
    # Exit with error if any channel failed
    has_errors = any('error' in r for r in results.values())
    sys.exit(1 if has_errors else 0)


if __name__ == '__main__':
    main()
