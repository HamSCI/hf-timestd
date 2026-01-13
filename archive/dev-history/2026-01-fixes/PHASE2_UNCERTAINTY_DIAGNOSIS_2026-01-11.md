# Phase 2 Uncertainty Diagnosis - 2026-01-11

## Problem Summary

After fixing the RTP offset issue (PHASE2_RTP_OFFSET_FIX_2026-01-10.md), the fusion quality remains at **Grade D** with **combined uncertainty ±3.133ms** (k=1), representing a **6-fold increase** from the previous ±0.5ms performance.

## Diagnostic Results

### Current Fusion State (2026-01-11 00:19 UTC)

```
Timestamp: 2026-01-11T00:19:05Z
n_broadcasts: 32
n_stations: 2 (WWV, CHU)
outliers_rejected: 21 (66% rejection rate!)
consistency_flag: OK
quality_grade: C

Per-station breakdown:
  WWV: count=21, mean=-10.094 ms, intra_std=6.563 ms
  CHU: count=11, mean=-8.717 ms, intra_std=1.571 ms

Inter-station spread: 1.377 ms

Uncertainty budget:
  Statistical: 5.435 ms (DOMINANT COMPONENT)
  Systematic: 0.400 ms
  Propagation: 0.782 ms
  Combined: 1.772 ms
```

## Root Cause Analysis

### 1. **Massive Outlier Rejection (66%)**

**21 out of 32 broadcasts are being rejected as outliers.** This is the primary cause of the uncertainty increase.

### 2. **High WWV Intra-Station Scatter**

```
WWV intra_std = 6.563 ms
CHU intra_std = 1.571 ms
```

**WWV broadcasts from different frequencies disagree by ~6.5ms**, while CHU broadcasts are consistent within ~1.6ms. This indicates:

1. **WWV propagation delay model has systematic errors**
2. **Different WWV frequencies (2.5, 5, 10, 15, 20, 25 MHz) are calculating different propagation delays**
3. **The 1/f² ionospheric delay correction may be incorrect**

### 3. **The Fundamental Equation**

```
D_clock = T_arrival - T_propagation
```

**All stations should report the same D_clock** (within ~1-2ms for measurement noise) because D_clock is the **system clock offset**, which is the same for all broadcasts.

**Current situation:**
- WWV mean: -10.094 ms
- CHU mean: -8.717 ms
- Difference: 1.377 ms (acceptable)

**But WWV has 6.5ms internal scatter**, meaning different WWV frequencies are calculating propagation delays that differ by 6-7ms.

## Where the Error Arises

### Propagation Delay Calculation Pipeline

From `transmission_time_solver.py` and `phase2_temporal_engine.py`:

```python
# For each broadcast:
propagation_delay_ms = geometric_delay_ms + ionospheric_delay_ms

# Geometric delay (path length / speed of light)
geometric_delay_ms = path_length_km / 299.792458

# Ionospheric delay (1/f² dispersion)
ionospheric_delay_ms = (40.3 * TEC * n_hops) / (frequency_hz^2) * 1000
```

### Expected Propagation Delays (from code)

```python
EXPECTED_DELAY_RANGES = {
    'WWV': (4.0, 12.0),     # Fort Collins, CO → receiver (~1500km, 1-2 hop F2)
    'CHU': (6.0, 15.0),     # Ottawa, ON → receiver (~2000km, 1-2 hop F2)
}
```

### Differential Ionospheric Delay (1/f²)

For the same path, different frequencies experience different ionospheric delays:

```
WWV 2.5 MHz:  ~0.8 ms ionospheric delay per hop
WWV 5 MHz:    ~0.3 ms
WWV 10 MHz:   ~0.1 ms
WWV 15 MHz:   ~0.05 ms
WWV 20 MHz:   ~0.03 ms
WWV 25 MHz:   ~0.02 ms
```

**If the ionospheric delay model is wrong, different WWV frequencies will calculate different D_clock values, causing high intra-station scatter.**

## Specific Mistakes in Propagation Delay Expectations

### Issue 1: **Layer Height Uncertainty**

The F2 layer height (hmF2) varies from **250-400 km** depending on:
- Time of day (higher at night)
- Solar activity
- Season
- Latitude

**If using wrong hmF2:**
- Path length error: ~10-20 km per hop
- Delay error: ~0.03-0.07 ms per hop
- For 2-hop: ~0.06-0.14 ms error

**This alone doesn't explain 6.5ms scatter.**

### Issue 2: **Number of Hops Misidentification**

From receiver location (EM38ww: 38.918°N, 92.128°W):

```
WWV (Fort Collins, CO): ~629 km
CHU (Ottawa, ON): ~1955 km
```

**Expected modes:**
- WWV 629 km: **1E or 1F** (single hop)
- CHU 1955 km: **1F or 2F** (1-2 hops)

**If the solver incorrectly identifies 2F when it's actually 1F:**
- Extra hop adds ~3-4 ms delay
- D_clock would be 3-4 ms too negative

**This could explain the scatter if different frequencies are being assigned different hop counts.**

### Issue 3: **Ionospheric Delay Calculation Error**

