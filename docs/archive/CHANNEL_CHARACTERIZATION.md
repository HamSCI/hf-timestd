# HF Channel Characterization for WWV/WWVH Discrimination

## Overview

The GRAPE signal recorder now implements sophisticated HF ionospheric channel characterization to optimize coherent integration and discrimination accuracy. This addresses the fundamental challenge: **Doppler shift from ionospheric movement limits coherent integration window size**.

## Physical Constraints

### Doppler Effect on Coherent Integration

**Problem**: The ionosphere acts as a moving reflector, causing Doppler shifts (Δf_D) that create phase rotation:

```
Δφ = 2π · Δf_D · T_window
```

When Δφ approaches π/2 (90°), coherent integration gain collapses.

**Maximum Window** (for <3 dB coherent loss):

```
T_max ≈ 1 / (8 · |Δf_D|)
```

| Doppler Shift | Max Coherent Window |
|---------------|---------------------|
| 0.05 Hz       | 2.5 seconds         |
| 0.1 Hz        | 1.25 seconds        |
| 0.5 Hz        | 0.25 seconds        |
| 1.0 Hz        | 0.125 seconds       |

### Typical HF Doppler Conditions

- **Stable (nighttime)**: 0.01-0.05 Hz → 60s windows viable
- **Moderate (daytime)**: 0.1-0.2 Hz → 10-20s windows max
- **Disturbed (storms, sunrise/sunset)**: 1-5 Hz → Coherent integration fails

## Implementation

### 1. Doppler Shift Estimation (`estimate_doppler_shift`)

**Method**: Phase tracking of 1000/1200 Hz tones across consecutive ticks

```python
# Track phase progression from tick detection
φ_k = phase at tick k (second k)
Δφ = φ_k - φ_{k-1}  # Unwrapped phase difference
Δf_D ≈ Δφ / (2π · 1s)  # Doppler shift in Hz
```

**Requirements**:
- Minimum 10 high-SNR ticks (SNR > 10 dB)
- Phase unwrapping to handle 2π discontinuities
- Linear regression to estimate Doppler from phase slope

**Outputs**:
- `wwv_doppler_hz`: WWV Doppler shift
- `wwvh_doppler_hz`: WWVH Doppler shift
- `max_coherent_window_sec`: Maximum safe integration window
- `doppler_quality`: Confidence metric (0-1, from phase fit residuals)
- `phase_variance_rad`: RMS phase deviation

### 2. Delay Spread Measurement (`measure_peak_width`)

**Method**: Full-Width at Half-Maximum (FWHM) of BCD correlation peaks

```python
# Measure peak width in correlation function
FWHM = time width where peak > peak_max/2
τ_D = FWHM  # Delay spread (multipath time spreading)
```

**Physical Meaning**:
- **Narrow peak** (~1-2 ms): Single-path or minimal multipath
- **Broad peak** (>5 ms): Strong multipath, multiple ionospheric layers
- **Double peak**: Distinct WWV/WWVH arrivals separated by differential delay

**Outputs** (per BCD window):
- `wwv_delay_spread_ms`: WWV peak width
- `wwvh_delay_spread_ms`: WWVH peak width

### 3. Adaptive BCD Window Sizing

**Integration**: Doppler info → BCD discrimination

```python
# Default: 60-second window for maximum SNR
window_seconds = 60.0

# Limit based on Doppler
if doppler_info and doppler_limit < 60:
    window_seconds = max(10.0, min(doppler_limit, 60.0))
    logger.info(f"Doppler-limited to {window_seconds:.1f}s (Δf_D={Δf_D:.3f} Hz)")
```

**Logic**:
1. Estimate Doppler from tick phases (every minute)
2. Calculate max coherent window (T_max)
3. Clamp BCD window to [10s, 60s] range
4. Log when Doppler limits are active

## Complementary Metrics

The system now measures **both** fundamental HF channel parameters:

### Doppler Spread (B_D) → Coherence Time (τ_c)

**From**: Tick phase tracking (1000/1200 Hz tones)

**Measure**: Frequency spread of received signal

```
τ_c ≈ 1 / B_D
```

**Use**: Determines how long channel remains stable

### Delay Spread (τ_D) → Coherence Bandwidth (B_c)

**From**: BCD correlation peak width (100 Hz modulation)

**Measure**: Time spread of received signal (multipath)

```
B_c ≈ 1 / τ_D
```

**Use**: Determines maximum signaling bandwidth before ISI

## Signal Quality Comparison

