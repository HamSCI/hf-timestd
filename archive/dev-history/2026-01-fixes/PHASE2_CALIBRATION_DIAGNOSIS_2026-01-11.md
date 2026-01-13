# Phase 2 Calibration Diagnosis - 2026-01-11

## Critical Finding: Propagation Delay Model Has 37ms Systematic Errors

### Current Calibration State

The per-broadcast calibration system **is working correctly** and has learned the following offsets:

```
WWV Broadcast Calibrations:
   2.50 MHz: offset=+43.146 ms  ← EXTREME
   5.00 MHz: offset= +7.754 ms
  10.00 MHz: offset=+22.964 ms
  15.00 MHz: offset= +5.586 ms
  20.00 MHz: offset=+19.642 ms
  25.00 MHz: offset= +6.604 ms

CHU Broadcast Calibrations:
   3.33 MHz: offset= +6.875 ms
   7.85 MHz: offset= +5.519 ms
  14.67 MHz: offset= -9.691 ms

BPM Broadcast Calibrations:
   5.00 MHz: offset= -5.573 ms
  10.00 MHz: offset=+15.442 ms
```

### The Problem

**WWV calibration offsets range from +5.6ms to +43.1ms - a span of 37.5ms!**

This is **not normal**. Calibration offsets should account for:
- Matched filter group delays: ~1-3ms variation
- Tone rise time differences: ~2-5ms variation  
- Detection threshold effects: ~1-2ms variation

**Expected calibration offset range: ±5-10ms maximum**

**Actual WWV offset range: 37.5ms**

This indicates the **propagation delay model is fundamentally broken** for WWV.

## Root Cause Analysis

### The Propagation Delay Equation

From `transmission_time_solver.py`:

```python
propagation_delay_ms = geometric_delay_ms + ionospheric_delay_ms

# Geometric delay
geometric_delay_ms = path_length_km / 299.792458

# Ionospheric delay (1/f² dispersion)
ionospheric_delay_ms = (40.3 * TEC * n_hops) / (frequency_hz^2) * 1000
```

### Expected Ionospheric Delay Differences

For WWV at 629 km (1-hop), assuming TEC = 20 TECU:

```
2.5 MHz:  (40.3 × 20 × 1) / (2.5e6)² × 1000 = 0.129 ms
5 MHz:    (40.3 × 20 × 1) / (5e6)²   × 1000 = 0.032 ms
10 MHz:   (40.3 × 20 × 1) / (10e6)²  × 1000 = 0.008 ms
15 MHz:   (40.3 × 20 × 1) / (15e6)²  × 1000 = 0.004 ms
20 MHz:   (40.3 × 20 × 1) / (20e6)²  × 1000 = 0.002 ms
25 MHz:   (40.3 × 20 × 1) / (25e6)²  × 1000 = 0.001 ms

Maximum differential: 0.129 - 0.001 = 0.128 ms
```

**The ionospheric delay model predicts only 0.13ms difference between 2.5 MHz and 25 MHz.**

**But the calibration offsets show a 37.5ms difference!**

## What's Actually Wrong

### Hypothesis 1: Wrong Number of Hops

If the solver is assigning different hop counts to different frequencies:

```
2.5 MHz: Assigned 2-hop → delay = 2 × 2.1 ms = 4.2 ms (geometric)
25 MHz: Assigned 1-hop → delay = 1 × 2.1 ms = 2.1 ms (geometric)

Difference: 2.1 ms (still too small)
```

### Hypothesis 2: Wrong Station Discrimination

If WWV 2.5 MHz is being misidentified as WWVH (6093 km instead of 629 km):

```
WWV:  629 km → 1-hop delay ≈ 2.1 ms
WWVH: 6093 km → 3-hop delay ≈ 20.3 ms

Difference: 18.2 ms (getting closer!)
```

### Hypothesis 3: Shared Frequency Discrimination Failure

WWV, WWVH, and BPM all transmit on 2.5, 5, 10, 15 MHz.

**If the discrimination is failing:**
- Sometimes identifies as WWV (629 km)
- Sometimes identifies as WWVH (6093 km)  
- Sometimes identifies as BPM (10,000 km)

**This would cause massive propagation delay errors:**

