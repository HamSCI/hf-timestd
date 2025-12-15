# Three-Phase Robust Time-Aligned Data Pipeline

## Overview

The grape-recorder implements a strict, non-circular hierarchy of time sources to guarantee data integrity and enable reprocessing of results at any stage.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         RTP Stream (radiod)                             │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│            PHASE 1: Immutable Raw Archive (20 kHz IQ DRF)               │
│  • System time tagging ONLY (t_system)                                  │
│  • Fixed-duration file splitting (1 hour)                               │
│  • Lossless compression (Shuffle + ZSTD/gzip)                           │
│  • NEVER modified based on analysis                                     │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│            PHASE 2: Analytical Engine (D_clock Series)                  │
│  • Tone detection (WWV/WWVH/CHU)                                        │
│  • WWV/WWVH discrimination                                              │
│  • Propagation delay calculation                                        │
│  • D_clock = t_system - t_UTC                                           │
│  • Output: Separate versionable CSV/JSON                                │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│         PHASE 3: Corrected Telemetry Product (10 Hz DRF)                │
│  • Reads Phase 1 raw archive                                            │
│  • Applies D_clock from Phase 2                                         │
│  • Decimates 20 kHz → 10 Hz                                             │
│  • UTC(NIST) aligned timestamps                                         │
│  • Output: PROCESSED/ALIGNED DRF for upload                             │
└─────────────────────────────────────────────────────────────────────────┘
```

## Phase 1: Immutable Raw Archive

### Design Principles

1. **Contamination Policy**: The raw 20 kHz IQ data must **NEVER** be modified or resampled based on any subsequent analysis (e.g., tone detection, discrimination).

2. **Time Tagging**: The only temporal reference is the monotonic **System Time** (`t_system`) provided by the radiod wall clock or derived from the initial sample index.

3. **File Splitting**: Files are split based on **FIXED DURATION** (1 hour) or **FIXED SIZE** (1 GB), **NOT** based on signal events (like tone detection).

### Storage Format

- **Format**: Digital RF (DRF) - treats the entire dataset as one continuous time series
- **Compression**: Lossless using HDF5 with:
  - Shuffle filter (reorganizes bytes for better compression)
  - ZSTD/LZ4/gzip for actual compression

### Key Classes

```python
from grape_recorder.grape import RawArchiveWriter, RawArchiveConfig

