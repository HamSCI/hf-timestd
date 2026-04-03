"""
Timing Validation: Compare Fusion-Derived Timing with radiod GPS_TIME

This module provides utilities to validate HF fusion timing against
radiod's GPS_TIME/RTP_TIMESNAP (ground truth when GPS+PPS is available).

Metrological Purpose:
--------------------
With L5 (GPS+PPS local) or L4 (GPS+PPS LAN), radiod's GPS_TIME provides
±100ns to ±1μs accuracy. This serves as ground truth to validate the
HF fusion methodology.

The validation answers: "How accurate is HF-derived timing compared to GPS?"

Usage:
------
    validator = TimingValidator(timing_config)
    
    # On each fusion update
    validator.record_fusion_offset(fusion_offset_ms, fusion_uncertainty_ms)
    
    # On each timing snapshot from radiod
    validator.record_radiod_snapshot(gps_time_ns, rtp_timesnap, current_rtp)
    
    # Get comparison
    discrepancy = validator.get_discrepancy_ms()
    if abs(discrepancy) > timing_config.validation_threshold_ms:
        logger.warning(f"Timing discrepancy: {discrepancy:.2f}ms")
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from collections import deque

from ..interfaces.data_models import TimingConfig, TimingAuthority

logger = logging.getLogger(__name__)

# Constants for GPS/Unix time conversion
GPS_EPOCH_UNIX = 315964800  # Unix timestamp of GPS epoch (Jan 6, 1980)
from .leap_second import get_current_gps_leap_seconds
GPS_LEAP_SECONDS = get_current_gps_leap_seconds()
BILLION = 1_000_000_000


@dataclass
class TimingComparison:
    """Result of comparing fusion timing to radiod timing."""
    timestamp: float  # Unix time of comparison
    fusion_offset_ms: float  # Fusion-derived D_clock
    radiod_offset_ms: float  # radiod GPS_TIME-derived offset
    discrepancy_ms: float  # Difference (fusion - radiod)
    fusion_uncertainty_ms: float
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp,
            'fusion_offset_ms': self.fusion_offset_ms,
            'radiod_offset_ms': self.radiod_offset_ms,
            'discrepancy_ms': self.discrepancy_ms,
            'fusion_uncertainty_ms': self.fusion_uncertainty_ms
        }


class TimingValidator:
    """
    Validates HF fusion timing against radiod's GPS_TIME ground truth.
    
    In L4/L5 scenarios (GPS+PPS on radiod), this provides rigorous
    validation of the HF timing methodology.
    
    In L3/L2/L1 scenarios, this documents the discrepancy between
    NTP-derived time and HF fusion time.
    """
    
    def __init__(self, timing_config: TimingConfig, sample_rate: int = 24000):
        """
        Initialize timing validator.
        
        Args:
            timing_config: TimingConfig from [timing] section
            sample_rate: Sample rate for RTP timestamp conversion
        """
        self.timing_config = timing_config
        self.sample_rate = sample_rate
        
        # Latest values
        self._last_fusion_offset_ms: Optional[float] = None
        self._last_fusion_uncertainty_ms: float = 0.0
        self._last_radiod_gps_time_ns: Optional[int] = None
        self._last_radiod_rtp_timesnap: Optional[int] = None
        self._last_radiod_receipt_time: float = 0.0
        
        # History for statistics (last 10 minutes)
        self._comparison_history: deque = deque(maxlen=1200)  # ~2 Hz for 10 min
        
        # Statistics
        self.comparisons_made = 0
        self.alerts_raised = 0
        
        logger.info(f"TimingValidator initialized: authority={timing_config.authority.value}, "
                   f"threshold={timing_config.validation_threshold_ms}ms")
    
    def record_fusion_offset(self, offset_ms: float, uncertainty_ms: float = 0.0):
        """
        Record latest fusion-derived D_clock offset.
        
        Args:
            offset_ms: Fusion D_clock in milliseconds
            uncertainty_ms: Fusion uncertainty estimate
        """
        self._last_fusion_offset_ms = offset_ms
        self._last_fusion_uncertainty_ms = uncertainty_ms
        
        # Trigger comparison if we have radiod data
        self._maybe_compare()
    
    def record_radiod_snapshot(
        self, 
        gps_time_ns: int, 
        rtp_timesnap: int,
        current_rtp: Optional[int] = None
    ):
        """
        Record GPS_TIME/RTP_TIMESNAP from radiod status.
        
        Args:
            gps_time_ns: radiod's GPS_TIME (ns since GPS epoch)
            rtp_timesnap: RTP timestamp at GPS_TIME moment
            current_rtp: Current RTP timestamp (for offset calculation)
        """
        self._last_radiod_gps_time_ns = gps_time_ns
        self._last_radiod_rtp_timesnap = rtp_timesnap
        self._last_radiod_receipt_time = time.time()
        
        # Trigger comparison if we have fusion data
        self._maybe_compare()
    
    def _maybe_compare(self):
        """Compare fusion and radiod timing if both are available."""
        if self._last_fusion_offset_ms is None:
            return
        if self._last_radiod_gps_time_ns is None:
            return
        
        # Calculate radiod-derived offset
        # GPS_TIME is ns since GPS epoch, convert to Unix time
        radiod_unix_ns = (
            self._last_radiod_gps_time_ns + 
            BILLION * (GPS_EPOCH_UNIX - GPS_LEAP_SECONDS)
        )
        radiod_unix_sec = radiod_unix_ns / BILLION
        
        # The "offset" from radiod's perspective is how much the local
        # wall clock differs from GPS time. For comparison with fusion,
        # we compute the equivalent D_clock.
        local_time = self._last_radiod_receipt_time
        radiod_offset_ms = (local_time - radiod_unix_sec) * 1000
        
        # Compute discrepancy
        discrepancy_ms = self._last_fusion_offset_ms - radiod_offset_ms
        
        comparison = TimingComparison(
            timestamp=time.time(),
            fusion_offset_ms=self._last_fusion_offset_ms,
            radiod_offset_ms=radiod_offset_ms,
            discrepancy_ms=discrepancy_ms,
            fusion_uncertainty_ms=self._last_fusion_uncertainty_ms
        )
        
        self._comparison_history.append(comparison)
        self.comparisons_made += 1
        
        # Check threshold
        if abs(discrepancy_ms) > self.timing_config.validation_threshold_ms:
            self.alerts_raised += 1
            logger.warning(
                f"[TIMING_VALIDATION] Discrepancy exceeds threshold: "
                f"{discrepancy_ms:+.2f}ms (threshold: ±{self.timing_config.validation_threshold_ms}ms) "
                f"fusion={self._last_fusion_offset_ms:.2f}ms, radiod={radiod_offset_ms:.2f}ms"
            )
            
            # In RTP authority mode, this is a serious misconfiguration
            if self.timing_config.authority == TimingAuthority.RTP:
                logger.error(
                    "[TIMING_VALIDATION] ALERT: RTP authority mode but large discrepancy! "
                    "Check if radiod actually has GPS+PPS, or switch to fusion authority."
                )
    
    def get_discrepancy_ms(self) -> Optional[float]:
        """Get latest discrepancy between fusion and radiod timing."""
        if not self._comparison_history:
            return None
        return self._comparison_history[-1].discrepancy_ms
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get validation statistics."""
        if not self._comparison_history:
            return {
                'comparisons_made': self.comparisons_made,
                'alerts_raised': self.alerts_raised,
                'mean_discrepancy_ms': None,
                'std_discrepancy_ms': None,
                'max_discrepancy_ms': None,
            }
        
        discrepancies = [c.discrepancy_ms for c in self._comparison_history]
        
        import statistics
        return {
            'comparisons_made': self.comparisons_made,
            'alerts_raised': self.alerts_raised,
            'mean_discrepancy_ms': statistics.mean(discrepancies),
            'std_discrepancy_ms': statistics.stdev(discrepancies) if len(discrepancies) > 1 else 0.0,
            'max_discrepancy_ms': max(abs(d) for d in discrepancies),
            'latest_discrepancy_ms': discrepancies[-1] if discrepancies else None,
            'history_length': len(discrepancies),
        }
    
    def get_recent_comparisons(self, count: int = 60) -> List[Dict[str, Any]]:
        """Get recent comparison history for dashboard display."""
        recent = list(self._comparison_history)[-count:]
        return [c.to_dict() for c in recent]
