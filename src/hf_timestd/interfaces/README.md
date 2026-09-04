# GRAPE Signal Recorder API Interfaces

This directory defines the **API contracts** between the 6 core functions of the GRAPE signal recorder.

## Purpose

These interfaces allow:
1. **Clear separation of concerns** - Each function has a well-defined input/output
2. **Independent testing** - Mock interfaces to test components in isolation
3. **Implementation flexibility** - Swap implementations without changing contracts
4. **Self-documenting code** - Type hints and data structures define the system

## Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Function 1: Quality & Time_Snap Analysis                    │
│ (RTP → Resequencing → Gap Detection → Time Calculation)     │
└───────────────────────┬─────────────────────────────────────┘
                        │ Produces: SampleBatch
                        │   - timestamp (time_snap corrected)
                        │   - samples (IQ data)
                        │   - quality (QualityInfo)
                        │   - time_snap (TimeSnapReference)
                        ├──────────┬──────────┬──────────┬─────────┐
                        ▼          ▼          ▼          ▼         ▼
                    Function 2 Function 3 Function 4 Function 5  Function 6
                    Archive    Tone Det. Decimation Digital RF Upload
```

## Core Data Models

### 1. `SampleBatch` - Primary Data Container

**Output of Function 1 → Input to Functions 2-5**

```python
@dataclass
class SampleBatch:
    timestamp: float              # UTC (time_snap corrected)
    samples: np.ndarray          # Complex IQ samples
    sample_rate: int             # Hz (typically 16000)
    quality: QualityInfo         # Quality metadata
    time_snap: Optional[TimeSnapReference]  # Time anchor
    channel_name: str            # e.g., "WWV 2.5 MHz"
    frequency_hz: float          # Center frequency
    ssrc: int                    # RTP identifier
```

**Key Features:**
- Immutable once created
- Contains all context needed by downstream functions
- Quality metadata embedded for scientific provenance

---

### 2. `QualityInfo` - Quality Metadata

**What Function 1 knows about data quality**

```python
@dataclass
class QualityInfo:
    completeness_pct: float       # 0-100
    gap_count: int                # Number of gaps
    gap_duration_ms: float        # Total gap time
    packet_loss_pct: float        # RTP packet loss
    resequenced_count: int        # Out-of-order packets
    time_snap_established: bool   # Time anchor active?
    time_snap_confidence: float   # 0.0-1.0
    discontinuities: List[Discontinuity]
    quality_grade: str            # A/B/C/D/F
    quality_score: float          # 0-100
```

**Usage:**
- Embedded in archive files (Function 2)
- Used by Digital RF metadata (Function 5)
- Drives quality monitoring dashboards

---

### 3. `TimeSnapReference` - Time Anchor

**KA9Q-radio timing architecture implementation**

```python
@dataclass(frozen=True)
class TimeSnapReference:
    rtp_timestamp: int            # RTP timestamp at anchor
    utc_timestamp: float          # UTC time at anchor
    sample_rate: int              # RTP clock rate
    source: str                   # 'wwv_first', 'wwv_verified', etc.
    confidence: float             # 0.0-1.0
    station: str                  # 'WWV', 'CHU', 'initial'
    established_at: float         # Wall clock creation time
    
    def calculate_sample_time(self, rtp_timestamp: int) -> float:
        """Convert RTP timestamp to UTC time"""
        ...
```

**Key Principle:**
> RTP timestamps are PRIMARY time reference.  
> UTC time is DERIVED from RTP + time_snap anchor.

**Established from:**
- WWV tone rising edge at :00.000
- CHU tone rising edge at :00.000
- Initial estimate (low confidence)

---

### 4. `Discontinuity` - Gap/Jump Record

**Scientific provenance of timing irregularities**

```python
@dataclass(frozen=True)
class Discontinuity:
    timestamp: float              # When it occurred (UTC)
    sample_index: int             # Where in stream
    discontinuity_type: DiscontinuityType
    magnitude_samples: int        # How big (+ = gap, - = overlap)
    magnitude_ms: float           # Time equivalent
    rtp_sequence_before: Optional[int]
    rtp_sequence_after: Optional[int]
    rtp_timestamp_before: Optional[int]
    rtp_timestamp_after: Optional[int]
    wwv_related: bool             # Related to tone detection?
    explanation: str              # Human-readable cause
```

**Types:**
- `GAP` - Missed RTP packets (filled with zeros)
- `RTP_RESET` - RTP sequence/timestamp reset
- `SYNC_ADJUST` - Time_snap correction applied
- `OVERFLOW/UNDERFLOW` - Buffer issues

**Why critical:**
Every gap must be logged for scientific data integrity.

---

### 5. `ToneDetectionResult` - WWV/WWVH Detection

**Output of Function 3 (tone discrimination)**

```python
class StationType(Enum):
    WWV = "WWV"      # Fort Collins, 1000 Hz - TIME_SNAP SOURCE
    WWVH = "WWVH"    # Hawaii, 1200 Hz - PROPAGATION STUDY ONLY
    CHU = "CHU"      # Ottawa, 1000 Hz - TIME_SNAP SOURCE

@dataclass(frozen=True)
class ToneDetectionResult:
    station: StationType          # Which station detected
    frequency_hz: float           # 1000 or 1200 Hz
    duration_sec: float           # Measured duration
    timestamp_utc: float          # Rising edge time
    timing_error_ms: float        # Error vs :00.000
    snr_db: float                 # Signal-to-noise ratio
    confidence: float             # 0.0-1.0
    use_for_time_snap: bool       # ⚠️ CRITICAL FLAG
    correlation_peak: float
    noise_floor: float
