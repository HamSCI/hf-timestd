#!/usr/bin/env python3
"""
Analytics Service - Process NPZ Archives to Derived Products

Watches for new NPZ files from core recorder and generates:
1. Quality metrics (completeness, gaps, packet loss)
2. WWV/CHU/WWVH tone detection → time_snap establishment
3. Discontinuity logs (scientific provenance)
4. Decimated Digital RF (10 Hz) with timing quality annotations
5. Upload queue management

Design Philosophy:
- Independent from core recorder (can restart without data loss)
- Reprocessable (can rerun with improved algorithms)
- Crash-tolerant (systemd restarts, aggressive retry)
- Always upload with quality annotations (no gaps in data)
"""

import numpy as np
import logging
import time
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from collections import deque
from scipy import signal as scipy_signal
import json
from enum import Enum

from .tone_detector import MultiStationToneDetector
from .wwvh_discrimination import WWVHDiscriminator, DiscriminationResult
# Note: decimation moved to grape-recorder package
from .discrimination_csv_writers import (
    DiscriminationCSVWriters,
    ToneDetectionRecord,
    StationID440HzRecord,
    TestSignalRecord,
    DopplerRecord,
    DiscriminationRecord
)
from .timing_metrics_writer import TimingMetricsWriter
from .transmission_time_solver import (
    TransmissionTimeSolver, create_solver_from_grid, SolverResult,
    MultiStationSolver, CombinedUTCResult, create_multi_station_solver
)
from .differential_time_solver import (
    DifferentialTimeSolver, DifferentialResult, MultiFrequencyResult,
    GlobalDifferentialSolver, GlobalSolveResult
)
from .global_timing_coordinator import GlobalTimingCoordinator, create_coordinator
from .gpsdo_monitor import GPSDOMonitor, AnchorState
from ..interfaces.data_models import (
    TimeSnapReference, 
    QualityInfo, 
    Discontinuity, 
    DiscontinuityType,
    ToneDetectionResult,
    StationType
)

logger = logging.getLogger(__name__)


class TimingQuality(Enum):
    """
    Quality levels for timestamp accuracy in Digital RF data
    
    Used to annotate each data segment with timing confidence,
    enabling scientists to filter/reprocess based on requirements.
    
    Hierarchy: TONE_LOCKED > NTP_SYNCED > WALL_CLOCK
    """
    TONE_LOCKED = "tone_locked"    # WWV/CHU time_snap within last 5 minutes - ±1ms accuracy
    NTP_SYNCED = "ntp_synced"      # System clock NTP-synchronized - ±10ms accuracy  
    WALL_CLOCK = "wall_clock"      # System clock unsynchronized - ±seconds accuracy


@dataclass
class TimingAnnotation:
    """
    Metadata about timing quality for a data segment
    """
    quality: TimingQuality
    utc_timestamp: float
    time_snap_age_seconds: Optional[float] = None  # Age of time_snap if used
    ntp_offset_ms: Optional[float] = None          # NTP offset if checked
    sample_count_error: int = 0                     # Expected vs actual samples
    reprocessing_recommended: bool = False
    notes: str = ""


@dataclass
class NPZArchive:
    """
    Parsed NPZ archive from core recorder
    """
    file_path: Path
    
    # Primary data
    iq_samples: np.ndarray
    
    # Timing reference (critical)
    rtp_timestamp: int
    rtp_ssrc: int
    sample_rate: int
    
    # Metadata
    frequency_hz: float
    channel_name: str
    unix_timestamp: float
    
    # Quality indicators
    gaps_filled: int
    gaps_count: int
    packets_received: int
    packets_expected: int
    
    # Gap details
    gap_rtp_timestamps: np.ndarray
    gap_sample_indices: np.ndarray
    gap_samples_filled: np.ndarray
    gap_packets_lost: np.ndarray
    
    # Provenance
    recorder_version: str
    created_timestamp: float

    # Embedded time_snap metadata (new NPZ fields)
    time_snap_rtp: Optional[int] = None
    time_snap_utc: Optional[float] = None
    time_snap_source: Optional[str] = None
    time_snap_confidence: Optional[float] = None
    time_snap_station: Optional[str] = None

    # Recorder-side tone measurements
    tone_power_1000_hz_db: Optional[float] = None
    tone_power_1200_hz_db: Optional[float] = None
    wwvh_differential_delay_ms: Optional[float] = None
    
    # NTP wall clock time (independent reference for drift measurement)
    ntp_wall_clock_time: Optional[float] = None  # Wall clock time when archive created
    ntp_offset_ms: Optional[float] = None        # NTP offset at creation time
    
    @classmethod
    def load(cls, file_path: Path) -> 'NPZArchive':
        """Load NPZ archive from file"""
        data = np.load(file_path, allow_pickle=True)
        
        return cls(
            file_path=file_path,
            iq_samples=data['iq'],
            rtp_timestamp=int(data['rtp_timestamp']),
            rtp_ssrc=int(data['rtp_ssrc']),
            sample_rate=int(data['sample_rate']),
            frequency_hz=float(data['frequency_hz']),
            channel_name=str(data['channel_name']),
            unix_timestamp=float(data['unix_timestamp']),
            gaps_filled=int(data['gaps_filled']),
            gaps_count=int(data['gaps_count']),
            packets_received=int(data['packets_received']),
            packets_expected=int(data['packets_expected']),
            gap_rtp_timestamps=data['gap_rtp_timestamps'],
            gap_sample_indices=data['gap_sample_indices'],
            gap_samples_filled=data['gap_samples_filled'],
            gap_packets_lost=data['gap_packets_lost'],
            recorder_version=str(data['recorder_version']),
            created_timestamp=float(data['created_timestamp']),
            time_snap_rtp=cls._get_optional_scalar(data, 'time_snap_rtp', int),
            time_snap_utc=cls._get_optional_scalar(data, 'time_snap_utc', float),
            time_snap_source=cls._get_optional_scalar(data, 'time_snap_source', str),
            time_snap_confidence=cls._get_optional_scalar(data, 'time_snap_confidence', float),
            time_snap_station=cls._get_optional_scalar(data, 'time_snap_station', str),
            tone_power_1000_hz_db=cls._get_optional_scalar(data, 'tone_power_1000_hz_db', float),
            tone_power_1200_hz_db=cls._get_optional_scalar(data, 'tone_power_1200_hz_db', float),
            wwvh_differential_delay_ms=cls._get_optional_scalar(data, 'wwvh_differential_delay_ms', float),
            ntp_wall_clock_time=cls._get_optional_scalar(data, 'ntp_wall_clock_time', float),
            ntp_offset_ms=cls._get_optional_scalar(data, 'ntp_offset_ms', float)
        )
    
    def calculate_utc_timestamp(self, time_snap: Optional[TimeSnapReference]) -> float:
        """
        Calculate UTC timestamp for first sample using time_snap reference
        
        Args:
            time_snap: Current time_snap reference for RTP→UTC conversion
            
        Returns:
            UTC timestamp (seconds since epoch)
            
        Note:
            Falls back to wall clock (unix_timestamp) if time_snap not available.
            This allows cold start operation before WWV/CHU tone detection.
        """
        if time_snap:
            # Use precise RTP-to-UTC conversion
            return time_snap.calculate_sample_time(self.rtp_timestamp)
        else:
            # Fall back to wall clock (approximate, will be marked as low quality)
            return self.unix_timestamp

    @staticmethod
    def _get_optional_scalar(data, key: str, cast):
        """Safely extract optional scalar field from NPZ archive"""
        if key not in data:
            return None
        value = data[key]
        if isinstance(value, np.ndarray) and value.shape == ():
            value = value.item()
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    def embedded_time_snap(self) -> Optional[TimeSnapReference]:
        """Build TimeSnapReference from embedded NPZ metadata if available"""
        if self.time_snap_rtp is None or self.time_snap_utc is None:
            return None

        station = None
        if self.time_snap_station:
            try:
                station = StationType(self.time_snap_station)
            except ValueError:
                station = None
        if station is None:
            station = StationType.WWV if 'CHU' not in (self.channel_name or '').upper() else StationType.CHU

        return TimeSnapReference(
            rtp_timestamp=int(self.time_snap_rtp),
            utc_timestamp=float(self.time_snap_utc),
            sample_rate=self.sample_rate,
            source=str(self.time_snap_source or 'archive_time_snap'),
            confidence=float(self.time_snap_confidence or 0.0),
            station=station,
            established_at=float(self.created_timestamp)
        )

    def startup_tone_snapshot(self) -> Optional[Dict[str, float]]:
        """Return recorder-side tone power measurements if present"""
        if self.tone_power_1000_hz_db is None and self.tone_power_1200_hz_db is None:
            return None
        return {
            'tone_power_1000_hz_db': self.tone_power_1000_hz_db,
            'tone_power_1200_hz_db': self.tone_power_1200_hz_db,
            'wwvh_differential_delay_ms': self.wwvh_differential_delay_ms
        }


@dataclass
class ProcessingState:
    """
    State tracking for analytics processing
    """
    last_processed_file: Optional[Path] = None
    last_processed_time: Optional[float] = None
    files_processed: int = 0
    time_snap: Optional[TimeSnapReference] = None
    time_snap_history: List[TimeSnapReference] = field(default_factory=list)
    detection_history: List[ToneDetectionResult] = field(default_factory=list)
    
    # Limits for history to prevent state file bloat
    MAX_TIME_SNAP_HISTORY = 20
    MAX_DETECTION_HISTORY = 50
    
    def to_dict(self) -> Dict:
        """Serialize state for persistence (limit history size)"""
        # Keep only recent history to prevent massive state files
        time_snap_slice = self.time_snap_history[-self.MAX_TIME_SNAP_HISTORY:] if self.time_snap_history else []
        
        return {
            'last_processed_file': str(self.last_processed_file) if self.last_processed_file else None,
            'last_processed_time': self.last_processed_time,
            'files_processed': self.files_processed,
            'time_snap': self.time_snap.to_dict() if self.time_snap else None,
            'time_snap_history': [ts.to_dict() for ts in time_snap_slice],
            'detection_count': len(self.detection_history)
        }


