#!/usr/bin/env python3
"""
Phase 2: Clock Offset Series (D_clock) - Data Structures and Persistence

================================================================================
PURPOSE
================================================================================
This module defines the DATA STRUCTURES and FILE I/O for the Clock Offset Series,
which is the primary output of Phase 2 analytics.

The Clock Offset Series is a time-indexed collection of D_clock measurements:

    D_clock = T_system - T_UTC(NIST)

Where:
    T_system = Monotonic system time from Phase 1 RTP timestamps
    T_UTC(NIST) = True UTC derived from WWV/WWVH/CHU radio signals

================================================================================
THE D_CLOCK EQUATION
================================================================================
The fundamental timing relationship:

    T_arrival = T_emission + T_propagation + D_clock

Rearranging to solve for D_clock:

    D_clock = T_arrival - T_emission - T_propagation

For time signal stations:
    T_emission = 0 (tones transmitted at exact second boundary)

Therefore:
    D_clock = T_arrival - T_propagation

Where:
    T_arrival = Detected tone arrival time (from matched filter)
    T_propagation = HF propagation delay (from TransmissionTimeSolver)

================================================================================
DATA STRUCTURES
================================================================================
This module defines three main data structures:

1. ClockOffsetMeasurement
   - A single D_clock measurement at one minute
   - Contains: d_clock_ms, station, propagation_mode, confidence, etc.
   - Quality graded: A (excellent), B (good), C (fair), D (poor), X (invalid)

2. ClockOffsetSeries
   - A time-indexed collection of measurements
   - Provides interpolation for arbitrary query times
   - Tracks quality summary and time bounds

3. ClockOffsetSeriesWriter
   - Persists measurements to CSV (continuous) and JSON (snapshots)
   - Coordinates with paths module for output location

================================================================================
QUALITY GRADES
================================================================================
Each measurement is assigned a quality grade:

    GRADE | UNCERTAINTY | DESCRIPTION
    ------|-------------|---------------------------------------------
      A   | < 0.5 ms    | Excellent - locked, multi-station verified
      B   | < 2 ms      | Good - single station, high confidence
      C   | < 5 ms      | Fair - moderate confidence
      D   | > 5 ms      | Poor - low confidence, use with caution
      X   | N/A         | Invalid - no valid measurement

Quality grading is performed by the ClockConvergenceModel based on:
    - Measurement uncertainty from TransmissionTimeSolver
    - Convergence state (acquiring vs locked)
    - Anomaly detection (propagation events)

================================================================================
FILE FORMATS
================================================================================
CSV (clock_offset_series.csv):
    - Continuous append-only time series
    - One row per minute
    - Primary columns:
        system_time          - Unix epoch seconds (float)
        utc_time             - Calculated UTC time (float)
        minute_boundary_utc  - Minute boundary this relates to (float)
        clock_offset_ms      - D_clock value in milliseconds (PRIMARY OUTPUT)
        station              - WWV, WWVH, CHU, or COMBINED
        frequency_mhz        - Carrier frequency
        propagation_delay_ms - Calculated propagation delay
        propagation_mode     - 1F, 2F, GW, etc.
        n_hops               - Number of ionospheric hops
        confidence           - 0-1 confidence score
        uncertainty_ms       - Estimated uncertainty
        quality_grade        - A, B, C, D, or X
        snr_db               - Signal-to-noise ratio
    
    NAMING CONVENTION NOTE (Issue 2.1 - 2025-12-08):
        The CSV column is named 'clock_offset_ms' for historical consistency.
        API responses rename this to 'd_clock_ms' for clarity.
        Both refer to the same value: D_clock = T_system - T_UTC(NIST)

JSON (clock_offset_{timestamp}.json):
    - Periodic snapshots with full metadata
    - Includes quality summary and measurement count
    - Used for API responses and archival

================================================================================
RELATIONSHIP TO OTHER MODULES
================================================================================
    Phase2TemporalEngine → produces Phase2Result
                         ↓
    ClockOffsetEngine → converts to ClockOffsetMeasurement
                      ↓
    ClockOffsetSeriesWriter → persists to CSV/JSON

The ClockOffsetEngine acts as an ADAPTER between the Phase2TemporalEngine
(which produces Phase2Result) and the persistence layer (ClockOffsetSeriesWriter).

================================================================================
USAGE
================================================================================
For batch processing:

    engine = ClockOffsetEngine(
        raw_buffer_dir=Path('/data/raw_buffer'),
        output_dir=Path('/data/phase2/WWV_10MHz/clock_offset'),
        channel_name='WWV_10MHz',
        frequency_hz=10e6,
        receiver_grid='EM38ww'
    )
    
    # Process a minute of data
    measurement = engine.process_minute(iq_samples, system_time, rtp_timestamp)
    
    # Query D_clock at a specific time
    offset_ms, uncertainty_ms = series.get_offset_at_time(target_time)

For continuous service, use Phase2AnalyticsService instead.

================================================================================
REVISION HISTORY
================================================================================
2025-12-07: Added comprehensive module documentation
2025-11-15: Integrated with Phase2TemporalEngine
2025-10-20: Initial implementation with data structures
"""

