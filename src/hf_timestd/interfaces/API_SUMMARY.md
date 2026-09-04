# GRAPE Signal Recorder - Complete API Specification

**Status:** ✅ Complete interface definitions for all 6 core functions  
**Location:** `/src/signal_recorder/interfaces/`  
**Purpose:** Define contracts without implementation details

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────┐
│ Function 1: QualityAnalyzedSampleProvider                      │
│ (RTP → Resequencing → Time_Snap → Quality Analysis)            │
└──────────────┬─────────────────────────────────────────────────┘
               │ Produces: SampleBatch
               │   • timestamp (time_snap corrected)
               │   • samples (IQ data)
               │   • quality (QualityInfo)
               │   • time_snap (TimeSnapReference)
               ├─────────────┬───────────┬───────────┬───────────┐
               ▼             ▼           ▼           ▼           ▼
          Function 2    Function 3  Function 4  Function 5  Function 6
          ArchiveWriter ToneDetector           DecimatorWriter
          (16 kHz NPZ)  (WWV/WWVH)   (Decimation) (Digital RF) UploadQueue
                                            └──────┘
                                         Combined Interface
```

**Key Principle:** Function 1 is the sole producer. Functions 2-6 are independent consumers that don't interact with each other.

---

## Quick Reference

| Function | Interface | Input | Output |
|----------|-----------|-------|--------|
| 1 | `QualityAnalyzedSampleProvider` | RTP packets | `SampleBatch` |
| 2 | `ArchiveWriter` | `SampleBatch` | NPZ files |
| 3 | `ToneDetector` | `SampleBatch` | `ToneDetectionResult` |
| 4+5 | `DecimatorWriter` | `SampleBatch` | Digital RF files |
| 6 | `UploadQueue` | Digital RF path + metadata | `UploadTask` |

---

## Data Models (Shared Structures)

### Core Data Flow

#### `SampleBatch` - Primary Container
**Function 1 output → Functions 2-5 input**

```python
@dataclass
class SampleBatch:
    timestamp: float              # UTC (time_snap corrected)
    samples: np.ndarray          # Complex IQ
    sample_rate: int             # Hz (16000)
    quality: QualityInfo         # Quality metadata
    time_snap: Optional[TimeSnapReference]
    channel_name: str
    frequency_hz: float
    ssrc: int
```

**Methods:**
- `__len__()` - Number of samples
- `duration_seconds()` - Duration in seconds
- `end_timestamp()` - End time

---

#### `QualityInfo` - Quality Metadata
**Produced by Function 1, consumed by all**

```python
@dataclass
class QualityInfo:
    completeness_pct: float       # 0-100
    gap_count: int
    gap_duration_ms: float
    packet_loss_pct: float
    resequenced_count: int
    time_snap_established: bool
    time_snap_confidence: float   # 0.0-1.0
    discontinuities: List[Discontinuity]
    quality_grade: str            # A/B/C/D/F
    quality_score: float          # 0-100
```

**Methods:**
- `is_high_quality()` - Grade A or B?
- `has_gaps()` - Any gaps present?
- `to_dict()` - JSON serialization

---

#### `TimeSnapReference` - Time Anchor
**KA9Q timing architecture implementation**

```python
@dataclass(frozen=True)
class TimeSnapReference:
    rtp_timestamp: int            # RTP at anchor
    utc_timestamp: float          # UTC at anchor
    sample_rate: int              # RTP clock rate
    source: str                   # 'wwv_first', 'wwv_verified', etc.
    confidence: float             # 0.0-1.0
    station: str                  # 'WWV', 'CHU', 'initial'
    established_at: float         # Wall clock creation
```

**Methods:**
- `calculate_sample_time(rtp_timestamp)` - Convert RTP to UTC

**Formula:**
```python
utc_time = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
```

---

#### `Discontinuity` - Gap/Jump Record
**Scientific provenance of timing irregularities**

```python
@dataclass(frozen=True)
class Discontinuity:
    timestamp: float
    sample_index: int
    discontinuity_type: DiscontinuityType  # GAP, RTP_RESET, SYNC_ADJUST, etc.
    magnitude_samples: int        # +gap, -overlap
    magnitude_ms: float
    rtp_sequence_before: Optional[int]
    rtp_sequence_after: Optional[int]
    rtp_timestamp_before: Optional[int]
    rtp_timestamp_after: Optional[int]
    wwv_related: bool
    explanation: str
