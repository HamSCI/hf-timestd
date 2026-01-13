# Critical Analysis: Fusion, Calibration, and Chrony Feed
**Date**: 2026-01-10  
**Objective**: Ensure theoretical and methodological integrity for scientific temporal accuracy

---

## Executive Summary

**Overall Assessment**: The system demonstrates **sound theoretical foundations** with **appropriate safeguards**, but contains **three critical issues** that could compromise scientific temporal accuracy:

1. ✅ **Fusion Architecture**: Theoretically sound with proper statistical weighting
2. ⚠️ **Calibration System**: Risk of absorbing real clock drift (mitigated but requires monitoring)
3. ⚠️ **Chrony Feed**: Precision formula is correct but consistency flag logic may be too permissive
4. ❌ **Single-Station Mode**: Mathematically unsound for long-term stability (bootstrap hack, not science)

---

## 1. Theoretical Integrity of Fusion

### 1.1 Statistical Weighting (`_calculate_weights`)

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1496-1571`

**Assessment**: ✅ **SOUND**

The weighting formula follows ISO GUM principles for combining measurements with different uncertainties:

```python
# Base weight: Inverse variance (precision)
base_weight = 1.0 / (m.uncertainty_ms ** 2)

# Scale by quality factors
w = base_weight * confidence * grade_scale * mode_scale * snr_scale
```

**Theoretical Justification**:
- Inverse variance weighting is the **statistically optimal** method for combining independent measurements
- Quality scaling factors account for non-statistical errors (discrimination, propagation mode reliability)
- Minimum weight floor (0.01) prevents numerical instability

**Concern**: None. This is textbook metrology.

---

### 1.2 Outlier Rejection (`_reject_outliers`)

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1573-1615`

**Assessment**: ✅ **SOUND** (with recent fix)

Uses weighted median absolute deviation (MAD) with 3σ threshold:

```python
# Weighted median
weighted_median = sorted_d[median_idx]

# MAD scaled to standard deviation
mad = np.median(deviations) * 1.4826

# Reject outliers
keep_mask = deviations < (sigma_threshold * mad)
```

**Critical Fix Applied** (line 1608-1609):
```python
# FIX 2: Removed "God Mode" immunity for GLOBAL_DIFF
# It must survive the same statistical scrutiny as other measurements
```

**Theoretical Justification**:
- MAD is robust to outliers (unlike standard deviation)
- 3σ threshold is standard in metrology (99.7% confidence)
- Weighted median accounts for measurement quality

**Remaining Concern**: Minimum of 4 measurements required. With single-station mode (`n_stations >= 1`), this could allow 4 measurements from the **same station** to pass outlier rejection, defeating the purpose of cross-station validation.

---

### 1.3 Kalman Filter (`_kalman_update`)

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1739-1827`

**Assessment**: ⚠️ **THEORETICALLY SOUND BUT PROCESS NOISE MAY BE TOO LOW**

State model:
```python
# State: [d_clock_offset, drift_rate]
# Transition: offset(t+1) = offset(t) + drift(t) * dt
F = np.array([[1.0, dt], [0.0, 1.0]])