import numpy as np
import logging
import json
import csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple, Any, NamedTuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import threading

logger = logging.getLogger(__name__)


class ClockOffsetQuality(Enum):
    """Quality grades for clock offset measurements."""
    EXCELLENT = "A"  # Sub-millisecond uncertainty, multi-station verified
    GOOD = "B"       # 1-2ms uncertainty, single station high confidence
    FAIR = "C"       # 2-5ms uncertainty, moderate confidence
    POOR = "D"       # >5ms uncertainty or low confidence
    INVALID = "X"    # No valid measurement


@dataclass
class ClockOffsetMeasurement:
    """
    A single clock offset measurement.
    
    This represents D_clock at a specific time, derived from tone detection
    and propagation delay calculation.
    """
    # Time reference
    system_time: float           # System time (t_system) from Phase 1
    utc_time: float              # Calculated UTC time (t_UTC)
    minute_boundary_utc: float   # UTC minute boundary this measurement relates to
    
    # The clock offset: D_clock = t_system - t_UTC
    clock_offset_ms: float       # Offset in milliseconds
    
    # Source identification
    station: str                 # 'WWV', 'WWVH', 'CHU', or 'COMBINED'
    frequency_mhz: float         # Carrier frequency
    
    # Propagation analysis
    propagation_delay_ms: float  # Calculated propagation delay
    propagation_mode: str        # '1F', '2F', 'GW', etc.
    n_hops: int                  # Number of ionospheric hops
    
    # Quality metrics
    confidence: float            # 0-1 confidence score
    uncertainty_ms: float        # Estimated uncertainty
    quality_grade: ClockOffsetQuality = ClockOffsetQuality.FAIR
    
    # Detection metrics (from tone detector)
    snr_db: float = 0.0
    delay_spread_ms: float = 0.0
    doppler_std_hz: float = 0.0
    fss_db: Optional[float] = None  # Frequency selectivity strength
    
    # Discrimination results
    wwv_power_db: Optional[float] = None
    wwvh_power_db: Optional[float] = None
    discrimination_confidence: str = 'low'
    
    # Verification
    utc_verified: bool = False   # True if emission offset < 2ms
    multi_station_verified: bool = False
    
    # Provenance
    archive_file: Optional[str] = None
    rtp_timestamp: Optional[int] = None
    processing_version: str = "1.0.0"
    processed_at: Optional[float] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d['quality_grade'] = self.quality_grade.value
        return d
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'ClockOffsetMeasurement':
        """Create from dictionary."""
        d = d.copy()
        if isinstance(d.get('quality_grade'), str):
            d['quality_grade'] = ClockOffsetQuality(d['quality_grade'])
        return cls(**d)