```

---

#### `ToneDetectionResult` - WWV/WWVH Detection
**Function 3 output with critical purpose separation**

```python
@dataclass(frozen=True)
class ToneDetectionResult:
    station: StationType          # WWV, WWVH, CHU
    frequency_hz: float           # 1000 or 1200
    duration_sec: float
    timestamp_utc: float
    timing_error_ms: float        # Error vs :00.000
    snr_db: float
    confidence: float             # 0.0-1.0
    use_for_time_snap: bool       # ⚠️ CRITICAL FLAG
    correlation_peak: float
    noise_floor: float
```

**StationType Enum:**
```python
class StationType(Enum):
    WWV = "WWV"      # Fort Collins, 1000 Hz - TIME_SNAP
    WWVH = "WWVH"    # Hawaii, 1200 Hz - PROPAGATION ONLY
    CHU = "CHU"      # Ottawa, 1000 Hz - TIME_SNAP
```

**Critical distinction:**

| Station | `use_for_time_snap` | Purpose |
|---------|---------------------|---------|
| WWV     | ✅ `True` | Time correction |
| WWVH    | ❌ `False` | Propagation study |
| CHU     | ✅ `True` | Time correction |

---

## API Interfaces

### Function 1: `QualityAnalyzedSampleProvider`

**Role:** Producer of quality-analyzed sample stream

**Key Methods:**

```python
class QualityAnalyzedSampleProvider(ABC):
    @abstractmethod
    def get_sample_batch(self) -> Optional[SampleBatch]:
        """Get next batch of samples (blocking)"""
        
    @abstractmethod
    def get_time_snap_reference(self) -> Optional[TimeSnapReference]:
        """Get current time anchor"""
        
    @abstractmethod
    def get_discontinuities(self, since_timestamp: float) -> List[Discontinuity]:
        """Get gaps/jumps since timestamp"""
        
    @abstractmethod
    def get_quality_summary(self) -> QualityInfo:
        """Get current quality statistics"""
        
    @abstractmethod
    def register_time_snap_update_callback(self, callback) -> None:
        """Notify when time_snap changes"""
        
    @abstractmethod
    def get_channel_info(self) -> dict:
        """Get channel configuration"""
```

**Usage:**
```python
# Consumer (Function 2-5) gets samples
batch = provider.get_sample_batch()
if batch:
    # Process samples with quality context
    archive.write_samples(
        batch.timestamp,
        batch.samples,
        batch.quality,
        batch.time_snap
    )
```

---

### Function 2: `ArchiveWriter`

**Role:** Store full-bandwidth compressed data

**Key Methods:**

```python
class ArchiveWriter(ABC):
    @abstractmethod
    def write_samples(
        self,
        timestamp: float,
        samples: np.ndarray,
        quality: QualityInfo,
        time_snap: Optional[TimeSnapReference] = None
    ) -> Optional[Path]:
        """Write samples, return path when minute complete"""
        
    @abstractmethod
    def add_discontinuity(self, discontinuity: Discontinuity) -> None:
        """Add gap record to current minute"""
        
    @abstractmethod
    def flush(self) -> Optional[Path]:
        """Flush incomplete minute (shutdown)"""
        
    @abstractmethod
    def verify_file_integrity(self, file_path: Path) -> bool:
        """Verify written file"""
```

**Companion: `ArchiveReader`**
```python
class ArchiveReader(ABC):
    @abstractmethod
    def read_minute(self, file_path: Path) -> Tuple[np.ndarray, dict]:
        """Read single minute file"""
        
    @abstractmethod
    def read_time_range(
        self, start_time: float, end_time: float, channel_name: str
    ) -> Tuple[np.ndarray, List[dict]]:
        """Read across multiple minutes"""
```

---

### Function 3: `ToneDetector`

**Role:** Discriminate WWV/WWVH tones

**Key Methods:**

```python
class ToneDetector(ABC):
    @abstractmethod
    def process_samples(
        self,
        timestamp: float,
        samples: np.ndarray,
        rtp_timestamp: Optional[int] = None
    ) -> Optional[List[ToneDetectionResult]]:
        """Detect tones, return list (may have WWV + WWVH)"""
        
    @abstractmethod
    def get_differential_delay(self) -> Optional[float]:
        """WWV-WWVH propagation delay (ms)"""
        
    @abstractmethod
    def set_detection_threshold(self, threshold: float) -> None:
        """Tune sensitivity"""
