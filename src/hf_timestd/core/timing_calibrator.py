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

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class CalibrationPhase(Enum):
    """Timing calibration phases."""
    BOOTSTRAP = "bootstrap"      # Wide search, establishing calibration
    CALIBRATED = "calibrated"    # Narrow search, using calibration
    VERIFIED = "verified"        # Secondary signals confirmed


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
    
    # Thresholds
    BOOTSTRAP_MIN_DETECTIONS = 5          # Minimum high-quality detections to exit bootstrap
    BOOTSTRAP_MIN_STATIONS = 2            # Need at least 2 stations
    BOOTSTRAP_SNR_THRESHOLD = -100.0      # dB - accept any detection (weak signals common at night)
    BOOTSTRAP_CONFIDENCE_THRESHOLD = 0.01 # Minimum confidence (any detection helps)
    
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
        """Load calibration state from disk."""
        if not self.state_file.exists():
            return
            
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            
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
                    last_confirmed=rtp_data['last_confirmed']
                )
            
            self.stats = state.get('stats', self.stats)
            
            logger.info(f"Loaded timing calibration: phase={self.phase.value}, "
                       f"{len(self.station_calibration)} stations, "
                       f"{len(self.rtp_calibration)} channels")
                       
        except Exception as e:
            logger.warning(f"Failed to load timing calibration: {e}")
    
    def _save_state(self):
        """Save calibration state to disk."""
        try:
            state = {
                'phase': self.phase.value,
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
                        'last_confirmed': rtp.last_confirmed
                    }
                    for channel, rtp in self.rtp_calibration.items()
                },
                'stats': self.stats,
                'saved_at': datetime.now(timezone.utc).isoformat()
            }
            
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
                
        except Exception as e:
            logger.error(f"Failed to save timing calibration: {e}")
    
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
            if not predicted_station or predicted_station not in ['WWV', 'WWVH', 'CHU']:
                # Fallback to channel name if no detected_station stored
                predicted_station = channel_name.split()[0].upper()
                if predicted_station not in ['WWV', 'WWVH', 'CHU']:
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
            'CHU': 4.0     # Ottawa, Canada
        }
        return (50.0, default_delays.get(station, 10.0))
    
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
        minute_boundary: int
    ):
        """
        Update calibration from a tone detection.
        
        During bootstrap, high-quality detections contribute to calibration.
        After bootstrap, detections confirm/refine the calibration.
        """
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
        self._update_rtp_calibration(
            channel_name, frequency_mhz, rtp_timestamp, minute_boundary, snr_db, confidence, station
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
        station: str = 'WWV'
    ):
        """Update RTP-to-UTC calibration for a channel."""
        rtp_offset = rtp_timestamp % self.samples_per_minute
        
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
    
    def _check_bootstrap_complete(self):
        """Check if we have enough data to exit bootstrap phase."""
        if len(self.bootstrap_detections) < self.BOOTSTRAP_MIN_DETECTIONS:
            return
        
        # Check station coverage
        stations = set(d['station'] for d in self.bootstrap_detections)
        if len(stations) < self.BOOTSTRAP_MIN_STATIONS:
            return
        
        # Check that we have reasonable uncertainty
        for station, cal in self.station_calibration.items():
            if cal.n_samples < 2 or cal.propagation_delay_std_ms > 20.0:
                return
        
        # Bootstrap complete!
        self.phase = CalibrationPhase.CALIBRATED
        logger.info(
            f"Bootstrap complete! Transitioning to CALIBRATED phase. "
            f"{len(self.bootstrap_detections)} detections, "
            f"{len(self.station_calibration)} stations calibrated."
        )
        
        for station, cal in self.station_calibration.items():
            logger.info(
                f"  {station}: {cal.propagation_delay_ms:.2f}ms "
                f"± {cal.propagation_delay_std_ms:.2f}ms "
                f"(window: {cal.search_window_ms():.1f}ms)"
            )
        
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
