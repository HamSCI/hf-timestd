"""
Data Models for HF Time Standard API Interfaces

These data structures define the contracts between the 6 core functions.
They formalize what Function 1 (quality/time_snap analysis) produces
and what Functions 2-6 consume.

Design principles:
- Immutable where possible (frozen dataclasses)
- Type hints for clarity
- Self-documenting field names
- Scientific provenance metadata included
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime
import numpy as np


# ============================================================================
# DISCONTINUITY TRACKING (Function 1 output)
# ============================================================================

class DiscontinuityType(Enum):
    """
    Types of discontinuities in the data stream.
    
    Every gap, jump, or correction is logged for scientific provenance.
    
    Categories:
    - Network/Processing: GAP, OVERFLOW, UNDERFLOW (normal packet-level issues)
    - System: RTP_RESET, SYNC_ADJUST (timing adjustments)
    - Infrastructure: SOURCE_UNAVAILABLE, RECORDER_OFFLINE (systematic failures)
    """
    GAP = "gap"                              # Missed RTP packets, samples lost
    SYNC_ADJUST = "sync_adjust"              # Time_snap correction applied
    RTP_RESET = "rtp_reset"                  # RTP sequence/timestamp reset
    OVERFLOW = "overflow"                    # Buffer overflow, samples dropped
    UNDERFLOW = "underflow"                  # Buffer underflow, samples duplicated
    SOURCE_UNAVAILABLE = "source_unavailable"  # radiod down/channel missing
    RECORDER_OFFLINE = "recorder_offline"    # signal-recorder daemon not running


@dataclass(frozen=True)
class Discontinuity:
    """
    Record of a timing discontinuity in the data stream.
    
    Critical for scientific provenance - every gap or time adjustment
    is permanently logged with full context.
    
    Attributes:
        timestamp: UTC timestamp when discontinuity occurred (seconds since epoch)
        sample_index: Sample number in output stream where discontinuity occurs
        discontinuity_type: Classification of the discontinuity
        magnitude_samples: Number of samples affected (positive=gap, negative=overlap)
        magnitude_ms: Time equivalent in milliseconds
        rtp_sequence_before: RTP sequence number before discontinuity
        rtp_sequence_after: RTP sequence number after discontinuity
        rtp_timestamp_before: RTP timestamp before discontinuity
        rtp_timestamp_after: RTP timestamp after discontinuity
        wwv_related: True if discontinuity related to WWV/CHU tone detection
        explanation: Human-readable description of cause
    """
    timestamp: float
    sample_index: int
    discontinuity_type: DiscontinuityType
    magnitude_samples: int
    magnitude_ms: float
    rtp_sequence_before: Optional[int]
    rtp_sequence_after: Optional[int]
    rtp_timestamp_before: Optional[int]
    rtp_timestamp_after: Optional[int]
    wwv_related: bool
    explanation: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            'timestamp': self.timestamp,
            'sample_index': self.sample_index,
            'type': self.discontinuity_type.value,
            'magnitude_samples': self.magnitude_samples,
            'magnitude_ms': self.magnitude_ms,
            'rtp_sequence_before': self.rtp_sequence_before,
            'rtp_sequence_after': self.rtp_sequence_after,
            'rtp_timestamp_before': self.rtp_timestamp_before,
            'rtp_timestamp_after': self.rtp_timestamp_after,
            'wwv_related': self.wwv_related,
            'explanation': self.explanation,
        }


# ============================================================================
# TIME_SNAP REFERENCE (Function 1 output)
# ============================================================================

@dataclass(frozen=True)
class TimeSnapReference:
    """
    Time anchor point established from WWV/CHU tone detection.
    
    Implements KA9Q-radio timing architecture with PPM correction:
        elapsed_time = (rtp_elapsed / sample_rate) * clock_ratio
        utc_time = time_snap_utc + elapsed_time
    
    The clock_ratio compensates for ADC clock drift:
        clock_ratio = 1 + (ppm_offset / 1e6)
    
    For example, if the ADC runs 5 PPM fast:
        ppm_offset = +5.0
        clock_ratio = 1.000005
        Each second of RTP time = 1.000005 seconds of real time
    
    Attributes:
        rtp_timestamp: RTP timestamp value at anchor point
        utc_timestamp: UTC time (seconds since epoch) at anchor point
        sample_rate: RTP clock rate (typically 20000 Hz)
        source: How this reference was established
        confidence: Confidence in this reference (0.0-1.0)
        station: Which station provided the reference (WWV, CHU, or initial guess)
        established_at: When this reference was created (wall clock time)
        ppm_offset: Measured clock drift in parts-per-million (+ = ADC fast)
        ppm_confidence: Confidence in the PPM measurement (0.0-1.0)
    """
    rtp_timestamp: int
    utc_timestamp: float
    sample_rate: int
    source: str  # 'wwv_first', 'wwv_verified', 'chu_first', 'chu_verified', 'initial_guess'
    confidence: float
    station: str  # 'WWV', 'CHU', 'initial'
    established_at: float  # Wall clock time when reference was created
    ppm_offset: float = 0.0  # Measured ADC clock drift (PPM)
    ppm_confidence: float = 0.0  # Confidence in PPM measurement
    
    @property
    def clock_ratio(self) -> float:
        """
        Clock ratio for PPM-corrected time calculation.
        
        Returns:
            1 + (ppm_offset / 1e6), or 1.0 if no PPM measurement
        """
        if self.ppm_confidence > 0.3:
            return 1.0 + (self.ppm_offset / 1e6)
        return 1.0  # No correction if low confidence
    
    def calculate_sample_time(self, rtp_timestamp: int) -> float:
        """
        Calculate UTC timestamp for a given RTP timestamp with PPM correction.
        
        This is the core time calculation: RTP offset → UTC time
        Corrected formula:
            elapsed = (rtp_elapsed / sample_rate) * clock_ratio
        
        Args:
            rtp_timestamp: RTP timestamp to convert
            
        Returns:
            UTC timestamp (seconds since epoch)
            
        Raises:
            ValueError: If elapsed time is unreasonable (wrap-around error)
        """
        # Handle RTP timestamp wrap-around (32-bit unsigned)
        rtp_elapsed = (rtp_timestamp - self.rtp_timestamp) & 0xFFFFFFFF
        # Handle large negative offsets (wrap-around detection)
        if rtp_elapsed > 0x80000000:
            rtp_elapsed -= 0x100000000
        
        # Apply PPM correction: actual elapsed time = nominal elapsed * clock_ratio
        elapsed_seconds = (rtp_elapsed / self.sample_rate) * self.clock_ratio
        
        # CRITICAL FIX: RTP wrap-around validation
        # Elapsed time should be reasonable (typically ~60s per minute)
        # If > 2 minutes, likely wrap-around error or packet reordering
        if abs(elapsed_seconds) > 120:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(
                f"CRITICAL: RTP elapsed time unreasonable: {elapsed_seconds:.1f}s "
                f"(rtp_elapsed={rtp_elapsed}, sample_rate={self.sample_rate}). "
                f"Possible wrap-around error or severe packet reordering."
            )
            raise ValueError(f"RTP elapsed time {elapsed_seconds:.1f}s exceeds bounds")
        
        return self.utc_timestamp + elapsed_seconds
    
    def with_updated_ppm(self, measured_ppm: float, measurement_confidence: float) -> 'TimeSnapReference':
        """
        Create new TimeSnapReference with updated PPM offset from tone-to-tone drift.
        
        Uses exponential smoothing to avoid jumps from noisy measurements.
        Returns self unchanged if measurement confidence is too low.
        
        Args:
            measured_ppm: New PPM measurement from tone-to-tone drift
            measurement_confidence: Confidence in this measurement (0.0-1.0)
            
        Returns:
            New TimeSnapReference with updated PPM values (or self if no update)
        """
        if measurement_confidence < 0.3:
            return self  # Ignore low-confidence measurements
        
        if self.ppm_confidence < 0.3:
            # First reliable measurement - use directly
            new_ppm = measured_ppm
            new_ppm_conf = measurement_confidence
        else:
            # Exponential smoothing: weight new measurement by its confidence
            alpha = 0.3 * measurement_confidence  # Learning rate scaled by confidence
            new_ppm = (1 - alpha) * self.ppm_offset + alpha * measured_ppm
            # Increase confidence (asymptotic to 1.0)
            new_ppm_conf = min(0.95, self.ppm_confidence + 0.05 * measurement_confidence)
        
        # Create new frozen instance with updated values
        return TimeSnapReference(
            rtp_timestamp=self.rtp_timestamp,
            utc_timestamp=self.utc_timestamp,
            sample_rate=self.sample_rate,
            source=self.source,
            confidence=self.confidence,
            station=self.station,
            established_at=self.established_at,
            ppm_offset=new_ppm,
            ppm_confidence=new_ppm_conf
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for metadata embedding"""
        station_val = self.station.value if hasattr(self.station, 'value') else self.station
        return {
            'rtp_timestamp': self.rtp_timestamp,
            'utc_timestamp': self.utc_timestamp,
            'sample_rate': self.sample_rate,
            'source': self.source,
            'confidence': self.confidence,
            'station': station_val,
            'established_at': self.established_at,
            'ppm_offset': self.ppm_offset,
            'ppm_confidence': self.ppm_confidence,
            'clock_ratio': self.clock_ratio,
        }