class AnalyticsService:
    """
    Main analytics service - processes NPZ archives to derived products
    """
    
    def __init__(
        self,
        archive_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        state_file: Optional[Path] = None,
        station_config: Optional[Dict] = None
    ):
        """
        Initialize analytics service
        
        Args:
            archive_dir: Directory containing NPZ archives from core recorder
            output_dir: Base directory for derived products
            channel_name: Channel identifier (for tone detector configuration)
            frequency_hz: Center frequency in Hz
            state_file: Path to state persistence file (optional)
            station_config: Station metadata (callsign, grid, etc.) for Digital RF
        """
        self.archive_dir = Path(archive_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.state_file = state_file
        self.station_config = station_config or {}
        
        # Create output directories
        self.quality_dir = self.output_dir / 'quality'
        self.decimated_dir = self.output_dir / 'decimated'
        self.drf_dir = self.output_dir / 'digital_rf'
        self.logs_dir = self.output_dir / 'logs'
        self.discrimination_log_dir = self.output_dir / 'discrimination'
        
        for d in [self.quality_dir, self.decimated_dir, self.drf_dir, self.logs_dir, self.discrimination_log_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        # Processing state
        self.state = ProcessingState()
        self._load_state()
        
        # Tone detector (resamples 16k → 3k internally)
        self.tone_detector = MultiStationToneDetector(
            channel_name=channel_name,
            sample_rate=3000  # Internal processing rate
        )
        
        # WWV/H discriminator (for channels that see both WWV and WWVH)
        self.wwvh_discriminator: Optional[WWVHDiscriminator] = None
        if self._is_wwv_wwvh_channel(channel_name):
            # Extract grid square from station config for geographic ToA prediction
            receiver_grid = self.station_config.get('grid_square')
            history_dir = str(self.output_dir / 'toa_history') if receiver_grid else None
            
            self.wwvh_discriminator = WWVHDiscriminator(
                channel_name=channel_name,
                receiver_grid=receiver_grid,
                history_dir=history_dir
            )
            if receiver_grid:
                logger.info(f"✅ WWVHDiscriminator initialized with geographic ToA prediction ({receiver_grid})")
            else:
                logger.info("✅ WWVHDiscriminator initialized (geographic ToA disabled - no grid_square in config)")
        
        # Transmission Time Solver - back-calculate UTC(NIST) from observed arrival times
        self.transmission_solver: Optional[TransmissionTimeSolver] = None
        self.differential_solver: Optional[DifferentialTimeSolver] = None
        self.latest_transmission_result: Optional[SolverResult] = None
        self.latest_differential_result: Optional[DifferentialResult] = None
        receiver_grid = self.station_config.get('grid_square')
        if receiver_grid and self._is_wwv_wwvh_channel(channel_name):
            try:
                # Determine sample rate from frequency (20 kHz for GRAPE channels)
                sample_rate = 20000
                self.transmission_solver = create_solver_from_grid(receiver_grid, sample_rate)
                logger.info(f"✅ TransmissionTimeSolver initialized - can back-calculate UTC(NIST)")
                
                # Differential solver uses WWV-WWVH difference (clock-error immune)
                from .transmission_time_solver import grid_to_latlon
                lat, lon = grid_to_latlon(receiver_grid)
                self.differential_solver = DifferentialTimeSolver(lat, lon)
                logger.info(f"✅ DifferentialTimeSolver initialized - uses clock-error-immune approach")
            except Exception as e:
                logger.warning(f"Could not initialize TransmissionTimeSolver: {e}")
        
        # Global Timing Coordinator - writes detections to shared file for cross-channel solving
        self.global_coordinator: Optional[GlobalTimingCoordinator] = None
        try:
            # Data root is typically output_dir's grandparent (e.g., /tmp/grape-test)
            data_root = output_dir.parent.parent
            if receiver_grid:
                self.global_coordinator = create_coordinator(data_root, receiver_grid)
                logger.info(f"✅ GlobalTimingCoordinator initialized - writes to shared/detections/")
        except Exception as e:
            logger.warning(f"Could not initialize GlobalTimingCoordinator: {e}")
        
        # Digital RF writing has been moved to standalone drf_writer_service.py
        # This service now only does tone detection, decimation, and outputs 10Hz NPZ files
        logger.info("✅ Analytics service initialized (tone detection + decimation only)")
        
        # Running flag
        self.running = False
        
        # Status tracking
        self.start_time = time.time()
        self.status_file = self.output_dir / 'status' / 'analytics-service-status.json'
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Discrimination tracking
        self.latest_discrimination: Optional[DiscriminationResult] = None
        
        # Cross-file buffering for tone detection
        # Store tail of previous file to span minute boundaries
        self.previous_file_tail: Optional[np.ndarray] = None  # Last 30s of previous file (16 kHz IQ)
        self.previous_file_rtp_end: Optional[int] = None  # RTP timestamp at end of previous file
        
        # Rolling buffer for tone detection (need ~30 seconds of context)
        # Store resampled (3 kHz) samples to prepend to new archives
        self.tone_detection_buffer: Optional[np.ndarray] = None
        self.buffer_max_samples = int(30 * 3000)  # 30 seconds at 3 kHz
        self.discrimination_log_dir = self.output_dir / 'discrimination'
        self.discrimination_log_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize separate CSV writers for each discrimination method
        # CSVWriters expects the root data directory (e.g., /tmp/grape-test)
        # output_dir is /tmp/grape-test/analytics/WWV_10_MHz, so go up 2 levels
        data_root = output_dir.parent.parent
        self.csv_writers = DiscriminationCSVWriters(
            data_root=str(data_root),
            channel_name=channel_name
        )
        
        # Timing metrics writer for web-UI timing analysis
        timing_dir = output_dir / 'timing'
        timing_dir.mkdir(parents=True, exist_ok=True)
        receiver_grid = self.station_config.get('grid_square')
        self.timing_writer = TimingMetricsWriter(
            output_dir=timing_dir,
            channel_name=channel_name,
            receiver_grid=receiver_grid
        )
        
        # Track last timing metrics write time
        self.last_timing_metrics_write = 0.0
        self.timing_metrics_interval = 60.0  # Write every 60 seconds
        
        # GPSDO Monitoring Architecture
        # Implements "Set, Monitor, Intervention" strategy for GPS-disciplined timing
        # Trust the sample counter, use tone detections for verification not correction
        self.gpsdo_monitor = GPSDOMonitor(sample_rate=20000)
        logger.info(f"✅ GPSDOMonitor initialized - GPSDO monitoring architecture active")
        
        # Store data_root for global status files (GPSDO status is system-wide, not per-channel)
        # output_dir is {data_root}/analytics/{CHANNEL}, so go up 2 levels
        self.data_root = output_dir.parent.parent
        self.gpsdo_status_file = self.data_root / 'status' / 'gpsdo_status.json'
        self.gpsdo_status_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Per-channel statistics
        self.stats = {
            'last_processed_file': None,
            'last_processed_time': None,
            'drf_samples_written': 0,
            'drf_files_written': 0,
            'wwv_detections': 0,
            'wwvh_detections': 0,
            'chu_detections': 0,
            'total_detections': 0,
            'last_detection_time': None,
            'last_completeness_pct': 0.0,
            'last_packet_loss_pct': 0.0,
            'last_snr_db': None
        }
        
        logger.info(f"AnalyticsService initialized for {channel_name}")
        logger.info(f"Archive dir: {self.archive_dir}")
        logger.info(f"Output dir: {self.output_dir}")
        logger.info(f"Files processed: {self.state.files_processed}")
        logger.info(f"Time snap established: {self.state.time_snap is not None}")
    
    def _load_state(self):
        """Load persistent state from file"""
        if self.state_file and self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    state_data = json.load(f)
                
                # Restore basic state
                self.state.files_processed = state_data.get('files_processed', 0)
                self.state.last_processed_time = state_data.get('last_processed_time')
                
                if state_data.get('last_processed_file'):
                    self.state.last_processed_file = Path(state_data['last_processed_file'])
                
                # Restore time_snap if available
                if state_data.get('time_snap'):
                    ts = state_data['time_snap']
                    # Convert station string back to enum
                    station_value = ts['station']
                    station = StationType(station_value) if isinstance(station_value, str) else station_value
                    
                    self.state.time_snap = TimeSnapReference(
                        rtp_timestamp=ts['rtp_timestamp'],
                        utc_timestamp=ts['utc_timestamp'],
                        sample_rate=ts['sample_rate'],
                        source=ts['source'],
                        confidence=ts['confidence'],
                        station=station,
                        established_at=ts['established_at']
                    )
                
                logger.info(f"Loaded state from {self.state_file}")
            except Exception as e:
                logger.warning(f"Failed to load state: {e}")

    def _store_time_snap(self, new_time_snap: TimeSnapReference):
        """Persist a new time_snap reference and trim history."""
        self.state.time_snap = new_time_snap
        self.state.time_snap_history.append(new_time_snap)
        if len(self.state.time_snap_history) > ProcessingState.MAX_TIME_SNAP_HISTORY:
            self.state.time_snap_history = self.state.time_snap_history[-ProcessingState.MAX_TIME_SNAP_HISTORY:]

    def _maybe_adopt_archive_time_snap(self, archive: NPZArchive) -> Optional[TimeSnapReference]:
        """Adopt time_snap embedded in NPZ if it improves current state."""
        archive_time_snap = archive.embedded_time_snap()
        if not archive_time_snap:
            return self.state.time_snap

        current = self.state.time_snap
        should_adopt = False

        if current is None:
            should_adopt = True
        elif current.source in ('ntp', 'wall_clock'):
            should_adopt = True
        elif archive_time_snap.utc_timestamp > current.utc_timestamp and archive_time_snap.confidence >= (current.confidence or 0.0) - 0.05:
            should_adopt = True

        if should_adopt:
            self._store_time_snap(archive_time_snap)
            logger.info(
                f"Adopted archive time_snap ({archive_time_snap.source}) from {archive.file_path.name}"
            )
            return archive_time_snap

        return current

    def _save_state(self):
        """Save persistent state to file"""
        if self.state_file:
            try:
                with open(self.state_file, 'w') as f:
                    json.dump(self.state.to_dict(), f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to save state: {e}")
    
    def _get_discrimination_status(self) -> Optional[Dict]:
        """Get WWV/H discrimination status for status file"""
        if not self.wwvh_discriminator:
            return None
        
        # Get statistics from discriminator
        stats = self.wwvh_discriminator.get_statistics()
        
        if stats['count'] == 0:
            return {
                'enabled': True,
                'measurements': 0,
                'latest': None
            }
        
        # Include latest measurement if available
        latest_dict = None
        if self.latest_discrimination:
            latest_dict = self.latest_discrimination.to_dict()
        
        return {
            'enabled': True,
            'measurements': stats['count'],
            'mean_power_ratio_db': stats['mean_power_ratio_db'],
            'mean_differential_delay_ms': stats['mean_differential_delay_ms'],
            'wwv_dominant_pct': (stats['wwv_dominant_count'] / stats['count'] * 100) if stats['count'] > 0 else 0,
            'wwvh_dominant_pct': (stats['wwvh_dominant_count'] / stats['count'] * 100) if stats['count'] > 0 else 0,
            'balanced_pct': (stats['balanced_count'] / stats['count'] * 100) if stats['count'] > 0 else 0,
            'latest': latest_dict
        }
    
    def _write_status(self):
        """Write current status to JSON file for web-ui monitoring"""
        try:
            # Calculate time_snap age if established
            time_snap_dict = None
            if self.state.time_snap:
                age_minutes = int((time.time() - self.state.time_snap.established_at) / 60)
                time_snap_dict = {
                    'established': True,
                    'source': self.state.time_snap.source,
                    'station': self.state.time_snap.station.value,  # Convert enum to string
                    'rtp_timestamp': self.state.time_snap.rtp_timestamp,
                    'utc_timestamp': self.state.time_snap.utc_timestamp,
                    'confidence': self.state.time_snap.confidence,
                    'age_minutes': age_minutes
                }
            
            # Calculate pending files
            pending_files = len(self.discover_new_files())
            
            status = {
                'service': 'analytics_service',
                'version': '1.0',
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'uptime_seconds': int(time.time() - self.start_time),
                'pid': os.getpid(),
                'channels': {
                    self.channel_name: {
                        'channel_name': self.channel_name,
                        'frequency_hz': self.frequency_hz,
                        'npz_files_processed': self.state.files_processed,
                        'last_processed_file': str(self.state.last_processed_file.name) if self.state.last_processed_file else None,
                        'last_processed_time': datetime.fromtimestamp(self.state.last_processed_time, timezone.utc).isoformat() if self.state.last_processed_time else None,
                        'time_snap': time_snap_dict,
                        'tone_detections': {
                            'wwv': self.stats['wwv_detections'],
                            'wwvh': self.stats['wwvh_detections'],
                            'chu': self.stats['chu_detections'],
                            'total': self.stats['total_detections'],
                            'last_detection_time': self.stats['last_detection_time']
                        },
                        'digital_rf': {
                            'samples_written': self.stats['drf_samples_written'],
                            'files_written': self.stats['drf_files_written'],
                            'last_write_time': self.stats['last_processed_time'],
                            'output_dir': str(self.drf_dir)
                        },
                        'quality_metrics': {
                            'last_completeness_pct': self.stats['last_completeness_pct'],
                            'last_packet_loss_pct': self.stats['last_packet_loss_pct'],
                            'last_snr_db': self.stats['last_snr_db']
                        },
                        'wwvh_discrimination': self._get_discrimination_status()
                    }
                },
                'overall': {
                    'channels_processing': 1,
                    'total_npz_processed': self.state.files_processed,
                    'pending_npz_files': pending_files
                }
            }
            
            # Write atomically
            temp_file = self.status_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            temp_file.replace(self.status_file)
            
        except Exception as e:
            logger.error(f"Failed to write status file: {e}")
    
    def _write_gpsdo_status(self):
        """Write GPSDO monitor status to JSON file for web-ui monitoring.
        
        This writes to a global status file (not per-channel) since the GPSDO
        timing anchor is system-wide. The web-ui uses this to display:
        - Anchor state (STARTUP/STEADY_STATE/HOLDOVER/REANCHOR_REQUIRED)
        - Minutes since anchor established
        - Verification error history
        - Quality flag (LOCKED/HOLDOVER/UNANCHORED)
        """
        try:
            # Get status from GPSDO monitor
            gpsdo_status = self.gpsdo_monitor.get_status()
            
            # Add metadata and format for web-ui
            status = {
                'anchor_state': gpsdo_status['anchor_state'],
                'consecutive_verifications': gpsdo_status['consecutive_verifications'],
                'last_verification_time': gpsdo_status['last_verification_time'],
                'last_verification_error_ms': gpsdo_status['last_verification_error_ms'],
                'best_channel': gpsdo_status['best_channel'],
                'best_station': gpsdo_status['best_station'],
                'holdover_since': gpsdo_status['holdover_since'],
                'total_reanchors': gpsdo_status['total_reanchors'],
                'verification_trend_ms': gpsdo_status['verification_trend_ms'],
                'verification_history': list(self.gpsdo_monitor.state.verification_history),
                'quality_flag': self.gpsdo_monitor.get_quality_flag(),
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'channel_name': self.channel_name,  # Which channel wrote this status
            }
            
            # Write atomically to avoid partial reads by web-ui
            temp_file = self.gpsdo_status_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(status, f, indent=2)
            temp_file.replace(self.gpsdo_status_file)
            
            logger.debug(f"GPSDO status written: state={status['anchor_state']}, "
                        f"verifications={status['consecutive_verifications']}")
            
        except Exception as e:
            logger.error(f"Failed to write GPSDO status file: {e}")
    
    def discover_new_files(self) -> List[Path]:
        """
        Discover new NPZ files to process
        
        Returns:
            List of NPZ file paths, sorted by timestamp in filename (chronological order)
        """
        # Find all NPZ files recursively
        all_files = list(self.archive_dir.rglob('*.npz'))
        
        # Filter to new files (after last processed)
        if self.state.last_processed_file:
            # Use filename comparison instead of mtime to ensure chronological order
            # NPZ files have format: YYYYMMDDTHHMMSSZ_freq_iq.npz
            last_processed_name = self.state.last_processed_file.name
            new_files = [
                f for f in all_files 
                if f.name > last_processed_name  # Lexicographic comparison works for ISO timestamps
            ]
        elif self.state.last_processed_time:
            # Legacy fallback for old state files
            new_files = [
                f for f in all_files 
                if f.stat().st_mtime > self.state.last_processed_time
            ]
        else:
            # First run - process all files
            new_files = all_files
        
        # CRITICAL: Sort by filename to ensure strict chronological order
        # This prevents out-of-order processing that causes DRF sample index conflicts
        new_files = sorted(new_files, key=lambda f: f.name)
        
        logger.info(f"Discovered {len(new_files)} new NPZ files")
        return new_files
    
    def process_archive(self, archive: NPZArchive) -> Dict:
        """
        Process a single NPZ archive through full analytics pipeline
        
        Args:
            archive: Loaded NPZ archive
            
        Returns:
            Dict with processing results
        """
        results = {
            'file': str(archive.file_path),
            'quality_metrics': None,
            'tone_detections': [],
            'time_snap_updated': False,
            'decimated_samples': 0,
            'errors': []
        }
        
        try:
            # Step 1: Calculate quality metrics
            quality = self._calculate_quality_metrics(archive)
            results['quality_metrics'] = quality.to_dict()
            self._write_quality_metrics(archive, quality)
            
            # Step 1.5: GPSDO Sample Integrity Watchdog (Monitor A)
            # If we detect gaps/packet loss, the "steel ruler" is broken and we need to re-anchor
            sample_integrity_ok = self.gpsdo_monitor.check_sample_integrity(quality)
            results['gpsdo_state'] = self.gpsdo_monitor.state.anchor_state.name
            if not sample_integrity_ok:
                # Force re-anchor on next valid minute
                results['gpsdo_reanchor_required'] = True
            
            # Step 2: Calculate timing product ONCE for all consumers
            # Note: _get_timing_annotation() calls _maybe_adopt_archive_time_snap()
            # which extracts the tone-detected time_snap from NPZ metadata
            timing = self._get_timing_annotation(archive)
            results['timing_quality'] = timing.quality.value
            results['timing_annotation'] = {
                'quality': timing.quality.value,
                'time_snap_age_seconds': timing.time_snap_age_seconds,
                'ntp_offset_ms': timing.ntp_offset_ms,
                'reprocessing_recommended': timing.reprocessing_recommended
            }
            
            # Step 2.5: Write timing metrics for web-UI (every 60 seconds)
            current_time = time.time()
            if current_time - self.last_timing_metrics_write >= self.timing_metrics_interval:
                # Use archive's embedded time_snap if available (self-contained reference)
                # This works across RTP session boundaries (core recorder restarts)
                if archive.time_snap_rtp is not None and archive.time_snap_utc is not None:
                    # Create TimeSnapReference from archive's embedded metadata
                    # Use current time for established_at since we're actively using this reference
                    archive_time_snap = TimeSnapReference(
                        rtp_timestamp=archive.time_snap_rtp,
                        utc_timestamp=archive.time_snap_utc,
                        sample_rate=archive.sample_rate,
                        source=archive.time_snap_source or 'unknown',
                        confidence=archive.time_snap_confidence or 0.0,
                        station=archive.time_snap_station or 'unknown',
                        established_at=current_time  # Use current time for freshness classification
                    )
                    
                    # Use archive's stored NTP offset (quality at recording time)
                    ntp_offset = archive.ntp_offset_ms
                    ntp_synced = (ntp_offset is not None and abs(ntp_offset) < 100)  # Synced if < 100ms offset
                    
                    # CRITICAL: Use archive's NTP wall clock time (independent reference)
                    # This is THE KEY to proper drift measurement:
                    # - RTP predicted: time_snap.calculate_time(rtp) = ADC clock says
                    # - NTP wall clock: archive.ntp_wall_clock_time = Reference says
                    # - Drift = NTP - RTP_predicted = ADC clock error
                    if archive.ntp_wall_clock_time is not None:
                        # Archive has independent NTP reference - PERFECT!
                        ppm_result = self.timing_writer.write_snapshot(
                            time_snap=archive_time_snap,
                            current_rtp=archive.rtp_timestamp,
                            current_utc=archive.ntp_wall_clock_time,  # INDEPENDENT NTP reference
                            ntp_offset_ms=ntp_offset,
                            ntp_synced=ntp_synced
                        )
                        
                        # Feed PPM measurement back to time_snap for correction
                        if ppm_result is not None and self.state.time_snap is not None:
                            ppm_offset, ppm_confidence = ppm_result
                            updated_snap = self.state.time_snap.with_updated_ppm(ppm_offset, ppm_confidence)
                            if updated_snap is not self.state.time_snap:
                                self.state.time_snap = updated_snap
                                logger.info(f"📊 PPM correction applied: {ppm_offset:+.2f} ppm "
                                           f"(conf={ppm_confidence:.2f}, clock_ratio={updated_snap.clock_ratio:.8f})")
                            
                            # GPSDO Drift Watchdog (Monitor B)
                            # Check if drift indicates GPSDO unlock
                            self.gpsdo_monitor.check_drift_health(ppm_offset, ppm_confidence)
                        
                        self.last_timing_metrics_write = current_time
                    else:
                        # Old archive format without NTP time - skip drift measurement
                        # (Can still use tone-to-tone for RTP characterization)
                        logger.debug(f"{self.channel_name}: Archive lacks ntp_wall_clock_time - "
                                   f"skipping drift measurement (use tone-to-tone instead)")
                        self.last_timing_metrics_write = current_time
                    
                    # Write propagation status for web-UI (uses receiver grid for mode calculation)
                    self.timing_writer.write_propagation_status(snr_db=self.stats.get('last_snr_db'))
            
            # Step 3: Tone detection (if applicable) - uses timing product
            if self._is_tone_detection_channel(archive.channel_name):
                detections = self._detect_tones(archive, timing)
                results['tone_detections'] = [
                    {
                        'station': det.station.value,
                        'frequency_hz': det.frequency_hz,
                        'timing_error_ms': det.timing_error_ms,
                        'snr_db': det.snr_db,
                        'use_for_time_snap': det.use_for_time_snap
                    }
                    for det in detections
                ]
                
                # Update time_snap if we got a good detection
                if detections:
                    updated = self._update_time_snap(detections, archive)
                    results['time_snap_updated'] = updated
                    
                    # Update detection stats
                    for det in detections:
                        if det.station == StationType.WWV:
                            self.stats['wwv_detections'] += 1
                        elif det.station == StationType.WWVH:
                            self.stats['wwvh_detections'] += 1
                        elif det.station == StationType.CHU:
                            self.stats['chu_detections'] += 1
                    self.stats['total_detections'] = len(self.state.detection_history)
                    self.stats['last_detection_time'] = datetime.now(timezone.utc).isoformat()
                    
                    # Track best SNR from detections
                    best_snr = max(det.snr_db for det in detections)
                    self.stats['last_snr_db'] = best_snr
                    
                    # Write REAL tone detections to CSV (with timing_error_ms!)
                    for det in detections:
                        tone_record = ToneDetectionRecord(
                            timestamp_utc=datetime.fromtimestamp(
                                det.timestamp_utc, tz=timezone.utc
                            ).isoformat(),
                            station=det.station.value,
                            frequency_hz=det.frequency_hz,
                            duration_sec=det.duration_sec,
                            timing_error_ms=det.timing_error_ms,  # ACTUAL timing!
                            snr_db=det.snr_db,
                            tone_power_db=det.tone_power_db if det.tone_power_db else det.snr_db,
                            confidence=det.confidence,
                            use_for_time_snap=det.use_for_time_snap
                        )
                        self.csv_writers.write_tone_detection(tone_record)
            
            # Step 4: Decimate and write 10Hz NPZ (for DRF writer + spectrogram generation)
            decimated_samples = self._write_decimated_npz(archive, timing, detections if self._is_tone_detection_channel(archive.channel_name) else [])
            results['decimated_samples'] = decimated_samples
            
            # Stats tracking for DRF handled by drf_writer_service
            
            # Update quality stats
            self.stats['last_completeness_pct'] = quality.completeness_pct
            self.stats['last_packet_loss_pct'] = quality.packet_loss_pct
            self.stats['last_processed_file'] = str(archive.file_path.name)
            self.stats['last_processed_time'] = datetime.now(timezone.utc).isoformat()
            
            # Step 5: Write discontinuity log
            self._write_discontinuity_log(archive, quality)
            
        except Exception as e:
            logger.error(f"Error processing {archive.file_path}: {e}", exc_info=True)
            results['errors'].append(str(e))
        
        return results
    
    def _calculate_quality_metrics(self, archive: NPZArchive) -> QualityInfo:
        """
        Calculate quality metrics from archive data
        
        Args:
            archive: Loaded NPZ archive
            
        Returns:
            QualityInfo with calculated metrics
        """
        total_samples = len(archive.iq_samples)
        
        # Completeness
        completeness_pct = 100.0 * (total_samples - archive.gaps_filled) / total_samples if total_samples > 0 else 0.0
        
        # Packet loss
        packet_loss_pct = 100.0 * (archive.packets_expected - archive.packets_received) / archive.packets_expected if archive.packets_expected > 0 else 0.0
        
        # Gap duration in ms
        gap_duration_ms = (archive.gaps_filled / archive.sample_rate) * 1000.0
        
        # Build discontinuity list
        discontinuities = []
        for i in range(len(archive.gap_rtp_timestamps)):
            disc = Discontinuity(
                timestamp=archive.unix_timestamp,  # Approximate
                sample_index=int(archive.gap_sample_indices[i]),
                discontinuity_type=DiscontinuityType.GAP,
                magnitude_samples=int(archive.gap_samples_filled[i]),
                magnitude_ms=(archive.gap_samples_filled[i] / archive.sample_rate) * 1000.0,
                rtp_sequence_before=None,
                rtp_sequence_after=None,
                rtp_timestamp_before=int(archive.gap_rtp_timestamps[i]),
                rtp_timestamp_after=int(archive.gap_rtp_timestamps[i] + archive.gap_samples_filled[i]),
                wwv_related=False,
                explanation=f"RTP packet loss: {archive.gap_packets_lost[i]} packets"
            )
            discontinuities.append(disc)
        
        quality = QualityInfo(
            completeness_pct=completeness_pct,
            gap_count=archive.gaps_count,
            gap_duration_ms=gap_duration_ms,
            packet_loss_pct=packet_loss_pct,
            resequenced_count=0,  # Not tracked in NPZ
            time_snap_established=self.state.time_snap is not None,
            time_snap_confidence=self.state.time_snap.confidence if self.state.time_snap else 0.0,
            discontinuities=discontinuities,
            network_gap_ms=gap_duration_ms,
            source_failure_ms=0.0,
            recorder_offline_ms=0.0
        )
        
        return quality
    
    def _detect_tones(self, archive: NPZArchive, timing: TimingAnnotation) -> List[ToneDetectionResult]:
        """
        Run WWV/CHU/WWVH tone detection on archive using provided timing product
        
        Combines tail of previous file with head of current file to create
        a buffer spanning the minute boundary where WWV/CHU tones occur.
        
        Args:
            archive: Loaded NPZ archive
            timing: Timing annotation with UTC timestamp and quality level
            
        Returns:
            List of detected tones
        """
        logger.info(f"⏱️ _detect_tones called for {archive.file_path.name}")
        
        # Create detection buffer spanning minute boundary
        # Tone occurs AT the boundary between files
        if self.previous_file_tail is not None:
            # Combine: tail of previous (30s) + head of current (30s)
            # This creates a 60s buffer with the minute boundary in the middle
            current_head_samples = 30 * archive.sample_rate  # 30 seconds
            current_head = archive.iq_samples[:current_head_samples]
            
            # Concatenate
            combined_iq = np.concatenate([self.previous_file_tail, current_head])
            
            # RTP timestamp of the combined buffer start (from previous file tail)
            buffer_rtp_start = self.previous_file_rtp_end - len(self.previous_file_tail)
            
            logger.info(f"Cross-file buffer: {len(self.previous_file_tail)} (prev) + {len(current_head)} (curr) = {len(combined_iq)} samples")
        else:
            # First file - just use current file
            # Tone detection may fail, but that's OK for the first file
            combined_iq = archive.iq_samples
            buffer_rtp_start = archive.rtp_timestamp
            logger.info(f"First file - using full archive ({len(combined_iq)} samples), tone detection may not work")
        
        # Store tail of current file for next iteration (last 30 seconds)
        tail_samples = 30 * archive.sample_rate
        self.previous_file_tail = archive.iq_samples[-tail_samples:]
        self.previous_file_rtp_end = archive.rtp_timestamp + len(archive.iq_samples)
        
        # Resample to 3 kHz for tone detection
        # Original rate is archive.sample_rate (typically 20 kHz)
        original_sample_rate = archive.sample_rate
        target_rate = 3000
        
        # Calculate resampling ratio (need integer up/down)
        from math import gcd
        g = gcd(target_rate, original_sample_rate)
        up = target_rate // g
        down = original_sample_rate // g
        
        try:
            resampled = scipy_signal.resample_poly(
                combined_iq,
                up=up,
                down=down,
                axis=0
            )
        except Exception as e:
            logger.error(f"Resampling failed: {e}")
            return []
        
        detection_buffer = resampled
        
        # Calculate UTC timestamp for the buffer using provided timing product
        # The buffer spans from previous file tail to current file head
        # Minute boundary (where tone occurs) should be at the MIDDLE of this buffer
        
        # Archive UTC is from timing product (may be TONE_LOCKED, NTP_SYNCED, or WALL_CLOCK)
        archive_utc = timing.utc_timestamp
        
        # If we have previous file, buffer starts 30s before current file
        if self.previous_file_rtp_end is not None:
            buffer_start_utc = archive_utc - 30.0
        else:
            # First file - buffer starts at file start
            buffer_start_utc = archive_utc
        
        # CRITICAL: Tone detector expects timestamp to be the MIDDLE of the buffer
        # It calculates buffer_start internally as: timestamp - (buffer_len / 2)
        buffer_duration_sec = len(detection_buffer) / 3000  # 3 kHz sample rate
        buffer_middle_utc = buffer_start_utc + (buffer_duration_sec / 2)
        
        # Run tone detection on combined buffer
        logger.info(f"Tone detection: buffer_len={len(detection_buffer)} samples ({buffer_duration_sec:.1f}s), "
                    f"buffer_start={buffer_start_utc:.2f}, buffer_middle={buffer_middle_utc:.2f} (expected tone)")
        
        detections = self.tone_detector.process_samples(
            timestamp=buffer_middle_utc,  # MUST be buffer middle (tone detector calculates start)
            samples=detection_buffer,
            rtp_timestamp=archive.rtp_timestamp,
            original_sample_rate=original_sample_rate,
            buffer_rtp_start=buffer_rtp_start
        )
        
        if detections:
            logger.info(f"Detected {len(detections)} tones in {archive.file_path.name}")
            for det in detections:
                logger.info(f"  {det.station.value}: {det.timing_error_ms:+.1f}ms, "
                           f"SNR={det.snr_db:.1f}dB, use_for_time_snap={det.use_for_time_snap}")
        
        self._compare_recorder_tones(archive, detections)
        
        # Write detections to shared file for global cross-channel solving
        if self.global_coordinator and detections:
            # Use archive.unix_timestamp (correct wall clock) to determine minute
            minute_utc = datetime.fromtimestamp(
                int(archive.unix_timestamp / 60) * 60, tz=timezone.utc
            )
            
            for det in detections:
                if det.use_for_time_snap:
                    try:
                        self.global_coordinator.write_detection(
                            minute_utc=minute_utc,
                            channel=self.channel_name,
                            station=det.station.value,
                            frequency_mhz=archive.frequency_hz / 1e6,
                            timing_error_ms=det.timing_error_ms,
                            snr_db=det.snr_db
                        )
                        logger.debug(f"Wrote detection to shared: {det.station.value} @ {det.timing_error_ms:+.2f}ms")
                    except Exception as e:
                        logger.warning(f"Failed to write detection to shared file: {e}")
            
            # Try to run global solve (if we're the last channel to report)
            try:
                result = self.global_coordinator.solve_and_save(minute_utc)
                if result and result.verified:
                    logger.info(f"🌐 GLOBAL: {result.n_channels} channels → "
                               f"clock_error={result.clock_error_ms:+.3f}ms, "
                               f"confidence={result.confidence:.0%}")
            except Exception as e:
                logger.debug(f"Global solve not ready: {e}")
        
        # Run WWV/H discrimination if this is a dual-station channel
        if self.wwvh_discriminator:
            # Get minute timestamp from timing product
            minute_timestamp = int(timing.utc_timestamp / 60) * 60
            
            # Run FULL discrimination analysis including 440 Hz detection
            # This requires the complete minute of IQ samples from the archive
            frequency_mhz = archive.frequency_hz / 1e6  # Convert Hz to MHz for geographic predictor
            discrimination = self.wwvh_discriminator.analyze_minute_with_440hz(
                iq_samples=archive.iq_samples,
                sample_rate=archive.sample_rate,
                minute_timestamp=minute_timestamp,
                frequency_mhz=frequency_mhz,
                detections=detections
            )
            
            # Log discrimination result (now includes 440 Hz info)
            self._log_discrimination(discrimination)
            
            # Log if both detected (detailed logging in discriminator)
            if discrimination.wwv_detected and discrimination.wwvh_detected:
                power_str = f"{discrimination.power_ratio_db:+.1f}dB" if discrimination.power_ratio_db is not None else "N/A"
                delay_str = f"{discrimination.differential_delay_ms:+.1f}ms" if discrimination.differential_delay_ms is not None else "N/A"
                logger.info(f"Discrimination: Power ratio={power_str}, "
                           f"Delay={delay_str}, "
                           f"Dominant={discrimination.dominant_station}")
            
            # Back-calculate UTC(NIST) transmission time if solver available
            if self.transmission_solver and discrimination.dominant_station:
                self._solve_transmission_time(
                    archive=archive,
                    discrimination=discrimination,
                    detections=detections
                )
        
        return detections or []

    def _compare_recorder_tones(self, archive: NPZArchive, detections: List[ToneDetectionResult]):
        """Cross-check recorder startup tone powers with analytics detections."""
        recorder_snapshot = archive.startup_tone_snapshot()
        if not recorder_snapshot or not detections:
            return

        detections_by_freq = {}
        for det in detections:
            freq_key = int(round(det.frequency_hz))
            detections_by_freq[freq_key] = det

        for freq_hz, key in [(1000, 'tone_power_1000_hz_db'), (1200, 'tone_power_1200_hz_db')]:
            recorder_power = recorder_snapshot.get(key)
            if recorder_power is None or recorder_power <= -998:
                continue
            detection = detections_by_freq.get(freq_hz)
            if detection:
                delta = detection.snr_db - recorder_power
                logger.debug(
                    f"Startup tone check {freq_hz}Hz: recorder={recorder_power:.1f}dB, "
                    f"analytics={detection.snr_db:.1f}dB, delta={delta:+.1f}dB"
                )
            else:
                logger.debug(
                    f"Startup tone {freq_hz}Hz present in metadata ({recorder_power:.1f}dB) "
                    "but not detected in analytics buffer"
                )
    
    def _update_time_snap(
        self,
        detections: List[ToneDetectionResult],
        archive: NPZArchive
    ) -> bool:
        """
        Update time_snap reference from tone detections
        
        GPSDO MONITORING ARCHITECTURE:
        - In STARTUP/REANCHOR state: Establish anchor normally
        - In STEADY_STATE: Verify projection, only update if physics requires it
        
        Args:
            detections: List of detected tones
            archive: Source archive
            
        Returns:
            True if time_snap was updated
        """
        # Find best time_snap-eligible detection
        # Priority: WWV/CHU (direct) > WWVH (after back-calculation)
        eligible = [d for d in detections if d.use_for_time_snap]
        
        if not eligible:
            return False
        
        # Prioritize by station type, then SNR within each tier
        # Tier 1: WWV, CHU (direct UTC(NIST) references)
        # Tier 2: WWVH (valid after propagation delay subtraction)
        primary = [d for d in eligible if d.station in [StationType.WWV, StationType.CHU]]
        secondary = [d for d in eligible if d.station == StationType.WWVH]
        
        if primary:
            best = max(primary, key=lambda d: d.snr_db)
        elif secondary:
            # Use WWVH only if no WWV/CHU available
            best = max(secondary, key=lambda d: d.snr_db)
            logger.info(f"Using WWVH for time_snap (no WWV/CHU available) - "
                       f"propagation delay back-calculation required")
        else:
            return False
        
        # Find minute boundary closest to detection
        minute_boundary = int(archive.unix_timestamp / 60) * 60
        
        # ========================================================================
        # GPSDO MONITORING: Check if we're in verification mode or anchor mode
        # ========================================================================
        gpsdo_anchor_state = self.gpsdo_monitor.state.anchor_state
        
        if gpsdo_anchor_state == AnchorState.STEADY_STATE and self.state.time_snap is not None:
            # STEADY STATE: Verify projection, don't re-anchor unless physics requires it
            verification = self.gpsdo_monitor.verify_projection(
                best, self.state.time_snap, minute_boundary
            )
            
            # Check if verification requires re-anchor
            if not verification.requires_reanchor:
                # Trust the counter - don't update time_snap
                logger.debug(f"🔒 GPSDO: Verification OK ({verification.error_ms:+.2f}ms), "
                           f"trusting counter (verifications={self.gpsdo_monitor.state.consecutive_verifications})")
                return False
            else:
                logger.warning(f"⚠️ GPSDO: Verification failed ({verification.error_ms:+.1f}ms), "
                              f"forcing re-anchor")
                # Fall through to anchor establishment
        
        # ========================================================================
        # ANCHOR ESTABLISHMENT (STARTUP, REANCHOR, or verification failure)
        # ========================================================================
        
        # Calculate RTP timestamp at minute boundary using ACTUAL detected sample position
        if best.sample_position_original is not None and best.buffer_rtp_start is not None:
            # PRECISE METHOD: Use actual detected sample position and buffer RTP start
            rtp_at_tone = best.buffer_rtp_start + best.sample_position_original
            timing_offset_samples = int(best.timing_error_ms * archive.sample_rate / 1000)
            rtp_at_minute = rtp_at_tone - timing_offset_samples
            
            logger.info(f"Time snap: PRECISE - tone at RTP {rtp_at_tone}, "
                       f"timing_error={best.timing_error_ms:+.1f}ms, "
                       f"minute boundary at RTP {rtp_at_minute}")
        else:
            # FALLBACK: Use UTC-based calculation (less precise)
            samples_from_archive_to_minute = (minute_boundary - archive.unix_timestamp) * archive.sample_rate
            rtp_at_minute = int(archive.rtp_timestamp + samples_from_archive_to_minute)
            logger.info(f"Time snap: UTC-based (no precise sample position available)")
        
        # Create new time_snap reference
        new_time_snap = TimeSnapReference(
            rtp_timestamp=rtp_at_minute,
            utc_timestamp=float(minute_boundary),
            sample_rate=archive.sample_rate,
            source=f"{best.station.value.lower()}_verified",
            confidence=best.confidence,
            station=best.station,
            established_at=time.time()
        )
        
        # Check if this is a significant update (clock drift detection)
        if self.state.time_snap:
            utc_elapsed = new_time_snap.utc_timestamp - self.state.time_snap.utc_timestamp
            
            if utc_elapsed > 0:
                expected_rtp_delta = utc_elapsed * archive.sample_rate
                actual_rtp_delta = new_time_snap.rtp_timestamp - self.state.time_snap.rtp_timestamp
                
                rtp_drift_samples = abs(actual_rtp_delta - expected_rtp_delta)
                drift_ms = (rtp_drift_samples / archive.sample_rate) * 1000
                
                if drift_ms > 50.0 and utc_elapsed > 30:
                    logger.warning(f"RTP clock drift detected: {drift_ms:.1f}ms over {utc_elapsed:.0f}s")
                elif drift_ms > 5.0:
                    logger.debug(f"Minor RTP adjustment: {drift_ms:.1f}ms over {utc_elapsed:.0f}s")
        
        # Update state
        old_time_snap = self.state.time_snap
        self._store_time_snap(new_time_snap)
        
        # Notify GPSDO monitor that anchor is established
        self.gpsdo_monitor.establish_anchor(
            new_time_snap, 
            best,
            self.channel_name
        )
        
        logger.info(f"Time snap {'updated' if old_time_snap else 'established'}: "
                   f"{best.station.value}, confidence={best.confidence:.2f}, "
                   f"timing_error={best.timing_error_ms:+.1f}ms")
        
        # If this is the FIRST time_snap, log milestone
        if not old_time_snap:
            logger.info("✅ TIME_SNAP ESTABLISHED - Tone-locked timestamps now available")
            logger.info("   GPSDO mode: Now trusting the counter, verifying with tones")
        
        return True
    
    def _validate_ntp_sync(self) -> Tuple[bool, Optional[float]]:
        """
        Check if system clock is NTP-synchronized with acceptable quality
        
        Returns:
            (is_synced, offset_ms): True if good sync, and offset in milliseconds
        """
        try:
            # Try ntpq first (ntpd)
            result = subprocess.run(
                ['ntpq', '-c', 'rv'],
                capture_output=True, text=True, timeout=2
            )
            
            if result.returncode == 0:
                offset_ms = None
                stratum = None
                
                for line in result.stdout.split(','):
                    if 'offset=' in line:
                        offset_ms = float(line.split('offset=')[1].strip())
                    if 'stratum=' in line:
                        stratum = int(line.split('stratum=')[1].strip())
                
                if offset_ms is not None and stratum is not None:
                    if abs(offset_ms) > 100:  # >100ms offset is poor
                        logger.warning(f"NTP offset too large: {offset_ms:.1f}ms")
                        return False, offset_ms
                    if stratum > 4:  # Stratum 5+ is questionable
                        logger.warning(f"NTP stratum too high: {stratum}")
                        return False, offset_ms
                    
                    logger.debug(f"NTP sync good: offset={offset_ms:.1f}ms, stratum={stratum}")
                    return True, offset_ms
            
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        try:
            # Try chronyc (chrony)
            result = subprocess.run(
                ['chronyc', 'tracking'],
                capture_output=True, text=True, timeout=2
            )
            
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'System time' in line:
                        # Parse: "System time     : 0.000123456 seconds slow of NTP time"
                        parts = line.split(':')[1].strip().split()
                        offset_sec = float(parts[0])
                        offset_ms = offset_sec * 1000
                        
                        if abs(offset_ms) > 100:
                            logger.warning(f"Chrony offset too large: {offset_ms:.1f}ms")
                            return False, offset_ms
                        
                        logger.debug(f"Chrony sync good: offset={offset_ms:.1f}ms")
                        return True, offset_ms
                        
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        logger.debug("No NTP sync detected (ntpq/chronyc not available)")
        return False, None
    
    def _get_timing_annotation(self, archive: 'NPZArchive') -> TimingAnnotation:
        """
        Determine timing quality for this archive and create annotation
        
        Timing hierarchy for 16 kHz channels:
        1. time_snap from WWV/CHU tone (best: ±1ms)
        2. NTP-synchronized system clock (good: ±10ms)
        3. Unsynchronized wall clock (fallback: ±seconds)
        
        Always returns an annotation - never skips upload.
        
        Returns:
            TimingAnnotation with quality level and metadata
        """
        current_time = time.time()
        time_snap = self._maybe_adopt_archive_time_snap(archive)
        if time_snap:
            try:
                # Calculate age of time_snap
                age_reference = getattr(time_snap, 'established_at', None) or time_snap.utc_timestamp
                time_snap_age = current_time - age_reference
                utc_timestamp = archive.calculate_utc_timestamp(time_snap)
                
                if time_snap_age < 300:  # < 5 minutes
                    return TimingAnnotation(
                        quality=TimingQuality.TONE_LOCKED,
                        utc_timestamp=utc_timestamp,
                        time_snap_age_seconds=time_snap_age,
                        reprocessing_recommended=False,
                        notes=f"WWV/CHU time_snap from {time_snap.station.value}"
                    )
                # Aged time_snap (>5 min): fall through to NTP/wall clock
            except (ValueError, AttributeError) as e:
                logger.warning(f"Error using time_snap for timing annotation: {e}, falling back to NTP/wall clock")
                # Fall through to NTP/wall clock check
        
        # No recent time_snap - check NTP
        ntp_synced, ntp_offset = self._validate_ntp_sync()
        utc_timestamp = archive.calculate_utc_timestamp(None)
        
        if ntp_synced:
            return TimingAnnotation(
                quality=TimingQuality.NTP_SYNCED,
                utc_timestamp=utc_timestamp,
                ntp_offset_ms=ntp_offset,
                reprocessing_recommended=False,
                notes="System clock NTP-synchronized"
            )
        
        # Fallback: Unsynchronized wall clock (mark for reprocessing)
        return TimingAnnotation(
            quality=TimingQuality.WALL_CLOCK,
            utc_timestamp=utc_timestamp,
            reprocessing_recommended=True,
            notes="Wall clock only - reprocessing recommended when time_snap available"
        )
    
    # Digital RF writing methods removed - moved to drf_writer_service.py
    # That service reads *_iq_10hz.npz files and writes Digital RF HDF5
    
    def _write_decimated_npz(self, archive: NPZArchive, timing: TimingAnnotation, 
                             detections: List[ToneDetectionResult]) -> int:
        """
        DEPRECATED: Decimation moved to grape-recorder package.
        
        This method is a no-op stub. For decimation and 10 Hz output,
        use the grape-recorder package: https://github.com/mijahauan/grape-recorder
        
        Args:
            archive: Source NPZ archive (unused)
            timing: Timing annotation (unused)
            detections: Tone detection results (unused)
            
        Returns:
            0 (no samples written)
        """
        logger.debug("Decimation disabled - use grape-recorder package for 10 Hz output")
        return 0
    
    def _write_quality_metrics(self, archive: NPZArchive, quality: QualityInfo):
        """Write quality metrics to CSV file"""
        csv_file = self.quality_dir / f"{self.channel_name.replace(' ', '_')}_quality.csv"
        
        # Create CSV if it doesn't exist
        if not csv_file.exists():
            with open(csv_file, 'w') as f:
                f.write("timestamp,file,completeness_pct,gap_count,gap_duration_ms,"
                       "packet_loss_pct,time_snap_established,time_snap_confidence\n")
        
        # Append metrics
        with open(csv_file, 'a') as f:
            f.write(f"{archive.unix_timestamp},{archive.file_path.name},"
                   f"{quality.completeness_pct:.2f},{quality.gap_count},"
                   f"{quality.gap_duration_ms:.2f},{quality.packet_loss_pct:.2f},"
                   f"{quality.time_snap_established},{quality.time_snap_confidence:.3f}\n")
    
    def _write_discontinuity_log(self, archive: NPZArchive, quality: QualityInfo):
        """Write discontinuity log for scientific provenance"""
        if quality.gap_count == 0:
            return
        
        log_file = self.logs_dir / f"{self.channel_name.replace(' ', '_')}_discontinuities.log"
        
        with open(log_file, 'a') as f:
            f.write(f"\n{'='*80}\n")
            f.write(f"File: {archive.file_path.name}\n")
            f.write(f"Timestamp: {datetime.fromtimestamp(archive.unix_timestamp, tz=timezone.utc).isoformat()}\n")
            f.write(f"Total gaps: {quality.gap_count}, Total duration: {quality.gap_duration_ms:.2f}ms\n")
            f.write(f"{'='*80}\n\n")
            
            for disc in quality.discontinuities:
                f.write(f"  Gap at sample {disc.sample_index}: "
                       f"{disc.magnitude_samples} samples ({disc.magnitude_ms:.2f}ms)\n")
                f.write(f"    RTP timestamp: {disc.rtp_timestamp_before} → {disc.rtp_timestamp_after}\n")
                f.write(f"    Explanation: {disc.explanation}\n\n")
    
    def _is_tone_detection_channel(self, channel_name: str) -> bool:
        """Check if channel is capable of tone detection
        
        Args:
            channel_name: Channel identifier
            
        Returns:
            True if WWV/CHU/WWVH channel, False otherwise
        """
        # Must be WWV/CHU/WWVH frequency
        tone_keywords = ['WWV', 'CHU', 'WWVH']
        result = any(kw in channel_name.upper() for kw in tone_keywords)
        logger.debug(f"🔍 Tone detection check for '{channel_name}': {result}")
        return result
    
    def _is_wwv_wwvh_channel(self, channel_name: str) -> bool:
        """
        Check if channel can receive both WWV and WWVH
        
        WWV and WWVH share frequencies: 2.5, 5, 10, 15 MHz
        """
        freq_mhz = self.frequency_hz / 1e6
        shared_freqs = [2.5, 5.0, 10.0, 15.0]
        return any(abs(freq_mhz - f) < 0.1 for f in shared_freqs)
    
    def _log_discrimination(self, result: 'DiscriminationResult'):
        """
        Log discrimination results to separate CSV files per method
        
        Writes to 5 separate files:
        1. Tone detections (1000/1200 Hz power analysis)
        2. Tick windows (10-sec coherent integration)
        3. Station ID (440 Hz identification)
        4. BCD discrimination (100 Hz time code)
        5. Final weighted voting (dominant station)
        """
        try:
            dt = datetime.fromtimestamp(result.minute_timestamp, timezone.utc)
            timestamp_utc = dt.isoformat()
            
            # 1. Write 1000/1200 Hz tone power data (if available)
            if result.wwv_power_db is not None or result.wwvh_power_db is not None:
                # Create records for each detected station
                if result.wwv_detected and result.wwv_power_db is not None:
                    tone_record = ToneDetectionRecord(
                        timestamp_utc=timestamp_utc,
                        station='WWV',
                        frequency_hz=1000.0,
                        duration_sec=0.8,  # 800ms tones
                        timing_error_ms=0.0,  # Not tracked at discrimination level
                        snr_db=result.wwv_power_db,  # Using power as proxy for SNR
                        tone_power_db=result.wwv_power_db,
                        confidence=1.0 if result.confidence == 'high' else 0.5,
                        use_for_time_snap=False  # Discrimination-level, not individual tone
                    )
                    self.csv_writers.write_tone_detection(tone_record)
                
                if result.wwvh_detected and result.wwvh_power_db is not None:
                    tone_record = ToneDetectionRecord(
                        timestamp_utc=timestamp_utc,
                        station='WWVH',
                        frequency_hz=1200.0,
                        duration_sec=0.8,
                        timing_error_ms=0.0,
                        snr_db=result.wwvh_power_db,
                        tone_power_db=result.wwvh_power_db,
                        confidence=1.0 if result.confidence == 'high' else 0.5,
                        use_for_time_snap=False
                    )
                    self.csv_writers.write_tone_detection(tone_record)
            
            # 2. Write tick windows data (if available)
            if result.tick_windows_10sec:
                self.csv_writers.write_tick_windows(timestamp_utc, result.tick_windows_10sec)
            
            # 3. Write 440 Hz station ID data (if detected)
            if result.tone_440hz_wwv_detected or result.tone_440hz_wwvh_detected:
                id_record = StationID440HzRecord(
                    timestamp_utc=timestamp_utc,
                    minute_number=dt.minute,
                    wwv_detected=result.tone_440hz_wwv_detected,
                    wwvh_detected=result.tone_440hz_wwvh_detected,
                    wwv_power_db=result.tone_440hz_wwv_power_db,
                    wwvh_power_db=result.tone_440hz_wwvh_power_db
                )
                self.csv_writers.write_440hz_detection(id_record)
            
            # 3.5. Write test signal detection data (minutes 8 and 44)
            # Note: Test signal is IDENTICAL for WWV/WWVH - station from SCHEDULE
            # Value is high-gain ToA and SNR measurement for channel characterization
            if result.test_signal_detected or dt.minute in [8, 44]:
                test_signal_record = TestSignalRecord(
                    timestamp_utc=timestamp_utc,
                    minute_number=dt.minute,
                    detected=result.test_signal_detected,
                    station=result.test_signal_station,
                    confidence=result.test_signal_confidence or 0.0,
                    multitone_score=result.test_signal_multitone_score or 0.0,
                    chirp_score=result.test_signal_chirp_score or 0.0,
                    noise_correlation=result.test_signal_noise_correlation or 0.0,
                    snr_db=result.test_signal_snr_db,
                    toa_offset_ms=result.test_signal_toa_offset_ms,
                    burst_toa_offset_ms=result.test_signal_burst_toa_offset_ms,
                    delay_spread_ms=result.test_signal_delay_spread_ms,
                    coherence_time_sec=result.test_signal_coherence_time_sec,
                    frequency_selectivity_db=result.test_signal_frequency_selectivity_db,
                    noise1_score=result.test_signal_noise1_score,
                    noise2_score=result.test_signal_noise2_score,
                    noise_coherence_diff=result.test_signal_noise_coherence_diff
                )
                self.csv_writers.write_test_signal(test_signal_record)
            
            # 4. Write BCD discrimination windows (if available)
            if result.bcd_windows:
                self.csv_writers.write_bcd_windows(timestamp_utc, result.bcd_windows)
            
            # 5. Write Doppler estimation (ionospheric channel characterization)
            if result.doppler_wwv_hz is not None or result.doppler_wwvh_hz is not None:
                doppler_record = DopplerRecord(
                    timestamp_utc=timestamp_utc,
                    wwv_doppler_hz=result.doppler_wwv_hz or 0.0,
                    wwvh_doppler_hz=result.doppler_wwvh_hz or 0.0,
                    wwv_doppler_std_hz=result.doppler_wwv_std_hz or 0.0,
                    wwvh_doppler_std_hz=result.doppler_wwvh_std_hz or 0.0,
                    max_coherent_window_sec=result.doppler_max_coherent_window_sec or 60.0,
                    doppler_quality=result.doppler_quality or 0.0,
                    phase_variance_rad=result.doppler_phase_variance_rad or 0.0,
                    valid_tick_count=result.doppler_valid_tick_count or 0
                )
                self.csv_writers.write_doppler_record(doppler_record)
            
            # 6. Write final weighted voting result - comprehensive format
            if result.dominant_station:
                import json
                
                # Serialize tick windows to JSON
                tick_windows_json = json.dumps(result.tick_windows_10sec) if result.tick_windows_10sec else None
                
                # Serialize BCD windows to JSON  
                bcd_windows_json = json.dumps(result.bcd_windows) if result.bcd_windows else None
                
                # Serialize inter-method validation
                agreements_json = json.dumps(result.inter_method_agreements) if result.inter_method_agreements else None
                disagreements_json = json.dumps(result.inter_method_disagreements) if result.inter_method_disagreements else None
                
                disc_record = DiscriminationRecord(
                    timestamp_utc=timestamp_utc,
                    minute_timestamp=result.minute_timestamp,
                    minute_number=dt.minute,
                    # Timing tones
                    wwv_detected=result.wwv_detected,
                    wwvh_detected=result.wwvh_detected,
                    wwv_power_db=result.wwv_power_db,
                    wwvh_power_db=result.wwvh_power_db,
                    power_ratio_db=result.power_ratio_db,
                    differential_delay_ms=result.differential_delay_ms,
                    # 440 Hz station ID
                    tone_440hz_wwv_detected=result.tone_440hz_wwv_detected,
                    tone_440hz_wwv_power_db=result.tone_440hz_wwv_power_db,
                    tone_440hz_wwvh_detected=result.tone_440hz_wwvh_detected,
                    tone_440hz_wwvh_power_db=result.tone_440hz_wwvh_power_db,
                    # Final decision
                    dominant_station=result.dominant_station,
                    confidence=result.confidence,
                    # Tick windows
                    tick_windows_10sec=tick_windows_json,
                    # BCD discrimination
                    bcd_wwv_amplitude=result.bcd_wwv_amplitude,
                    bcd_wwvh_amplitude=result.bcd_wwvh_amplitude,
                    bcd_differential_delay_ms=result.bcd_differential_delay_ms,
                    bcd_correlation_quality=result.bcd_correlation_quality,
                    bcd_windows=bcd_windows_json,
                    # 500/600 Hz ground truth
                    tone_500_600_detected=result.tone_500_600_detected,
                    tone_500_600_power_db=result.tone_500_600_power_db,
                    tone_500_600_freq_hz=result.tone_500_600_freq_hz,
                    tone_500_600_ground_truth_station=result.tone_500_600_ground_truth_station,
                    # Harmonic power ratios
                    harmonic_ratio_500_1000=result.harmonic_ratio_500_1000,
                    harmonic_ratio_600_1200=result.harmonic_ratio_600_1200,
                    # BCD validation
                    bcd_minute_validated=result.bcd_minute_validated,
                    bcd_correlation_peak_quality=result.bcd_correlation_peak_quality,
                    # Inter-method cross-validation
                    inter_method_agreements=agreements_json,
                    inter_method_disagreements=disagreements_json
                )
                self.csv_writers.write_discrimination_result(disc_record)
                       
        except Exception as e:
            logger.warning(f"Failed to log discrimination: {e}")
    
    def _solve_transmission_time(
        self,
        archive: 'NPZArchive',
        discrimination: 'DiscriminationResult',
        detections: List[ToneDetectionResult]
    ):
        """
        Back-calculate UTC(NIST) transmission time from observed arrival.
        
        CRITICAL INSIGHT: Using differential WWV-WWVH measurements eliminates
        clock error contamination. The key equation:
        
            Δ_observed = timing_wwv - timing_wwvh
                       = (T_prop_wwv + clock_error) - (T_prop_wwvh + clock_error)
                       = T_prop_wwv - T_prop_wwvh  ← Clock error CANCELS!
        
        Strategy:
        1. If BOTH WWV and WWVH detected with RTP positions → use differential solver
        2. Use differential to identify mode pair (clock-error immune)
        3. Derive clock error from identified propagation delays
        4. Cross-validate: WWV and WWVH should give same clock error
        
        Args:
            archive: NPZ archive with RTP timestamps
            discrimination: Discrimination result with station identification
            detections: Tone detection results with timing info
        """
        try:
            frequency_mhz = archive.frequency_hz / 1e6
            
            # Extract WWV and WWVH detections separately
            wwv_det = None
            wwvh_det = None
            for det in detections or []:
                if det.station == StationType.WWV:
                    if wwv_det is None or det.snr_db > wwv_det.snr_db:
                        wwv_det = det
                elif det.station == StationType.WWVH:
                    if wwvh_det is None or det.snr_db > wwvh_det.snr_db:
                        wwvh_det = det
            
            # Check if we can use differential approach (PREFERRED)
            can_use_differential = (
                self.differential_solver is not None and
                wwv_det is not None and
                wwvh_det is not None and
                wwv_det.sample_position_original is not None and
                wwvh_det.sample_position_original is not None and
                wwv_det.buffer_rtp_start is not None and
                wwvh_det.buffer_rtp_start is not None
            )
            
            if can_use_differential:
                self._solve_differential(archive, wwv_det, wwvh_det, discrimination, frequency_mhz)
            else:
                # Fallback to single-station approach (contaminated by clock error)
                self._solve_single_station(archive, discrimination, detections, frequency_mhz)
                
        except Exception as e:
            logger.warning(f"Transmission time solving failed: {e}", exc_info=True)
    
    def _solve_differential(
        self,
        archive: 'NPZArchive',
        wwv_det: ToneDetectionResult,
        wwvh_det: ToneDetectionResult,
        discrimination: 'DiscriminationResult',
        frequency_mhz: float
    ):
        """
        Differential approach: Use WWV-WWVH difference for clock-error-immune solving.
        
        This is the CORRECT approach because the differential measurement
        cancels out local clock error entirely.
        """
        # Calculate RTP arrival positions
        wwv_arrival_rtp = wwv_det.buffer_rtp_start + wwv_det.sample_position_original
        wwvh_arrival_rtp = wwvh_det.buffer_rtp_start + wwvh_det.sample_position_original
        
        # The differential in RTP samples
        differential_samples = wwv_arrival_rtp - wwvh_arrival_rtp
        differential_ms = (differential_samples / archive.sample_rate) * 1000
        
        logger.info(
            f"🔬 DIFFERENTIAL solve: {frequency_mhz:.1f}MHz | "
            f"WWV RTP={wwv_arrival_rtp}, WWVH RTP={wwvh_arrival_rtp}, "
            f"Δ={differential_ms:+.3f}ms"
        )
        
        # Calculate minute boundary RTP from archive
        # This is derived from the archive's time_snap, but we use it only
        # as a reference point - the differential is what matters
        minute_boundary_rtp = archive.rtp_timestamp
        
        # Get quality metrics
        delay_spread_ms = 0.5
        doppler_std_hz = 0.1
        if discrimination.doppler_wwv_std_hz is not None:
            doppler_std_hz = max(doppler_std_hz, discrimination.doppler_wwv_std_hz)
        if discrimination.doppler_wwvh_std_hz is not None:
            doppler_std_hz = max(doppler_std_hz, discrimination.doppler_wwvh_std_hz)
        
        # Use differential solver
        result = self.differential_solver.solve_with_anchor(
            wwv_arrival_rtp=wwv_arrival_rtp,
            wwvh_arrival_rtp=wwvh_arrival_rtp,
            minute_boundary_rtp=minute_boundary_rtp,
            sample_rate=archive.sample_rate,
            frequency_mhz=frequency_mhz,
            delay_spread_ms=delay_spread_ms,
            doppler_std_hz=doppler_std_hz
        )
        
        # Store result
        self.latest_differential_result = result
        
        # Log result
        if result.confidence > 0.1:
            wwv_mode_str = f"{result.wwv_mode.value} ({result.wwv_n_hops}-hop)"
            wwvh_mode_str = f"{result.wwvh_mode.value} ({result.wwvh_n_hops}-hop)"
            verified_str = "✓" if result.clock_error_verified else "✗"
            
            logger.info(
                f"🎯 DIFFERENTIAL UTC(NIST): {frequency_mhz:.1f}MHz | "
                f"WWV: {wwv_mode_str} {result.wwv_delay_ms:.2f}ms, "
                f"WWVH: {wwvh_mode_str} {result.wwvh_delay_ms:.2f}ms | "
                f"Clock Error: {result.clock_error_ms:+.3f}ms {verified_str} | "
                f"Conf: {result.confidence:.0%}"
            )
            
            if result.clock_error_verified:
                logger.info(
                    f"✅ VERIFIED: WWV/WWVH agree within {result.wwv_wwvh_agreement_ms:.2f}ms"
                )
            elif result.wwv_wwvh_agreement_ms < 3.0:
                logger.warning(
                    f"⚠️ MARGINAL: WWV/WWVH disagree by {result.wwv_wwvh_agreement_ms:.2f}ms "
                    f"(possible mode misidentification)"
                )
            else:
                logger.warning(
                    f"❌ CONFLICT: WWV/WWVH disagree by {result.wwv_wwvh_agreement_ms:.2f}ms "
                    f"(mode identification likely wrong)"
                )
        
        # Convert to SolverResult format for logging compatibility
        # Use WWV as the "station" for logging purposes
        from .transmission_time_solver import PropagationMode as OldMode
        mode_map = {
            'UNK': OldMode.UNKNOWN,
            '1E': OldMode.ONE_HOP_E,
            '1F': OldMode.ONE_HOP_F,
            '2F': OldMode.TWO_HOP_F,
            '3F': OldMode.THREE_HOP_F,
        }
        
        compat_result = SolverResult(
            arrival_rtp=wwv_arrival_rtp,
            emission_rtp=wwv_arrival_rtp - int(result.wwv_delay_ms * archive.sample_rate / 1000),
            emission_offset_ms=result.clock_error_ms,
            propagation_delay_ms=result.wwv_delay_ms,
            mode=mode_map.get(result.wwv_mode.value, OldMode.UNKNOWN),
            mode_name=f"{result.wwv_mode.value} (differential)",
            n_hops=result.wwv_n_hops,
            layer_height_km=300.0,  # Assumed F2
            elevation_angle_deg=0.0,  # Not calculated in differential
            confidence=result.confidence,
            mode_separation_ms=result.mode_separation_ms,
            delay_spread_penalty=1.0,
            doppler_penalty=1.0,
            fss_consistency=1.0,
            candidates=[],
            utc_nist_offset_ms=result.clock_error_ms,
            utc_nist_verified=result.clock_error_verified
        )
        
        self.latest_transmission_result = compat_result
        self._log_transmission_time(archive, compat_result, 'WWV', frequency_mhz)
    
    def _solve_single_station(
        self,
        archive: 'NPZArchive',
        discrimination: 'DiscriminationResult',
        detections: List[ToneDetectionResult],
        frequency_mhz: float
    ):
        """
        Fallback: Single-station solve when differential not available.
        
        WARNING: This approach is CONTAMINATED by local clock error!
        The timing_error_ms includes both T_prop AND clock_error, so
        subtracting T_prop leaves clock_error - but we can't verify it.
        """
        station = discrimination.dominant_station
        if station not in ['WWV', 'WWVH']:
            return
        
        # Find the best detection for this station
        best_detection = None
        for det in detections or []:
            if det.station.value.upper() == station:
                if best_detection is None or det.snr_db > best_detection.snr_db:
                    best_detection = det
        
        if not best_detection:
            logger.debug(f"No {station} detection for transmission time solving")
            return
        
        observed_delay_ms = best_detection.timing_error_ms
        
        has_precise_timing = (
            best_detection.sample_position_original is not None and
            best_detection.buffer_rtp_start is not None
        )
        
        if has_precise_timing:
            rtp_at_tone = best_detection.buffer_rtp_start + best_detection.sample_position_original
            timing_offset_samples = int(observed_delay_ms * archive.sample_rate / 1000)
            expected_second_rtp = rtp_at_tone - timing_offset_samples
            arrival_rtp = rtp_at_tone
        else:
            expected_second_rtp = archive.rtp_timestamp
            arrival_rtp = archive.rtp_timestamp + int(observed_delay_ms * archive.sample_rate / 1000)
        
        logger.info(
            f"📡 SINGLE-STATION solve (clock-error contaminated): "
            f"{station} {frequency_mhz:.1f}MHz | timing_error={observed_delay_ms:.2f}ms"
        )
        
        # Get quality metrics
        delay_spread_ms = 0.5
        doppler_std_hz = 0.1
        if discrimination.doppler_wwv_std_hz is not None and station == 'WWV':
            doppler_std_hz = discrimination.doppler_wwv_std_hz
        elif discrimination.doppler_wwvh_std_hz is not None and station == 'WWVH':
            doppler_std_hz = discrimination.doppler_wwvh_std_hz
        
        # Solve with old method (for compatibility)
        result = self.transmission_solver.solve(
            station=station,
            frequency_mhz=frequency_mhz,
            arrival_rtp=arrival_rtp,
            delay_spread_ms=delay_spread_ms,
            doppler_std_hz=doppler_std_hz,
            fss_db=None,
            expected_second_rtp=expected_second_rtp
        )
        
        self.latest_transmission_result = result
        
        # CRITICAL: Cap confidence since we can't verify clock error
        # The result is UNVERIFIABLE without differential reference
        effective_confidence = min(0.3, result.confidence)
        
        logger.warning(
            f"⚠️ Single-station result UNVERIFIABLE | "
            f"Mode: {result.mode_name} | "
            f"UTC offset: {result.utc_nist_offset_ms:+.2f}ms | "
            f"Conf: {effective_confidence:.0%} (capped - no differential reference)"
        )
        
        result.confidence = effective_confidence
        self._log_transmission_time(archive, result, station, frequency_mhz)
    
    def _log_transmission_time(
        self,
        archive: 'NPZArchive',
        result: 'SolverResult',
        station: str,
        frequency_mhz: float
    ):
        """Write transmission time result to CSV for analysis."""
        try:
            import csv
            
            # Create transmission_time directory
            tx_time_dir = self.output_dir / 'transmission_time'
            tx_time_dir.mkdir(parents=True, exist_ok=True)
            
            # Daily CSV file
            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
            csv_file = tx_time_dir / f'{self.channel_name.replace(" ", "_")}_utc_nist_{date_str}.csv'
            
            # Check if file exists (for header)
            write_header = not csv_file.exists()
            
            with open(csv_file, 'a', newline='') as f:
                writer = csv.writer(f)
                
                if write_header:
                    writer.writerow([
                        'timestamp_utc',
                        'station',
                        'frequency_mhz',
                        'mode',
                        'n_hops',
                        'layer_height_km',
                        'elevation_deg',
                        'propagation_delay_ms',
                        'utc_nist_offset_ms',
                        'utc_nist_verified',
                        'confidence',
                        'mode_separation_ms',
                        'arrival_rtp',
                        'emission_rtp'
                    ])
                
                # Write data row
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    station,
                    f'{frequency_mhz:.2f}',
                    result.mode.value,
                    result.n_hops,
                    f'{result.layer_height_km:.0f}',
                    f'{result.elevation_angle_deg:.1f}',
                    f'{result.propagation_delay_ms:.3f}',
                    f'{result.utc_nist_offset_ms:.3f}' if result.utc_nist_offset_ms else '',
                    result.utc_nist_verified,
                    f'{result.confidence:.3f}',
                    f'{result.mode_separation_ms:.3f}',
                    result.arrival_rtp,
                    result.emission_rtp
                ])
            
            # Also write to shared observations file for multi-station correlation
            self._write_shared_observation(archive, result, station, frequency_mhz)
                
        except Exception as e:
            logger.warning(f"Failed to log transmission time: {e}")
    
    def _write_shared_observation(
        self,
        archive: 'NPZArchive',
        result: 'SolverResult',
        station: str,
        frequency_mhz: float
    ):
        """Write observation to shared JSON for multi-station correlation."""
        try:
            import json
            import fcntl
            
            # Shared observations file at data root level
            shared_file = self.output_dir.parent.parent / 'status' / 'transmission_observations.json'
            shared_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Create observation record
            obs = {
                'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                'minute_utc': archive.timestamp.isoformat() if archive.timestamp else None,
                'channel': self.channel_name,
                'station': station,
                'frequency_mhz': frequency_mhz,
                'mode': result.mode.value,
                'mode_name': result.mode_name,
                'n_hops': result.n_hops,
                'propagation_delay_ms': result.propagation_delay_ms,
                'utc_offset_ms': result.utc_nist_offset_ms,
                'confidence': result.confidence,
                'elevation_deg': result.elevation_angle_deg,
                'verified': result.utc_nist_verified
            }
            
            # Read existing, append, write (with file locking for concurrent access)
            observations = []
            if shared_file.exists():
                try:
                    with open(shared_file, 'r') as f:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                        data = json.load(f)
                        observations = data.get('observations', [])
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (json.JSONDecodeError, KeyError):
                    observations = []
            
            # Keep only last 10 minutes of observations
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            observations = [
                o for o in observations 
                if datetime.fromisoformat(o['timestamp_utc'].replace('Z', '+00:00')) > cutoff
            ]
            
            observations.append(obs)
            
            # Write back with lock
            with open(shared_file, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                json.dump({
                    'updated_utc': datetime.now(timezone.utc).isoformat(),
                    'observations': observations
                }, f, indent=2)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
        except Exception as e:
            logger.debug(f"Failed to write shared observation: {e}")
    
    def _backfill_gaps_on_startup(self, max_backfill: int):
        """
        Detect and backfill gaps in discrimination data on startup.
        
        Compares NPZ archive coverage vs discrimination CSV coverage,
        reprocesses missing files.
        
        Args:
            max_backfill: Maximum number of gaps to fill (prevents long startup delays)
        """
        print("📊 Gap backfill: Starting...")
        try:
            from .gap_backfill import find_gaps, format_gap_summary
            print("📊 Gap backfill: Imports successful")
            
            # Find today's discrimination CSV
            today = datetime.now(timezone.utc).strftime('%Y%m%d')
            csv_file = self.output_dir / 'discrimination' / f'{self.channel_name.replace(" ", "_")}_discrimination_{today}.csv'
            
            # Detect gaps
            gaps = find_gaps(self.archive_dir, csv_file)
            
            if not gaps:
                logger.info("✅ No gaps detected - discrimination data complete")
                return
            
            logger.info(f"⚠️  Detected {len(gaps)} gaps in discrimination data")
            logger.info(format_gap_summary(gaps))
            
            # Backfill (limited to prevent blocking startup)
            if len(gaps) > max_backfill:
                logger.info(f"Backfilling first {max_backfill} gaps (remaining {len(gaps) - max_backfill} will process in background)")
                gaps_to_fill = gaps[:max_backfill]
            else:
                gaps_to_fill = gaps
            
            successful = 0
            for i, (gap_time, npz_file) in enumerate(gaps_to_fill):
                try:
                    logger.info(f"  Backfill [{i+1}/{len(gaps_to_fill)}]: {gap_time.strftime('%Y-%m-%d %H:%M')} UTC")
                    archive = NPZArchive.load(npz_file)
                    self.process_archive(archive)
                    successful += 1
                except Exception as e:
                    logger.error(f"  Failed to backfill {npz_file.name}: {e}")
            
            logger.info(f"✅ Backfilled {successful}/{len(gaps_to_fill)} gaps")
            
        except Exception as e:
            logger.error(f"Gap backfill failed: {e}", exc_info=True)
    
    def run(self, poll_interval: float = 10.0, backfill_gaps: bool = True, max_backfill: int = 100):
        """
        Main processing loop - polls for new files and processes them
        
        Args:
            poll_interval: Seconds between directory scans
            backfill_gaps: If True, detect and backfill gaps on startup
            max_backfill: Maximum gaps to backfill on startup (prevents long delays)
        """
        logger.info("Analytics service started")
        self.running = True
        
        # Write initial status
        self._write_status()
        self._write_gpsdo_status()
        
        # GAP BACKFILL: Check for missing discrimination data on startup
        if backfill_gaps:
            print(f"🔍 Checking for gaps in discrimination data (max_backfill={max_backfill})...")
            logger.info(f"Starting gap backfill (max_backfill={max_backfill})")
            self._backfill_gaps_on_startup(max_backfill)
        
        last_status_time = 0
        try:
            while self.running:
                try:
                    # Discover new files
                    new_files = self.discover_new_files()
                    
                    if not new_files:
                        logger.debug("No new files to process")
                    
                    # Process each new file
                    for file_path in new_files:
                        if not self.running:
                            break
                        
                        try:
                            # Load NPZ archive
                            archive = NPZArchive.load(file_path)
                            
                            # Process through full pipeline
                            results = self.process_archive(archive)
                            
                            # Update state
                            self.state.last_processed_file = file_path
                            self.state.last_processed_time = file_path.stat().st_mtime
                            self.state.files_processed += 1
                            
                            # Save state periodically
                            if self.state.files_processed % 10 == 0:
                                self._save_state()
                            
                            logger.info(f"Processed: {file_path.name} "
                                       f"(completeness={results['quality_metrics']['completeness_pct']:.1f}%, "
                                       f"detections={len(results['tone_detections'])})")
                            
                        except Exception as e:
                            logger.error(f"Failed to process {file_path}: {e}", exc_info=True)
                            # Continue to next file
                    
                    # Save state after batch
                    if new_files:
                        self._save_state()
                    
                    # Write status periodically (every 10 seconds)
                    now = time.time()
                    if now - last_status_time >= 10:
                        self._write_status()
                        self._write_gpsdo_status()
                        last_status_time = now
                    
                    # Sleep until next poll
                    time.sleep(poll_interval)
                
                except KeyboardInterrupt:
                    logger.info("Shutting down on keyboard interrupt")
                    self.running = False
                except Exception as e:
                    logger.error(f"Error in main loop: {e}", exc_info=True)
                    time.sleep(poll_interval)
        
        finally:
            # Final state save
            self._save_state()
            
            # Digital RF flushing handled by separate service
            
            logger.info("Analytics service stopped")
    
    def stop(self):
        """Stop the analytics service"""
        self.running = False
        
        # Digital RF flushing handled by separate service


def main():
    """CLI entry point for testing"""
    import argparse
    
    parser = argparse.ArgumentParser(description='GRAPE Analytics Service')
    parser.add_argument('--archive-dir', required=True, help='NPZ archive directory')
    parser.add_argument('--output-dir', required=True, help='Output directory for derived products')
    parser.add_argument('--channel-name', required=True, help='Channel name')
    parser.add_argument('--frequency-hz', type=float, required=True, help='Center frequency in Hz')
    parser.add_argument('--state-file', help='State persistence file')
    parser.add_argument('--poll-interval', type=float, default=10.0, help='Poll interval (seconds)')
    parser.add_argument('--backfill-gaps', action='store_true', default=True, help='Auto-detect and backfill gaps on startup (default: True)')
    parser.add_argument('--no-backfill-gaps', action='store_false', dest='backfill_gaps', help='Disable gap backfill on startup')
    parser.add_argument('--max-backfill', type=int, default=100, help='Maximum gaps to backfill on startup (default: 100)')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    parser.add_argument('--callsign', help='Station callsign for Digital RF metadata')
    parser.add_argument('--grid-square', help='Grid square for Digital RF metadata')
    parser.add_argument('--receiver-name', default='GRAPE', help='Receiver name for Digital RF metadata')
    parser.add_argument('--psws-station-id', help='PSWS station ID (for upload compatibility)')
    parser.add_argument('--psws-instrument-id', default='1', help='PSWS instrument number (for upload compatibility)')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Build station config (PSWS-compatible)
    station_config = {}
    if args.callsign:
        station_config['callsign'] = args.callsign
    if args.grid_square:
        station_config['grid_square'] = args.grid_square
    if args.receiver_name:
        station_config['receiver_name'] = args.receiver_name
    if args.psws_station_id:
        station_config['psws_station_id'] = args.psws_station_id
    if args.psws_instrument_id:
        station_config['psws_instrument_id'] = args.psws_instrument_id
    
    # Create and run service
    service = AnalyticsService(
        archive_dir=Path(args.archive_dir),
        output_dir=Path(args.output_dir),
        channel_name=args.channel_name,
        frequency_hz=args.frequency_hz,
        state_file=Path(args.state_file) if args.state_file else None,
        station_config=station_config if station_config else None
    )
    
    service.run(
        poll_interval=args.poll_interval,
        backfill_gaps=args.backfill_gaps,
        max_backfill=args.max_backfill
    )


if __name__ == '__main__':
    main()
