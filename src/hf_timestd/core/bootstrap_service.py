"""
Bootstrap Service: Coordinates Bootstrap Acquisition Across All Channels

This service manages the bootstrap process for the entire hf-timestd system:
1. Receives samples from all channel recorders
2. Accumulates them in rolling buffers (no archiving during bootstrap)
3. Searches for minute marker tones
4. Validates detections across multiple broadcasts
5. Establishes RTP-to-UTC correspondence
6. Signals lock to enable archiving and operational mode

Architecture:
------------
                     ┌─────────────────────────────────────────┐
                     │         Bootstrap Service               │
                     │                                         │
  Channel 1 ────────►│  ┌─────────────────────────────────┐   │
  (CHU 3.33)         │  │  Rolling Buffer (2.5 min)       │   │
                     │  └─────────────────────────────────┘   │
                     │                                         │
  Channel 2 ────────►│  ┌─────────────────────────────────┐   │
  (WWV 10)           │  │  Rolling Buffer (2.5 min)       │   │
                     │  └─────────────────────────────────┘   │
                     │                                         │
  Channel N ────────►│  ┌─────────────────────────────────┐   │
  (...)              │  │  Rolling Buffer (2.5 min)       │   │
                     │  └─────────────────────────────────┘   │
                     │                                         │
                     │  ┌─────────────────────────────────┐   │
                     │  │  TimingBootstrap State Machine   │   │
                     │  │  ACQUIRING → CORRELATING →       │   │
                     │  │  TRACKING → LOCKED               │   │
                     │  └─────────────────────────────────┘   │
                     │                                         │
                     │  ──────────► LOCK SIGNAL ──────────►   │
                     │              (to recorders)             │
                     └─────────────────────────────────────────┘

Bootstrap Philosophy:
--------------------
During bootstrap, we DON'T KNOW what time it is. The system clock is just
a starting point. The tones ARE the ground truth - they tell us UTC.

We search for tones without assuming minute boundaries, validate the pattern
across multiple broadcasts, and only then establish the RTP-to-UTC mapping.

Once locked:
- Feed D_clock to Chrony to discipline system clock
- Enable minute-aligned archiving (now we know where minutes are)
- Switch to narrow-window operational detection

Usage:
------
    # Create service
    bootstrap_service = BootstrapService(
        receiver_lat=38.9,
        receiver_lon=-92.1,
        sample_rate=24000
    )
    
    # In recorder's _handle_samples:
    if not bootstrap_service.is_locked:
        bootstrap_service.add_samples(channel_name, samples, rtp_timestamp)
        # Don't archive yet
    else:
        # Normal archiving
        archive_writer.write_samples(...)
    
    # Periodically check for lock
    if bootstrap_service.check_for_lock():
        offset = bootstrap_service.get_rtp_to_utc_offset()
        # Start archiving, feed Chrony, etc.
"""

import logging
import time
import threading
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from enum import Enum
from pathlib import Path

from .bootstrap_rolling_buffer import (
    BootstrapRollingBuffer,
    BootstrapBufferManager,
    ToneCandidate,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_BUFFER_DURATION_SEC
)
from .timing_bootstrap import TimingBootstrap, BootstrapState, LockTier

logger = logging.getLogger(__name__)


class BootstrapPhase(Enum):
    """High-level bootstrap phases."""
    INITIALIZING = "initializing"    # Starting up, waiting for data
    SEARCHING = "searching"          # Searching for tones in rolling buffers
    CONFIRMING = "confirming"        # Found candidates, validating pattern
    PROVISIONAL_LOCK = "provisional" # Provisional lock, feeding Chrony
    LOCKED = "locked"               # Full lock, operational mode