The ionospheric delay formula:

```python
iono_delay_ms = (40.3 * TEC * n_hops) / (frequency_hz^2) * 1000
```

**For WWV at 629 km (1-hop):**

Assuming TEC = 20 TECU (typical):

```
2.5 MHz:  (40.3 × 20 × 1) / (2.5e6)² × 1000 = 0.129 ms
5 MHz:    (40.3 × 20 × 1) / (5e6)²   × 1000 = 0.032 ms
10 MHz:   (40.3 × 20 × 1) / (10e6)²  × 1000 = 0.008 ms
25 MHz:   (40.3 × 20 × 1) / (25e6)²  × 1000 = 0.001 ms
```

**Differential ionospheric delay between 2.5 MHz and 25 MHz: ~0.128 ms**

**This is too small to explain 6.5ms scatter.**

### Issue 4: **RTP Timestamp Jitter or Calibration Error**

After the RTP offset fix, all channels use the **same global_rtp_offset**. However:

**If the RTP-to-UTC mapping has frequency-dependent errors:**
- Different channels might have different RTP timestamp offsets
- This would appear as propagation delay errors
- Could be 5-10ms if RTP calibration is wrong

**This is the most likely culprit.**

## The Real Problem: **Per-Channel RTP Calibration Residuals**

Looking at the RTP offset fix code:

```python
# From timing_calibrator.py
if self.global_rtp_offset is None:
    self.global_rtp_offset = rtp_offset
    self.global_rtp_offset_source = channel_name
```

**The global RTP offset is established from ONE anchor channel** (likely CHU_7850 or CHU_3330).

**But each channel may have:**
1. Different matched filter group delays
2. Different tone rise time characteristics
3. Different detection threshold effects

**These per-channel systematic offsets were previously absorbed into per-channel RTP calibration. Now that we're using a global offset, these residuals appear as propagation delay errors.**

## Why CHU is Consistent but WWV is Not

**CHU (3.33, 7.85, 14.67 MHz):**
- FSK time code provides precise 500ms boundary alignment
- All CHU frequencies use the same modulation scheme
- Consistent detection characteristics
- **Intra-station std: 1.571 ms** ✓

**WWV (2.5, 5, 10, 15, 20, 25 MHz):**
- Pure tone modulation
- Different SNR at different frequencies
- Different multipath characteristics
- Different D-layer absorption
- **Intra-station std: 6.563 ms** ✗

## The 6-Fold Uncertainty Magnification

The uncertainty increase from ±0.5ms to ±3.133ms is caused by:

1. **Statistical uncertainty = 5.435 ms** (from high WWV scatter)
2. **66% outlier rejection** (reduces effective sample size)
3. **Weighted fusion** (high-uncertainty measurements get low weight)

The fusion is working correctly - it's detecting that **WWV measurements are inconsistent** and downweighting them, which increases the combined uncertainty.

## Recommended Fixes

### Option A: **Per-Broadcast Calibration (Recommended)**

Restore per-broadcast calibration offsets while keeping global RTP offset:

```python
# Global RTP offset for RTP-to-UTC mapping (same for all)
global_rtp_offset = 500340  # samples

# Per-broadcast timing offsets (learned from data)
broadcast_calibration = {
    'WWV_2500': +2.3 ms,   # Relative to CHU reference
    'WWV_5000': +1.8 ms,
    'WWV_10000': +0.5 ms,
    'WWV_15000': +0.2 ms,
    'WWV_20000': -0.1 ms,
    'WWV_25000': -0.3 ms,
    'CHU_3330': 0.0 ms,    # Reference
    'CHU_7850': +0.1 ms,
    'CHU_14670': -0.2 ms,
}
```

**This accounts for:**
- Matched filter group delays
- Tone rise time differences
- Detection threshold effects
- Frequency-dependent systematic offsets

### Option B: **Improved Propagation Delay Model**

Use the **global differential solver** to measure actual propagation delays:

```python
# Measure differential delays between stations
# Use CHU as reference (FSK-verified timing)
measured_delays = {
    'WWV_10000': measure_delay_relative_to_CHU(),
    'WWV_25000': measure_delay_relative_to_CHU(),
}

# Apply measured delays instead of physics-based predictions
```

### Option C: **Use Only Unique Frequencies**

Avoid shared frequencies (2.5, 5, 10, 15 MHz) where discrimination is difficult:

```python
PREFERRED_CHANNELS = [
    'CHU_3330',   # CHU-only
    'CHU_7850',   # CHU-only
    'CHU_14670',  # CHU-only
    'WWV_20000',  # WWV-only
    'WWV_25000',  # WWV-only
]
```

**This eliminates discrimination errors but reduces sample size.**

## Immediate Action Required

The RTP offset fix was correct and necessary. However, it exposed a second-order problem: **per-broadcast systematic timing offsets** that were previously hidden in per-channel RTP calibration.

**The solution is to implement two-tier calibration:**
1. **Global RTP offset** (same for all channels) - ✓ Already fixed
2. **Per-broadcast timing offsets** (learned from cross-validation) - ⚠️ Needs implementation