| Feature | 1000/1200 Hz Tones | 100 Hz BCD | 5ms Ticks |
|---------|-------------------|------------|-----------|
| **Duration** | 800 ms/minute | 53 sec/minute | 59 × 5ms/minute |
| **Primary Use** | Instantaneous power ratio | Time-of-Arrival, delay | Coherent SNR gain |
| **Channel Metric** | Doppler shift (Δf_D) | Delay spread (τ_D) | Doppler spread (B_D) |
| **Integration** | 800 ms max | Adaptive 10-60s | Adaptive coherent/incoherent |
| **Discrimination** | Fast, tone-specific | Robust, long-term | Sensitive, phase-dependent |

## Data Flow

```
IQ Samples (16 kHz, complex)
    │
    ├─→ Tick Detection (5ms pulses @ 1000/1200 Hz)
    │      ├─→ Coherent/Incoherent integration
    │      └─→ Phase tracking per tick
    │
    ├─→ Doppler Estimation (from tick phases)
    │      ├─→ Phase unwrapping & linear fit
    │      ├─→ Calculate Δf_D (Hz)
    │      └─→ Determine max_coherent_window
    │
    └─→ BCD Correlation (100 Hz modulation)
           ├─→ Adaptive window (Doppler-limited)
           ├─→ Cross-correlation with template
           ├─→ Peak detection (WWV & WWVH)
           ├─→ Delay spread measurement (τ_D)
           └─→ Time-of-Arrival + Amplitudes
```

## Benefits

### 1. Prevents Coherent Integration Failure
- **Before**: 60s BCD window always used, failed in high-Doppler conditions
- **After**: Window adapts to channel (10-60s), maintains correlation quality

### 2. Provides Channel Diagnostics
- **Doppler spread**: Tells you if ionosphere is stable or turbulent
- **Delay spread**: Tells you if propagation is single-path or multipath
- **Combined**: Full characterization of HF channel quality

### 3. Improves Discrimination Confidence
- **Doppler quality**: High = phase-based discrimination reliable
- **Delay spread**: Narrow = clean discrimination, Broad = multipath confusion

### 4. Future Enhancements Enable
- Adaptive modulation/coding based on channel quality
- Predictive propagation modeling
- Long-term ionospheric statistics
- Real-time propagation alerts

## Usage in Analytics

The Doppler and delay spread metrics are automatically calculated and used:

```python
# In analyze_minute_with_440hz():

# 1. Detect ticks with phase tracking
tick_windows = detect_tick_windows(iq_samples, sample_rate, window_seconds=60)

# 2. Estimate Doppler from tick phases
doppler_info = estimate_doppler_shift(tick_windows)
# → {wwv_doppler_hz, max_coherent_window_sec, doppler_quality, ...}

# 3. BCD discrimination with Doppler-adaptive window
bcd_wwv, bcd_wwvh, bcd_delay, bcd_quality, bcd_windows = detect_bcd_discrimination(
    iq_samples, sample_rate, minute_timestamp, frequency_mhz, doppler_info
)

# bcd_windows contains delay spread for each correlation window
```

## Log Messages

### Doppler Estimation
```
INFO: WWV 10 MHz: Doppler estimate: WWV=+0.123 Hz, WWVH=+0.145 Hz, max_window=10.2s, quality=0.87
```

### Doppler-Limited BCD Window
```
INFO: WWV 10 MHz: Doppler-limited BCD window to 15.0s (Δf_D=+0.083 Hz, quality=0.92)
```

### BCD Correlation with Delay Spread
```
INFO: WWV 10 MHz: BCD correlation (1 windows, 60.0s) - WWV amp=2.5±0.1, WWVH amp=1.8±0.1, 
                  ratio=+2.9dB, delay=12.35±0.45ms, quality=8.2
                  [Delay spread: WWV=2.1ms, WWVH=2.8ms indicates moderate multipath]
```

## References

1. **Doppler Effect on HF**: Ionospheric movement causes frequency shifts proportional to velocity
2. **Coherence Time**: Duration over which channel complex gain is stable
3. **Delay Spread**: Time duration over which multipath arrivals are distributed
4. **Coherent Integration**: Vector sum of complex amplitudes for SNR gain
5. **Cross-Correlation**: Matched filtering for time-of-arrival and amplitude estimation

## Technical Implementation Files

- `wwvh_discrimination.py::estimate_doppler_shift()` - Lines 982-1072
- `wwvh_discrimination.py::bcd_correlation_discrimination()` - Lines 1074-1423
  - `measure_peak_width()` helper function - Lines 1273-1291
- `wwvh_discrimination.py::detect_bcd_discrimination()` - Lines 1425-1471 (adaptive wrapper)
- `wwvh_discrimination.py::analyze_minute_with_440hz()` - Lines 1576-1598 (integration)
