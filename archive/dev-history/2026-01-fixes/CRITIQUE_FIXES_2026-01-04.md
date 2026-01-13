# Theoretical & Methodological Fixes - 2026-01-04

## Summary
Comprehensive fixes addressing theoretical errors, methodological issues, and missed opportunities identified in the RTP → Chrony pipeline critique.

## Immediate Priority Fixes (Deployed)

### 1. Propagation Delay Bounds Checks ✅
**File:** `transmission_time_solver.py`

**Problem:** No validation that propagation delays are physically reasonable. Sign errors could cause 20-40ms systematic errors.

**Fix:**
- Added bounds check: 0 < geometric_delay < 100ms
- Added bounds check: 5 < total_delay < 120ms
- Reject negative delays (sign error detection)
- Log warnings for suspicious values

**Impact:** Prevents catastrophic sign errors in D_clock calculation

---

### 2. RTP Wrap-Around Validation ✅
**File:** `data_models.py`

**Problem:** 32-bit RTP timestamp wraps every ~24 hours. No validation that elapsed time is reasonable.

**Fix:**
- Added check: |elapsed_time| < 120 seconds
- Raise ValueError if wrap-around detected
- Log critical error with diagnostic info

**Impact:** Prevents 24-hour timing errors from wrap-around

---

### 3. Complete Uncertainty Budget (In Progress)
**File:** `multi_broadcast_fusion.py`

**Missing Components:**
1. Tone detection uncertainty (~0.2-0.5ms, SNR-dependent)
2. GPSDO stability (Allan deviation)
3. Multipath delay spread (~1-5ms)
4. Ionospheric variability within mode

**Current Status:** Implementing enhanced uncertainty calculation

---

## High Priority Fixes (In Progress)

### 4. Adaptive Cross-Station Threshold
**Problem:** Fixed 1.0ms threshold doesn't account for ionospheric conditions

**Solution:** Dynamic threshold based on:
- Geomagnetic activity (Kp index)
- Time of day (day/night)
- Base threshold scaled by conditions

```python
base_threshold = 0.5  # ms
kp_factor = 1.0 + 0.5 * kp_index
time_factor = 1.5 if is_nighttime else 1.0
threshold = base_threshold * kp_factor * time_factor
```

---

### 5. Mode Plausibility Checks
**Problem:** No validation that identified propagation mode is physically reasonable

**Solution:** Add checks for:
- E-layer only exists during daytime
- Ground wave limited to <500km
- Mode must match expected for time/distance

---

### 6. VTEC-Based Propagation Refinement
**Problem:** VTEC read but not used to refine propagation delay

**Solution:** Use GNSS VTEC to correct ionospheric delay:
```python
tec_correction_ms = vtec_tecu * 0.16 / (frequency_mhz / 10.0)**2
t_prop_corrected = geometric_delay + tec_correction_ms
```

---

## Medium Priority Enhancements

### 7. Multi-Frequency Dispersion Validation
Use ionospheric dispersion between frequencies to detect tone misidentification

### 8. Frequency-Dependent Calibration
Key calibration by (station, frequency) instead of station only

### 9. GPSDO Holdover Mode
Graceful degradation when GPS lock lost

### 10. Ionospheric Storm Detection
Monitor TEC rate of change to flag disturbed conditions

---

## Deployment Plan

1. ✅ Deploy immediate fixes (propagation bounds, RTP validation)
2. 🔄 Complete uncertainty budget
3. 🔄 Implement adaptive threshold
4. 🔄 Add mode plausibility checks
5. 🔄 Integrate VTEC refinement
6. Test all fixes in production
7. Monitor for 24 hours
8. Document results

---

## Expected Improvements

- **Reliability:** Prevent catastrophic errors from sign inversion, wrap-around
- **Accuracy:** Better uncertainty quantification, VTEC-refined delays
- **Robustness:** Adaptive thresholds reduce false positives during storms
- **Traceability:** Complete ISO GUM-compliant uncertainty budget
