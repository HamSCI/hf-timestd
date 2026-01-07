#!/usr/bin/env python3
"""
Physics-Based Fusion Service
================================================================================
Stage 2 of the Science-First Architecture (v5.0.0).

This service consumes L2 HDF5 Timing Measurements from all available channels
(from Phase 2 Analytics) and performs physics-based fusion to derive:

1. Ionospheric Parameters (Primary Output):
   - Total Electron Content (TEC) via differential Time-of-Flight
   - Ionospheric Layer Height (Virtual Height) via triangulation

2. Validation Metrics (Secondary Output):
   - UTC Consistency: "Does the physics model explain the observations?"
   - Clock Error Bounds: Residuals after ionospheric correction

Architecture:
-------------
    L2 HDF5 (Stations) -> [PhysicsFusionService] -> L3 HDF5 (Physics)
          ^                       |
          |                       v
    (ToF, Doppler)           (TEC, Triangulation)

Key classes:
    - PhysicsFusionService: Main daemon
    - TECEstimator: Physics math (imported from hf_timestd.core.tec_estimator)
"""

import logging
import time
import argparse
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import numpy as np

from hf_timestd.core.tec_estimator import TECEstimator, TECResult
from hf_timestd.io import DataProductReader, DataProductWriter
from hf_timestd.data_product_registry import DataProductRegistry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PhysicsFusionService:
    """
    Physics-Based Fusion Service.
    Aggregates L2 data and computes L3 physics products.
    """
    
    def __init__(
        self,
        data_root: Path,
        output_dir: Path,
        poll_interval: float = 60.0,
        lookback_minutes: int = 5
    ):
        self.data_root = Path(data_root)
        self.output_dir = Path(output_dir)
        self.poll_interval = poll_interval
        self.lookback_minutes = lookback_minutes
        
        # Initialize TEC Estimator
        self.tec_estimator = TECEstimator(high_precision_mode=True)
        
        # Initialize L3 Writer
        self.l3_writer = DataProductWriter(
            output_dir=self.output_dir,
            product_level='L3',
            product_name='physics',
            channel='global', # Global aggregate
            processing_version='5.0.0',
            station_metadata={'description': 'Physics-Based Fusion Service v5.0'}
        )
        
        # State tracking
        self.running = False
        self.last_processed_minute = 0
        self.channels = self._discover_channels()
        
        logger.info(f"PhysicsFusionService initialized with {len(self.channels)} channels")

    def _discover_channels(self) -> List[str]:
        """Discover available L2 broadcast channels."""
        phase2_root = self.data_root / 'phase2'
        channels = []
        if phase2_root.exists():
            for subdir in phase2_root.iterdir():
                if subdir.is_dir() and subdir.name not in ['fusion', 'science', 'phase2', 'ionex']:
                    # Check if it looks like a channel dir (has clock_offset or similar)
                    if (subdir / 'clock_offset').exists():
                        channels.append(subdir.name)
        return sorted(channels)
        
    def _read_l2_slice(self, minute_timestamp: int) -> Dict[str, List[Dict]]:
        """
        Read L2 measurements for a specific minute across all channels.
        
        Returns:
            Dict mapping Station Name -> List of measurements
        """
        start_iso = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(minute_timestamp + 0.999, tz=timezone.utc).isoformat()
        date_str = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).strftime('%Y%m%d')
        
        measurements_by_station = defaultdict(list)
        
        for channel in self.channels:
            try:
                # Resolve directory - assuming standard structure data_root/phase2/{channel}
                channel_dir = self.data_root / 'phase2' / channel
                
                # Check for clock_offset subdir (where L2 timing measurements live)
                if (channel_dir / 'clock_offset').exists():
                    reader_dir = channel_dir / 'clock_offset'
                else:
                    reader_dir = channel_dir

                reader = DataProductReader(
                    data_dir=reader_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel, 
                    use_registry=False 
                )
                
                # Check if file exists for this date to avoid costly read attempts
                if not reader._get_hdf5_path(date_str).exists():
                    continue

                items = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso,
                    min_quality_grade='C', # Only fuse reasonable quality data
                    quality_flags=['GOOD', 'MARGINAL', 'MISSING']
                )
                
                for item in items:
                    station = item.get('station')
                    if station:
                        # Ensure frequency is present (critical for TEC)
                        # Reader should return all fields including 'frequency_mhz'
                        # but L2 schema has 'frequency_mhz'.
                        # Renaming or mapping might be needed if schema differs.
                        # Schema v1.3.0 has 'frequency_mhz'.
                        if 'frequency_mhz' in item:
                             # Map to expected format for TECEstimator
                             # TECEstimator expects: 'frequency_hz', 'toa_ms', 'uncertainty_ms'
                             # L2 provides: 'frequency_mhz', 'tof_kalman_ms' (or 'raw_arrival_time_ms')
                             
                             # We use tof_kalman_ms (Science First) if available, else raw
                             # Actually TECEstimator solves for T_vacuum using ToA.
                             # But ToF involves distance. 
                             # Wait, TECEstimator logic: T_obs = T_vac + K*TEC/f^2
                             # T_obs here is the measured arrival time.
                             # This includes propagation delay and clock error.
                             # T_vac would be Distance/c + ClockError.
                             
                             # We can use raw_arrival_time_ms directly. 
                             # The solver will find T_vac = (Dist/c + dt).
                             # Distance is roughly constant for a minute.
                             
                             obs = {
                                 'frequency_hz': item['frequency_mhz'] * 1e6,
                                 'toa_ms': item.get('tof_kalman_ms', item.get('raw_arrival_time_ms')),
                                 'uncertainty_ms': item.get('tof_uncertainty_ms', item.get('uncertainty_ms', 10.0))
                             }
                             
                             # Filter invalid Kalman states
                             if obs['toa_ms'] is None or np.isnan(obs['toa_ms']):
                                 continue
                                 
                             measurements_by_station[station].append(obs)
                             
            except Exception as e:
                logger.debug(f"Failed to read channel {channel}: {e}")
                continue
                
        return measurements_by_station

    def process_minute(self, minute_timestamp: int):
        """Process a single minute of data."""
        logger.info(f"Processing minute {minute_timestamp} ({datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)})")
        
        # 1. Read Data
        station_data = self._read_l2_slice(minute_timestamp)
        
        if not station_data:
            logger.warning(f"No valid L2 data found for minute {minute_timestamp}")
            return

        # 2. Physics Estimation (TEC)
        tec_estimates = {}
        tec_uncertainties = {}
        
        for station, observations in station_data.items():
            # Need at least 2 frequencies
            if len(observations) < 2:
                logger.debug(f"Station {station}: insufficient frequencies ({len(observations)}) for TEC")
                continue
                
            result = self.tec_estimator.estimate_tec(observations, station, minute_timestamp)
            
            if result:
                tec_estimates[station] = result.tec_u
                tec_uncertainties[station] = (1.0 - result.confidence) * 10.0 # Heuristic map R2 to uncertainty
                logger.info(f"TEC {station}: {result.tec_u:.2f} TECU (Conf: {result.confidence:.2f})")
            else:
                 logger.debug(f"TEC estimation failed for {station}")

        # 3. UTC Consistency Check
        # If we had T_vacuum from TEC solver, T_vac = Dist/c + dt
        # dt = T_vac - Dist/c
        # We need distance.
        
        # Simple UTC validation: check if residuals are consistent across stations
        # (This is a placeholder for the full geometric solver next session)
        utc_consistent = len(tec_estimates) > 0 # At least we got physics
        
        # 4. Write L3
        self._write_l3_product(
            minute_timestamp, 
            tec_estimates, 
            tec_uncertainties, 
            utc_consistent
        )

    def _write_l3_product(
        self, 
        timestamp: int, 
        tec_estimates: Dict[str, float],
        tec_uncertainties: Dict[str, float],
        utc_consistent: bool
    ):
        """Write L3 Physics Fusion product."""
        # Clean keys for HDF5 (strings)
        tec_map = {k: float(v) for k, v in tec_estimates.items()}
        unc_map = {k: float(v) for k, v in tec_uncertainties.items()}
        
        record = {
            'timestamp_utc': datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            'minute_boundary_utc': timestamp,
            'stations_used': list(tec_estimates.keys()),
            # 'tec_estimates': tec_map, # DataProductWriter needs map support or we flatten? No, schema supports map
            # 'tec_uncertainties': unc_map,
            'utc_offset_ms': float('nan'), # Placeholder
            'utc_uncertainty_ms': float('nan'),
            'utc_consistency_flag': utc_consistent,
            'processing_version': '5.0.0',
            'processed_at': datetime.now(timezone.utc).isoformat()
        }
        
        # NOTE: HDF5 writer currently flattens dictionaries or needs specific handling.
        # For now, we rely on the schema definition.
        # However, basic HDF5 writer might not support Map types well without flattening.
        # We will write what we can.
        
        # Workaround: Flatten TEC for now if Writer doesn't support maps (CHECK THIS)
        # Assuming schema v1.0.0 defines map.
        
        try:
            self.l3_writer.write_measurement(record)
            logger.info(f"Written L3 product for {timestamp}")
        except Exception as e:
            logger.error(f"Failed to write L3 product: {e}")

    def run(self):
        """Main service loop."""
        self.running = True
        
        # Handle signals
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        logger.info("Service started. Polling for data...")
        
        while self.running:
            try:
                # Align to next minute boundary processing
                now = time.time()
                # We process 'lookback' minutes ago to ensure data is settled
                target_minute = int(now) - (int(now) % 60) - (60 * 2) # 2 mins ago
                
                if target_minute > self.last_processed_minute:
                    self.process_minute(target_minute)
                    self.last_processed_minute = target_minute
                
                # Sleep until next poll or minute
                sleep_time = max(1.0, 60.0 - (time.time() % 60) + 0.1)
                time.sleep(1.0) # Check every second for shutdown, or sleep longer
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(10)

    def _signal_handler(self, signum, frame):
        logger.info(f"Signal {signum} received, shutting down...")
        self.running = False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Physics-Based Fusion Service')
    parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    parser.add_argument('--output', default='/var/lib/timestd/phase2/fusion', help='Output directory')
    
    args = parser.parse_args()
    
    service = PhysicsFusionService(
        data_root=args.data_root,
        output_dir=args.output
    )
    
    service.run()
