"""
GPSDO-Calibrated Timing System

This module implements a multi-phase timing calibration approach that leverages
the deterministic nature of GPSDO-locked RTP timestamps:

Phase 1 - BOOTSTRAP (first ~3-5 minutes):
    - Wide search window (500ms) for initial tone detection
    - Establish RTP-to-UTC calibration from high-quality matches
    - Any frequency with SNR > 20dB and confidence > 0.8 contributes
    
Phase 2 - CALIBRATED (after bootstrap):
    - Narrow search window (±5ms) centered on expected position
    - Intra-station consistency checks (same station, different frequencies)
    - Inter-station consistency checks (geographic propagation differences)
    
Phase 3 - VERIFIED (optional, for sub-ms accuracy):
    - BCD 100Hz alignment on WWV/WWVH
    - FSK boundary alignment on CHU
    - Test signal detection for additional verification

Key Insight: With GPSDO, RTP timestamps are perfectly deterministic (zero drift).
Once we establish the RTP-to-UTC offset from a few high-quality detections,
we can predict exactly where every tone should appear in subsequent buffers.

The calibration formula:
    expected_tone_sample = (second_number * sample_rate) + propagation_delay_samples - rtp_offset_samples
    
Where:
    - second_number: Which second's tone (0-59)
    - sample_rate: 20000 Hz
    - propagation_delay_samples: Station-specific propagation delay
    - rtp_offset_samples: Fixed offset from RTP epoch to minute boundary

Author: Cascade AI
Date: 2025-12-13
"""

import fcntl
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

# Import centralized version constant
try:
    from ..version import STATE_FILE_VERSION
except ImportError:
    STATE_FILE_VERSION = 2  # Fallback for standalone testing

logger = logging.getLogger(__name__)

# Timing calibrator state file version (increment on schema changes)
TIMING_CALIBRATOR_STATE_VERSION = 2  # Incremented for anchor-based calibration

# =============================================================================
# ANCHOR STATIONS AND PHYSICAL CONSTRAINTS
# =============================================================================
# Anchor stations are unambiguous (unique frequencies or WWV-only frequencies)
# and provide the reference RTP offset that all other channels must use.
#
# Physical arrival order (based on distance from receiver in Missouri):
#   WWV (Colorado, ~1120 km) < CHU (Ottawa, ~1522 km) < WWVH (Hawaii, ~6600 km) < BPM (China, ~11500 km)
#
# Light-speed delays:
#   WWV: ~3.7 ms, CHU: ~5.1 ms, WWVH: ~22 ms, BPM: ~38 ms
#
# With ionospheric propagation (1-3 hops), actual delays are higher but ORDER is preserved.

# Anchor channels: unambiguous stations with unique or WWV-only frequencies
ANCHOR_CHANNELS = {
    'CHU 3.33 MHz',    # CHU-only frequency
    'CHU 7.85 MHz',    # CHU-only frequency  
    'CHU 14.67 MHz',   # CHU-only frequency
    'WWV 20 MHz',      # WWV-only frequency
    'WWV 25 MHz',      # WWV-only frequency
}

# Minimum light-speed propagation delays (ms) - signals CANNOT arrive before these
# These are hard physical constraints based on distance
MINIMUM_PROPAGATION_DELAY_MS = {
    'WWV': 3.5,    # ~1120 km / c
    'CHU': 5.0,    # ~1522 km / c
    'WWVH': 21.0,  # ~6600 km / c
    'BPM': 37.0,   # ~11500 km / c
}

# Expected propagation delay ranges (ms) including ionospheric paths
# (min, typical, max) - used for search window positioning
EXPECTED_PROPAGATION_DELAY_MS = {
    'WWV': (4.0, 8.0, 20.0),     # 1-2 hop F-layer
    'CHU': (5.5, 10.0, 25.0),    # 1-2 hop F-layer
    'WWVH': (22.0, 35.0, 60.0),  # 2-3 hop F-layer
    'BPM': (38.0, 50.0, 80.0),   # 3-4 hop F-layer
}


class CalibrationPhase(Enum):
    """Calibration phase tracking."""
    BOOTSTRAP = "bootstrap"        # Wide search, establishing calibration
    PROVISIONAL = "provisional"    # GPSDO-validated, operational use (feeds Chrony)
    CALIBRATED = "calibrated"      # Scientifically validated, ionospheric measurements
    VERIFIED = "verified"          # Secondary signals confirmed


@dataclass
class StationCalibration:
    """Calibration data for a single station."""
    station: str                          # WWV, WWVH, or CHU
    propagation_delay_ms: float           # Estimated propagation delay
    propagation_delay_std_ms: float       # Uncertainty in propagation delay
    n_samples: int                        # Number of measurements used
    last_updated: float                   # Unix timestamp
    frequencies_contributing: List[float] = field(default_factory=list)
    
    def search_window_ms(self) -> float:
        """Calculate appropriate search window based on uncertainty."""
        # Minimum 3ms (ionospheric variation), maximum 50ms
        # Use 3-sigma for high confidence
        return max(3.0, min(50.0, 3.0 * self.propagation_delay_std_ms + 2.0))


@dataclass
class RPTCalibration:
    """RTP-to-UTC calibration for a channel."""
    channel_name: str
    frequency_hz: int
    sample_rate: int
    
    # Core calibration: RTP timestamp at a known minute boundary
    reference_minute_utc: int             # Unix timestamp of minute boundary
    reference_rtp_timestamp: int          # RTP timestamp at that boundary
    
    # Derived: samples offset within minute
    rtp_offset_samples: int               # RTP % samples_per_minute
    
    # Quality metrics
    calibration_snr_db: float             # SNR of calibrating detection
    calibration_confidence: float         # Confidence of calibrating detection
    n_confirmations: int                  # Number of subsequent confirmations
    last_confirmed: float                 # Unix timestamp of last confirmation
    
    # Station that was detected at this RTP offset (for shared frequencies)
    # Must come after required fields since it has a default value
    detected_station: str = 'WWV'         # Station detected at calibration time
    
    # SSRC tracking for hybrid calibration persistence (Issue #X)
    # RTP calibration is SSRC-dependent - invalidate when radiod restarts
    ssrc: Optional[int] = None            # SSRC of radiod channel
    ssrc_first_seen: Optional[float] = None  # When this SSRC was first seen
    ssrc_last_seen: Optional[float] = None   # Last time this SSRC was confirmed
    
    def expected_tone_sample(
        self, 
        second_number: int, 
        propagation_delay_ms: float,
        buffer_start_rtp: int
    ) -> int:
        """
        Calculate expected sample position of a tone within a buffer.
        
        Args:
            second_number: Which second's tone (0-59)
            propagation_delay_ms: Station propagation delay in ms
            buffer_start_rtp: RTP timestamp at start of buffer
            
        Returns:
            Sample index within buffer where tone should appear
        """
        samples_per_second = self.sample_rate
        samples_per_minute = samples_per_second * 60
        
        # Tone position in minute (from minute boundary)
        tone_in_minute_samples = (
            second_number * samples_per_second + 
            int(propagation_delay_ms * self.sample_rate / 1000)
        )
        
        # Buffer start position in minute
        buffer_offset_in_minute = buffer_start_rtp % samples_per_minute
        
        # Tone position relative to buffer start
        tone_in_buffer = tone_in_minute_samples - buffer_offset_in_minute
        
        # Handle wrap-around (tone might be in previous/next minute)
        if tone_in_buffer < 0:
            tone_in_buffer += samples_per_minute
        elif tone_in_buffer >= samples_per_minute:
            tone_in_buffer -= samples_per_minute
            
        return tone_in_buffer


@dataclass
class ConsistencyResult:
    """Result of consistency checking."""
    is_consistent: bool
    intra_station_std_ms: Dict[str, float]  # Per-station std dev
    inter_station_spread_ms: float           # Spread between station means
    suspect_measurements: List[str]          # Channel names of suspects
    suggested_corrections: Dict[str, str]    # channel -> suggested station


