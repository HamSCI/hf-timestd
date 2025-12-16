# Three-Phase Robust Time-Aligned Data Pipeline - Design Document

**Version:** 1.0.0  
**Date:** December 3, 2025  
**Author:** Cascade AI / grape-recorder team

## Executive Summary

This document describes the design of a three-phase data pipeline that ensures strict, non-circular hierarchy of time sources to guarantee data integrity and enable reprocessing at any stage.

---

## 1. Problem Statement

### Current Architecture Limitations

The existing grape-recorder architecture has several limitations:

1. **Time source contamination**: Raw data timestamps may be modified based on analysis results, creating circular dependencies
2. **Event-based file splitting**: Files are split based on signal events (tone detection), making reprocessing difficult
3. **Coupled storage and analysis**: NPZ archives embed time_snap, preventing algorithm updates without re-recording
4. **Single-pass processing**: No ability to re-run analysis with improved algorithms

### Requirements

1. **Immutable raw data**: Raw IQ samples must never be modified
2. **Reprocessable analysis**: Ability to re-run timing analysis with improved algorithms
3. **Regenerable products**: Final products can be recreated from raw data + analysis
4. **UTC(NIST) accuracy**: Sub-millisecond timing accuracy through propagation modeling

---

## 2. Architecture Overview

### Three-Phase Pipeline

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         RTP Stream (radiod)                             │
│                    20 kHz IQ @ 320 samples/packet                       │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    PHASE 1: Immutable Raw Archive                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  • Format: Digital RF (DRF) with HDF5 backend                   │   │
│  │  • Sample Rate: 20 kHz (native from radiod)                     │   │
│  │  • Time Reference: System time (t_system) ONLY                  │   │
│  │  • File Splitting: Fixed duration (1 hour) or size (1 GB)       │   │
│  │  • Compression: Lossless (Shuffle + gzip/ZSTD/LZ4)              │   │
│  │  • Policy: NEVER modified based on analysis results             │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Output: /data/raw_archive/{channel}/{date}/rf_data/*.h5                │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                 PHASE 2: Analytical Engine (D_clock)                    │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  Inputs (from Phase 1):                                         │   │
│  │    • Raw 20 kHz IQ samples                                      │   │
│  │    • System time reference (t_system)                           │   │
│  │                                                                 │   │
│  │  Processing:                                                    │   │
│  │    1. Tone Detection (WWV 1000Hz, WWVH 1200Hz, CHU 1kHz)        │   │
│  │    2. WWV/WWVH Discrimination (500/600Hz, BCD, Test Signal)     │   │
│  │    3. Propagation Delay Calculation (TransmissionTimeSolver)    │   │
│  │    4. Clock Offset: D_clock = t_system - t_UTC                  │   │
│  │                                                                 │   │
│  │  Output:                                                        │   │
│  │    • Clock Offset Series (CSV/JSON) - versionable               │   │
│  │    • Per-minute D_clock with confidence and quality grade       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Output: /data/clock_offset/{channel}/clock_offset_series.csv           │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              PHASE 3: Corrected Telemetry Product                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  Inputs:                                                        │   │
│  │    • Phase 1: Raw 20 kHz IQ archive                             │   │
│  │    • Phase 2: Clock Offset Series (D_clock)                     │   │
│  │                                                                 │   │
│  │  Processing:                                                    │   │
│  │    1. Read raw samples from Phase 1                             │   │
│  │    2. Look up D_clock for timestamp range                       │   │
│  │    3. Apply correction: t_UTC = t_system - D_clock              │   │
│  │    4. Decimate 20 kHz → 10 Hz                                   │   │
│  │    5. Write to DRF with UTC(NIST) timestamps                    │   │
│  │                                                                 │   │
│  │  Output:                                                        │   │
│  │    • 10 Hz Digital RF (PSWS/wsprdaemon compatible)              │   │
│  │    • Marked as PROCESSED/ALIGNED                                │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  Output: /data/processed/{channel}/{date}/digital_rf/                   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Phase 1: Immutable Raw Archive

### 3.1 Design Principles

| Principle | Description |
|-----------|-------------|
| **Contamination Policy** | Raw 20 kHz IQ data is NEVER modified or resampled based on any subsequent analysis |
| **Time Tagging** | Only temporal reference is monotonic System Time (t_system) from radiod |
| **File Splitting** | Fixed duration (1 hour) or fixed size (1 GB), NOT signal events |
| **Immutability** | Once written, data is never modified |

### 3.2 Storage Format

**Digital RF (DRF)** was chosen because:
- Treats entire dataset as one continuous time series
- Optimized for RF data with sample-accurate indexing
- HDF5 backend provides compression and metadata
- Compatible with existing analysis tools

### 3.3 Compression Strategy

```python
# HDF5 compression pipeline
raw_data → Shuffle Filter → Compression Algorithm → Disk

# Shuffle reorganizes bytes for better compression of numeric data
# Compression options: gzip (compatible), ZSTD (fast), LZ4 (fastest)
```

### 3.4 Key Classes

```python
@dataclass
class RawArchiveConfig:
    output_dir: Path
    channel_name: str
    frequency_hz: float
    sample_rate: int = 20000
    station_config: Dict[str, Any]
    file_duration_sec: int = 3600  # 1 hour files
    compression: str = 'gzip'

class RawArchiveWriter:
    def write_samples(samples: np.ndarray, rtp_timestamp: int, system_time: float)
    def write_gap_metadata(gap_start_rtp: int, gap_samples: int, system_time: float)
    def flush()
    def close()

class RawArchiveReader:
    def read_samples(start_index: int, num_samples: int) -> Tuple[np.ndarray, int]
    def get_bounds() -> Tuple[int, int]
    def get_metadata() -> Dict
```

### 3.5 Output Structure

```
/data/raw_archive/
└── WWV_10MHz/
    ├── 20241203/
    │   ├── rf@1733222400.000.h5    # Hour 0
    │   ├── rf@1733226000.000.h5    # Hour 1
    │   └── metadata/
    │       └── raw_archive_metadata.h5
    └── metadata/
        └── session_summary.json
```

---

## 4. Phase 2: Analytical Engine

### 4.1 Clock Offset Determination

The core equation:

$$D_{clock} = t_{system} - t_{UTC}$$

Where $t_{UTC}$ is derived from the "Holy Grail" equation:

$$T_{emission} = T_{arrival} - T_{prop}$$

### 4.2 Processing Pipeline

```
Raw 20 kHz IQ
      │
      ▼
┌─────────────────┐
│ Resample to 3kHz│  (for tone detection)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Tone Detection  │  WWV (1000Hz), WWVH (1200Hz), CHU (1kHz)
│                 │  Returns: timing_error_ms, snr_db, confidence
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Discrimination  │  Identify dominant station:
│                 │  - 500/600 Hz ground truth (14 minutes/hour)
│                 │  - BCD correlation
│                 │  - 440 Hz (minutes 1, 2)
│                 │  - Test signal (minutes 8, 44)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ TransmissionTime│  Back-calculate UTC(NIST):
│ Solver          │  - Great circle distance
│                 │  - Ionospheric mode (1F, 2F, etc.)
│                 │  - Propagation delay calculation
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ D_clock Output  │  Clock Offset Series (CSV/JSON)
└─────────────────┘
```

### 4.3 TransmissionTimeSolver Physics

The solver identifies the propagation mode and calculates delay:

```python
# Propagation modes
GROUND_WAVE = "GW"   # Direct (short range only)
ONE_HOP_E = "1E"     # Single E-layer reflection
ONE_HOP_F = "1F"     # Single F-layer reflection  
TWO_HOP_F = "2F"     # Two F-layer reflections
THREE_HOP_F = "3F"   # Three F-layer reflections

# Path length calculation
path_length = 2 * n_hops * sqrt(half_hop_distance² + layer_height²)

# Total delay
D_prop = (path_length / c) + ionospheric_delay
```

### 4.4 Quality Grades

| Grade | Uncertainty | Criteria |
|-------|-------------|----------|
| **A** (Excellent) | < 0.5 ms | High confidence, multi-station verified |
| **B** (Good) | 0.5-1.5 ms | High confidence, single station |
| **C** (Fair) | 1.5-3.0 ms | Moderate confidence |
| **D** (Poor) | > 3.0 ms | Low confidence |
| **X** (Invalid) | N/A | No valid measurement |

### 4.5 Key Classes

```python
@dataclass
class ClockOffsetMeasurement:
    system_time: float
    utc_time: float
    clock_offset_ms: float
    station: str  # 'WWV', 'WWVH', 'CHU'
    propagation_delay_ms: float
    propagation_mode: str  # '1F', '2F', etc.
    confidence: float
    uncertainty_ms: float
    quality_grade: ClockOffsetQuality
    snr_db: float
    utc_verified: bool

class ClockOffsetEngine:
    def process_minute(iq_samples, system_time, rtp_timestamp) -> ClockOffsetMeasurement
    def save_series()

class ClockOffsetSeries:
    def add_measurement(measurement)
    def get_offset_at_time(target_time, interpolate=True) -> Tuple[offset_ms, uncertainty_ms]
```

### 4.6 Output Format

**CSV (streaming):**
```csv
system_time,utc_time,clock_offset_ms,station,propagation_mode,confidence,quality_grade
1733222400.0,1733222399.985,15.23,WWV,1F,0.92,A
1733222460.0,1733222459.987,12.87,WWVH,2F,0.88,B
```

**JSON (complete series):**
```json
{
  "channel_name": "WWV_10MHz",
  "frequency_hz": 10000000.0,
  "receiver_grid": "EM38ww",
  "version": "1.0.0",
  "measurements": [
    {
      "system_time": 1733222400.0,
      "clock_offset_ms": 15.23,
      "station": "WWV",
      "propagation_mode": "1F",
      "confidence": 0.92,
      "quality_grade": "A"
    }
  ]
}
```

---

## 5. Phase 3: Corrected Telemetry Product

### 5.1 Processing Flow

```python
# For each minute of raw data:
raw_samples = phase1_reader.read_samples(start_index, samples_per_minute)
offset_ms, uncertainty = phase2_series.get_offset_at_time(system_time)

# Apply correction
utc_timestamp = system_time - (offset_ms / 1000.0)

# Decimate and write
decimated = decimate_20k_to_10hz(raw_samples)
phase3_writer.write(decimated, utc_timestamp)
```

### 5.2 Key Classes

```python
@dataclass
class ProductConfig:
    raw_archive_dir: Path      # Phase 1 input
    clock_offset_dir: Path     # Phase 2 input
    output_dir: Path           # Phase 3 output
    input_sample_rate: int = 20000
    output_sample_rate: int = 10

class CorrectedProductGenerator:
    def process_minute(raw_samples, system_time) -> int
    def process_range(start_time, end_time) -> Dict

class StreamingProductGenerator(CorrectedProductGenerator):
    def add_raw_minute(system_time, raw_samples)  # Real-time processing
```

### 5.3 Output Structure

```
/data/processed/
└── WWV_10MHz/
    └── 20241203/
        └── W3PM_EM38ww/
            └── GRAPE@W3PM_1/
                └── OBS2024-12-03T00-00/
                    └── WWV_10MHz/
                        ├── rf@1733222400.000.h5
                        └── metadata/
                            └── metadata.h5
```

### 5.4 Metadata Tags

Phase 3 products include provenance metadata:

```json
{
  "product_type": "corrected_10hz_iq",
  "phase": "phase3_aligned",
  "time_reference": "utc_nist_corrected",
  "clock_offset_applied": true,
  "source_archive_type": "raw_20khz_iq",
  "decimation_factor": 2000
}
```

---

## 6. Pipeline Orchestrator

### 6.1 Real-Time Coordination

```python
class PipelineOrchestrator:
    def __init__(config: PipelineConfig):
        self.phase1_writer = RawArchiveWriter(...)   # Immediate write
        self.phase2_engine = ClockOffsetEngine(...)  # Minute-aligned
        self.phase3_generator = StreamingProductGenerator(...)  # Delayed
    
    def process_samples(samples, rtp_timestamp, system_time):
        # Phase 1: Write immediately (system time only)
        self.phase1_writer.write_samples(samples, rtp_timestamp, system_time)
        
        # Accumulate for minute-aligned processing
        self._accumulate_minute(samples, rtp_timestamp, system_time)
    
    def _analysis_loop():
        # Background thread for Phase 2 + Phase 3
        while running:
            system_time, rtp, samples = analysis_queue.get()
            
            # Phase 2: Generate D_clock
            measurement = self.phase2_engine.process_minute(samples, system_time, rtp)
            
            # Phase 3: Generate product (with latency for D_clock availability)
            self.phase3_generator.add_raw_minute(system_time, samples)
```

### 6.2 Latency Considerations

| Phase | Latency | Reason |
|-------|---------|--------|
| Phase 1 | Immediate | Raw data written as received |
| Phase 2 | ~60s | Requires complete minute for analysis |
| Phase 3 | ~120s | Requires D_clock from Phase 2 |

---

## 7. Batch Reprocessing

### 7.1 Use Cases

1. **Algorithm improvement**: Re-run Phase 2 with better tone detection
2. **Bug fix**: Correct propagation model errors
3. **New discrimination method**: Add new station identification technique

### 7.2 Reprocessing Workflow

```python
reprocessor = BatchReprocessor(data_dir, channel_name, ...)

# Re-run Phase 2 with new algorithm (v2)
results = reprocessor.reprocess_phase2(
    start_time=start,
    end_time=end,
    output_version="v2"
)

# Regenerate Phase 3 using new D_clock
results = reprocessor.reprocess_phase3(
    start_time=start,
    end_time=end,
    clock_offset_version="v2",  # Use new analysis
    output_version="v2"
)
```

### 7.3 Version Management

```
/data/clock_offset/WWV_10MHz/
├── v1/                        # Original analysis
│   └── clock_offset_series.csv
├── v2/                        # Improved algorithm
│   └── clock_offset_series.csv
└── v3/                        # Bug fix
    └── clock_offset_series.csv

/data/processed/WWV_10MHz/
├── v1/                        # Original products
├── v2/                        # From v2 D_clock
└── v3/                        # From v3 D_clock
```

---

## 8. Module Summary

### 8.1 New Files

| File | Lines | Purpose |
|------|-------|---------|
| `raw_archive_writer.py` | ~500 | Phase 1: Immutable 20 kHz DRF archive |
| `clock_offset_series.py` | ~600 | Phase 2: D_clock analytical engine |
| `corrected_product_generator.py` | ~450 | Phase 3: Corrected 10 Hz products |
| `pipeline_orchestrator.py` | ~500 | Three-phase coordination |
| `pipeline_recorder.py` | ~350 | Drop-in GrapeRecorder replacement |

### 8.2 Key Exports

```python
# Phase 1
from grape_recorder.grape import RawArchiveWriter, RawArchiveConfig, create_raw_archive_writer

# Phase 2
from grape_recorder.grape import ClockOffsetEngine, ClockOffsetSeries, ClockOffsetMeasurement

# Phase 3
from grape_recorder.grape import CorrectedProductGenerator, StreamingProductGenerator

# Pipeline
from grape_recorder.grape import PipelineOrchestrator, create_pipeline, BatchReprocessor

# Recorder
from grape_recorder.grape import PipelineRecorder, create_pipeline_recorder
```

---

## 9. Backward Compatibility

The legacy system remains fully functional:

| Legacy Component | Status | Migration Path |
|------------------|--------|----------------|
| `GrapeRecorder` | ✅ Unchanged | Optional switch to `PipelineRecorder` |
| `GrapeNPZWriter` | ✅ Unchanged | Phase 1 replaces NPZ storage |
| `AnalyticsService` | ✅ Unchanged | Phase 2 replaces analysis |
| `DRFWriterService` | ✅ Unchanged | Phase 3 replaces DRF generation |

---

## 10. Testing Strategy

### 10.1 Unit Tests

- [ ] `RawArchiveWriter`: Write/read cycle, compression
- [ ] `ClockOffsetEngine`: Tone detection → D_clock calculation
- [ ] `CorrectedProductGenerator`: Time correction, decimation
- [ ] `PipelineOrchestrator`: Full pipeline integration

### 10.2 Integration Tests

- [ ] End-to-end: RTP → Phase 1 → Phase 2 → Phase 3
- [ ] Reprocessing: Phase 2 v1 → v2, Phase 3 regeneration
- [ ] Streaming: Real-time latency verification

### 10.3 Validation Tests

- [ ] D_clock accuracy vs known UTC source
- [ ] Phase 3 timestamp accuracy
- [ ] PSWS/wsprdaemon compatibility

---

## 11. Future Enhancements

1. **Multi-channel coordination**: Cross-channel D_clock averaging
2. **Adaptive compression**: Dynamic algorithm selection based on data
3. **Cloud sync**: Optional upload of Phase 2 series for collaborative analysis
4. **Real-time visualization**: D_clock trending dashboard

---

## Appendix A: Mathematical Derivations

### A.1 Propagation Delay

For N-hop ionospheric propagation:

$$T_{prop} = \frac{d_{path}}{c} + \sum_{i=1}^{N} \tau_{iono,i}$$

Where:
- $d_{path} = 2N\sqrt{(\frac{d_{ground}}{2N})^2 + h_{layer}^2}$
- $\tau_{iono} \approx 0.15 \cdot f_{factor}$ ms per hop
- $f_{factor}$ is frequency-dependent (higher freq = less delay)

### A.2 Clock Offset

$$D_{clock} = D_{total} - D_{prop}$$
$$D_{total} = t_{RTP} - t_{UTC\_expected}$$

Where $t_{UTC\_expected}$ is the exact second boundary when WWV/WWVH transmits.

---

## Appendix B: Configuration Reference

```toml
[pipeline]
# Phase 1
raw_archive_compression = "gzip"  # gzip, zstd, lz4, none
raw_archive_file_duration_sec = 3600

# Phase 2
analysis_latency_sec = 120
enable_multi_station = true

# Phase 3
output_sample_rate = 10
streaming_latency_minutes = 2
```

---

*End of Design Document*