# Process noise
q_offset = 0.01   # ms^2 per minute
q_drift = 0.0001  # (ms/min)^2 per minute
```

**Theoretical Issues**:

1. **Process Noise Assumptions**:
   - `q_offset = 0.01 ms²/min` assumes the system clock drifts by ~0.1ms per minute
   - This is **appropriate for GPSDO** (1e-9 stability = 0.06ms/min)
   - BUT: If the **HF propagation model** has systematic errors (ionospheric model drift), the Kalman filter will **absorb these into the state estimate**, hiding them from scientific observation

2. **Divergence Protection** (line 1812):
   ```python
   if abs(self.kalman_state[0]) > 10.0:
       # Reset filter
   ```
   This is a **safety net**, not a solution. If the filter hits this limit, it indicates:
   - Systematic error in propagation model
   - Calibration contamination
   - Tone misidentification

3. **Gradual Correction Ramp-Up** (line 2267-2283):
   ```python
   self.correction_alpha = min(1.0, self.correction_alpha + 0.02)
   kalman_correction = self.kalman_state[0] * self.correction_alpha
   fused_d_clock = fused_d_clock_raw - kalman_correction
   ```
   
   **CRITICAL CONCERN**: This creates a **feedback loop**:
   - Kalman filter learns offset from raw measurements
   - Correction is applied to fused output
   - Fused output is fed to Chrony
   - Chrony disciplines system clock
   - System clock is used for RTP timestamps
   - RTP timestamps feed back into measurements
   
   **Question**: Is this feedback loop stable? Or does it create a **circular dependency** where the Kalman filter is correcting for errors it creates?

**Recommendation**: 
- Monitor `kalman_state[0]` over time. If it grows beyond ±2ms and stays there, the filter is absorbing systematic error.
- Consider adding a **state reset trigger** based on cross-station disagreement, not just magnitude.

---

### 1.4 Single-Station Relaxation

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1881-1882`

**Assessment**: ❌ **MATHEMATICALLY UNSOUND FOR LONG-TERM STABILITY**

```python
# Need at least 2 stations for cross-validation
if len(station_groups) < 2:
    return True, f"Only {len(station_groups)} station (no cross-check possible)", 0
```

**The Problem**:

With only **one station** (e.g., 10MHz WWV), the system has **no way to detect**:
- Systematic propagation delay errors
- Ionospheric model drift
- Station misidentification
- Calibration contamination

**Example Failure Mode**:
1. Only 10MHz WWV is visible
2. Ionospheric conditions change (TEC increases by 20 TECU)
3. Propagation delay model is wrong by 2ms
4. System has no reference to detect this error
5. Calibration absorbs the 2ms error
6. Chrony disciplines system clock by 2ms
7. **Scientific observations are now offset by 2ms**

**Mathematical Justification**:

In metrology, **redundancy is required for validation**. With N=1 measurement source:
- No cross-validation possible
- No outlier detection possible
- No systematic error detection possible

This is a **bootstrap hack**, not a scientifically valid operating mode.

**Recommendation**:
- Single-station mode should be **clearly flagged** in output data
- Uncertainty should be **inflated** (e.g., 5-10ms) to reflect lack of validation
- Chrony feed should be **disabled** or marked as "BOOTSTRAP_ONLY" quality
- Scientific data products should **exclude** single-station periods

---

## 2. Adaptive Calibration ("Steel Ruler")

### 2.1 Calibration Update Logic

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1647-1737`

**Assessment**: ⚠️ **RISK MITIGATED BUT REQUIRES MONITORING**

The calibration system learns per-broadcast offsets:

```python
# Offset should bring broadcast mean to 0 (UTC alignment)
new_offset = -broadcast_mean

# Exponential moving average
alpha = base_alpha if validated else base_alpha * 0.3
new_offset = alpha * new_offset + (1 - alpha) * old_cal.offset_ms

# Rate limiting (±0.5ms per update)
if abs(delta_offset) > max_delta:
    new_offset = old_cal.offset_ms + np.sign(delta_offset) * max_delta