# ============================================================================
# QUALITY METADATA (Function 1 output)
# ============================================================================

@dataclass
class QualityInfo:
    """
    Quantitative quality metrics for a batch of samples.
    
    Produced by Function 1 (quality/time_snap analysis) and consumed
    by all downstream functions for metadata embedding and monitoring.
    
    NO SUBJECTIVE GRADING: Reports what happened, scientists decide usability.
    
    Attributes:
        completeness_pct: Percentage of expected samples received (0-100)
        gap_count: Number of gaps detected in this batch
        gap_duration_ms: Total duration of gaps in milliseconds
        packet_loss_pct: Percentage of RTP packets lost (0-100)
        resequenced_count: Number of packets that arrived out of order
        time_snap_established: Whether time_snap reference is active
        time_snap_confidence: Confidence in current time_snap (0.0-1.0)
        discontinuities: List of all discontinuities in this batch
        network_gap_ms: Total time lost to network/processing issues (GAP, OVERFLOW, UNDERFLOW)
        source_failure_ms: Total time lost to radiod unavailability (SOURCE_UNAVAILABLE)
        recorder_offline_ms: Total time recorder daemon was not running (RECORDER_OFFLINE)
    """
    completeness_pct: float
    gap_count: int
    gap_duration_ms: float
    packet_loss_pct: float
    resequenced_count: int
    time_snap_established: bool
    time_snap_confidence: float
    discontinuities: List[Discontinuity] = field(default_factory=list)
    network_gap_ms: float = 0.0
    source_failure_ms: float = 0.0
    recorder_offline_ms: float = 0.0
    
    def has_gaps(self) -> bool:
        """Check if any gaps present"""
        return self.gap_count > 0
    
    def get_gap_breakdown(self) -> Dict[str, Any]:
        """Categorize gaps by type for analysis"""
        network_gaps = []
        source_failures = []
        recorder_offline = []
        
        for disc in self.discontinuities:
            if disc.discontinuity_type in [DiscontinuityType.GAP, DiscontinuityType.OVERFLOW, DiscontinuityType.UNDERFLOW]:
                network_gaps.append(disc)
            elif disc.discontinuity_type == DiscontinuityType.SOURCE_UNAVAILABLE:
                source_failures.append(disc)
            elif disc.discontinuity_type == DiscontinuityType.RECORDER_OFFLINE:
                recorder_offline.append(disc)
        
        return {
            'network': {
                'count': len(network_gaps),
                'total_ms': sum(d.magnitude_ms for d in network_gaps),
                'gaps': [d.to_dict() for d in network_gaps]
            },
            'source_failure': {
                'count': len(source_failures),
                'total_ms': sum(d.magnitude_ms for d in source_failures),
                'gaps': [d.to_dict() for d in source_failures]
            },
            'recorder_offline': {
                'count': len(recorder_offline),
                'total_ms': sum(d.magnitude_ms for d in recorder_offline),
                'gaps': [d.to_dict() for d in recorder_offline]
            }
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON/metadata"""
        return {
            'completeness_pct': self.completeness_pct,
            'gap_count': self.gap_count,
            'gap_duration_ms': self.gap_duration_ms,
            'packet_loss_pct': self.packet_loss_pct,
            'resequenced_count': self.resequenced_count,
            'time_snap_established': self.time_snap_established,
            'time_snap_confidence': self.time_snap_confidence,
            'discontinuities': [d.to_dict() for d in self.discontinuities],
            'network_gap_ms': self.network_gap_ms,
            'source_failure_ms': self.source_failure_ms,
            'recorder_offline_ms': self.recorder_offline_ms,
            'gap_breakdown': self.get_gap_breakdown(),
        }


# ============================================================================
# SAMPLE BATCH (Function 1 output to Functions 2-5)
# ============================================================================

@dataclass
class SampleBatch:
    """
    Batch of quality-analyzed samples ready for downstream processing.
    
    This is the primary output of Function 1 and the input to Functions 2-5.
    Contains time-corrected IQ samples with full quality metadata.
    
    Attributes:
        timestamp: UTC timestamp of first sample (time_snap corrected)
        samples: Complex IQ samples (np.ndarray of complex64/128)
        sample_rate: Sample rate in Hz (typically 16000)
        quality: Quality metrics for this batch
        time_snap: Current time_snap reference (may be None if not yet established)
        channel_name: Human-readable channel identifier
        frequency_hz: Center frequency in Hz
        ssrc: RTP SSRC identifier
    """
    timestamp: float
    samples: np.ndarray
    sample_rate: int
    quality: QualityInfo
    time_snap: Optional[TimeSnapReference]
    channel_name: str
    frequency_hz: float
    ssrc: int
    
    def __len__(self) -> int:
        """Number of samples in batch"""
        return len(self.samples)
    
    def duration_seconds(self) -> float:
        """Duration of this batch in seconds"""
        return len(self.samples) / self.sample_rate
    
    def end_timestamp(self) -> float:
        """UTC timestamp of last sample"""
        return self.timestamp + self.duration_seconds()


# ============================================================================
# TONE DETECTION (Function 3 output)
# ============================================================================

class StationType(Enum):
    """
    Time standard radio stations.
    
    Critical distinction:
    - WWV (Fort Collins) and CHU (Ottawa): 1000 Hz tone, used for time_snap
    - WWVH (Hawaii): 1200 Hz tone, used ONLY for propagation analysis
    - BPM (Pucheng, China): 1000 Hz tone, shared frequency with WWV
    """
    WWV = "WWV"      # NIST Fort Collins, CO (1000 Hz) - TIME_SNAP SOURCE
    WWVH = "WWVH"    # NIST Hawaii (1200 Hz) - PROPAGATION STUDY ONLY
    CHU = "CHU"      # NRC Ottawa, Canada (1000 Hz) - TIME_SNAP SOURCE
    BPM = "BPM"      # NTSC Pucheng, China (1000 Hz) - PROPAGATION STUDY


@dataclass(frozen=True)
class ToneDetectionResult:
    """
    Result of WWV/WWVH/CHU tone detection.
    
    Produced by Function 3 (tone discrimination) for use by Function 1
    (time_snap updates) and scientific analysis (propagation studies).
    
    The `use_for_time_snap` field is CRITICAL: it separates timing
    reference (WWV/CHU) from propagation study (WWVH).
    
    Attributes:
        station: Which station was detected (WWV, WWVH, or CHU)
        frequency_hz: Tone frequency (1000 Hz or 1200 Hz)
        duration_sec: Measured tone duration in seconds
        timestamp_utc: UTC timestamp of tone rising edge
        timing_error_ms: Timing error vs expected :00.000 (milliseconds)
        snr_db: Signal-to-noise ratio of detection (dB)
        confidence: Detection confidence (0.0-1.0)
        use_for_time_snap: True for WWV/CHU (timing), False for WWVH (propagation)
        correlation_peak: Peak correlation value from matched filter
        noise_floor: Estimated noise floor during detection
        tone_power_db: Power of detected tone relative to noise floor (dB) - for discrimination
        sample_position_original: Sample position in ORIGINAL rate buffer (for RTP calculation)
        original_sample_rate: Original sample rate (Hz) for sample_position_original
        
    Metrological Fields (2026-01-24 Enhancement):
        timing_uncertainty_ms: ToA uncertainty from Cramér-Rao bound (ms)
        multipath_detected: True if multipath propagation detected
        multipath_delay_spread_ms: Delay spread from multipath analysis (ms)
        multipath_quality: Quality metric (0-1, higher = cleaner signal)
        doppler_hz: Estimated Doppler shift (Hz)
        phase_at_peak_rad: Phase at correlation peak for sub-sample refinement (rad)
    """
    station: StationType
    frequency_hz: float
    duration_sec: float
    timestamp_utc: float
    timing_error_ms: float
    snr_db: float
    confidence: float
    use_for_time_snap: bool
    correlation_peak: float
    noise_floor: float
    tone_power_db: Optional[float] = None
    sample_position_original: Optional[int] = None  # Sample position in original rate
    original_sample_rate: Optional[int] = None  # Original sample rate (e.g., 20000 Hz)
    buffer_rtp_start: Optional[int] = None  # RTP timestamp at start of detection buffer
    # Metrological fields for uncertainty propagation
    timing_uncertainty_ms: Optional[float] = None  # Cramér-Rao bound ToA uncertainty
    multipath_detected: Optional[bool] = None  # Multipath flag
    multipath_delay_spread_ms: Optional[float] = None  # Delay spread (ms)
    multipath_quality: Optional[float] = None  # 0-1, higher = cleaner
    doppler_hz: Optional[float] = None  # Doppler shift estimate
    phase_at_peak_rad: Optional[float] = None  # Phase for sub-sample refinement
    
    def is_wwv_or_chu(self) -> bool:
        """Check if this is a timing reference station (not WWVH)"""
        return self.station in [StationType.WWV, StationType.CHU]
    
    def is_wwvh(self) -> bool:
        """Check if this is WWVH (propagation study only)"""
        return self.station == StationType.WWVH
    
    def is_high_confidence(self) -> bool:
        """Check if detection meets high confidence threshold"""
        return self.confidence >= 0.8 and self.snr_db >= 15.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/storage"""
        return {
            'station': self.station.value,
            'frequency_hz': self.frequency_hz,
            'duration_sec': self.duration_sec,
            'timestamp_utc': self.timestamp_utc,
            'timing_error_ms': self.timing_error_ms,
            'snr_db': self.snr_db,
            'confidence': self.confidence,
            'use_for_time_snap': self.use_for_time_snap,
            'correlation_peak': self.correlation_peak,
            'noise_floor': self.noise_floor,
            'tone_power_db': self.tone_power_db,
            'sample_position_original': self.sample_position_original,
            'original_sample_rate': self.original_sample_rate,
            'buffer_rtp_start': self.buffer_rtp_start,
            # Metrological fields
            'timing_uncertainty_ms': self.timing_uncertainty_ms,
            'multipath_detected': self.multipath_detected,
            'multipath_delay_spread_ms': self.multipath_delay_spread_ms,
            'multipath_quality': self.multipath_quality,
            'doppler_hz': self.doppler_hz,
            'phase_at_peak_rad': self.phase_at_peak_rad,
        }


# ============================================================================
# UPLOAD (Function 6 data structures)
# ============================================================================

class UploadStatus(Enum):
    """Status of an upload task"""
    PENDING = "pending"        # Queued, not yet started
    UPLOADING = "uploading"    # Currently being uploaded
    COMPLETED = "completed"    # Successfully uploaded
    FAILED = "failed"          # Upload failed (may retry)
    CANCELLED = "cancelled"    # Manually cancelled


@dataclass
class FileMetadata:
    """
    Metadata for a file being uploaded.
    
    Attributes:
        channel_name: Channel identifier
        frequency_hz: Center frequency
        start_time: UTC timestamp of first sample
        end_time: UTC timestamp of last sample
        sample_rate: Sample rate (10 Hz for upload)
        sample_count: Total number of samples
        file_format: Format identifier ('digital_rf', 'hdf5', etc.)
        quality_summary: Summary quality metrics
        time_snap_used: Time_snap reference used for this file
    """
    channel_name: str
    frequency_hz: float
    start_time: float
    end_time: float
    sample_rate: int
    sample_count: int
    file_format: str
    quality_summary: Dict[str, Any]
    time_snap_used: Optional[Dict[str, Any]]
    
    def duration_seconds(self) -> float:
        """Duration of data in file"""
        return self.end_time - self.start_time
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'channel_name': self.channel_name,
            'frequency_hz': self.frequency_hz,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'sample_rate': self.sample_rate,
            'sample_count': self.sample_count,
            'file_format': self.file_format,
            'quality_summary': self.quality_summary,
            'time_snap_used': self.time_snap_used,
        }