@dataclass
class BootstrapConfig:
    """
    Configuration for bootstrap service.
    
    Two-Tier Bootstrap Philosophy (2026-01-27):
    -------------------------------------------
    The ionosphere introduces path delay variations at multiple timescales:
    - Seconds: Scintillation/multipath (±5-20ms)
    - Minutes: Traveling Ionospheric Disturbances (±10-30ms)  
    - Hours: Diurnal TEC variation (±50-100ms equivalent)
    
    To achieve a stable RTP-to-UTC offset, we need to average over the TID
    timescale (~10-15 minutes). Locking too quickly captures ionospheric
    variability as systematic offset error.
    
    Tier 1 (Provisional): Quick lock to establish minute boundaries
    - Allows archiving to begin with correct alignment
    - Uses wide detection window (±500ms)
    - Achieved in 2-3 minutes
    
    Tier 2 (Refined): Stable lock after ionospheric averaging
    - Refines RTP-to-UTC offset using median of many measurements
    - Narrows detection window for operational mode
    - Requires 10-15 minutes of consistent tracking
    """
    receiver_lat: float
    receiver_lon: float
    sample_rate: int = DEFAULT_SAMPLE_RATE
    buffer_duration_sec: float = DEFAULT_BUFFER_DURATION_SEC
    
    # Search parameters
    search_interval_sec: float = 10.0  # How often to search buffers
    min_data_duration_sec: float = 65.0  # Minimum data before searching
    
    # Tier 1: Provisional lock criteria (quick, for minute alignment)
    min_stations_for_provisional: int = 2  # Stations needed for provisional lock
    min_frequencies_for_provisional: int = 2  # Frequencies needed
    min_minutes_for_provisional: int = 2  # Minutes of tracking before provisional
    
    # Tier 2: Refined lock criteria (stable, after ionospheric averaging)
    # The Allan deviation of ionospheric delay reaches minimum at τ ≈ 10-20 min
    refined_lock_duration_sec: float = 600.0  # 10 minutes for TID averaging
    min_measurements_for_refined: int = 50  # Minimum tone detections for median
    max_offset_std_for_refined_ms: float = 15.0  # Offset std must be below this
    
    # Timeouts
    bootstrap_timeout_sec: float = 900.0  # 15 minutes (allow time for refined lock)
    
    # Callbacks
    on_provisional_lock: Optional[Callable[[float], None]] = None  # Called with D_clock
    on_full_lock: Optional[Callable[[float, float], None]] = None  # Called with D_clock, uncertainty


