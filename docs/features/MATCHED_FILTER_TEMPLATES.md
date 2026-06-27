# Matched Filter Templates for Time Signal Detection

## Overview

The `hf_timestd.core` module provides advanced matched filter templates for detecting and correlating time signal modulation patterns from WWV, WWVH, CHU, and BPM stations. These templates enable:

1. **Per-second tick detection** with overlapping windows for improved SNR
2. **BCD time code correlation** for WWV/WWVH 100 Hz subcarrier
3. **AFSK demodulation** for CHU Bell 103 FSK time code
4. **BPM pattern recognition** with UTC/UT1 mode awareness

## Modules

### 1. `tick_matched_filter.py` - Per-Second Tick Detection

Provides station-specific matched filtering for per-second timing ticks using overlapping 5-second windows.

#### Theory

Traditional FFT-based tick detection uses single-second windows, limiting SNR improvement. Matched filtering with overlapping windows provides:

- **Coherent integration** across multiple ticks (up to 5 seconds)
- **Doppler tracking** via window overlap (1-second steps)
- **Phase-invariant detection** using quadrature templates
- **Sub-sample timing** via parabolic interpolation

The 5-second window balances SNR gain against Doppler decorrelation (coherence time Tc ≈ 10-20 seconds at HF).

#### Station-Specific Templates

| Station | Frequency | Tick Duration | Skip Seconds | Notes |
|---------|-----------|---------------|--------------|-------|
| WWV | 1000 Hz | 5 ms | 29, 59 | Minute marker, voice announcements (second 0 carries the minute marker and is NOT skipped) |
| WWVH | 1200 Hz | 5 ms | 29, 59 | Minute marker, voice announcements (second 0 carries the minute marker and is NOT skipped) |
| CHU | 1000 Hz | 10 ms / 300 ms | 29 | 300ms for seconds 31-39 (FSK) |
| BPM | 1000 Hz | 10 ms / 100 ms | 0 | Minute-dependent (UTC vs UT1) |

#### Usage

```python
from hf_timestd.core import create_tick_filter, WWV_TEMPLATE

# Create filter for WWV
filter = create_tick_filter(station='WWV', sample_rate=20000)

# Process 60 seconds of IQ samples
results = filter.process_minute(iq_samples, minute=30)

# Access timing results
for window in results.window_results:
    print(f"Window {window.window_index}: offset={window.timing_offset_ms:.3f} ms")

print(f"Drift rate: {results.drift_rate_ppm:.2f} ppm")
```

#### Key Classes

- **`TickTemplate`**: Station-specific tick parameters (frequency, duration, skip seconds)
- **`TickMatchedFilter`**: Main filter class with quadrature templates
- **`TickDetectionResult`**: Per-window detection results
- **`MinuteTickAnalysis`**: Aggregated minute analysis with drift estimation

---

### 2. `signal_templates.py` - Modulation Pattern Templates

Provides matched filter templates for complex modulation patterns: BCD time code, AFSK, and BPM patterns.

#### BCD 100 Hz Modulation (WWV/WWVH)

The BCD (Binary Coded Decimal) time code is transmitted on a 100 Hz subcarrier with pulse-width modulation:

| Bit Type | HIGH Duration | LOW Duration |
|----------|---------------|--------------|
| Binary 0 | 200 ms | 800 ms |
| Binary 1 | 500 ms | 500 ms |
| Marker | 800 ms | 200 ms |

Position markers occur at seconds 0, 9, 19, 29, 39, 49, 59.

**Key insight**: WWV and WWVH transmit **identical** BCD patterns, making this ideal for dual-peak delay measurement and station discrimination.

```python
from hf_timestd.core import create_bcd_generator, create_correlator

# Generate BCD template for specific minute
bcd = create_bcd_generator(sample_rate=20000)
template = bcd.generate_minute_template(timestamp)

# Or use the correlator for overlapping window analysis
correlator = create_correlator(sample_rate=20000)
results = correlator.correlate_bcd(
    iq_samples, 
    timestamp,
    window_seconds=10,
    overlap_seconds=1
)

for r in results:
    print(f"WWV delay: {r.wwv_delay_ms:.3f} ms")
    print(f"WWVH delay: {r.wwvh_delay_ms:.3f} ms")
    print(f"Differential: {r.differential_delay_ms:.3f} ms")
```

#### CHU AFSK (Bell 103)

CHU transmits FSK time code during seconds 31-39 of each minute:

| Parameter | Value |
|-----------|-------|
| Mark frequency | 2225 Hz |
| Space frequency | 2025 Hz |
| Baud rate | 300 bps |
| Bit duration | 3.333 ms |
| Frame format | 1 start + 8 data + 2 stop = 11 bits |

**Timing structure per FSK second:**
- 0-10 ms: 1000 Hz tick
- 10-133 ms: Mark tone (sync)
- 133-500 ms: Data stream
- **500 ms: Precise timing boundary**