```

**Critical Distinction:**

| Station | Frequency | Duration | Purpose | `use_for_time_snap` |
|---------|-----------|----------|---------|---------------------|
| WWV     | 1000 Hz   | 0.8s     | Timing reference | ✅ `True` |
| WWVH    | 1200 Hz   | 0.8s     | Propagation study | ❌ `False` |
| CHU     | 1000 Hz   | 0.5s     | Timing reference | ✅ `True` |

**Why separate?**
- WWV/CHU: Correct time_snap for timing accuracy
- WWVH: Study ionospheric propagation differences (WWV-WWVH delay)
- Never mix purposes!

---

### 6. `UploadTask` - Upload Tracking

**For Function 6 (repository upload)**

```python
class UploadStatus(Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class UploadTask:
    task_id: str
    local_path: str               # Digital RF file
    remote_path: str              # Destination on repository
    metadata: FileMetadata        # File metadata
    status: UploadStatus
    created_at: float
    attempts: int
    bytes_uploaded: int
    total_bytes: int
```

---

## Example Usage

### Creating a SampleBatch (Function 1 output)

```python
from interfaces import SampleBatch, QualityInfo, TimeSnapReference, Discontinuity, DiscontinuityType
import numpy as np

# Quality info from analysis
quality = QualityInfo(
    completeness_pct=99.8,
    gap_count=1,
    gap_duration_ms=20.0,
    packet_loss_pct=0.2,
    resequenced_count=3,
    time_snap_established=True,
    time_snap_confidence=0.95,
    discontinuities=[
        Discontinuity(
            timestamp=1699876543.21,
            sample_index=100000,
            discontinuity_type=DiscontinuityType.GAP,
            magnitude_samples=320,
            magnitude_ms=20.0,
            rtp_sequence_before=12345,
            rtp_sequence_after=12347,
            rtp_timestamp_before=1000000,
            rtp_timestamp_after=1000640,
            wwv_related=False,
            explanation="Missed 2 RTP packets (seq 12345→12347)"
        )
    ],
    quality_grade='A',
    quality_score=98.5
)

# Time_snap reference from WWV detection
time_snap = TimeSnapReference(
    rtp_timestamp=1000000,
    utc_timestamp=1699876543.0,
    sample_rate=16000,
    source='wwv_verified',
    confidence=0.95,
    station='WWV',
    established_at=1699876543.1
)

# Create batch
batch = SampleBatch(
    timestamp=1699876543.0,
    samples=np.random.randn(16000) + 1j*np.random.randn(16000),
    sample_rate=16000,
    quality=quality,
    time_snap=time_snap,
    channel_name="WWV 5.0 MHz",
    frequency_hz=5000000.0,
    ssrc=0x12345678
)

# Downstream functions can now consume this
print(f"Batch duration: {batch.duration_seconds():.2f}s")
print(f"Quality grade: {batch.quality.quality_grade}")
print(f"Time_snap confidence: {batch.time_snap.confidence}")
```

### Processing Tone Detection (Function 3 output)

```python
from interfaces import ToneDetectionResult, StationType

# WWV detection (timing reference)
wwv_result = ToneDetectionResult(
    station=StationType.WWV,
    frequency_hz=1000.0,
    duration_sec=0.799,
    timestamp_utc=1699876800.002,
    timing_error_ms=2.0,
    snr_db=45.3,
    confidence=0.92,
    use_for_time_snap=True,  # ✅ Use for timing
    correlation_peak=0.85,
    noise_floor=-60.0
)

# WWVH detection (propagation study)
wwvh_result = ToneDetectionResult(
    station=StationType.WWVH,
    frequency_hz=1200.0,
    duration_sec=0.801,
    timestamp_utc=1699876800.015,
    timing_error_ms=15.0,
    snr_db=38.7,
    confidence=0.88,
    use_for_time_snap=False,  # ❌ Do NOT use for timing
    correlation_peak=0.78,
    noise_floor=-58.0
)

# Function 1 processes results differently:
if wwv_result.use_for_time_snap:
    # Update time_snap reference
    print(f"Updating time_snap from {wwv_result.station.value}")
    print(f"Timing error: {wwv_result.timing_error_ms:+.1f}ms")

if wwv_result.is_timing_reference() and wwvh_result.is_wwvh():
    # Calculate differential propagation delay
    differential_delay = wwv_result.timing_error_ms - wwvh_result.timing_error_ms
    print(f"WWV-WWVH differential delay: {differential_delay:+.1f}ms")
    print("(This is ionospheric path difference)")
```

---

## Benefits of This Design

1. **Type Safety**: Catch errors at development time with type hints
2. **Self-Documenting**: Field names and docstrings explain purpose
3. **Testability**: Mock `SampleBatch` to test Function 2-5 independently
4. **Clarity**: `use_for_time_snap` makes WWV/WWVH separation explicit
5. **Provenance**: Every discontinuity permanently recorded
6. **Flexibility**: Change implementations without breaking contracts

---

## Next Steps

With these data models defined, we can now create:

1. **`sample_provider.py`** - Interface for Function 1 output
2. **`archive.py`** - Interface for Function 2 (storage)
3. **`tone_detection.py`** - Interface for Function 3 (WWV/WWVH/CHU)
4. **`decimation.py`** - Interface for Function 4+5 (decimate + Digital RF)
5. **`upload.py`** - Interface for Function 6 (repository upload)

Each interface will use these data models as inputs/outputs, creating a complete API specification for the entire system.