class BootstrapService:
    """
    Service that coordinates bootstrap acquisition across all channels.
    
    This is the main entry point for the bootstrap system. It:
    1. Receives samples from channel recorders
    2. Manages rolling buffers per channel
    3. Coordinates tone detection and validation
    4. Establishes RTP-to-UTC correspondence
    5. Signals when operational mode can begin
    """
    
    def __init__(self, config: BootstrapConfig):
        """
        Initialize the bootstrap service.
        
        Args:
            config: BootstrapConfig with receiver location and parameters
        """
        self.config = config
        
        # Bootstrap state machine
        self.timing_bootstrap = TimingBootstrap(
            receiver_lat=config.receiver_lat,
            receiver_lon=config.receiver_lon,
            sample_rate=config.sample_rate
        )
        
        # Buffer manager
        self.buffer_manager = BootstrapBufferManager(
            sample_rate=config.sample_rate,
            buffer_duration_sec=config.buffer_duration_sec
        )
        self.buffer_manager.bootstrap = self.timing_bootstrap
        
        # Tone detectors per channel (lazy initialization to avoid circular imports)
        # Each channel needs its own detector with appropriate templates
        self._tone_detectors: Dict[str, Any] = {}
        
        # State
        self.phase = BootstrapPhase.INITIALIZING
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._last_search_time = 0.0
        
        # Results
        self._rtp_to_utc_offset_samples: Optional[int] = None
        self._offset_uncertainty_samples: int = 0
        self._d_clock_ms: Optional[float] = None
        
        # Statistics
        self.stats = {
            'samples_received': 0,
            'searches_performed': 0,
            'candidates_found': 0,
            'validations_passed': 0,
            'validations_failed': 0,
        }
        
        logger.info(f"[BOOTSTRAP_SERVICE] Initialized for receiver at "
                   f"({config.receiver_lat:.2f}, {config.receiver_lon:.2f})")
    
    @property
    def is_locked(self) -> bool:
        """Check if bootstrap has achieved lock (provisional or full)."""
        # Also check for refined lock transition during provisional phase
        if self.phase == BootstrapPhase.PROVISIONAL_LOCK:
            try:
                self._check_refined_lock()
            except Exception as e:
                logger.error(f"[BOOTSTRAP] Error in _check_refined_lock: {e}")
        return self.phase in (BootstrapPhase.PROVISIONAL_LOCK, BootstrapPhase.LOCKED)
    
    @property
    def is_fully_locked(self) -> bool:
        """Check if bootstrap has achieved full lock."""
        return self.phase == BootstrapPhase.LOCKED
    
    def _get_tone_detector(self, channel_name: str):
        """Get or create tone detector for a specific channel.
        
        Each channel needs its own detector with appropriate templates:
        - CHU channels get CHU templates (500ms @ 1000Hz)
        - WWV channels get WWV templates (800ms @ 1000Hz)
        - Shared channels get WWV + WWVH + CHU templates
        """
        if channel_name not in self._tone_detectors:
            from .tone_detector import MultiStationToneDetector
            self._tone_detectors[channel_name] = MultiStationToneDetector(
                channel_name=channel_name,  # Use actual channel name for correct templates
                sample_rate=self.config.sample_rate
            )
            logger.info(f"[BOOTSTRAP_SERVICE] Created ToneDetector for {channel_name}")
        return self._tone_detectors[channel_name]
    
    def add_samples(
        self,
        channel_name: str,
        samples: np.ndarray,
        rtp_timestamp: int
    ) -> bool:
        """
        Add samples from a channel recorder.
        
        During bootstrap, samples are accumulated in rolling buffers.
        Once locked, this method returns True to signal that normal
        archiving should proceed.
        
        Args:
            channel_name: Channel identifier (e.g., "CHU_3330")
            samples: IQ samples (complex64)
            rtp_timestamp: RTP timestamp of first sample
            
        Returns:
            True if locked (caller should archive), False if still bootstrapping
        """
        with self._lock:
            if self.phase == BootstrapPhase.LOCKED:
                return True  # Already locked, proceed with archiving
            
            self.stats['samples_received'] += len(samples)
        
        # Add to rolling buffer
        self.buffer_manager.add_samples(channel_name, samples, rtp_timestamp)
        
        # Update phase if we have enough data
        if self.phase == BootstrapPhase.INITIALIZING:
            if self._has_enough_data():
                with self._lock:
                    self.phase = BootstrapPhase.SEARCHING
                    logger.info("[BOOTSTRAP_SERVICE] Enough data accumulated → SEARCHING")
        
        return False
    
    def _has_enough_data(self) -> bool:
        """Check if we have enough data to start searching."""
        for buffer in self.buffer_manager.buffers.values():
            if buffer.has_enough_data(self.config.min_data_duration_sec):
                return True
        return False
    
    def search_and_update(self) -> Optional[str]:
        """
        Search all buffers for tones and update bootstrap state.
        
        This should be called periodically (e.g., every 10 seconds).
        
        Returns:
            Status message or None
        """
        with self._lock:
            if self.phase == BootstrapPhase.LOCKED:
                return "LOCKED"
            
            if self.phase == BootstrapPhase.INITIALIZING:
                return "INITIALIZING"
            
            # Rate limit searches
            now = time.time()
            if now - self._last_search_time < self.config.search_interval_sec:
                return None
            
            self._last_search_time = now
            self.stats['searches_performed'] += 1
        
        # Check timeout
        elapsed = time.time() - self._start_time
        if elapsed > self.config.bootstrap_timeout_sec:
            logger.error(f"[BOOTSTRAP_SERVICE] Bootstrap timeout after {elapsed:.0f}s")
            return "TIMEOUT"
        
        # Search each channel with its own tone detector
        status = None
        
        for channel_name in list(self.buffer_manager.buffers.keys()):
            # Get channel-specific tone detector (CHU channels get CHU templates, etc.)
            tone_detector = self._get_tone_detector(channel_name)
            result = self.buffer_manager.search_and_process(
                channel_name=channel_name,
                tone_detector=tone_detector,
                current_time=time.time()
            )
            
            if result:
                status = result
        
        # CRITICAL FIX (2026-01-27): Always check phase, not just when result is truthy
        # The TimingBootstrap state can change without search_and_process returning a result
        self._update_phase_from_bootstrap()
        
        return status
    
    def _update_phase_from_bootstrap(self):
        """Update our phase based on TimingBootstrap state."""
        bootstrap_state = self.timing_bootstrap.state
        
        with self._lock:
            if bootstrap_state == BootstrapState.LOCKED:
                if self.phase != BootstrapPhase.LOCKED:
                    self._on_lock_achieved()
                    self.phase = BootstrapPhase.LOCKED
                    
            elif bootstrap_state == BootstrapState.TRACKING:
                if self.phase not in (BootstrapPhase.PROVISIONAL_LOCK, BootstrapPhase.LOCKED):
                    self._on_provisional_lock()
                    self.phase = BootstrapPhase.PROVISIONAL_LOCK
                    
                    # Collect offset measurements from validated clusters for refined lock
                    self._collect_offset_measurements()
                    
                    # CRITICAL FIX (2026-01-27): Free bootstrap resources on provisional lock
                    # Once we're in TRACKING/PROVISIONAL_LOCK, archiving begins and we no longer
                    # need the rolling buffers. Waiting for LOCKED state wastes ~250MB of memory.
                    self._free_bootstrap_buffers()
                
                # During provisional lock, check for refined lock criteria
                elif self.phase == BootstrapPhase.PROVISIONAL_LOCK:
                    self._check_refined_lock()
                    
            elif bootstrap_state == BootstrapState.CORRELATING:
                if self.phase == BootstrapPhase.SEARCHING:
                    self.phase = BootstrapPhase.CONFIRMING
                    logger.info("[BOOTSTRAP_SERVICE] Found candidates → CONFIRMING")
    
    def _on_provisional_lock(self):
        """Handle provisional lock event."""
        import time
        
        offset = self.timing_bootstrap.get_rtp_to_utc_offset()
        if offset:
            self._rtp_to_utc_offset_samples, self._offset_uncertainty_samples = offset
            
            # Calculate D_clock (system clock offset from UTC)
            # This is approximate until we have more data
            self._d_clock_ms = self._calculate_d_clock()
            
            # Set two-tier bootstrap state (Tier 1: Provisional Lock)
            self.timing_bootstrap.lock_tier = LockTier.PROVISIONAL
            self.timing_bootstrap.provisional_lock_time = time.time()
            
            # CRITICAL FIX (2026-01-27): Enable propagation bounds enforcement
            # After bootstrap locks, tone detectors should reject detections outside
            # physical propagation bounds. This prevents bad detections (400-500ms)
            # from polluting the D_clock calculation.
            self._set_tone_detectors_locked(True)
            
            logger.info(f"[BOOTSTRAP_SERVICE] PROVISIONAL LOCK achieved! "
                       f"D_clock ≈ {self._d_clock_ms:+.1f}ms")
            logger.info(f"[BOOTSTRAP] PROVISIONAL LOCK: D_clock ≈ {self._d_clock_ms:+.1f}ms")
            
            if self.config.on_provisional_lock:
                try:
                    self.config.on_provisional_lock(self._d_clock_ms)
                except Exception as e:
                    logger.error(f"Error in on_provisional_lock callback: {e}")
    
    def _on_lock_achieved(self):
        """Handle full lock event."""
        offset = self.timing_bootstrap.get_rtp_to_utc_offset()
        if offset:
            self._rtp_to_utc_offset_samples, self._offset_uncertainty_samples = offset
            self._d_clock_ms = self._calculate_d_clock()
            
            uncertainty_ms = self._offset_uncertainty_samples * 1000 / self.config.sample_rate
            
            logger.info(f"[BOOTSTRAP_SERVICE] FULL LOCK achieved! "
                       f"D_clock = {self._d_clock_ms:+.1f}ms ± {uncertainty_ms:.1f}ms")
            
            # CRITICAL FIX (2026-01-27): Free bootstrap buffers after lock to prevent memory leak
            # The rolling buffers are no longer needed once we've locked - they hold ~250MB
            self._free_bootstrap_buffers()
            
            if self.config.on_full_lock:
                try:
                    self.config.on_full_lock(self._d_clock_ms, uncertainty_ms)
                except Exception as e:
                    logger.error(f"Error in on_full_lock callback: {e}")
    
    def _calculate_d_clock(self) -> Optional[float]:
        """
        Calculate D_clock (system clock offset from UTC).
        
        D_clock = system_time - UTC
        Positive means system clock is ahead of UTC.
        
        Calculation:
        1. Get the most recent RTP timestamp from any active buffer
        2. Convert RTP to UTC using the bootstrap offset
        3. Compare to current system time
        
        D_clock = system_time - UTC_from_RTP
               = time.time() - (last_rtp - rtp_to_utc_offset) / sample_rate
        """
        if self._rtp_to_utc_offset_samples is None:
            return None
        
        # Find the most recent RTP timestamp from any buffer
        last_rtp = None
        for buffer in self.buffer_manager.buffers.values():
            if buffer.last_rtp is not None:
                if last_rtp is None or buffer.last_rtp > last_rtp:
                    last_rtp = buffer.last_rtp
        
        if last_rtp is None:
            logger.debug("[BOOTSTRAP] Cannot calculate D_clock: no RTP timestamps available")
            return None
        
        # Convert RTP to UTC seconds
        # rtp_to_utc_offset_samples is the RTP sample number at UTC=0
        # So: UTC_seconds = (current_rtp - offset) / sample_rate
        rtp_since_epoch = last_rtp - self._rtp_to_utc_offset_samples
        utc_from_rtp = rtp_since_epoch / self.config.sample_rate
        
        # D_clock = system_time - UTC
        system_time = time.time()
        d_clock_sec = system_time - utc_from_rtp
        d_clock_ms = d_clock_sec * 1000.0
        
        # Sanity check: D_clock should be small if system is NTP-synced
        # Large values (>1 second) suggest calculation error or unsynced clock
        if abs(d_clock_ms) > 1000:
            logger.warning(f"[BOOTSTRAP] D_clock={d_clock_ms:+.1f}ms is large - "
                          f"system clock may be unsynced or calculation error")
        
        return d_clock_ms
    
    def _collect_offset_measurements(self):
        """
        Collect offset measurements from validated tones during provisional lock.
        
        This is called once when entering provisional lock to gather all existing
        offset measurements for computing the refined lock.
        """
        import time
        from .timing_bootstrap import OffsetMeasurement, SAMPLES_PER_MINUTE
        
        tb = self.timing_bootstrap
        
        # Only collect once (when _offset_measurements is empty)
        if tb._offset_measurements:
            return
        
        # Use validated_tones (populated by _handle_tracking) if available
        if tb.validated_tones:
            for vt in tb.validated_tones:
                c = vt.candidate
                
                # Get propagation delay for this station
                delay_samples = tb.station_expectations.get(
                    c.station, {}
                ).get('delay_samples', 0)
                
                # Compute offset: RTP at UTC minute 0
                minute_rtp = c.rtp_timestamp - delay_samples
                minute_0_rtp = minute_rtp - (vt.minute_index * SAMPLES_PER_MINUTE)
                
                # Extract frequency from channel name
                try:
                    freq_khz = int(c.channel.split('_')[1])
                except (IndexError, ValueError):
                    freq_khz = 0
                
                measurement = OffsetMeasurement(
                    timestamp=time.time(),
                    offset_samples=minute_0_rtp,
                    station=c.station,
                    snr_db=c.snr_db,
                    frequency_khz=freq_khz
                )
                tb._offset_measurements.append(measurement)
            
            if tb._offset_measurements:
                logger.info(f"[BOOTSTRAP] Collected {len(tb._offset_measurements)} offset measurements "
                           f"from {len(tb.validated_tones)} validated tones")
            return
        
        # Fallback to validated_clusters if no validated_tones
        if not tb.validated_clusters or tb.reference_rtp is None:
            return
        
        for cluster in tb.validated_clusters:
            anchor_rtp = cluster['anchor_rtp']
            anchor_station = cluster['anchor_station']
            anchor_snr = cluster.get('anchor_snr', 20.0)
            
            # Compute minute index from reference
            samples_from_ref = anchor_rtp - tb.reference_rtp
            minute_index = round(samples_from_ref / SAMPLES_PER_MINUTE)
            
            # Get propagation delay for this station
            delay_samples = tb.station_expectations.get(
                anchor_station, {}
            ).get('delay_samples', 0)
            
            # Compute offset: RTP at UTC minute 0
            minute_rtp = anchor_rtp - delay_samples
            minute_0_rtp = minute_rtp - (minute_index * SAMPLES_PER_MINUTE)
            
            # Extract frequency from cluster if available
            freq_khz = cluster.get('frequency_khz', 0)
            
            measurement = OffsetMeasurement(
                timestamp=time.time(),
                offset_samples=minute_0_rtp,
                station=anchor_station,
                snr_db=anchor_snr,
                frequency_khz=freq_khz
            )
            tb._offset_measurements.append(measurement)
        
        if tb._offset_measurements:
            logger.info(f"[BOOTSTRAP] Collected {len(tb._offset_measurements)} offset measurements "
                       f"from {len(tb.validated_clusters)} validated clusters")
    
    def _check_refined_lock(self):
        """
        Check if criteria for refined (Tier 2) lock are met.
        
        Criteria:
        1. At least refined_lock_duration_sec (10 min) since provisional lock
        2. At least min_measurements_for_refined (50) measurements
        3. Offset standard deviation < max_offset_std_for_refined_ms (15ms)
        """
        import time
        from math import sqrt
        from statistics import median
        
        tb = self.timing_bootstrap
        
        if tb.provisional_lock_time is None:
            return
        
        # Already at refined lock
        if tb.lock_tier == LockTier.REFINED:
            return
        
        elapsed = time.time() - tb.provisional_lock_time
        n_measurements = len(tb._offset_measurements)
        
        # Check minimum duration
        if elapsed < tb.refined_lock_duration_sec:
            return
        
        # Check minimum measurements
        if n_measurements < tb.min_measurements_for_refined:
            if n_measurements > 0 and n_measurements % 10 == 0:
                logger.info(f"[BOOTSTRAP] Refined lock: {n_measurements}/{tb.min_measurements_for_refined} "
                           f"measurements after {elapsed:.0f}s")
            return
        
        # Compute median and standard deviation
        offsets = [m.offset_samples for m in tb._offset_measurements]
        median_offset = int(median(offsets))
        
        # Standard deviation in ms
        mean_offset = sum(offsets) / len(offsets)
        variance = sum((o - mean_offset) ** 2 for o in offsets) / len(offsets)
        std_samples = sqrt(variance)
        std_ms = std_samples * 1000 / self.config.sample_rate
        
        # Check stability criterion
        if std_ms > tb.max_offset_std_for_refined_ms:
            logger.info(f"[BOOTSTRAP] Refined lock: std={std_ms:.1f}ms > {tb.max_offset_std_for_refined_ms}ms, "
                       f"continuing to collect measurements")
            return
        
        # All criteria met - transition to refined lock!
        tb._refined_offset_samples = median_offset
        tb._refined_offset_std_ms = std_ms
        
        # Update the main offset with refined value
        old_offset = tb.rtp_to_utc_offset_samples
        tb.rtp_to_utc_offset_samples = median_offset
        tb.offset_uncertainty_samples = int(std_samples)
        
        # Transition to LOCKED state with Tier 2
        tb.lock_tier = LockTier.REFINED
        tb.state = BootstrapState.LOCKED
        self.phase = BootstrapPhase.LOCKED
        
        offset_change_ms = (median_offset - old_offset) * 1000 / self.config.sample_rate if old_offset else 0
        
        logger.info(f"[BOOTSTRAP] TIER 2 REFINED LOCK achieved!")
        logger.info(f"  Duration: {elapsed:.0f}s, Measurements: {n_measurements}")
        logger.info(f"  Offset: {median_offset} samples (median), std={std_ms:.1f}ms")
        logger.info(f"  Offset change from provisional: {offset_change_ms:+.1f}ms")
        
        # Log station distribution
        station_counts = {}
        for m in tb._offset_measurements:
            station_counts[m.station] = station_counts.get(m.station, 0) + 1
        logger.info(f"  Station distribution: {station_counts}")
        
        # Trigger full lock callback
        self._on_lock_achieved()
    
    def get_rtp_to_utc_offset(self) -> Optional[Tuple[int, int]]:
        """
        Get the RTP-to-UTC offset if available.
        
        Returns:
            Tuple of (offset_samples, uncertainty_samples) or None
        """
        if self._rtp_to_utc_offset_samples is not None:
            return (self._rtp_to_utc_offset_samples, self._offset_uncertainty_samples)
        return self.timing_bootstrap.get_rtp_to_utc_offset()
    
    def get_d_clock_ms(self) -> Optional[float]:
        """Get current D_clock estimate in milliseconds."""
        return self._d_clock_ms
    
    def _free_bootstrap_buffers(self):
        """
        Free bootstrap rolling buffers after lock to reclaim memory.
        
        The rolling buffers hold ~250MB (9 channels × 27MB each) and are no longer
        needed once we've achieved lock. This prevents memory growth over time.
        """
        import gc
        
        # Count buffers before clearing
        n_buffers = len(self.buffer_manager.buffers)
        total_samples = sum(
            buf.buffer_size for buf in self.buffer_manager.buffers.values()
        )
        estimated_mb = (total_samples * 8) / (1024 * 1024)  # complex64 = 8 bytes
        
        # Clear all buffers
        self.buffer_manager.clear_all()
        
        # Also clear tone detectors (they hold FFT templates)
        self._tone_detectors.clear()
        
        # Force garbage collection to actually free the memory
        gc.collect()
        
        logger.info(
            f"[BOOTSTRAP_SERVICE] Freed {n_buffers} bootstrap buffers "
            f"(~{estimated_mb:.0f}MB) after lock"
        )
    
    def get_minute_boundary_rtp(self, minute_index: int = 0) -> Optional[int]:
        """
        Get the RTP timestamp of a minute boundary.
        
        Args:
            minute_index: Which minute (0 = reference minute)
            
        Returns:
            RTP timestamp at the minute boundary, or None if not locked
        """
        if self._rtp_to_utc_offset_samples is None:
            return None
        
        samples_per_minute = self.config.sample_rate * 60
        return self._rtp_to_utc_offset_samples + (minute_index * samples_per_minute)
    
    def get_status(self) -> dict:
        """Get current bootstrap status including two-tier lock information."""
        with self._lock:
            elapsed = time.time() - self._start_time
            bootstrap_status = self.timing_bootstrap.get_status()
            
            return {
                'phase': self.phase.value,
                'is_locked': self.is_locked,
                'is_fully_locked': self.is_fully_locked,
                'lock_tier': bootstrap_status.get('lock_tier', 0),  # 0=none, 1=provisional, 2=refined
                'elapsed_sec': elapsed,
                'd_clock_ms': self._d_clock_ms,
                'rtp_offset_samples': self._rtp_to_utc_offset_samples,
                'offset_uncertainty_samples': self._offset_uncertainty_samples,
                'stats': self.stats.copy(),
                'bootstrap_state': bootstrap_status,
                'buffers': {
                    name: buf.get_status()
                    for name, buf in self.buffer_manager.buffers.items()
                }
            }
    
    def reset(self):
        """Reset bootstrap state to start over."""
        with self._lock:
            self.phase = BootstrapPhase.INITIALIZING
            self._start_time = time.time()
            self._last_search_time = 0.0
            self._rtp_to_utc_offset_samples = None
            self._offset_uncertainty_samples = 0
            self._d_clock_ms = None
            
            # Reset buffer manager
            self.buffer_manager.clear_all()
            self.buffer_manager.is_locked = False
            
            # Reset timing bootstrap
            self.timing_bootstrap = TimingBootstrap(
                receiver_lat=self.config.receiver_lat,
                receiver_lon=self.config.receiver_lon,
                sample_rate=self.config.sample_rate
            )
            self.buffer_manager.bootstrap = self.timing_bootstrap
            
            # Reset stats
            self.stats = {
                'samples_received': 0,
                'searches_performed': 0,
                'candidates_found': 0,
                'validations_passed': 0,
                'validations_failed': 0,
            }
            
            logger.info("[BOOTSTRAP_SERVICE] Reset to INITIALIZING")


def create_bootstrap_service(
    receiver_lat: float,
    receiver_lon: float,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    on_provisional_lock: Optional[Callable[[float], None]] = None,
    on_full_lock: Optional[Callable[[float, float], None]] = None
) -> BootstrapService:
    """
    Factory function to create a bootstrap service.
    
    Args:
        receiver_lat: Receiver latitude in degrees
        receiver_lon: Receiver longitude in degrees
        sample_rate: Sample rate in Hz
        on_provisional_lock: Callback when provisional lock achieved
        on_full_lock: Callback when full lock achieved
        
    Returns:
        Configured BootstrapService instance
    """
    config = BootstrapConfig(
        receiver_lat=receiver_lat,
        receiver_lon=receiver_lon,
        sample_rate=sample_rate,
        on_provisional_lock=on_provisional_lock,
        on_full_lock=on_full_lock
    )
    
    return BootstrapService(config)
