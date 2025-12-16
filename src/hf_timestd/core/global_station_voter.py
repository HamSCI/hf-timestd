#!/usr/bin/env python3
"""
Global Station Voter - Cross-Channel Coherent Processing

DEPRECATED: This module is replaced by MultiStationDetector which uses a
physics-based approach. The "voting" concept was flawed - the GPSDO is the
timing reference, not the loudest station. All detected stations should be
passed to fusion, not voted on. Use MultiStationDetector for new code.

Leverages the GPS-disciplined RTP timestamps shared across all channels to
implement "Station Lock" - using strong detections on one frequency to guide
detection on weaker frequencies.

Key Insight:
------------
Because radiod's RTP timestamps are GPS-disciplined, all channels share a common
"ruler". A strong WWVH detection on 15 MHz tells us EXACTLY where to look for
WWVH on 2.5 MHz (within ionospheric dispersion ~3ms).

This enables:
1. **Anchor Discovery**: Find high-confidence detections on any frequency
2. **Guided Search**: Narrow the search window on weak channels from ±500ms to ±3ms
3. **Coherent Stacking**: Sum correlation arrays across frequencies for virtual SNR boost

Physics Caveat:
---------------
Group delay (dispersion) varies by frequency:
- 15 MHz vs 5 MHz: Typically < 2-3 ms differential
- WWV vs WWVH: ~15-20 ms separation (path length difference)

The dispersion uncertainty (3ms) << station separation (15ms), so a strong
detection of one station unambiguously identifies the search window on all bands.
"""

import logging
import numpy as np
import os
import glob
import json
import time
import fcntl
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import defaultdict
from enum import Enum

logger = logging.getLogger(__name__)

# Import shared constants
from .wwv_constants import (
    MAX_DISPERSION_MS,
    STATION_SEPARATION_MS,
    ANCHOR_SNR_HIGH,
    ANCHOR_SNR_MEDIUM,
    ANCHOR_SNR_LOW,
    SAMPLE_RATE_FULL,
    GUIDED_SEARCH_SAFETY_MARGIN_MS
)

# Samples per millisecond (computed from sample rate)
SAMPLES_PER_MS = SAMPLE_RATE_FULL // 1000  # 20 samples/ms at 20 kHz


class AnchorQuality(Enum):
    """Quality level of an anchor detection"""
    HIGH = "high"  # SNR > 15 dB, high confidence
    MEDIUM = "medium"  # SNR 10-15 dB, usable
    LOW = "low"  # SNR 6-10 dB, marginal
    NONE = "none"  # No valid anchor


@dataclass
class StationAnchor:
    """
    A high-confidence detection that can guide weaker channels.
    
    The RTP timestamp is the "ruler" position where we found the signal.
    """
    station: str  # 'WWV', 'WWVH', or 'CHU'
    channel: str  # Source channel (e.g., 'WWV_10_MHz')
    frequency_mhz: float  # Center frequency
    rtp_timestamp: int  # RTP timestamp of detection (GPS-locked)
    snr_db: float  # Detection SNR
    quality: AnchorQuality
    confidence: float  # 0-1 confidence score
    toa_offset_samples: int  # Offset from minute boundary in samples
    correlation_array: Optional[np.ndarray] = None  # Raw correlation for stacking
    
    def search_window_samples(self, target_freq_mhz: float) -> int:
        """
        Calculate search window size accounting for dispersion.
        
        Higher frequency difference = more dispersion uncertainty.
        Added SAFETY MARGIN to be conservative on initial lock.
        """
        freq_diff_mhz = abs(self.frequency_mhz - target_freq_mhz)
        
        # Empirical model: dispersion scales roughly with frequency difference
        # At HF, typical dispersion is ~0.1 ms/MHz (varies with ionosphere)
        # Add Safety Margin (5ms) to be robust against unmodeled errors
        dispersion_ms = min(MAX_DISPERSION_MS, 0.1 * freq_diff_mhz + 1.0)
        dispersion_ms += GUIDED_SEARCH_SAFETY_MARGIN_MS
        
        # Convert to samples (± window)
        window_samples = int(dispersion_ms * SAMPLES_PER_MS)
        
        # Minimum window based on sample rate (1 ms) for timing jitter
        return max(SAMPLES_PER_MS, window_samples)


