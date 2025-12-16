"""
GPSDO Monitoring Architecture for HF Time Standard

This module implements the "Set, Monitor, Intervention" strategy for GPSDO-disciplined
timing. Instead of constantly recalibrating the time anchor from noisy tone detections,
we trust the GPSDO's "steel ruler" and use tone detections only for verification.

Architecture:
    STARTUP → STEADY_STATE → (HOLDOVER) → STEADY_STATE
                   ↓
           Sample Loss Detected → REANCHOR_REQUIRED → STARTUP

Key Principles:
    1. Trust the Counter: Determine time by counting samples from the last known good
       anchor (TimeSnapReference), trusting the GPSDO hardware.
    
    2. Verify, Don't Reset: Use received tones to audit the counter. Only update the
       TimeSnapReference if error exceeds a "Physics Threshold" (> 3ms, which is
       impossible from just ionospheric jitter).
    
    3. Fail-Safe: Instantaneously invalidate TimeSnapReference if radiod reports
       dropped packets, forcing fresh anchor establishment.

Monitors:
    A. Sample Integrity Watchdog - Detects if "ruler" broke (data loss)
    B. Drift Watchdog - Confirms GPSDO hasn't unlocked (PPM alarm)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Dict, Any, Tuple, List

from ..interfaces.data_models import TimeSnapReference, QualityInfo, ToneDetectionResult

logger = logging.getLogger(__name__)


class AnchorState(Enum):
    """State of the GPSDO time anchor system."""
    
    STARTUP = auto()
    """No anchor established yet. Need full Pass 0 search."""
    
    STEADY_STATE = auto()
    """Anchor established and verified. Trust the sample counter."""
    
    HOLDOVER = auto()
    """GPSDO may be unlocked (drift alarm). Data quality flagged but not invalidated."""
    
    REANCHOR_REQUIRED = auto()
    """Sample integrity lost (gaps/discontinuities). Must re-establish anchor."""


@dataclass
class VerificationResult:
    """Result of verifying tone arrival against GPSDO projection."""
    
    expected_sample: int
    """Expected sample position based on GPSDO projection."""
    
    actual_sample: int
    """Actual sample position where tone was detected."""
    
    error_ms: float
    """Difference between expected and actual arrival (ms)."""
    
    within_jitter_threshold: bool
    """True if error within normal ionospheric jitter (±1ms)."""
    
    within_physics_threshold: bool
    """True if error within physical possibility (±5ms)."""
    
    requires_reanchor: bool
    """True if error indicates sample counter is broken."""
    
    station: str
    """Station that provided the verification."""
    
    snr_db: float
    """SNR of the verification detection."""


@dataclass
class GPSDOMonitorState:
    """Persistent state for the GPSDO monitor."""
    
    anchor_state: AnchorState = AnchorState.STARTUP
    """Current state of the anchor system."""
    
    consecutive_verifications: int = 0
    """Number of consecutive successful verifications."""
    
    last_verification_time: Optional[float] = None
    """Unix time of last successful verification."""
    
    last_verification_error_ms: float = 0.0
    """Error from last verification (for trend analysis)."""
    
    best_channel: Optional[str] = None
    """Best channel from previous minute (for optimized search)."""
    
    best_station: Optional[str] = None
    """Best station from previous minute."""
    
    holdover_since: Optional[float] = None
    """Unix time when holdover mode started (if applicable)."""
    
    total_reanchors: int = 0
    """Count of times we've had to re-establish anchor."""
    
    verification_history: List[float] = field(default_factory=list)
    """Recent verification errors for trend analysis (last 10)."""


