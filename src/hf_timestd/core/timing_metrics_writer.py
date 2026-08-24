#!/usr/bin/env python3
"""
Timing Metrics Writer for Web-UI Timing Analysis

Collects and writes timing metrics for visualization:
- Time_snap status and quality
- RTP drift measurements
- NTP comparison
- Time source transitions
"""

import csv
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, List, Any, Tuple
import time
import re

from .wwv_constants import WWVH_FREQUENCIES

logger = logging.getLogger(__name__)


@dataclass
class TimingSnapshot:
    """Single timing measurement snapshot"""
    timestamp_utc: str
    channel: str
    source_type: str  # 'wwv_startup', 'chu_startup', 'ntp', 'wall_clock'
    quality: str  # 'TONE_LOCKED', 'NTP_SYNCED', 'INTERPOLATED', 'WALL_CLOCK'
    snr_db: Optional[float]
    confidence: float
    age_seconds: float
    rtp_anchor: int
    utc_anchor: float
    drift_ms: float
    jitter_ms: float
    ntp_offset_ms: Optional[float]
    health_score: int


@dataclass
class TimingTransition:
    """Time source transition event"""
    timestamp: str
    channel: str
    from_source: str
    to_source: str
    from_quality: str
    to_quality: str
    reason: str
    last_snr_db: Optional[float]
    last_confidence: float
    duration_on_previous_source_minutes: float