```python
from hf_timestd.core import create_afsk_generator

afsk = create_afsk_generator(sample_rate=20000)

# Generate quadrature templates for phase-invariant detection
sin_t, cos_t = afsk.generate_quadrature_templates(
    duration_ms=100,
    frequency=2225  # Mark frequency
)

# Generate FSK second template
template = afsk.generate_fsk_second_template(second=31)
```

#### BPM Patterns (China)

BPM uses minute-dependent tick durations for UTC/UT1 discrimination:

| Minute Range | Timing Mode | Tick Duration | Use for UTC? |
|--------------|-------------|---------------|--------------|
| 0-24, 30-54 | UTC | 10 ms | ✓ Yes |
| 25-29, 55-59 | UT1 | 100 ms | ✗ No |

**Additional BPM parameters:**
- Tick frequency: 1000 Hz
- Minute marker: 300 ms
- BCD time code on 100 Hz subcarrier (similar to WWV)

```python
from hf_timestd.core import create_bpm_generator

bpm = create_bpm_generator(sample_rate=20000)

# Check if minute is safe for UTC timing
if bpm.is_utc_minute(minute=30):
    sin_t, cos_t = bpm.generate_tick_template(minute=30, second=5)
    # Use for timing...
else:
    print("UT1 minute - do not use for UTC timing")
```

---

## Unified Correlation Engine

The `SignalTemplateCorrelator` class provides a unified interface for all template types:

```python
from hf_timestd.core import create_correlator

correlator = create_correlator(sample_rate=20000)

# BCD correlation (WWV/WWVH)
bcd_results = correlator.correlate_bcd(iq_samples, timestamp)

# AFSK correlation (CHU)
afsk_results = correlator.correlate_afsk(iq_samples)

# BPM correlation
bpm_results = correlator.correlate_bpm(iq_samples, minute=30)
```

---

## Overlapping Window Strategy

For patterns spanning multiple seconds, overlapping windows track timing drift and handle Doppler decorrelation:

| Pattern | Window Duration | Overlap | Rationale |
|---------|-----------------|---------|-----------|
| Tick detection | 5 seconds | 1 second | Balance SNR vs Doppler coherence |
| BCD correlation | 10 seconds | 1 second | Within Tc ≈ 10-20s |
| AFSK correlation | 1 second | Per-second | FSK seconds are independent |
| BPM correlation | 5 seconds | 1 second | Match tick detection |

---

## Result Data Classes

### `TickDetectionResult`
```python
@dataclass
class TickDetectionResult:
    window_index: int           # Window number (0-54 for 55 windows)
    window_start_sec: float     # Start time within minute
    timing_offset_ms: float     # Offset from expected position
    correlation_peak: float     # Normalized peak amplitude
    snr_db: float              # Signal-to-noise ratio
    valid_ticks: int           # Number of ticks in window
```

### `BCDCorrelationResult`
```python
@dataclass
class BCDCorrelationResult:
    window_start_sec: float
    wwv_delay_ms: float         # WWV peak delay
    wwvh_delay_ms: float        # WWVH peak delay
    differential_delay_ms: float # WWVH - WWV
    wwv_amplitude: float
    wwvh_amplitude: float
    amplitude_ratio_db: float   # 20*log10(WWV/WWVH)
    correlation_quality: float
    detection_type: str         # 'dual_peak', 'single_wwv', 'single_wwvh'
```

### `AFSKCorrelationResult`
```python
@dataclass
class AFSKCorrelationResult:
    second: int                 # FSK second (31-39)
    timing_offset_ms: float     # Offset from 500ms boundary
    correlation_peak: float
    snr_db: float
    mark_power_db: float
    space_power_db: float
    fsk_quality: float          # 0-1 quality metric
```

### `BPMCorrelationResult`
```python
@dataclass
class BPMCorrelationResult:
    window_start_sec: float
    timing_offset_ms: float
    tick_duration_ms: float     # Measured duration
    expected_duration_ms: float # Expected based on minute
    duration_match: bool
    correlation_peak: float
    snr_db: float
    timing_mode: str            # 'UTC' or 'UT1'
    is_usable: bool            # True if UTC minute
```

---

## Test Coverage

The modules include comprehensive test suites:

- **`test_tick_matched_filter.py`**: 22 tests covering templates, filter creation, tick duration selection, composite templates, synthetic signal detection, overlapping windows, and BPM UT1/UTC handling.

- **`test_signal_templates.py`**: 41 tests covering BCD pulse widths, AFSK frequencies, BPM timing modes, correlation engine, and factory functions.

Run tests:
```bash
pytest tests/test_tick_matched_filter.py tests/test_signal_templates.py -v
```

---

## Dependencies

- NumPy
- SciPy (signal processing, FFT, filters)

---

## References

1. NIST SP 250-67: WWV/WWVH Time Code Format
2. NRC CHU Technical Documentation
3. BPM (China) Time Signal Specifications
4. IRIG Standard 200-04: Time Code Formats
