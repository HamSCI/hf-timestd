#!/usr/bin/env python3
"""
Primary Time Standard - HF Time Transfer Implementation

This module integrates all components to transform the hf-timestd receiver
into a PRIMARY TIME STANDARD that can verify UTC(NIST) directly.

The Chain of Trust:
-------------------
1. NIST Cesium Clock (Fort Collins) → WWV RF Emission
2. WWV RF Emission → Ionospheric Propagation → RTP Arrival (GPS-locked)
3. Propagation Mode Identification → Delay Calculation
4. Arrival Time - Delay = Emission Time = UTC(NIST)

The Result:
-----------
We can verify our local clock against UTC(NIST) with ~1 ms accuracy
by comparing:
    - GPS-disciplined RTP timestamp (arrival)
    - Back-calculated emission time (what UTC(NIST) says)
    
If they match (within tolerance), we've verified the entire chain
from NIST's atomic clock to our ADC.

Cross-Channel Verification:
---------------------------
Using Global Station Lock, we can:
1. Get emission time estimates from multiple frequencies
2. All should agree (within mode uncertainty)
3. Disagreement indicates mode misidentification
4. Agreement provides HIGH confidence in the result

Usage:
------
    standard = PrimaryTimeStandard(
        receiver_grid='EM38ww',
        data_root='/tmp/timestd-test'
    )
    
    # Process a minute's worth of data
    result = standard.process_minute(
        minute_utc=datetime.now(timezone.utc),
        channel_archives=archives  # Dict[channel, NPZArchive]
    )
    
    # Get verified UTC(NIST)
    print(f"UTC(NIST) offset: {result.utc_nist_offset_ms:.2f} ms")
    print(f"Confidence: {result.confidence:.0%}")
"""

import logging
import numpy as np
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import defaultdict
import json

from .propagation_mode_solver import (
    PropagationModeSolver,
    PropagationMode,
    ModeIdentificationResult,
    EmissionTimeResult
)
from .multi_station_detector import MultiStationDetector, StationDetection, DetectionQuality
from ..interfaces.data_models import TimeSnapReference

logger = logging.getLogger(__name__)


@dataclass
class ChannelTimeResult:
    """
    Time standard result for a single channel.
    """
    channel: str
    station: str  # 'WWV', 'WWVH', 'CHU'
    frequency_mhz: float
    
    # Timing
    arrival_time_utc: float
    emission_time_utc: float
    propagation_delay_ms: float
    
    # Mode identification
    mode: PropagationMode
    n_hops: int
    mode_confidence: float
    
    # Quality
    snr_db: float
    accuracy_ms: float
    
    # Verification
    second_aligned: bool
    utc_offset_ms: float  # Offset from expected second boundary


@dataclass
class StationConsensus:
    """
    Consensus emission time for a station across all frequencies.
    
    When multiple channels detect the same station, their back-calculated
    emission times should agree. This provides cross-validation.
    """
    station: str
    
    # Consensus timing
    emission_time_utc: float  # Weighted average
    emission_time_std_ms: float  # Standard deviation
    
    # Contributing channels
    channel_results: List[ChannelTimeResult] = field(default_factory=list)
    n_channels: int = 0
    
    # Quality
    consensus_confidence: float = 0.0  # Higher if channels agree
    mode_agreement: bool = True  # True if all channels agree on mode
    
    # Verification
    utc_offset_ms: float = 0.0  # Offset from second boundary
    verified: bool = False  # True if high confidence and aligned


