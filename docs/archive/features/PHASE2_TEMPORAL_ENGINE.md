# Phase 2: Temporal Analysis Engine

## Overview

The Phase 2 Temporal Analysis Engine implements a **refined temporal analysis order** that systematically "zeros in" on true UTC time. Each sequential step benefits from the precision of the preceding one.

## Core Principle

Establish the most precise estimate of the **RTP → Wall Clock offset** first, which allows subsequent, more sensitive techniques (like BCD correlation and Doppler analysis) to operate within tighter search windows and thus achieve higher confidence and accuracy.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      PHASE 2: TEMPORAL ANALYSIS ENGINE                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 1: FUNDAMENTAL TONE DETECTION (Least Precision → Anchor)       │   │
│  │                                                                     │   │
│  │  • Matched Filter Detection (1000 Hz WWV, 1200 Hz WWVH)            │   │
│  │  • Establishes initial Time Snap Reference                         │   │
│  │  • Provides timing_error_ms for the current minute                 │   │
│  │  • Search window: ±500ms                                           │   │
│  │                                                                     │   │
│  │  Output: TimeSnapResult                                            │   │
│  │    - timing_error_ms                                               │   │
│  │    - arrival_rtp                                                   │   │
│  │    - anchor_station ('WWV', 'WWVH', 'CHU')                        │   │
│  │    - anchor_confidence                                             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              ↓                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 2: IONOSPHERIC CHANNEL CHARACTERIZATION                        │   │
│  │         (High Sensitivity → Confidence Scoring)                     │   │
│  │                                                                     │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │ 2A: BCD CORRELATION & DUAL-PEAK DELAY                       │   │   │
│  │  │  • 100 Hz subcarrier cross-correlation                      │   │   │
│  │  │  • Measures Δτ (differential delay between WWV/WWVH)        │   │   │
│  │  │  • Template sync using Time Snap from Step 1                │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                     │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │ 2B: DOPPLER AND COHERENCE                                   │   │   │
│  │  │  • Per-tick phase tracking for Δf_D estimation              │   │   │
│  │  │  • Maximum coherent integration window: T_max = 1/(8×|Δf_D|) │   │   │
│  │  │  • Phase variance for channel stability                     │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                     │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │ 2C: STATION IDENTITY & GROUND TRUTH                         │   │   │
│  │  │  • 500/600 Hz exclusive tones (14 minutes/hour)             │   │   │
│  │  │  • 440 Hz tone detection (minutes 1, 2)                     │   │   │
│  │  │  • Weighted voting combiner                                 │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                     │   │
│  │  Output: ChannelCharacterization                                   │   │
│  │    - bcd_differential_delay_ms                                     │   │
│  │    - doppler_wwv_hz, doppler_wwvh_hz                               │   │
│  │    - max_coherent_window_sec                                       │   │
│  │    - dominant_station                                              │   │
│  │    - refined_search_window_ms (narrowed to ±10-50ms)              │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              ↓                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ STEP 3: TRANSMISSION TIME SOLUTION (Highest Precision → D_clock)   │   │
│  │                                                                     │   │
│  │  • Fuses T_arrival (Step 1) + Channel Metrics (Step 2)             │   │
│  │  • TransmissionTimeSolver identifies propagation mode              │   │
│  │  • Calculates T_propagation using ionospheric model                │   │
│  │  • Back-calculates T_emission (UTC)                                │   │
│  │                                                                     │   │
│  │  Formula:                                                          │   │
│  │    D_clock = T_system - T_UTC                                      │   │
│  │    T_UTC = T_arrival - T_propagation                               │   │
│  │                                                                     │   │
│  │  Output: TransmissionTimeSolution                                  │   │
│  │    - d_clock_ms (THE HOLY GRAIL)                                   │   │
│  │    - propagation_mode ('1F', '2F', 'GW', etc.)                     │   │
│  │    - confidence                                                    │   │
│  │    - uncertainty_ms                                                │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 32-bit Float Data Handling

### Input Format
- **Phase 1 stores**: `np.complex64` (32-bit float I + 32-bit float Q)
- **Dynamic range**: 144 dB (vs 96 dB for 16-bit int)
- **AGC disabled**: F32 has sufficient dynamic range

### Normalization
The engine applies fixed normalization to prevent numerical issues while preserving linearity:

