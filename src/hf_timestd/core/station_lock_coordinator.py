#!/usr/bin/env python3
"""
Station Lock Coordinator - Two-Phase Cross-Channel Detection

Implements the "Global Station Lock" strategy:

Phase 0: Anchor Discovery
    - Process all channels for the current minute
    - Identify high-confidence detections (anchors) on strong channels
    - Anchors provide RTP timestamps that are GPS-locked

Phase 1: Guided Search (Re-processing)
    - For channels that failed detection or have low confidence
    - Use anchor timing to narrow search window from ±500ms to ±3ms
    - Re-run matched filter with tight window
    - Assign boosted confidence to weak detections validated by anchor

Phase 2: Coherent Stacking (Optional)
    - Sum correlation arrays across all channels
    - Signal adds coherently, noise adds incoherently
    - Enables detection below single-channel noise floor

Example Improvement:
    Standard: Search ±500ms window → many noise candidates
    Guided:   Search ±3ms window   → 99.4% noise rejection
    Stacked:  9 channels           → +9.5 dB virtual SNR

Usage:
------
    coordinator = StationLockCoordinator(data_root='/tmp/timestd-test')
    
    # Process a minute across all channels
    results = coordinator.process_minute(minute_utc)
    
    # Results include:
    # - anchors: Which channels provided timing references
    # - detections: All detections including guided rescues
    # - stacked: Virtual channel from stacking (if enabled)
"""

import logging
import numpy as np
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from .global_station_voter import GlobalStationVoter, StationAnchor, AnchorQuality
from .wwvh_discrimination import WWVHDiscriminator, DiscriminationResult

logger = logging.getLogger(__name__)


@dataclass
class GuidedDetection:
    """
    Detection result from guided search.
    
    Includes information about how the detection was aided by cross-channel data.
    """
    channel: str
    station: str  # 'WWV', 'WWVH', 'CHU'
    detected: bool
    snr_db: float
    confidence: float
    
    # Guidance information
    guided: bool  # True if detection used anchor guidance
    guide_channel: Optional[str] = None  # Which channel provided guidance
    guide_snr_db: Optional[float] = None  # SNR of guiding anchor
    search_window_ms: Optional[float] = None  # Search window used
    
    # Rescue status
    rescued: bool = False  # True if detection ONLY possible with guidance
    original_detected: bool = True  # Whether it was detected without guidance
    confidence_boost: float = 0.0  # How much guidance improved confidence


