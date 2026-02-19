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
        
        # Third writer for carrier-phase dTEC per-minute summary records
        self.dtec_dir = self.data_root / 'phase2' / 'science' / 'dtec'
        self.dtec_dir.mkdir(parents=True, exist_ok=True)
        self.dtec_writer = DataProductWriter(
            output_dir=self.dtec_dir,
            product_level='L3',
            product_name='dtec',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Carrier-Phase dTEC Output'}
        )

        # Fourth writer for full per-tick dTEC time series (P3-B fix)
        # Preserves the ~1-second resolution carrier-phase data that the
        # per-minute summary discards.  Stored separately to avoid bloating
        # the summary HDF5 files.
        self.dtec_ts_dir = self.data_root / 'phase2' / 'science' / 'dtec_timeseries'
        self.dtec_ts_dir.mkdir(parents=True, exist_ok=True)
        self.dtec_ts_writer = DataProductWriter(
            output_dir=self.dtec_ts_dir,
            product_level='L3',
            product_name='dtec_timeseries',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Carrier-Phase dTEC Full Time Series (~1s resolution)'}
        )
        
        # Tick-phase reader cache (separate from clock_offset readers)
        self._tick_phase_reader_cache: Dict[str, DataProductReader] = {}
        
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
        
    def _read_l2_slice(self, minute_timestamp: int) -> Dict[str, List[Dict]]:
        """
        Read L2 measurements for a specific minute across all channels.
        
        Returns:
            Dict mapping station -> List of measurements (all modes combined).
            Each measurement has frequency_hz, toa_ms, uncertainty_ms, snr_db, mode.
            Multiple same-frequency measurements are median-aggregated.
        """
        start_iso = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        end_iso = datetime.fromtimestamp(minute_timestamp + 59.999, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
        
        # Collect raw measurements grouped by (station, frequency_mhz)
        raw_by_station_freq: Dict[tuple, List[Dict]] = defaultdict(list)
        
        for channel in self.channels:
            try:
                reader = self._get_reader(channel)

                items = reader.read_time_range(
                    start=start_iso, 
                    end=end_iso
                )
                
                for item in items:
                    station = item.get('station')
                    if not station:
                        continue
                        
                    if 'frequency_mhz' not in item:
                        continue

                    # Resolve D_clock residual: prefer Kalman if available,
                    # fallback to clock_offset_ms (= D_clock, the timing residual
                    # after subtracting the propagation model delay).  Do NOT use
                    # raw_arrival_time_ms here — that is an absolute ToA and its
                    # intercept is dominated by the geometric delay, not TEC.
                    toa = item.get('tof_kalman_ms')
                    if toa is None or np.isnan(toa):
                        toa = item.get('clock_offset_ms')
                        
                    uncertainty = item.get('tof_uncertainty_ms')
                    if uncertainty is None or np.isnan(uncertainty):
                        uncertainty = item.get('uncertainty_ms', 10.0)

                    if toa is None or np.isnan(toa):
                        continue

                    mode = item.get('propagation_mode', 'UNKNOWN')
                    snr = item.get('snr_db', 0.0)

                    freq_mhz = float(item['frequency_mhz'])
                    raw_by_station_freq[(station, freq_mhz)].append({
                        'toa_ms': toa,
                        'uncertainty_ms': uncertainty,
                        'mode': mode,
                        'snr_db': snr,
                    })
                             
            except Exception as e:
                logger.warning(f"Failed to read channel {channel}: {e}")
                continue
        
        # Median-aggregate per (station, frequency) to produce one measurement
        # per distinct frequency per station
        measurements_by_station: Dict[str, List[Dict]] = defaultdict(list)
        
        for (station, freq_mhz), obs_list in raw_by_station_freq.items():
            toas = np.array([o['toa_ms'] for o in obs_list])
            median_toa = float(np.median(toas))
            # Use minimum uncertainty (best measurement)
            min_unc = min(o['uncertainty_ms'] for o in obs_list)
            # Best SNR
            best_snr = max(o.get('snr_db', 0.0) for o in obs_list)
            # Dominant mode: use the mode from the highest-SNR observation.
            # Mode-by-count is unreliable when different channels disagree;
            # the highest-SNR measurement is the most trustworthy single source.
            best_obs = max(obs_list, key=lambda o: o.get('snr_db', 0.0))
            dominant_mode = best_obs['mode']
            
            measurements_by_station[station].append({
                'frequency_hz': freq_mhz * 1e6,
                'toa_ms': median_toa,
                'uncertainty_ms': min_unc,
                'snr_db': best_snr,
                'mode': dominant_mode,
                'n_raw': len(obs_list),
            })
        
        return measurements_by_station

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
        # station_data is now Dict[station, List[measurements]] with one entry per distinct frequency
        tec_estimates = {}
        
        for station, observations in station_data.items():
            # Need at least 2 distinct frequencies for TEC
            distinct_freqs = set(o['frequency_hz'] for o in observations)
            if len(distinct_freqs) < 2:
                logger.debug(f"Station {station}: insufficient distinct frequencies ({len(distinct_freqs)}: {[f/1e6 for f in sorted(distinct_freqs)]})")
                continue
            
            # Determine dominant mode for metadata
            mode_counts: Dict[str, int] = defaultdict(int)
            for o in observations:
                mode_counts[o.get('mode', 'UNKNOWN')] += 1
            dominant_mode = max(mode_counts, key=mode_counts.get)
                
            result = self.tec_estimator.estimate_tec(observations, station, minute_timestamp)
            
            if result:
                if not (0.0 < result.tec_u <= 200.0):
                    logger.warning(
                        f"TEC out of bounds for {station}: {result.tec_u:.2f} TECU - skipping"
                    )
                    continue

                result.propagation_mode = dominant_mode
                tec_estimates[(station, dominant_mode)] = result
                freq_list = ", ".join([f"{f/1e6:.1f}" for f in sorted(distinct_freqs)])
                logger.info(f"TEC {station}: {result.tec_u:.2f} TECU (Conf: {result.confidence:.2f}, N_freq={len(distinct_freqs)}, freqs=[{freq_list}] MHz)")
            else:
                 logger.debug(f"TEC estimation failed for {station}")

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
        # VTEC requires credible group-delay TEC estimates (confidence >= 0.3).
        # In production the group-delay TEC is below the noise floor (F1 in
        # CRITIC_CONTEXT), so ipp_measurements is always empty and vtec_tecu is
        # always NaN.  Log this explicitly rather than silently writing NaN.
        vtec_result = None
        ipp_measurements = self._build_ipp_measurements(tec_estimates)
        vtec_by_station: Dict[str, float] = {}

        if not ipp_measurements:
            n_low_conf = sum(
                1 for r in tec_estimates.values() if r.confidence < 0.3
            )
            if n_low_conf > 0:
                logger.debug(
                    f"VTEC unavailable: {n_low_conf} station(s) have group-delay TEC "
                    f"confidence < 0.3 (propagation model noise floor). "
                    f"vtec_tecu will be NaN in TEC records. Fix: improve propagation model (P2-A)."
                )
        else:
            # Build per-station vtec lookup (median vtec across frequencies per station)
            for ipm in ipp_measurements:
                station_key = ipm.station
                if station_key not in vtec_by_station:
                    vtec_by_station[station_key] = []
                vtec_by_station[station_key].append(ipm.vtec_tecu)
            vtec_by_station = {k: float(np.median(v)) for k, v in vtec_by_station.items()}

            if len(ipp_measurements) >= 3:
                try:
                    vtec_result = self.vtec_mapper.generate_map(
                        ipp_measurements, timestamp=float(minute_timestamp)
                    )
                    if vtec_result:
                        logger.info(
                            f"VTEC map: {vtec_result.n_ipps} IPPs, "
                            f"RMS={vtec_result.rms_residual_tecu:.2f} TECU, "
                            f"conf={vtec_result.confidence:.2f}"
                        )
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
            tec_estimates,
            vtec_by_station
        )

        # 8. Carrier-phase dTEC estimation
        self._process_carrier_dtec(minute_timestamp, tec_estimates)

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
        # utc_offset_ms: median of t_vacuum_error_ms across all TEC estimates.
        # t_vacuum_error_ms is the TEC-fit intercept — the ionosphere-free D_clock,
        # i.e. the residual timing error after removing the dispersive ionospheric
        # component.  It is the best available UTC offset estimate from this pipeline.
        vacuum_errors = [r.t_vacuum_error_ms for r in tec_estimates.values()
                         if np.isfinite(r.t_vacuum_error_ms)]
        utc_offset = float(np.median(vacuum_errors)) if vacuum_errors else float('nan')
        # Uncertainty: MAD-based robust spread, converted to 1-sigma equivalent
        utc_unc = float(np.median(np.abs(np.array(vacuum_errors) - utc_offset)) * 1.4826) \
            if len(vacuum_errors) > 1 else float('nan')

        record = {
            'timestamp_utc': datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z'),
            'minute_boundary_utc': timestamp,
            'stations_used': ", ".join(sorted(set(k[0] for k in tec_estimates.keys()))),
            'utc_offset_ms': utc_offset,
            'utc_uncertainty_ms': utc_unc,
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
        tec_estimates: Dict[str, TECResult],
        vtec_by_station: Optional[Dict[str, float]] = None,
    ):
        """Write individual station TEC records for L3A product."""
        ts_iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        if vtec_by_station is None:
            vtec_by_station = {}

        for (station, mode), result in tec_estimates.items():
            # Follow l3_tec_v1.json schema
            record = {
                'timestamp_utc': ts_iso,
                'minute_boundary': timestamp,
                'station': station,
                'propagation_mode': mode,
                'tec_tecu': float(result.tec_u),
                'vtec_tecu': float(vtec_by_station.get(station, float('nan'))),
                't_vacuum_error_ms': float(result.t_vacuum_error_ms),
                'confidence': float(result.confidence),
                'n_frequencies': int(result.n_frequencies),
                'residuals_ms': float(result.residuals_ms),
                # Format frequencies as comma-separated list
                'frequencies_mhz': ",".join([f"{f:.2f}" for f in result.group_delay_ms.keys()]),
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

    def _get_tick_phase_reader(self, channel: str) -> Optional[DataProductReader]:
        """Get (or create) a cached reader for tick_phase data."""
        reader = self._tick_phase_reader_cache.get(channel)
        if reader is not None:
            return reader

        tp_dir = self.data_root / 'phase2' / channel / 'tick_phase'
        if not tp_dir.exists():
            return None

        try:
            reader = DataProductReader(
                data_dir=tp_dir,
                product_level='L2',
                product_name='tick_phase',
                channel=channel,
                use_registry=False
            )
            self._tick_phase_reader_cache[channel] = reader
            return reader
        except Exception as e:
            logger.debug(f"Failed to create tick_phase reader for {channel}: {e}")
            return None

    def _read_tick_phase_minute(
        self,
        minute_timestamp: int
    ) -> Dict[str, List[Dict]]:
        """
        Read tick_phase data for a specific minute across all channels.

        Returns:
            Dict mapping channel -> List of tick records with keys:
                utc_epoch, carrier_phase_rad, snr_db, station, frequency_mhz
        """
        result: Dict[str, List[Dict]] = {}

        for channel in self.channels:
            reader = self._get_tick_phase_reader(channel)
            if reader is None:
                continue

            try:
                start_iso = datetime.fromtimestamp(
                    minute_timestamp, tz=timezone.utc
                ).isoformat().replace('+00:00', 'Z')
                end_iso = datetime.fromtimestamp(
                    minute_timestamp + 59.999, tz=timezone.utc
                ).isoformat().replace('+00:00', 'Z')

                items = reader.read_time_range(start=start_iso, end=end_iso)
                if not items:
                    continue

                records = []
                for item in items:
                    mb = item.get('minute_boundary_utc')
                    wc = item.get('window_center_second')
                    cp = item.get('carrier_phase_rad')

                    if mb is None or wc is None or cp is None:
                        continue

                    # Epoch = minute boundary + tick position within minute
                    try:
                        epoch = float(mb) + float(wc)
                    except (TypeError, ValueError):
                        continue

                    if not np.isfinite(cp):
                        continue

                    records.append({
                        'utc_epoch': epoch,
                        'carrier_phase_rad': float(cp),
                        'snr_db': float(item.get('snr_db', 0.0)),
                        'station': item.get('station', ''),
                        'frequency_mhz': float(item.get('frequency_mhz', 0.0)),
                    })

                if records:
                    result[channel] = records

            except Exception as e:
                logger.debug(f"Failed to read tick_phase for {channel}: {e}")
                continue

        return result

    def _process_carrier_dtec(
        self,
        minute_timestamp: int,
        tec_estimates: Dict[tuple, 'TECResult']
    ):
        """
        Compute carrier-phase dTEC for each channel and anchor to group-delay TEC.

        Reads tick_phase data (carrier phase per tick, ~55/min), converts to
        dTEC via Doppler, integrates, and anchors to the absolute TEC from
        the group-delay 1/f² fit.
        """
        tick_data = self._read_tick_phase_minute(minute_timestamp)
        if not tick_data:
            return

        # Build anchor lookup: station -> TEC in TECU (from group-delay fit).
        # Only anchor when the group-delay TEC estimate is credible.
        # confidence < 0.5 means the 1/f² fit is dominated by noise (SNR ~0.01-0.14
        # per CRITIC_CONTEXT F1); anchoring to it would inject a large DC bias into
        # the carrier-phase dTEC series.  Unanchored dTEC (is_anchored=False) is
        # still scientifically valid as a relative rate-of-change product.
        ANCHOR_MIN_CONFIDENCE = 0.5
        anchor_by_station: Dict[str, float] = {}
        for (station, mode), result in tec_estimates.items():
            if result.confidence >= ANCHOR_MIN_CONFIDENCE and 0 < result.tec_u <= 200:
                anchor_by_station[station] = result.tec_u
            elif result.confidence < ANCHOR_MIN_CONFIDENCE and 0 < result.tec_u <= 200:
                logger.debug(
                    f"dTEC anchor suppressed for {station}: group-delay TEC confidence "
                    f"{result.confidence:.2f} < {ANCHOR_MIN_CONFIDENCE} threshold "
                    f"(TEC={result.tec_u:.1f} TECU). dTEC will be unanchored."
                )

        ts_iso = datetime.fromtimestamp(
            minute_timestamp, tz=timezone.utc
        ).isoformat()
        n_written = 0

        for channel, records in tick_data.items():
            if len(records) < 5:
                continue

            # Shared channels contain multiple stations — group by station
            by_station: Dict[str, List[Dict]] = defaultdict(list)
            for r in records:
                st = r.get('station', '')
                if isinstance(st, bytes):
                    st = st.decode('utf-8', errors='replace')
                if st:
                    by_station[st].append(r)

            for station, station_records in by_station.items():
                if len(station_records) < 5:
                    continue

                freq_mhz = station_records[0]['frequency_mhz']
                if freq_mhz <= 0:
                    continue

                # Get anchor TEC for this station (if available from group-delay)
                anchor_tec = anchor_by_station.get(station)
                anchor_epoch = float(minute_timestamp + 30) if anchor_tec else None

                try:
                    dtec_result = self.carrier_tec.compute_dtec_from_records(
                        records=station_records,
                        frequency_mhz=freq_mhz,
                        station=station,
                        channel=channel,
                        anchor_tec_tecu=anchor_tec,
                        anchor_epoch=anchor_epoch,
                    )
                except Exception as e:
                    logger.debug(f"dTEC computation failed for {channel}/{station}: {e}")
                    continue

                if dtec_result is None or dtec_result.n_points < 3:
                    continue

                # Compute summary statistics for the minute
                dtec_arr = np.array(dtec_result.dtec_tecu)
                rate_arr = np.array(dtec_result.dtec_rate_tecu_per_s)

                dtec_mean = float(np.mean(dtec_arr))
                dtec_std = float(np.std(dtec_arr))
                rate_mean = float(np.mean(rate_arr))

                # Quality flag.
                # Unanchored dTEC is capped at MARGINAL: dtec_mean_tecu has no
                # absolute reference and drifts freely (P1-B in physics review).
                # Only dtec_rate_tecu_per_s is reliable when unanchored.
                if dtec_result.n_points >= 30 and dtec_result.mean_snr_db >= 15:
                    qflag = 'GOOD'
                elif dtec_result.n_points >= 10 and dtec_result.mean_snr_db >= 8:
                    qflag = 'MARGINAL'
                else:
                    qflag = 'BAD'
                if not dtec_result.is_anchored and qflag == 'GOOD':
                    qflag = 'MARGINAL'  # Unanchored: integrated TEC is relative only

                # anchor_status: human-readable reason for anchor state
                if dtec_result.is_anchored:
                    anchor_status = 'ANCHORED'
                elif anchor_tec is not None:
                    anchor_status = 'ANCHOR_LOW_CONF'
                else:
                    anchor_status = 'NO_ANCHOR'

                record = {
                    'timestamp_utc': ts_iso,
                    'minute_boundary': minute_timestamp,
                    'station': station,
                    'channel': channel,
                    'frequency_mhz': freq_mhz,
                    'n_ticks': dtec_result.n_points,
                    'dtec_mean_tecu': dtec_mean,
                    'dtec_std_tecu': dtec_std,
                    'dtec_rate_tecu_per_s': rate_mean,
                    'anchor_tec_tecu': anchor_tec if anchor_tec else float('nan'),
                    'is_anchored': dtec_result.is_anchored,
                    'anchor_status': anchor_status,
                    'sigma_noise_tecu': dtec_result.sigma_dtec_tecu,
                    'mean_snr_db': dtec_result.mean_snr_db,
                    'quality_flag': qflag,
                    'processing_version': '5.0.0',
                }

                try:
                    self.dtec_writer.write_measurement(record)
                    n_written += 1
                except Exception as e:
                    logger.error(f"Failed to write dTEC record for {channel}/{station}: {e}")

                # P3-B: Write full per-tick time series so ~1-second resolution
                # is preserved for scintillation and TID analysis.
                # Each tick is one record; epochs are absolute UTC seconds.
                try:
                    epochs = dtec_result.epochs
                    rates = dtec_result.dtec_rate_tecu_per_s
                    dtecs = dtec_result.dtec_tecu
                    for i, (ep, rate, dtec_val) in enumerate(zip(epochs, rates, dtecs)):
                        ts_record = {
                            'timestamp_utc': datetime.fromtimestamp(
                                ep, tz=timezone.utc
                            ).isoformat(),
                            'epoch': float(ep),
                            'minute_boundary': minute_timestamp,
                            'station': station,
                            'channel': channel,
                            'frequency_mhz': freq_mhz,
                            'dtec_rate_tecu_per_s': float(rate),
                            'dtec_tecu': float(dtec_val),
                            'is_anchored': dtec_result.is_anchored,
                            'anchor_status': anchor_status,
                            'snr_db': float(dtec_result.mean_snr_db),
                            'processing_version': '5.0.0',
                        }
                        self.dtec_ts_writer.write_measurement(ts_record)
                except Exception as e:
                    logger.debug(f"Failed to write dTEC time series for {channel}/{station}: {e}")

        if n_written > 0:
            n_anchored = sum(
                1 for ch_records in tick_data.values()
                for st in set(r.get('station', '') for r in ch_records)
                if st in anchor_by_station
            )
            logger.info(
                f"Written {n_written} carrier-phase dTEC records for {minute_timestamp} "
                f"({n_anchored} station-channels anchored to group-delay TEC)"
            )

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