class GPSDOMonitor:
    """
    Monitors GPSDO timing integrity and manages the "steel ruler" anchor.
    
    This class implements the transition from "corrective" to "monitoring"
    architecture. Instead of recalibrating every minute, we:
    
    1. Establish anchor once (STARTUP → STEADY_STATE)
    2. Project expected sample positions using GPSDO count
    3. Verify actual tone arrivals against projection
    4. Only re-anchor if sample integrity is lost
    
    Thresholds:
        JITTER_THRESHOLD_MS: Normal ionospheric jitter (0.1ms typical, 1ms max)
        PHYSICS_THRESHOLD_MS: Maximum physically possible error (5ms)
        REANCHOR_THRESHOLD_MS: Error indicating broken sample count (50ms)
        DRIFT_ALARM_PPM: Maximum acceptable GPSDO drift (0.1 PPM)
    
    Usage:
        monitor = GPSDOMonitor(sample_rate=20000)
        
        # At startup or after gap
        if monitor.needs_anchor():
            detections = do_full_pass0_search()
            monitor.establish_anchor(time_snap, detections)
        
        # In steady state
        result = monitor.verify_projection(tone_detection, time_snap)
        if result.requires_reanchor:
            monitor.invalidate_anchor("Sample count mismatch")
    """
    
    # Timing thresholds (ms)
    JITTER_THRESHOLD_MS = 1.0      # Normal ionospheric jitter
    PHYSICS_THRESHOLD_MS = 5.0     # Maximum physical possibility
    REANCHOR_THRESHOLD_MS = 50.0   # Definitely broken sample count
    
    # GPSDO health thresholds
    DRIFT_ALARM_PPM = 0.1          # GPSDO should be < 0.001 PPM when locked
    
    # State machine parameters
    MIN_VERIFICATIONS_FOR_STEADY = 3  # Consecutive verifications before steady state
    MAX_HOLDOVER_MINUTES = 10         # Max minutes in holdover before forcing reanchor
    
    def __init__(self, sample_rate: int = 20000):
        """
        Initialize the GPSDO monitor.
        
        Args:
            sample_rate: RTP sample rate (Hz)
        """
        self.sample_rate = sample_rate
        self.state = GPSDOMonitorState()
    
    @property
    def anchor_state(self) -> AnchorState:
        """Current anchor state."""
        return self.state.anchor_state
    
    def needs_anchor(self) -> bool:
        """Check if we need to perform a full anchor search."""
        return self.state.anchor_state in (AnchorState.STARTUP, AnchorState.REANCHOR_REQUIRED)
    
    def is_steady_state(self) -> bool:
        """Check if we're in steady state (trusting the counter)."""
        return self.state.anchor_state == AnchorState.STEADY_STATE
    
    def is_holdover(self) -> bool:
        """Check if we're in holdover mode."""
        return self.state.anchor_state == AnchorState.HOLDOVER
    
    # =========================================================================
    # MONITOR A: Sample Integrity Watchdog
    # =========================================================================
    
    def check_sample_integrity(self, quality: QualityInfo) -> bool:
        """
        Check if sample integrity is maintained (no gaps/drops).
        
        This is Monitor A - the Sample Integrity Watchdog. If we detect any
        discontinuities, the "steel ruler" is broken and we must re-anchor.
        
        Args:
            quality: QualityInfo from the current batch
            
        Returns:
            True if integrity is maintained, False if anchor must be invalidated
        """
        if quality.gap_count > 0 or quality.packet_loss_pct > 0.0:
            logger.error(
                f"🔴 SAMPLE INTEGRITY LOST: {quality.gap_count} gaps, "
                f"{quality.packet_loss_pct:.2f}% packet loss, "
                f"{quality.gap_duration_ms:.1f}ms total gap"
            )
            self.invalidate_anchor("RTP discontinuity detected")
            return False
        
        return True
    
    # =========================================================================
    # MONITOR B: Drift Watchdog (GPSDO Health)
    # =========================================================================
    
    def check_drift_health(self, drift_ppm: float, confidence: float) -> bool:
        """
        Check if GPSDO drift is within acceptable limits.
        
        This is Monitor B - the Drift Watchdog. A GPSDO should have < 0.001 PPM
        drift when locked. If we see > 0.1 PPM, the GPSDO may be unlocked.
        
        Args:
            drift_ppm: Measured tone-to-tone drift in PPM
            confidence: Confidence in the drift measurement
            
        Returns:
            True if drift is healthy, False if GPSDO may be unlocked
        """
        if confidence < 0.5:
            # Low confidence measurement - can't determine health
            return True
        
        if abs(drift_ppm) > self.DRIFT_ALARM_PPM:
            logger.warning(
                f"⚠️ DRIFT ALARM: {drift_ppm:+.3f} PPM exceeds {self.DRIFT_ALARM_PPM} PPM threshold. "
                f"GPSDO may be unlocked or in holdover."
            )
            
            # Enter holdover mode (don't invalidate, but flag quality)
            if self.state.anchor_state == AnchorState.STEADY_STATE:
                self.state.anchor_state = AnchorState.HOLDOVER
                self.state.holdover_since = time.time()
                logger.warning("📉 Entering HOLDOVER mode - data quality flagged")
            
            return False
        
        # Good drift - exit holdover if we were in it
        if self.state.anchor_state == AnchorState.HOLDOVER:
            logger.info("✅ Drift returned to normal - exiting HOLDOVER mode")
            self.state.anchor_state = AnchorState.STEADY_STATE
            self.state.holdover_since = None
        
        return True
    
    # =========================================================================
    # Anchor Management
    # =========================================================================
    
    def establish_anchor(
        self,
        time_snap: TimeSnapReference,
        best_detection: ToneDetectionResult,
        channel_name: str
    ) -> None:
        """
        Establish a new time anchor (used at startup or after reanchor).
        
        Args:
            time_snap: The newly created TimeSnapReference
            best_detection: The detection used to establish the anchor
            channel_name: Channel that provided the anchor
        """
        self.state.anchor_state = AnchorState.STEADY_STATE
        self.state.consecutive_verifications = 1
        self.state.last_verification_time = time.time()
        self.state.last_verification_error_ms = 0.0
        self.state.best_channel = channel_name
        self.state.best_station = best_detection.station.value if hasattr(best_detection.station, 'value') else str(best_detection.station)
        self.state.holdover_since = None
        self.state.total_reanchors += 1
        
        logger.info(
            f"⚓ ANCHOR ESTABLISHED: {self.state.best_station} on {channel_name}, "
            f"RTP={time_snap.rtp_timestamp}, UTC={time_snap.utc_timestamp:.3f}, "
            f"total reanchors={self.state.total_reanchors}"
        )
    
    def invalidate_anchor(self, reason: str) -> None:
        """
        Invalidate the current anchor, forcing a fresh Pass 0 search.
        
        Args:
            reason: Why the anchor was invalidated
        """
        logger.error(f"🔴 ANCHOR INVALIDATED: {reason}")
        
        self.state.anchor_state = AnchorState.REANCHOR_REQUIRED
        self.state.consecutive_verifications = 0
        self.state.last_verification_time = None
        self.state.holdover_since = None
        # Keep best_channel/best_station as hints for next anchor search
    
    # =========================================================================
    # Projection and Verification
    # =========================================================================
    
    def calculate_expected_sample(
        self,
        time_snap: TimeSnapReference,
        target_minute: int
    ) -> int:
        """
        Calculate the expected sample position for a minute boundary.
        
        This is the "steel ruler" projection: we trust the GPSDO and simply
        calculate where the sample SHOULD be based on counting.
        
        Args:
            time_snap: Current TimeSnapReference
            target_minute: Unix timestamp of the target minute boundary
            
        Returns:
            Expected RTP sample position
        """
        # Time elapsed since anchor
        seconds_elapsed = target_minute - time_snap.utc_timestamp
        
        # Samples elapsed (with PPM correction)
        samples_elapsed = seconds_elapsed * self.sample_rate / time_snap.clock_ratio
        
        # Expected sample position
        expected_sample = time_snap.rtp_timestamp + int(samples_elapsed)
        
        return expected_sample
    
    def verify_projection(
        self,
        detection: ToneDetectionResult,
        time_snap: TimeSnapReference,
        minute_boundary: int
    ) -> VerificationResult:
        """
        Verify that a detected tone arrived at the expected sample position.
        
        This is the core of the monitoring architecture: we project where the
        tone SHOULD arrive based on GPSDO counting, then compare to where it
        actually arrived. The difference tells us about:
        
        - Normal ionospheric jitter (< 1ms): Do nothing
        - Path change (< 5ms): Log but trust counter
        - Broken counter (> 50ms): Force reanchor
        
        Args:
            detection: The tone detection to verify
            time_snap: Current TimeSnapReference
            minute_boundary: Unix timestamp of the minute boundary
            
        Returns:
            VerificationResult with analysis
        """
        # Calculate expected sample position
        expected_sample = self.calculate_expected_sample(time_snap, minute_boundary)
        
        # Get actual sample position
        if detection.buffer_rtp_start is not None and detection.sample_position_original is not None:
            actual_sample = detection.buffer_rtp_start + detection.sample_position_original
            timing_offset_samples = int(detection.timing_error_ms * self.sample_rate / 1000)
            actual_sample_at_minute = actual_sample - timing_offset_samples
        else:
            # Fallback - less precise
            actual_sample_at_minute = expected_sample  # Can't verify without precise position
            logger.warning("Cannot verify projection - no precise sample position")
        
        # Calculate error
        sample_error = actual_sample_at_minute - expected_sample
        error_ms = sample_error / self.sample_rate * 1000
        
        # Classify the error
        within_jitter = abs(error_ms) <= self.JITTER_THRESHOLD_MS
        within_physics = abs(error_ms) <= self.PHYSICS_THRESHOLD_MS
        requires_reanchor = abs(error_ms) > self.REANCHOR_THRESHOLD_MS
        
        station_name = detection.station.value if hasattr(detection.station, 'value') else str(detection.station)
        
        result = VerificationResult(
            expected_sample=expected_sample,
            actual_sample=actual_sample_at_minute,
            error_ms=error_ms,
            within_jitter_threshold=within_jitter,
            within_physics_threshold=within_physics,
            requires_reanchor=requires_reanchor,
            station=station_name,
            snr_db=detection.snr_db
        )
        
        # Update state based on verification
        self._process_verification_result(result)
        
        return result
    
    def _process_verification_result(self, result: VerificationResult) -> None:
        """Process a verification result and update state."""
        
        if result.requires_reanchor:
            self.invalidate_anchor(
                f"Projection error {result.error_ms:+.1f}ms exceeds threshold "
                f"({self.REANCHOR_THRESHOLD_MS}ms)"
            )
            return
        
        # Update verification history
        self.state.verification_history.append(result.error_ms)
        if len(self.state.verification_history) > 10:
            self.state.verification_history.pop(0)
        
        if result.within_jitter_threshold:
            # Perfect verification
            self.state.consecutive_verifications += 1
            self.state.last_verification_time = time.time()
            self.state.last_verification_error_ms = result.error_ms
            
            logger.debug(
                f"✅ VERIFICATION OK: {result.station} error={result.error_ms:+.3f}ms "
                f"(consecutive={self.state.consecutive_verifications})"
            )
            
        elif result.within_physics_threshold:
            # Acceptable but notable - path may have changed
            self.state.consecutive_verifications = max(0, self.state.consecutive_verifications - 1)
            self.state.last_verification_time = time.time()
            self.state.last_verification_error_ms = result.error_ms
            
            logger.info(
                f"📊 VERIFICATION MARGINAL: {result.station} error={result.error_ms:+.1f}ms "
                f"(within physics threshold but above jitter)"
            )
            
        else:
            # Large error but not catastrophic - investigate
            logger.warning(
                f"⚠️ VERIFICATION WARNING: {result.station} error={result.error_ms:+.1f}ms "
                f"exceeds physics threshold ({self.PHYSICS_THRESHOLD_MS}ms)"
            )
            self.state.consecutive_verifications = 0
    
    # =========================================================================
    # Optimized Pass 0 Logic
    # =========================================================================
    
    def get_preferred_channel(self) -> Optional[str]:
        """
        Get the preferred channel for quick verification.
        
        Returns the best channel from previous minute, if available.
        This enables optimized Pass 0 where we check only the best channel first.
        
        Returns:
            Channel name to check first, or None if no preference
        """
        return self.state.best_channel
    
    def get_preferred_station(self) -> Optional[str]:
        """
        Get the preferred station for quick verification.
        
        Returns:
            Station name to prioritize, or None if no preference
        """
        return self.state.best_station
    
    def update_best_channel(self, channel: str, station: str) -> None:
        """
        Update the best channel/station for future optimized searches.
        
        Args:
            channel: Channel name (e.g., "WWV_10_MHz")
            station: Station name (e.g., "WWV")
        """
        self.state.best_channel = channel
        self.state.best_station = station
    
    # =========================================================================
    # Status and Diagnostics
    # =========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        """Get current monitor status for diagnostics."""
        return {
            'anchor_state': self.state.anchor_state.name,
            'consecutive_verifications': self.state.consecutive_verifications,
            'last_verification_time': self.state.last_verification_time,
            'last_verification_error_ms': self.state.last_verification_error_ms,
            'best_channel': self.state.best_channel,
            'best_station': self.state.best_station,
            'holdover_since': self.state.holdover_since,
            'total_reanchors': self.state.total_reanchors,
            'verification_trend_ms': (
                sum(self.state.verification_history) / len(self.state.verification_history)
                if self.state.verification_history else 0.0
            ),
        }
    
    def get_quality_flag(self) -> str:
        """
        Get data quality flag based on current state.
        
        Returns:
            'LOCKED': GPSDO locked, anchor verified
            'HOLDOVER': GPSDO may be unlocked, data usable with caution
            'UNANCHORED': No time anchor, data timing unreliable
        """
        if self.state.anchor_state == AnchorState.STEADY_STATE:
            return 'LOCKED'
        elif self.state.anchor_state == AnchorState.HOLDOVER:
            return 'HOLDOVER'
        else:
            return 'UNANCHORED'