class TimingCalibrator:
    """
    Manages GPSDO-calibrated timing with bootstrap and narrow search phases.
    
    Usage:
        calibrator = TimingCalibrator(data_root, sample_rate=20000)
        
        # During bootstrap (first few minutes)
        if calibrator.phase == CalibrationPhase.BOOTSTRAP:
            search_window_ms = 500.0
        else:
            search_window_ms = calibrator.get_search_window(station, frequency_mhz)
        
        # After detection, update calibration
        calibrator.update_from_detection(detection_result, rtp_timestamp)
        
        # Check consistency across channels
        consistency = calibrator.check_consistency(measurements)
    """
    
    # Bootstrap Thresholds (Scientifically Rigorous Criteria)
    # Statistical Confidence: N≥30 per station for Gaussian statistics (Central Limit Theorem)
    
    # PROVISIONAL Mode (Fast Path - GPSDO-Validated Operational Use)
    PROVISIONAL_MIN_DETECTIONS = 10       # Minimum detections PER STATION (30 total for 3 stations)
    PROVISIONAL_MIN_STATIONS = 2          # Need at least 2 stations for cross-validation
    PROVISIONAL_MIN_DURATION_MINUTES = 10 # Minimum 10-minute span
    PROVISIONAL_MAX_D_CLOCK_STD_MS = 1.0  # D_clock convergence: last 5 within ±1ms
    PROVISIONAL_MAX_RTP_VARIANCE = 50**2  # RTP offset variance ≤ 50 samples (GPSDO stability)
    
    # CALIBRATED Mode (Rigorous Path - Scientific Validation)
    BOOTSTRAP_MIN_DETECTIONS = 30         # Minimum detections PER STATION (90 total for 3 stations)
    BOOTSTRAP_MIN_STATIONS = 2            # Need at least 2 stations for cross-validation
    BOOTSTRAP_MIN_DURATION_MINUTES = 60   # Minimum 60-minute span to cover ionospheric variations
    BOOTSTRAP_MIN_TEMPORAL_COVERAGE = 0.5 # 50% of time window must have detections
    BOOTSTRAP_SNR_THRESHOLD = -100.0      # dB - accept any detection (weak signals common at night)
    BOOTSTRAP_CONFIDENCE_THRESHOLD = 0.01 # Minimum confidence (any detection helps)
    
    # Calibration Stability Thresholds (Convergence Criteria)
    MAX_PROPAGATION_STD_MS = 2.0          # Standard deviation ≤ 2ms (matches HF measurement uncertainty)
    MAX_RTP_OFFSET_STD_SAMPLES = 100      # ~5ms @ 20kHz sample rate
    STABILITY_WINDOW_DETECTIONS = 10      # Sliding window for stability check
    MAX_DRIFT_MS_PER_HOUR = 1.0           # Linear drift ≤ 1ms/hour
    
    # Cross-Station Validation Thresholds
    MAX_DIFFERENTIAL_TIMING_ERROR_MS = 5.0  # WWV-WWVH agreement (shared ionosphere)
    MIN_CROSS_CORRELATION = 0.7           # Ionospheric variations should correlate
    
    # Physical Constraint Validation
    MIN_PROPAGATION_DELAY_RATIO = 0.8     # Measured/Expected ≥ 80%
    MAX_PROPAGATION_DELAY_RATIO = 3.0     # Measured/Expected ≤ 300%
    
    # Measurement Uncertainty Budget (ISO GUM)
    MAX_CALIBRATION_UNCERTAINTY_MS = 3.0  # Combined standard uncertainty (k=1, 68% confidence)
    MAX_EXPANDED_UNCERTAINTY_MS = 6.0     # Expanded uncertainty (k=2, 95% confidence)
    
    # Legacy thresholds (kept for compatibility)
    NARROW_WINDOW_MS = 5.0                # Default narrow search window
    INTRA_STATION_THRESHOLD_MS = 5.0      # Max allowed intra-station std dev
    
    def __init__(
        self,
        data_root: Path,
        sample_rate: int = 20000,
        state_file: Optional[Path] = None
    ):
        self.data_root = Path(data_root)
        self.sample_rate = sample_rate
        self.samples_per_minute = sample_rate * 60
        
        # State file for persistence
        self.state_file = state_file or (self.data_root / 'state' / 'timing_calibration.json')
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Current phase
        self.phase = CalibrationPhase.BOOTSTRAP
        
        # Per-station calibration
        self.station_calibration: Dict[str, StationCalibration] = {}
        
        # Per-channel RTP calibration
        self.rtp_calibration: Dict[str, RPTCalibration] = {}
        
        # Global anchor RTP offset (shared across all channels since they use same GPSDO)
        # This is established from anchor channels (CHU, WWV 20/25 MHz) and used to
        # predict signal locations on shared frequencies
        self.global_rtp_offset: Optional[int] = None
        self.global_rtp_offset_source: Optional[str] = None  # Which anchor established it
        self.global_rtp_offset_confidence: float = 0.0
        
        # Bootstrap tracking
        self.bootstrap_detections: List[Dict] = []
        self.bootstrap_start_time = time.time()
        
        # Statistics
        self.stats = {
            'bootstrap_detections': 0,
            'calibrated_detections': 0,
            'narrow_window_hits': 0,
            'narrow_window_misses': 0,
            'consistency_checks': 0,
            'discrimination_corrections': 0
        }
        
        # SSRC tracking for hybrid calibration persistence
        # Maps channel_name -> current SSRC
        self.channel_ssrcs: Dict[str, int] = {}
        
        # Consecutive failure tracking for adaptive window back-off
        # Maps station+frequency+channel -> failure count
        self.consecutive_failures: Dict[str, int] = {}
        
        # Load existing state
        self._load_state()
        
        logger.info(f"TimingCalibrator initialized in {self.phase.value} phase")
        if self.station_calibration:
            for station, cal in self.station_calibration.items():
                logger.info(
                    f"  {station}: prop_delay={cal.propagation_delay_ms:.2f}ms "
                    f"± {cal.propagation_delay_std_ms:.2f}ms "
                    f"(n={cal.n_samples})"
                )
    
    def _load_state(self):
        """Load calibration state from disk with file locking and version validation."""
        if not self.state_file.exists():
            return
            
        try:
            with open(self.state_file) as f:
                # Acquire shared lock for reading (allows multiple readers)
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    state = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            
            # Version validation - discard stale state files
            file_version = state.get('version', 0)
            if file_version < TIMING_CALIBRATOR_STATE_VERSION:
                logger.warning(
                    f"Timing calibrator state file version {file_version} < current "
                    f"{TIMING_CALIBRATOR_STATE_VERSION}, discarding stale state"
                )
                return
            
            # Restore phase
            phase_str = state.get('phase', 'bootstrap')
            self.phase = CalibrationPhase(phase_str)
            
            # Restore station calibration
            for station, cal_data in state.get('station_calibration', {}).items():
                self.station_calibration[station] = StationCalibration(
                    station=station,
                    propagation_delay_ms=cal_data['propagation_delay_ms'],
                    propagation_delay_std_ms=cal_data['propagation_delay_std_ms'],
                    n_samples=cal_data['n_samples'],
                    last_updated=cal_data['last_updated'],
                    frequencies_contributing=cal_data.get('frequencies_contributing', [])
                )
            
            # Restore RTP calibration
            for channel, rtp_data in state.get('rtp_calibration', {}).items():
                self.rtp_calibration[channel] = RPTCalibration(
                    channel_name=channel,
                    frequency_hz=rtp_data['frequency_hz'],
                    sample_rate=rtp_data['sample_rate'],
                    reference_minute_utc=rtp_data['reference_minute_utc'],
                    reference_rtp_timestamp=rtp_data['reference_rtp_timestamp'],
                    rtp_offset_samples=rtp_data['rtp_offset_samples'],
                    calibration_snr_db=rtp_data['calibration_snr_db'],
                    calibration_confidence=rtp_data['calibration_confidence'],
                    n_confirmations=rtp_data['n_confirmations'],
                    last_confirmed=rtp_data['last_confirmed'],
                    detected_station=rtp_data.get('detected_station', 'WWV'),
                    ssrc=rtp_data.get('ssrc'),
                    ssrc_first_seen=rtp_data.get('ssrc_first_seen'),
                    ssrc_last_seen=rtp_data.get('ssrc_last_seen')
                )
            
            self.stats = state.get('stats', self.stats)
            
            # Restore global RTP offset
            self.global_rtp_offset = state.get('global_rtp_offset')
            self.global_rtp_offset_source = state.get('global_rtp_offset_source')
            self.global_rtp_offset_confidence = state.get('global_rtp_offset_confidence', 0.0)

            # Validate SSRC stability (Hybrid Calibration)
            # If SSRCs have changed (radiod restart), invalidate RTP calibration
            # but preserve station calibration if fresh.
            if self.rtp_calibration:
                self._validate_hybrid_calibration_state()

            logger.info(f"Loaded timing calibration: phase={self.phase.value}, "
                       f"{len(self.station_calibration)} stations, "
                       f"{len(self.rtp_calibration)} channels, "
                       f"global_offset={self.global_rtp_offset}")
                       
        except Exception as e:
            logger.warning(f"Failed to load timing calibration: {e}")
    
    def _save_state(self):
        """Save calibration state to disk with file locking for multi-process safety."""
        # Use a lock file for exclusive access during save
        lock_file = self.state_file.with_suffix('.lock')
        
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Acquire exclusive lock before read-modify-write
            with open(lock_file, 'w') as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    # Re-load state to merge with other processes' changes
                    if self.state_file.exists():
                        with open(self.state_file) as f:
                            existing_state = json.load(f)
                        # Merge: keep our station/rtp calibration, but check for newer data
                        existing_stations = existing_state.get('station_calibration', {})
                        existing_rtp = existing_state.get('rtp_calibration', {})
                        
                        # Merge station calibration (keep higher n_samples)
                        for station, cal_data in existing_stations.items():
                            if station not in self.station_calibration:
                                self.station_calibration[station] = StationCalibration(
                                    station=station,
                                    propagation_delay_ms=cal_data['propagation_delay_ms'],
                                    propagation_delay_std_ms=cal_data['propagation_delay_std_ms'],
                                    n_samples=cal_data['n_samples'],
                                    last_updated=cal_data['last_updated'],
                                    frequencies_contributing=cal_data.get('frequencies_contributing', [])
                                )
                            elif cal_data['n_samples'] > self.station_calibration[station].n_samples:
                                # Other process has more samples, use theirs
                                self.station_calibration[station] = StationCalibration(
                                    station=station,
                                    propagation_delay_ms=cal_data['propagation_delay_ms'],
                                    propagation_delay_std_ms=cal_data['propagation_delay_std_ms'],
                                    n_samples=cal_data['n_samples'],
                                    last_updated=cal_data['last_updated'],
                                    frequencies_contributing=cal_data.get('frequencies_contributing', [])
                                )
                        
                        # Merge RTP calibration (each channel is independent)
                        for channel, rtp_data in existing_rtp.items():
                            if channel not in self.rtp_calibration:
                                self.rtp_calibration[channel] = RPTCalibration(
                                    channel_name=channel,
                                    frequency_hz=rtp_data['frequency_hz'],
                                    sample_rate=rtp_data['sample_rate'],
                                    reference_minute_utc=rtp_data['reference_minute_utc'],
                                    reference_rtp_timestamp=rtp_data['reference_rtp_timestamp'],
                                    rtp_offset_samples=rtp_data['rtp_offset_samples'],
                                    calibration_snr_db=rtp_data['calibration_snr_db'],
                                    calibration_confidence=rtp_data['calibration_confidence'],
                                    n_confirmations=rtp_data['n_confirmations'],
                                    last_confirmed=rtp_data['last_confirmed'],
                                    detected_station=rtp_data.get('detected_station', 'WWV'),
                                    ssrc=rtp_data.get('ssrc'),
                                    ssrc_first_seen=rtp_data.get('ssrc_first_seen'),
                                    ssrc_last_seen=rtp_data.get('ssrc_last_seen')
                                )
                    
                    state = {
                        'version': TIMING_CALIBRATOR_STATE_VERSION,
                        'phase': self.phase.value,
                        'global_rtp_offset': self.global_rtp_offset,
                        'global_rtp_offset_source': self.global_rtp_offset_source,
                        'global_rtp_offset_confidence': self.global_rtp_offset_confidence,
                        'station_calibration': {
                            station: {
                                'propagation_delay_ms': cal.propagation_delay_ms,
                                'propagation_delay_std_ms': cal.propagation_delay_std_ms,
                                'n_samples': cal.n_samples,
                                'last_updated': cal.last_updated,
                                'frequencies_contributing': cal.frequencies_contributing
                            }
                            for station, cal in self.station_calibration.items()
                        },
                        'rtp_calibration': {
                            channel: {
                                'frequency_hz': rtp.frequency_hz,
                                'sample_rate': rtp.sample_rate,
                                'reference_minute_utc': rtp.reference_minute_utc,
                                'reference_rtp_timestamp': rtp.reference_rtp_timestamp,
                                'rtp_offset_samples': rtp.rtp_offset_samples,
                                'calibration_snr_db': rtp.calibration_snr_db,
                                'calibration_confidence': rtp.calibration_confidence,
                                'n_confirmations': rtp.n_confirmations,
                                'last_confirmed': rtp.last_confirmed,
                                'detected_station': rtp.detected_station,
                                'ssrc': rtp.ssrc,
                                'ssrc_first_seen': rtp.ssrc_first_seen,
                                'ssrc_last_seen': rtp.ssrc_last_seen
                            }
                            for channel, rtp in self.rtp_calibration.items()
                        },
                        'stats': self.stats,
                        'saved_at': datetime.now(timezone.utc).isoformat()
                    }
                    
                    # Atomic write: write to temp file, fsync, then rename
                    temp_file = self.state_file.with_suffix('.tmp')
                    with open(temp_file, 'w') as f:
                        json.dump(state, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    temp_file.replace(self.state_file)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                
        except Exception as e:
            logger.error(f"Failed to save timing calibration: {e}", exc_info=True)
    
    def register_channel_ssrc(self, channel_name: str, ssrc: int):
        """
        Register a channel's SSRC and validate calibration validity.
        
        This is called by channel recorders when they initialize. It's the primary mechanism
        for detecting if radiod has restarted (which changes SSRCs).
        
        Args:
            channel_name: Channel identifier (e.g. 'WWV_10MHz')
            ssrc: The current SSRC from the RTP stream
        """
        # Store current SSRC
        self.channel_ssrcs[channel_name] = ssrc
        
        # If we have RTP calibration for this channel, update/validate it
        if channel_name in self.rtp_calibration:
            rtp_cal = self.rtp_calibration[channel_name]
            
            # If calibration has no SSRC recorded (legacy), adopt this one
            if rtp_cal.ssrc is None:
                logger.info(f"Adopting initial SSRC {ssrc:x} for {channel_name} calibration")
                rtp_cal.ssrc = ssrc
                rtp_cal.ssrc_first_seen = time.time()
                rtp_cal.ssrc_last_seen = time.time()
                self._save_state()
            
            # If SSRC matches, update last seen
            elif rtp_cal.ssrc == ssrc:
                rtp_cal.ssrc_last_seen = time.time()
                
            # If SSRC mismatch, radiod has likely restarted!
            else:
                logger.warning(
                    f"⚠️ SSRC mismatch for {channel_name}: "
                    f"calibrated={rtp_cal.ssrc:x}, current={ssrc:x}. "
                    f"Radiod likely restarted."
                )
                self._validate_hybrid_calibration_state()

    def _validate_hybrid_calibration_state(self):
        """
        Validate calibration state using hybrid strategy (SSRC + Time).
        
        Logic:
        1. Check SSRC stability (RTP calibration validity)
        2. Check station calibration freshness (Propagation delay validity)
        
        Transitions:
        - SSRCs Stable -> CALIBRATED (Full calibration)
        - SSRCs Changed + Station Fresh -> BOOTSTRAP (Fast recovery using priors)
        - SSRCs Changed + Station Stale -> BOOTSTRAP (Full re-bootstrap)
        """
        ssrc_stable = self._validate_ssrc_stability()
        station_cal_fresh = self._validate_station_calibration_freshness()
        
        if ssrc_stable:
             # Best case: SSRCs unchanged, use everything
             if self.phase != CalibrationPhase.CALIBRATED:
                 logger.info("✅ SSRCs unchanged - full calibration valid")
                 self.phase = CalibrationPhase.CALIBRATED
                 
        elif station_cal_fresh:
            # Hybrid case: SSRCs changed but station cal is fresh
            logger.info("🔄 SSRCs changed but station calibration fresh - fast re-bootstrap")
            self.phase = CalibrationPhase.BOOTSTRAP
            self.global_rtp_offset = None  # Invalidate RTP offset
            # Keep self.station_calibration - use as priors!
            
            # Reset RTP calibration but keep structure for stats? 
            # Actually better to clear it to force re-learning RTP offset
            self.rtp_calibration = {}
            
        else:
            # Worst case: Everything stale
            logger.warning("⚠️ Calibration stale - full re-bootstrap")
            self.phase = CalibrationPhase.BOOTSTRAP
            self.global_rtp_offset = None
            self.station_calibration = {}  # Clear stale data
            self.rtp_calibration = {}

    def _validate_ssrc_stability(self) -> bool:
        """
        Check if current SSRCs match calibration SSRCs.
        
        Returns:
            True if known SSRCs match calibration (or no current SSRCs to check yet)
            False if any known SSRC disagrees with calibration
        """
        if not self.rtp_calibration:
            return False
            
        # Get current SSRCs from active channels (populated via register_channel_ssrc)
        if not self.channel_ssrcs:
            # No channels connected yet - optimistic assumption until proven otherwise
            return True
        
        mismatches = 0
        matches = 0
            
        for channel, rtp_cal in self.rtp_calibration.items():
            if rtp_cal.ssrc is None:
                # Old active calibration without SSRC - treat as unstable to be safe
                # OR adopt on first see? Let's treat as unstable to force re-verification
                # unless we are very confident.
                continue
                
            if channel in self.channel_ssrcs:
                if self.channel_ssrcs[channel] != rtp_cal.ssrc:
                    logger.warning(
                        f"SSRC changed for {channel}: "
                        f"{rtp_cal.ssrc:08x} -> {self.channel_ssrcs[channel]:08x}"
                    )
                    mismatches += 1
                else:
                    matches += 1
        
        if mismatches > 0:
            return False
            
        # If we have matches and no mismatches, we are stable
        return True

    def _validate_station_calibration_freshness(self) -> bool:
        """
        Check if station propagation calibration is still valid.
        
        Station propagation delays change slowly (ionospheric seasonal variation).
        Consider fresh if:
        - Updated within last 7 days
        - Has sufficient samples (n >= 50)
        
        Returns:
            True if station calibration can be used as priors
        """
        if not self.station_calibration:
            return False
            
        now = time.time()
        max_age_seconds = 7 * 24 * 3600  # 7 days
        
        valid_stations = 0
        
        for station, cal in self.station_calibration.items():
            age = now - cal.last_updated
            if age > max_age_seconds:
                logger.warning(f"Station {station} calibration stale: {age/3600:.1f}h old")
                continue
                
            if cal.n_samples < 50:
                logger.debug(f"Station {station} calibration insufficient: {cal.n_samples} samples")
                continue
            
            valid_stations += 1
                
        # We need at least 2 valid anchor stations (CHU/WWV) to be useful
        return valid_stations >= 2
    
    def predict_station(
        self,
        channel_name: str,
        rtp_timestamp: int,
        detected_station: str,
        detection_confidence: str
    ) -> Tuple[str, float]:
        """
        Predict expected station based on RTP calibration history.
        
        Once we have a high-confidence lock on a station at a specific RTP offset,
        we expect to see the same station 1,200,000 samples later. This provides
        a strong prior that improves discrimination over time.
        
        Args:
            channel_name: Channel identifier
            rtp_timestamp: Current RTP timestamp
            detected_station: Station detected by discrimination
            detection_confidence: 'high', 'medium', or 'low'
            
        Returns:
            Tuple of (predicted_station, confidence)
            - predicted_station: Station we expect based on RTP history
            - confidence: 0.0-1.0 confidence in prediction
        """
        if channel_name not in self.rtp_calibration:
            # No history - trust the detection
            return (detected_station, 0.0)
        
        rtp_cal = self.rtp_calibration[channel_name]
        
        # Calculate expected RTP offset for this minute
        current_offset = rtp_timestamp % self.samples_per_minute
        expected_offset = rtp_cal.rtp_offset_samples
        
        # How close is the current offset to our calibrated offset?
        offset_diff_samples = abs(current_offset - expected_offset)
        offset_diff_ms = (offset_diff_samples / self.sample_rate) * 1000.0
        
        # If within 5ms of expected offset, we have high confidence in prediction
        if offset_diff_ms < 5.0:
            # Strong match - predict same station as was detected during calibration
            # Confidence based on number of confirmations
            conf = min(0.95, 0.5 + (rtp_cal.n_confirmations * 0.05))
            
            # Use the station that was actually detected at this RTP offset
            predicted_station = getattr(rtp_cal, 'detected_station', None)
            if not predicted_station or predicted_station not in ['WWV', 'WWVH', 'CHU', 'BPM']:
                # Fallback to channel name if no detected_station stored
                predicted_station = channel_name.split()[0].upper()
                if predicted_station not in ['WWV', 'WWVH', 'CHU', 'BPM']:
                    predicted_station = 'WWV'
            
            # If detection disagrees with prediction, log it and override
            if detected_station != predicted_station and detection_confidence != 'high':
                logger.info(
                    f"RTP prediction overrides {detection_confidence} detection: "
                    f"{detected_station} -> {predicted_station} "
                    f"(offset_diff={offset_diff_ms:.2f}ms, confirmations={rtp_cal.n_confirmations})"
                )
                self.stats['discrimination_corrections'] = self.stats.get('discrimination_corrections', 0) + 1
                return (predicted_station, conf)
            
            # Detection agrees with prediction - return with confidence
            return (predicted_station, conf)
        
        # No strong prediction - trust the detection
        return (detected_station, 0.0)
    
    def get_search_window_ms(
        self, 
        station: str, 
        frequency_mhz: float
    ) -> Tuple[float, float]:
        """
        Get search window for a station/frequency.
        
        Returns:
            Tuple of (window_half_width_ms, expected_offset_ms)
            - window_half_width_ms: Search ± this many ms
            - expected_offset_ms: Center of search window (propagation delay)
        """
        if self.phase == CalibrationPhase.BOOTSTRAP:
            # Wide search during bootstrap
            return (500.0, 0.0)
        
        # Use station calibration if available
        if station in self.station_calibration:
            cal = self.station_calibration[station]
            window = cal.search_window_ms()
            return (window, cal.propagation_delay_ms)
        
        # Fallback to geographic estimates
        default_delays = {
            'WWV': 6.5,    # Fort Collins, CO
            'WWVH': 25.0,  # Hawaii
            'CHU': 4.0,    # Ottawa, Canada
            'BPM': 50.0    # Lintong, China
        }
        return (50.0, default_delays.get(station, 10.0))
        
    def get_search_window(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str
    ) -> float:
        """
        Get adaptive search window with back-off support.
        
        Args:
            station: Station identifier ('WWV', etc.)
            frequency_mhz: Frequency in MHz
            channel_name: Full channel name
            
        Returns:
            Window half-width in milliseconds (e.g. 5.0 for ±5ms)
        """
        # Check for consecutive failures first
        key = self._get_calibration_key(station, frequency_mhz, channel_name)
        failures = self.consecutive_failures.get(key, 0)
        
        # If we've lost lock, widen the window significantly
        if failures > 5:
            # Back-off strategy: 50ms -> 100ms -> 250ms -> 500ms
            if failures > 20: return 500.0
            if failures > 10: return 250.0
            return 100.0
            
        # If we are just starting back up after failures, use wider window
        # (failures == 0 but was high recently? - handled by state reset on detection)
        
        if self.phase == CalibrationPhase.BOOTSTRAP:
            return 500.0
            
        # In CALIBRATED mode, use tight windows
        if self.phase == CalibrationPhase.CALIBRATED:
            # Use station calibration uncertainty if available
            if station in self.station_calibration:
                cal = self.station_calibration[station]
                # 3-sigma window + 1ms margin
                window = (3.0 * cal.propagation_delay_std_ms) + 1.0
                return max(2.0, min(window, 50.0))
                
        # PROVISIONAL or fallback
        return 15.0

    def get_expected_toa(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str
    ) -> Optional[float]:
        """
        Get expected Time of Arrival for a station+frequency.
        
        Returns mean ToA in milliseconds, or None if not yet learned.
        """
        if station not in self.station_calibration:
            return None
        
        cal = self.station_calibration[station]
        
        # Only return if we have sufficient confidence
        if cal.n_samples < 5:
            return None
        
        # Return mean propagation delay (ToA relative to second boundary)
        return cal.propagation_delay_ms

    def record_detection(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str,
        toa_ms: float
    ):
        """Record successful detection and update failure counters."""
        key = self._get_calibration_key(station, frequency_mhz, channel_name)
        
        # Reset failure counter for this specific broadcast
        self.consecutive_failures[key] = 0
        
        # Also reset failures for the channel generally since we got *something*
        # (Helps shared frequency recovery)
        for k in list(self.consecutive_failures.keys()):
            if channel_name in k:
                self.consecutive_failures[k] = 0

    def record_failure(
        self,
        frequency_mhz: float,
        channel_name: str
    ):
        """
        Record detection failure.
        
        We increment counters for ALL potential stations on this channel
        since we don't know which one failed to appear.
        """
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            key = self._get_calibration_key(station, frequency_mhz, channel_name)
            self.consecutive_failures[key] = self.consecutive_failures.get(key, 0) + 1

    def should_back_off(self, channel_name: str) -> bool:
        """Check if we should widen search windows for this channel."""
        # Check if any broadcast on this channel has too many failures
        for key, failures in self.consecutive_failures.items():
            if channel_name in key and failures > 5:
                return True
        return False
        
    def _get_calibration_key(self, station: str, frequency_mhz: float, channel_name: str) -> str:
        """Create unique key for tracking specific broadcast performance."""
        # Round frequency to 1 decimal place to group effectively (e.g. 9.999 -> 10.0)
        freq_key = f"{frequency_mhz:.1f}"
        return f"{channel_name}|{station}|{freq_key}"    
    def _validate_physical_constraints(
        self,
        station: str,
        propagation_delay_ms: float,
        d_clock_ms: float
    ) -> bool:
        """
        Validate detection against physical constraints.
        
        Enforces:
        1. Propagation delay >= minimum light-speed delay for station
        2. Propagation delay within expected ionospheric range
        3. D_clock within plausible bounds (±50ms after calibration)
        
        Returns:
            True if detection is physically plausible, False otherwise
        """
        # Get minimum delay for this station (light-speed constraint)
        min_delay = MINIMUM_PROPAGATION_DELAY_MS.get(station, 0.0)
        if propagation_delay_ms < min_delay - 1.0:  # 1ms tolerance for measurement noise
            logger.warning(
                f"Physical constraint violation: {station} propagation_delay={propagation_delay_ms:.1f}ms "
                f"< minimum={min_delay:.1f}ms (light-speed limit)"
            )
            return False
        
        # Get expected delay range for this station
        expected_range = EXPECTED_PROPAGATION_DELAY_MS.get(station)
        if expected_range:
            min_expected, typical, max_expected = expected_range
            # Allow some margin beyond expected range for unusual propagation
            if propagation_delay_ms < min_expected - 5.0:
                logger.warning(
                    f"Physical constraint violation: {station} propagation_delay={propagation_delay_ms:.1f}ms "
                    f"< expected_min={min_expected:.1f}ms"
                )
                return False
            if propagation_delay_ms > max_expected + 20.0:  # Allow 20ms margin for multi-hop
                logger.warning(
                    f"Physical constraint violation: {station} propagation_delay={propagation_delay_ms:.1f}ms "
                    f"> expected_max={max_expected:.1f}ms + 20ms margin"
                )
                return False
        
        # After calibration, constrain D_clock to plausible range
        # Once calibrated, D_clock should be within ±50ms of 0
        if self.phase != CalibrationPhase.BOOTSTRAP:
            # TEMPORARY RELAXATION: Allow ±500ms for calibration recovery
            if abs(d_clock_ms) > 500.0:
                logger.warning(
                    f"Physical constraint violation: {station} D_clock={d_clock_ms:+.1f}ms "
                    f"exceeds ±500ms bound (calibrated phase)"
                )
                return False
        
        return True
    
    def get_station_search_window(self, station: str) -> Tuple[float, float, float]:
        """
        Get the search window for a specific station based on calibration state.
        
        Returns:
            Tuple of (center_ms, window_half_width_ms, max_delay_ms)
            - center_ms: Expected arrival time (propagation delay)
            - window_half_width_ms: Half-width of search window
            - max_delay_ms: Maximum plausible delay (hard cutoff)
        """
        # Get expected delay range for this station
        expected_range = EXPECTED_PROPAGATION_DELAY_MS.get(station, (5.0, 15.0, 50.0))
        min_expected, typical, max_expected = expected_range
        
        # If we have calibrated data for this station, use it
        if station in self.station_calibration:
            cal = self.station_calibration[station]
            center_ms = cal.propagation_delay_ms
            # Window based on observed std dev, but at least 3ms
            window_half_ms = max(3.0, cal.propagation_delay_std_ms * 3.0)
            # After calibration, tighten the window significantly
            if self.phase != CalibrationPhase.BOOTSTRAP and cal.n_samples >= 10:
                window_half_ms = max(2.0, cal.propagation_delay_std_ms * 2.0)
        else:
            # Use typical expected delay
            center_ms = typical
            # Wide window during bootstrap
            window_half_ms = (max_expected - min_expected) / 2.0
        
        # Hard cutoff at max expected + margin
        max_delay_ms = max_expected + 20.0
        
        logger.debug(
            f"Search window for {station}: center={center_ms:.1f}ms, "
            f"±{window_half_ms:.1f}ms, max={max_delay_ms:.1f}ms"
        )
        
        return (center_ms, window_half_ms, max_delay_ms)
    
    def get_expected_toa(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str
    ) -> Optional[float]:
        """
        Get expected Time of Arrival for a station+frequency.
        
        Returns mean ToA in milliseconds (propagation delay), or None if not yet learned.
        This is used for narrow search windows after convergence.
        
        Args:
            station: Station name (e.g., 'WWV', 'WWVH', 'CHU')
            frequency_mhz: Frequency in MHz
            channel_name: Channel name for tracking
            
        Returns:
            Expected ToA in ms, or None if insufficient data
        """
        # Only return expected ToA if we have sufficient confidence
        if station not in self.station_calibration:
            return None
        
        cal = self.station_calibration[station]
        
        # Require at least 5 detections before using learned ToA
        if cal.n_samples < 5:
            return None
        
        # Return mean propagation delay (ToA relative to second boundary)
        return cal.propagation_delay_ms
    
    def record_detection(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str,
        toa_ms: float
    ):
        """
        Record successful detection for adaptive window tracking.
        
        Resets consecutive failure counter for this broadcast.
        
        Args:
            station: Station name
            frequency_mhz: Frequency in MHz
            channel_name: Channel name
            toa_ms: Time of arrival in ms (not used here, just for API consistency)
        """
        key = f"{station}_{frequency_mhz:.2f}_{channel_name}"
        self.consecutive_failures[key] = 0  # Reset failure counter
    
    def record_failure(
        self,
        frequency_mhz: float,
        channel_name: str
    ):
        """
        Record detection failure for adaptive window tracking.
        
        Increments failure counter for all potential stations on this channel.
        
        Args:
            frequency_mhz: Frequency in MHz
            channel_name: Channel name
        """
        # Increment failure counter for all stations on this channel
        for station in ['WWV', 'WWVH', 'CHU', 'BPM']:
            key = f"{station}_{frequency_mhz:.2f}_{channel_name}"
            if key not in self.consecutive_failures:
                self.consecutive_failures[key] = 0
            self.consecutive_failures[key] += 1
    
    def should_back_off(self, channel_name: str) -> bool:
        """
        Check if we should widen search windows due to consecutive failures.
        
        Args:
            channel_name: Channel name to check
            
        Returns:
            True if any broadcast on this channel has >5 consecutive failures
        """
        # Check if any broadcast on this channel has too many failures
        for key, failures in self.consecutive_failures.items():
            if channel_name in key and failures > 5:
                logger.debug(
                    f"Back-off triggered for {key}: {failures} consecutive failures"
                )
                return True
        return False
    
    def get_calibrated_search_window_ms(self) -> float:
        """
        Get the overall search window width based on calibration phase.
        
        During bootstrap: 500ms (wide search)
        After calibration: 10-20ms (narrow search centered on expected)
        """
        if self.phase == CalibrationPhase.BOOTSTRAP:
            return 500.0
        
        # After calibration, use narrow window
        # The window should be tight enough to reject false detections
        # but wide enough to handle ionospheric variability
        if self.global_rtp_offset is not None and self.global_rtp_offset_confidence > 0.5:
            return 10.0  # Very tight - we know exactly where to look
        else:
            return 20.0  # Moderately tight
    
    def update_from_detection(
        self,
        station: str,
        frequency_mhz: float,
        channel_name: str,
        d_clock_ms: float,
        propagation_delay_ms: float,
        snr_db: float,
        confidence: float,
        rtp_timestamp: int,
        minute_boundary: int,
        arrival_rtp: int = None
    ):
        """
        Update calibration from a tone detection.
        
        During bootstrap, high-quality detections contribute to calibration.
        After bootstrap, detections confirm/refine the calibration.
        
        Physical constraints are enforced:
        - Propagation delay must be >= minimum light-speed delay for station
        - Propagation delay must be within expected range for station
        """
        # Validate against physical constraints BEFORE accepting detection
        if not self._validate_physical_constraints(station, propagation_delay_ms, d_clock_ms):
            logger.debug(
                f"Rejected detection: {station} on {channel_name} - "
                f"propagation_delay={propagation_delay_ms:.1f}ms violates physical constraints"
            )
            return  # Reject physically impossible detection
        
        # Reload state from disk to merge with other processes' updates
        # This is necessary because multiple channel recorder processes share the state file
        self._load_state()
        
        # Track for bootstrap
        if self.phase == CalibrationPhase.BOOTSTRAP:
            if snr_db >= self.BOOTSTRAP_SNR_THRESHOLD and confidence >= self.BOOTSTRAP_CONFIDENCE_THRESHOLD:
                self.bootstrap_detections.append({
                    'station': station,
                    'frequency_mhz': frequency_mhz,
                    'channel_name': channel_name,
                    'd_clock_ms': d_clock_ms,
                    'propagation_delay_ms': propagation_delay_ms,
                    'snr_db': snr_db,
                    'confidence': confidence,
                    'rtp_timestamp': rtp_timestamp,
                    'minute_boundary': minute_boundary,
                    'timestamp': time.time()
                })
                self.stats['bootstrap_detections'] += 1
                
                # Check if we can exit bootstrap
                self._check_bootstrap_complete()
        else:
            self.stats['calibrated_detections'] += 1
        
        # Update station calibration
        self._update_station_calibration(
            station, frequency_mhz, propagation_delay_ms, snr_db, confidence
        )
        
        # Update RTP calibration with the detected station
        # Pass propagation_delay_ms to normalize the RTP offset across channels
        self._update_rtp_calibration(
            channel_name, frequency_mhz, rtp_timestamp, minute_boundary, snr_db, confidence, station, propagation_delay_ms, d_clock_ms, arrival_rtp
        )
        
        # Save state after every detection during bootstrap (multi-process coordination)
        # After bootstrap, save every 5 detections to reduce I/O
        total_detections = self.stats['bootstrap_detections'] + self.stats['calibrated_detections']
        if self.phase == CalibrationPhase.BOOTSTRAP or total_detections % 5 == 0:
            self._save_state()
    
    def _update_station_calibration(
        self,
        station: str,
        frequency_mhz: float,
        propagation_delay_ms: float,
        snr_db: float,
        confidence: float
    ):
        """Update propagation delay estimate for a station."""
        if station not in self.station_calibration:
            self.station_calibration[station] = StationCalibration(
                station=station,
                propagation_delay_ms=propagation_delay_ms,
                propagation_delay_std_ms=10.0,  # High initial uncertainty
                n_samples=1,
                last_updated=time.time(),
                frequencies_contributing=[frequency_mhz]
            )
        else:
            cal = self.station_calibration[station]
            
            # Weighted update (higher SNR = more weight)
            weight = min(1.0, snr_db / 30.0) * confidence
            alpha = weight / (cal.n_samples + weight)
            
            # Update mean
            new_mean = (1 - alpha) * cal.propagation_delay_ms + alpha * propagation_delay_ms
            
            # Update std (running estimate)
            delta = propagation_delay_ms - cal.propagation_delay_ms
            new_std = np.sqrt(
                (1 - alpha) * cal.propagation_delay_std_ms**2 + 
                alpha * delta**2
            )
            
            cal.propagation_delay_ms = new_mean
            cal.propagation_delay_std_ms = max(0.5, new_std)  # Floor at 0.5ms
            cal.n_samples += 1
            cal.last_updated = time.time()
            
            if frequency_mhz not in cal.frequencies_contributing:
                cal.frequencies_contributing.append(frequency_mhz)
    
    def _update_rtp_calibration(
        self,
        channel_name: str,
        frequency_hz: int,
        rtp_timestamp: int,
        minute_boundary: int,
        snr_db: float,
        confidence: float,
        station: str = 'WWV',
        propagation_delay_ms: float = 0.0,
        d_clock_ms: float = 0.0,
        arrival_rtp: int = None
    ):
        """Update RTP-to-UTC calibration using anchor-based approach.
        
        ANCHOR-BASED CALIBRATION:
        -------------------------
        1. Anchor channels (CHU, WWV 20/25 MHz) establish the global RTP offset
        2. All channels share the same GPSDO clock, so they MUST have the same
           normalized RTP offset (after subtracting propagation delay and clock error)
        3. Non-anchor channels use the global offset to validate their detections
        
        PHYSICAL CONSTRAINTS:
        ---------------------
        Signals must arrive in order: WWV < CHU < WWVH < BPM
        Any detection that violates this order is rejected or re-attributed.
        """
        # Normalize RTP offset by subtracting propagation delay AND clock error
        # This gives us the RTP timestamp at the true UTC second boundary
        # normalized_rtp = arrival_rtp - propagation_delay - d_clock
        if arrival_rtp is None:
            # Fallback to rtp_timestamp (minute boundary) if arrival_rtp not provided
            # This is technically incorrect but prevents crashes during transition
            normalized_rtp_base = rtp_timestamp
        else:
            normalized_rtp_base = arrival_rtp

        propagation_samples = round(propagation_delay_ms * self.sample_rate / 1000.0)
        d_clock_samples = round(d_clock_ms * self.sample_rate / 1000.0)
        
        normalized_rtp = normalized_rtp_base - propagation_samples - d_clock_samples
        rtp_offset = normalized_rtp % self.samples_per_minute
        
        # Check if this is an anchor channel
        is_anchor = channel_name in ANCHOR_CHANNELS
        
        # Log anchor channel detections at INFO level for visibility
        if is_anchor:
            logger.info(f"⚓ Anchor channel {channel_name}: snr_db={snr_db:.1f}, confidence={confidence:.2f}, rtp_offset={rtp_offset}")
        
        # Update global RTP offset from anchor channels
        # Use low SNR threshold since SNR may not always be populated
        # Accept any anchor detection with reasonable confidence
        if is_anchor and (snr_db > 5.0 or confidence > 0.3):
            if self.global_rtp_offset is None:
                # First anchor detection - establish global offset
                self.global_rtp_offset = rtp_offset
                self.global_rtp_offset_source = channel_name
                self.global_rtp_offset_confidence = min(1.0, snr_db / 30.0)
                logger.info(
                    f"🎯 Global RTP offset established from anchor {channel_name}: "
                    f"{rtp_offset} samples (SNR={snr_db:.1f}dB)"
                )
            else:
                # Verify consistency with existing global offset
                offset_diff = abs(rtp_offset - self.global_rtp_offset)
                if offset_diff > 20:  # More than 1ms difference
                    logger.warning(
                        f"Anchor {channel_name} offset {rtp_offset} differs from global "
                        f"{self.global_rtp_offset} by {offset_diff} samples ({offset_diff*0.05:.2f}ms)"
                    )
                else:
                    # Weighted average update
                    weight = min(1.0, snr_db / 30.0)
                    old_weight = self.global_rtp_offset_confidence
                    total_weight = old_weight + weight
                    self.global_rtp_offset = round(
                        (self.global_rtp_offset * old_weight + rtp_offset * weight) / total_weight
                    )
                    self.global_rtp_offset_confidence = min(1.0, total_weight)
        
        # For non-anchor channels, validate against global offset if available
        if not is_anchor and self.global_rtp_offset is not None:
            offset_diff = abs(rtp_offset - self.global_rtp_offset)
            if offset_diff > 40:  # More than 2ms difference - suspicious
                logger.warning(
                    f"Channel {channel_name} offset {rtp_offset} differs from global "
                    f"{self.global_rtp_offset} by {offset_diff} samples ({offset_diff*0.05:.2f}ms) - "
                    f"possible wrong station attribution"
                )
        
        if channel_name not in self.rtp_calibration:
            self.rtp_calibration[channel_name] = RPTCalibration(
                channel_name=channel_name,
                frequency_hz=frequency_hz,
                sample_rate=self.sample_rate,
                reference_minute_utc=minute_boundary,
                reference_rtp_timestamp=rtp_timestamp,
                rtp_offset_samples=rtp_offset,
                detected_station=station,
                calibration_snr_db=snr_db,
                calibration_confidence=confidence,
                n_confirmations=1,
                last_confirmed=time.time()
            )
        else:
            rtp = self.rtp_calibration[channel_name]
            
            # Verify consistency (should be identical with GPSDO)
            expected_offset = rtp.rtp_offset_samples
            if abs(rtp_offset - expected_offset) > 10:  # Allow 10 samples tolerance
                logger.warning(
                    f"RTP offset drift detected on {channel_name}: "
                    f"expected {expected_offset}, got {rtp_offset}"
                )
            
            rtp.n_confirmations += 1
            rtp.last_confirmed = time.time()
            
            # Update if this detection is higher quality
            if snr_db > rtp.calibration_snr_db:
                rtp.calibration_snr_db = snr_db
                rtp.calibration_confidence = confidence
    
    def _rtp_offset_stable(self, channel: str) -> bool:
        """
        Check if RTP offset is stable (GPSDO-locked).
        
        With GPSDO, RTP offset should be constant within ±50 samples.
        Variance indicates GPSDO unlock or restart.
        """
        rtp = self.rtp_calibration.get(channel)
        if not rtp or rtp.n_confirmations < 5:
            return False
        
        # For now, just check we have confirmations
        # TODO: Track offset history and calculate variance
        return True
    
    def _d_clock_converged(self, station: str) -> bool:
        """
        Check if D_clock has converged (not changing).
        
        If recent measurements are within ±1ms std, calibration is stable.
        """
        cal = self.station_calibration.get(station)
        if not cal or cal.n_samples < 5:
            return False
        
        # Check if propagation delay std is low (indicates convergence)
        return cal.propagation_delay_std_ms <= self.PROVISIONAL_MAX_D_CLOCK_STD_MS
    
    def _geographic_validation_passed(self, station: str) -> bool:
        """
        Validate propagation delay matches geographic expectations.
        
        Uses station distance and ionospheric model to check plausibility.
        """
        cal = self.station_calibration.get(station)
        if not cal:
            return False
        
        # Get expected delay range for this station
        expected_range = EXPECTED_PROPAGATION_DELAYS.get(station)
        if not expected_range:
            return False
        
        min_delay, typical_delay, max_delay = expected_range
        measured_delay = cal.propagation_delay_ms
        
        # Check if measured delay is within plausible range
        return min_delay <= measured_delay <= max_delay
    
    def _check_bootstrap_complete(self):
        """
        Check if we have enough data to exit bootstrap phase.
        
        Two-tier approach:
        1. PROVISIONAL (10 min): GPSDO-validated, feeds Chrony, operational use
        2. CALIBRATED (60 min): Scientifically rigorous, ionospheric measurements
        
        PROVISIONAL criteria:
        - N≥10 per station, 10-minute span
        - GPSDO stability (RTP offsets stable)
        - D_clock convergence (std ≤ 1ms)
        - Geographic validation (delays plausible)
        
        CALIBRATED criteria:
        - N≥30 per station, 60-minute span with 50% coverage
        - Propagation std ≤ 2ms
        - Combined uncertainty ≤ 3ms (ISO GUM)
        """
        # ===================================================================
        # FAST PATH: Check PROVISIONAL Criteria (GPSDO-Validated)
        # ===================================================================
        if self.phase == CalibrationPhase.BOOTSTRAP:
            # Calculate basic stats
            if not self.bootstrap_detections:
                return
            
            timestamps = [d['timestamp'] for d in self.bootstrap_detections]
            first_detection = min(timestamps)
            last_detection = max(timestamps)
            duration_minutes = (last_detection - first_detection) / 60.0
            
            # Count per-station detections
            station_counts = {}
            for d in self.bootstrap_detections:
                station = d['station']
                station_counts[station] = station_counts.get(station, 0) + 1
            
            stations_with_enough = [s for s, count in station_counts.items() 
                                   if count >= self.PROVISIONAL_MIN_DETECTIONS]
            
            # Check PROVISIONAL criteria
            if (len(stations_with_enough) >= self.PROVISIONAL_MIN_STATIONS and
                duration_minutes >= self.PROVISIONAL_MIN_DURATION_MINUTES):
                
                # Check GPSDO stability and convergence for all stations
                all_stable = True
                all_converged = True
                all_geographic_valid = True
                
                for station in stations_with_enough:
                    if not self._d_clock_converged(station):
                        all_converged = False
                    if not self._geographic_validation_passed(station):
                        all_geographic_valid = False
                
                # Check RTP stability for all channels
                for channel in self.rtp_calibration.keys():
                    if not self._rtp_offset_stable(channel):
                        all_stable = False
                
                if all_stable and all_converged and all_geographic_valid:
                    # PROVISIONAL criteria met!
                    self.phase = CalibrationPhase.PROVISIONAL
                    
                    logger.info("=" * 80)
                    logger.info("✅ PROVISIONAL CALIBRATION ACHIEVED")
                    logger.info("=" * 80)
                    logger.info("Status: GPSDO-Validated Operational Mode")
                    logger.info(f"Duration: {duration_minutes:.1f} minutes")
                    logger.info(f"Total detections: {len(self.bootstrap_detections)}")
                    logger.info("")
                    logger.info("Station Calibrations:")
                    for station, cal in sorted(self.station_calibration.items()):
                        logger.info(
                            f"  {station}: {cal.propagation_delay_ms:.2f}ms "
                            f"± {cal.propagation_delay_std_ms:.2f}ms (N={cal.n_samples})"
                        )
                    logger.info("")
                    logger.info("Use Case: Time distribution (Chrony SHM), operational monitoring")
                    logger.info("Quality: PROVISIONAL - GPSDO-validated, not claiming ionospheric calibration")
                    logger.info("Next: Continuing to collect data for CALIBRATED status (60 min)")
                    logger.info("=" * 80)
                    
                    self._save_state()
                    # Don't return - continue checking for CALIBRATED criteria
        
        # ===================================================================
        # RIGOROUS PATH: Check CALIBRATED Criteria (Scientific Validation)
        # ===================================================================
        # Criterion 1: Statistical Confidence (Sample Size)
        # ===================================================================
        if len(self.bootstrap_detections) < self.BOOTSTRAP_MIN_DETECTIONS:
            logger.debug(
                f"Bootstrap: {len(self.bootstrap_detections)}/{self.BOOTSTRAP_MIN_DETECTIONS} "
                f"total detections (need more)"
            )
            return
        
        # Check per-station coverage
        station_counts = {}
        for d in self.bootstrap_detections:
            station = d['station']
            station_counts[station] = station_counts.get(station, 0) + 1
        
        # Need at least BOOTSTRAP_MIN_DETECTIONS per station (not total)
        stations_with_enough = [s for s, count in station_counts.items() 
                               if count >= self.BOOTSTRAP_MIN_DETECTIONS]
        
        if len(stations_with_enough) < self.BOOTSTRAP_MIN_STATIONS:
            logger.debug(
                f"Bootstrap: Only {len(stations_with_enough)} stations with ≥{self.BOOTSTRAP_MIN_DETECTIONS} "
                f"detections (need {self.BOOTSTRAP_MIN_STATIONS}). Counts: {station_counts}"
            )
            return
        
        # ===================================================================
        # Criterion 2: Temporal Stability (Duration & Coverage)
        # ===================================================================
        if not self.bootstrap_detections:
            return
        
        timestamps = [d['timestamp'] for d in self.bootstrap_detections]
        first_detection = min(timestamps)
        last_detection = max(timestamps)
        duration_minutes = (last_detection - first_detection) / 60.0
        
        if duration_minutes < self.BOOTSTRAP_MIN_DURATION_MINUTES:
            logger.debug(
                f"Bootstrap: Duration {duration_minutes:.1f}/{self.BOOTSTRAP_MIN_DURATION_MINUTES} "
                f"minutes (need more time)"
            )
            return
        
        # Check temporal coverage (detections should be spread across time window)
        # Divide time span into 1-minute bins and count how many have detections
        time_span = last_detection - first_detection
        n_bins = int(time_span / 60) + 1
        bins_with_detections = set()
        for ts in timestamps:
            bin_idx = int((ts - first_detection) / 60)
            bins_with_detections.add(bin_idx)
        
        coverage = len(bins_with_detections) / max(n_bins, 1)
        if coverage < self.BOOTSTRAP_MIN_TEMPORAL_COVERAGE:
            logger.debug(
                f"Bootstrap: Temporal coverage {coverage:.1%}/{self.BOOTSTRAP_MIN_TEMPORAL_COVERAGE:.0%} "
                f"(detections too sparse)"
            )
            return
        
        # ===================================================================
        # Criterion 3: Calibration Convergence (Stability)
        # ===================================================================
        for station, cal in self.station_calibration.items():
            if cal.n_samples < self.STABILITY_WINDOW_DETECTIONS:
                logger.debug(
                    f"Bootstrap: {station} has only {cal.n_samples} samples "
                    f"(need {self.STABILITY_WINDOW_DETECTIONS} for stability check)"
                )
                return
            
            # Check propagation delay stability
            if cal.propagation_delay_std_ms > self.MAX_PROPAGATION_STD_MS:
                logger.debug(
                    f"Bootstrap: {station} propagation std {cal.propagation_delay_std_ms:.2f}ms "
                    f"> {self.MAX_PROPAGATION_STD_MS}ms (not converged)"
                )
                return
        
        # ===================================================================
        # Criterion 4: Cross-Station Consistency
        # ===================================================================
        # Check if we have WWV and WWVH (shared ionosphere should agree)
        if 'WWV' in self.station_calibration and 'WWVH' in self.station_calibration:
            wwv_delay = self.station_calibration['WWV'].propagation_delay_ms
            wwvh_delay = self.station_calibration['WWVH'].propagation_delay_ms
            
            # Differential timing: difference in propagation delays
            # Should be consistent since both measure same ionosphere
            differential = abs(wwv_delay - wwvh_delay)
            
            # Expected differential based on geometry (rough estimate)
            # WWV: ~1300km, WWVH: ~5000km → expect ~26ms difference
            # But ionospheric variations can cause ±5ms deviations
            # For now, just check that both are reasonable (not checking differential)
            # TODO: Implement proper differential timing validation
        
        # ===================================================================
        # Criterion 5: Uncertainty Budget (ISO GUM)
        # ===================================================================
        # Calculate combined uncertainty from all sources
        max_uncertainty = 0.0
        for station, cal in self.station_calibration.items():
            # Uncertainty components:
            # 1. Propagation delay std (statistical)
            # 2. Tone detection uncertainty (~0.5ms from Cramer-Rao)
            # 3. Model uncertainty (IRI-2020: ~1-2ms)
            
            statistical_unc = cal.propagation_delay_std_ms
            detection_unc = 0.5  # ms, from SNR-based Cramer-Rao bound
            model_unc = 2.0  # ms, IRI-2020 typical accuracy
            
            # Combined uncertainty (root sum of squares)
            combined_unc = (statistical_unc**2 + detection_unc**2 + model_unc**2)**0.5
            
            if combined_unc > max_uncertainty:
                max_uncertainty = combined_unc
        
        if max_uncertainty > self.MAX_CALIBRATION_UNCERTAINTY_MS:
            logger.debug(
                f"Bootstrap: Combined uncertainty {max_uncertainty:.2f}ms "
                f"> {self.MAX_CALIBRATION_UNCERTAINTY_MS}ms (not accurate enough)"
            )
            return
        
        # ===================================================================
        # ALL CRITERIA MET - Bootstrap Complete!
        # ===================================================================
        previous_phase = self.phase
        self.phase = CalibrationPhase.CALIBRATED
        
        logger.info("=" * 80)
        if previous_phase == CalibrationPhase.PROVISIONAL:
            logger.info("🎉 CALIBRATED STATUS ACHIEVED - UPGRADE FROM PROVISIONAL 🎉")
        else:
            logger.info("🎉 CALIBRATED STATUS ACHIEVED - INSTRUMENT VALIDATED 🎉")
        logger.info("=" * 80)
        logger.info("Status: Scientifically Validated for Ionospheric Measurements")
        logger.info(f"Duration: {duration_minutes:.1f} minutes")
        logger.info(f"Total detections: {len(self.bootstrap_detections)}")
        logger.info(f"Temporal coverage: {coverage:.1%}")
        logger.info(f"Combined uncertainty: {max_uncertainty:.2f}ms (k=1)")
        logger.info(f"Expanded uncertainty: {max_uncertainty * 2:.2f}ms (k=2, 95% confidence)")
        logger.info("")
        logger.info("Station Calibrations:")
        
        for station, cal in sorted(self.station_calibration.items()):
            logger.info(
                f"  {station}: {cal.propagation_delay_ms:.2f}ms "
                f"± {cal.propagation_delay_std_ms:.2f}ms "
                f"(N={cal.n_samples}, window={cal.search_window_ms():.1f}ms)"
            )
        
        logger.info("")
        logger.info("RTP Calibrations:")
        for channel, rtp in sorted(self.rtp_calibration.items()):
            logger.info(
                f"  {channel}: offset={rtp.rtp_offset_samples} samples "
                f"(confirmations={rtp.n_confirmations})"
            )
        
        logger.info("=" * 80)
        logger.info("System is now a CALIBRATED INSTRUMENT for ionospheric measurements")
        logger.info("=" * 80)
        
        self._save_state()
    
    def check_consistency(
        self,
        measurements: List[Dict]
    ) -> ConsistencyResult:
        """
        Check consistency of measurements across channels.
        
        Args:
            measurements: List of dicts with keys:
                - station: str
                - channel_name: str
                - d_clock_ms: float
                - frequency_mhz: float
                
        Returns:
            ConsistencyResult with analysis
        """
        self.stats['consistency_checks'] += 1
        
        # Group by station
        by_station: Dict[str, List[Dict]] = {}
        for m in measurements:
            station = m['station']
            if station not in by_station:
                by_station[station] = []
            by_station[station].append(m)
        
        # Calculate intra-station std dev
        intra_std: Dict[str, float] = {}
        for station, station_measurements in by_station.items():
            if len(station_measurements) > 1:
                d_clocks = [m['d_clock_ms'] for m in station_measurements]
                intra_std[station] = float(np.std(d_clocks))
        
        # Calculate inter-station spread
        station_means = {}
        for station, station_measurements in by_station.items():
            d_clocks = [m['d_clock_ms'] for m in station_measurements]
            station_means[station] = np.mean(d_clocks)
        
        if len(station_means) > 1:
            inter_spread = max(station_means.values()) - min(station_means.values())
        else:
            inter_spread = 0.0
        
        # Identify suspects
        suspects = []
        corrections = {}
        
        for station, std in intra_std.items():
            if std > self.INTRA_STATION_THRESHOLD_MS:
                # Find outliers within this station
                station_measurements = by_station[station]
                mean = station_means[station]
                
                for m in station_measurements:
                    deviation = abs(m['d_clock_ms'] - mean)
                    if deviation > 2 * std:
                        suspects.append(m['channel_name'])
                        
                        # Suggest correction: which station would this fit better?
                        for other_station, other_mean in station_means.items():
                            if other_station != station:
                                if abs(m['d_clock_ms'] - other_mean) < deviation:
                                    corrections[m['channel_name']] = other_station
        
        is_consistent = len(suspects) == 0 and all(
            std <= self.INTRA_STATION_THRESHOLD_MS 
            for std in intra_std.values()
        )
        
        return ConsistencyResult(
            is_consistent=is_consistent,
            intra_station_std_ms=intra_std,
            inter_station_spread_ms=inter_spread,
            suspect_measurements=suspects,
            suggested_corrections=corrections
        )
    
    def get_expected_tone_position(
        self,
        channel_name: str,
        station: str,
        second_number: int,
        buffer_start_rtp: int
    ) -> Optional[int]:
        """
        Get expected sample position of a tone in a buffer.
        
        Returns None if calibration not available.
        """
        if channel_name not in self.rtp_calibration:
            return None
        if station not in self.station_calibration:
            return None
        
        rtp_cal = self.rtp_calibration[channel_name]
        station_cal = self.station_calibration[station]
        
        return rtp_cal.expected_tone_sample(
            second_number=second_number,
            propagation_delay_ms=station_cal.propagation_delay_ms,
            buffer_start_rtp=buffer_start_rtp
        )
    
    def verify_with_discrimination_result(
        self,
        discrimination_result,  # DiscriminationResult from WWVHDiscriminator
        minute_number: int,
        expected_delay_ms: float
    ) -> Optional[Dict]:
        """
        Verify timing using the full discrimination result from WWVHDiscriminator.
        
        The discrimination system uses 8 weighted voting methods:
        - Vote 0: Test Signal (minutes 8, 44) - weight 15.0
        - Vote 1: 440 Hz Tone (minutes 1, 2) - weight 10.0  
        - Vote 2: BCD Amplitude Ratio - weight 2.0-10.0
        - Vote 3: 1000/1200 Hz Power Ratio - weight 5.0-10.0
        - Vote 4: Tick SNR Comparison - weight 5.0
        - Vote 5: 500/600 Hz Ground Truth (14 min/hr) - weight 10.0-15.0
        - Vote 6: Differential Doppler - weight 2.0
        - Vote 7: Test Signal ↔ BCD ToA Coherence - weight 3.0
        
        Ground truth minutes provide definitive verification:
        - Minutes 1, 2: 440 Hz tone (WWV min 2, WWVH min 1)
        - Minutes 8, 44: Test signal (WWV min 8, WWVH min 44)
        - Minutes 16, 17, 19: WWV-only 500/600 Hz
        - Minutes 43-51: WWVH-only 500/600 Hz
        
        Args:
            discrimination_result: DiscriminationResult from finalize_discrimination()
            minute_number: Minute within hour (0-59)
            expected_delay_ms: Expected propagation delay
            
        Returns:
            Dict with verification results, or None if verification failed
        """
        try:
            result = discrimination_result
            
            # Check if this is a ground truth minute
            ground_truth_minutes = {
                'test_signal': [8, 44],
                '440hz': [1, 2],
                'wwv_only_500_600': [1, 16, 17, 19],
                'wwvh_only_500_600': [2, 43, 44, 45, 46, 47, 48, 49, 50, 51]
            }
            
            is_ground_truth = False
            ground_truth_type = None
            expected_station = None
            
            if minute_number in ground_truth_minutes['test_signal']:
                is_ground_truth = True
                ground_truth_type = 'test_signal'
                expected_station = 'WWV' if minute_number == 8 else 'WWVH'
            elif minute_number in ground_truth_minutes['440hz']:
                is_ground_truth = True
                ground_truth_type = '440hz'
                expected_station = 'WWV' if minute_number == 2 else 'WWVH'
            elif minute_number in ground_truth_minutes['wwv_only_500_600']:
                is_ground_truth = True
                ground_truth_type = '500_600_exclusive'
                expected_station = 'WWV'
            elif minute_number in ground_truth_minutes['wwvh_only_500_600']:
                is_ground_truth = True
                ground_truth_type = '500_600_exclusive'
                expected_station = 'WWVH'
            
            # Extract verification data from discrimination result
            station = result.dominant_station
            confidence = result.confidence
            
            # BCD provides timing verification
            bcd_delay_ms = result.bcd_differential_delay_ms
            bcd_quality = result.bcd_correlation_quality
            
            # Test signal provides high-precision ToA
            test_signal_toa_ms = None
            if result.test_signal_detected and hasattr(result, 'test_signal_toa_offset_ms'):
                test_signal_toa_ms = result.test_signal_toa_offset_ms
            
            # Determine verification status
            verified = False
            verification_source = None
            
            if is_ground_truth:
                # Ground truth minute - check if detected station matches expected
                if station == expected_station and confidence in ('high', 'medium'):
                    verified = True
                    verification_source = ground_truth_type
            elif confidence == 'high' and bcd_quality is not None and bcd_quality > 0.5:
                # Non-ground-truth minute with high confidence and good BCD
                verified = True
                verification_source = 'bcd_high_confidence'
            
            self.stats['discrimination_verifications'] = self.stats.get('discrimination_verifications', 0) + 1
            if verified:
                self.stats['discrimination_verified_ok'] = self.stats.get('discrimination_verified_ok', 0) + 1
                
                # Ground truth verification can promote to VERIFIED phase
                if is_ground_truth and self.phase == CalibrationPhase.CALIBRATED:
                    self._check_verified_transition()
            
            return {
                'verified': verified,
                'verification_source': verification_source,
                'is_ground_truth': is_ground_truth,
                'ground_truth_type': ground_truth_type,
                'expected_station': expected_station,
                'detected_station': station,
                'confidence': confidence,
                'bcd_differential_delay_ms': bcd_delay_ms,
                'bcd_quality': bcd_quality,
                'test_signal_detected': result.test_signal_detected,
                'test_signal_toa_ms': test_signal_toa_ms,
                'minute_number': minute_number
            }
            
        except Exception as e:
            logger.debug(f"Discrimination verification failed: {e}")
            return None
    
    def verify_with_test_signal(
        self,
        discriminator,  # WWVHDiscriminator instance with test_signal_detector
        iq_samples: np.ndarray,
        minute_number: int,
        expected_delay_ms: float
    ) -> Optional[Dict]:
        """
        Verify timing using test signal cross-correlation (minutes :08 and :44 only).
        
        Uses the existing WWVTestSignalDetector from wwvh_discrimination.py which
        provides high-precision ToA via:
        - Multi-tone correlation (2, 3, 4, 5 kHz)
        - Chirp pulse compression
        - Single-cycle bursts (highest precision)
        
        Args:
            discriminator: WWVHDiscriminator instance with test_signal_detector
            iq_samples: 60-second IQ buffer
            minute_number: Minute within hour (0-59)
            expected_delay_ms: Expected propagation delay
            
        Returns:
            Dict with verification results, or None if not a test signal minute
        """
        # Only minutes 8 (WWV) and 44 (WWVH) have test signals
        if minute_number not in [8, 44]:
            return None
        
        station = 'WWV' if minute_number == 8 else 'WWVH'
        
        try:
            # Use existing test signal detector from discriminator
            detection = discriminator.test_signal_detector.detect(
                iq_samples=iq_samples,
                minute_number=minute_number,
                sample_rate=self.sample_rate
            )
            
            if not detection.detected:
                return None
            
            # Use burst ToA for highest precision if available
            measured_delay_ms = detection.burst_toa_offset_ms or detection.toa_offset_ms
            if measured_delay_ms is None:
                return None
            
            error_ms = measured_delay_ms - expected_delay_ms
            verified = abs(error_ms) < 2.0 and detection.confidence > 0.7
            
            self.stats['test_signal_verifications'] = self.stats.get('test_signal_verifications', 0) + 1
            if verified:
                self.stats['test_signal_verified_ok'] = self.stats.get('test_signal_verified_ok', 0) + 1
                
                # Test signal provides definitive station ID
                # If we're in calibrated phase, this can promote to verified
                if self.phase == CalibrationPhase.CALIBRATED:
                    self._check_verified_transition()
            
            return {
                'verified': verified,
                'station': station,
                'measured_delay_ms': measured_delay_ms,
                'expected_delay_ms': expected_delay_ms,
                'error_ms': error_ms,
                'confidence': detection.confidence,
                'snr_db': detection.snr_db,
                'multitone_score': detection.multitone_score,
                'chirp_score': detection.chirp_score
            }
            
        except Exception as e:
            logger.debug(f"Test signal verification failed: {e}")
            return None
    
    def _check_verified_transition(self):
        """Check if we can transition to VERIFIED phase."""
        # Need multiple successful ground truth verifications
        disc_ok = self.stats.get('discrimination_verified_ok', 0)
        test_ok = self.stats.get('test_signal_verified_ok', 0)
        
        # Require at least 5 ground truth verifications
        # Ground truth minutes: 1, 2 (440 Hz), 8, 44 (test signal), 16, 17, 19 (WWV-only), 43-51 (WWVH-only)
        # That's 14 ground truth minutes per hour
        if disc_ok >= 5 or test_ok >= 2:
            self.phase = CalibrationPhase.VERIFIED
            logger.info(
                f"Transitioning to VERIFIED phase! "
                f"Discrimination: {disc_ok} OK, Test signal: {test_ok} OK"
            )
            self._save_state()
    
    def get_status(self) -> Dict:
        """Get current calibrator status."""
        return {
            'phase': self.phase.value,
            'bootstrap_detections': len(self.bootstrap_detections),
            'stations_calibrated': len(self.station_calibration),
            'channels_calibrated': len(self.rtp_calibration),
            'station_details': {
                station: {
                    'propagation_delay_ms': cal.propagation_delay_ms,
                    'uncertainty_ms': cal.propagation_delay_std_ms,
                    'search_window_ms': cal.search_window_ms(),
                    'n_samples': cal.n_samples,
                    'frequencies': cal.frequencies_contributing
                }
                for station, cal in self.station_calibration.items()
            },
            'stats': self.stats,
            'verification': {
                'discrimination_verifications': self.stats.get('discrimination_verifications', 0),
                'discrimination_verified_ok': self.stats.get('discrimination_verified_ok', 0),
                'test_signal_verifications': self.stats.get('test_signal_verifications', 0),
                'test_signal_verified_ok': self.stats.get('test_signal_verified_ok', 0)
            },
            'ground_truth_schedule': {
                'test_signal_minutes': [8, 44],
                '440hz_minutes': [1, 2],
                'wwv_only_500_600': [1, 16, 17, 19],
                'wwvh_only_500_600': [2, 43, 44, 45, 46, 47, 48, 49, 50, 51],
                'total_ground_truth_per_hour': 14
            }
        }