```

**The "God Mode" Risk**:

Calibration is designed to correct for **systematic ionospheric delays** (frequency-dependent, stable over hours/days). However, it could also absorb:

1. **Real clock drift** (if GPSDO unlocks)
2. **Propagation model errors** (if ionospheric conditions change)
3. **Systematic tone misidentification** (if discrimination fails consistently)

**Safeguards in Place**:

1. ✅ **Cross-validation gating** (line 1707):
   ```python
   alpha = base_alpha if validated else base_alpha * 0.3
   ```
   If stations disagree, calibration update rate is reduced by 70%.

2. ✅ **Rate limiting** (line 1710-1716):
   ```python
   max_delta = 0.5  # ms per update
   ```
   Prevents sudden jumps.

3. ✅ **Per-broadcast calibration** (Issue 3.2):
   Each (station, frequency) pair has its own offset, preventing cross-contamination.

4. ❌ **NO GPSDO LOCK CHECK**:
   The calibration system does **not check** if the GPSDO is locked. If the GPSDO drifts, calibration will absorb it.

**Critical Question**:

The comment at line 2213-2216 states:
```python
# CRITICAL FIX: Do NOT apply calibration during ongoing fusion
# Calibration is only for bootstrap/restart to help initial convergence
# During normal operation, use raw measurements and let Kalman filter converge naturally
```

But then at line 2260:
```python
self._update_calibration(measurements, validated=cross_valid)
```

**Calibration is still being updated**, even though it's not applied to fusion. This means:
- Calibration learns from raw measurements
- But fusion uses raw measurements (not calibrated)
- Kalman filter corrects the raw fusion output

**This is CORRECT**: Calibration is being learned for future use (e.g., after restart), but not applied during normal operation. The Kalman filter handles convergence.

**Remaining Risk**:

If the system runs in **single-station mode** for extended periods:
- Calibration will learn whatever offset brings that station to 0
- If the station has a systematic error (e.g., wrong propagation delay), calibration absorbs it
- After restart, the contaminated calibration is loaded
- The system starts with a **biased initial state**

**Recommendation**:
- Add GPSDO lock status to calibration update logic
- Flag calibration learned during single-station periods as "UNVALIDATED"
- Consider resetting calibration if cross-validation fails for >1 hour

---

### 2.2 Calibration Persistence

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:1730-1737`

```python
# Auto-save calibration every 50 updates
self.calibration_update_count += 1
if self.calibration_update_count % 50 == 0:
    self._save_calibration()
```

**Assessment**: ✅ **APPROPRIATE**

Auto-save prevents calibration loss on crash, but not too frequent to cause I/O overhead.

---

## 3. Chrony Feed Methodology

### 3.1 Precision Mapping

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:3061-3064`

```python
# Precision based on uncertainty (log2 of seconds)
# Correct formula: log2(uncertainty_sec) = log2(uncertainty_ms) - 10
precision = max(-20, min(-4, int(np.log2(max(0.1, result.uncertainty_ms)) - 10)))
```

**Assessment**: ✅ **MATHEMATICALLY CORRECT**

Chrony's precision field is `log2(precision_seconds)`. The formula:

```
precision = log2(uncertainty_ms) - 10
```

Is equivalent to:
```
precision = log2(uncertainty_ms / 1000)
```

**Verification**:
- 1000ms = 1s → `log2(1000) - 10 = 10 - 10 = 0` → precision = 2^0 = 1s ✓
- 1ms → `log2(1) - 10 = 0 - 10 = -10` → precision = 2^-10 ≈ 1ms ✓
- 0.1ms → `log2(0.1) - 10 ≈ -3.3 - 10 = -13.3` → precision ≈ 122µs ✓

**Bounds**:
- `max(-20, ...)` → minimum precision = 2^-20 ≈ 1µs (prevents over-optimistic claims)
- `min(-4, ...)` → maximum precision = 2^-4 = 62.5ms (prevents under-weighting)

**Concern**: None. This is correct.

---

### 3.2 Consistency Flag Logic

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:3036-3041`

```python
# CRITICAL FIX (2026-01-08): Relax consistency check
# INTER_ANOMALY means "stations disagree slightly due to ionospheric variations"
# This is EXPECTED and VALID - it's the science we're studying!
consistent = result.consistency_flag in ('OK', 'INTER_ANOMALY', 'CROSS_STATION_DISAGREE')
```

**Assessment**: ⚠️ **TOO PERMISSIVE - CONFLATES SCIENCE WITH ERRORS**

