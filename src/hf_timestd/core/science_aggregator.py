#!/usr/bin/env python3
"""
Science Aggregator Service - Multi-Channel Data Aggregation for Science Products

This service runs independently from Phase 2 analytics, reading pre-computed
CSV files to generate science products that require multi-channel coordination:

1. TEC Estimation: Aggregates timing data across frequencies for same station
2. Event Detection: Identifies ionospheric disturbances from Doppler/TEC anomalies
3. Cross-Channel Correlation: Analyzes propagation consistency

Design Philosophy:
- NO IQ PROCESSING: Reads only CSV files from Phase 2
- LOW CPU PRIORITY: Background processing, doesn't block metrology
- COMPLETE DATA: Phase 2 already extracted everything, this just aggregates
"""

import argparse
import csv
import json
import logging
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TECMeasurement:
    """Single-frequency ToA measurement for TEC estimation."""
    frequency_hz: float
    toa_ms: float
    uncertainty_ms: float
    timestamp: float


class ScienceAggregator:
    """
    Aggregates Phase 2 data across channels for science products.
    
    Runs every 5 minutes, reading clock_offset CSVs from all channels
    to calculate TEC and detect ionospheric events.
    """
    
    def __init__(
        self,
        data_root: Path,
        poll_interval: float = 300.0,  # 5 minutes
        lookback_minutes: int = 10  # Process last 10 minutes
    ):
        """
        Initialize Science Aggregator.
        
        Args:
            data_root: Root directory containing phase2/{CHANNEL}/ subdirectories
            poll_interval: Seconds between aggregation cycles
            lookback_minutes: How many minutes back to process each cycle
        """
        self.data_root = Path(data_root)
        self.poll_interval = poll_interval
        self.lookback_minutes = lookback_minutes
        
        # Science output directory
        self.science_dir = self.data_root / 'phase2' / 'science'
        self.science_dir.mkdir(parents=True, exist_ok=True)
        
        # TEC output
        self.tec_dir = self.science_dir / 'tec'
        self.tec_dir.mkdir(parents=True, exist_ok=True)
        
        # Events output
        self.events_dir = self.science_dir / 'events'
        self.events_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize TEC estimator
        from hf_timestd.core.tec_estimator import TECEstimator
        self.tec_estimator = TECEstimator(high_precision_mode=True)
        
        # Track processed timestamps to avoid duplicates
        self.processed_timestamps = set()
        
        # Running flag
        self.running = False
        
        logger.info(f"Science Aggregator initialized")
        logger.info(f"  Data root: {data_root}")
        logger.info(f"  Science dir: {self.science_dir}")
        logger.info(f"  Poll interval: {poll_interval}s")
    
    def _find_channel_dirs(self) -> List[Path]:
        """Find all Phase 2 channel directories."""
        phase2_dir = self.data_root / 'phase2'
        if not phase2_dir.exists():
            logger.warning(f"Phase 2 directory not found: {phase2_dir}")
            return []
        
        # Find directories matching pattern: WWV_10000, WWVH_5000, etc.
        channels = []
        for item in phase2_dir.iterdir():
            if item.is_dir() and item.name != 'science':
                # Check if it has clock_offset subdirectory
                if (item / 'clock_offset').exists():
                    channels.append(item)
        
        logger.debug(f"Found {len(channels)} channel directories")
        return channels
    
    def _read_clock_offset_csv(
        self,
        channel_dir: Path,
        date_str: str,
        start_timestamp: float,
        end_timestamp: float
    ) -> List[Dict]:
        """
        Read clock offset CSV for given date and time range.
        
        Returns list of dicts with keys: minute_boundary, station, frequency_mhz,
        clock_offset_ms, uncertainty_ms, etc.
        """
        # Find CSV file
        clock_offset_dir = channel_dir / 'clock_offset'
        csv_files = list(clock_offset_dir.glob(f'*_clock_offset_{date_str}.csv'))
        
        if not csv_files:
            return []
        
        csv_file = csv_files[0]
        measurements = []
        
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    minute_boundary = float(row['minute_boundary_utc'])
                    
                    # Filter by time range
                    if start_timestamp <= minute_boundary <= end_timestamp:
                        measurements.append(row)
        
        except Exception as e:
            logger.error(f"Failed to read {csv_file}: {e}")
        
        return measurements
    
    def _aggregate_tec(self):
        """
        Aggregate multi-frequency measurements and calculate TEC.
        
        For each station (WWV, WWVH, CHU, BPM), collect ToA measurements
        across all frequencies for each minute, then estimate TEC.
        """
        # Determine time range to process
        now = datetime.now(timezone.utc)
        end_time = now - timedelta(minutes=2)  # Allow 2 min for Phase 2 to finish
        start_time = end_time - timedelta(minutes=self.lookback_minutes)
        
        start_timestamp = start_time.timestamp()
        end_timestamp = end_time.timestamp()
        
        date_str = end_time.strftime('%Y%m%d')
        
        logger.info(f"Aggregating TEC for {start_time} to {end_time}")
        
        # Find all channel directories
        channels = self._find_channel_dirs()
        
        if not channels:
            logger.warning("No channel directories found")
            return
        
        # Collect measurements from all channels
        all_measurements = []
        for channel_dir in channels:
            measurements = self._read_clock_offset_csv(
                channel_dir, date_str, start_timestamp, end_timestamp
            )
            all_measurements.extend(measurements)
        
        logger.debug(f"Collected {len(all_measurements)} measurements from {len(channels)} channels")
        
        # Group by (station, minute_boundary)
        grouped = {}
        for m in all_measurements:
            try:
                station = m['station']
                minute_boundary = int(float(m['minute_boundary']))
                freq_mhz = float(m['frequency_mhz'])
                clock_offset_ms = float(m['clock_offset_ms'])
                uncertainty_ms = float(m.get('uncertainty_ms', 1.0))
                
                key = (station, minute_boundary)
                if key not in grouped:
                    grouped[key] = []
                
                grouped[key].append({
                    'frequency_hz': freq_mhz * 1e6,
                    'toa_ms': clock_offset_ms,  # D_clock is effectively ToA
                    'uncertainty_ms': uncertainty_ms
                })
            except (KeyError, ValueError) as e:
                logger.debug(f"Skipping malformed row: {e}")
                continue
        
        logger.info(f"Grouped into {len(grouped)} (station, timestamp) pairs")
        
        # Calculate TEC for each group
        tec_results = []
        for (station, minute_boundary), measurements in grouped.items():
            # Need at least 2 frequencies
            if len(measurements) < 2:
                continue
            
            # Skip if already processed
            if (station, minute_boundary) in self.processed_timestamps:
                continue
            
            try:
                tec_result = self.tec_estimator.estimate_tec(
                    measurements=measurements,
                    station=station,
                    timestamp=float(minute_boundary)
                )
                
                if tec_result:
                    tec_results.append((station, minute_boundary, tec_result, measurements))
                    self.processed_timestamps.add((station, minute_boundary))
                    
                    logger.info(
                        f"TEC: {station} @ {minute_boundary}: {tec_result.tec_u:.2f} TECU "
                        f"(n_freq={tec_result.n_frequencies}, conf={tec_result.confidence:.2f})"
                    )
            
            except Exception as e:
                logger.error(f"TEC estimation failed for {station} @ {minute_boundary}: {e}")
        
        # Write TEC results to CSV
        if tec_results:
            self._write_tec_results(date_str, tec_results)
    
    def _write_tec_results(self, date_str: str, results: List[Tuple]):
        """Write TEC results to daily CSV file."""
        csv_file = self.tec_dir / f'tec_{date_str}.csv'
        
        # Check if file exists to determine if we need headers
        file_exists = csv_file.exists()
        
        try:
            with open(csv_file, 'a', newline='') as f:
                writer = csv.writer(f)
                
                # Write header if new file
                if not file_exists:
                    writer.writerow([
                        'timestamp_utc', 'minute_boundary', 'station',
                        'tec_tecu', 't_vacuum_error_ms', 'confidence', 'residuals_ms',
                        'n_frequencies', 'frequencies_mhz',
                        'group_delay_2_5_mhz', 'group_delay_5_mhz', 'group_delay_10_mhz',
                        'group_delay_15_mhz', 'group_delay_20_mhz', 'group_delay_25_mhz'
                    ])
                
                # Write results
                for station, minute_boundary, tec_result, measurements in results:
                    utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat()
                    
                    # Extract frequencies
                    freq_list = sorted([m['frequency_hz'] / 1e6 for m in measurements])
                    freq_str = ';'.join([f"{f:.2f}" for f in freq_list])
                    
                    # Map group delays
                    delay_map = tec_result.group_delay_ms
                    
                    writer.writerow([
                        utc_time,
                        minute_boundary,
                        station,
                        round(tec_result.tec_u, 3),
                        round(tec_result.t_vacuum_error_ms, 3),
                        round(tec_result.confidence, 4),
                        round(tec_result.residuals_ms, 3),
                        tec_result.n_frequencies,
                        freq_str,
                        round(delay_map.get(2.5, 0), 3) if 2.5 in delay_map else '',
                        round(delay_map.get(5.0, 0), 3) if 5.0 in delay_map else '',
                        round(delay_map.get(10.0, 0), 3) if 10.0 in delay_map else '',
                        round(delay_map.get(15.0, 0), 3) if 15.0 in delay_map else '',
                        round(delay_map.get(20.0, 0), 3) if 20.0 in delay_map else '',
                        round(delay_map.get(25.0, 0), 3) if 25.0 in delay_map else ''
                    ])
            
            logger.info(f"Wrote {len(results)} TEC results to {csv_file}")
        
        except Exception as e:
            logger.error(f"Failed to write TEC results: {e}")
    
    def _detect_events(self):
        """
        Detect ionospheric events from TEC and Doppler anomalies.
        
        Future implementation: Analyze time series for:
        - Traveling Ionospheric Disturbances (TIDs)
        - Spread-F events
        - Solar flare absorption
        """
        # Placeholder for future implementation
        pass
    
    def run(self):
        """Main processing loop."""
        self.running = True
        
        logger.info("Science Aggregator started")
        
        while self.running:
            try:
                # Aggregate TEC
                self._aggregate_tec()
                
                # Detect events (future)
                # self._detect_events()
                
                # Sleep until next cycle
                logger.debug(f"Sleeping for {self.poll_interval}s")
                time.sleep(self.poll_interval)
            
            except Exception as e:
                logger.error(f"Error in aggregation cycle: {e}", exc_info=True)
                time.sleep(60)  # Wait 1 minute on error
    
    def stop(self):
        """Stop the aggregator."""
        logger.info("Stopping Science Aggregator")
        self.running = False


def signal_handler(signum, frame):
    """Handle shutdown signals."""
    logger.info(f"Received signal {signum}, shutting down")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='Science Aggregator Service')
    parser.add_argument(
        '--data-root',
        type=Path,
        default=Path('/var/lib/timestd'),
        help='Root directory containing phase2/ subdirectories'
    )
    parser.add_argument(
        '--poll-interval',
        type=float,
        default=300.0,
        help='Seconds between aggregation cycles (default: 300)'
    )
    parser.add_argument(
        '--lookback',
        type=int,
        default=10,
        help='Minutes to look back each cycle (default: 10)'
    )
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and run aggregator
    aggregator = ScienceAggregator(
        data_root=args.data_root,
        poll_interval=args.poll_interval,
        lookback_minutes=args.lookback
    )
    
    aggregator.run()


if __name__ == '__main__':
    main()