@dataclass
class MinuteTimeStandardResult:
    """
    Complete time standard result for one minute.
    """
    minute_utc: datetime
    
    # Per-station consensus
    wwv_consensus: Optional[StationConsensus] = None
    wwvh_consensus: Optional[StationConsensus] = None
    chu_consensus: Optional[StationConsensus] = None
    
    # Best overall result
    best_station: Optional[str] = None
    best_emission_time_utc: Optional[float] = None
    best_confidence: float = 0.0
    best_accuracy_ms: float = 10.0
    
    # Cross-station verification
    # If WWV and WWVH both visible, their emission times should match
    # (both synchronized to UTC(NIST))
    cross_station_agreement_ms: Optional[float] = None
    cross_verified: bool = False
    
    # Overall quality
    overall_confidence: float = 0.0
    utc_nist_offset_ms: float = 0.0  # Best estimate of local vs UTC(NIST)
    time_transfer_accuracy_ms: float = 10.0
    
    # Flags
    high_confidence: bool = False
    multi_path_detected: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage"""
        def consensus_to_dict(c: Optional[StationConsensus]) -> Optional[Dict]:
            if c is None:
                return None
            return {
                'station': str(c.station),
                'emission_time_utc': float(c.emission_time_utc),
                'emission_time_std_ms': float(c.emission_time_std_ms),
                'n_channels': int(c.n_channels),
                'consensus_confidence': float(c.consensus_confidence),
                'mode_agreement': bool(c.mode_agreement),
                'utc_offset_ms': float(c.utc_offset_ms),
                'verified': bool(c.verified)
            }
        
        return {
            'minute_utc': self.minute_utc.isoformat(),
            'wwv_consensus': consensus_to_dict(self.wwv_consensus),
            'wwvh_consensus': consensus_to_dict(self.wwvh_consensus),
            'chu_consensus': consensus_to_dict(self.chu_consensus),
            'best_station': self.best_station,
            'best_emission_time_utc': float(self.best_emission_time_utc) if self.best_emission_time_utc else None,
            'best_confidence': float(self.best_confidence),
            'best_accuracy_ms': float(self.best_accuracy_ms),
            'cross_station_agreement_ms': float(self.cross_station_agreement_ms) if self.cross_station_agreement_ms else None,
            'cross_verified': bool(self.cross_verified),
            'overall_confidence': float(self.overall_confidence),
            'utc_nist_offset_ms': float(self.utc_nist_offset_ms),
            'time_transfer_accuracy_ms': float(self.time_transfer_accuracy_ms),
            'high_confidence': bool(self.high_confidence)
        }


class PrimaryTimeStandard:
    """
    Primary Time Standard implementation using HF time transfer.
    
    Combines:
    1. GPS-disciplined RTP timestamps (arrival time)
    2. Propagation mode identification (delay calculation)
    3. Cross-channel validation (confidence boosting)
    4. Back-calculation (emission time = UTC at transmitter)
    """
    
    def __init__(
        self,
        receiver_grid: str,
        data_root: Optional[Path] = None,
        channels: Optional[List[str]] = None
    ):
        """
        Initialize primary time standard.
        
        Args:
            receiver_grid: Maidenhead grid square
            data_root: Root directory for data/output
            channels: List of channel names
        """
        self.receiver_grid = receiver_grid
        self.data_root = Path(data_root) if data_root else None
        
        # Initialize propagation solver
        self.prop_solver = PropagationModeSolver(receiver_grid=receiver_grid)
        
        # Default channels if not specified
        if channels is None:
            channels = [
                'WWV 2.5 MHz', 'WWV 5 MHz', 'WWV 10 MHz',
                'WWV 15 MHz', 'WWV 20 MHz', 'WWV 25 MHz',
                'CHU 3.33 MHz', 'CHU 7.85 MHz', 'CHU 14.67 MHz'
            ]
        
        self.channels = channels
        
        # Initialize multi-station detector for physics-based detection
        self.multi_station_detector = MultiStationDetector(
            receiver_lat=self.receiver_lat,
            receiver_lon=self.receiver_lon
        )
        
        # Output directory for results
        if self.data_root:
            self.output_dir = self.data_root / 'time_standard'
            self.output_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.output_dir = None
        
        # History
        self.history: List[MinuteTimeStandardResult] = []
        self.max_history = 1440  # 24 hours
        
        # Statistics
        self.stats = {
            'minutes_processed': 0,
            'high_confidence_results': 0,
            'cross_verified_results': 0,
            'mean_accuracy_ms': 0.0,
            'best_accuracy_ms': float('inf')
        }
        
        logger.info(f"PrimaryTimeStandard initialized at {receiver_grid}")
        logger.info(f"Distances to stations:")
        for station in ['WWV', 'WWVH', 'CHU']:
            dist = self.prop_solver.get_station_distance_km(station)
            logger.info(f"  {station}: {dist:.0f} km")
    
    def _extract_frequency(self, channel_name: str) -> float:
        """Extract frequency in MHz from channel name"""
        import re
        match = re.search(r'(\d+\.?\d*)\s*MHz', channel_name, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return 0.0
    
    def _extract_station(self, channel_name: str) -> str:
        """Extract station name from channel name"""
        upper = channel_name.upper()
        if 'WWVH' in upper:
            return 'WWVH'
        elif 'WWV' in upper:
            return 'WWV'
        elif 'CHU' in upper:
            return 'CHU'
        return 'UNKNOWN'
    
    def process_channel(
        self,
        channel: str,
        arrival_time_utc: float,
        snr_db: float,
        measured_delay_ms: Optional[float] = None,
        channel_metrics: Optional[Dict[str, float]] = None
    ) -> Optional[ChannelTimeResult]:
        """
        Process a single channel to get time standard result.
        
        Args:
            channel: Channel name
            arrival_time_utc: GPS-locked arrival time (from RTP + time_snap)
            snr_db: Detection SNR
            measured_delay_ms: Optional measured propagation delay
            channel_metrics: Optional channel quality metrics
            
        Returns:
            ChannelTimeResult or None if processing fails
        """
        station = self._extract_station(channel)
        frequency_mhz = self._extract_frequency(channel)
        
        if station == 'UNKNOWN' or frequency_mhz == 0:
            return None
        
        # Back-calculate emission time
        emission_result = self.prop_solver.back_calculate_emission_time(
            station=station,
            arrival_time_utc=arrival_time_utc,
            frequency_mhz=frequency_mhz,
            measured_delay_ms=measured_delay_ms,
            channel_metrics=channel_metrics
        )
        
        # Calculate offset from second boundary
        fractional_second = emission_result.emission_time_utc % 1.0
        if fractional_second > 0.5:
            fractional_second -= 1.0
        utc_offset_ms = fractional_second * 1000.0
        
        return ChannelTimeResult(
            channel=channel,
            station=station,
            frequency_mhz=frequency_mhz,
            arrival_time_utc=arrival_time_utc,
            emission_time_utc=emission_result.emission_time_utc,
            propagation_delay_ms=emission_result.propagation_delay_ms,
            mode=emission_result.mode,
            n_hops=emission_result.n_hops,
            mode_confidence=emission_result.confidence,
            snr_db=snr_db,
            accuracy_ms=emission_result.accuracy_ms,
            second_aligned=emission_result.second_aligned,
            utc_offset_ms=utc_offset_ms
        )
    
    def build_consensus(
        self,
        station: str,
        channel_results: List[ChannelTimeResult]
    ) -> StationConsensus:
        """
        Build consensus emission time from multiple channels.
        
        Weights by SNR and mode confidence to get best estimate.
        """
        if not channel_results:
            return StationConsensus(
                station=station,
                emission_time_utc=0,
                emission_time_std_ms=float('inf'),
                n_channels=0,
                consensus_confidence=0
            )
        
        # Weight by SNR * confidence
        weights = []
        emission_times = []
        
        for result in channel_results:
            weight = max(0.1, result.snr_db) * result.mode_confidence
            weights.append(weight)
            emission_times.append(result.emission_time_utc)
        
        weights = np.array(weights)
        emission_times = np.array(emission_times)
        
        # Normalize weights
        weights = weights / weights.sum()
        
        # Weighted average
        emission_time_utc = np.sum(weights * emission_times)
        
        # Weighted standard deviation
        if len(emission_times) > 1:
            variance = np.sum(weights * (emission_times - emission_time_utc)**2)
            emission_time_std_ms = np.sqrt(variance) * 1000
        else:
            emission_time_std_ms = channel_results[0].accuracy_ms
        
        # Check mode agreement
        modes = [r.mode for r in channel_results]
        n_hops = [r.n_hops for r in channel_results]
        mode_agreement = len(set(n_hops)) == 1  # All same hop count
        
        # Confidence based on agreement and number of channels
        consensus_confidence = np.mean([r.mode_confidence for r in channel_results])
        
        # Boost confidence if multiple channels agree
        if len(channel_results) >= 3 and emission_time_std_ms < 1.0:
            consensus_confidence = min(1.0, consensus_confidence * 1.3)
        
        # Further boost if modes agree
        if mode_agreement:
            consensus_confidence = min(1.0, consensus_confidence * 1.1)
        
        # Calculate UTC offset
        fractional_second = emission_time_utc % 1.0
        if fractional_second > 0.5:
            fractional_second -= 1.0
        utc_offset_ms = fractional_second * 1000.0
        
        # Verified if high confidence and aligned
        verified = (
            consensus_confidence > 0.7 and
            abs(utc_offset_ms) < 2.0 and
            len(channel_results) >= 2
        )
        
        return StationConsensus(
            station=station,
            emission_time_utc=emission_time_utc,
            emission_time_std_ms=emission_time_std_ms,
            channel_results=channel_results,
            n_channels=len(channel_results),
            consensus_confidence=consensus_confidence,
            mode_agreement=mode_agreement,
            utc_offset_ms=utc_offset_ms,
            verified=verified
        )
    
    def process_minute(
        self,
        minute_utc: datetime,
        channel_data: Dict[str, Dict[str, Any]]
    ) -> MinuteTimeStandardResult:
        """
        Process all channels for a single minute.
        
        Args:
            minute_utc: UTC timestamp of minute
            channel_data: Dict mapping channel names to data dicts:
                {
                    'arrival_time_utc': float,  # From RTP + time_snap
                    'snr_db': float,
                    'measured_delay_ms': Optional[float],
                    'channel_metrics': Optional[Dict]
                }
                
        Returns:
            MinuteTimeStandardResult with complete analysis
        """
        result = MinuteTimeStandardResult(minute_utc=minute_utc)
        
        # Process each channel
        wwv_results: List[ChannelTimeResult] = []
        wwvh_results: List[ChannelTimeResult] = []
        chu_results: List[ChannelTimeResult] = []
        
        for channel, data in channel_data.items():
            channel_result = self.process_channel(
                channel=channel,
                arrival_time_utc=data.get('arrival_time_utc', minute_utc.timestamp()),
                snr_db=data.get('snr_db', 0),
                measured_delay_ms=data.get('measured_delay_ms'),
                channel_metrics=data.get('channel_metrics')
            )
            
            if channel_result is None:
                continue
            
            # Sort by station
            if channel_result.station == 'WWV':
                wwv_results.append(channel_result)
            elif channel_result.station == 'WWVH':
                wwvh_results.append(channel_result)
            elif channel_result.station == 'CHU':
                chu_results.append(channel_result)
        
        # Build consensus for each station
        if wwv_results:
            result.wwv_consensus = self.build_consensus('WWV', wwv_results)
        if wwvh_results:
            result.wwvh_consensus = self.build_consensus('WWVH', wwvh_results)
        if chu_results:
            result.chu_consensus = self.build_consensus('CHU', chu_results)
        
        # Find best result
        best_consensus: Optional[StationConsensus] = None
        for consensus in [result.wwv_consensus, result.wwvh_consensus, result.chu_consensus]:
            if consensus and consensus.n_channels > 0:
                if best_consensus is None or consensus.consensus_confidence > best_consensus.consensus_confidence:
                    best_consensus = consensus
        
        if best_consensus:
            result.best_station = best_consensus.station
            result.best_emission_time_utc = best_consensus.emission_time_utc
            result.best_confidence = best_consensus.consensus_confidence
            result.best_accuracy_ms = best_consensus.emission_time_std_ms
            result.utc_nist_offset_ms = best_consensus.utc_offset_ms
        
        # Cross-station verification
        # WWV and WWVH are both synchronized to UTC(NIST)
        # If we see both, their emission times should match
        if result.wwv_consensus and result.wwvh_consensus:
            if result.wwv_consensus.n_channels > 0 and result.wwvh_consensus.n_channels > 0:
                # Both should emit at same time (synchronized to UTC)
                agreement_ms = abs(
                    result.wwv_consensus.emission_time_utc - 
                    result.wwvh_consensus.emission_time_utc
                ) * 1000
                
                result.cross_station_agreement_ms = agreement_ms
                
                # Verified if they agree within 2 ms
                result.cross_verified = agreement_ms < 2.0
                
                if result.cross_verified:
                    logger.info(
                        f"Cross-station verified: WWV-WWVH agreement = {agreement_ms:.2f} ms"
                    )
        
        # Overall quality
        result.overall_confidence = result.best_confidence
        if result.cross_verified:
            result.overall_confidence = min(1.0, result.overall_confidence * 1.2)
        
        result.high_confidence = result.overall_confidence > 0.8
        
        # Time transfer accuracy (conservative estimate)
        if best_consensus:
            result.time_transfer_accuracy_ms = max(
                result.best_accuracy_ms,
                abs(result.utc_nist_offset_ms) if not result.high_confidence else 0.5
            )
        
        # Update statistics
        self.stats['minutes_processed'] += 1
        if result.high_confidence:
            self.stats['high_confidence_results'] += 1
        if result.cross_verified:
            self.stats['cross_verified_results'] += 1
        if result.time_transfer_accuracy_ms < self.stats['best_accuracy_ms']:
            self.stats['best_accuracy_ms'] = result.time_transfer_accuracy_ms
        
        # Add to history
        self.history.append(result)
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        
        # Save result
        if self.output_dir:
            self._save_result(result)
        
        # Log summary
        logger.info(
            f"Time Standard: {result.best_station or 'None'} | "
            f"Offset: {result.utc_nist_offset_ms:+.2f} ms | "
            f"Accuracy: {result.time_transfer_accuracy_ms:.2f} ms | "
            f"Confidence: {result.overall_confidence:.0%}"
            f"{' ✓ VERIFIED' if result.cross_verified else ''}"
        )
        
        return result
    
    def _save_result(self, result: MinuteTimeStandardResult):
        """Save result to JSON file"""
        filename = f"time_standard_{result.minute_utc.strftime('%Y%m%dT%H%M%SZ')}.json"
        filepath = self.output_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(result.to_dict(), f, indent=2)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get accumulated statistics"""
        return {
            **self.stats,
            'history_length': len(self.history),
            'receiver_grid': self.receiver_grid,
            'channels': self.channels
        }
    
    def get_recent_accuracy(self, n_minutes: int = 60) -> Dict[str, float]:
        """
        Get accuracy statistics for recent results.
        
        Returns dict with mean, std, min, max accuracy in ms.
        """
        recent = self.history[-n_minutes:] if len(self.history) >= n_minutes else self.history
        
        if not recent:
            return {'mean_ms': 0, 'std_ms': 0, 'min_ms': 0, 'max_ms': 0}
        
        accuracies = [r.time_transfer_accuracy_ms for r in recent if r.high_confidence]
        
        if not accuracies:
            return {'mean_ms': 0, 'std_ms': 0, 'min_ms': 0, 'max_ms': 0}
        
        return {
            'mean_ms': float(np.mean(accuracies)),
            'std_ms': float(np.std(accuracies)),
            'min_ms': float(np.min(accuracies)),
            'max_ms': float(np.max(accuracies)),
            'n_samples': len(accuracies)
        }


# Convenience function for testing
def create_test_standard():
    """Create a test time standard for AC0G (EM38ww)"""
    return PrimaryTimeStandard(
        receiver_grid='EM38ww',
        data_root=Path('/tmp/timestd-test')
    )
