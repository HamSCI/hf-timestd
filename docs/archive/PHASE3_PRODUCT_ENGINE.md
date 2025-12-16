# Phase 3: Product Generation Engine

**Status:** ✅ Implemented (December 4, 2025)  
**Module:** `src/grape_recorder/grape/phase3_product_engine.py`

## Overview

Phase 3 transforms Phase 1 raw archive + Phase 2 timing analysis into corrected 10 Hz Digital RF products for PSWS/wsprdaemon upload.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      PHASE 3: PRODUCT GENERATION                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  INPUTS:                                                                 │
│  ├─ Phase 1: raw_archive/{CHANNEL}/ (20 kHz Digital RF)                 │
│  └─ Phase 2: phase2/{CHANNEL}/clock_offset/ (D_clock CSV)               │
│                                                                          │
│  PROCESSING:                                                             │
│  1. Read 20 kHz IQ from Phase 1 raw archive                             │
│  2. Load D_clock for timestamp correction from Phase 2                  │
│  3. Apply UTC correction: t_UTC = t_system - D_clock                    │
│  4. Decimate 20 kHz → 10 Hz (factor 2000)                               │
│  5. Analyze gaps and data completeness                                   │
│  6. Write to Digital RF with UTC(NIST) timestamps                       │
│                                                                          │
│  OUTPUTS:                                                                │
│  ├─ products/{CHANNEL}/decimated/         # 10 Hz DRF (PSWS format)     │
│  ├─ products/{CHANNEL}/gap_analysis/      # JSON gap reports            │
│  └─ products/{CHANNEL}/timing_annotations/# CSV timing quality          │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

## Key Classes

### Phase3ProductEngine

Main engine for Phase 3 processing:

```python
from grape_recorder.grape import Phase3ProductEngine, Phase3Config

config = Phase3Config(
    data_root=Path('/tmp/grape-test'),
    channel_name='WWV 10 MHz',
    frequency_hz=10e6,
    station_config={
        'callsign': 'W3PM',
        'grid_square': 'EM38ww',
        'receiver_name': 'GRAPE',
        'psws_station_id': 'W3PM_1',
        'psws_instrument_id': '1'
    }
)

engine = Phase3ProductEngine(config)

# Process a full day
results = engine.process_day('2025-12-04')

# Or process individual minutes
result = engine.process_minute(system_time=1733302800.0)
```

### GapAnalysis

Detailed gap analysis for each minute:

```python
@dataclass
class GapAnalysis:
    minute_boundary_utc: float    # UTC minute boundary
    total_samples: int            # Expected samples (1,200,000 @ 20kHz)
    valid_samples: int            # Actually received
    gap_samples: int              # Missing samples
    gap_count: int                # Number of gaps
    gaps: List[GapInfo]           # Gap details
    completeness_pct: float       # Data completeness %
    largest_gap_ms: float         # Largest single gap
    data_quality: str             # 'complete', 'minor_gaps', 'major_gaps', 'unusable'
```

### TimingAnnotation

Timing quality metadata per minute:

```python
@dataclass
class TimingAnnotation:
    system_time: float        # Original Phase 1 time
    utc_time: float           # Corrected UTC time
    d_clock_ms: float         # Applied correction
    uncertainty_ms: float     # Timing uncertainty
    quality_grade: str        # 'A', 'B', 'C', 'D', 'X'
    station: str              # 'WWV', 'WWVH', 'CHU'
    propagation_mode: str     # '1F', '2F', etc.
    anchor_confidence: float  # Confidence in timing anchor
```

## Output Structure

### PSWS-Compatible DRF

```
products/{CHANNEL}/decimated/{YYYYMMDD}/
└── {CALLSIGN}_{GRID}/
    └── {RECEIVER}@{STATION_ID}_{INSTRUMENT_ID}/
        └── OBS{YYYY-MM-DD}T{HH-MM}/
            └── ch0/
                ├── rf@{timestamp}.h5    # 10 Hz IQ data
                └── metadata/
                    └── metadata.h5       # Station metadata
```

### Gap Analysis JSON