```python
EXPECTED_DTYPE = np.complex64
MAX_EXPECTED_AMPLITUDE = 1.0
AMPLITUDE_WARNING_THRESHOLD = 10.0

def _validate_input(self, iq_samples):
    # Check dtype - must be complex64 (32-bit float IQ)
    if iq_samples.dtype != EXPECTED_DTYPE:
        iq_samples = iq_samples.astype(EXPECTED_DTYPE)
    
    # Apply fixed normalization if needed
    max_amp = np.max(np.abs(iq_samples))
    if max_amp > MAX_EXPECTED_AMPLITUDE:
        iq_samples = iq_samples / max_amp
    
    return iq_samples, metrics
```

## Quality Grading

| Grade | Criteria | D_clock Uncertainty |
|-------|----------|---------------------|
| **A** (Excellent) | UTC verified + ground truth OR multi-method agreement | < 1 ms |
| **B** (Good) | High confidence, no disagreements | 1-2 ms |
| **C** (Fair) | Moderate confidence | 2-5 ms |
| **D** (Poor) | Low confidence | > 5 ms |
| **X** (Invalid) | No valid measurement | N/A |

## Key Files

| File | Description |
|------|-------------|
| `phase2_temporal_engine.py` | Core engine implementing 3-step refined analysis |
| `clock_offset_series.py` | ClockOffsetEngine (now delegates to Phase2TemporalEngine) |
| `tone_detector.py` | Step 1: Matched filter tone detection |
| `wwvh_discrimination.py` | Step 2: BCD correlation, Doppler, station ID |
| `transmission_time_solver.py` | Step 3: Propagation mode solving |

## Usage

```python
from hf_timestd.core import create_phase2_engine

engine = create_phase2_engine(
    raw_buffer_dir=Path('/data/raw_buffer'),
    output_dir=Path('/data/phase2'),
    channel_name='WWV_10MHz',
    frequency_hz=10e6,
    receiver_grid='EM38ww'
)

# Process a minute of data
result = engine.process_minute(
    iq_samples=samples,      # np.complex64 array
    system_time=timestamp,   # Unix timestamp
    rtp_timestamp=rtp_ts     # RTP timestamp
)

# Access results
print(f"D_clock: {result.d_clock_ms:+.2f}ms")
print(f"Quality: {result.quality_grade}")
print(f"Station: {result.solution.station}")
print(f"Mode: {result.solution.propagation_mode}")
```

## Integration with ClockOffsetEngine

The existing `ClockOffsetEngine` now delegates to `Phase2TemporalEngine`:

```python
class ClockOffsetEngine:
    def _init_analyzers(self):
        # Phase 2 Temporal Engine - implements refined analysis order
        self.phase2_engine = Phase2TemporalEngine(...)
    
    def process_minute(self, iq_samples, system_time, rtp_timestamp):
        # Delegate to Phase2TemporalEngine
        phase2_result = self.phase2_engine.process_minute(...)
        
        # Convert to ClockOffsetMeasurement
        measurement = ClockOffsetMeasurement(
            d_clock_ms=phase2_result.solution.d_clock_ms,
            ...
        )
        return measurement
```

## Search Window Narrowing

The refined analysis order progressively narrows the search window:

| Step | Search Window | Method |
|------|---------------|--------|
| Initial | ±500ms | Tone detection range |
| After Step 1 | ±100ms | Time snap anchor established |
| After Step 2A | ±50ms | BCD correlation peak |
| After Step 2B | ±25ms | Doppler-limited coherence |
| After Step 2C | ±10ms | Ground truth confirmation |
| Step 3 | <3ms | Final propagation solve |

## Channel Quality Metrics

The engine calculates the **Spreading Factor** L to assess channel quality:

```
L = τ_D × f_D

Where:
- τ_D = delay spread (seconds)
- f_D = Doppler spread ≈ 1/(π × τ_c)
- τ_c = coherence time (seconds)

Interpretation:
- L < 0.05: Underspread (clean, timing reliable)
- L > 0.3: Moderately spread (timing degraded)
- L > 1.0: Overspread (timing severely unreliable)
```

## Version History

- **2.0.0** (2025-12-03): Initial implementation with refined temporal analysis order
  - 3-step hierarchical analysis
  - 32-bit float (complex64) support
  - Integrated with existing ClockOffsetEngine