config = RawArchiveConfig(
    output_dir=Path('/data/grape'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    sample_rate=20000,
    station_config={'callsign': 'W3PM', 'grid_square': 'EM38ww'},
    compression='gzip',
    file_duration_sec=3600  # 1 hour files
)

writer = RawArchiveWriter(config)
writer.write_samples(iq_samples, rtp_timestamp, system_time)
```

### Output Structure

```
/data/grape/raw_archive/WWV_10MHz/
├── 20241209/                    # Daily subdirectory
│   ├── rf_data/                 # DRF data files
│   └── metadata/                # DRF metadata
│       └── raw_archive_metadata.h5
└── metadata/
    └── session_summary.json     # Session provenance
```

## Phase 2: Analytical Engine (Clock Offset Series)

### Design Principles

1. **Read-Only Access**: Phase 2 reads from Phase 1 but **NEVER** writes back to it
2. **Versionable Output**: D_clock series are stored in separate, versioned files
3. **Reprocessable**: Can be re-run with improved algorithms without affecting Phase 1

### Clock Offset Determination

The analysis reads raw 20 kHz DRF and detects the precise `t_RTP` of synchronization tones (WWV, WWVH, CHU).

**Key Equations:**

1. **Total Delay**:
   ```
   D_total = t_RTP - t_UTC_expected
   ```

2. **Clock Offset** (after propagation correction):
   ```
   D_clock = D_total - D_prop
   ```

Where `D_prop` is the propagation delay calculated by the `TransmissionTimeSolver`.

### WWV/WWVH Discrimination

Discrimination is a **required input** for accurate timing:

- Uses 500/600 Hz ground truth minutes (14 per hour)
- BCD correlation for station identification
- Test signal analysis (minutes 8, 44)
- 440 Hz tone detection (minutes 1, 2)

The discrimination result determines the correct propagation delay calculation.

### TransmissionTimeSolver

The "Holy Grail" calculation that turns a passive receiver into a primary time standard:

```python
T_emission = T_arrival - T_prop
```

Where:
- `T_emission`: Time the signal was transmitted at WWV/WWVH
- `T_arrival`: Time the signal was detected (t_RTP)
- `T_prop`: Propagation delay (geometric + ionospheric)

### Key Classes

```python
from grape_recorder.grape import ClockOffsetEngine, create_clock_offset_engine

engine = create_clock_offset_engine(
    raw_archive_dir=Path('/data/grape/raw_archive'),
    output_dir=Path('/data/grape/clock_offset'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    receiver_grid='EM38ww'
)

# Process a minute of data
measurement = engine.process_minute(
    iq_samples=minute_samples,
    system_time=system_time,
    rtp_timestamp=rtp_timestamp
)

# D_clock result
print(f"Clock offset: {measurement.clock_offset_ms:+.2f} ms")
print(f"Confidence: {measurement.confidence:.2%}")
print(f"Quality: {measurement.quality_grade.value}")
```

### Output Format

**CSV (streaming):**
```
clock_offset_series.csv:
system_time,utc_time,clock_offset_ms,station,confidence,quality_grade,...
1733760000.0,1733759999.985,15.23,WWV,0.92,A
1733760060.0,1733760059.987,12.87,WWVH,0.88,B
```

**JSON (complete series):**
```json
{
  "channel_name": "WWV_10MHz",
  "frequency_hz": 10000000.0,
  "receiver_grid": "EM38ww",
  "measurements": [
    {
      "system_time": 1733760000.0,
      "utc_time": 1733759999.985,
      "clock_offset_ms": 15.23,
      "station": "WWV",
      "propagation_delay_ms": 14.52,
      "propagation_mode": "1F",
      "confidence": 0.92,
      "quality_grade": "A"
    }
  ]
}
```

## Phase 3: Corrected Telemetry Product

### Design Principles

1. **Consumes Phase 1 + Phase 2**: Never modifies source data
2. **Applies D_clock Correction**: 
   ```
   t_UTC(NIST) = t_system - D_clock
   ```
3. **Decimation**: 20 kHz → 10 Hz
4. **Separate Output**: Marked as PROCESSED/ALIGNED

### Key Classes

```python
from grape_recorder.grape import (
    CorrectedProductGenerator,
    StreamingProductGenerator,
    ProductConfig
)

config = ProductConfig(
    raw_archive_dir=Path('/data/grape/raw_archive'),
    clock_offset_dir=Path('/data/grape/clock_offset'),
    output_dir=Path('/data/grape/processed'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    station_config={'callsign': 'W3PM', 'grid_square': 'EM38ww'}
)

# Batch processing
generator = CorrectedProductGenerator(config)
results = generator.process_range(start_time, end_time)

# Streaming (real-time)
streaming = StreamingProductGenerator(config, latency_minutes=2)
streaming.add_raw_minute(system_time, raw_samples)
```

### Output Structure

```
/data/grape/processed/WWV_10MHz/
├── 20241209/
│   └── W3PM_EM38ww/
│       └── GRAPE@STATIONID_1/
│           └── OBS2024-12-09T00-00/
│               └── WWV_10MHz/
│                   ├── rf_data/          # 10 Hz DRF
│                   └── metadata/
└── session_summary.json
```

### Metadata Tags

Each Phase 3 product includes metadata indicating:
- `product_type: "corrected_10hz_iq"`
- `phase: "phase3_aligned"`
- `time_reference: "utc_nist_corrected"`
- `clock_offset_applied: true`

## Pipeline Orchestrator

For real-time operation, the `PipelineOrchestrator` coordinates all three phases:

```python
from grape_recorder.grape import create_pipeline

orchestrator = create_pipeline(
    data_dir=Path('/data/grape'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    receiver_grid='EM38ww',
    station_config={
        'callsign': 'W3PM',
        'grid_square': 'EM38ww',
        'psws_station_id': 'W3PM',
        'psws_instrument_id': '1'
    }
)

# Start processing
orchestrator.start()

# Feed RTP packets (from ka9q-radio)
orchestrator.process_samples(iq_samples, rtp_timestamp, system_time)

# Check status
status = orchestrator.get_stats()
print(f"Archived: {status['samples_archived']} samples")
print(f"Analyzed: {status['minutes_analyzed']} minutes")
print(f"Products: {status['products_generated']} files")

# Stop gracefully
orchestrator.stop()
```

## Batch Reprocessing

A key advantage of this architecture is the ability to reprocess without data loss:

```python
from grape_recorder.grape import BatchReprocessor

reprocessor = BatchReprocessor(
    data_dir=Path('/data/grape'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    receiver_grid='EM38ww',
    station_config={'callsign': 'W3PM'}
)

# Re-run Phase 2 with improved algorithm
results = reprocessor.reprocess_phase2(
    start_time=start,
    end_time=end,
    output_version="v2"  # New version
)

# Re-generate Phase 3 products using new D_clock
results = reprocessor.reprocess_phase3(
    start_time=start,
    end_time=end,
    clock_offset_version="v2",  # Use new analysis
    output_version="v2"
)
```

## Integration with Existing Modules

### TransmissionTimeSolver Integration

The Phase 2 engine uses the existing `TransmissionTimeSolver` for UTC back-calculation:

```python
from grape_recorder.grape import create_solver_from_grid

solver = create_solver_from_grid('EM38ww', sample_rate=20000)

result = solver.solve(
    station='WWV',
    frequency_mhz=10.0,
    arrival_rtp=arrival_timestamp,
    delay_spread_ms=0.5,
    doppler_std_hz=0.1,
    fss_db=-2.0,
    expected_second_rtp=second_boundary_rtp
)

print(f"Mode: {result.mode_name}")  # "1-hop F-layer"
print(f"UTC offset: {result.utc_nist_offset_ms:.2f} ms")
print(f"Verified: {result.utc_nist_verified}")
```

### MultiStationSolver for Cross-Channel Correlation

```python
from grape_recorder.grape import create_multi_station_solver

multi_solver = create_multi_station_solver('EM38ww', sample_rate=20000)

# Add observations from multiple channels
multi_solver.add_observation('WWV', 10.0, arrival_rtp, expected_rtp, snr_db=25.0)
multi_solver.add_observation('WWVH', 10.0, arrival_rtp2, expected_rtp, snr_db=22.0)

# Get combined result
combined = multi_solver.solve_combined()
print(f"Combined UTC offset: {combined.utc_offset_ms:.2f} ms")
print(f"Uncertainty: {combined.uncertainty_ms:.2f} ms")
print(f"Quality grade: {combined.quality_grade}")
```

## Quality Grades

Clock offset measurements are graded:

| Grade | Uncertainty | Criteria |
|-------|-------------|----------|
| A (Excellent) | < 0.5 ms | High confidence, multi-station verified |
| B (Good) | 0.5 - 1.5 ms | High confidence, single station |
| C (Fair) | 1.5 - 3.0 ms | Moderate confidence |
| D (Poor) | > 3.0 ms | Low confidence |
| X (Invalid) | N/A | No valid measurement |

## Migration from Legacy System

The new three-phase pipeline coexists with the legacy NPZ-based system. Migration path:

1. **Parallel Operation**: Run both systems simultaneously
2. **Validation**: Compare D_clock from new system vs legacy time_snap
3. **Cutover**: Switch to new pipeline when validated

### Legacy Components (Still Supported)

- `GrapeRecorder`: NPZ-based recording
- `AnalyticsService`: NPZ → decimated NPZ → DRF
- `DRFWriterService`: NPZ to DRF conversion

### New Components

- `RawArchiveWriter`: Phase 1
- `ClockOffsetEngine`: Phase 2
- `CorrectedProductGenerator`: Phase 3
- `PipelineOrchestrator`: Coordination

## File Locations

```
src/grape_recorder/grape/
├── raw_archive_writer.py        # Phase 1: Immutable raw archive
├── clock_offset_series.py       # Phase 2: D_clock analytical engine
├── corrected_product_generator.py  # Phase 3: Corrected products
├── pipeline_orchestrator.py     # Three-phase coordination
├── transmission_time_solver.py  # UTC back-calculation (existing)
├── wwvh_discrimination.py       # Station discrimination (existing)
└── tone_detector.py             # Tone detection (existing)
```

## Configuration

### Environment Variables

```bash
# Phase 1 settings
GRAPE_RAW_ARCHIVE_COMPRESSION=gzip
GRAPE_RAW_ARCHIVE_FILE_DURATION=3600

# Phase 2 settings
GRAPE_ANALYSIS_LATENCY=120
GRAPE_ENABLE_MULTI_STATION=true

# Phase 3 settings
GRAPE_OUTPUT_SAMPLE_RATE=10
GRAPE_STREAMING_LATENCY=2
```

### Config File (TOML)

```toml
[pipeline]
raw_archive_compression = "gzip"
raw_archive_file_duration_sec = 3600
analysis_latency_sec = 120
output_sample_rate = 10
streaming_latency_minutes = 2
enable_multi_station = true
```

## Conclusion

This three-phase architecture ensures:

1. **Data Integrity**: Raw data is never modified
2. **Reprocessability**: Any phase can be re-run
3. **Traceability**: Complete provenance chain
4. **Accuracy**: UTC(NIST) alignment through propagation modeling
5. **Flexibility**: Versioned outputs for algorithm improvements
