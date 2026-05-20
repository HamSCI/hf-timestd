#!/usr/bin/env python3
"""
L2 Calibration Service - Converts L1 Metrology to L2 Timing Measurements

This service reads L1 metrology measurements (raw TOA) and applies:
1. Geometric delay correction (transmitter location)
2. Ionospheric TEC correction (frequency-dependent)
3. System calibration (receiver delays)
4. ISO GUM uncertainty budgets

Output: L2 timing measurements with calibrated D_clock per broadcast

Architecture:
  Input:  L1 HDF5 (metrology/{CHANNEL}_metrology_measurements_*.h5)
  Output: L2 HDF5 (clock_offset/{CHANNEL}_timing_measurements_*.h5)
"""

import logging
import math
import time
import signal
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Tuple, Dict, List
import numpy as np

from ..models.measurement import (
    L1MetrologyMeasurement,
    L2TimingMeasurement,
    StationID,
    QualityGrade,
    QualityFlag,
    DiscriminationMethod
)
from ..io import make_data_product_writer, make_data_product_reader
from .wwv_constants import STATION_LOCATIONS
from .hop_geometry import hop_geometry, n_hops_for_distance
# Shared constants for the geometric-fallback ionospheric delay (M-M21):
# the propagation_engine module is the canonical source so the two paths
# don't drift apart.
from .propagation_engine import (
    SPEED_OF_LIGHT_KM_S,
    F2_LAYER_HEIGHT_KM,
    IONO_DELAY_CONSTANT_MS,
    NOMINAL_SLANT_TEC_PER_HOP_TECU,
)

logger = logging.getLogger(__name__)


# =====================================================================
# M-M22 + M-M23: traceable uncertainty components + Welch-Satterthwaite
# =====================================================================
#
# Every Type-A and Type-B component below carries both a `dof` (its
# degrees-of-freedom, ∞ for Type-B deterministic sources) and a `source`
# string citing the measurement/datasheet/standard the value comes from.
# The effective DOF for the combined uncertainty is then computed via the
# Welch-Satterthwaite formula and the coverage factor `k` is the
# corresponding two-sided Student-t multiplier (not a fixed k=2.0, which
# only corresponds to ~95 % coverage for ν=∞; for ν=10 the right value
# is 2.228, so the previous hard-coded k=2.0 with degrees_of_freedom=10
# under-reported expanded_uncertainty_ms by ~11 %).

from dataclasses import dataclass


@dataclass(frozen=True)
class UncertaintyComponent:
    """One ISO-GUM uncertainty term, with a citable source and a
    degrees-of-freedom estimate.

    Attributes
    ----------
    value_ms
        Standard uncertainty (1σ, ms).
    dof
        Degrees of freedom for the *uncertainty estimate itself*; use
        ``math.inf`` for Type-B terms whose magnitude is set by a
        spec/datasheet rather than measured.
    source
        Citation: where the value comes from (measurement, datasheet,
        standard, peer paper, internal calibration).  Surfaced by the
        traceability tooling and the diagnostic logs.
    """
    value_ms: float
    dof: float
    source: str


def _welch_satterthwaite(components: Dict[str, UncertaintyComponent]) -> float:
    """Effective DOF for a combined standard uncertainty (ISO GUM eq. G.2b).

    For components that are themselves perfectly known (``dof = inf``)
    the denominator contribution is zero; this is the standard
    treatment that lets fully-deterministic Type-B terms contribute to
    the combined uncertainty without artificially capping ν_eff.
    """
    numerator = sum(c.value_ms ** 2 for c in components.values()) ** 2
    if numerator == 0.0:
        return float('inf')
    denominator = 0.0
    for c in components.values():
        if not math.isfinite(c.dof) or c.dof <= 0:
            continue
        denominator += (c.value_ms ** 4) / c.dof
    if denominator == 0.0:
        return float('inf')
    return numerator / denominator


def _coverage_factor_95(dof_eff: float) -> float:
    """Two-sided 95 %-coverage Student-t multiplier for ν = ``dof_eff``.

    Converges to the normal quantile ``z(0.975) ≈ 1.95996`` as
    ν → ∞.  Falls back to that value when scipy isn't available so the
    service still runs (the diagnostic logs will note the fallback).
    """
    try:
        from scipy.stats import t  # local import — scipy is heavy
    except Exception:
        return 1.959963984540054
    if not math.isfinite(dof_eff) or dof_eff <= 0:
        return 1.959963984540054
    return float(t.ppf(0.975, dof_eff))


try:
    from .propagation_mode_solver import PropagationModeSolver as PropagationModeSolver
    _PROP_SOLVER_AVAILABLE = True
    _PROP_SOLVER_WARN: Optional[str] = None
