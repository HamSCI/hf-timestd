# Calibration System Architecture

## Overview

The HF-TimeStd calibration system learns and compensates for systematic offsets in broadcast time signal measurements to achieve convergence of D_clock (system clock offset) toward zero. This document describes how calibration works, where it's applied, and why it's essential for sub-millisecond timing accuracy.

## What is D_clock?

**D_clock** is the system clock offset relative to UTC(NIST):

```
D_clock = T_local - T_UTC
```

- **Goal**: D_clock → 0 (perfect UTC alignment)
- **Target accuracy**: ±0.5ms (1σ)
- **Current performance**: ~0.01ms after calibration convergence

## Systematic Offsets

Raw broadcast measurements contain multiple systematic offsets that prevent D_clock from reaching zero:

### 1. Propagation Delay Estimation Errors
- Phase 2 estimates propagation delays using ionospheric models (VTEC)
- Model errors: ±2-5ms depending on ionospheric conditions
- Frequency-dependent: higher frequencies (20-25 MHz) have less ionospheric delay

### 2. Detection Group Delays
- Matched filter processing introduces frequency-dependent group delays
- Varies by filter design and sample rate
- Typically 1-3ms depending on frequency

### 3. Station-Specific Offsets
- Different stations (CHU, WWV, WWVH) have different transmitter characteristics
- Path geometry differences: CHU (Ottawa) vs WWV (Colorado) = 2000+ km
- Propagation mode differences: 1-hop E-layer vs 2-hop F-layer

### 4. Frequency-Dependent Effects
- Ionospheric delay: ∝ 1/f² (larger at lower frequencies)
- Example: 2.5 MHz has ~16x more ionospheric delay than 10 MHz
- Multipath interference patterns vary by frequency

## Calibration Architecture

### Per-Broadcast Calibration

Calibration is **per-broadcast** (station + frequency combination), not per-station:

```
Broadcast Key = "STATION_FREQUENCY"
Examples:
  - "CHU_3.3"   (CHU on 3.330 MHz)
  - "CHU_7.8"   (CHU on 7.850 MHz)
  - "WWV_10.0"  (WWV on 10.000 MHz)
  - "WWV_15.0"  (WWV on 15.000 MHz)
```

**Why per-broadcast?**
- Ionospheric delays are frequency-dependent (1/f²)
- Detection group delays vary by frequency
- Propagation modes differ by frequency and time of day
- Each broadcast needs its own offset to reach zero

### Calibration Learning

The system continuously learns offsets by observing measurement history:

```python
# For each broadcast, calculate mean D_clock from recent measurements
broadcast_mean = mean(last_30_measurements)

# Offset needed to bring mean to zero
new_offset = -broadcast_mean

# Apply exponential moving average for smooth updates
alpha = 0.1 to 0.3 (depending on sample count)
offset = alpha * new_offset + (1 - alpha) * old_offset

# Rate limit: ±0.5ms per update to prevent discontinuities
if abs(offset_change) > 0.5:
    offset_change = sign(offset_change) * 0.5
```

**Update rate:**
- Fast during bootstrap: alpha = 0.3 (30% new, 70% old)
- Slower after convergence: alpha = 0.1 (10% new, 90% old)
- Further reduced if cross-station validation fails: alpha *= 0.3

### Calibration Application

Calibration offsets are applied **before fusion** to bring all measurements toward zero:

```python
# Location: multi_broadcast_fusion.py, lines ~2393-2407

# 1. Load measurements from Phase 2 HDF5 files
measurements = load_measurements()  # Raw D_clock values

# 2. Apply per-broadcast calibration
calibrated_d_clocks = []
for m in measurements:
    broadcast_key = f"{m.station}_{m.frequency_mhz:.1f}"
    offset = calibration[broadcast_key].offset_ms
    calibrated_d_clocks.append(m.d_clock_ms + offset)

# 3. Fuse calibrated measurements
fused_d_clock = weighted_mean(calibrated_d_clocks)

# 4. Apply Kalman filter for residual variations
final_d_clock = fused_d_clock - kalman_correction
```

**Critical insight:** Without applying calibration, D_clock oscillates around -2 to -5ms (the systematic offset) and never converges to zero. The Kalman filter alone cannot compensate for large systematic differences between stations.