```json
{
  "date": "2025-12-04",
  "channel_name": "WWV 10 MHz",
  "total_minutes": 1440,
  "complete_minutes": 1423,
  "minutes_with_gaps": 17,
  "total_gap_samples": 45000,
  "overall_completeness_pct": 99.87,
  "minutes": [
    {
      "minute_boundary_utc": 1733302800.0,
      "total_samples": 1200000,
      "valid_samples": 1198000,
      "gap_samples": 2000,
      "gap_count": 1,
      "completeness_pct": 99.83,
      "largest_gap_ms": 100.0,
      "data_quality": "minor_gaps",
      "gaps": [
        {
          "start_sample": 500000,
          "duration_samples": 2000,
          "duration_ms": 100.0,
          "fill_method": "zeros"
        }
      ]
    }
  ]
}
```

### Timing Annotations CSV

```csv
system_time,utc_time,d_clock_ms,uncertainty_ms,quality_grade,station,propagation_mode,anchor_confidence
1733302800.0,1733302799.985,15.2,0.5,A,WWV,1F,0.95
1733302860.0,1733302859.987,12.8,0.6,B,WWVH,2F,0.88
```

## CLI Usage

```bash
# Process a single channel for one day
python scripts/run_phase3_processor.py \
    --data-root /tmp/grape-test \
    --channel "WWV 10 MHz" \
    --date 2025-12-04

# Process all channels for yesterday
python scripts/run_phase3_processor.py \
    --data-root /tmp/grape-test \
    --all-channels \
    --yesterday

# Process with custom station config
python scripts/run_phase3_processor.py \
    --data-root /tmp/grape-test \
    --channel "WWV 10 MHz" \
    --date 2025-12-04 \
    --callsign W3PM \
    --grid EM38ww \
    --psws-station-id W3PM_1
```

## Integration with PSWS Upload

Phase 3 output is directly compatible with existing upload scripts:

```bash
# Upload yesterday's decimated data
./scripts/daily-drf-upload.sh /tmp/grape-test/products

# The uploader reads from products/{CHANNEL}/decimated/
# and uploads to PSWS via rsync/SFTP
```

## Gap Handling

| Gap Size | Handling | Flag |
|----------|----------|------|
| < 10 samples (0.5ms) | Ignored | - |
| 10-1000 samples (<50ms) | Zero fill | minor_gaps |
| 1000-20000 samples (1s) | Zero fill | major_gaps |
| > 20000 samples | Zero fill | unusable |

## Quality Grades

| Grade | D_clock Uncertainty | Criteria |
|-------|---------------------|----------|
| A | < 0.5 ms | High confidence, verified |
| B | 0.5-1.5 ms | Good confidence |
| C | 1.5-3.0 ms | Moderate confidence |
| D | > 3.0 ms | Low confidence |
| X | N/A | No valid measurement |

## Data Flow

```
Phase 1 (raw_archive)    Phase 2 (phase2)
        │                       │
        │ 20 kHz IQ             │ D_clock CSV
        ▼                       ▼
┌─────────────────────────────────────────┐
│         Phase 3 Product Engine          │
│                                         │
│  1. Read Phase 1 DRF                    │
│  2. Load Phase 2 D_clock                │
│  3. Apply UTC correction                │
│  4. Decimate 20kHz → 10Hz               │
│  5. Analyze gaps                        │
│  6. Write output DRF                    │
└─────────────────────────────────────────┘
        │
        ▼
products/{CHANNEL}/
├── decimated/      → PSWS Upload
├── gap_analysis/   → Quality Reports
└── timing_annotations/ → Audit Trail
```

## Factory Functions

```python
from grape_recorder.grape import create_phase3_engine, process_channel_day

# Create engine for real-time processing
engine = create_phase3_engine(
    data_root=Path('/tmp/grape-test'),
    channel_name='WWV 10 MHz',
    frequency_hz=10e6,
    station_config={...}
)

# Or use convenience function for batch processing
results = process_channel_day(
    data_root=Path('/tmp/grape-test'),
    channel_name='WWV 10 MHz',
    frequency_hz=10e6,
    station_config={...},
    date_str='2025-12-04'
)
```

## Reprocessing

Phase 3 can be safely re-run on any historical data:

```python
# Reprocess with updated Phase 2 analysis
engine = Phase3ProductEngine(config)

# Will overwrite existing Phase 3 outputs
results = engine.process_day('2025-12-01')
```

This enables algorithm improvements without re-recording.