@dataclass
class MinuteState:
    """
    State for a single minute across all channels.
    
    Tracks anchor detections and enables cross-channel coordination.
    """
    minute_rtp: int  # RTP timestamp at minute boundary
    utc_timestamp: float  # UTC time of minute start
    
    # Anchors by station
    wwv_anchor: Optional[StationAnchor] = None
    wwvh_anchor: Optional[StationAnchor] = None
    chu_anchor: Optional[StationAnchor] = None
    
    # Per-channel results
    channel_results: Dict[str, Any] = field(default_factory=dict)
    
    # Correlation arrays for stacking (channel -> array)
    wwv_correlations: Dict[str, np.ndarray] = field(default_factory=dict)
    wwvh_correlations: Dict[str, np.ndarray] = field(default_factory=dict)
    
    # Stacking results (computed on demand)
    stacked_wwv_correlation: Optional[np.ndarray] = None
    stacked_wwvh_correlation: Optional[np.ndarray] = None
    
    def get_anchor(self, station: str) -> Optional[StationAnchor]:
        """Get anchor for specified station"""
        if station == 'WWV':
            return self.wwv_anchor
        elif station == 'WWVH':
            return self.wwvh_anchor
        elif station == 'CHU':
            return self.chu_anchor
        return None
    
    def set_anchor(self, anchor: StationAnchor):
        """Set anchor if it's better than current"""
        current = self.get_anchor(anchor.station)
        
        # Replace if no current anchor or new one is better
        if current is None or anchor.snr_db > current.snr_db:
            if anchor.station == 'WWV':
                self.wwv_anchor = anchor
            elif anchor.station == 'WWVH':
                self.wwvh_anchor = anchor
            elif anchor.station == 'CHU':
                self.chu_anchor = anchor


class VoterBackend:
    """Abstract backend for voter state storage"""
    def save_anchor(self, minute_rtp: int, anchor: StationAnchor):
        raise NotImplementedError
        
    def get_anchors(self, minute_rtp: int) -> List[StationAnchor]:
        raise NotImplementedError

class MemoryBackend(VoterBackend):
    """In-memory backend for single-process use (testing/monolithic)"""
    def __init__(self):
        self.anchors: Dict[int, List[StationAnchor]] = defaultdict(list)
        
    def save_anchor(self, minute_rtp: int, anchor: StationAnchor):
        # Remove existing anchor for this station/channel if present
        current_list = self.anchors[minute_rtp]
        self.anchors[minute_rtp] = [
            a for a in current_list 
            if not (a.station == anchor.station and a.channel == anchor.channel)
        ]
        self.anchors[minute_rtp].append(anchor)
        
    def get_anchors(self, minute_rtp: int) -> List[StationAnchor]:
        return self.anchors[minute_rtp]