@dataclass
class ClockOffsetSeries:
    """
    The Clock Offset Series - a time-indexed collection of D_clock measurements.
    
    This is the primary output of Phase 2, providing the correction data
    needed by Phase 3 to generate UTC-aligned products.
    """
    channel_name: str
    frequency_hz: float
    receiver_grid: str
    
    # Series data
    measurements: List[ClockOffsetMeasurement] = field(default_factory=list)
    
    # Series metadata
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    version: str = "1.0.0"
    created_at: Optional[float] = None
    
    def add_measurement(self, measurement: ClockOffsetMeasurement):
        """Add a measurement to the series."""
        self.measurements.append(measurement)
        
        # Update bounds
        if self.start_time is None or measurement.system_time < self.start_time:
            self.start_time = measurement.system_time
        if self.end_time is None or measurement.system_time > self.end_time:
            self.end_time = measurement.system_time
    
    def get_offset_at_time(
        self,
        target_time: float,
        interpolate: bool = True
    ) -> Optional[Tuple[float, float]]:
        """
        Get the clock offset at a specific time.
        
        Args:
            target_time: System time to query
            interpolate: Whether to interpolate between measurements
            
        Returns:
            Tuple of (offset_ms, uncertainty_ms) or None if not available
        """
        if not self.measurements:
            return None
        
        # Sort by time
        sorted_meas = sorted(self.measurements, key=lambda m: m.system_time)
        
        # Find bracketing measurements
        before = None
        after = None
        
        for m in sorted_meas:
            if m.system_time <= target_time:
                before = m
            elif after is None:
                after = m
                break
        
        if before is None and after is None:
            return None
        
        if before is None:
            return (after.clock_offset_ms, after.uncertainty_ms)
        
        if after is None:
            return (before.clock_offset_ms, before.uncertainty_ms)
        
        if not interpolate:
            # Return nearest
            if abs(target_time - before.system_time) < abs(target_time - after.system_time):
                return (before.clock_offset_ms, before.uncertainty_ms)
            else:
                return (after.clock_offset_ms, after.uncertainty_ms)
        
        # Linear interpolation
        dt = after.system_time - before.system_time
        if dt <= 0:
            return (before.clock_offset_ms, before.uncertainty_ms)
        
        alpha = (target_time - before.system_time) / dt
        offset = before.clock_offset_ms + alpha * (after.clock_offset_ms - before.clock_offset_ms)
        uncertainty = max(before.uncertainty_ms, after.uncertainty_ms) * (1 + alpha * (1 - alpha))
        
        return (offset, uncertainty)
    
    def get_quality_summary(self) -> Dict[str, int]:
        """Get count of measurements by quality grade."""
        counts = {g.value: 0 for g in ClockOffsetQuality}
        for m in self.measurements:
            counts[m.quality_grade.value] += 1
        return counts
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'channel_name': self.channel_name,
            'frequency_hz': self.frequency_hz,
            'receiver_grid': self.receiver_grid,
            'measurements': [m.to_dict() for m in self.measurements],
            'start_time': self.start_time,
            'end_time': self.end_time,
            'version': self.version,
            'created_at': self.created_at,
            'quality_summary': self.get_quality_summary(),
            'measurement_count': len(self.measurements)
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'ClockOffsetSeries':
        """Create from dictionary."""
        series = cls(
            channel_name=d['channel_name'],
            frequency_hz=d['frequency_hz'],
            receiver_grid=d['receiver_grid'],
            start_time=d.get('start_time'),
            end_time=d.get('end_time'),
            version=d.get('version', '1.0.0'),
            created_at=d.get('created_at')
        )
        for m_dict in d.get('measurements', []):
            series.measurements.append(ClockOffsetMeasurement.from_dict(m_dict))
        return series