@dataclass
class UploadTask:
    """
    Upload task for Function 6 (repository upload).
    
    Tracks the upload lifecycle of a single file.
    
    Attributes:
        task_id: Unique identifier for this task
        local_path: Full path to local file
        remote_path: Destination path on remote repository
        metadata: File metadata
        status: Current upload status
        created_at: When task was created (UTC timestamp)
        started_at: When upload started (UTC timestamp)
        completed_at: When upload completed (UTC timestamp)
        attempts: Number of upload attempts
        last_error: Most recent error message (if failed)
        bytes_uploaded: Number of bytes uploaded so far
        total_bytes: Total file size in bytes
    """
    task_id: str
    local_path: str
    remote_path: str
    metadata: FileMetadata
    status: UploadStatus
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempts: int = 0
    last_error: Optional[str] = None
    bytes_uploaded: int = 0
    total_bytes: int = 0
    
    def progress_pct(self) -> float:
        """Upload progress as percentage"""
        if self.total_bytes == 0:
            return 0.0
        return (self.bytes_uploaded / self.total_bytes) * 100.0
    
    def is_terminal(self) -> bool:
        """Check if task is in terminal state (completed, failed, cancelled)"""
        return self.status in [UploadStatus.COMPLETED, UploadStatus.FAILED, UploadStatus.CANCELLED]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence"""
        return {
            'task_id': self.task_id,
            'local_path': self.local_path,
            'remote_path': self.remote_path,
            'metadata': self.metadata.to_dict(),
            'status': self.status.value,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'attempts': self.attempts,
            'last_error': self.last_error,
            'bytes_uploaded': self.bytes_uploaded,
            'total_bytes': self.total_bytes,
        }