class FileBackend(VoterBackend):
    """
    File-based backend for multi-process use.
    Uses /dev/shm (RAM disk) for low-latency IPC.
    """
    def __init__(self, root_dir: Path = Path('/dev/shm/timestd_voter')):
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        # Clean up old files on startup? Maybe not, other processes might be running.
        
    def _get_minute_dir(self, minute_rtp: int) -> Path:
        """Get directory for a specific minute"""
        d = self.root_dir / str(minute_rtp)
        d.mkdir(EXIST_OK=True) 
        return d
        
    def save_anchor(self, minute_rtp: int, anchor: StationAnchor):
        """Save anchor to a JSON file"""
        try:
            minute_dir = self.root_dir / str(minute_rtp)
            minute_dir.mkdir(parents=True, exist_ok=True)
            
            # Filename: station_channel.json
            filename = f"{anchor.station}_{anchor.channel.replace(' ', '_')}.json"
            filepath = minute_dir / filename
            
            data = {
                'station': anchor.station,
                'channel': anchor.channel,
                'frequency_mhz': anchor.frequency_mhz,
                'rtp_timestamp': anchor.rtp_timestamp,
                'snr_db': anchor.snr_db,
                'quality': anchor.quality.value,
                'confidence': anchor.confidence,
                'toa_offset_samples': anchor.toa_offset_samples,
                'timestamp': time.time() # Write timestamp
            }
            
            # Atomic write using temp file and rename
            tmp_path = filepath.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            
            tmp_path.rename(filepath)
            
        except Exception as e:
            logger.error(f"Failed to save anchor to IPC: {e}")

    def get_anchors(self, minute_rtp: int) -> List[StationAnchor]:
        """Read all anchors for a minute"""
        anchors = []
        minute_dir = self.root_dir / str(minute_rtp)
        
        if not minute_dir.exists():
            return []
            
        try:
            for filepath in minute_dir.glob('*.json'):
                try:
                    with open(filepath, 'r') as f:
                        # Simple non-blocking read
                        data = json.load(f)
                        
                    anchors.append(StationAnchor(
                        station=data['station'],
                        channel=data['channel'],
                        frequency_mhz=data['frequency_mhz'],
                        rtp_timestamp=data['rtp_timestamp'],
                        snr_db=data['snr_db'],
                        quality=AnchorQuality(data['quality']),
                        confidence=data['confidence'],
                        toa_offset_samples=data['toa_offset_samples'],
                        correlation_array=None # Can't easily share numpy array via JSON
                    ))
                except (json.JSONDecodeError, KeyError):
                    continue # Ignore partial/corrupt files
                    
        except Exception as e:
            logger.error(f"Failed to read anchors from IPC: {e}")
            
        return anchors