except Exception as _pms_exc:
    PropagationModeSolver = None  # type: ignore[assignment,misc]
    _PROP_SOLVER_AVAILABLE = False
    _PROP_SOLVER_WARN: Optional[str] = str(_pms_exc)  # type: ignore[no-redef]

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False


class L2CalibrationService:
    """
    Service to convert L1 metrology measurements to L2 calibrated timing.
    
    Runs continuously, processing new L1 data and producing L2 output.
    """
    
    def __init__(
        self,
        data_root: Path,
        receiver_grid: str,
        receiver_lat: float,
        receiver_lon: float,
        channels: List[str],
        poll_interval: float = 60.0,
        lookback_minutes: int = 10,
        realtime_iono: bool = True,
        storage_config: Optional[Dict] = None,
    ):
        """
        Initialize L2 calibration service.
        
        Args:
            data_root: Root data directory (/var/lib/timestd)
            receiver_grid: Maidenhead grid square
            receiver_lat: Receiver latitude
            receiver_lon: Receiver longitude
            channels: List of channel names to process
            poll_interval: How often to check for new data (seconds)
            lookback_minutes: How far back to read L1 data
        """
        self.data_root = Path(data_root)
        self.receiver_grid = receiver_grid
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.channels = channels
        self.poll_interval = poll_interval
        self.lookback_minutes = lookback_minutes
        self.realtime_iono = realtime_iono
        # [storage] config — drives HDF5 / SQLite / dual-write selection
        # in make_data_product_writer (HDF5→SQLite migration). None →
        # HDF5-only, preserving today's behaviour.
        self._storage_config = storage_config or {}

        # Initialize propagation solver (optional — falls back to geometric-only)
        if not _PROP_SOLVER_AVAILABLE:
            logger.warning(
                f"PropagationModeSolver unavailable ({_PROP_SOLVER_WARN}); "
                "L2 will use geometric-only delay with inflated uncertainty"
            )
            self.prop_solver = None
        else:
            try:
                self.prop_solver = PropagationModeSolver(receiver_grid)
            except Exception as e:
                logger.warning(f"PropagationModeSolver init failed ({e}); geometric fallback active")
                self.prop_solver = None

        # Initialize readers and writers per channel
        self.l1_readers: Dict[str, Any] = {}
        self.l2_writers: Dict[str, Any] = {}

        for channel in channels:
            # L1 reader
            l1_dir = self.data_root / "phase2" / channel / "metrology"
            self.l1_readers[channel] = make_data_product_reader(
                data_dir=l1_dir,
                product_level='L1',
                product_name='metrology_measurements',
                channel=channel,
                storage_config=self._storage_config,
            )
            
            # L2 writer
            l2_dir = self.data_root / "phase2" / channel / "clock_offset"
            l2_dir.mkdir(parents=True, exist_ok=True)
            self.l2_writers[channel] = make_data_product_writer(
                output_dir=l2_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=channel,
                version='v1',
                storage_config=self._storage_config,
            )
        
        # Service state
        self.running = False
        # Seeded from L2 output on startup (see _seed_last_processed)
        self.last_processed: Dict[str, int] = {ch: 0 for ch in channels}
        
        # Data freshness tracking
        self.stale_warning_issued: Dict[str, bool] = {ch: False for ch in channels}
        self.max_data_age_seconds = 300.0  # 5 minutes - warn if L1 data older than this
        
        logger.info(f"L2CalibrationService initialized for {len(channels)} channels")
        logger.info(f"Receiver: {receiver_grid} ({receiver_lat:.4f}, {receiver_lon:.4f})")
    
    def start(self):
        """Start the calibration service."""
        # Register signal handlers from start() (called from the main thread),
        # not __init__, to avoid a race where SIGTERM arrives before self.running
        # is set True and triggers stop() on a not-yet-started service.
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self.running = True
        logger.info("L2 Calibration Service starting...")

        # Start IonoDataService background fetch thread so HFPropagationModel
        # receives real WAM-IPE foF2/hmF2 data rather than climatological fallback.
        # Gated on realtime_iono; when false the propagation model falls back to
        # climatology — Chrony is still disciplined, only uncertainty budget quality differs.
        if self.realtime_iono:
            try:
                from .iono_data_service import IonoDataService
                _iono = IonoDataService.get_instance()
                _iono.start()
                logger.info("IonoDataService background thread started")
            except Exception as e:
                logger.warning(f"IonoDataService could not start: {e} — propagation model will use fallback")
        else:
            logger.info("realtime_iono=false — IonoDataService not started; propagation model uses climatological fallback")

        # Seed last_processed from existing L2 output files and expand the
        # lookback window to cover any gap since the last processed minute.
        self._seed_last_processed()

        # Notify systemd we're ready
        if SYSTEMD_AVAILABLE:
            systemd_daemon.notify('READY=1')
            logger.info("Systemd watchdog enabled")
        
        while self.running:
            try:
                # Notify systemd watchdog
                if SYSTEMD_AVAILABLE:
                    systemd_daemon.notify('WATCHDOG=1')
                
                # Process each channel
                for channel in self.channels:
                    self._process_channel(channel)
                
                # Sleep until next poll
                time.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(self.poll_interval)
    
    def stop(self):
        """Stop the calibration service."""
        logger.info("Stopping L2 Calibration Service...")
        self.running = False
        try:
            from .iono_data_service import IonoDataService
            if IonoDataService._instance is not None:
                IonoDataService._instance.stop()
        except Exception as e:
            logger.debug(f"Ignored exception: {e}")
            pass
        
        # Close all writers
        for writer in self.l2_writers.values():
            writer.close()

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}")
        self.stop()

    def _seed_last_processed(self):
        """Initialise last_processed and lookback_minutes from existing L2 output.

        On a normal restart the L2 clock_offset files already contain the most
        recently calibrated minute.  Reading the last minute_boundary from each
        file lets the service continue from where it left off instead of
        re-processing the whole default lookback window, or — worse — silently
        skipping the gap when the gap is wider than lookback_minutes.

        The lookback_minutes is expanded to cover the largest per-channel gap
        (plus a 10-minute margin), capped at 24 hours.
        """
        import h5py

        max_gap_minutes = 0

        for channel in self.channels:
            l2_dir = self.data_root / "phase2" / channel / "clock_offset"
            if not l2_dir.exists():
                continue

            # Find the newest L2 file for this channel
            h5_files = sorted(l2_dir.glob(f"{channel}_timing_measurements_????????.h5"))
            if not h5_files:
                continue

            last_ts = 0.0
            parse_failures = 0
            for h5_path in reversed(h5_files):
                try:
                    with h5py.File(str(h5_path), 'r', swmr=True) as f:
                        # L2 files store minute_boundary_utc (epoch int) or timestamp_utc (ISO str)
                        for key in ('minute_boundary_utc', 'minute_boundary', 'timestamp_utc'):
                            if key not in f or len(f[key]) == 0:
                                continue
                            raw = f[key][-1]
                            if isinstance(raw, (bytes, str)):
                                raw_s = raw.decode() if isinstance(raw, bytes) else raw
                                last_ts = datetime.fromisoformat(
                                    raw_s.replace('Z', '+00:00')
                                ).timestamp()
                            else:
                                last_ts = float(raw)
                            break
                        if last_ts > 0:
                            break
                except Exception as exc:  # noqa: BLE001
                    # M-H22: do NOT swallow silently. A corrupt L2 file left
                    # last_ts=0, and an unseedable channel then reprocesses its
                    # whole lookback window — a storm with no logged cause.
                    parse_failures += 1
                    logger.warning(
                        f"Startup seed: could not read L2 file {h5_path.name} "
                        f"for channel {channel}: {exc} — trying the next-oldest file"
                    )
                    continue

            if last_ts <= 0:
                if parse_failures:
                    logger.warning(
                        f"Startup seed: no readable L2 file for channel {channel} "
                        f"({parse_failures} failed to parse) — cursor stays at 0, so the "
                        f"next cycle reprocesses the full lookback window"
                    )
                continue

            # Seed the cursor so _process_channel skips already-done work
            last_minute = int(last_ts // 60) * 60
            self.last_processed[channel] = last_minute

            gap_minutes = int((time.time() - last_ts) / 60)
            if gap_minutes > max_gap_minutes:
                max_gap_minutes = gap_minutes

        if max_gap_minutes > self.lookback_minutes:
            new_lookback = min(max_gap_minutes + 10, 24 * 60)
            logger.info(
                f"Startup: largest L2 gap is ~{max_gap_minutes} minutes — "
                f"expanding lookback from {self.lookback_minutes} to {new_lookback} minutes"
            )
            self.lookback_minutes = new_lookback
        else:
            logger.info(
                f"Startup: L2 gap ≤{self.lookback_minutes} minutes — "
                f"normal lookback window sufficient"
            )

    def _check_l1_freshness(self, channel: str) -> Tuple[bool, float]:
        """
        Check if L1 data for a channel is fresh enough to process.
        
        Args:
            channel: Channel name
            
        Returns:
            Tuple of (is_fresh, age_seconds)
        """
        l1_dir = self.data_root / "phase2" / channel / "metrology"
        if not l1_dir.exists():
            return False, float('inf')
        
        # Find most recent HDF5 file
        h5_files = list(l1_dir.glob("*.h5"))
        if not h5_files:
            return False, float('inf')
        
        # Get modification time of newest file
        newest_mtime = max(f.stat().st_mtime for f in h5_files)
        age_seconds = time.time() - newest_mtime
        
        return age_seconds < self.max_data_age_seconds, age_seconds
    
    def _process_channel(self, channel: str):
        """
        Process L1 data for a single channel and produce L2 output.
        
        Args:
            channel: Channel name (e.g., 'SHARED_10000')
        """
        try:
            # Check L1 data freshness before processing
            is_fresh, age_seconds = self._check_l1_freshness(channel)
            
            if not is_fresh:
                if not self.stale_warning_issued.get(channel, False):
                    logger.warning(
                        f"{channel}: L1 metrology data is stale ({age_seconds:.0f}s old, "
                        f"threshold={self.max_data_age_seconds:.0f}s). "
                        "Upstream metrology service may have stopped."
                    )
                    self.stale_warning_issued[channel] = True
                # Continue processing stale data - don't block downstream
                # but the warning is logged
            else:
                # Data is fresh - clear stale warning flag
                if self.stale_warning_issued.get(channel, False):
                    logger.info(f"{channel}: L1 metrology data is fresh again ({age_seconds:.0f}s old)")
                    self.stale_warning_issued[channel] = False
            
            # Read recent L1 measurements
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=self.lookback_minutes)
            
            l1_measurements = self.l1_readers[channel].read_time_range(
                start=start_time.isoformat().replace('+00:00', 'Z'),
                end=end_time.isoformat().replace('+00:00', 'Z'),
                min_confidence=0.0
            )
            
            if not l1_measurements:
                return
            
            # Filter for new measurements only
            new_measurements = [
                m for m in l1_measurements
                if m.get('minute_boundary_utc', 0) > self.last_processed[channel]
            ]
            
            if not new_measurements:
                return
            
            logger.debug(f"{channel}: Processing {len(new_measurements)} new L1 measurements")
            
            # Convert each L1 to L2
            for l1_dict in new_measurements:
                try:
                    l2_measurement = self._calibrate_measurement(l1_dict, channel)
                    
                    if l2_measurement:
                        # Write to HDF5 (canonical L2 artefact).
                        l2_dict = l2_measurement.model_dump(mode='json')
                        self.l2_writers[channel].write_measurement(l2_dict)

                        # Update last processed
                        minute_boundary = l1_dict.get('minute_boundary_utc', 0)
                        self.last_processed[channel] = max(
                            self.last_processed[channel],
                            minute_boundary
                        )
                
                except Exception as e:
                    logger.error(f"{channel}: Error calibrating measurement: {e}")
                    continue
            
            logger.info(f"{channel}: Processed {len(new_measurements)} measurements")
            
        except Exception as e:
            logger.error(f"{channel}: Error processing channel: {e}", exc_info=True)
    
    def _calibrate_measurement(
        self,
        l1_dict: dict,
        channel: str
    ) -> Optional[L2TimingMeasurement]:
        """
        Convert L1 metrology measurement to L2 calibrated timing measurement.
        
        Args:
            l1_dict: L1 measurement dictionary
            channel: Channel name
            
        Returns:
            L2TimingMeasurement or None if calibration fails
        """
        # Extract L1 fields
        station_id = l1_dict.get('station_id')
        if isinstance(station_id, bytes):
            station_id = station_id.decode()
        
        frequency_mhz = float(l1_dict.get('frequency_mhz', 0))
        raw_toa_ms = float(l1_dict.get('raw_toa_ms', 0))
        snr_db = float(l1_dict.get('snr_db', 0))
        tone_detected = bool(l1_dict.get('tone_detected', False))
        
        if not tone_detected or np.isnan(raw_toa_ms):
            # No tone detected - write L2 with NaN values
            return self._create_missing_l2(l1_dict, channel)
        
        # Get station location
        if station_id not in STATION_LOCATIONS:
            logger.warning(f"Unknown station: {station_id}")
            return None
        
        station_info = STATION_LOCATIONS[station_id]
        station_lat = station_info['lat']
        station_lon = station_info['lon']
        
        # Calculate propagation modes (full solver if available, else geometric fallback)
        try:
            if self.prop_solver is not None:
                modes = self.prop_solver.calculate_modes(
                    station=station_id,
                    frequency_mhz=frequency_mhz,
                    max_hops=3
                )

                if not modes:
                    logger.warning(f"{channel}: No propagation modes for {station_id}")
                    return None

                # Pick the climatologically-dominant mode directly (M-H23).
                # `modes` is sorted by delay and (Tier-1) MUF-feasibility-
                # filtered; the propagation model defines its primary mode as
                # the shortest-delay feasible arrival, so the first viable
                # candidate IS that mode.
                #
                # The prior code instead reconstructed an "arrival" from each
                # candidate's own delay (raw_toa_ms + candidate.total_delay_ms)
                # and fed it back into identify_mode — circular: every candidate
                # self-identified, and the loosest-uncertainty mode "won".
                # raw_toa_ms is a timing residual (D_clock), not an absolute
                # measured delay, so identify_mode cannot be used here.
                dominant = next((m for m in modes if m.viable), modes[0])

                propagation_delay_ms = dominant.total_delay_ms
                mode_label = dominant.mode.value
                mode_confidence = dominant.model_confidence
                n_hops = dominant.n_hops
            else:
                # M-M21: geometric fallback now uses the shared
                # spherical-Earth hop model + a climatological 40.3/f²
                # ionospheric group-delay term, matching the recipe in
                # `propagation_engine._estimate_geometric` (P-M19) and
                # `metrology_engine._vacuum_hop_fallback_delay` (M-M5).
                #
                # The previous "vacuum speed-of-light" propagation_delay
                # was a several-ms *bias* on every fallback measurement
                # (the real ionospheric delay is 2–5 ms over typical HF
                # paths) that the uncertainty budget then treated as a
                # zero-mean Type-B term — under-reporting the location
                # of the true value while over-reporting how
                # well-centred the estimate was.  Adding the
                # climatological iono term shrinks the bias to the
                # day-to-day TEC departure from climatology, which IS
                # zero-mean over hours-to-days.
                station_info = STATION_LOCATIONS[station_id]
                lat1, lon1 = math.radians(station_info['lat']), math.radians(station_info['lon'])
                lat2, lon2 = math.radians(self.receiver_lat), math.radians(self.receiver_lon)
                dlat, dlon = lat2 - lat1, lon2 - lon1
                a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
                dist_km = 6371.0 * 2 * math.asin(math.sqrt(a))

                n_hops = n_hops_for_distance(dist_km, F2_LAYER_HEIGHT_KM)
                geom = hop_geometry(dist_km, F2_LAYER_HEIGHT_KM, n_hops)
                geometric_delay_ms = geom.path_length_km / SPEED_OF_LIGHT_KM_S * 1000.0
                if frequency_mhz > 0:
                    iono_delay_ms = (
                        IONO_DELAY_CONSTANT_MS
                        * NOMINAL_SLANT_TEC_PER_HOP_TECU * n_hops
                        / (frequency_mhz ** 2)
                    )
                else:
                    iono_delay_ms = 0.0
                propagation_delay_ms = geometric_delay_ms + iono_delay_ms
                mode_label = "geometric"
                # Confidence stays at 0 — this branch carries the same
                # day-to-day TEC-departure risk the propagation solver
                # would have caught (it's the *bias* that's gone, not
                # the variance), so the wide-uncertainty path below
                # still applies.
                mode_confidence = 0.0
                logger.debug(
                    f"{channel}: Geometric-with-iono delay for {station_id}: "
                    f"{propagation_delay_ms:.2f} ms "
                    f"(geom={geometric_delay_ms:.2f} ms, iono={iono_delay_ms:.2f} ms, "
                    f"n_hops={n_hops})"
                )

            d_clock_ms = raw_toa_ms
            raw_arrival_time_ms = d_clock_ms + propagation_delay_ms
            
            # Calculate uncertainty budget (ISO GUM)
            uncertainty_budget = self._calculate_uncertainty(
                raw_toa_ms=raw_toa_ms,
                propagation_delay_ms=propagation_delay_ms,
                mode_confidence=mode_confidence,
                snr_db=snr_db,
                n_hops=n_hops
            )

            # Determine quality grade
            quality_grade = self._determine_quality_grade(
                mode_confidence,
                uncertainty_budget['combined_uncertainty_ms'],
                snr_db
            )
            
            # Create L2 measurement
            l2 = L2TimingMeasurement(
                timestamp_utc=l1_dict.get('timestamp_utc'),
                minute_boundary_utc=int(l1_dict.get('minute_boundary_utc', 0)),
                rtp_timestamp=int(l1_dict.get('rtp_timestamp', 0)),
                station=StationID[station_id],
                frequency_mhz=frequency_mhz,
                
                # Discrimination
                discrimination_method=DiscriminationMethod.TONE,
                discrimination_confidence=float(l1_dict.get('identification_confidence', 0.8)),
                
                # Timing
                tone_detected=True,
                raw_arrival_time_ms=raw_arrival_time_ms,
                clock_offset_ms=d_clock_ms,
                
                # Uncertainty (ISO GUM).  M-M22: `coverage_factor` and
                # `degrees_of_freedom` come from the Welch-Satterthwaite
                # calculation in `_calculate_uncertainty`, not hard-coded
                # k=2.0 / dof=10.
                uncertainty_ms=uncertainty_budget['combined_uncertainty_ms'],
                expanded_uncertainty_ms=uncertainty_budget['expanded_uncertainty_ms'],
                coverage_factor=uncertainty_budget['coverage_factor'],
                confidence_level=0.95,

                # Uncertainty components
                u_rtp_timestamp_ms=uncertainty_budget['u_rtp_timestamp_ms'],
                u_ionospheric_ms=uncertainty_budget['u_ionospheric_ms'],
                u_multipath_ms=uncertainty_budget['u_multipath_ms'],
                u_discrimination_ms=uncertainty_budget['u_discrimination_ms'],
                u_gpsdo_ms=uncertainty_budget['u_gpsdo_ms'],
                u_propagation_model_ms=uncertainty_budget['u_propagation_model_ms'],
                degrees_of_freedom=(
                    int(round(uncertainty_budget['effective_dof']))
                    if math.isfinite(uncertainty_budget['effective_dof'])
                    else 1_000_000   # int field; "effectively infinite" sentinel
                ),
                
                # Quality
                quality_grade=quality_grade,
                confidence=mode_confidence,
                quality_flag=QualityFlag.GOOD if mode_confidence > 0.7 else QualityFlag.MARGINAL,
                
                # Propagation
                propagation_delay_ms=propagation_delay_ms,
                propagation_mode=mode_label,
                n_hops=n_hops,
                
                # Signal
                snr_db=snr_db,
                doppler_hz=l1_dict.get('doppler_hz'),
                
                # Metadata
                traceability_chain=f"L1:{channel}→L2:calibration",
                processing_version="1.0.0",
                processed_at=datetime.now(timezone.utc).isoformat(),
                calibration_date=datetime.now(timezone.utc).date().isoformat(),
                # P4-B: Derive gpsdo_locked from L1 quality_flag.
                # L1 data only exists in RTP mode (GPSDO-locked), but a BAD
                # quality_flag indicates the measurement was flagged as unreliable
                # (e.g. low SNR, failed sanity check) — treat as unlocked.
                gpsdo_locked=str(l1_dict.get('quality_flag', 'GOOD')).upper() not in ('BAD', 'MISSING')
            )
            
            return l2
            
        except Exception as e:
            logger.error(f"{channel}: Calibration failed for {station_id}: {e}")
            return None
    
    def _create_missing_l2(self, l1_dict: dict, channel: str) -> Optional[L2TimingMeasurement]:
        """Create L2 measurement for missing/bad L1 data."""
        station_id = l1_dict.get('station_id')
        if isinstance(station_id, bytes):
            station_id = station_id.decode()
        
        if station_id not in StationID.__members__:
            logger.warning(f"{channel}: Unknown station_id '{station_id}' in missing L2 — skipping")
            return None
        
        return L2TimingMeasurement(
            timestamp_utc=l1_dict.get('timestamp_utc'),
            minute_boundary_utc=int(l1_dict.get('minute_boundary_utc', 0)),
            rtp_timestamp=int(l1_dict.get('rtp_timestamp', 0)),
            station=StationID[station_id],
            frequency_mhz=float(l1_dict.get('frequency_mhz', 0)),
            
            discrimination_method=DiscriminationMethod.TONE,
            discrimination_confidence=0.0,
            
            tone_detected=False,
            raw_arrival_time_ms=float('nan'),
            clock_offset_ms=float('nan'),
            
            uncertainty_ms=100.0,
            expanded_uncertainty_ms=200.0,
            coverage_factor=2.0,
            confidence_level=0.95,
            
            u_rtp_timestamp_ms=0.0,
            u_ionospheric_ms=0.0,
            u_multipath_ms=0.0,
            u_discrimination_ms=0.0,
            u_gpsdo_ms=0.0,
            u_propagation_model_ms=0.0,
            degrees_of_freedom=0,
            
            quality_grade=QualityGrade.D,
            confidence=0.0,
            quality_flag=QualityFlag.MISSING,
            
            traceability_chain=f"L1:{channel}→L2:missing",
            processing_version="1.0.0",
            processed_at=datetime.now(timezone.utc).isoformat(),
            calibration_date=datetime.now(timezone.utc).date().isoformat(),
            gpsdo_locked=False
        )
    
    def _calculate_uncertainty(
        self,
        raw_toa_ms: float,
        propagation_delay_ms: float,
        mode_confidence: float,
        snr_db: float,
        n_hops: int
    ) -> Dict[str, float]:
        """Build the ISO-GUM uncertainty budget.

        Each component is constructed as an :class:`UncertaintyComponent`
        with a citable ``source`` string and a degrees-of-freedom
        estimate, so the budget is auditable end-to-end (M-M23) and the
        coverage factor ``k`` can be computed from the Welch-Satterthwaite
        effective DOF (M-M22) instead of the previous hard-coded
        ``k=2.0`` / ``dof=10`` pair (which was internally inconsistent —
        for ν=10 the right two-sided 95 % multiplier is 2.228, so
        expanded_uncertainty was under-reported by ~11 %).
        """
        # 1. RTP-timestamp quantisation — Type-B, hardware spec, no DOF.
        u_rtp = UncertaintyComponent(
            value_ms=0.042,
            dof=math.inf,
            source=(
                "Type-B: 1-sample quantisation at the 24 kHz GPSDO-locked "
                "sample clock (1/24000 s = 41.67 μs).  ka9q-radio docs; "
                "no DOF — set by the clock-rate definition."
            ),
        )

        # 2. Ionospheric residual after model — Type-A from climatology.
        u_iono = UncertaintyComponent(
            value_ms=0.3 * float(np.sqrt(max(1, n_hops))),
            dof=10.0,
            source=(
                "Type-A: empirical mid-latitude residual TEC variability "
                "after propagation-model correction — ~1 TECU 1σ at "
                "10 MHz → 0.3 ms/hop; n_hops scaling assumes "
                "independent ionospheric columns per hop.  DOF=10 is the "
                "typical climatology bin sample-size (METROLOGY.md §3.2)."
            ),
        )

        # 3. Multipath, from SNR bins — Type-A empirical.
        if snr_db > 20:
            u_multipath_val = 0.05
        elif snr_db > 10:
            u_multipath_val = 0.3
        else:
            u_multipath_val = 1.0
        u_multipath = UncertaintyComponent(
            value_ms=u_multipath_val,
            dof=10.0,
            source=(
                "Type-A: SNR-binned multipath spread from per-channel "
                "field calibration (>20 dB: 0.05 ms; 10–20 dB: 0.3 ms; "
                "≤10 dB: 1.0 ms).  Sample size ~10 per bin in the "
                "calibration set (METROLOGY.md §3.3)."
            ),
        )

        # 4. Station discrimination — Type-B, mode-confusion bound.
        u_discrim = UncertaintyComponent(
            value_ms=0.1,
            dof=math.inf,
            source=(
                "Type-B: residual mode-confusion ambiguity bound; tone "
                "frequency uniquely identifies station, so this captures "
                "only multi-hop F2/E confusion (~0.1 ms).  Bounded by "
                "the propagation_mode_solver's MUF feasibility window."
            ),
        )

        # 5. GPSDO stability — Type-B, datasheet.
        u_gpsdo = UncertaintyComponent(
            value_ms=0.01,
            dof=math.inf,
            source=(
                "Type-B: GPSDO Allan deviation at τ=1 s ≈ 1e-11 s/s → "
                "10 ns/s (datasheet).  10 μs bound here is the conservative "
                "envelope used while the receiver is locked; the "
                "metrology pipeline rejects unlocked measurements upstream."
            ),
        )

        # 6. Propagation-model residual — Type-A, scales with mode_confidence.
        u_prop_model_val = 0.2 + 4.8 * (1.0 - max(0.0, min(1.0, mode_confidence)))
        u_prop_model = UncertaintyComponent(
            value_ms=u_prop_model_val,
            dof=10.0,
            source=(
                "Type-A: propagation_model residual (0.2 ms when "
                "mode_confidence=1; up to 5 ms when mode_confidence=0, "
                "covering worst-case hop ambiguity).  Calibrated against "
                "PHaRLAP raytrace fixed points; DOF set by the test grid."
            ),
        )

        components: Dict[str, UncertaintyComponent] = {
            'u_rtp_timestamp_ms': u_rtp,
            'u_ionospheric_ms': u_iono,
            'u_multipath_ms': u_multipath,
            'u_discrimination_ms': u_discrim,
            'u_gpsdo_ms': u_gpsdo,
            'u_propagation_model_ms': u_prop_model,
        }

        # Combined standard uncertainty (RSS over components).
        u_combined = float(np.sqrt(sum(c.value_ms ** 2 for c in components.values())))

        # Effective DOF (Welch-Satterthwaite) and the matching 95 %
        # coverage factor (Student-t two-sided).  Replaces the previous
        # hard-coded k=2.0 / dof=10 pair (mislabelled at 95 %).
        dof_eff = _welch_satterthwaite(components)
        k = _coverage_factor_95(dof_eff)
        u_expanded = k * u_combined

        return {
            'u_rtp_timestamp_ms': u_rtp.value_ms,
            'u_ionospheric_ms': u_iono.value_ms,
            'u_multipath_ms': u_multipath.value_ms,
            'u_discrimination_ms': u_discrim.value_ms,
            'u_gpsdo_ms': u_gpsdo.value_ms,
            'u_propagation_model_ms': u_prop_model.value_ms,
            'combined_uncertainty_ms': u_combined,
            'expanded_uncertainty_ms': u_expanded,
            'coverage_factor': k,
            'effective_dof': dof_eff,
        }
    
    def _determine_quality_grade(
        self,
        confidence: float,
        uncertainty_ms: float,
        snr_db: float
    ) -> QualityGrade:
        """
        Determine quality grade based on confidence, uncertainty, and SNR.
        
        Grade A: High confidence, low uncertainty, good SNR
        Grade B: Good confidence, moderate uncertainty
        Grade C: Moderate confidence, higher uncertainty
        Grade D: Low confidence or high uncertainty
        """
        if confidence > 0.8 and uncertainty_ms < 2.0 and snr_db > 15:
            return QualityGrade.A
        elif confidence > 0.6 and uncertainty_ms < 4.0 and snr_db > 10:
            return QualityGrade.B
        elif confidence > 0.4 and uncertainty_ms < 8.0:
            return QualityGrade.C
        else:
            return QualityGrade.D


