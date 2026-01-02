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
- USES TimeStdPaths: Proper path management via coordinated paths system
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

# Import TimeStdPaths for proper path management
from hf_timestd.paths import TimeStdPaths
from hf_timestd.core.propagation_stats import PropagationStatsCalculator

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
    
    Uses TimeStdPaths for all path operations to ensure consistency.
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
        
        # Initialize TimeStdPaths for proper path management
        self.paths = TimeStdPaths(data_root)
        
        # Science output directory (not managed by TimeStdPaths yet, but follows convention)
        self.science_dir = self.paths.get_phase2_root() / 'science'
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
        
        # Initialize TEC validator
        from hf_timestd.core.tec_validator import TECValidator
        ionex_dir = self.data_root / 'ionex'
        self.tec_validator = TECValidator(ionex_dir=ionex_dir)
        
        # Initialize propagation statistics calculator
        self.prop_stats_calculator = PropagationStatsCalculator(processing_version="3.3.0")
        
        # Propagation statistics output
        self.prop_stats_dir = self.science_dir / 'propagation_stats'
        self.prop_stats_dir.mkdir(parents=True, exist_ok=True)
        
        # Track processed timestamps to avoid duplicates
        self.processed_timestamps = set()
        
        # Running flag
        self.running = False
        
        logger.info(f"Science Aggregator initialized")
        logger.info(f"  Data root: {data_root}")
        logger.info(f"  Science dir: {self.science_dir}")
        logger.info(f"  Poll interval: {poll_interval}s")
        logger.info(f"  Using TimeStdPaths for path management")
    
    def _find_channel_dirs(self) -> List[str]:
        """
        Find all Phase 2 channel names using TimeStdPaths discovery.
        
        Returns:
            List of channel names (e.g., ['CHU_3330', 'WWV_10000'])
        """
        channels = self.paths.discover_phase2_channels()
        logger.debug(f"Discovered {len(channels)} Phase 2 channels: {channels}")
        return channels
    
    def _read_clock_offset_csv(
        self,
        channel_name: str,
        date_str: str,
        start_timestamp: float,
        end_timestamp: float
    ) -> List[Dict]:
        """
        Read clock offset data for given date and time range using HDF5 with CSV fallback.
        
        Args:
            channel_name: Channel name (e.g., 'CHU_3330', 'WWV_10000')
            date_str: Date string in YYYYMMDD format
            start_timestamp: Start timestamp (Unix epoch)
            end_timestamp: End timestamp (Unix epoch)
        
        Returns:
            List of dicts with keys: minute_boundary_utc, station, frequency_mhz,
            clock_offset_ms, uncertainty_ms, etc.
        """
        # HDF5 timing measurements are in the channel root directory, not clock_offset subdirectory
        # Use TimeStdPaths to get the Phase 2 channel directory
        channel_dir = self.paths.get_phase2_dir(channel_name)
        
        if not channel_dir.exists():
            logger.debug(f"Channel directory not found: {channel_dir}")
            return []
        
        # Try HDF5 first
        try:
            from hf_timestd.io import DataProductReader
            from datetime import datetime, timezone
            
            reader = DataProductReader(
                data_dir=channel_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=channel_name
            )
            
            # Convert timestamps to ISO format
            start_iso = datetime.fromtimestamp(start_timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')
            end_iso = datetime.fromtimestamp(end_timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')
            
            # Read measurements from HDF5
            measurements_hdf5 = reader.read_time_range(start=start_iso, end=end_iso)
            
            if measurements_hdf5:
                # Convert HDF5 format to expected dict format
                measurements = []
                for m in measurements_hdf5:
                    measurements.append({
                        'minute_boundary_utc': str(m.get('minute_boundary_utc', 0)),
                        'station': m.get('station', 'UNKNOWN'),
                        'frequency_mhz': str(m.get('frequency_mhz', 0)),
                        'clock_offset_ms': str(m.get('clock_offset_ms', 0)),
                        'uncertainty_ms': str(m.get('uncertainty_ms', 1.0))
                    })
                
                logger.debug(f"Read {len(measurements)} measurements from HDF5 for {channel_name}")
                return measurements
            else:
                logger.debug(f"No HDF5 measurements found for {channel_name}, trying CSV")
        
        except Exception as e:
            logger.debug(f"HDF5 read failed for {channel_name}, trying CSV: {e}")
        
        # CSV fallback (original implementation) - CSV files are in clock_offset subdirectory
        clock_offset_dir = self.paths.get_clock_offset_dir(channel_name)
        csv_files = list(clock_offset_dir.glob(f'*_clock_offset_{date_str}.csv'))
        
        if not csv_files:
            logger.debug(f"No clock offset CSV found for {channel_name} on {date_str}")
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
            
            logger.debug(f"Read {len(measurements)} measurements from CSV for {channel_name}")
        
        except Exception as e:
            logger.error(f"Failed to read {csv_file}: {e}")
        
        return measurements
    
    def _aggregate_tec(self):
        """
        Aggregate multi-frequency measurements and calculate TEC.
        
        For each station (WWV, WWVH, CHU, BPM), collect ToA measurements
        across all frequencies for each minute, then estimate TEC.
        
        Uses TimeStdPaths to discover channels and locate data.
        """
        # Determine time range to process
        now = datetime.now(timezone.utc)
        end_time = now - timedelta(minutes=2)  # Allow 2 min for Phase 2 to finish
        start_time = end_time - timedelta(minutes=self.lookback_minutes)
        
        start_timestamp = start_time.timestamp()
        end_timestamp = end_time.timestamp()
        
        date_str = end_time.strftime('%Y%m%d')
        
        logger.info(f"Aggregating TEC for {start_time} to {end_time}")
        
        # Find all channel names using TimeStdPaths
        channel_names = self._find_channel_dirs()
        
        if not channel_names:
            logger.warning("No Phase 2 channels found")
            return
        
        # Collect measurements from all channels
        all_measurements = []
        for channel_name in channel_names:
            measurements = self._read_clock_offset_csv(
                channel_name, date_str, start_timestamp, end_timestamp
            )
            all_measurements.extend(measurements)
        
        logger.debug(f"Collected {len(all_measurements)} measurements from {len(channel_names)} channels")
        
        # Group by (station, minute_boundary)
        grouped = {}
        for m in all_measurements:
            try:
                station = m['station']
                minute_boundary = int(float(m['minute_boundary_utc']))  # Fixed: was 'minute_boundary'
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
        """Write TEC results to HDF5 with CSV fallback."""
        
        # Try HDF5 first
        hdf5_success = False
        try:
            from hf_timestd.io import DataProductWriter
            
            writer = DataProductWriter(
                output_dir=self.tec_dir,
                product_level='L3',
                product_name='tec',
                channel='AGGREGATED',
                processing_version='3.2.0'
            )
            
            # Write each result to HDF5
            for station, minute_boundary, tec_result, measurements in results:
                utc_time = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
                
                # Extract frequencies
                freq_list = sorted([m['frequency_hz'] / 1e6 for m in measurements])
                freq_str = ','.join([f"{f:.2f}" for f in freq_list])
                
                # Determine quality flag based on confidence and residuals
                if tec_result.n_frequencies >= 4 and tec_result.confidence > 0.8 and tec_result.residuals_ms < 1.0:
                    quality_flag = 'GOOD'
                elif tec_result.n_frequencies >= 3 and tec_result.confidence > 0.5 and tec_result.residuals_ms < 2.0:
                    quality_flag = 'MARGINAL'
                else:
                    quality_flag = 'BAD'
                
                # Create base measurement
                measurement = {
                    'timestamp_utc': utc_time,
                    'minute_boundary': int(minute_boundary),
                    'station': station,
                    'tec_tecu': tec_result.tec_u,
                    't_vacuum_error_ms': tec_result.t_vacuum_error_ms,
                    'confidence': tec_result.confidence,
                    'n_frequencies': tec_result.n_frequencies,
                    'residuals_ms': tec_result.residuals_ms,
                    'frequencies_mhz': freq_str,
                    'quality_flag': quality_flag,
                    'processing_version': '3.3.0'
                }
                
                # Validate against GPS VTEC
                # Get receiver location (hardcoded for now - should come from config)
                receiver_lat = 40.0  # TODO: Get from system config
                receiver_lon = -105.0
                
                validation_fields = self.tec_validator.validate_tec_measurement(
                    measurement,
                    receiver_lat,
                    receiver_lon
                )
                
                # Add validation fields
                measurement.update(validation_fields)
                
                writer.write_measurement(measurement)
            
            writer.close()
            hdf5_success = True
            logger.info(f"Wrote {len(results)} TEC results to HDF5")
        
        except Exception as e:
            logger.warning(f"HDF5 write failed, falling back to CSV: {e}")
        
        # CSV fallback (always write for now during transition)
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
            
            logger.info(f"Wrote {len(results)} TEC results to CSV: {csv_file}")
        
        except Exception as e:
            logger.error(f"Failed to write TEC CSV results: {e}")
            if not hdf5_success:
                raise  # Re-raise if both HDF5 and CSV failed
    
    def _aggregate_propagation_stats(self):
        """
        Aggregate propagation mode statistics from timing measurements.
        
        Calculates hourly statistics on propagation modes, MUF estimates,
        and data quality from the timing measurements.
        """
        # Determine time range - aggregate the previous hour
        now = datetime.now(timezone.utc)
        period_end = now.replace(minute=0, second=0, microsecond=0)
        period_start = period_end - timedelta(hours=1)
        
        # Skip if we've already processed this hour
        hour_key = period_end.strftime('%Y%m%d_%H')
        if hasattr(self, '_processed_prop_hours'):
            if hour_key in self._processed_prop_hours:
                return
        else:
            self._processed_prop_hours = set()
        
        date_str = period_end.strftime('%Y%m%d')
        
        logger.info(f"Aggregating propagation stats for {period_start} to {period_end}")
        
        # Find all channel names
        channel_names = self._find_channel_dirs()
        
        if not channel_names:
            logger.warning("No Phase 2 channels found for propagation stats")
            return
        
        # Collect timing measurements with propagation mode info
        all_measurements = []
        for channel_name in channel_names:
            measurements = self._read_timing_measurements_for_propagation(
                channel_name, period_start, period_end
            )
            all_measurements.extend(measurements)
        
        if not all_measurements:
            logger.info("No measurements found for propagation statistics")
            return
        
        logger.info(f"Collected {len(all_measurements)} measurements for propagation stats")
        
        # Calculate hourly statistics
        hourly_stats = self.prop_stats_calculator.calculate_hourly_stats(
            measurements=all_measurements,
            period_start=period_start,
            period_end=period_end
        )
        
        if hourly_stats:
            self._write_propagation_stats(date_str, hourly_stats)
            self._processed_prop_hours.add(hour_key)
            logger.info(f"Wrote {len(hourly_stats)} propagation statistics records")
    
    def _read_timing_measurements_for_propagation(
        self,
        channel_name: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Dict]:
        """
        Read timing measurements with propagation mode information.
        
        Args:
            channel_name: Channel name
            start_time: Start of time range
            end_time: End of time range
        
        Returns:
            List of measurement dictionaries with propagation_mode, snr_db, etc.
        """
        channel_dir = self.paths.get_phase2_dir(channel_name)
        
        if not channel_dir.exists():
            return []
        
        try:
            from hf_timestd.io import DataProductReader
            
            reader = DataProductReader(
                data_dir=channel_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=channel_name
            )
            
            start_iso = start_time.isoformat().replace('+00:00', 'Z')
            end_iso = end_time.isoformat().replace('+00:00', 'Z')
            
            measurements = reader.read_time_range(start=start_iso, end=end_iso)
            
            return measurements if measurements else []
        
        except Exception as e:
            logger.debug(f"Failed to read timing measurements for {channel_name}: {e}")
            return []
    
    def _write_propagation_stats(self, date_str: str, stats_list: List[Dict]):
        """
        Write propagation statistics to HDF5.
        
        Args:
            date_str: Date string in YYYYMMDD format
            stats_list: List of statistics dictionaries
        """
        try:
            from hf_timestd.io import DataProductWriter
            
            writer = DataProductWriter(
                output_dir=self.prop_stats_dir,
                product_level='L3C',
                product_name='propagation_stats',
                channel='AGGREGATED'
            )
            
            for stats in stats_list:
                writer.write_measurement(stats)
            
            logger.info(f"Wrote {len(stats_list)} propagation statistics to HDF5")
        
        except Exception as e:
            logger.error(f"Failed to write propagation statistics: {e}")
    
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
                
                # Aggregate propagation statistics (hourly)
                self._aggregate_propagation_stats()
                
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
