"""
Tone Detection Interface (Function 3)

Defines the contract for WWV/WWVH discrimination.
Separates timing reference (WWV) from propagation study (WWVH).
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import numpy as np
from .data_models import ToneDetectionResult, StationType


class ToneDetector(ABC):
    """
    Interface for Function 3: WWV/WWVH Tone Discrimination
    
    Detects and discriminates time standard radio station tones:
    - WWV (Fort Collins): 1000 Hz, 0.8s → TIME_SNAP REFERENCE
    - WWVH (Hawaii): 1200 Hz, 0.8s → PROPAGATION STUDY ONLY
    
    CRITICAL: The use_for_time_snap flag separates timing corrections
    from propagation analysis. Never mix these purposes!
    
    Design principle:
        Consumer (Function 1) doesn't care about matched filtering,
        correlation algorithms, or resampling details. Just needs
        detection results with clear purpose separation.
    """
    
    @abstractmethod
    def process_samples(
        self,
        timestamp: float,
        samples: np.ndarray,
        rtp_timestamp: Optional[int] = None
    ) -> Optional[List[ToneDetectionResult]]:
        """
        Process samples and detect tones.
        
        May return multiple detections if both WWV and WWVH are present
        (e.g., on 2.5, 5, 10, 15 MHz where both stations can be heard).
        
        Args:
            timestamp: UTC timestamp of samples (from time_snap if available)
            samples: Complex IQ samples at detector's expected rate
            rtp_timestamp: Optional RTP timestamp for provenance
            
        Returns:
            List of ToneDetectionResult objects (may contain WWV + WWVH),
            or None if no tones detected
            
        Example results:
            [
                ToneDetectionResult(
                    station=StationType.WWV,
                    frequency_hz=1000.0,
                    timing_error_ms=2.3,
                    use_for_time_snap=True  # ← Use for timing!
                ),
                ToneDetectionResult(
                    station=StationType.WWVH,
                    frequency_hz=1200.0,
                    timing_error_ms=15.7,
                    use_for_time_snap=False  # ← Propagation only!
                )
            ]
            
        Threading:
            May be called from Function 1's RTP processing thread.
            Should not block for long periods.
        """
        pass
    
    @abstractmethod
    def get_differential_delay(self) -> Optional[float]:
        """
        Get most recent WWV-WWVH differential propagation delay.
        
        Calculated when both WWV and WWVH are detected in same minute:
            differential_delay = wwv_timing_error - wwvh_timing_error
        
        This represents the difference in ionospheric propagation paths
        from Fort Collins vs Hawaii to the receiver.
        
        Returns:
            Differential delay in milliseconds, or None if not available
            
        Usage:
            Scientific analysis of ionospheric conditions:
                delay = detector.get_differential_delay()
                if delay:
                    print(f"Path difference: {delay:+.1f}ms")
        """
        pass
    
    @abstractmethod
    def get_detection_statistics(self) -> Dict[str, int]:
        """
        Get detection counts by station.
        
        Returns:
            dict with:
            - 'wwv_detections': int
            - 'wwvh_detections': int  
            - 'total_attempts': int
            - 'detection_rate_pct': float
            
        Usage:
            Status monitoring, quality assessment
        """
        pass
    
    @abstractmethod
    def get_station_active_list(self) -> List[StationType]:
        """
        Get list of stations that have been detected.
        
        Returns:
            List of StationType values that have been detected
            (useful for confirming which stations are receivable)
            
        Usage:
            status = detector.get_station_active_list()
            if StationType.WWVH in status:
                print("WWVH propagation analysis available")
        """
        pass
    
    @abstractmethod
    def set_detection_threshold(self, threshold: float) -> None:
        """
        Set detection confidence threshold.
        
        Args:
            threshold: Confidence threshold (0.0-1.0)
                      Higher = fewer false positives, may miss weak signals
                      Lower = more detections, may have false positives
                      
        Default: 0.5 (50% normalized correlation)
        
        Usage:
            Tune based on observed false positive/negative rates:
                # Aggressive (good propagation)
                detector.set_detection_threshold(0.3)
                
                # Conservative (noisy conditions)
                detector.set_detection_threshold(0.7)
        """
        pass
    
    @abstractmethod
    def get_last_detection_time(self) -> Optional[float]:
        """
        Get UTC timestamp of most recent detection.
        
        Returns:
            UTC timestamp of last successful detection (any station),
            or None if no detections yet
            
        Usage:
            Monitoring - alert if no detections for extended period:
                last = detector.get_last_detection_time()
                if last and (time.time() - last) > 3600:
                    alert("No WWV detections in past hour")
        """
        pass
    
    @abstractmethod
    def get_timing_accuracy_stats(self) -> Dict[str, float]:
        """
        Get timing accuracy statistics for time_snap-eligible stations.
        
        Only includes WWV (not WWVH).
        
        Returns:
            dict with:
            - 'mean_error_ms': float (mean timing error)
            - 'std_error_ms': float (standard deviation)
            - 'max_error_ms': float (worst timing error)
            - 'min_error_ms': float (best timing error)
            - 'sample_count': int (number of detections)
            
        Usage:
            Assess time_snap quality:
                stats = detector.get_timing_accuracy_stats()
                if stats['std_error_ms'] > 10.0:
                    logger.warning("High timing variability")
        """
        pass
    
    @abstractmethod
    def reset_statistics(self) -> None:
        """
        Reset detection statistics.
        
        Called at day boundaries to start fresh daily statistics.
        Does not affect detection configuration or state.
        
        Usage:
            # At midnight UTC
            detector.reset_statistics()
        """
        pass


class MultiStationToneDetector(ToneDetector):
    """
    Extended interface for detectors that handle multiple station types.
    
    Adds functionality specific to multi-station scenarios where
    WWV and WWVH can be detected simultaneously.
    """
    
    @abstractmethod
    def get_detections_by_station(
        self,
        station: StationType
    ) -> List[ToneDetectionResult]:
        """
        Get recent detections for specific station.
        
        Args:
            station: Which station to retrieve (WWV or WWVH)
            
        Returns:
            List of recent detections for that station (e.g., last hour)
            
        Usage:
            # Analyze WWVH propagation independently
            wwvh_detections = detector.get_detections_by_station(
                StationType.WWVH
            )
            for det in wwvh_detections:
                analyze_propagation(det)
        """
        pass
    
    @abstractmethod
    def get_differential_delay_history(
        self,
        count: int = 10
    ) -> List[Dict[str, float]]:
        """
        Get recent WWV-WWVH differential delay measurements.
        
        Args:
            count: Number of recent measurements to return
            
        Returns:
            List of dicts with:
            - 'timestamp': float (UTC)
            - 'differential_ms': float (WWV - WWVH timing)
            - 'wwv_snr_db': float
            - 'wwvh_snr_db': float
            
        Usage:
            Propagation time series analysis:
                history = detector.get_differential_delay_history(60)
                delays = [h['differential_ms'] for h in history]
                plot_time_series(delays)
        """
        pass
    
    @abstractmethod
    def configure_station_priorities(
        self,
        priorities: Dict[StationType, int]
    ) -> None:
        """
        Configure station priorities for time_snap selection.
        
        When multiple time_snap-eligible stations are detected,
        use priority to determine which to use.
        
        Args:
            priorities: Dict mapping StationType to priority (higher = preferred)
            
        Example:
            # Prefer WWV for time_snap
            detector.configure_station_priorities({
                StationType.WWV: 100,
                StationType.WWVH: 0  # Not used for time_snap anyway
            })
        """
        pass
