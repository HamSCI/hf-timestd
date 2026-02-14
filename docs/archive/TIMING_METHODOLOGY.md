# GRAPE Timing Methodology

## Overview

GRAPE measures the offset between your system clock and UTC(NIST) atomic time by analyzing time standard broadcasts from WWV, WWVH, and CHU. This document explains how we achieve sub-millisecond accuracy.

---

## The D_clock Measurement

**D_clock** (Delta Clock) represents the difference between your system clock and UTC(NIST):

```
D_clock = System_Time - UTC(NIST)
```

- **D_clock = 0**: Your clock is perfectly aligned with UTC(NIST)
- **D_clock > 0**: Your clock is ahead of UTC(NIST)
- **D_clock < 0**: Your clock is behind UTC(NIST)

### How We Measure It

1. **Tone Detection**: WWV/WWVH transmit precisely-timed 1000 Hz and 1200 Hz tones at the start of each minute. We detect these using matched filters.

2. **Propagation Delay Correction**: Radio signals travel at the speed of light but take time to reach you. We calculate the expected delay based on:
   - Distance to transmitter (haversine formula)
   - Ionospheric propagation mode (1-hop, 2-hop, etc.)
   - Layer height (E-layer ~110km, F-layer ~300km)

3. **Transmission Time Solution**: 
   ```
   D_clock = Tone_Arrival_Time - Expected_Arrival_Time
   Expected = Minute_Boundary + Propagation_Delay
   ```

---

## Multi-Broadcast Fusion

### Why Fusion?

Single-broadcast D_clock has systematic errors:
- Ionospheric delay uncertainty (±0.5-2 ms)
- Propagation mode ambiguity
- Station-specific biases

By combining **13 broadcasts** (6 WWV + 4 WWVH + 3 CHU), we can:
- Average out random errors
- Learn and correct systematic biases
- Achieve higher confidence

### The Calibration Process

Each station (WWV, WWVH, CHU) may have a consistent offset due to:
- Propagation model inaccuracies for your location
- Ionospheric conditions
- Equipment delays

We learn these offsets using **Exponential Moving Average (EMA)**:

```
calibration_offset = -mean(raw_d_clock)
```

This offset is continuously updated:
```
new_offset = α × ideal_offset + (1-α) × old_offset
```

Where α = 0.5 (50% new, 50% old) for responsive tracking.

### Fused D_clock Calculation

```
fused_d_clock = Σ(weight_i × (raw_d_clock_i + calibration_offset_i)) / Σ(weight_i)
```

Weights are based on:
- **SNR**: Higher signal = more reliable
- **Quality Grade**: A/B/C/D based on detection confidence
- **Propagation Mode**: Lower hops = more reliable (1F > 2F > 3F)

---

## Visualizations Explained

### 1. Clock Stability Convergence (Kalman Funnel)

Shows D_clock measurements over time with uncertainty bounds.

- **Green points (Locked)**: High confidence, low uncertainty
- **Yellow points (Hold)**: Medium confidence
- **Red points (Anomaly)**: Outside expected bounds
- **Green line at 0**: UTC(NIST) reference

**What to look for**: Points should cluster around 0 after calibration converges.

### 2. Station Constellation

Polar plot showing timing error by station azimuth.

- **Distance from center**: Magnitude of timing error (closer = better)
- **Angle**: Azimuth to transmitter from your location
- **Colors**: Green (<1ms), Yellow (1-5ms), Red (>5ms)

**What to look for**: All stations should cluster near center after calibration.

### 3. Consensus Time Distribution

Kernel Density Estimation (KDE) showing agreement across all measurements.

- **Sharp peak at 0**: All stations agree, well-calibrated
- **Peak offset from 0**: Systematic bias (calibration still converging)
- **Double hump**: Mode ambiguity (different propagation paths)

**What to look for**: Single sharp peak centered at 0 ms.

### 4. Propagation Mode Probability

Shows which ionospheric propagation modes are most likely for each frequency.

- **1E/1F**: Single-hop (most direct, lowest delay)
- **2F/3F**: Multi-hop (longer path, higher delay)
- **Probability bars**: Relative likelihood of each mode

---

## Accuracy Expectations

| Condition | Expected Accuracy |
|-----------|-------------------|
| Multiple stations, calibrated | ±0.5 ms |
| Single station, good SNR | ±1-2 ms |
| Poor propagation | ±5-10 ms |
| Uncalibrated | Systematic bias up to ±10 ms |

### Factors Affecting Accuracy

1. **Ionospheric conditions**: Solar activity affects F-layer height
2. **Time of day**: Propagation modes change day/night
3. **Season**: Winter vs summer ionospheric profiles
4. **Signal strength**: Higher SNR = better timing precision

---

## Convergence Indicators

The fusion panel shows calibration progress per station:

- **✓ Locked** (≥95%): Calibration stable, high confidence
- **Converging** (50-95%): Learning in progress
- **Learning** (<50%): Initial calibration phase
- **No signal**: Station not received

Convergence is based on:
- Sample count (60% weight): More samples = more confident
- Uncertainty (40% weight): Lower spread = more stable

---

## Technical References

- **WWV/WWVH**: NIST time and frequency broadcasts (2.5-25 MHz)
- **CHU**: NRC Canada time broadcasts (3.33, 7.85, 14.67 MHz)
- **Propagation**: ITU-R P.533 HF propagation model
- **Tone Detection**: Matched filter with Goertzel algorithm

---

## Questions?

If measurements seem incorrect:
1. Check SNR - signals below 10 dB are unreliable
2. Verify propagation mode - wrong mode = wrong delay
3. Allow calibration to converge (5-10 minutes minimum)
4. Check for interference or equipment issues