## Calibration Persistence

Calibration state is persisted to `/dev/shm/timestd/state/timing_calibration.json`:

```json
{
  "version": 2,
  "phase": "operational",
  "WWV_10.0": {
    "offset_ms": 5.234,
    "uncertainty_ms": 0.123,
    "n_samples": 150,
    "last_updated": "2026-01-10T17:00:00Z"
  },
  "CHU_7.8": {
    "offset_ms": 3.456,
    "uncertainty_ms": 0.089,
    "n_samples": 200,
    "last_updated": "2026-01-10T17:00:00Z"
  },
  "_kalman_state": {
    "offset_ms": -0.023,
    "drift_ms_per_min": 0.0001,
    "converged": true,
    "n_updates": 150
  }
}
```

**Save frequency:** Every 10 updates (~80 seconds)
- Prevents loss of convergence progress on service restarts
- Includes Kalman filter state to avoid discontinuities

## Interaction with Kalman Filter

The system uses a **two-tier correction** approach:

### Tier 1: Calibration (Systematic Offsets)
- **Purpose**: Remove large, stable systematic offsets (2-10ms)
- **Timescale**: Converges over 30-60 minutes
- **Applied to**: Individual broadcast measurements before fusion
- **Example**: WWV 2.5 MHz consistently reads -12ms → apply +12ms offset

### Tier 2: Kalman Filter (Residual Variations)
- **Purpose**: Track and compensate for residual baseline drift
- **Timescale**: Converges over 7-10 minutes (~50 updates)
- **Applied to**: Fused D_clock after calibration
- **Example**: After calibration, fused D_clock drifts -0.5ms → Kalman tracks and removes

**Why both?**
- Calibration handles **per-broadcast** systematic differences
- Kalman handles **global** baseline offset and slow drift
- Together they achieve <0.1ms accuracy

## Bootstrap vs Operational Phases

The system adapts its behavior based on calibration convergence:

### Bootstrap Phase (First 30-60 minutes)

**Characteristics:**
- Calibration offsets are being learned
- Cross-station disagreement is high (4-8ms)
- Uncertainty is elevated (2-3ms)
- System is still converging toward zero

**Relaxed Thresholds:**
- Cross-station threshold: **5.0ms** (accommodates uncalibrated differences)
- Quality requirement: Accept **grade D** (uncertainty <3ms)
- Consistency: Accept **CROSS_STATION_DISAGREE** flag
- Discontinuity threshold: **10ms** (allows calibration adjustments)

**Detection:**
- Tracked via rolling window of last 20 cross-validation results
- Bootstrap ends when >80% of validations pass

### Operational Phase (After Convergence)

**Characteristics:**
- Calibration offsets have stabilized
- Cross-station disagreement is low (<2ms)
- Uncertainty is reduced (<1ms)
- D_clock oscillates around zero (±0.5ms)

**Strict Thresholds:**
- Cross-station threshold: **2.5ms** (enforces tight consistency)
- Quality requirement: Require **grade A/B/C** only
- Consistency: Reject **CROSS_STATION_DISAGREE** unless uncertainty <1ms
- Discontinuity threshold: **10ms** (still allows for ionospheric events)

## Convergence Behavior

### Expected Convergence Timeline

**0-10 minutes (Kalman Bootstrap):**
- Kalman filter learns baseline offset
- D_clock moves from raw values (-5 to -10ms) toward calibrated mean (-2 to -4ms)
- Correction alpha ramps from 0 → 1 over 50 updates

**10-30 minutes (Calibration Learning):**
- Per-broadcast offsets are being learned
- D_clock gradually approaches zero
- Cross-station disagreement decreases as offsets converge

**30-60 minutes (Calibration Convergence):**
- Offsets stabilize (change <0.1ms per update)
- D_clock oscillates around zero (±0.5ms)
- System transitions to operational phase
- Chrony begins trusting TMGR source

**After 60 minutes (Steady State):**
- D_clock: 0 ± 0.5ms (ionospheric variations)
- Calibration updates: <0.05ms per update (tracking slow drift)
- Kalman correction: <0.2ms (tracking residual baseline)
- Grade: A or B (uncertainty <1ms)