class TimingMetricsWriter:
    """
    Write timing metrics for web-UI analysis
    
    Creates two outputs:
    1. timing_metrics_YYYYMMDD.csv - Minute-by-minute metrics
    2. timing_transitions_YYYYMMDD.json - Transition events
    3. propagation_status.json - Real-time propagation mode data (if grid provided)
    """
    
    def __init__(self, output_dir: Path, channel_name: str, receiver_grid: Optional[str] = None):
        """
        Initialize timing metrics writer
        
        Args:
            output_dir: Directory for timing metrics (e.g., /analytics/{CHANNEL}/timing/)
            channel_name: Channel identifier
            receiver_grid: Maidenhead grid square for propagation mode analysis
        """
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.receiver_grid = receiver_grid
        
        # Ensure directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Track previous state for transition detection
        self.previous_source = None
        self.previous_quality = None
        self.previous_source_start_time = None
        self.previous_snr_db = None
        self.previous_confidence = None
        
        # Track previous archive for minute-to-minute drift calculation
        self.previous_rtp = None
        self.previous_utc = None  # UTC calculated from time_snap + RTP
        self.previous_time_snap_rtp = None  # Track if time_snap changed
        
        # Track tone-to-tone measurements (gold standard for A/D clock stability)
        self.last_tone_snap = None  # Previous tone-based time_snap
        self.tone_to_tone_drift_ppm = None  # A/D clock drift in ppm
        
        # Drift tracking for jitter calculation
        # Use 60 measurements (1 hour) for better statistical averaging
        # This helps smooth out short-term noise from host clock jitter
        self.drift_history = []
        self.max_drift_history = 60
        
        # Propagation mode solver for time standard analysis
        self.prop_solver = None
        self.station = self._extract_station(channel_name)
        self.frequency_mhz = self._extract_frequency(channel_name)
        
        if receiver_grid and self.station:
            try:
                from .propagation_mode_solver import PropagationModeSolver
                self.prop_solver = PropagationModeSolver(receiver_grid=receiver_grid)
                logger.info(f"{channel_name}: PropagationModeSolver enabled for {self.station} @ {self.frequency_mhz} MHz")
            except Exception as e:
                logger.warning(f"{channel_name}: Could not initialize PropagationModeSolver: {e}")
        
        logger.info(f"{channel_name}: TimingMetricsWriter initialized at {output_dir}")
    
    def _extract_station(self, channel_name: str) -> Optional[str]:
        """Extract station (WWV/WWVH/CHU) from channel name"""
        upper = channel_name.upper()
        if 'WWVH' in upper:
            return 'WWVH'
        elif 'WWV' in upper:
            return 'WWV'
        elif 'CHU' in upper:
            return 'CHU'
        return None
    
    def _extract_frequency(self, channel_name: str) -> float:
        """Extract frequency in MHz from channel name"""
        match = re.search(r'(\d+\.?\d*)\s*MHz', channel_name, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return 0.0
    
    def write_snapshot(self, time_snap: 'TimeSnapReference', 
                      current_rtp: int, current_utc: float,
                      ntp_offset_ms: Optional[float] = None,
                      ntp_synced: bool = False) -> Optional[Tuple[float, float]]:
        """
        Write current timing snapshot and return PPM measurement if available.
        
        Args:
            time_snap: Current time_snap reference (tone-locked precision)
            current_rtp: Current RTP timestamp
            current_utc: Current system time (NTP-synced if available)
            ntp_offset_ms: NTP offset for comparison
            ntp_synced: Whether system time is NTP-synchronized
            
        Returns:
            Tuple of (ppm_offset, confidence) if tone-to-tone measurement was made,
            None otherwise. Caller should use this to update TimeSnapReference PPM.
            
        Note:
            Drift measurement hierarchy:
            1. RTP clock (what we're testing) via time_snap anchor
            2. vs NTP-synced time (±10ms precision) - preferred
            3. vs Unsynchronized wall clock (±seconds) - fallback
            
            This shows if RTP clock is drifting from real time.
            Drift reliability depends on reference quality (NTP > wall clock).
        """
        if time_snap is None:
            logger.debug(f"{self.channel_name}: No time_snap available for metrics")
            return None
        
        ppm_measurement = None
        try:
            # Check if we have a new tone detection (gold standard reference)
            # This returns (ppm_offset, confidence) if a new measurement was made
            ppm_measurement = self._check_tone_to_tone_drift(time_snap)
            
            # Calculate drift: RTP clock vs real time (NTP-synced or wall clock)
            # Note: This measures combined (RTP + reference) drift, not pure RTP stability
            # For pure A/D clock stability, use tone_to_tone_drift_ppm
            drift_ms = self._calculate_drift_minute_to_minute(
                time_snap, current_rtp, current_utc, ntp_synced
            )
            
            # Track drift for jitter calculation
            self.drift_history.append(drift_ms)
            if len(self.drift_history) > self.max_drift_history:
                self.drift_history.pop(0)
            
            # Calculate jitter (peak-to-peak variation)
            jitter_ms = self._calculate_jitter()
            
            # Classify quality
            quality = self._classify_quality(time_snap, drift_ms, ntp_offset_ms)
            
            # Calculate health score using UTC timestamp age (when tone was detected)
            age_seconds = time.time() - time_snap.utc_timestamp
            health_score = self._calculate_health_score(
                time_snap, drift_ms, jitter_ms, age_seconds
            )
            
            # Get SNR if available
            snr_db = getattr(time_snap, 'detection_snr_db', None)
            
            # Create snapshot
            snapshot = TimingSnapshot(
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                channel=self.channel_name,
                source_type=time_snap.source,
                quality=quality,
                snr_db=snr_db,
                confidence=time_snap.confidence,
                age_seconds=age_seconds,
                rtp_anchor=time_snap.rtp_timestamp,
                utc_anchor=time_snap.utc_timestamp,
                drift_ms=drift_ms,
                jitter_ms=jitter_ms,
                ntp_offset_ms=ntp_offset_ms,
                health_score=health_score
            )
            
            # Write to CSV
            self._write_csv(snapshot)
            
            # Check for transition
            self._check_transition(snapshot, snr_db, time_snap.confidence)
            
            # Return PPM measurement for caller to update time_snap
            return ppm_measurement
            
        except Exception as e:
            logger.error(f"{self.channel_name}: Error writing timing snapshot: {e}")
            return None
    
    def _calculate_drift_minute_to_minute(self, time_snap: 'TimeSnapReference',
                                          current_rtp: int, current_time_utc: float,
                                          ntp_synced: bool) -> float:
        """
        Calculate ADC clock DRIFT RATE against wall clock reference
        
        This measures the CHANGE in phase offset between RTP-derived time and
        wall clock time since the last measurement. The absolute offset is not
        meaningful (varies by channel due to packet arrival timing), but the
        rate of change indicates ADC clock frequency error.
        
        Measurement:
        1. offset = wall_clock - rtp_predicted (phase difference)
        2. drift_rate = current_offset - previous_offset (change in phase)
        
        Interpretation:
        - drift_rate ≈ 0: ADC clock running at exactly 16000 Hz
        - drift_rate > 0: ADC clock slow (RTP falling behind wall clock)
        - drift_rate < 0: ADC clock fast (RTP running ahead of wall clock)
        
        Limitations:
        - Host clock jitter directly affects measurement noise
        - For sub-ms precision, host needs quality NTP/PTP synchronization
        - Use tone-to-tone measurement for gold-standard ADC characterization
        
        Returns:
            Accumulated drift in milliseconds since last measurement.
            This is written to the 'drift_ms' CSV column.
        """
        # Calculate current offset between wall clock and RTP-predicted time
        rtp_predicted_utc = time_snap.calculate_sample_time(current_rtp)
        current_offset_ms = (current_time_utc - rtp_predicted_utc) * 1000.0
        
        # Initialize baseline on first call
        if not hasattr(self, '_last_offset_ms'):
            self._last_offset_ms = current_offset_ms
            self._last_offset_time = current_time_utc
            logger.info(f"{self.channel_name}: Drift baseline established, offset={current_offset_ms:.1f}ms")
            return 0.0
        
        # Calculate drift rate (change in offset)
        offset_change_ms = current_offset_ms - self._last_offset_ms
        time_elapsed = current_time_utc - self._last_offset_time
        
        # Update baseline for next measurement
        self._last_offset_ms = current_offset_ms
        self._last_offset_time = current_time_utc
        
        # Sanity check: offset change should be small (< 1 second per minute for reasonable clocks)
        # Only suppress if this is a short interval (< 2 minutes) - longer gaps may legitimately
        # accumulate more drift
        if abs(offset_change_ms) > 1000 and time_elapsed < 120.0:
            logger.warning(f"{self.channel_name}: Large offset change ({offset_change_ms:.1f}ms over {time_elapsed:.0f}s) - "
                          f"possible time_snap discontinuity or clock jump")
            return 0.0
        
        # Log drift rate if significant
        if abs(offset_change_ms) > 10 and time_elapsed > 30:
            drift_ppm = (offset_change_ms / 1000.0) / time_elapsed * 1e6
            logger.debug(f"{self.channel_name}: Drift rate {offset_change_ms:.1f}ms over {time_elapsed:.0f}s "
                        f"({drift_ppm:.1f} ppm)")
        
        return offset_change_ms
    
    def _check_tone_to_tone_drift(self, time_snap: 'TimeSnapReference') -> Optional[Tuple[float, float]]:
        """
        Calculate A/D clock drift using tone-to-tone measurement (gold standard)
        
        This is the definitive measurement of RTP clock stability:
        - Uses consecutive tone detections as ground truth
        - Measures: Is the A/D clock running at exactly the nominal sample rate?
        - Result in PPM (parts per million) frequency error
        
        Example:
            Tone A: RTP=1000000, UTC=100.0 (WWV tone)
            Tone B: RTP=5760000, UTC=400.0 (WWV tone)
            
            Expected samples = (400.0 - 100.0) * 20000 = 6,000,000
            Actual samples = 5760000 - 1000000 = 4,760,000
            
            Drift = 4,760,000 / 6,000,000 = 0.7933
            Error = (0.7933 - 1.0) * 1e6 = -206667 ppm (clock running slow)
            
        Returns:
            Tuple of (ppm_offset, confidence) or None if no measurement
        """
        # Only process tone-based time_snaps
        source_lower = time_snap.source.lower() if time_snap.source else ''
        is_tone = any(station in source_lower for station in ['wwv', 'chu'])
        
        if not is_tone:
            return None  # Not a tone detection
        
        # First tone detection - establish baseline
        if self.last_tone_snap is None:
            self.last_tone_snap = time_snap
            logger.info(f"{self.channel_name}: Baseline tone established for A/D clock measurement")
            return None
        
        # Check if this is a NEW tone detection (not just re-using old time_snap)
        if time_snap.rtp_timestamp == self.last_tone_snap.rtp_timestamp:
            return None  # Same tone, no new measurement
        
        # Calculate tone-to-tone drift
        # Time between tones (ground truth from WWV/CHU)
        tone_time_elapsed = time_snap.utc_timestamp - self.last_tone_snap.utc_timestamp
        
        if tone_time_elapsed <= 0:
            return None  # Invalid measurement
        
        # RTP samples between tones (A/D clock measurement)
        rtp_samples_elapsed = time_snap.rtp_timestamp - self.last_tone_snap.rtp_timestamp
        if rtp_samples_elapsed < 0:  # Handle wraparound
            rtp_samples_elapsed += 0x100000000
        
        # Expected samples based on tone times
        expected_samples = tone_time_elapsed * time_snap.sample_rate
        
        # A/D clock frequency ratio
        clock_ratio = rtp_samples_elapsed / expected_samples
        
        # Drift in PPM (parts per million)
        # 1.000000 = perfect
        # 1.000010 = +10 ppm (clock running fast)
        # 0.999990 = -10 ppm (clock running slow)
        drift_ppm = (clock_ratio - 1.0) * 1e6
        
        self.tone_to_tone_drift_ppm = drift_ppm
        self.last_tone_snap = time_snap
        
        # Confidence based on measurement duration (longer = more confident)
        # 60s gives 0.5, 300s gives 0.83, 600s gives 0.91
        confidence = 1.0 - (60.0 / (60.0 + tone_time_elapsed))
        
        logger.info(f"{self.channel_name}: Tone-to-tone A/D clock drift: {drift_ppm:+.2f} ppm "
                   f"(over {tone_time_elapsed:.1f}s, conf={confidence:.2f})")
        
        return (drift_ppm, confidence)
    
    def _calculate_jitter(self) -> float:
        """
        Calculate jitter (short-term timing instability)
        
        Uses RMS (Root Mean Square) of drift differences for robustness:
        - More statistically sound than peak-to-peak
        - Less sensitive to outliers
        - Represents typical variation, not worst-case
        
        Returns: RMS jitter in milliseconds
        """
        if len(self.drift_history) < 2:
            return 0.0
        
        # Calculate successive differences (Δdrift)
        drift_diffs = []
        for i in range(1, len(self.drift_history)):
            diff = self.drift_history[i] - self.drift_history[i-1]
            drift_diffs.append(diff * diff)  # Square for RMS
        
        # RMS of differences
        if drift_diffs:
            mean_square = sum(drift_diffs) / len(drift_diffs)
            rms_jitter = mean_square ** 0.5
            return rms_jitter
        
        return 0.0
    
    def _classify_quality(self, time_snap: 'TimeSnapReference', drift_ms: float,
                          ntp_offset_ms: Optional[float] = None) -> str:
        """
        Classify timing quality based on source and age
        
        Quality hierarchy:
        1. TONE_LOCKED - Fresh tone detection (< 5 min, low drift)
        2. INTERPOLATED - Aging tone reference (5 min - 1 hour)
        3. NTP_SYNCED - NTP anchor OR aged tone with NTP fallback available
        4. WALL_CLOCK - No recent tone, no NTP (poorest quality)
        
        Note: This classifies the ANCHOR quality, not the drift measurement.
        - TONE_LOCKED anchor + large drift = indicates A/D clock problem
        - NTP_SYNCED anchor + large drift = indicates system clock problem
        
        Returns: 'TONE_LOCKED', 'INTERPOLATED', 'NTP_SYNCED', 'WALL_CLOCK'
        """
        # Use established_at (when time_snap was stored/confirmed) for freshness
        # This matches the Node.js timing status logic
        # Fall back to utc_timestamp if established_at not available
        reference_time = getattr(time_snap, 'established_at', None) or time_snap.utc_timestamp
        age_seconds = time.time() - reference_time
        
        # Check if source is tone-based (wwv, chu, wwvh in any form)
        source_lower = time_snap.source.lower() if time_snap.source else ''
        is_tone_source = any(station in source_lower for station in ['wwv', 'chu'])
        
        # Tone-based classifications (best quality)
        if is_tone_source:
            # Fresh tone with low drift = actively tone-locked
            if age_seconds < 300 and abs(drift_ms) < 5.0:
                return 'TONE_LOCKED'
            # Aging tone but still valid
            elif age_seconds < 3600:
                return 'INTERPOLATED'
            # Very old tone - fall through to check NTP fallback
        
        # NTP-based anchor (explicit NTP source)
        if time_snap.source == 'ntp':
            return 'NTP_SYNCED'
        
        # Aged tone (>1 hour) - check if NTP available as fallback
        # This allows graceful degradation: TONE → NTP → WALL_CLOCK
        if is_tone_source and age_seconds >= 3600:
            if ntp_offset_ms is not None:
                return 'NTP_SYNCED'  # Fallback to NTP for aged tone
        
        # No good anchor available - wall clock only
        return 'WALL_CLOCK'
    
    def _calculate_health_score(self, time_snap: 'TimeSnapReference',
                                drift_ms: float, jitter_ms: float,
                                age_seconds: float) -> int:
        """
        Calculate health score (0-100)
        
        Higher is better. Considers:
        - Time_snap source quality
        - Drift magnitude
        - Jitter (stability)
        - Age of reference
        - SNR (if tone-locked)
        """
        health = 100.0
        
        # Age penalty (older references less reliable)
        age_minutes = age_seconds / 60.0
        health -= min(age_minutes * 0.1, 30.0)  # Max 30 point penalty
        
        # Drift penalty
        health -= min(abs(drift_ms) * 2.0, 30.0)  # Max 30 points
        
        # Jitter penalty (instability)
        health -= min(jitter_ms * 5.0, 20.0)  # Max 20 points
        
        # Source quality penalty
        if time_snap.source in ['wwv_startup', 'chu_startup', 'wwvh_startup']:
            health += 0  # Best source, no penalty
        elif time_snap.source == 'ntp':
            health -= 10
        elif time_snap.source == 'wall_clock':
            health -= 40
        
        # SNR bonus (strong signal = more reliable)
        snr_db = getattr(time_snap, 'detection_snr_db', None)
        if snr_db and snr_db > 20:
            health += min((snr_db - 20) * 0.5, 10.0)  # Max 10 point bonus
        
        # Confidence factor
        health *= time_snap.confidence
        
        return max(0, min(100, int(health)))
    
    def _write_csv(self, snapshot: TimingSnapshot):
        """Write snapshot to daily CSV file"""
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        csv_path = self.output_dir / f"{self.channel_name.replace(' ', '_')}_timing_metrics_{date_str}.csv"
        
        fieldnames = [
            'timestamp_utc', 'channel', 'source_type', 'quality', 'snr_db',
            'confidence', 'age_seconds', 'rtp_anchor', 'utc_anchor',
            'drift_ms', 'jitter_ms', 'ntp_offset_ms', 'health_score'
        ]
        
        file_exists = csv_path.exists()
        
        with open(csv_path, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp_utc': snapshot.timestamp_utc,
                'channel': snapshot.channel,
                'source_type': snapshot.source_type,
                'quality': snapshot.quality,
                'snr_db': f"{snapshot.snr_db:.1f}" if snapshot.snr_db else '',
                'confidence': f"{snapshot.confidence:.2f}",
                'age_seconds': f"{snapshot.age_seconds:.1f}",
                'rtp_anchor': snapshot.rtp_anchor,
                'utc_anchor': f"{snapshot.utc_anchor:.3f}",
                'drift_ms': f"{snapshot.drift_ms:.3f}",
                'jitter_ms': f"{snapshot.jitter_ms:.3f}",
                'ntp_offset_ms': f"{snapshot.ntp_offset_ms:.2f}" if snapshot.ntp_offset_ms else '',
                'health_score': snapshot.health_score
            })
    
    def _check_transition(self, snapshot: TimingSnapshot, 
                         current_snr_db: Optional[float],
                         current_confidence: float):
        """Check if time source transitioned and log event"""
        current_source = snapshot.source_type
        current_quality = snapshot.quality
        
        # First call - just store state
        if self.previous_source is None:
            self.previous_source = current_source
            self.previous_quality = current_quality
            self.previous_source_start_time = time.time()
            self.previous_snr_db = current_snr_db
            self.previous_confidence = current_confidence
            return
        
        # Check for transition
        if current_source != self.previous_source or current_quality != self.previous_quality:
            # Calculate duration on previous source
            duration_minutes = (time.time() - self.previous_source_start_time) / 60.0
            
            # Determine reason
            reason = self._determine_transition_reason(
                self.previous_source, current_source,
                self.previous_snr_db, current_snr_db,
                self.previous_confidence, current_confidence
            )
            
            # Create transition event
            transition = TimingTransition(
                timestamp=snapshot.timestamp_utc,
                channel=self.channel_name,
                from_source=self.previous_source,
                to_source=current_source,
                from_quality=self.previous_quality,
                to_quality=current_quality,
                reason=reason,
                last_snr_db=self.previous_snr_db,
                last_confidence=self.previous_confidence,
                duration_on_previous_source_minutes=duration_minutes
            )
            
            # Log transition
            self._write_transition(transition)
            
            logger.info(
                f"{self.channel_name}: Time source transition: "
                f"{self.previous_source} ({self.previous_quality}) → "
                f"{current_source} ({current_quality}), reason: {reason}"
            )
            
            # Update state
            self.previous_source = current_source
            self.previous_quality = current_quality
            self.previous_source_start_time = time.time()
        
        # Always update SNR/confidence
        self.previous_snr_db = current_snr_db
        self.previous_confidence = current_confidence
    
    def _determine_transition_reason(self, from_source: str, to_source: str,
                                    from_snr: Optional[float], to_snr: Optional[float],
                                    from_conf: float, to_conf: float) -> str:
        """Determine why transition occurred"""
        # Tone → NTP/Wall clock
        if from_source in ['wwv_startup', 'chu_startup', 'wwvh_startup']:
            if to_source == 'ntp':
                if from_snr and from_snr < 10:
                    return f"tone_snr_low (SNR={from_snr:.1f}dB)"
                elif from_conf < 0.7:
                    return f"tone_confidence_low (conf={from_conf:.2f})"
                else:
                    return "tone_lost"
            elif to_source == 'wall_clock':
                return "ntp_unavailable"
        
        # NTP → Tone (upgrade)
        if from_source == 'ntp' and to_source in ['wwv_startup', 'chu_startup', 'wwvh_startup']:
            if to_snr:
                return f"tone_detected (SNR={to_snr:.1f}dB, conf={to_conf:.2f})"
            return "tone_recovered"
        
        # Tone → Tone (better reference)
        if (from_source in ['wwv_startup', 'chu_startup', 'wwvh_startup'] and
            to_source in ['wwv_startup', 'chu_startup', 'wwvh_startup']):
            if to_conf > from_conf + 0.05:
                return f"better_confidence (conf={to_conf:.2f})"
            if to_snr and from_snr and to_snr > from_snr + 5:
                return f"better_snr (SNR={to_snr:.1f}dB)"
            return "tone_upgrade"
        
        # Wall clock → NTP (recovery)
        if from_source == 'wall_clock' and to_source == 'ntp':
            return "ntp_restored"
        
        return "source_changed"
    
    def _write_transition(self, transition: TimingTransition):
        """Append transition to daily JSON log"""
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        json_path = self.output_dir / f"{self.channel_name.replace(' ', '_')}_timing_transitions_{date_str}.json"
        
        # Load existing transitions
        transitions = []
        if json_path.exists():
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                    transitions = data.get('transitions', [])
            except Exception as e:
                logger.warning(f"Error reading transitions file: {e}")
        
        # Append new transition
        transitions.append({
            'timestamp': transition.timestamp,
            'channel': transition.channel,
            'from_source': transition.from_source,
            'to_source': transition.to_source,
            'from_quality': transition.from_quality,
            'to_quality': transition.to_quality,
            'reason': transition.reason,
            'last_snr_db': transition.last_snr_db,
            'last_confidence': transition.last_confidence,
            'duration_on_previous_source_minutes': round(transition.duration_on_previous_source_minutes, 1)
        })
        
        # Write back
        with open(json_path, 'w') as f:
            json.dump({'transitions': transitions}, f, indent=2)
    
    def write_propagation_status(self, snr_db: Optional[float] = None):
        """
        Write propagation mode status for web-UI display.
        
        Creates propagation_status.json with:
        - Expected propagation modes and delays for primary station
        - WWVH alternate propagation for shared frequencies (2.5, 5, 10, 15 MHz)
        - Distance to station
        - Current mode identification (if SNR provided)
        """
        if not self.prop_solver or not self.station:
            return
        
        try:
            # Get station distance
            distance_km = self.prop_solver.get_station_distance_km(self.station)
            
            # Get expected delay range
            delay_range = self.prop_solver.get_expected_delay_range_ms(
                self.station, self.frequency_mhz
            )
            
            # Calculate all modes
            modes = self.prop_solver.calculate_modes(self.station, self.frequency_mhz)
            
            # Build mode list for display
            mode_list = []
            for mode in modes[:5]:  # Top 5 modes
                mode_list.append({
                    'mode': mode.mode.value,
                    'n_hops': mode.n_hops,
                    'layer_height_km': round(mode.layer_height_km, 0),
                    'delay_ms': round(mode.total_delay_ms, 2),
                    'uncertainty_ms': round(mode.delay_uncertainty_ms, 2),
                    'elevation_deg': round(mode.elevation_angle_deg, 1)
                })
            
            # Status data
            status = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'channel': self.channel_name,
                'station': self.station,
                'frequency_mhz': self.frequency_mhz,
                'receiver_grid': self.receiver_grid,
                'distance_km': round(distance_km, 0),
                'expected_delay_range_ms': [round(delay_range[0], 1), round(delay_range[1], 1)],
                'propagation_modes': mode_list,
                'current_snr_db': round(snr_db, 1) if snr_db else None,
                'primary_mode': mode_list[0] if mode_list else None
            }
            
            # Add WWVH alternate for shared frequencies (2.5, 5, 10, 15 MHz)
            # WWV and WWVH share these frequencies; discrimination determines which is received
            if self.station == 'WWV' and self.frequency_mhz in WWVH_FREQUENCIES:
                try:
                    wwvh_distance = self.prop_solver.get_station_distance_km('WWVH')
                    wwvh_delay_range = self.prop_solver.get_expected_delay_range_ms(
                        'WWVH', self.frequency_mhz
                    )
                    wwvh_modes = self.prop_solver.calculate_modes('WWVH', self.frequency_mhz)
                    
                    wwvh_mode_list = []
                    for mode in wwvh_modes[:5]:
                        wwvh_mode_list.append({
                            'mode': mode.mode.value,
                            'n_hops': mode.n_hops,
                            'layer_height_km': round(mode.layer_height_km, 0),
                            'delay_ms': round(mode.total_delay_ms, 2),
                            'uncertainty_ms': round(mode.delay_uncertainty_ms, 2),
                            'elevation_deg': round(mode.elevation_angle_deg, 1)
                        })
                    
                    status['wwvh_alternate'] = {
                        'station': 'WWVH',
                        'distance_km': round(wwvh_distance, 0),
                        'expected_delay_range_ms': [round(wwvh_delay_range[0], 1), round(wwvh_delay_range[1], 1)],
                        'propagation_modes': wwvh_mode_list,
                        'primary_mode': wwvh_mode_list[0] if wwvh_mode_list else None
                    }
                except Exception as e:
                    logger.debug(f"{self.channel_name}: Could not add WWVH alternate: {e}")
            
            # Write status file
            status_path = self.output_dir / 'propagation_status.json'
            with open(status_path, 'w') as f:
                json.dump(status, f, indent=2)
                
        except Exception as e:
            logger.warning(f"{self.channel_name}: Error writing propagation status: {e}")
    
    def get_propagation_info(self) -> Optional[Dict[str, Any]]:
        """
        Get current propagation info for API response.
        
        Returns dict with station distance and expected modes.
        """
        if not self.prop_solver or not self.station:
            return None
        
        try:
            distance_km = self.prop_solver.get_station_distance_km(self.station)
            delay_range = self.prop_solver.get_expected_delay_range_ms(
                self.station, self.frequency_mhz
            )
            modes = self.prop_solver.calculate_modes(self.station, self.frequency_mhz)
            
            primary_mode = modes[0] if modes else None
            
            return {
                'station': self.station,
                'frequency_mhz': self.frequency_mhz,
                'distance_km': round(distance_km, 0),
                'expected_delay_ms': round(primary_mode.total_delay_ms, 2) if primary_mode else None,
                'expected_mode': primary_mode.mode.value if primary_mode else None,
                'delay_range_ms': [round(delay_range[0], 1), round(delay_range[1], 1)]
            }
        except Exception as e:
            logger.warning(f"{self.channel_name}: Error getting propagation info: {e}")
            return None
    
    # NOTE: get_ntp_offset() has been removed.
    # NTP offset is now obtained from the centralized NTPStatusCache in CoreRecorder
    # and passed to write_snapshot() via the ntp_offset_ms parameter.
    # This eliminates blocking subprocess calls in the analytics path.
