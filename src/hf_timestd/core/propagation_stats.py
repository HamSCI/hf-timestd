"""
Propagation Mode Statistics Calculator

Aggregates propagation mode observations from timing measurements to produce
hourly and daily statistics on propagation conditions and MUF estimates.

Author: HF-TimeStd Science Team
"""

from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter
import logging

logger = logging.getLogger(__name__)


class PropagationStatsCalculator:
    """
    Calculate propagation mode statistics from timing measurements.
    
    Aggregates propagation modes observed in timing measurements to produce
    statistics on mode probabilities, MUF estimates, and data quality.
    """
    
    # Propagation mode mapping
    VALID_MODES = {'1E', '1F', '2F', '3F', 'GW', 'UNKNOWN'}
    
    # Quality thresholds
    QUALITY_THRESHOLDS = {
        'GOOD': {'min_obs': 40, 'min_completeness': 0.8},
        'MARGINAL': {'min_obs': 20, 'min_completeness': 0.5},
        'BAD': {'min_obs': 0, 'min_completeness': 0.0}
    }
    
    def __init__(self, processing_version: str = "3.3.0"):
        """
        Initialize propagation statistics calculator.
        
        Args:
            processing_version: Version string for processing metadata
        """
        self.processing_version = processing_version
    
    def calculate_hourly_stats(
        self,
        measurements: List[Dict],
        period_start: datetime,
        period_end: datetime
    ) -> List[Dict]:
        """
        Calculate hourly propagation statistics.
        
        Args:
            measurements: List of timing measurements with propagation_mode field
            period_start: Start of aggregation period
            period_end: End of aggregation period
        
        Returns:
            List of hourly statistics dictionaries
        """
        # Group measurements by station and frequency
        grouped = self._group_measurements(measurements)
        
        stats_list = []
        
        for (station, freq_mhz), meas_list in grouped.items():
            stats = self._calculate_stats(
                measurements=meas_list,
                station=station,
                frequency_mhz=freq_mhz,
                period_start=period_start,
                period_end=period_end,
                aggregation_period='HOURLY',
                expected_observations=60  # 1 per minute
            )
            
            if stats:
                stats_list.append(stats)
        
        return stats_list
    
    def calculate_daily_stats(
        self,
        measurements: List[Dict],
        period_start: datetime,
        period_end: datetime
    ) -> List[Dict]:
        """
        Calculate daily propagation statistics.
        
        Args:
            measurements: List of timing measurements with propagation_mode field
            period_start: Start of aggregation period
            period_end: End of aggregation period
        
        Returns:
            List of daily statistics dictionaries
        """
        # Group measurements by station and frequency
        grouped = self._group_measurements(measurements)
        
        stats_list = []
        
        for (station, freq_mhz), meas_list in grouped.items():
            stats = self._calculate_stats(
                measurements=meas_list,
                station=station,
                frequency_mhz=freq_mhz,
                period_start=period_start,
                period_end=period_end,
                aggregation_period='DAILY',
                expected_observations=1440  # 1 per minute
            )
            
            if stats:
                stats_list.append(stats)
        
        return stats_list
    
    def _group_measurements(
        self,
        measurements: List[Dict]
    ) -> Dict[Tuple[str, float], List[Dict]]:
        """
        Group measurements by station and frequency.
        
        Args:
            measurements: List of timing measurements
        
        Returns:
            Dictionary mapping (station, frequency) to list of measurements
        """
        grouped = defaultdict(list)
        
        for m in measurements:
            station = m.get('station', 'UNKNOWN')
            freq_mhz = float(m.get('frequency_mhz', 0))
            
            grouped[(station, freq_mhz)].append(m)
        
        return grouped
    
    def _calculate_stats(
        self,
        measurements: List[Dict],
        station: str,
        frequency_mhz: float,
        period_start: datetime,
        period_end: datetime,
        aggregation_period: str,
        expected_observations: int
    ) -> Optional[Dict]:
        """
        Calculate statistics for a single station/frequency combination.
        
        Args:
            measurements: List of measurements for this station/frequency
            station: Station name
            frequency_mhz: Frequency in MHz
            period_start: Start of period
            period_end: End of period
            aggregation_period: 'HOURLY' or 'DAILY'
            expected_observations: Expected number of observations
        
        Returns:
            Statistics dictionary or None if insufficient data
        """
        if not measurements:
            return None
        
        n_observations = len(measurements)
        
        # Count propagation modes
        mode_counts = Counter()
        snr_values = []
        
        for m in measurements:
            mode = m.get('propagation_mode', 'UNKNOWN')
            
            # Normalize mode names
            mode = mode.upper().strip()
            if mode not in self.VALID_MODES:
                mode = 'UNKNOWN'
            
            mode_counts[mode] += 1
            
            # Collect SNR if available
            snr = m.get('snr_db')
            if snr is not None and snr > -999:
                snr_values.append(float(snr))
        
        # Calculate mode probabilities
        mode_probs = self._calculate_mode_probabilities(mode_counts, n_observations)
        
        # Estimate MUF
        muf_mhz, muf_confidence = self._estimate_muf(
            frequency_mhz=frequency_mhz,
            mode_probs=mode_probs,
            n_observations=n_observations
        )
        
        # Calculate data completeness
        data_completeness = min(1.0, n_observations / expected_observations)
        
        # Determine quality flag
        quality_flag = self._determine_quality_flag(n_observations, data_completeness)
        
        # Calculate mean SNR
        mean_snr = sum(snr_values) / len(snr_values) if snr_values else None
        
        # Build statistics dictionary
        stats = {
            'timestamp_utc': period_end.isoformat().replace('+00:00', 'Z'),
            'period_start': period_start.isoformat().replace('+00:00', 'Z'),
            'aggregation_period': aggregation_period,
            'station': station,
            'frequency_mhz': frequency_mhz,
            'mode_1e_probability': mode_probs['1E'],
            'mode_1f_probability': mode_probs['1F'],
            'mode_2f_probability': mode_probs['2F'],
            'mode_3f_probability': mode_probs['3F'],
            'mode_gw_probability': mode_probs['GW'],
            'mode_unknown_probability': mode_probs['UNKNOWN'],
            'estimated_muf_mhz': muf_mhz,
            'muf_confidence': muf_confidence,
            'mean_snr_db': mean_snr,
            'n_observations': n_observations,
            'data_completeness': data_completeness,
            'quality_flag': quality_flag,
            'processing_version': self.processing_version
        }
        
        return stats
    
    def _calculate_mode_probabilities(
        self,
        mode_counts: Counter,
        total_observations: int
    ) -> Dict[str, float]:
        """
        Calculate probability for each propagation mode.
        
        Args:
            mode_counts: Counter of mode occurrences
            total_observations: Total number of observations
        
        Returns:
            Dictionary mapping mode to probability
        """
        probs = {}
        
        for mode in self.VALID_MODES:
            count = mode_counts.get(mode, 0)
            probs[mode] = count / total_observations if total_observations > 0 else 0.0
        
        return probs
    
    def _estimate_muf(
        self,
        frequency_mhz: float,
        mode_probs: Dict[str, float],
        n_observations: int
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Estimate Maximum Usable Frequency (MUF) from mode probabilities.
        
        MUF is estimated as the highest frequency with F-layer propagation
        probability > 0.5. This is a simplified estimate.
        
        Args:
            frequency_mhz: Observation frequency
            mode_probs: Dictionary of mode probabilities
            n_observations: Number of observations
        
        Returns:
            Tuple of (estimated_muf_mhz, confidence) or (None, None)
        """
        # Calculate F-layer propagation probability
        f_layer_prob = (
            mode_probs.get('1F', 0.0) +
            mode_probs.get('2F', 0.0) +
            mode_probs.get('3F', 0.0)
        )
        
        # Only estimate MUF if we have significant F-layer propagation
        if f_layer_prob < 0.3:
            return None, None
        
        # For now, use a simple heuristic:
        # If F-layer propagation is dominant at this frequency,
        # MUF is likely higher than this frequency
        # This is a placeholder for more sophisticated MUF estimation
        
        if f_layer_prob > 0.7:
            # Strong F-layer: MUF likely higher
            estimated_muf = frequency_mhz * 1.2
        elif f_layer_prob > 0.5:
            # Moderate F-layer: MUF near this frequency
            estimated_muf = frequency_mhz * 1.1
        else:
            # Weak F-layer: MUF near or below this frequency
            estimated_muf = frequency_mhz
        
        # Confidence based on number of observations and F-layer probability
        confidence = min(1.0, (n_observations / 60.0) * f_layer_prob)
        
        return estimated_muf, confidence
    
    def _determine_quality_flag(
        self,
        n_observations: int,
        data_completeness: float
    ) -> str:
        """
        Determine quality flag based on observations and completeness.
        
        Args:
            n_observations: Number of observations
            data_completeness: Fraction of expected observations present
        
        Returns:
            Quality flag: 'GOOD', 'MARGINAL', or 'BAD'
        """
        if (n_observations >= self.QUALITY_THRESHOLDS['GOOD']['min_obs'] and
            data_completeness >= self.QUALITY_THRESHOLDS['GOOD']['min_completeness']):
            return 'GOOD'
        elif (n_observations >= self.QUALITY_THRESHOLDS['MARGINAL']['min_obs'] and
              data_completeness >= self.QUALITY_THRESHOLDS['MARGINAL']['min_completeness']):
            return 'MARGINAL'
        else:
            return 'BAD'