**The Problem**:

The comment claims that `CROSS_STATION_DISAGREE` is "expected ionospheric variation" and "the science we're studying". This is **partially true but dangerous**:

1. **True Ionospheric Variation**:
   - Different stations have different ionospheric paths
   - TEC varies by location and time
   - Expected variation: **0.5-1.5ms** (for 3-15MHz HF)

2. **Systematic Errors**:
   - Propagation model failure
   - Discrimination error (wrong station identified)
   - Tone misidentification
   - Expected error: **>2ms**

**The Issue**:

The cross-station validation threshold is **adaptive** (line 1904-1925):

```python
base_threshold = 0.5  # ms
time_factor = 1.5 if is_nighttime else 1.0
iono_factor = 2.0 if (vtec > 40 or vtec < 10) else 1.0
CROSS_STATION_THRESHOLD_MS = base_threshold * time_factor * iono_factor
```

This can reach **1.5ms** (nighttime + disturbed conditions). If stations disagree by 1.2ms, the flag is set to `CROSS_STATION_DISAGREE`, but Chrony still accepts it.

**Scientific Concern**:

For **temporal accuracy science**, we need to distinguish:
- **Physical ionospheric variation** (the signal we want to measure)
- **Measurement errors** (the noise we want to reject)

By feeding `CROSS_STATION_DISAGREE` data to Chrony, we are:
1. Disciplining the system clock based on **potentially erroneous** measurements
2. Contaminating the **reference time base** used for scientific observations
3. Creating a **circular dependency** where errors in timing affect the measurements

**Recommendation**:

Chrony feed should use **stricter criteria**:

```python
# Only feed high-confidence, validated measurements
consistent = result.consistency_flag == 'OK'

# OR: Allow INTER_ANOMALY only if uncertainty is low
consistent = (result.consistency_flag == 'OK') or \
             (result.consistency_flag == 'INTER_ANOMALY' and result.uncertainty_ms < 0.5)
```