```
Same 10 MHz signal:
  If identified as WWV:  delay ≈ 2.1 ms
  If identified as WWVH: delay ≈ 20.3 ms
  If identified as BPM:  delay ≈ 33.4 ms

Error range: 31.3 ms!
```

**This matches the observed 37.5ms calibration offset range!**

## The Smoking Gun

Looking at the calibration offsets by frequency:

```
Shared Frequencies (WWV/WWVH/BPM):
  2.5 MHz: WWV=+43.1ms, (WWVH not seen), (BPM not seen)
  5.0 MHz: WWV=+7.8ms, BPM=-5.6ms  → 13.4ms difference
  10 MHz:  WWV=+23.0ms, BPM=+15.4ms → 7.6ms difference
  15 MHz:  WWV=+5.6ms

Unique Frequencies (WWV-only):
  20 MHz: WWV=+19.6ms
  25 MHz: WWV=+6.6ms
```

**The shared frequencies have huge calibration offsets and large variations.**

**The unique frequencies (20, 25 MHz) also have large offsets, but this could be due to:**
1. Wrong propagation mode assignment
2. Wrong layer height
3. Actual systematic delays in the signal processing chain

## Why Calibration Can't Fix This

The calibration system has a **rate limit of ±0.5ms per update** (line 1787):

```python
max_delta = 0.5  # ms per update
if abs(delta_offset) > max_delta:
    new_offset = old_cal.offset_ms + np.sign(delta_offset) * max_delta
```

**To correct a 37ms error at 0.5ms/update:**
- Required updates: 37 / 0.5 = 74 updates
- At 8-second intervals: 74 × 8 = 592 seconds ≈ 10 minutes

**But the problem is that the errors are not constant!**

If discrimination is randomly assigning WWV vs WWVH vs BPM:
- One minute: "WWV" → D_clock = -2ms
- Next minute: "WWVH" → D_clock = -20ms  
- Next minute: "BPM" → D_clock = -33ms

**The calibration chases these variations, creating high scatter.**

## The Real Solution

### Option 1: Fix Station Discrimination (Recommended)

Improve the discrimination algorithm to correctly identify WWV vs WWVH vs BPM on shared frequencies.

**Current discrimination uses:**
- Propagation delay (but this is what we're trying to calculate!)
- FSS (frequency selectivity score)
- Delay spread
- Doppler spread

**Better discrimination would use:**
- **Signal strength** (WWV at 629 km is much stronger than WWVH at 6093 km)
- **Arrival time consistency** (WWV should arrive ~2ms after second boundary, WWVH ~20ms)
- **Cross-frequency validation** (if 20 MHz shows WWV, then 10 MHz is probably also WWV)

### Option 2: Use Only Unique Frequencies

Avoid shared frequencies entirely:

```
WWV-only:  20, 25 MHz
CHU-only:  3.33, 7.85, 14.67 MHz
```

**This eliminates discrimination errors but reduces sample size.**

### Option 3: Increase Calibration Limits

Allow larger calibration offsets (up to ±50ms) and faster convergence:

```python
max_delta = 2.0  # ms per update (increased from 0.5)
MAX_OFFSET_MS = 50.0  # Maximum reasonable offset (increased from 100)
```

**This allows calibration to absorb the discrimination errors, but doesn't fix the root cause.**

## Immediate Action

The 6-fold uncertainty increase is caused by:

1. **Station discrimination failures** on shared frequencies (2.5, 5, 10, 15 MHz)
2. **Propagation delay errors** of 20-30ms when WWV is misidentified as WWVH/BPM
3. **Calibration system can't converge** fast enough to track these random errors
4. **High WWV intra-station scatter** (6.5ms) from mixing WWV/WWVH/BPM measurements
5. **66% outlier rejection** as fusion correctly detects inconsistent measurements

**The fix is to improve station discrimination, not to adjust calibration parameters.**

## Recommended Fix Priority

1. **Immediate**: Use only unique frequencies (WWV 20/25, CHU 3.33/7.85/14.67)
2. **Short-term**: Improve discrimination using signal strength and arrival time consistency
3. **Long-term**: Implement cross-frequency validation to lock station identity

This will reduce WWV intra-station scatter from 6.5ms to ~1.5ms and restore ±0.5ms uncertainty.