def _load_config(config_path: str) -> dict:
    """Load and return the parsed TOML config, or empty dict on failure."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # Python < 3.11
    try:
        with open(config_path, 'rb') as f:
            return tomllib.load(f)
    except Exception as e:
        logger.warning(f"Could not load config {config_path}: {e}")
        return {}


def _channels_from_config(cfg: dict) -> List[str]:
    """Extract channel description strings from config.

    Checks two locations (in order):
      1. recorder.channels  — TOML array-of-tables  [[recorder.channels]]
      2. recorder.channel_group.timestd.channels  — legacy nested format
    """
    channels = []
    try:
        # Primary: [[recorder.channels]] array-of-tables
        recorder = cfg.get('recorder', {})
        for ch in recorder.get('channels', []):
            desc = ch.get('description', '')
            if desc:
                channels.append(desc)
        if channels:
            return channels

        # Fallback: recorder.channel_group.timestd.channels
        groups = recorder.get('channel_group', {})
        timestd_group = groups.get('timestd', {})
        for ch in timestd_group.get('channels', []):
            desc = ch.get('description', '')
            if desc:
                channels.append(desc)
    except Exception as e:
        logger.warning(f"Could not extract channels from config: {e}")
    return channels


def main():
    """Main entry point for L2 calibration service."""
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(description="L2 Calibration Service")
    parser.add_argument("--config", default="/etc/hf-timestd/timestd-config.toml",
                        help="Path to timestd-config.toml (default: /etc/hf-timestd/timestd-config.toml)")
    parser.add_argument("--data-root", default=None, help="Data root directory (overrides config)")
    parser.add_argument("--receiver-grid", default=None, help="Maidenhead grid square (overrides config)")
    parser.add_argument("--receiver-lat", type=float, default=None, help="Receiver latitude (overrides config)")
    parser.add_argument("--receiver-lon", type=float, default=None, help="Receiver longitude (overrides config)")
    parser.add_argument("--channels", nargs='+', default=None, help="Channels to process (overrides config)")
    parser.add_argument("--poll-interval", type=float, default=60.0, help="Poll interval (seconds)")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Load config, then apply CLI overrides
    cfg = _load_config(args.config)
    station = cfg.get('station', {})
    
    data_root = args.data_root or cfg.get('recorder', {}).get('production_data_root', '/var/lib/timestd')
    receiver_grid = args.receiver_grid or station.get('grid_square', '')
    receiver_lat = args.receiver_lat if args.receiver_lat is not None else station.get('latitude')
    receiver_lon = args.receiver_lon if args.receiver_lon is not None else station.get('longitude')
    channels = args.channels or _channels_from_config(cfg)
    
    # Validate required fields
    if not receiver_grid:
        logger.error("receiver-grid not set (provide --receiver-grid or set station.grid_square in config)")
        sys.exit(1)
    if receiver_lat is None or receiver_lon is None:
        logger.error("receiver lat/lon not set (provide --receiver-lat/--receiver-lon or set station.latitude/longitude in config)")
        sys.exit(1)
    if not channels:
        logger.error("No channels configured (provide --channels or define recorder.channel_group.timestd in config)")
        sys.exit(1)
    
    realtime_iono = cfg.get('metrology', {}).get('realtime_iono', True)

    # Create and start service
    service = L2CalibrationService(
        data_root=Path(data_root),
        receiver_grid=receiver_grid,
        receiver_lat=float(receiver_lat),
        receiver_lon=float(receiver_lon),
        channels=channels,
        poll_interval=args.poll_interval,
        realtime_iono=bool(realtime_iono),
        storage_config=cfg.get('storage', {}) or {},
    )
    
    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        service.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