class GlobalStationVoter:
    """
    Cross-channel coordination for coherent station detection.
    
    Uses GPS-disciplined RTP timestamps as a shared "ruler" to:
    1. Find strong detections (anchors) on any channel
    2. Guide detection on weak channels using anchor timing
    3. Stack correlations across channels for maximum sensitivity
    """
    
    def __init__(
        self,
        channels: List[str],
        sample_rate: int = 20000,
        history_minutes: int = 60,
        use_ipc: bool = True
    ):
        """
        Initialize global voter.
        
        Args:
            channels: List of channel names to coordinate
            sample_rate: Sample rate for RTP calculations
            history_minutes: Number of minutes to keep in history
            use_ipc: If True, use /dev/shm for cross-process coordination
        """
        self.channels = set(channels)
        self.sample_rate = sample_rate
        self.history_minutes = history_minutes
        
        # Select backend
        if use_ipc:
            self.backend = FileBackend()
            logger.info("GlobalStationVoter: Using FileBackend (IPC) at /dev/shm/timestd_voter")
        else:
            self.backend = MemoryBackend()
            logger.info("GlobalStationVoter: Using MemoryBackend (Local)")

        # Local cache of minute states to avoid constant disk reads for "my own" state
        self.minute_states: Dict[int, MinuteState] = {}
        
        # Channel -> frequency mapping (extracted from channel name)
        self.channel_frequencies: Dict[str, float] = {}
        for ch in channels:
            freq = self._extract_frequency(ch)
            if freq:
                self.channel_frequencies[ch] = freq
        
        # Statistics
        self.stats = {
            'anchors_found': 0,
            'guided_searches': 0,
            'stacked_detections': 0,
            'weak_channel_rescues': 0  # Detections that wouldn't exist without guidance
        }
        
        logger.info(f"GlobalStationVoter initialized with {len(channels)} channels")
        logger.info(f"Frequency map: {self.channel_frequencies}")
    
    def _extract_frequency(self, channel_name: str) -> Optional[float]:
        """Extract frequency in MHz from channel name"""
        # Pattern: "WWV 10 MHz" or "WWV_10_MHz" or "CHU 7.85 MHz"
        import re
        match = re.search(r'(\d+\.?\d*)\s*MHz', channel_name, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None
    
    def _minute_rtp_key(self, rtp_timestamp: int) -> int:
        """
        Convert RTP timestamp to minute boundary key.
        
        At 16 kHz, one minute = 960,000 samples.
        Floor to minute boundary.
        """
        samples_per_minute = self.sample_rate * 60
        return (rtp_timestamp // samples_per_minute) * samples_per_minute
    
    def _get_or_create_minute(self, minute_rtp: int, utc_timestamp: Optional[float] = None) -> MinuteState:
        """Get existing minute state or create new one"""
        if minute_rtp not in self.minute_states:
            utc = utc_timestamp or datetime.now(timezone.utc).timestamp()
            self.minute_states[minute_rtp] = MinuteState(
                minute_rtp=minute_rtp,
                utc_timestamp=utc
            )
            
            # Prune old minutes
            self._prune_history()
        
        return self.minute_states[minute_rtp]
    
    def _prune_history(self):
        """Remove old minute states beyond history limit"""
        if len(self.minute_states) > self.history_minutes:
            # Sort by minute_rtp and keep only recent ones
            sorted_keys = sorted(self.minute_states.keys())
            to_remove = sorted_keys[:-self.history_minutes]
            for key in to_remove:
                del self.minute_states[key]

    def _sync_from_backend(self, minute_rtp: int):
        """Update local state with anchors from backend"""
        minute_state = self._get_or_create_minute(minute_rtp)
        anchors = self.backend.get_anchors(minute_rtp)
        
        for anchor in anchors:
            minute_state.set_anchor(anchor)

    def report_detection(
        self,
        channel: str,
        rtp_timestamp: int,
        station: str,
        snr_db: float,
        toa_offset_samples: int,
        confidence: float,
        correlation_array: Optional[np.ndarray] = None,
        utc_timestamp: Optional[float] = None
    ):
        """
        Report a detection from a channel.
        
        If detection is strong enough, it becomes an anchor for other channels.
        """
        minute_rtp = self._minute_rtp_key(rtp_timestamp)
        minute_state = self._get_or_create_minute(minute_rtp, utc_timestamp)
        
        # Determine anchor quality
        if snr_db >= ANCHOR_SNR_HIGH:
            quality = AnchorQuality.HIGH
        elif snr_db >= ANCHOR_SNR_MEDIUM:
            quality = AnchorQuality.MEDIUM
        elif snr_db >= ANCHOR_SNR_LOW:
            quality = AnchorQuality.LOW
        else:
            quality = AnchorQuality.NONE
        
        # Create anchor if quality is sufficient
        if quality != AnchorQuality.NONE:
            freq = self.channel_frequencies.get(channel, 0.0)
            anchor = StationAnchor(
                station=station,
                channel=channel,
                frequency_mhz=freq,
                rtp_timestamp=rtp_timestamp,
                snr_db=snr_db,
                quality=quality,
                confidence=confidence,
                toa_offset_samples=toa_offset_samples,
                correlation_array=correlation_array
            )
            
            minute_state.set_anchor(anchor)
            self.stats['anchors_found'] += 1
            
            # Persist to backend
            self.backend.save_anchor(minute_rtp, anchor)
            
            logger.debug(
                f"Anchor set: {station} on {channel} @ {snr_db:.1f} dB "
                f"(quality={quality.value}, offset={toa_offset_samples} samples)"
            )
        
        # Store correlation for stacking
        if correlation_array is not None:
            if station == 'WWV':
                minute_state.wwv_correlations[channel] = correlation_array
            elif station == 'WWVH':
                minute_state.wwvh_correlations[channel] = correlation_array
    
    def get_search_window(
        self,
        channel: str,
        minute_rtp: int,
        station: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get guided search window for a weak channel.
        
        Returns the RTP position and window size to search, based on
        an anchor from a stronger channel.
        """
        minute_key = self._minute_rtp_key(minute_rtp)
        
        # Sync with backend
        self._sync_from_backend(minute_key)
        
        if minute_key not in self.minute_states:
            return None
        
        minute_state = self.minute_states[minute_key]
        anchor = minute_state.get_anchor(station)
        
        if anchor is None:
            return None
        
        # Don't use same channel as anchor
        if anchor.channel == channel:
            return None
        
        # Calculate search window accounting for dispersion
        target_freq = self.channel_frequencies.get(channel, 0.0)
        window_samples = anchor.search_window_samples(target_freq)
        
        self.stats['guided_searches'] += 1
        
        return {
            'center_rtp': anchor.rtp_timestamp,
            'center_offset_samples': anchor.toa_offset_samples,
            'window_samples': window_samples,
            'source_channel': anchor.channel,
            'source_freq_mhz': anchor.frequency_mhz,
            'anchor_snr_db': anchor.snr_db,
            'anchor_quality': anchor.quality.value
        }
    
    def get_stacked_correlation(
        self,
        minute_rtp: int,
        station: str,
        normalize: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Get coherently stacked correlation across all channels.
        """
        minute_key = self._minute_rtp_key(minute_rtp)
        
        if minute_key not in self.minute_states:
            return None
        
        minute_state = self.minute_states[minute_key]
        
        # Get correlation arrays for this station
        if station == 'WWV':
            correlations = minute_state.wwv_correlations
        elif station == 'WWVH':
            correlations = minute_state.wwvh_correlations
        else:
            return None
        
        if len(correlations) < 2:
            # Need at least 2 channels to stack
            return None
        
        # Find common length (trim to shortest)
        min_len = min(len(arr) for arr in correlations.values())
        
        # Stack correlations
        stacked = np.zeros(min_len, dtype=np.float64)
        for channel, arr in correlations.items():
            # Normalize each channel's correlation to unit peak
            arr_trimmed = arr[:min_len]
            if normalize:
                peak = np.max(np.abs(arr_trimmed))
                if peak > 0:
                    arr_trimmed = arr_trimmed / peak
            stacked += arr_trimmed
        
        # Theoretical SNR improvement: sqrt(N) for incoherent stacking
        n_channels = len(correlations)
        snr_improvement_db = 10 * np.log10(n_channels)  # Best case
        
        self.stats['stacked_detections'] += 1
        
        return {
            'stacked_correlation': stacked,
            'channels_used': list(correlations.keys()),
            'n_channels': n_channels,
            'snr_improvement_db': snr_improvement_db,
            'peak_index': int(np.argmax(stacked)),
            'peak_value': float(np.max(stacked))
        }
    
    def get_minute_summary(self, minute_rtp: int) -> Optional[Dict[str, Any]]:
        """
        Get summary of all detections for a minute.
        """
        minute_key = self._minute_rtp_key(minute_rtp)
        
        if minute_key not in self.minute_states:
            return None
        
        minute_state = self.minute_states[minute_key]
        
        def anchor_to_dict(anchor: Optional[StationAnchor]) -> Optional[Dict]:
            if anchor is None:
                return None
            return {
                'channel': anchor.channel,
                'frequency_mhz': anchor.frequency_mhz,
                'snr_db': anchor.snr_db,
                'quality': anchor.quality.value,
                'toa_offset_samples': anchor.toa_offset_samples
            }
        
        return {
            'minute_rtp': minute_rtp,
            'utc_timestamp': minute_state.utc_timestamp,
            'wwv_anchor': anchor_to_dict(minute_state.wwv_anchor),
            'wwvh_anchor': anchor_to_dict(minute_state.wwvh_anchor),
            'chu_anchor': anchor_to_dict(minute_state.chu_anchor),
            'wwv_channels_with_correlation': list(minute_state.wwv_correlations.keys()),
            'wwvh_channels_with_correlation': list(minute_state.wwvh_correlations.keys())
        }
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get voter statistics"""
        return {
            **self.stats,
            'minutes_tracked': len(self.minute_states),
            'channels': list(self.channels)
        }
    
    def get_best_time_snap_anchor(
        self,
        minute_rtp: int,
        prefer_wwv_chu: bool = True,
        min_snr_db: float = 8.0
    ) -> Optional[Dict[str, Any]]:
        """
        Get the best anchor across ALL channels for time_snap establishment.
        """
        minute_key = self._minute_rtp_key(minute_rtp)
        
        # Sync with backend to get latest anchors from other channels
        self._sync_from_backend(minute_key)
        
        if minute_key not in self.minute_states:
            return None
        
        minute_state = self.minute_states[minute_key]
        
        # Collect all valid anchors
        candidates = []
        
        # WWV anchor (preferred for timing)
        if minute_state.wwv_anchor and minute_state.wwv_anchor.snr_db >= min_snr_db:
            candidates.append({
                'anchor': minute_state.wwv_anchor,
                'station': 'WWV',
                'timing_preference': 1.0 if prefer_wwv_chu else 0.5
            })
        
        # CHU anchor (also preferred for timing)  
        if minute_state.chu_anchor and minute_state.chu_anchor.snr_db >= min_snr_db:
            candidates.append({
                'anchor': minute_state.chu_anchor,
                'station': 'CHU',
                'timing_preference': 1.0 if prefer_wwv_chu else 0.5
            })
        
        # WWVH anchor (lower timing preference - WWVH tones are at different offsets)
        if minute_state.wwvh_anchor and minute_state.wwvh_anchor.snr_db >= min_snr_db:
            candidates.append({
                'anchor': minute_state.wwvh_anchor,
                'station': 'WWVH',
                'timing_preference': 0.3 if prefer_wwv_chu else 0.5
            })
        
        if not candidates:
            return None
        
        # Score each candidate: SNR + timing preference bonus
        def score_candidate(c):
            anchor = c['anchor']
            snr_score = anchor.snr_db  # Higher SNR = better
            preference_bonus = c['timing_preference'] * 5.0  # Up to 5 dB equivalent
            
            # Quality bonus
            quality_bonus = {
                AnchorQuality.HIGH: 3.0,
                AnchorQuality.MEDIUM: 1.0,
                AnchorQuality.LOW: 0.0,
                AnchorQuality.NONE: -5.0
            }.get(anchor.quality, 0.0)
            
            return snr_score + preference_bonus + quality_bonus
        
        # Select best candidate
        best = max(candidates, key=score_candidate)
        anchor = best['anchor']
        
        logger.info(
            f"🏆 Best time_snap anchor: {anchor.station} @ {anchor.channel} "
            f"({anchor.snr_db:.1f} dB, quality={anchor.quality.value})"
        )
        
        return {
            'station': anchor.station,
            'channel': anchor.channel,
            'frequency_mhz': anchor.frequency_mhz,
            'rtp_timestamp': anchor.rtp_timestamp,
            'toa_offset_samples': anchor.toa_offset_samples,
            'snr_db': anchor.snr_db,
            'confidence': anchor.confidence,
            'quality': anchor.quality.value,
            'use_for_time_snap': anchor.station in ('WWV', 'CHU', 'WWVH'),
            'all_candidates': len(candidates)
        }
    
    def report_detection_result(
        self,
        channel: str,
        detection_result: Any,  # ToneDetectionResult
        minute_rtp: int,
        correlation_array: Optional[np.ndarray] = None
    ):
        """
        Convenience method to report a ToneDetectionResult.
        """
        # Extract station name handling both string and enum
        station = detection_result.station
        if hasattr(station, 'value'):
            station = station.value
        
        self.report_detection(
            channel=channel,
            rtp_timestamp=minute_rtp,
            station=station,
            snr_db=detection_result.snr_db,
            toa_offset_samples=int(detection_result.timing_error_ms * self.sample_rate / 1000),
            confidence=detection_result.confidence,
            correlation_array=correlation_array,
            utc_timestamp=detection_result.timestamp_utc
        )


# Convenience function for testing
def create_test_voter():
    """Create a voter with standard WWV/WWVH channel configuration"""
    channels = [
        'WWV 2.5 MHz',
        'WWV 5 MHz',
        'WWV 10 MHz',
        'WWV 15 MHz',
        'WWV 20 MHz',
        'WWV 25 MHz',
        'CHU 3.33 MHz',
        'CHU 7.85 MHz',
        'CHU 14.67 MHz'
    ]
    return GlobalStationVoter(channels=channels)