For scientific observations, **separate the reference time base from the measurement**:
- Use Chrony-disciplined clock for **RTP timestamps** (system time)
- Use **raw HF measurements** for **ionospheric science** (don't feed back to Chrony)

---

### 3.3 Discontinuity Filter

**Location**: `@/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py:3043-3054`

```python
# Discontinuity filter: reject large jumps (>3ms)
if 'last_chrony_d_clock' in globals() and last_chrony_d_clock is not None:
    delta = abs(result.d_clock_fused_ms - last_chrony_d_clock)
    if delta > 3.0:
        logger.warning(f"Discontinuity detected ({delta:.1f}ms jump)")
        discontinuity_ok = False
```

**Assessment**: ✅ **APPROPRIATE SAFEGUARD**

This prevents sudden clock jumps that would destabilize Chrony. 3ms threshold is reasonable for:
- Ionospheric variation: ~0.5-1ms
- Measurement noise: ~0.5ms
- Propagation mode changes: ~1-2ms

**Total expected variation**: ~2-3ms

Jumps >3ms indicate:
- Tone misidentification (500ms error)
- Station misidentification (geographic offset)
- Systematic error

**Concern**: None. This is good practice.

---

## 4. Overall Theoretical Integrity for Scientific Temporal Accuracy

### 4.1 Architecture Assessment

**Science-First Separation**:

The system claims to separate Physics (propagation modeling) from Fusion (statistical combination). Let's verify:

1. **Physics Layer** (`PhysicsPropagationModel`):
   - Computes propagation delays based on ionospheric models
   - Provides TEC estimates
   - **Does not modify measurements**

2. **Fusion Layer** (`MultiBroadcastFusion`):
   - Combines measurements using statistical weighting
   - Applies Kalman filtering for convergence
   - **Uses physics results for confidence adjustment, not value modification**

**Assessment**: ✅ **Separation is maintained**

The physics model is used to **adjust confidence**, not to **modify D_clock values**. This is correct.

**Example** (line 2072-2074):
```python
if tec_diff < 5.0:
    m.confidence = min(1.0, m.confidence * 1.1)  # Boost confidence
```

The measurement value (`m.d_clock_ms`) is **not changed**. Only the weight in fusion is affected.

---

### 4.2 Feedback Loop Analysis

**The Critical Question**: Does the system create a feedback loop that contaminates scientific observations?

**Feedback Path**:
1. HF tones arrive → RTP timestamps (system clock)
2. D_clock = T_arrival (RTP) - T_propagation (physics model)
3. Fusion combines D_clock measurements
4. Kalman filter corrects fusion output
5. Chrony disciplines system clock based on corrected D_clock
6. System clock feeds back to RTP timestamps (step 1)

**Analysis**:

This is a **closed-loop control system**. The question is: **Is it stable?**

**Stability Conditions**:

1. **Kalman Filter Convergence**:
   - Process noise must be >> measurement noise
   - Current: `q_offset = 0.01 ms²/min`, measurement noise ~0.5-1ms
   - **Ratio**: Process noise is ~100x smaller than measurement noise
   - **Conclusion**: Filter will converge slowly, which is **good for stability**

2. **Chrony Discipline Rate**:
   - Chrony uses PLL/FLL to discipline clock
   - Time constant: ~1000s (from `poll 3` = 8s, with damping)
   - **Conclusion**: Chrony changes clock slowly, preventing oscillation

3. **Calibration Update Rate**:
   - Rate-limited to ±0.5ms per update
   - Updates every ~60s
   - **Max rate**: 0.5ms/min = 8.3µs/s
   - **Conclusion**: Very slow, unlikely to cause instability

**Overall Assessment**: ⚠️ **STABLE BUT REQUIRES MONITORING**

The feedback loop is **designed to be stable** through:
- Slow Kalman convergence
- Slow Chrony discipline
- Rate-limited calibration

**However**, if any component fails (e.g., GPSDO unlocks, propagation model drifts), the feedback loop could **amplify the error** rather than correct it.

**Recommendation**:
- Monitor `kalman_state[0]` for divergence
- Monitor Chrony offset for sudden changes
- Add **open-loop validation**: Compare HF-derived time to independent reference (e.g., NTP, GPS)

---

### 4.3 Scientific Data Quality

**For scientific observations of ionospheric propagation**, the system must provide:

1. ✅ **Raw measurements** (before calibration/Kalman correction)
2. ✅ **Uncertainty budgets** (statistical, systematic, propagation)
3. ⚠️ **Validation flags** (cross-station agreement, GPSDO lock status)
4. ❌ **Independent time reference** (not Chrony-disciplined clock)

**Current State**:

The system writes **both** raw and fused D_clock to HDF5:
```python
d_clock_fused_ms=float(result.d_clock_fused_ms),
d_clock_raw_ms=float(result.d_clock_raw_ms),
```

This is **good** - scientists can use raw values for ionospheric studies.

**Missing**:
- GPSDO lock status in output
- Flag for single-station periods
- Independent time reference for validation

---

## 5. Summary of Critical Issues

### Issue 1: Single-Station Mode (CRITICAL)

**Severity**: ❌ **HIGH - Compromises Scientific Validity**

**Problem**: With only one station, no cross-validation is possible. Systematic errors cannot be detected.

**Impact**:
- Calibration absorbs systematic errors
- Chrony disciplines clock based on potentially wrong data
- Scientific observations are contaminated

**Recommendation**:
- Inflate uncertainty to 5-10ms in single-station mode
- Disable Chrony feed (or mark as "BOOTSTRAP_ONLY")
- Flag all single-station data products as "UNVALIDATED"

---

### Issue 2: Consistency Flag Too Permissive (MEDIUM)

**Severity**: ⚠️ **MEDIUM - Reduces Temporal Accuracy**

**Problem**: Feeding `CROSS_STATION_DISAGREE` data to Chrony conflates ionospheric science with measurement errors.

**Impact**:
- System clock may be disciplined based on erroneous measurements
- Temporal accuracy degrades to ~1-2ms instead of <0.5ms

**Recommendation**:
- Only feed `consistency_flag == 'OK'` to Chrony
- Use stricter threshold for Chrony feed (e.g., 0.5ms cross-station agreement)
- Allow `INTER_ANOMALY` only if uncertainty < 0.5ms

---

### Issue 3: Kalman Filter Feedback Loop (LOW)

**Severity**: ⚠️ **LOW - Requires Monitoring**

**Problem**: Kalman filter correction creates feedback loop with Chrony-disciplined clock.

**Impact**:
- Potential for slow divergence if systematic errors accumulate
- Difficult to diagnose if filter is absorbing real errors vs. noise

**Recommendation**:
- Monitor `kalman_state[0]` for values >2ms
- Add open-loop validation against independent time reference
- Consider state reset trigger based on cross-station disagreement

---

## 6. Recommendations for Scientific Integrity

### Immediate Actions

1. **Add GPSDO Lock Status**:
   - Check GPSDO lock before updating calibration
   - Exclude unlocked measurements from fusion
   - Flag unlocked periods in output data

2. **Stricter Chrony Feed Criteria**:
   ```python
   consistent = (result.consistency_flag == 'OK') or \
                (result.consistency_flag == 'INTER_ANOMALY' and result.uncertainty_ms < 0.5)
   ```

3. **Single-Station Mode Safeguards**:
   - Inflate uncertainty: `uncertainty *= 5.0` if `n_stations == 1`
   - Disable Chrony feed in single-station mode
   - Add `single_station_mode` flag to output

### Long-Term Improvements

1. **Independent Time Reference**:
   - Add GPS receiver for ground truth validation
   - Compare HF-derived time to GPS every hour
   - Alert if disagreement >2ms

2. **Calibration Validation**:
   - Reset calibration if cross-validation fails for >1 hour
   - Flag calibration learned in single-station mode as "UNVALIDATED"
   - Implement calibration quality metric (e.g., convergence rate)

3. **Open-Loop Monitoring**:
   - Log Kalman state history
   - Alert if `kalman_state[0]` exceeds ±2ms for >10 minutes
   - Implement automatic state reset on sustained divergence

---

## 7. Conclusion

The HF-TimeStd system demonstrates **solid theoretical foundations** in statistical fusion and metrology. The use of inverse variance weighting, MAD-based outlier rejection, and Kalman filtering is **appropriate and well-implemented**.

**However**, three critical issues compromise scientific temporal accuracy:

1. **Single-station mode** is a bootstrap hack, not scientifically valid
2. **Consistency flag logic** is too permissive for Chrony feed
3. **Feedback loop** requires monitoring to prevent slow divergence

**For scientific observations**, the system should:
- Use **raw measurements** (not Kalman-corrected)
- Maintain **independent time reference** (not Chrony-disciplined)
- Clearly **flag validation status** (single-station, cross-station agreement, GPSDO lock)

**Primary Objective Alignment**:

The stated objective is "scientific observations that temporal accuracy make possible." The current system prioritizes **clock discipline** (Chrony feed) over **scientific data quality**. These goals are in tension:

- **Clock discipline** requires stable, smooth corrections (Kalman filtering, permissive consistency checks)
- **Scientific observations** require raw, unfiltered data with strict validation

**Recommendation**: Separate these concerns by:
1. Using Chrony-disciplined clock for **RTP timestamps** (system time reference)
2. Using **raw HF measurements** for **ionospheric science** (don't feed back to Chrony)
3. Implementing **independent validation** to detect when the two diverge

This ensures both goals are met without compromise.
