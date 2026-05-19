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
import math
import time
import argparse
import signal
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # Python < 3.11 fallback
    except ImportError:
        tomllib = None

from hf_timestd.core.tec_estimator import TECEstimator, TECResult
from hf_timestd.core.carrier_tec import CarrierTECEstimator
from hf_timestd.core.iono_tomography import IonoTomography, RayPath
from hf_timestd.core.vtec_mapper import VTECMapper, IPPMeasurement
from hf_timestd.io import DataProductReader, DataProductWriter, make_data_product_writer

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
        lookback_minutes: int = 5,
        receiver_lat: Optional[float] = None,
        receiver_lon: Optional[float] = None,
        gnss_vtec_dir: Optional[Path] = None,
        storage_config: Optional[Dict] = None,
    ):
        self.data_root = Path(data_root)
        self.output_dir = Path(output_dir)
        self.poll_interval = poll_interval
        self.lookback_minutes = lookback_minutes
        # [storage] config — drives HDF5 / SQLite / dual-write selection
        # in make_data_product_writer (HDF5→SQLite migration). None →
        # HDF5-only, preserving today's behaviour.
        self._storage_config = storage_config or {}

        # GNSS VTEC anchoring: path to HDF5 files written by live_vtec.py
        if gnss_vtec_dir is not None:
            self.gnss_vtec_dir = Path(gnss_vtec_dir)
        else:
            self.gnss_vtec_dir = self.data_root / 'data' / 'gnss_vtec'
        # _gnss_vtec_cache removed — _read_gnss_vtec() now does tail-reads
        # instead of caching the entire daily file in memory.

        # Receiver coordinates — used for IPP computation and elevation geometry.
        # Default to EM38ww (Columbia, MO) if not provided.
        self.receiver_lat = receiver_lat if receiver_lat is not None else 38.92
        self.receiver_lon = receiver_lon if receiver_lon is not None else -92.13
        logger.info(
            f"Receiver location: {self.receiver_lat:.4f}°N {self.receiver_lon:.4f}°E"
        )

        # Initialize TEC Estimator
        self.tec_estimator = TECEstimator()
        
        # Initialize carrier-phase dTEC estimator
        self.carrier_tec = CarrierTECEstimator(data_root=self.data_root)
        
        # Initialize E/F layer tomography
        self.tomography = IonoTomography()
        
        # Initialize VTEC mapper with actual receiver coordinates (P1-C fix)
        self.vtec_mapper = VTECMapper(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon,
        )
        self.ionex_dir = self.data_root / 'phase2' / 'ionex'
        self.ionex_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize L3 Writers
        self.l3_writer = make_data_product_writer(
            output_dir=self.output_dir,
            product_level='L3',
            product_name='physics',
            channel='global', # Global aggregate
            processing_version='5.0.0',
            station_metadata={'description': 'Physics-Based Fusion Service v5.0'},
            storage_config=self._storage_config,
        )
        
        # Second writer for individual station TEC records (consumed by Web API)
        # PropagationService looks in phase2/science/tec/AGGREGATED_tec_*.h5
        self.tec_dir = self.data_root / 'phase2' / 'science' / 'tec'
        self.tec_writer = make_data_product_writer(
            output_dir=self.tec_dir,
            product_level='L3', # Schema says L3A but product_level is used for schema lookup L3
            product_name='tec',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Physics-Based Fusion TEC Output'},
            storage_config=self._storage_config,
        )
        
        # Third writer for carrier-phase dTEC per-minute summary records
        self.dtec_dir = self.data_root / 'phase2' / 'science' / 'dtec'
        self.dtec_dir.mkdir(parents=True, exist_ok=True)
        self.dtec_writer = make_data_product_writer(
            output_dir=self.dtec_dir,
            product_level='L3',
            product_name='dtec',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Carrier-Phase dTEC Output'},
            storage_config=self._storage_config,
        )

        # Fourth writer for full per-tick dTEC time series (P3-B fix)
        # Preserves the ~1-second resolution carrier-phase data that the
        # per-minute summary discards.  Stored separately to avoid bloating
        # the summary HDF5 files.
        self.dtec_ts_dir = self.data_root / 'phase2' / 'science' / 'dtec_timeseries'
        self.dtec_ts_dir.mkdir(parents=True, exist_ok=True)
        self.dtec_ts_writer = make_data_product_writer(
            output_dir=self.dtec_ts_dir,
            product_level='L3',
            product_name='dtec_timeseries',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Carrier-Phase dTEC Full Time Series (~1s resolution)'},
            storage_config=self._storage_config,
        )

        # Fifth writer for per-minute differential dTEC frequency-pair records.
        # Each row is one (station, freq1, freq2, minute) tuple with RMS of
        # dTEC_f1 - dTEC_f2.  Near-zero RMS confirms ionospheric consistency
        # across frequencies; elevated RMS flags scintillation or mode changes.
        self.dtec_diff_dir = self.data_root / 'phase2' / 'science' / 'dtec_diff'
        self.dtec_diff_dir.mkdir(parents=True, exist_ok=True)
        self.dtec_diff_writer = make_data_product_writer(
            output_dir=self.dtec_diff_dir,
            product_level='L3',
            product_name='dtec_diff',
            channel='AGGREGATED',
            processing_version='5.0.0',
            station_metadata={'description': 'Differential dTEC frequency-pair RMS'},
            storage_config=self._storage_config,
        )
        
        # Tick-phase reader cache (separate from clock_offset readers)
        self._tick_phase_reader_cache: Dict[str, DataProductReader] = {}
        
        # State tracking
        self.running = False
        self.last_processed_minute = 0
        self.channels = self._discover_channels()
        self._reader_cache: Dict[str, DataProductReader] = {}
        self._minute_first_attempt: Dict[int, float] = {}  # minute_ts → first attempt epoch
        self._minute_last_attempt: Dict[int, float] = {}  # minute_ts → last attempt epoch
        self._processed_minutes: set = set()  # Minutes successfully processed — never re-process
        self._max_retry_history = 720  # Keep at most 12h of minute retry state
        self._retry_abandon_seconds = 300  # Abandon a minute after 5 min of failed attempts
        self._retry_cooldown_seconds = 10  # Wait 10s between retries for the same minute
        
        # Data freshness tracking for upstream starvation detection
        self.upstream_stale_warning_issued = False
        self.max_upstream_age_seconds = 300.0  # 5 minutes - warn if L2 data older than this

        # HDF5 write timeout (seconds).  If a write_measurement() call blocks
        # longer than this (typically due to file lock contention with
        # concurrent readers), we abandon that write rather than letting the
        # service hang until the systemd watchdog kills it.
        self._write_timeout_seconds = 30
        self._write_timeout_count = 0
        
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
        stale_minutes = [m for m in self._minute_first_attempt if m < cutoff]
        for minute in stale_minutes:
            self._minute_first_attempt.pop(minute, None)
            self._minute_last_attempt.pop(minute, None)

        if len(self._minute_first_attempt) > self._max_retry_history:
            for minute in sorted(self._minute_first_attempt)[:-self._max_retry_history]:
                self._minute_first_attempt.pop(minute, None)
                self._minute_last_attempt.pop(minute, None)

        # Prune _processed_minutes to the same 12h window
        self._processed_minutes = {m for m in self._processed_minutes if m >= cutoff}

    def _pet_watchdog(self):
        """Notify systemd watchdog.  Call this frequently during long processing."""
        if SYSTEMD_AVAILABLE:
            systemd_daemon.notify('WATCHDOG=1')

    def _timed_write(self, writer: 'DataProductWriter', record: dict, label: str = '') -> bool:
        """Write a measurement with a timeout to prevent file-lock hangs.

        Returns True if the write succeeded, False if it timed out or failed.
        On timeout, logs a warning and increments the timeout counter but does
        NOT raise — the caller can continue processing the next record.
        """
        import threading

        result = [None]  # [exception_or_None]

        def _do_write():
            try:
                writer.write_measurement(record)
            except Exception as e:
                result[0] = e

        t = threading.Thread(target=_do_write, daemon=True)
        t.start()
        t.join(timeout=self._write_timeout_seconds)

        if t.is_alive():
            self._write_timeout_count += 1
            logger.warning(
                f"HDF5 write timed out after {self._write_timeout_seconds}s "
                f"({label or 'unknown'}), total timeouts: {self._write_timeout_count}"
            )
            return False

        if result[0] is not None:
            logger.error(f"HDF5 write failed ({label}): {result[0]}")
            return False

        return True

    def _timed_write_batch(self, writer: 'DataProductWriter', records: list, label: str = '') -> bool:
        """Write a batch of measurements in a single HDF5 open/close cycle.

        Same timeout semantics as _timed_write but uses
        write_measurements_batch() — one open/append-N/close instead of N
        open/append-1/close cycles.  Critical for high-frequency products
        like dTEC timeseries (~55 rows/station/min × 9 channels).
        """
        if not records:
            return True

        import threading

        result = [None]

        def _do_write():
            try:
                writer.write_measurements_batch(records)
            except Exception as e:
                result[0] = e

        t = threading.Thread(target=_do_write, daemon=True)
        t.start()
        t.join(timeout=self._write_timeout_seconds)

        if t.is_alive():
            self._write_timeout_count += 1
            logger.warning(
                f"HDF5 batch write timed out after {self._write_timeout_seconds}s "
                f"({label or 'unknown'}, {len(records)} records), "
                f"total timeouts: {self._write_timeout_count}"
            )
            return False

        if result[0] is not None:
            logger.error(f"HDF5 batch write failed ({label}, {len(records)} records): {result[0]}")
            return False

        return True

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
                    # Key the aggregation by mode as well as (station,
                    # frequency): a mid-minute mode hop otherwise lets the
                    # median collapse two different geometric regimes into
                    # one toa, injecting a multi-ms step into the 1/f^2
                    # TEC fit (P-H26).
                    raw_by_station_freq[(station, freq_mhz, mode)].append({
                        'toa_ms': toa,
                        'uncertainty_ms': uncertainty,
                        'mode': mode,
                        'snr_db': snr,
                    })
                             
            except Exception as e:
                logger.warning(f"Failed to read channel {channel}: {e}")
                continue
        
        # Median-aggregate per (station, frequency, mode) — one measurement
        # per distinct frequency *and mode* per station, so the median is
        # never taken across a mode transition (P-H26).  ``mode`` is the
        # group key, so there is no separate (and previously disagreeing)
        # dominant-mode computation here.
        measurements_by_station: Dict[str, List[Dict]] = defaultdict(list)

        for (station, freq_mhz, mode), obs_list in raw_by_station_freq.items():
            toas = np.array([o['toa_ms'] for o in obs_list])
            median_toa = float(np.median(toas))
            # Use minimum uncertainty (best measurement)
            min_unc = min(o['uncertainty_ms'] for o in obs_list)
            # Best SNR
            best_snr = max(o.get('snr_db', 0.0) for o in obs_list)

            measurements_by_station[station].append({
                'frequency_hz': freq_mhz * 1e6,
                'toa_ms': median_toa,
                'uncertainty_ms': min_unc,
                'snr_db': best_snr,
                'mode': mode,
                'n_raw': len(obs_list),
            })
        
        return measurements_by_station

    def process_minute(self, minute_timestamp: int, station_data: Optional[Dict[tuple, List[Dict]]] = None) -> bool:
        """Process a single minute of data.  Returns True if the L3 summary was written."""
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
            return False

        self._pet_watchdog()

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
                # CR-2 (settled 2026-05-17, DATA_CONTRACT.md): a negative or
                # out-of-range tec_u is RETAINED, not discarded. Group-delay
                # TEC is below the noise floor, so a negative estimate is a
                # normal noisy realisation; discarding on value censors the
                # estimator and biases aggregates high. Flag it, keep it.
                if not (0.0 < result.tec_u <= 200.0):
                    logger.warning(
                        f"TEC out of nominal range for {station}: "
                        f"{result.tec_u:.2f} TECU — retained, flagged MARGINAL"
                    )

                result.propagation_mode = dominant_mode
                tec_estimates[(station, dominant_mode)] = result
                freq_list = ", ".join([f"{f/1e6:.1f}" for f in sorted(distinct_freqs)])
                logger.info(f"TEC {station}: {result.tec_u:.2f} TECU (Conf: {result.confidence:.2f}, N_freq={len(distinct_freqs)}, freqs=[{freq_list}] MHz)")
            else:
                 logger.debug(f"TEC estimation failed for {station}")

        self._pet_watchdog()

        # 3. E/F Layer Tomography
        tomo_result = None
        # Tomography is a bounded inverse problem; feed it only physically
        # plausible slant TEC. Negative / out-of-range values (retained in
        # tec_estimates per CR-2, DATA_CONTRACT.md) are excluded here — a
        # consumption-time guard, not a record-level rejection.
        physical_tec = {
            k: v for k, v in tec_estimates.items() if 0.0 < v.tec_u <= 200.0
        }
        if len(physical_tec) >= 2:
            try:
                paths = self.tomography.build_paths_from_tec_results(physical_tec)
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

        self._pet_watchdog()

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

        self._pet_watchdog()

        # 5. UTC Consistency Check
        utc_consistent = len(tec_estimates) > 0
        
        # 6. Write L3
        l3_ok = self._write_physics_summary(
            minute_timestamp,
            tec_estimates,
            utc_consistent
        )
        
        self._pet_watchdog()

        # 7. Write per-station TEC records
        self._write_tec_records(
            minute_timestamp,
            tec_estimates,
            vtec_by_station
        )

        self._pet_watchdog()

        # 8. Carrier-phase dTEC estimation
        self._process_carrier_dtec(minute_timestamp, tec_estimates)

        return l3_ok

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

            # Compute elevation angle geometrically from path distance and
            # F2 layer height (P1-C fix — replaces hardcoded 30°).
            # For a 1-hop path: elevation = atan(h / (d/2)) where d is the
            # great-circle distance and h is the virtual reflection height.
            gc_dist_km = self._great_circle_km(
                self.receiver_lat, self.receiver_lon, station_lat, station_lon
            )
            # n_hops from mode string (e.g. '2F2' -> 2, '1E' -> 1)
            n_hops = max(1, int(''.join(filter(str.isdigit, str(mode)[:2])) or '1'))
            h_km = 300.0  # F2 virtual height (km) — improved by P2-A later
            half_hop_km = gc_dist_km / (2 * n_hops)
            elevation_geometric = math.degrees(math.atan2(h_km, half_hop_km))
            elevation_geometric = max(5.0, min(85.0, elevation_geometric))

            for f_mhz in result.group_delay_ms.keys():
                elevation = elevation_geometric
                
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
    ) -> bool:
        """Write global L3 Physics Fusion product.  Returns True if write succeeded."""
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
        
        ok = self._timed_write(self.l3_writer, record, 'L3 physics summary')
        if ok:
            logger.info(f"Written L3 physics summary for {timestamp}")
        return ok

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
                'quality_flag': (
                    'GOOD' if result.confidence > 0.8
                    and 0.0 <= result.tec_u <= 200.0 else 'MARGINAL'
                ),
                'validation_flag': 'UNVALIDATED',
                'processing_version': '5.0.0'
            }
            
            self._timed_write(self.tec_writer, record, f'TEC {station}')
            self._pet_watchdog()
        
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
            logger.warning(f"Failed to create tick_phase reader for {channel}: {e}")
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
                logger.warning(f"Failed to read tick_phase for {channel}: {e}")
                continue

        return result

    def _read_gnss_vtec(self, epoch: float) -> Optional[float]:
        """
        Read the nearest GNSS overhead VTEC measurement for a given epoch.

        Returns VTEC in TECU, or None if no data is available within ±120s.
        Reads only the tail of the HDF5 file (last 300 rows ≈ 5 minutes at 1 Hz)
        to avoid loading the entire daily file (~368 MB) into memory.
        """
        import h5py

        target_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        date_str = target_dt.strftime('%Y%m%d')

        h5_path = self.gnss_vtec_dir / f'GNSS_gnss_vtec_{date_str}.h5'
        if not h5_path.exists():
            return None

        TAIL_ROWS = 300  # ~5 minutes at 1 Hz — covers ±120s search window

        try:
            with h5py.File(h5_path, 'r', libver='latest', swmr=True) as f:
                if 'unix_timestamp' not in f or 'vtec_tecu' not in f:
                    return None
                n = f['unix_timestamp'].shape[0]
                if n == 0:
                    return None
                start = max(0, n - TAIL_ROWS)
                timestamps = f['unix_timestamp'][start:].astype(np.float64)
                vtecs = f['vtec_tecu'][start:].astype(np.float64)
                # Optional quality gate
                if 'quality_flag' in f:
                    qflags = f['quality_flag'][start:]
                    good_mask = np.array([
                        (q == b'GOOD' or q == b'MARGINAL')
                        if isinstance(q, bytes) else (q in ('GOOD', 'MARGINAL'))
                        for q in qflags
                    ])
                    timestamps = timestamps[good_mask]
                    vtecs = vtecs[good_mask]
        except Exception as e:
            logger.warning(f"Failed to read GNSS VTEC from {h5_path}: {e}")
            return None

        if len(timestamps) == 0:
            return None

        # Find nearest measurement within ±120 seconds
        idx = np.searchsorted(timestamps, epoch)
        best_vtec = None
        best_dt = 121.0  # just above threshold
        for candidate in [idx - 1, idx]:
            if 0 <= candidate < len(timestamps):
                dt_sec = abs(timestamps[candidate] - epoch)
                if dt_sec < best_dt:
                    best_dt = dt_sec
                    best_vtec = float(vtecs[candidate])

        if best_vtec is not None and 0 < best_vtec <= 200:
            return best_vtec
        return None

    def _process_carrier_dtec(
        self,
        minute_timestamp: int,
        tec_estimates: Dict[tuple, 'TECResult']
    ):
        """
        Compute carrier-phase dTEC for each channel and anchor to GNSS VTEC
        (preferred) or group-delay TEC (fallback).

        Reads tick_phase data (carrier phase per tick, ~55/min), converts to
        dTEC via Doppler, integrates, and anchors to the absolute TEC from
        the local ZED-F9P GNSS receiver (overhead VTEC) or, if unavailable,
        from the group-delay 1/f² fit.
        """
        tick_data = self._read_tick_phase_minute(minute_timestamp)
        if not tick_data:
            return

        # --- Anchor source selection (priority order) ---
        # 1. GNSS overhead VTEC from local ZED-F9P (best: ~1 TECU accuracy)
        # 2. Group-delay TEC from HF 1/f² fit (poor: SNR ~0.13, rarely usable)
        # 3. No anchor (dTEC rate still valid as relative product)
        #
        # GNSS VTEC is overhead (zenith).  For dTEC anchoring we use it
        # directly as the DC level for all stations — the integrated
        # carrier-phase dTEC is a *relative* product, and the GNSS VTEC
        # provides a much better absolute scale than group-delay TEC.
        # A per-path slant correction could refine this further but the
        # mapping function error (~10-30%) is still far smaller than the
        # group-delay TEC noise floor.
        gnss_vtec = self._read_gnss_vtec(float(minute_timestamp + 30))  # mid-minute
        anchor_source = 'NONE'

        # Build anchor lookup: station -> TEC in TECU
        anchor_by_station: Dict[str, float] = {}

        if gnss_vtec is not None:
            # Use GNSS VTEC for all stations (overhead, station-independent)
            anchor_source = 'GNSS'
            for channel_records in tick_data.values():
                for r in channel_records:
                    st = r.get('station', '')
                    if isinstance(st, bytes):
                        st = st.decode('utf-8', errors='replace')
                    if st and st not in anchor_by_station:
                        anchor_by_station[st] = gnss_vtec
            logger.info(
                f"dTEC anchor: GNSS VTEC = {gnss_vtec:.1f} TECU "
                f"(applied to {len(anchor_by_station)} stations)"
            )
        else:
            # Fallback: group-delay TEC (rarely usable)
            ANCHOR_MIN_CONFIDENCE = 0.5
            for (station, mode), result in tec_estimates.items():
                if result.confidence >= ANCHOR_MIN_CONFIDENCE and 0 < result.tec_u <= 200:
                    anchor_by_station[station] = result.tec_u
                    anchor_source = 'GROUP_DELAY'
                elif result.confidence < ANCHOR_MIN_CONFIDENCE and 0 < result.tec_u <= 200:
                    logger.debug(
                        f"dTEC anchor suppressed for {station}: group-delay TEC confidence "
                        f"{result.confidence:.2f} < {ANCHOR_MIN_CONFIDENCE} threshold "
                        f"(TEC={result.tec_u:.1f} TECU). dTEC will be unanchored."
                    )
            if not anchor_by_station:
                anchor_source = 'NONE'

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

                # P3-A: Downgrade quality when phase unwrapping is ambiguous
                unwrap_q = getattr(dtec_result, 'unwrap_quality', 1.0)
                n_jumps = getattr(dtec_result, 'n_phase_jumps', 0)
                if unwrap_q < 0.8 and qflag == 'GOOD':
                    qflag = 'MARGINAL'
                elif unwrap_q < 0.5:
                    qflag = 'BAD'

                # anchor_status: human-readable reason for anchor state
                if dtec_result.is_anchored:
                    anchor_status = f'ANCHORED_{anchor_source}'
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
                    'unwrap_quality': unwrap_q,
                    'n_phase_jumps': n_jumps,
                    'quality_flag': qflag,
                    'processing_version': '5.0.0',
                }

                if self._timed_write(self.dtec_writer, record, f'dTEC {channel}/{station}'):
                    n_written += 1

                self._pet_watchdog()

                # P3-B: Write full per-tick time series so ~1-second resolution
                # is preserved for scintillation and TID analysis.
                # Batch all ticks into a single HDF5 open/close cycle to avoid
                # SSD thrashing and HDF5 heap fragmentation (~500 writes/min → ~1).
                try:
                    epochs = dtec_result.epochs
                    rates = dtec_result.dtec_rate_tecu_per_s
                    dtecs = dtec_result.dtec_tecu
                    ts_batch = []
                    for ep, rate, dtec_val in zip(epochs, rates, dtecs):
                        ts_batch.append({
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
                        })
                    self._timed_write_batch(
                        self.dtec_ts_writer, ts_batch,
                        f'dTEC_ts {channel}/{station}'
                    )
                    self._pet_watchdog()
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
                f"({n_anchored} station-channels anchored via {anchor_source})"
            )

        # P3-C: Differential carrier-phase TEC between frequency pairs.
        # For stations with multiple frequencies (WWV: 5, WWVH: 4, CHU: 3),
        # the inter-frequency differential removes common-mode errors (clock,
        # geometry) and isolates the dispersive ionospheric component.
        # This is the correct GNSS-style approach (analogous to L1-L2 combination).
        # Results are logged; HDF5 write will be added once validated.
        self._compute_differential_dtec_pairs(tick_data, minute_timestamp)

    def _compute_differential_dtec_pairs(
        self,
        tick_data: Dict[str, List[Dict]],
        minute_timestamp: int,
    ):
        """
        P3-C: Compute inter-frequency differential dTEC for multi-frequency stations.

        Groups tick_phase records by (station, frequency), computes per-frequency
        CarrierTECResult, then calls compute_differential_dtec() on all pairs.
        The differential removes common-mode errors and isolates ionospheric dispersion.
        """
        # Collect per-(station, freq) records across all channels
        by_station_freq: Dict[str, Dict[float, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        for channel, records in tick_data.items():
            for r in records:
                st = r.get('station', '')
                if isinstance(st, bytes):
                    st = st.decode('utf-8', errors='replace')
                freq = float(r.get('frequency_mhz', 0.0))
                if st and freq > 0:
                    by_station_freq[st][freq].append(r)

        for station, freq_records in by_station_freq.items():
            freqs = sorted(freq_records.keys())
            if len(freqs) < 2:
                continue  # Need at least 2 frequencies for a differential

            # Compute per-frequency CarrierTECResult
            freq_results: Dict[float, Any] = {}
            for freq_mhz, records in freq_records.items():
                if len(records) < 5:
                    continue
                try:
                    result = self.carrier_tec.compute_dtec_from_records(
                        records=records,
                        frequency_mhz=freq_mhz,
                        station=station,
                        channel='DIFFERENTIAL',
                    )
                    if result is not None and result.n_points >= 3:
                        freq_results[freq_mhz] = result
                except Exception as e:
                    logger.debug(f"Differential dTEC: per-freq compute failed {station}/{freq_mhz}: {e}")

            if len(freq_results) < 2:
                continue

            # Compute differential between all frequency pairs (lowest vs highest
            # gives the largest dispersive signal; log all pairs for now)
            sorted_freqs = sorted(freq_results.keys())
            n_pairs = 0
            best_diff = None  # lowest-highest pair (largest dispersive signal)
            ts_iso = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
            for i, f1 in enumerate(sorted_freqs):
                for f2 in sorted_freqs[i + 1:]:
                    try:
                        diff = self.carrier_tec.compute_differential_dtec(
                            freq_results[f1], freq_results[f2]
                        )
                        if diff is None:
                            continue
                        n_pairs += 1
                        n_pts = diff['n_points']
                        rms = diff['rms_diff_tecu']
                        mean_diff = float(np.mean(diff['dtec_diff_tecu']))
                        logger.debug(
                            f"Differential dTEC {station} {f1:.2f}-{f2:.2f} MHz: "
                            f"RMS={rms:.4f} TECU mean={mean_diff:.4f} TECU n={n_pts}"
                        )
                        # Track the widest-separation pair for INFO-level summary
                        if i == 0 and f2 == sorted_freqs[-1]:
                            best_diff = (f1, f2, diff, mean_diff)
                        # Determine quality flag
                        if n_pts >= 30 and rms < 0.5:
                            qflag = 'GOOD'
                        elif n_pts >= 10 and rms < 2.0:
                            qflag = 'MARGINAL'
                        else:
                            qflag = 'BAD'
                        # Write to HDF5
                        diff_record = {
                            'timestamp_utc': ts_iso,
                            'minute_boundary': minute_timestamp,
                            'station': station,
                            'freq1_mhz': f1,
                            'freq2_mhz': f2,
                            'rms_diff_tecu': rms,
                            'mean_diff_tecu': mean_diff,
                            'n_points': n_pts,
                            'quality_flag': qflag,
                            'processing_version': '5.0.0',
                        }
                        self._timed_write(self.dtec_diff_writer, diff_record, f'dtec_diff {station} {f1}/{f2}')
                        self._pet_watchdog()
                    except Exception as e:
                        logger.debug(f"Differential dTEC pair {station} {f1}/{f2}: {e}")

            if n_pairs > 0:
                info_parts = [
                    f"Differential dTEC {station}: {n_pairs} pair(s) "
                    f"from {len(freq_results)} freqs {[f'{f:.2f}' for f in sorted_freqs]} MHz"
                ]
                if best_diff is not None:
                    f1, f2, d, mean_d = best_diff
                    info_parts.append(
                        f"  widest pair {f1:.2f}-{f2:.2f} MHz: "
                        f"RMS={d['rms_diff_tecu']:.4f} TECU mean={mean_d:.4f} TECU n={d['n_points']}"
                    )
                logger.info("\n".join(info_parts))

    def _seed_processed_minutes_from_l3(self) -> None:
        """Seed ``_processed_minutes`` from minutes already written to the
        L3 dtec output (P-H25).

        ``_processed_minutes`` is an in-memory set, so on every restart it
        is empty; the startup lookback window then reprocesses minutes
        whose L3 records already exist, producing duplicate TEC/dTEC/L3
        records (the contract forbids duplicate records).  Reading the
        ``minute_boundary`` dataset of the recent L3 dtec files back into
        the set makes the loop's ``target_minute in self._processed_minutes``
        guard effective across a restart.  The set is pruned to a 12 h
        window elsewhere, so scanning the few most-recent day files is
        sufficient.
        """
        import h5py
        dtec_dir = self.data_root / 'phase2' / 'science' / 'dtec'
        if not dtec_dir.exists():
            return
        seeded = 0
        for l3_path in sorted(
            dtec_dir.glob('AGGREGATED_dtec_????????.h5'), reverse=True,
        )[:3]:
            try:
                with h5py.File(str(l3_path), 'r', swmr=True) as f:
                    if 'minute_boundary' not in f or len(f['minute_boundary']) == 0:
                        continue
                    for raw in f['minute_boundary'][:]:
                        # Normalise to a minute-aligned epoch second so the
                        # value matches the loop's target_minute exactly.
                        m = int(raw)
                        m -= m % 60
                        if m not in self._processed_minutes:
                            self._processed_minutes.add(m)
                            seeded += 1
            except Exception as e:
                logger.debug(f"L3 seed scan failed for {l3_path}: {e}")
        if seeded:
            logger.info(
                f"Seeded {seeded} already-processed minute(s) from L3 "
                f"output — restart will not reprocess them"
            )

    def _startup_lookback_minutes(self) -> int:
        """Return how many minutes back to scan on startup.

        Reads the last written timestamp from the L3 dtec output file for
        today (and yesterday as a fallback) to find where processing stopped.
        Falls back to L2 file mtime if no L3 output exists yet.
        Capped at 24 hours.
        """
        import h5py

        last_written_ts = 0.0

        # Scan L3 dtec files newest-first to find the oldest stale file.
        # We want the OLDEST last-timestamp that is more than 5 minutes behind
        # now — that is where backfill must start.  Scanning newest-first and
        # stopping at the first file whose last timestamp is recent (≤5 min old)
        # handles the UTC-midnight-crossing case correctly: if physics crashed
        # partway through March 7 UTC and a March 8 UTC file already exists
        # (because metrology kept running), we must resume from March 7's last
        # written minute, not March 8's.
        dtec_dir = self.data_root / 'phase2' / 'science' / 'dtec'
        if dtec_dir.exists():
            for l3_path in sorted(dtec_dir.glob('AGGREGATED_dtec_????????.h5'), reverse=True):
                try:
                    with h5py.File(str(l3_path), 'r', swmr=True) as f:
                        # minute_boundary is epoch float; timestamp_utc is ISO string
                        for key in ('minute_boundary', 'timestamp_utc'):
                            if key not in f or len(f[key]) == 0:
                                continue
                            raw = f[key][-1]
                            if isinstance(raw, (bytes, str)):
                                raw_s = raw.decode() if isinstance(raw, bytes) else raw
                                ts = datetime.fromisoformat(
                                    raw_s.replace('Z', '+00:00')
                                ).timestamp()
                            else:
                                ts = float(raw)
                            # If this file is current (≤5 min old), skip it and
                            # keep looking at older files for a gap.
                            if time.time() - ts <= 5 * 60:
                                ts = 0.0
                            if ts > last_written_ts:
                                last_written_ts = ts
                            break
                except Exception:
                    pass
            # If every file was current, last_written_ts stays 0 → returns 5 below

        if last_written_ts == 0.0:
            # No L3 output yet — fall back to L2 file mtime
            newest_mtime = 0.0
            for channel in self.channels:
                l2_dir = self.data_root / 'phase2' / channel / 'clock_offset'
                if l2_dir.exists():
                    for h5_file in l2_dir.glob("*.h5"):
                        try:
                            mtime = h5_file.stat().st_mtime
                            if mtime > newest_mtime:
                                newest_mtime = mtime
                        except OSError:
                            pass
            last_written_ts = newest_mtime

        if last_written_ts == 0.0:
            return 5

        gap_minutes = int((time.time() - last_written_ts) / 60) + 10
        if gap_minutes <= 5:
            return 5

        # Cap to 30 minutes.  Physics/fusion is a real-time service: spending
        # startup time backfilling hours of old data delays chrony SHM updates
        # and can cascade into system-clock drift.  Missed history can be
        # reprocessed offline if needed.
        MAX_STARTUP_LOOKBACK = 30
        capped = min(gap_minutes, MAX_STARTUP_LOOKBACK)
        last_dt = datetime.fromtimestamp(last_written_ts, tz=timezone.utc).strftime('%H:%M:%SZ')
        if gap_minutes > MAX_STARTUP_LOOKBACK:
            logger.warning(
                f"Startup: last L3 output at {last_dt}, gap is ~{gap_minutes} minutes — "
                f"capping lookback to {MAX_STARTUP_LOOKBACK} min (real-time priority)"
            )
        else:
            logger.info(
                f"Startup: last L3 output at {last_dt}, gap is ~{gap_minutes} minutes — "
                f"extending lookback to {capped} minutes to backfill"
            )
        return capped

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

        # On restart after a gap (crash, update, etc.) the standard 5-minute
        # lookback window misses all minutes between the last processed minute
        # and now.  Compute a wider initial window that shrinks to 5 once we
        # have caught up to real-time.  Seed _processed_minutes from the L3
        # output first so the wider window does not reprocess (and duplicate)
        # minutes already written before the restart (P-H25).
        self._seed_processed_minutes_from_l3()
        max_lookback = self._startup_lookback_minutes()
        
        while self.running:
            try:
                # Notify systemd watchdog
                if SYSTEMD_AVAILABLE:
                    systemd_daemon.notify('WATCHDOG=1')
                
                # Align to next minute boundary processing
                now = time.time()
                # Process last few minutes to find enough frequencies for verification
                # L2 calibration service has ~1-2 minute lag, so we look back 2-5 minutes
                # We retry each minute up to 3 times to handle L2 write delays.
                # max_lookback starts wide on restart (to cover any gap) and
                # shrinks to 5 once we have caught up to within 5 minutes of now.
                if max_lookback > 5:
                    oldest_target = int(now) - (int(now) % 60) - (60 * max_lookback)
                    if self.last_processed_minute >= oldest_target:
                        max_lookback = max(5, max_lookback - 1)
                for offset in range(max_lookback, 1, -1):
                    target_minute = int(now) - (int(now) % 60) - (60 * offset)

                    # Never re-process a minute that already succeeded
                    if target_minute in self._processed_minutes:
                        continue

                    # Time-based retry: abandon after _retry_abandon_seconds,
                    # cooldown between attempts to avoid hammering the reader.
                    first_attempt = self._minute_first_attempt.get(target_minute)
                    if first_attempt is not None and (now - first_attempt) > self._retry_abandon_seconds:
                        continue  # Abandoned — past retry window
                    last_attempt = self._minute_last_attempt.get(target_minute, 0.0)
                    if (now - last_attempt) < self._retry_cooldown_seconds:
                        continue  # Cooling down — skip this pass

                    # Try to process this minute
                    self._pet_watchdog()
                    station_data = self._read_l2_slice(target_minute)
                    self._pet_watchdog()
                    if station_data:
                        ok = self.process_minute(target_minute, station_data=station_data)
                        if ok:
                            self.last_processed_minute = max(self.last_processed_minute, target_minute)
                            self._processed_minutes.add(target_minute)
                            self._minute_first_attempt.pop(target_minute, None)
                            self._minute_last_attempt.pop(target_minute, None)
                        else:
                            if first_attempt is None:
                                self._minute_first_attempt[target_minute] = now
                            self._minute_last_attempt[target_minute] = now
                            logger.warning(f"L3 write failed for minute {target_minute}, will retry")
                    else:
                        if first_attempt is None:
                            self._minute_first_attempt[target_minute] = now
                            logger.debug(f"No L2 data for minute {target_minute}, will retry for up to {self._retry_abandon_seconds}s")
                        self._minute_last_attempt[target_minute] = now

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

    @staticmethod
    def _great_circle_km(
        lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Haversine great-circle distance in km."""
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _load_receiver_coords(config_path: str) -> tuple:
    """
    Read receiver lat/lon from the timestd config toml.
    Returns (lat, lon) or (None, None) if unavailable.
    """
    if tomllib is None:
        return None, None
    try:
        with open(config_path, 'rb') as f:
            cfg = tomllib.load(f)
        station = cfg.get('station', {})
        lat = station.get('latitude')
        lon = station.get('longitude')
        if lat is not None and lon is not None:
            return float(lat), float(lon)
    except Exception as e:
        logger.warning(f"Could not read receiver coords from {config_path}: {e}")
    return None, None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Physics-Based Fusion Service')
    parser.add_argument('--data-root', default='/var/lib/timestd', help='Data root directory')
    parser.add_argument('--output', default='/var/lib/timestd/phase2/fusion', help='Output directory')
    parser.add_argument('--config', default='/etc/hf-timestd/timestd-config.toml',
                        help='Path to timestd-config.toml')
    parser.add_argument('--receiver-lat', type=float, default=None,
                        help='Receiver latitude (overrides config toml)')
    parser.add_argument('--receiver-lon', type=float, default=None,
                        help='Receiver longitude (overrides config toml)')

    args = parser.parse_args()

    # Resolve receiver coordinates: CLI > config toml > built-in default
    rx_lat, rx_lon = args.receiver_lat, args.receiver_lon
    if rx_lat is None or rx_lon is None:
        rx_lat, rx_lon = _load_receiver_coords(args.config)

    # Load GNSS VTEC path and [storage] config from the config file.
    gnss_vtec_dir = None
    storage_config = {}
    if tomllib is not None:
        try:
            with open(args.config, 'rb') as _f:
                _cfg = tomllib.load(_f)
            storage_config = _cfg.get('storage', {}) or {}
            gnss_cfg = _cfg.get('gnss_vtec', {})
            if gnss_cfg.get('enabled', False):
                hdf5_rel = gnss_cfg.get('hdf5_path')
                if hdf5_rel:
                    p = Path(hdf5_rel)
                    # Resolve relative paths against data_root
                    gnss_vtec_dir = p if p.is_absolute() else Path(args.data_root) / p
        except Exception as e:
            logger.warning(f"Could not read gnss_vtec config: {e}")

    service = PhysicsFusionService(
        data_root=args.data_root,
        output_dir=args.output,
        receiver_lat=rx_lat,
        receiver_lon=rx_lon,
        gnss_vtec_dir=gnss_vtec_dir,
        storage_config=storage_config,
    )

    service.run()