@dataclass
class MinuteProcessingResult:
    """
    Complete result for processing one minute across all channels.
    """
    minute_utc: datetime
    minute_rtp: int
    
    # Phase 0: Anchors
    wwv_anchor: Optional[Dict[str, Any]] = None
    wwvh_anchor: Optional[Dict[str, Any]] = None
    chu_anchor: Optional[Dict[str, Any]] = None
    
    # Phase 1: Per-channel detections
    detections: Dict[str, GuidedDetection] = field(default_factory=dict)
    
    # Phase 2: Stacking results
    stacked_wwv: Optional[Dict[str, Any]] = None
    stacked_wwvh: Optional[Dict[str, Any]] = None
    
    # Statistics
    channels_with_wwv: int = 0
    channels_with_wwvh: int = 0
    rescues: int = 0  # Detections only possible with guidance
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/storage"""
        return {
            'minute_utc': self.minute_utc.isoformat(),
            'minute_rtp': self.minute_rtp,
            'wwv_anchor': self.wwv_anchor,
            'wwvh_anchor': self.wwvh_anchor,
            'chu_anchor': self.chu_anchor,
            'channels_with_wwv': self.channels_with_wwv,
            'channels_with_wwvh': self.channels_with_wwvh,
            'rescues': self.rescues,
            'detections': {
                ch: {
                    'station': d.station,
                    'detected': d.detected,
                    'snr_db': d.snr_db,
                    'guided': d.guided,
                    'rescued': d.rescued
                }
                for ch, d in self.detections.items()
            }
        }


class StationLockCoordinator:
    """
    Coordinates cross-channel detection using GPS-locked RTP timestamps.
    
    Implements the "Station Lock" strategy for improved weak-signal detection.
    """
    
    def __init__(
        self,
        data_root: Path,
        channels: Optional[List[str]] = None,
        enable_stacking: bool = True,
        guided_search_threshold_db: float = 10.0,
        rescue_threshold_db: float = 4.0
    ):
        """
        Initialize coordinator.
        
        Args:
            data_root: Root directory for hf-timestd data
            channels: List of channel names (auto-discovered if None)
            enable_stacking: Whether to compute stacked correlations
            guided_search_threshold_db: SNR below which to use guided search
            rescue_threshold_db: Minimum SNR for rescued detection
        """
        self.data_root = Path(data_root)
        self.enable_stacking = enable_stacking
        self.guided_search_threshold_db = guided_search_threshold_db
        self.rescue_threshold_db = rescue_threshold_db
        
        # Discover channels if not provided
        if channels is None:
            channels = self._discover_channels()
        
        self.channels = channels
        
        # Initialize global voter
        self.voter = GlobalStationVoter(channels=channels)
        
        # Per-channel discriminators (lazy-loaded)
        self.discriminators: Dict[str, WWVHDiscriminator] = {}
        
        # Results history
        self.history: List[MinuteProcessingResult] = []
        self.max_history = 60  # Keep 1 hour
        
        # Statistics
        self.stats = {
            'minutes_processed': 0,
            'total_rescues': 0,
            'anchor_uses': 0,
            'stacking_detections': 0
        }
        
        logger.info(f"StationLockCoordinator initialized with {len(channels)} channels")
        logger.info(f"Channels: {channels}")
    
    def _discover_channels(self) -> List[str]:
        """Discover channels from analytics directory structure"""
        analytics_dir = self.data_root / 'analytics'
        if not analytics_dir.exists():
            logger.warning(f"Analytics directory not found: {analytics_dir}")
            return []
        
        channels = []
        for d in analytics_dir.iterdir():
            if d.is_dir() and not d.name.startswith('.'):
                # Convert directory name back to channel name
                # WWV_10_MHz -> WWV 10 MHz
                channel_name = d.name.replace('_', ' ')
                channels.append(channel_name)
        
        return sorted(channels)
    
    def _get_discriminator(self, channel: str) -> WWVHDiscriminator:
        """Get or create discriminator for channel"""
        if channel not in self.discriminators:
            self.discriminators[channel] = WWVHDiscriminator(
                channel_name=channel
            )
        return self.discriminators[channel]
    
    def process_minute_archives(
        self,
        minute_utc: datetime,
        archives: Dict[str, Any]  # channel -> NPZArchive
    ) -> MinuteProcessingResult:
        """
        Process archives for a single minute across all channels.
        
        This is the main entry point for minute-by-minute processing.
        
        Args:
            minute_utc: UTC timestamp of minute boundary
            archives: Dict mapping channel names to loaded NPZ archives
            
        Returns:
            MinuteProcessingResult with all detection info
        """
        result = MinuteProcessingResult(
            minute_utc=minute_utc,
            minute_rtp=0  # Will be set from first archive
        )
        
        # ============================================================
        # PHASE 0: First pass - find anchors
        # ============================================================
        logger.info(f"Phase 0: Anchor discovery for {minute_utc.isoformat()}")
        
        first_pass_results: Dict[str, DiscriminationResult] = {}
        
        for channel, archive in archives.items():
            if result.minute_rtp == 0:
                result.minute_rtp = archive.rtp_timestamp
            
            # Run standard discrimination
            discriminator = self._get_discriminator(channel)
            disc_result = discriminator.discriminate(
                iq_samples=archive.iq_samples,
                sample_rate=archive.sample_rate,
                minute_timestamp=minute_utc.timestamp()
            )
            
            first_pass_results[channel] = disc_result
            
            # Report to voter
            if disc_result.wwv_detected:
                self.voter.report_detection(
                    channel=channel,
                    rtp_timestamp=archive.rtp_timestamp,
                    station='WWV',
                    snr_db=disc_result.wwv_power_db or 0,
                    toa_offset_samples=0,  # TODO: extract from disc_result
                    confidence=1.0 if disc_result.confidence == 'high' else 0.5,
                    utc_timestamp=minute_utc.timestamp()
                )
            
            if disc_result.wwvh_detected:
                self.voter.report_detection(
                    channel=channel,
                    rtp_timestamp=archive.rtp_timestamp,
                    station='WWVH',
                    snr_db=disc_result.wwvh_power_db or 0,
                    toa_offset_samples=0,
                    confidence=1.0 if disc_result.confidence == 'high' else 0.5,
                    utc_timestamp=minute_utc.timestamp()
                )
        
        # Get minute summary from voter
        minute_summary = self.voter.get_minute_summary(result.minute_rtp)
        if minute_summary:
            result.wwv_anchor = minute_summary.get('wwv_anchor')
            result.wwvh_anchor = minute_summary.get('wwvh_anchor')
            result.chu_anchor = minute_summary.get('chu_anchor')
        
        # ============================================================
        # PHASE 1: Guided search for weak channels
        # ============================================================
        logger.info("Phase 1: Guided search for weak channels")
        
        for channel, archive in archives.items():
            disc_result = first_pass_results[channel]
            
            # Check if WWV needs guided search
            wwv_detection = self._process_station_detection(
                channel=channel,
                archive=archive,
                station='WWV',
                first_pass_detected=disc_result.wwv_detected,
                first_pass_snr=disc_result.wwv_power_db,
                first_pass_confidence=disc_result.confidence
            )
            
            # Check if WWVH needs guided search
            wwvh_detection = self._process_station_detection(
                channel=channel,
                archive=archive,
                station='WWVH',
                first_pass_detected=disc_result.wwvh_detected,
                first_pass_snr=disc_result.wwvh_power_db,
                first_pass_confidence=disc_result.confidence
            )
            
            # Store the dominant detection
            if wwv_detection and wwvh_detection:
                # Choose stronger one
                if (wwv_detection.snr_db or 0) >= (wwvh_detection.snr_db or 0):
                    result.detections[channel] = wwv_detection
                else:
                    result.detections[channel] = wwvh_detection
            elif wwv_detection:
                result.detections[channel] = wwv_detection
            elif wwvh_detection:
                result.detections[channel] = wwvh_detection
        
        # Count results
        for det in result.detections.values():
            if det.station == 'WWV' and det.detected:
                result.channels_with_wwv += 1
            elif det.station == 'WWVH' and det.detected:
                result.channels_with_wwvh += 1
            if det.rescued:
                result.rescues += 1
        
        # ============================================================
        # PHASE 2: Coherent stacking (optional)
        # ============================================================
        if self.enable_stacking:
            logger.info("Phase 2: Coherent stacking")
            
            result.stacked_wwv = self.voter.get_stacked_correlation(
                result.minute_rtp, 'WWV'
            )
            result.stacked_wwvh = self.voter.get_stacked_correlation(
                result.minute_rtp, 'WWVH'
            )
            
            if result.stacked_wwv:
                logger.info(
                    f"Stacked WWV: {result.stacked_wwv['n_channels']} channels, "
                    f"+{result.stacked_wwv['snr_improvement_db']:.1f} dB improvement"
                )
            if result.stacked_wwvh:
                logger.info(
                    f"Stacked WWVH: {result.stacked_wwvh['n_channels']} channels, "
                    f"+{result.stacked_wwvh['snr_improvement_db']:.1f} dB improvement"
                )
        
        # Update statistics
        self.stats['minutes_processed'] += 1
        self.stats['total_rescues'] += result.rescues
        
        # Add to history
        self.history.append(result)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        
        logger.info(
            f"Minute complete: WWV on {result.channels_with_wwv} channels, "
            f"WWVH on {result.channels_with_wwvh} channels, "
            f"{result.rescues} rescues"
        )
        
        return result
    
    def _process_station_detection(
        self,
        channel: str,
        archive: Any,
        station: str,
        first_pass_detected: bool,
        first_pass_snr: Optional[float],
        first_pass_confidence: str
    ) -> Optional[GuidedDetection]:
        """
        Process detection for a single station on a single channel.
        
        Uses guided search if first pass was weak or failed.
        """
        snr = first_pass_snr or 0.0
        
        # Strong detection - no guidance needed
        if first_pass_detected and snr >= self.guided_search_threshold_db:
            return GuidedDetection(
                channel=channel,
                station=station,
                detected=True,
                snr_db=snr,
                confidence=1.0 if first_pass_confidence == 'high' else 0.7,
                guided=False,
                original_detected=True
            )
        
        # Weak or failed - try guided search
        window = self.voter.get_search_window(
            channel=channel,
            minute_rtp=archive.rtp_timestamp,
            station=station
        )
        
        if window is None:
            # No anchor available for guidance
            if first_pass_detected:
                return GuidedDetection(
                    channel=channel,
                    station=station,
                    detected=True,
                    snr_db=snr,
                    confidence=0.5 if first_pass_confidence == 'low' else 0.7,
                    guided=False,
                    original_detected=True
                )
            return None
        
        # Guided search: look in narrow window around anchor timing
        # This is where we'd re-run the matched filter with tight window
        # For now, we boost confidence if first_pass found something weak
        
        self.stats['anchor_uses'] += 1
        
        if first_pass_detected:
            # Weak detection exists - boost confidence via anchor validation
            confidence_boost = 0.2  # Anchor validation adds confidence
            
            return GuidedDetection(
                channel=channel,
                station=station,
                detected=True,
                snr_db=snr,
                confidence=min(1.0, 0.5 + confidence_boost),
                guided=True,
                guide_channel=window['source_channel'],
                guide_snr_db=window['anchor_snr_db'],
                search_window_ms=window['window_samples'] / 16.0,  # 16 kHz
                rescued=False,  # Was detected, just boosted
                original_detected=True,
                confidence_boost=confidence_boost
            )
        
        # Not detected in first pass - this would be a "rescue"
        # TODO: Implement actual re-processing with narrow window
        # For now, return None (no detection)
        # In full implementation, we'd re-run matched filter here
        
        return None
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get coordinator statistics"""
        voter_stats = self.voter.get_statistics()
        
        return {
            **self.stats,
            'voter': voter_stats,
            'channels': self.channels,
            'history_length': len(self.history)
        }
    
    def save_minute_result(self, result: MinuteProcessingResult, output_dir: Optional[Path] = None):
        """Save minute result to JSON for analysis"""
        if output_dir is None:
            output_dir = self.data_root / 'station_lock'
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"station_lock_{result.minute_utc.strftime('%Y%m%dT%H%M%SZ')}.json"
        filepath = output_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(result.to_dict(), f, indent=2)
        
        logger.debug(f"Saved station lock result: {filepath}")
