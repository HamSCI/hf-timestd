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
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from hf_timestd.core.tec_estimator import TECEstimator, TECResult
from hf_timestd.core.carrier_tec import CarrierTECEstimator
from hf_timestd.core.iono_tomography import IonoTomography, RayPath
from hf_timestd.core.vtec_mapper import VTECMapper, IPPMeasurement
from hf_timestd.io import DataProductReader, DataProductWriter

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False

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
        self.tec_estimator = TECEstimator()
        
        # Initialize carrier-phase dTEC estimator
        self.carrier_tec = CarrierTECEstimator(data_root=self.data_root)
        
        # Initialize E/F layer tomography
        self.tomography = IonoTomography()
        
        # Initialize VTEC mapper
        self.vtec_mapper = VTECMapper()
        self.ionex_dir = self.data_root / 'phase2' / 'ionex'
        self.ionex_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize L3 Writers
        self.l3_writer = DataProductWriter(
            output_dir=self.output_dir,
            product_level='L3',
            product_name='physics',
            channel='global', # Global aggregate
            processing_version='5.0.0',
            station_metadata={'description': 'Physics-Based Fusion Service v5.0'}
        )
        
        # Second writer for individual station TEC records (consumed by Web API)
        # PropagationService looks in phase2/science/tec/AGGREGATED_tec_*.h5
        self.tec_dir = self.data_root / 'phase2' / 'science' / 'tec'
        self.tec_writer = DataProductWriter(
            output_dir=self.tec_dir,
            product_level='L3', # Schema says L3A but product_level is used for schema lookup L3
            product_name='tec',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Physics-Based Fusion TEC Output'}
        )
        
        # State tracking
        self.running = False
        self.last_processed_minute = 0
        self.channels = self._discover_channels()
        self._reader_cache: Dict[str, DataProductReader] = {}
        self._minute_retry_counts: Dict[int, int] = {}
        self._max_retry_history = 720  # Keep at most 12h of minute retry state
        
        # Data freshness tracking for upstream starvation detection
        self.upstream_stale_warning_issued = False
        self.max_upstream_age_seconds = 300.0  # 5 minutes - warn if L2 data older than this
        
        logger.info(f"PhysicsFusionService initialized with {len(self.channels)} channels")

    def _get_reader(self, channel: str) -> DataProductReader:
        """Get (or create) a cached reader for a channel."""
        reader = self._reader_cache.get(channel)
        if reader is not None:
            return reader

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
        self._reader_cache[channel] = reader
        return reader

    def _prune_retry_counters(self, now_epoch: float) -> None:
        """Keep retry tracking bounded to avoid unbounded state growth."""
        cutoff = int(now_epoch) - (12 * 3600)
        stale_minutes = [minute for minute in self._minute_retry_counts if minute < cutoff]
        for minute in stale_minutes:
            del self._minute_retry_counts[minute]

        if len(self._minute_retry_counts) > self._max_retry_history:
            for minute in sorted(self._minute_retry_counts)[:-self._max_retry_history]:
                del self._minute_retry_counts[minute]

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
    
    def _check_upstream_freshness(self) -> Tuple[bool, float]:
        """
        Check if upstream L2 data is fresh enough.
        
        Returns:
            Tuple of (is_fresh, newest_age_seconds)
        """
        newest_mtime = 0.0
        
        for channel in self.channels:
            l2_dir = self.data_root / 'phase2' / channel / 'clock_offset'
            if l2_dir.exists():
                h5_files = list(l2_dir.glob("*.h5"))
                if h5_files:
                    channel_mtime = max(f.stat().st_mtime for f in h5_files)
                    newest_mtime = max(newest_mtime, channel_mtime)
        
        if newest_mtime == 0.0:
            return False, float('inf')
        
        age_seconds = time.time() - newest_mtime
        return age_seconds < self.max_upstream_age_seconds, age_seconds
        
    def _read_l2_slice(self, minute_timestamp: int) -> Dict[tuple, List[Dict]]:
        """
        Read L2 measurements for a specific minute across all channels.
        
        Returns:
            Dict mapping (Station, Mode) -> List of measurements
        """
        start_iso = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        end_iso = datetime.fromtimestamp(minute_timestamp + 59.999, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        
        measurements_grouped = defaultdict(list)
        
        for channel in self.channels:
            try:
                reader = self._get_reader(channel)

                items = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso
                )
                
                for item in items:
                    station = item.get('station')
                    # The following 'if station:' block was incomplete and causing indentation issues.
                    # The logic below should apply to all items with a station.
                    if not station:
                        continue
                        
                    # Ensure frequency is present (critical for TEC)
                    # Reader should return all fields including 'frequency_mhz'
                    # but L2 schema has 'frequency_mhz'.
                    # Renaming or mapping might be needed if schema differs.
                    if 'frequency_mhz' not in item:
                        continue

                    # Resolve TOA
                    # Prefer Kalman if available and valid, fallback to raw
                    toa = item.get('tof_kalman_ms')
                    if toa is None or np.isnan(toa):
                        toa = item.get('raw_arrival_time_ms')
                        
                    uncertainty = item.get('tof_uncertainty_ms')
                    if uncertainty is None or np.isnan(uncertainty):
                        uncertainty = item.get('uncertainty_ms', 10.0)

                    # Resolve Mode
                    mode = item.get('propagation_mode', 'UNKNOWN')

                    obs = {
                        'frequency_hz': item['frequency_mhz'] * 1e6,
                        'toa_ms': toa,
                        'uncertainty_ms': uncertainty,
                        'mode': mode
                    }
                    
                    # Filter invalid Kalman states
                    if obs['toa_ms'] is None or np.isnan(obs['toa_ms']):
                        continue
                    
                    measurements_grouped[(station, mode)].append(obs)
                             
            except Exception as e:
                logger.debug(f"Failed to read channel {channel}: {e}")
                continue
                
        return measurements_grouped

    def process_minute(self, minute_timestamp: int, station_data: Optional[Dict[tuple, List[Dict]]] = None):
        """Process a single minute of data."""
        logger.info(f"Processing minute {minute_timestamp} ({datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)})")
        
        # 0. Check upstream data freshness
        is_fresh, age_seconds = self._check_upstream_freshness()
        if not is_fresh:
            if not self.upstream_stale_warning_issued:
                logger.warning(
                    f"Upstream L2 data is stale ({age_seconds:.0f}s old, "
                    f"threshold={self.max_upstream_age_seconds:.0f}s). "
                    "L2 calibration service may have stopped."
                )
                self.upstream_stale_warning_issued = True
            # Continue processing - use whatever data is available
        else:
            if self.upstream_stale_warning_issued:
                logger.info(f"Upstream L2 data is fresh again ({age_seconds:.0f}s old)")
                self.upstream_stale_warning_issued = False
        
        # 1. Read Data (allow pre-fetched station_data from run loop)
        if station_data is None:
            station_data = self._read_l2_slice(minute_timestamp)
        
        if not station_data:
            logger.warning(f"No valid L2 data found for minute {minute_timestamp}")
            return

        # 2. Physics Estimation (TEC)
        tec_estimates = {}
        
        for (station, mode), observations in station_data.items():
            # Need at least 2 frequencies for this SPECIFIC mode
            if len(observations) < 2:
                logger.debug(f"Station {station} Mode {mode}: insufficient frequencies ({len(observations)})")
                continue
                
            result = self.tec_estimator.estimate_tec(observations, station, minute_timestamp)
            
            if result:
                if not (0.0 < result.tec_u <= 200.0):
                    logger.warning(
                        f"TEC out of bounds for {station} ({mode}): {result.tec_u:.2f} TECU - skipping"
                    )
                    continue

                # Attach mode to result for writing
                result.propagation_mode = mode
                tec_estimates[(station, mode)] = result
                logger.info(f"TEC {station} ({mode}): {result.tec_u:.2f} TECU (Conf: {result.confidence:.2f})")
            else:
                 logger.debug(f"TEC estimation failed for {station} ({mode})")

        # 3. E/F Layer Tomography
        tomo_result = None
        if len(tec_estimates) >= 2:
            try:
                paths = self.tomography.build_paths_from_tec_results(tec_estimates)
                if len(paths) >= 2:
                    tomo_result = self.tomography.solve(paths)
                    if tomo_result:
                        tomo_result.timestamp = float(minute_timestamp)
                        logger.info(
                            f"Tomography: E={tomo_result.tec_e_tecu:.1f} F={tomo_result.tec_f_tecu:.1f} TECU "
                            f"(ratio={tomo_result.e_f_ratio:.2f}, conf={tomo_result.confidence:.2f})"
                        )
            except Exception as e:
                logger.warning(f"Tomography failed: {e}")

        # 4. VTEC Map Generation
        vtec_result = None
        if len(tec_estimates) >= 3:
            try:
                ipp_measurements = self._build_ipp_measurements(tec_estimates)
                if len(ipp_measurements) >= 3:
                    vtec_result = self.vtec_mapper.generate_map(
                        ipp_measurements, timestamp=float(minute_timestamp)
                    )
                    if vtec_result:
                        logger.info(
                            f"VTEC map: {vtec_result.n_ipps} IPPs, "
                            f"RMS={vtec_result.rms_residual_tecu:.2f} TECU, "
                            f"conf={vtec_result.confidence:.2f}"
                        )
                        # Write IONEX file
                        ts = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
                        ionex_path = self.ionex_dir / f"hftd_{ts.strftime('%Y%m%d_%H%M')}.ionex"
                        self.vtec_mapper.write_ionex(vtec_result, ionex_path)
            except Exception as e:
                logger.warning(f"VTEC map generation failed: {e}")

        # 5. UTC Consistency Check
        utc_consistent = len(tec_estimates) > 0
        
        # 6. Write L3
        self._write_physics_summary(
            minute_timestamp, 
            tec_estimates, 
            utc_consistent
        )
        
        # 7. Write per-station TEC records
        self._write_tec_records(
            minute_timestamp,
            tec_estimates
        )

    def _build_ipp_measurements(
        self,
        tec_estimates: Dict[tuple, TECResult],
    ) -> List[IPPMeasurement]:
        """
        Build IPP measurements from TEC estimates for VTEC mapping.
        
        Computes ionospheric pierce points at the great-circle midpoint
        between receiver and transmitter, and converts sTEC to vTEC.
        """
        # Known station coordinates (lat, lon)
        STATION_COORDS = {
            'WWV': (40.68, -105.04),
            'WWVH': (21.99, -159.76),
            'CHU': (45.29, -75.75),
            'BPM': (34.95, 109.51),
        }
        
        ipp_list = []
        for (station, mode), result in tec_estimates.items():
            if result.tec_u <= 0 or result.confidence < 0.3:
                continue
            
            # Look up station coordinates
            station_base = station.split('_')[0] if '_' in station else station
            coords = STATION_COORDS.get(station_base)
            if coords is None:
                continue
            
            station_lat, station_lon = coords
            
            # Compute IPP at midpoint
            ipp_lat, ipp_lon = self.vtec_mapper.compute_ipp(
                station_lat, station_lon
            )
            
            # Estimate elevation angle from frequency (rough heuristic)
            # Higher frequencies tend to use higher elevation paths
            for f_mhz in result.group_delay_ms.keys():
                elevation = 30.0  # Default mid-elevation
                
                vtec, mf = self.vtec_mapper.stec_to_vtec(result.tec_u, elevation)
                uncertainty = max(1.0, result.tec_u * (1.0 - result.confidence))
                
                ipp_list.append(IPPMeasurement(
                    station=station,
                    frequency_mhz=f_mhz,
                    ipp_lat=ipp_lat,
                    ipp_lon=ipp_lon,
                    stec_tecu=result.tec_u,
                    vtec_tecu=vtec,
                    mapping_factor=mf,
                    elevation_deg=elevation,
                    uncertainty_tecu=uncertainty / mf,
                    propagation_mode=mode,
                ))
        
        return ipp_list

    def _write_physics_summary(
        self, 
        timestamp: int, 
        tec_estimates: Dict[str, TECResult],
        utc_consistent: bool
    ):
        """Write global L3 Physics Fusion product."""
        # Simple summary records for now (flattened for HDF5 compatibility)
        record = {
            'timestamp_utc': datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z'),
            'minute_boundary_utc': timestamp,
            'stations_used': ", ".join(sorted(set(k[0] for k in tec_estimates.keys()))),
            'utc_offset_ms': float('nan'), # Placeholder
            'utc_uncertainty_ms': float('nan'),
            'utc_consistency_flag': utc_consistent,
            'processing_version': '5.0.0',
            'processed_at': datetime.now(timezone.utc).isoformat()
        }
        
        try:
            self.l3_writer.write_measurement(record)
            logger.info(f"Written L3 physics summary for {timestamp}")
        except Exception as e:
            logger.error(f"Failed to write L3 physics summary: {e}")

    def _write_tec_records(
        self, 
        timestamp: int, 
        tec_estimates: Dict[str, TECResult]
    ):
        """Write individual station TEC records for L3A product."""
        ts_iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        
        for (station, mode), result in tec_estimates.items():
            # Follow l3_tec_v1.json schema
            record = {
                'timestamp_utc': ts_iso,
                'minute_boundary': timestamp,
                'station': station,
                'propagation_mode': mode,
                'tec_tecu': float(result.tec_u),
                't_vacuum_error_ms': float(result.t_vacuum_error_ms),
                'confidence': float(result.confidence),
                'n_frequencies': int(result.n_frequencies),
                'residuals_ms': float(result.residuals_ms),
                # Format frequencies as comma-separated list
                'frequencies_mhz': ",".join([f"{f/1e6:.2f}" for f in result.group_delay_ms.keys()]),
                'quality_flag': 'GOOD' if result.confidence > 0.8 else 'MARGINAL',
                'validation_flag': 'UNVALIDATED',
                'processing_version': '5.0.0'
            }
            
            try:
                self.tec_writer.write_measurement(record)
                logger.debug(f"Written TEC record for {station} at {timestamp}")
            except Exception as e:
                logger.error(f"Failed to write TEC record for {station}: {e}")
        
        if tec_estimates:
            logger.info(f"Written {len(tec_estimates)} TEC station records for {timestamp}")

    def run(self):
        """Main service loop."""
        self.running = True
        
        # Handle signals
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        # Notify systemd we're ready
        if SYSTEMD_AVAILABLE:
            systemd_daemon.notify('READY=1')
            logger.info("Systemd watchdog enabled")
        
        logger.info("Service started. Polling for data...")
        
        while self.running:
            try:
                # Notify systemd watchdog
                if SYSTEMD_AVAILABLE:
                    systemd_daemon.notify('WATCHDOG=1')
                
                # Align to next minute boundary processing
                now = time.time()
                # Process last few minutes to find enough frequencies for verification
                # L2 calibration service has ~1-2 minute lag, so we look back 2-5 minutes
                # We retry each minute up to 3 times to handle L2 write delays
                for offset in range(5, 1, -1):
                    target_minute = int(now) - (int(now) % 60) - (60 * offset)
                    retry_count = self._minute_retry_counts.get(target_minute, 0)
                    
                    if target_minute > self.last_processed_minute or retry_count < 3:
                        # Try to process this minute
                        station_data = self._read_l2_slice(target_minute)
                        if station_data:
                            self.process_minute(target_minute, station_data=station_data)
                            self.last_processed_minute = max(self.last_processed_minute, target_minute)
                            # Clear retry counter on success
                            self._minute_retry_counts.pop(target_minute, None)
                        else:
                            # Increment retry counter
                            self._minute_retry_counts[target_minute] = retry_count + 1
                            if retry_count == 0:
                                logger.debug(f"No L2 data for minute {target_minute}, will retry")

                self._prune_retry_counters(now)
                
                # Sleep until next poll or minute
                # We process once per minute, check every second for shutdown
                time.sleep(1.0)
                
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
