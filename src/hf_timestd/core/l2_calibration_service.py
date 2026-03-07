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
import time
import signal
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, List
import numpy as np

from ..models.measurement import (
    L1MetrologyMeasurement,
    L2TimingMeasurement,
    StationID,
    QualityGrade,
    QualityFlag,
    DiscriminationMethod
)
from ..io.hdf5_writer import DataProductWriter
from ..io.hdf5_reader import DataProductReader
from .propagation_mode_solver import PropagationModeSolver
from .wwv_constants import STATION_LOCATIONS

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False

logger = logging.getLogger(__name__)


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
        lookback_minutes: int = 10
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
        
        # Initialize propagation solver
        self.prop_solver = PropagationModeSolver(receiver_grid)
        
        # Initialize readers and writers per channel
        self.l1_readers: Dict[str, DataProductReader] = {}
        self.l2_writers: Dict[str, DataProductWriter] = {}
        
        for channel in channels:
            # L1 reader
            l1_dir = self.data_root / "phase2" / channel / "metrology"
            self.l1_readers[channel] = DataProductReader(
                data_dir=l1_dir,
                product_level='L1',
                product_name='metrology_measurements',
                channel=channel
            )
            
            # L2 writer
            l2_dir = self.data_root / "phase2" / channel / "clock_offset"
            l2_dir.mkdir(parents=True, exist_ok=True)
            self.l2_writers[channel] = DataProductWriter(
                output_dir=l2_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel=channel,
                version='v1'
            )
        
        # Service state
        self.running = False
        self.last_processed: Dict[str, int] = {ch: 0 for ch in channels}
        
        # Data freshness tracking
        self.stale_warning_issued: Dict[str, bool] = {ch: False for ch in channels}
        self.max_data_age_seconds = 300.0  # 5 minutes - warn if L1 data older than this
        
        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        logger.info(f"L2CalibrationService initialized for {len(channels)} channels")
        logger.info(f"Receiver: {receiver_grid} ({receiver_lat:.4f}, {receiver_lon:.4f})")
    
    def start(self):
        """Start the calibration service."""
        self.running = True
        logger.info("L2 Calibration Service starting...")

        # Start IonoDataService background fetch thread so HFPropagationModel
        # receives real WAM-IPE foF2/hmF2 data rather than climatological fallback.
        try:
            from .iono_data_service import IonoDataService
            _iono = IonoDataService.get_instance()
            _iono.start()
            logger.info("IonoDataService background thread started")
        except Exception as e:
            logger.warning(f"IonoDataService could not start: {e} — propagation model will use fallback")

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
                        # Write to HDF5
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
        
        # Calculate propagation modes
        try:
            modes = self.prop_solver.calculate_modes(
                station=station_id,
                frequency_mhz=frequency_mhz,
                max_hops=3
            )
            
            if not modes:
                logger.warning(f"{channel}: No propagation modes for {station_id}")
                return None
            
            # raw_toa_ms is D_clock (timing residual = arrival - expected_delay).
            # To identify the propagation mode we need the absolute arrival time:
            #   arrival ≈ D_clock + mode.total_delay_ms
            # We do NOT assume the lowest-hop mode a priori — that is circular.
            # Instead, try every candidate mode, reconstruct the implied arrival,
            # and let identify_mode score each one.  Pick the mode whose implied
            # arrival produces the highest identification confidence.
            best_mode_result = None
            for candidate_mode in modes:
                candidate_arrival_ms = raw_toa_ms + candidate_mode.total_delay_ms
                candidate_result = self.prop_solver.identify_mode(
                    station=station_id,
                    measured_delay_ms=candidate_arrival_ms,
                    frequency_mhz=frequency_mhz
                )
                if (best_mode_result is None or
                        candidate_result.confidence > best_mode_result.confidence):
                    best_mode_result = candidate_result
            mode_result = best_mode_result
            
            # L1 raw_toa_ms currently carries timing error (D_clock), not absolute ToA.
            # Reconstruct an absolute arrival time for L2 schema consistency:
            #   raw_arrival_time_ms = d_clock_ms + propagation_delay_ms
            propagation_delay_ms = mode_result.calculated_delay_ms
            d_clock_ms = raw_toa_ms
            raw_arrival_time_ms = d_clock_ms + propagation_delay_ms
            
            # Calculate uncertainty budget (ISO GUM)
            uncertainty_budget = self._calculate_uncertainty(
                raw_toa_ms=raw_toa_ms,
                propagation_delay_ms=propagation_delay_ms,
                mode_confidence=mode_result.confidence,
                snr_db=snr_db,
                n_hops=mode_result.n_hops
            )
            
            # Determine quality grade
            quality_grade = self._determine_quality_grade(
                mode_result.confidence,
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
                
                # Uncertainty (ISO GUM)
                uncertainty_ms=uncertainty_budget['combined_uncertainty_ms'],
                expanded_uncertainty_ms=uncertainty_budget['expanded_uncertainty_ms'],
                coverage_factor=2.0,
                confidence_level=0.95,
                
                # Uncertainty components
                u_rtp_timestamp_ms=uncertainty_budget['u_rtp_timestamp_ms'],
                u_ionospheric_ms=uncertainty_budget['u_ionospheric_ms'],
                u_multipath_ms=uncertainty_budget['u_multipath_ms'],
                u_discrimination_ms=uncertainty_budget['u_discrimination_ms'],
                u_gpsdo_ms=uncertainty_budget['u_gpsdo_ms'],
                u_propagation_model_ms=uncertainty_budget['u_propagation_model_ms'],
                degrees_of_freedom=10,
                
                # Quality
                quality_grade=quality_grade,
                confidence=mode_result.confidence,
                quality_flag=QualityFlag.GOOD if mode_result.confidence > 0.7 else QualityFlag.MARGINAL,
                
                # Propagation
                propagation_delay_ms=propagation_delay_ms,
                propagation_mode=mode_result.identified_mode.value,
                n_hops=mode_result.n_hops,
                
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
        """
        Calculate ISO GUM uncertainty budget.
        
        Returns dict with uncertainty components and combined uncertainty.
        """
        # Component uncertainties (all in ms)
        
        # 1. RTP timestamp precision (GPSDO-locked, 24 kHz sample clock → ~42 μs)
        u_rtp = 0.042  # 1 sample at 24 kHz
        
        # 2. Ionospheric delay uncertainty.
        # The propagation model uncertainty already captures the ionospheric
        # model error (it scales with 1 - mode_confidence below).  Here we
        # account only for the residual TEC variability NOT captured by the
        # model: empirically ~0.3 ms 1-sigma for a single hop at mid-latitudes
        # (corresponds to ~1 TECU TEC uncertainty at 10 MHz).
        # For multi-hop paths the uncertainty grows as sqrt(n_hops) because
        # each hop samples a different ionospheric column.
        u_iono = 0.3 * np.sqrt(max(1, n_hops))
        
        # 3. Multipath (from SNR).
        # SNR < 10 dB: multipath-limited (~1 ms); 10-20 dB: ~0.3 ms; >20 dB: ~0.05 ms.
        if snr_db > 20:
            u_multipath = 0.05
        elif snr_db > 10:
            u_multipath = 0.3
        else:
            u_multipath = 1.0
        
        # 4. Station discrimination.
        # Tone frequency is unique per station — misidentification is rare.
        # Residual ambiguity from multi-hop mode confusion: ~0.1 ms.
        u_discrim = 0.1
        
        # 5. GPSDO stability (Allan deviation at 1 s for a typical GPSDO: ~1e-11 s/s
        # → ~10 ns over 1 s, negligible; dominant term is holdover during unlocked
        # intervals, but we only process when locked).
        u_gpsdo = 0.01  # 10 μs conservative bound for locked GPSDO
        
        # 6. Propagation model uncertainty.
        # mode_confidence=1.0 → model matches well → small residual (~0.2 ms).
        # mode_confidence=0.0 → mode unknown → up to ~5 ms (worst-case hop ambiguity).
        u_prop_model = 0.2 + 4.8 * (1.0 - max(0.0, min(1.0, mode_confidence)))
        
        # Combined uncertainty (RSS - Root Sum of Squares)
        u_combined = np.sqrt(
            u_rtp**2 +
            u_iono**2 +
            u_multipath**2 +
            u_discrim**2 +
            u_gpsdo**2 +
            u_prop_model**2
        )
        
        # Expanded uncertainty (k=2 for 95% confidence)
        u_expanded = 2.0 * u_combined
        
        return {
            'u_rtp_timestamp_ms': u_rtp,
            'u_ionospheric_ms': u_iono,
            'u_multipath_ms': u_multipath,
            'u_discrimination_ms': u_discrim,
            'u_gpsdo_ms': u_gpsdo,
            'u_propagation_model_ms': u_prop_model,
            'combined_uncertainty_ms': u_combined,
            'expanded_uncertainty_ms': u_expanded
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
    
    # Create and start service
    service = L2CalibrationService(
        data_root=Path(data_root),
        receiver_grid=receiver_grid,
        receiver_lat=float(receiver_lat),
        receiver_lon=float(receiver_lon),
        channels=channels,
        poll_interval=args.poll_interval
    )
    
    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        service.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