class ClockOffsetSeriesWriter:
    """
    Write Clock Offset Series to files.
    
    Supports multiple output formats:
    - JSON: Full structured data with all metadata
    - CSV: Tabular format for analysis tools
    - DRF metadata: Integration with Digital RF
    """
    
    def __init__(self, output_dir: Path, channel_name: str):
        """
        Initialize writer.
        
        Args:
            output_dir: Clock offset directory from paths.get_clock_offset_dir()
                       Should be: {data_root}/phase2/{CHANNEL}/clock_offset/
            channel_name: Channel identifier
        """
        # Use output_dir directly - caller should provide paths.get_clock_offset_dir()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.channel_name = channel_name
        
        # CSV file for continuous logging
        self.csv_file = self.output_dir / 'clock_offset_series.csv'
        self._init_csv()
        
        logger.info(f"ClockOffsetSeriesWriter initialized: {self.output_dir}")
    
    def _init_csv(self):
        """Initialize CSV file with headers if needed."""
        if not self.csv_file.exists():
            with open(self.csv_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'system_time', 'utc_time', 'minute_boundary_utc',
                    'clock_offset_ms', 'station', 'frequency_mhz',
                    'propagation_delay_ms', 'propagation_mode', 'n_hops',
                    'confidence', 'uncertainty_ms', 'quality_grade',
                    'snr_db', 'delay_spread_ms', 'doppler_std_hz', 'fss_db',
                    'wwv_power_db', 'wwvh_power_db', 'discrimination_confidence',
                    'utc_verified', 'multi_station_verified',
                    'rtp_timestamp', 'processed_at'
                ])
    
    def write_measurement(self, measurement: ClockOffsetMeasurement):
        """
        Write a single measurement to CSV.
        
        Args:
            measurement: ClockOffsetMeasurement to write
        """
        with open(self.csv_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                measurement.system_time,
                measurement.utc_time,
                measurement.minute_boundary_utc,
                measurement.clock_offset_ms,
                measurement.station,
                measurement.frequency_mhz,
                measurement.propagation_delay_ms,
                measurement.propagation_mode,
                measurement.n_hops,
                measurement.confidence,
                measurement.uncertainty_ms,
                measurement.quality_grade.value,
                measurement.snr_db,
                measurement.delay_spread_ms,
                measurement.doppler_std_hz,
                measurement.fss_db,
                measurement.wwv_power_db,
                measurement.wwvh_power_db,
                measurement.discrimination_confidence,
                measurement.utc_verified,
                measurement.multi_station_verified,
                measurement.rtp_timestamp,
                measurement.processed_at
            ])
    
    def write_series(self, series: ClockOffsetSeries, filename: Optional[str] = None):
        """
        Write complete series to JSON file.
        
        Args:
            series: ClockOffsetSeries to write
            filename: Optional filename (default: auto-generated)
        """
        if filename is None:
            # Generate filename from time range
            start_dt = datetime.fromtimestamp(series.start_time or 0, tz=timezone.utc)
            filename = f"clock_offset_{start_dt.strftime('%Y%m%d_%H%M%S')}.json"
        
        output_file = self.output_dir / filename
        with open(output_file, 'w') as f:
            json.dump(series.to_dict(), f, indent=2)
        
        logger.info(f"Clock offset series written: {output_file}")
    
    def load_series(self, filename: str) -> Optional[ClockOffsetSeries]:
        """
        Load a series from JSON file.
        
        Args:
            filename: Filename to load
            
        Returns:
            ClockOffsetSeries or None if not found
        """
        input_file = self.output_dir / filename
        if not input_file.exists():
            return None
        
        with open(input_file, 'r') as f:
            data = json.load(f)
        
        return ClockOffsetSeries.from_dict(data)