```

**Extended: `MultiStationToneDetector`**
```python
class MultiStationToneDetector(ToneDetector):
    @abstractmethod
    def get_detections_by_station(
        self, station: StationType
    ) -> List[ToneDetectionResult]:
        """Get recent detections for specific station"""
        
    @abstractmethod
    def get_differential_delay_history(
        self, count: int = 10
    ) -> List[Dict[str, float]]:
        """Time series of WWV-WWVH delays"""
```

**Critical usage:**
```python
results = detector.process_samples(timestamp, samples)
if results:
    for detection in results:
        if detection.use_for_time_snap:
            # Update time_snap (WWV or CHU)
            update_time_snap(detection)
        else:
            # Propagation analysis (WWVH)
            analyze_propagation(detection)
```

---

### Functions 4+5: `DecimatorWriter`

**Role:** Decimate and write Digital RF (combined interface)

**Key Methods:**

```python
class DecimatorWriter(ABC):
    @abstractmethod
    def write_decimated(
        self,
        timestamp: float,
        samples: np.ndarray,
        quality: QualityInfo,
        time_snap: Optional[TimeSnapReference] = None
    ) -> Optional[Path]:
        """Decimate 16kHz→10Hz and write Digital RF"""
        
    @abstractmethod
    def get_decimation_factor(self) -> int:
        """Get input_rate / output_rate"""
        
    @abstractmethod
    def embed_quality_metadata(
        self, quality_summary: Dict, discontinuities: list
    ) -> None:
        """Embed quality in Digital RF metadata channel"""
        
    @abstractmethod
    def flush(self) -> Optional[Path]:
        """Finalize current file"""
        
    @abstractmethod
    def verify_digital_rf_integrity(self, file_path: Path) -> bool:
        """Verify Digital RF file"""
```

**Companion: `DigitalRFReader`**
```python
class DigitalRFReader(ABC):
    @abstractmethod
    def read_time_range(
        self, start_time: float, end_time: float, channel_name: str
    ) -> np.ndarray:
        """Read decimated 10 Hz samples"""
```

**Why combined?**
- Decimation and Digital RF writing are always coupled
- Filter state must persist across writes
- Avoids unnecessary intermediate API

---

### Function 6: `UploadQueue`

**Role:** Manage upload to remote repository

**Key Methods:**

```python
class UploadQueue(ABC):
    @abstractmethod
    def queue_file(
        self,
        local_path: Path,
        metadata: FileMetadata,
        priority: int = 0
    ) -> str:
        """Queue file, return task_id"""
        
    @abstractmethod
    def get_task_status(self, task_id: str) -> Optional[UploadTask]:
        """Check upload progress"""
        
    @abstractmethod
    def get_pending_count(self) -> int:
        """Files waiting to upload"""
        
    @abstractmethod
    def retry_failed_tasks(self) -> int:
        """Retry all failed uploads"""
        
    @abstractmethod
    def set_bandwidth_limit(self, kbps: int) -> None:
        """Limit upload rate"""
        
    @abstractmethod
    def register_completion_callback(
        self, callback: Callable[[str, bool], None]
    ) -> None:
        """Notify on completion"""
```

**Companion: `UploadProtocol`**
```python
class UploadProtocol(ABC):
    @abstractmethod
    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        progress_callback: Optional[Callable] = None
    ) -> bool:
        """Upload via specific protocol (rsync, sftp, etc.)"""
```

**Usage:**
```python
# Function 5 queues completed file
completed_file = writer.write_decimated(...)
if completed_file:
    task_id = upload_queue.queue_file(
        local_path=completed_file,
        metadata=FileMetadata(
            channel_name="WWV 5.0 MHz",
            frequency_hz=5e6,
            start_time=...,
            end_time=...,
            sample_rate=10,
            sample_count=36000,
            file_format='digital_rf',
            quality_summary={'completeness': 99.8},
            time_snap_used=time_snap.to_dict()
        )
    )