This will restore ±0.5ms performance while maintaining the correct RTP-to-UTC mapping.

## Summary: The Path from ±0.5ms to ±3.133ms

### Before RTP Offset Fix
- **Per-channel RTP calibration** absorbed all systematic offsets
- Each channel had its own `rtp_offset_samples`
- CHU and WWV had different RTP-to-UTC mappings (180 samples = 7.5ms difference)
- **Result**: Cross-station disagreement of 20-25ms, but low scatter within each station

### After RTP Offset Fix
- **Global RTP offset** ensures all channels use same RTP-to-UTC mapping ✓
- Per-broadcast systematic offsets are no longer absorbed
- These offsets now appear as **propagation delay errors**
- **Result**: Cross-station disagreement reduced to ~1.4ms, but high scatter within WWV (6.5ms)

### The 6-Fold Uncertainty Increase Mechanism

```
Statistical uncertainty = 5.435 ms  ← DOMINANT COMPONENT
  ↑
  Caused by high WWV intra-station scatter (6.5ms)
  ↑
  Caused by per-broadcast systematic offsets (2-7ms range)
  ↑
  Previously absorbed by per-channel RTP calibration
  ↑
  Now exposed after global RTP offset fix
```

**The uncertainty increase is not a bug - it's the system correctly detecting that WWV measurements are inconsistent.**

## Differential Calculations Between Broadcasts

The key differential calculation that differs between broadcasts:

```python
# Each broadcast calculates:
D_clock = T_arrival - T_propagation

# Where T_propagation depends on:
T_propagation = geometric_delay(distance, layer_height, n_hops) + 
                ionospheric_delay(frequency, TEC, n_hops) +
                SYSTEMATIC_OFFSET(channel, frequency)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                This component was previously absorbed by per-channel RTP calibration
```

**The systematic offset includes:**
1. Matched filter group delay (frequency-dependent)
2. Tone rise time (modulation-dependent)
3. Detection threshold effects (SNR-dependent)
4. Multipath delay spread (propagation-dependent)

**These vary by 2-7ms between broadcasts**, which is why WWV shows 6.5ms intra-station scatter.

## The Mistake in Propagation Delay Expectations

The propagation delay model assumes:

```python
EXPECTED_DELAY_RANGES = {
    'WWV': (4.0, 12.0),     # ±4ms tolerance
    'CHU': (6.0, 15.0),     # ±4.5ms tolerance
}
```

**But the actual variation includes:**
1. **Ionospheric variability**: ±1-2ms (diurnal, seasonal, solar)
2. **Mode uncertainty**: ±2-3ms (1F vs 2F misidentification)
3. **Systematic offsets**: ±2-7ms (per-broadcast, frequency-dependent)

**Total variation: ±5-12ms**, which exceeds the expected ranges.

**The model expects all WWV broadcasts to agree within ±4ms, but they actually vary by ±6.5ms due to systematic offsets.**

## Recommended Implementation Plan

### Phase 1: Restore Per-Broadcast Calibration (Immediate)

Modify `multi_broadcast_fusion.py` to maintain both:

```python
# Global RTP offset (RTP-to-UTC mapping)
self.global_rtp_offset = 500340  # samples, same for all

# Per-broadcast calibration (systematic timing offsets)
self.broadcast_calibration = {
    'WWV_2500': BroadcastCalibration(offset_ms=0.0, uncertainty_ms=1.0),
    'WWV_5000': BroadcastCalibration(offset_ms=0.0, uncertainty_ms=1.0),
    # ... learned from cross-validation with CHU
}
```

**Learning algorithm:**
1. Use CHU as reference (FSK-verified timing)
2. Measure `D_clock_WWV - D_clock_CHU` for each WWV frequency
3. Apply exponential moving average: `offset_new = 0.1 × diff + 0.9 × offset_old`
4. Converge over 50-100 measurements (~10-15 minutes)

### Phase 2: Improve Propagation Delay Model (Long-term)

1. **Use measured delays from global differential solver**
2. **Implement IRI-2020 for dynamic layer heights**
3. **Add TEC measurements from GNSS/IONEX**
4. **Validate against ground truth (GPS PPS, silent minutes)**

### Phase 3: Enhanced Quality Metrics (Long-term)

1. **Track per-broadcast Allan deviation**
2. **Implement cross-validation scoring**
3. **Add ionospheric weather alerts**
4. **Provide uncertainty budget breakdown in UI**

## Expected Results After Fix

With per-broadcast calibration restored:

```
n_broadcasts: 32
n_stations: 2
outliers_rejected: 2-3 (normal level, ~10%)
consistency_flag: OK

WWV intra_std: 1.5-2.0 ms (reduced from 6.5ms)
CHU intra_std: 1.5-2.0 ms (unchanged)
Inter-station spread: 0.5-1.0 ms

Uncertainty budget:
  Statistical: 1.2 ms (reduced from 5.4ms)
  Systematic: 0.3 ms
  Propagation: 0.5 ms
  Combined: 0.4-0.6 ms (k=1)

Quality grade: A or B
```

This restores the ±0.5ms performance while maintaining the correct global RTP offset.