class ClockOffsetEngine:
    """
    Phase 2 Analytical Engine - Generate Clock Offset Series from raw_buffer.
    
    This engine:
    1. Reads raw 20 kHz IQ from Phase 1 archive
    2. Detects synchronization tones (WWV, WWVH, CHU)
    3. Performs WWV/WWVH discrimination for correct propagation calculation
    4. Uses TransmissionTimeSolver to back-calculate UTC(NIST)
    5. Produces the Clock Offset Series (D_clock)
    """
    
    def __init__(
        self,
        raw_buffer_dir: Path,
        output_dir: Path,
        channel_name: str,
        frequency_hz: float,
        receiver_grid: str,
        sample_rate: int = 20000,
        timing_calibrator: Optional['TimingCalibrator'] = None
    ):
        """
        Initialize the clock offset engine.
        
        Args:
            raw_buffer_dir: Directory containing Phase 1 raw_buffer
            output_dir: Output directory for D_clock series
            channel_name: Channel identifier
            frequency_hz: Center frequency
            receiver_grid: Receiver grid square (for propagation calculation)
            sample_rate: Sample rate (default 20000)
            timing_calibrator: Optional TimingCalibrator for RTP-first timing
        """
        self.raw_buffer_dir = Path(raw_buffer_dir)
        self.output_dir = Path(output_dir)
        self.channel_name = channel_name
        self.frequency_hz = frequency_hz
        self.receiver_grid = receiver_grid
        self.sample_rate = sample_rate
        self.timing_calibrator = timing_calibrator
        
        # Initialize output writer
        self.writer = ClockOffsetSeriesWriter(output_dir, channel_name)
        
        # Current series
        self.current_series = ClockOffsetSeries(
            channel_name=channel_name,
            frequency_hz=frequency_hz,
            receiver_grid=receiver_grid,
            created_at=datetime.now(tz=timezone.utc).timestamp()
        )
        
        # Import required modules (lazy import to avoid circular deps)
        self._init_analyzers()
        
        # Processing state
        self._lock = threading.Lock()
        self.measurements_processed = 0
        
        logger.info(f"ClockOffsetEngine initialized for {channel_name}")
        logger.info(f"  Raw buffer: {raw_buffer_dir}")
        logger.info(f"  Output: {output_dir}")
        logger.info(f"  Receiver: {receiver_grid}")
    
    def _init_analyzers(self):
        """Initialize analysis components."""
        try:
            from .phase2_temporal_engine import Phase2TemporalEngine
            from .binary_archive_writer import BinaryArchiveReader
            
            # Phase 2 Temporal Engine - implements refined analysis order:
            # 1. Fundamental Tone Detection → Time Snap Anchor
            # 2. Ionospheric Channel Characterization → Confidence Scoring
            # 3. Transmission Time Solution → D_clock
            self.phase2_engine = Phase2TemporalEngine(
                raw_buffer_dir=self.raw_buffer_dir,
                output_dir=self.output_dir,
                channel_name=self.channel_name,
                frequency_hz=self.frequency_hz,
                receiver_grid=self.receiver_grid,
                sample_rate=self.sample_rate
            )
            
            # Wire RTP calibration callback for GPSDO-first timing
            if self.timing_calibrator is not None:
                self.phase2_engine.rtp_calibration_callback = self._get_calibrated_rtp_offset
                self.phase2_engine.station_predictor = self.timing_calibrator.predict_station
                logger.info("✅ RTP calibration callback wired for GPSDO-first timing")
            
            # Raw archive reader (for batch processing)
            self.archive_reader = BinaryArchiveReader(
                self.raw_buffer_dir,
                self.channel_name
            )
            
            logger.info("✅ Phase 2 Temporal Engine initialized")
            
        except ImportError as e:
            logger.error(f"Failed to initialize Phase 2 engine: {e}")
            raise
    
    def _get_calibrated_rtp_offset(self, channel_name: str) -> Optional[int]:
        """
        Get calibrated RTP offset for GPSDO-first timing.
        
        The RTP offset within a minute is deterministic with GPSDO.
        Once calibrated, this is the gold standard ruler for timing.
        
        Args:
            channel_name: Channel identifier
            
        Returns:
            Calibrated RTP offset (samples within minute), or None if not calibrated
        """
        if self.timing_calibrator is None:
            return None
        
        rtp_cal = self.timing_calibrator.rtp_calibration.get(channel_name)
        if rtp_cal is None:
            return None
        
        # Only use calibration if we have confirmations (stable)
        if rtp_cal.n_confirmations < 2:
            return None
        
        return rtp_cal.rtp_offset_samples
    
    def process_minute(
        self,
        iq_samples: np.ndarray,
        system_time: float,
        rtp_timestamp: int
    ) -> Optional[ClockOffsetMeasurement]:
        """
        Process one minute of IQ data to produce a clock offset measurement.
        
        This method now delegates to the Phase2TemporalEngine which implements
        the refined temporal analysis order:
        
        1. Fundamental Tone Detection → Time Snap Anchor
        2. Ionospheric Channel Characterization → Confidence Scoring
        3. Transmission Time Solution → D_clock
        
        Args:
            iq_samples: Complex64 IQ samples (60 seconds at sample_rate)
            system_time: System time of first sample
            rtp_timestamp: RTP timestamp of first sample
            
        Returns:
            ClockOffsetMeasurement or None if no valid detection
        """
        # Delegate to Phase2TemporalEngine for refined analysis order
        try:
            phase2_result = self.phase2_engine.process_minute(
                iq_samples=iq_samples,
                system_time=system_time,
                rtp_timestamp=rtp_timestamp
            )
        except Exception as e:
            logger.warning(f"Phase 2 processing failed: {e}")
            return None
        
        if phase2_result is None:
            logger.debug("Phase 2 returned no result")
            return None
        
        # Convert Phase2Result to ClockOffsetMeasurement
        solution = phase2_result.solution
        channel = phase2_result.channel
        time_snap = phase2_result.time_snap
        
        # Map quality from uncertainty_ms (Issue 6.2: quality_grade removed from Phase2Result)
        unc = phase2_result.uncertainty_ms
        if unc < 1.0:
            quality = ClockOffsetQuality.EXCELLENT
        elif unc < 3.0:
            quality = ClockOffsetQuality.GOOD
        elif unc < 10.0:
            quality = ClockOffsetQuality.FAIR
        else:
            quality = ClockOffsetQuality.POOR
        
        # Create measurement from Phase2Result
        measurement = ClockOffsetMeasurement(
            system_time=phase2_result.system_time,
            utc_time=phase2_result.utc_time,
            minute_boundary_utc=phase2_result.minute_boundary_utc,
            clock_offset_ms=solution.d_clock_ms,
            station=solution.station,
            frequency_mhz=solution.frequency_mhz,
            propagation_delay_ms=solution.t_propagation_ms,
            propagation_mode=solution.propagation_mode,
            n_hops=solution.n_hops,
            confidence=solution.confidence,
            uncertainty_ms=solution.uncertainty_ms,
            quality_grade=quality,
            snr_db=time_snap.wwv_snr_db or time_snap.wwvh_snr_db or time_snap.chu_snr_db or 0.0,
            delay_spread_ms=channel.delay_spread_ms or 0.5,
            doppler_std_hz=channel.doppler_wwv_std_hz or channel.doppler_wwvh_std_hz or 0.1,
            fss_db=None,  # TODO: Extract from test signal if available
            wwv_power_db=None,  # TODO: Map from channel characterization
            wwvh_power_db=None,
            discrimination_confidence=channel.station_confidence,
            utc_verified=solution.utc_verified,
            multi_station_verified=solution.dual_station_verified,
            rtp_timestamp=rtp_timestamp,
            processed_at=phase2_result.processed_at,
            processing_version=phase2_result.processing_version
        )
        
        # Add to series and write to CSV
        with self._lock:
            self.current_series.add_measurement(measurement)
            self.writer.write_measurement(measurement)
            self.measurements_processed += 1
        
        # Compute grade string for logging
        grade_str = 'A' if unc < 1.0 else 'B' if unc < 3.0 else 'C' if unc < 10.0 else 'D'
        logger.info(
            f"D_clock: {solution.d_clock_ms:+.2f}ms (station={solution.station}, "
            f"mode={solution.propagation_mode}, confidence={solution.confidence:.2f}, "
            f"uncertainty={unc:.1f}ms, grade={grade_str})"
        )
        
        return measurement
    
    def process_archive_file(self, file_path: Path) -> List[ClockOffsetMeasurement]:
        """
        Process a raw archive file to extract clock offset measurements.
        
        Args:
            file_path: Path to raw archive file
            
        Returns:
            List of ClockOffsetMeasurement objects
        """
        # This would read from DRF files and process minute by minute
        # For now, this is a placeholder for the full implementation
        raise NotImplementedError("Archive file processing not yet implemented")
    
    def get_current_series(self) -> ClockOffsetSeries:
        """Get the current clock offset series."""
        with self._lock:
            return self.current_series
    
    def save_series(self):
        """Save the current series to disk."""
        with self._lock:
            self.writer.write_series(self.current_series)
            logger.info(f"Saved series with {len(self.current_series.measurements)} measurements")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        with self._lock:
            quality_summary = self.current_series.get_quality_summary()
            return {
                'measurements_processed': self.measurements_processed,
                'measurements_in_series': len(self.current_series.measurements),
                'quality_summary': quality_summary,
                'start_time': self.current_series.start_time,
                'end_time': self.current_series.end_time
            }


# Convenience function
def create_clock_offset_engine(
    raw_buffer_dir: Path,
    output_dir: Path,
    channel_name: str,
    frequency_hz: float,
    receiver_grid: str,
    sample_rate: int = 20000
) -> ClockOffsetEngine:
    """
    Create a clock offset engine with standard configuration.
    
    Args:
        raw_buffer_dir: Directory containing Phase 1 raw_buffer
        output_dir: Output directory for D_clock series
        channel_name: Channel identifier
        frequency_hz: Center frequency
        receiver_grid: Receiver grid square
        sample_rate: Sample rate
        
    Returns:
        Configured ClockOffsetEngine
    """
    return ClockOffsetEngine(
        raw_buffer_dir=raw_buffer_dir,
        output_dir=output_dir,
        channel_name=channel_name,
        frequency_hz=frequency_hz,
        receiver_grid=receiver_grid,
        sample_rate=sample_rate
    )