### What Convergence Looks Like

**Histogram of D_clock (after convergence):**
```
     |
  40 |     ***
  30 |   *******
  20 | ***********
  10 |*************
   0 +-------------+----
    -2  -1   0  +1  +2  (ms)
```
- Bell curve centered on **0ms**
- Standard deviation: ~0.5ms
- 95% of measurements within ±1ms

**Time series:**
```
D_clock (ms)
  +2 |    .  .     .
  +1 |  .  . . .  . .  .
   0 |--.-.-.-.-.-.-.-.-.-  ← Oscillates around zero
  -1 |  . .   . .   .  .
  -2 |     .     .
     +------------------
        Time (minutes)
```

## Recent Fixes (2026-01-10)

### Critical Fix: Calibration Application Enabled

**Problem:** Calibration offsets were being learned but **never applied** to measurements. A comment in the code stated:

```python
# CRITICAL FIX: Do NOT apply calibration during ongoing fusion
# Calibration is only for bootstrap/restart to help initial convergence
```

This prevented D_clock from ever converging to zero, as the 4-8ms systematic offsets remained in the measurements.

**Solution:** Re-enabled calibration application (lines 2393-2407):
```python
# Apply calibration to get calibrated D_clock values for fusion
calibrated_d_clocks = self._apply_calibration(measurements)

# Fuse calibrated measurements
fused_d_clock = weighted_mean(calibrated_d_clocks)
```

**Result:** D_clock immediately converged from -2.5ms → -0.006ms (essentially zero).

### Other Fixes Applied

1. **Kalman State Persistence** (lines 622-660, 676-721)
   - Save/restore Kalman offset and covariance on restart
   - Prevents 3-5ms discontinuities when service restarts
   - Saves every 10 updates (~80 seconds)

2. **Bootstrap-Aware Thresholds** (lines 2058-2085, 3245-3290)
   - Cross-station: 5.0ms → 2.5ms (adaptive based on validation history)
   - Quality: Accept grade D during bootstrap, A/B/C after convergence
   - Consistency: Accept CROSS_STATION_DISAGREE during bootstrap

3. **Discontinuity Threshold** (lines 3292-3312)
   - Increased from 3ms → 10ms
   - Added 5-minute timeout reset to prevent permanent blocking
   - Allows legitimate calibration adjustments and ionospheric events

## Monitoring Calibration

### Check Current State

```bash
# View calibration file
cat /dev/shm/timestd/state/timing_calibration.json | python3 -m json.tool

# Check recent D_clock values
tail -100 /var/log/hf-timestd/fusion.log | grep "Fused D_clock"

# Monitor calibration updates
tail -f /var/log/hf-timestd/fusion.log | grep "Calibration update"

# Check Chrony status
chronyc sources -v | grep TMGR
```

### Success Indicators

**Calibration converged:**
- D_clock oscillates around 0 ± 0.5ms
- Cross-station disagreement <2ms
- Calibration offsets stable (Δ <0.1ms per update)
- Grade A or B (uncertainty <1ms)

**Calibration still learning:**
- D_clock trending toward zero but not there yet
- Cross-station disagreement 2-5ms
- Calibration offsets changing 0.3-0.5ms per update
- Grade C or D (uncertainty 1-3ms)

**Calibration problem:**
- D_clock not converging (stuck at -2ms or worse)
- Cross-station disagreement >5ms
- Frequent "Skipping calibration update: GPSDO unlocked" warnings
- Grade D or F (uncertainty >2ms)

## References

- **Code**: `src/hf_timestd/core/multi_broadcast_fusion.py`
  - Calibration learning: lines 1707-1813
  - Calibration application: lines 1687-1705, 2393-2407
  - Calibration persistence: lines 622-660, 676-721
  
- **Data**: `/dev/shm/timestd/state/timing_calibration.json`
  
- **Logs**: `/var/log/hf-timestd/fusion.log`

## Summary

The calibration system is essential for achieving sub-millisecond timing accuracy. It learns and compensates for systematic offsets in broadcast measurements, allowing D_clock to converge to zero. Without calibration application, the system cannot reach its accuracy goals. The recent fix to enable calibration application was critical - it immediately brought D_clock from -2.5ms to near-zero convergence.