```

---

## Benefits of This API Design

### 1. **Clear Separation of Concerns**
Each function has well-defined inputs/outputs. No cross-cutting dependencies.

### 2. **Testability**
Mock `QualityAnalyzedSampleProvider` to test Functions 2-5 independently:
```python
class MockSampleProvider(QualityAnalyzedSampleProvider):
    def get_sample_batch(self):
        return SampleBatch(
            timestamp=1699876543.0,
            samples=np.random.randn(16000) + 1j*np.random.randn(16000),
            sample_rate=16000,
            quality=mock_quality_info(),
            time_snap=mock_time_snap(),
            channel_name="Test",
            frequency_hz=5e6,
            ssrc=0x12345678
        )

# Test Function 2 independently
archive = MyArchiveWriter(...)
batch = mock_provider.get_sample_batch()
archive.write_samples(batch.timestamp, batch.samples, batch.quality)
```

### 3. **Implementation Flexibility**
Swap implementations without changing consumers:
- Archive: NPZ → HDF5 → Custom format
- Upload: rsync → SFTP → S3
- Tone detection: Matched filter → ML model

### 4. **WWV/WWVH Separation Made Explicit**
The `use_for_time_snap` flag in `ToneDetectionResult` prevents mixing timing correction with propagation analysis.

### 5. **Scientific Provenance**
Every gap, correction, and quality metric is explicitly modeled and passed through the API.

### 6. **Documentation as Code**
Interface definitions serve as definitive specifications. No ambiguity about contracts.

---

## Integration with Current Code

### Current Implementation Status

| Function | Interface | Implementation | Status |
|----------|-----------|----------------|--------|
| 1 | `QualityAnalyzedSampleProvider` | `GRAPEChannelRecorderV2` | ✅ Exists, needs interface wrapper |
| 2 | `ArchiveWriter` | `MinuteFileWriter` | ✅ Exists, needs interface wrapper |
| 3 | `ToneDetector` | `MultiStationToneDetector` (in recorder) | ✅ Exists, needs extraction |
| 4+5 | `DecimatorWriter` | `DigitalRFWriter` | ✅ Exists, needs interface wrapper |
| 6 | `UploadQueue` | `UploadManager` | ⚠️ Exists but NOT integrated |

### Next Steps

1. **Create interface adapters** for existing implementations
2. **Integrate UploadManager** with `DecimatorWriter` output
3. **Extract tone detector** to standalone component
4. **Add unit tests** using interface mocks
5. **Refactor gradually** - old code continues working during transition

---

## File Structure

```
src/signal_recorder/interfaces/
├── __init__.py               # Package exports
├── README.md                 # User guide with examples
├── API_SUMMARY.md            # This document
├── data_models.py            # Shared data structures (450 lines)
├── sample_provider.py        # Function 1 interface
├── archive.py                # Function 2 interface
├── tone_detection.py         # Function 3 interface
├── decimation.py             # Functions 4+5 interface
└── upload.py                 # Function 6 interface
```

**Total:** 2000+ lines of interface definitions, fully documented

---

## Example: Complete Data Flow

```python
# Function 1: Produce samples
provider = GRAPEChannelRecorderV2Adapter(...)
batch = provider.get_sample_batch()

# Function 2: Archive
archive = MinuteFileWriterAdapter(...)
completed_archive = archive.write_samples(
    batch.timestamp, batch.samples, batch.quality, batch.time_snap
)

# Function 3: Tone detection
detector = MultiStationToneDetectorAdapter(...)
detections = detector.process_samples(batch.timestamp, batch.samples)
if detections:
    for det in detections:
        if det.use_for_time_snap:
            provider.update_time_snap(det)  # Feedback to Function 1

# Functions 4+5: Decimate and Digital RF
drf_writer = DigitalRFWriterAdapter(...)
completed_drf = drf_writer.write_decimated(
    batch.timestamp, batch.samples, batch.quality, batch.time_snap
)

# Function 6: Upload
if completed_drf:
    upload_queue = UploadManagerAdapter(...)
    task_id = upload_queue.queue_file(
        local_path=completed_drf,
        metadata=drf_writer.get_current_file_metadata()
    )
```

---

## Conclusion

✅ **Complete API specification for all 6 core functions**  
✅ **WWV/WWVH purpose separation clearly defined**  
✅ **Scientific provenance built into data models**  
✅ **Current implementations map cleanly to interfaces**  
✅ **Ready for implementation, testing, and documentation**

**This API design provides a solid foundation for:**
- Clear communication between team members
- Independent testing of components
- Flexible implementation evolution
- Comprehensive documentation
- Scientific data integrity
