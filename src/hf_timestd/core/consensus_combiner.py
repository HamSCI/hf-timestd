"""
Consensus Combiner - Multi-Station UTC(NIST) Estimator

Combines independent D_clock measurements from multiple channels/stations
to produce a weighted consensus estimate of the system clock offset from UTC.

Theory:
    If our clock is truly offset by D_clock milliseconds from UTC(NIST),
    then ALL stations (WWV, WWVH, CHU) should report the same D_clock
    after their propagation delays are subtracted.
    
    In practice, measurements differ due to:
    - Ionospheric variability (different paths)
    - Multipath interference
    - SNR variations
    - Mode identification errors
    
    The Consensus Combiner:
    1. Groups measurements by station (WWV, WWVH, CHU)
    2. Weights by quality grade and SNR
    3. Detects outliers (>3σ from median)
    4. Produces weighted mean with uncertainty bounds

Output:
    d_clock_consensus_ms: Best estimate of clock offset
    uncertainty_ms: Confidence interval
    station_agreement: Whether stations agree (convergence metric)
    outliers: Channels excluded as outliers
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ChannelMeasurement:
    """A single D_clock measurement from one channel."""
    channel_name: str
    station: str  # WWV, WWVH, CHU
    d_clock_ms: float
    quality_grade: str  # A, B, C, D, X
    snr_db: float
    propagation_delay_ms: float
    n_hops: int
    confidence: float
    timestamp: float  # Unix timestamp of measurement


@dataclass
class StationEstimate:
    """Aggregated estimate from one station (across frequencies)."""
    station: str
    d_clock_ms: float  # Weighted mean across frequencies
    uncertainty_ms: float  # Std dev across frequencies
    n_channels: int
    best_quality: str
    mean_snr_db: float
    channels: List[str]


@dataclass
class ConsensusResult:
    """Final consensus UTC estimate."""
    # The consensus D_clock
    d_clock_ms: float
    uncertainty_ms: float
    
    # Station-level breakdown
    station_estimates: Dict[str, StationEstimate]
    
    # Convergence metrics
    station_agreement_ms: float  # Spread between stations
    convergence_state: str  # LOCKED, CONVERGING, DIVERGENT, SINGLE_SOURCE
    
    # Outlier detection
    outlier_channels: List[str]
    included_channels: int
    total_channels: int
    
    # Metadata
    timestamp: float
    computation_time_ms: float


# Quality grade weights (higher = more trusted)
GRADE_WEIGHTS = {
    'A': 1.0,
    'B': 0.7,
    'C': 0.4,
    'D': 0.15,
    'X': 0.0  # Exclude
}

# Minimum SNR to include (dB)
MIN_SNR_DB = 5.0

# Outlier threshold (number of MADs from median)
OUTLIER_THRESHOLD_MAD = 3.0


class ConsensusCombiner:
    """
    Combines multi-channel, multi-station D_clock measurements
    into a single consensus UTC(NIST) offset estimate.
    """
    
    def __init__(
        self,
        phase2_dir: Path,
        output_file: Path,
        channels: List[str]
    ):
        """
        Args:
            phase2_dir: Base directory containing phase2/{CHANNEL}/ subdirs
            output_file: Path to write consensus JSON
            channels: List of channel names to aggregate
        """
        self.phase2_dir = Path(phase2_dir)
        self.output_file = Path(output_file)
        self.channels = channels
        
    def _read_channel_status(self, channel_name: str) -> Optional[ChannelMeasurement]:
        """Read Phase 2 status for one channel."""
        # Convert channel name to directory format
        dir_name = channel_name.replace(' ', '_')
        status_file = self.phase2_dir / dir_name / 'status' / 'analytics-service-status.json'
        
        if not status_file.exists():
            return None
            
        try:
            with open(status_file) as f:
                data = json.load(f)
            
            # Extract channel data
            channel_data = data.get('channels', {}).get(channel_name)
            if not channel_data:
                # Try first channel in dict
                channel_data = list(data.get('channels', {}).values())[0] if data.get('channels') else None
            
            if not channel_data or channel_data.get('d_clock_ms') is None:
                return None
            
            return ChannelMeasurement(
                channel_name=channel_name,
                station=channel_data.get('station', 'UNKNOWN'),
                d_clock_ms=channel_data.get('d_clock_ms', 0),
                quality_grade=channel_data.get('quality_grade', 'X'),
                snr_db=channel_data.get('quality_metrics', {}).get('last_snr_db', 0) or 0,
                propagation_delay_ms=channel_data.get('propagation_delay_ms', 0) or 0,
                n_hops=channel_data.get('n_hops', 0) or 0,
                confidence=channel_data.get('time_snap', {}).get('confidence', 0) or 0,
                timestamp=time.time()
            )
        except Exception as e:
            logger.warning(f"Failed to read status for {channel_name}: {e}")
            return None
    
    def _group_by_station(
        self, 
        measurements: List[ChannelMeasurement]
    ) -> Dict[str, List[ChannelMeasurement]]:
        """Group measurements by transmitting station."""
        groups = {'WWV': [], 'WWVH': [], 'CHU': []}
        
        for m in measurements:
            station = m.station.upper()
            if station in groups:
                groups[station].append(m)
            else:
                logger.warning(f"Unknown station {station} for {m.channel_name}")
        
        return groups
    
    def _calculate_weight(self, m: ChannelMeasurement) -> float:
        """Calculate weight for a measurement based on quality and SNR."""
        grade_weight = GRADE_WEIGHTS.get(m.quality_grade, 0)
        
        if grade_weight == 0 or m.snr_db < MIN_SNR_DB:
            return 0.0
        
        # SNR weight: linear from MIN_SNR to 30 dB
        snr_weight = min(1.0, max(0.1, (m.snr_db - MIN_SNR_DB) / 25.0))
        
        # Confidence weight
        conf_weight = max(0.1, m.confidence)
        
        return grade_weight * snr_weight * conf_weight
    
    def _detect_outliers(
        self, 
        measurements: List[ChannelMeasurement]
    ) -> Tuple[List[ChannelMeasurement], List[str]]:
        """
        Detect and remove outlier measurements using MAD (Median Absolute Deviation).
        
        Returns:
            (included_measurements, outlier_channel_names)
        """
        if len(measurements) < 3:
            return measurements, []
        
        values = np.array([m.d_clock_ms for m in measurements])
        median = np.median(values)
        mad = np.median(np.abs(values - median))
        
        if mad < 0.01:  # All values nearly identical
            return measurements, []
        
        # Modified Z-score using MAD
        threshold = OUTLIER_THRESHOLD_MAD * mad * 1.4826  # 1.4826 scales MAD to std dev
        
        included = []
        outliers = []
        
        for m in measurements:
            if abs(m.d_clock_ms - median) > threshold:
                outliers.append(m.channel_name)
                logger.info(f"Outlier detected: {m.channel_name} D_clock={m.d_clock_ms:.2f}ms (median={median:.2f}ms)")
            else:
                included.append(m)
        
        return included, outliers
    
    def _estimate_station(
        self, 
        station: str, 
        measurements: List[ChannelMeasurement]
    ) -> Optional[StationEstimate]:
        """Calculate weighted estimate for one station."""
        if not measurements:
            return None
        
        weights = np.array([self._calculate_weight(m) for m in measurements])
        values = np.array([m.d_clock_ms for m in measurements])
        snrs = np.array([m.snr_db for m in measurements])
        
        total_weight = weights.sum()
        if total_weight < 0.01:
            return None
        
        # Weighted mean
        weighted_mean = (values * weights).sum() / total_weight
        
        # Weighted std dev
        if len(measurements) > 1:
            variance = (weights * (values - weighted_mean) ** 2).sum() / total_weight
            std_dev = np.sqrt(variance)
        else:
            std_dev = 2.0  # Single measurement uncertainty
        
        # Best quality grade
        grades_order = ['A', 'B', 'C', 'D', 'X']
        best_grade = min((m.quality_grade for m in measurements), 
                         key=lambda g: grades_order.index(g) if g in grades_order else 99)
        
        return StationEstimate(
            station=station,
            d_clock_ms=float(weighted_mean),
            uncertainty_ms=float(std_dev),
            n_channels=len(measurements),
            best_quality=best_grade,
            mean_snr_db=float(snrs.mean()),
            channels=[m.channel_name for m in measurements]
        )
    
    def compute_consensus(self) -> ConsensusResult:
        """
        Compute consensus D_clock from all available channel measurements.
        """
        start_time = time.time()
        
        # Read all channel statuses
        measurements = []
        for channel in self.channels:
            m = self._read_channel_status(channel)
            if m:
                measurements.append(m)
        
        total_channels = len(self.channels)
        
        if not measurements:
            return ConsensusResult(
                d_clock_ms=0.0,
                uncertainty_ms=100.0,
                station_estimates={},
                station_agreement_ms=0.0,
                convergence_state='NO_DATA',
                outlier_channels=[],
                included_channels=0,
                total_channels=total_channels,
                timestamp=time.time(),
                computation_time_ms=0.0
            )
        
        # Detect outliers across all measurements
        included, outliers = self._detect_outliers(measurements)
        
        # Group by station
        groups = self._group_by_station(included)
        
        # Calculate per-station estimates
        station_estimates = {}
        for station, station_measurements in groups.items():
            est = self._estimate_station(station, station_measurements)
            if est:
                station_estimates[station] = est
        
        # Calculate final consensus
        if not station_estimates:
            return ConsensusResult(
                d_clock_ms=0.0,
                uncertainty_ms=100.0,
                station_estimates={},
                station_agreement_ms=0.0,
                convergence_state='NO_DATA',
                outlier_channels=outliers,
                included_channels=len(included),
                total_channels=total_channels,
                timestamp=time.time(),
                computation_time_ms=(time.time() - start_time) * 1000
            )
        
        # Weight station estimates by their uncertainties and channel counts
        station_values = []
        station_weights = []
        
        for station, est in station_estimates.items():
            # Weight inversely by uncertainty, proportionally by channel count
            weight = est.n_channels / max(0.1, est.uncertainty_ms)
            station_values.append(est.d_clock_ms)
            station_weights.append(weight)
        
        station_values = np.array(station_values)
        station_weights = np.array(station_weights)
        total_weight = station_weights.sum()
        
        # Final weighted mean
        d_clock_consensus = (station_values * station_weights).sum() / total_weight
        
        # Station agreement (spread between station estimates)
        station_agreement = float(station_values.max() - station_values.min()) if len(station_values) > 1 else 0.0
        
        # Combined uncertainty (propagate from station uncertainties)
        combined_variance = sum(
            (w / total_weight) ** 2 * est.uncertainty_ms ** 2
            for w, est in zip(station_weights, station_estimates.values())
        )
        combined_uncertainty = np.sqrt(combined_variance + (station_agreement / 2) ** 2)
        
        # Determine convergence state
        n_stations = len(station_estimates)
        if n_stations == 0:
            state = 'NO_DATA'
        elif n_stations == 1:
            state = 'SINGLE_SOURCE'
        elif station_agreement < 1.0:
            state = 'LOCKED'
        elif station_agreement < 3.0:
            state = 'CONVERGING'
        else:
            state = 'DIVERGENT'
        
        result = ConsensusResult(
            d_clock_ms=float(d_clock_consensus),
            uncertainty_ms=float(combined_uncertainty),
            station_estimates={k: asdict(v) for k, v in station_estimates.items()},
            station_agreement_ms=station_agreement,
            convergence_state=state,
            outlier_channels=outliers,
            included_channels=len(included),
            total_channels=total_channels,
            timestamp=time.time(),
            computation_time_ms=(time.time() - start_time) * 1000
        )
        
        return result
    
    def run_and_save(self) -> ConsensusResult:
        """Compute consensus and save to output file."""
        result = self.compute_consensus()
        
        # Convert to JSON-serializable dict
        output = {
            'service': 'consensus_combiner',
            'version': '1.0',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'consensus': {
                'd_clock_ms': result.d_clock_ms,
                'uncertainty_ms': result.uncertainty_ms,
                'convergence_state': result.convergence_state,
                'station_agreement_ms': result.station_agreement_ms
            },
            'stations': result.station_estimates,
            'diagnostics': {
                'included_channels': result.included_channels,
                'total_channels': result.total_channels,
                'outlier_channels': result.outlier_channels,
                'computation_time_ms': result.computation_time_ms
            }
        }
        
        # Write atomically
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.output_file.with_suffix('.tmp')
        with open(temp_file, 'w') as f:
            json.dump(output, f, indent=2)
        temp_file.replace(self.output_file)
        
        logger.info(
            f"Consensus: D_clock={result.d_clock_ms:+.2f}ms ±{result.uncertainty_ms:.2f}ms, "
            f"state={result.convergence_state}, stations={list(result.station_estimates.keys())}"
        )
        
        return result


def create_combiner_from_config(config: dict, data_root: Path) -> ConsensusCombiner:
    """Create a ConsensusCombiner from config."""
    channels = []
    for ch in config.get('recorder', {}).get('channels', []):
        if ch.get('enabled', True):
            channels.append(ch.get('description', f"Channel {ch.get('ssrc')}"))
    
    phase2_dir = data_root / 'phase2'
    output_file = data_root / 'shared' / 'consensus_timing.json'
    
    return ConsensusCombiner(phase2_dir, output_file, channels)
