# Detection Methodology Critical Review

**Date:** 2026-02-05  
**Reviewer:** Cascade AI  
**Status:** In Progress

---

## Executive Summary

This document reviews the hf-timestd detection methodology as requested in `CRITIC_CONTEXT.md`. The review examines redundancy, circularity, template optimization, SNR thresholds, missed opportunities, and edge cases.

---

## 1. Redundancy Analysis: Path A vs Path B

### Current Architecture

| Path | Component | Purpose | Output |
|------|-----------|---------|--------|
| **Path A** | `_measure_tone_at_known_time()` | Minute marker detection (800ms/500ms tones) | 1 measurement/minute |
| **Path B** | `TickMatchedFilter` | Per-second tick analysis (5ms ticks) | 55 measurements/minute |

### Assessment: **NOT REDUNDANT - Complementary**

**Path A (Minute Marker):**
- High SNR due to long integration (800ms template = 19,200 samples at 24kHz)
- Single measurement per minute
- Optimal for initial lock and high-confidence timing
- Template energy: ~160x greater than 5ms tick

**Path B (Tick Analysis):**
- Lower per-tick SNR but 55 measurements per minute
- Provides timing drift tracking within minute
- Robust to single-tick dropouts
- Enables Doppler estimation from phase drift

**Recommendation:** Keep both paths. They serve different purposes:
- Path A: High-confidence minute boundary establishment
- Path B: Intra-minute drift tracking and redundancy

---

## 2. Circularity Analysis

### Question: Does expected arrival time depend on measurements that themselves depend on expected arrival time?

### Assessment: **NO CIRCULARITY - Physics-First Design**

The architecture explicitly avoids circularity:

1. **Expected arrival** comes from `ArrivalPatternMatrix`:
   - Great circle distance (fixed geometry)
   - IRI-2020 ionospheric model (external physics model)
   - Speed of light propagation
   - **NOT** from previous measurements

2. **Measurements** are validated against physics predictions:
   - Detection within ±100ms of expected → accepted
   - Detection outside tolerance → rejected
   - No feedback from measurements to expected values

3. **TEC feedback** (optional):
   - Measured TEC can refine ionospheric model
   - But this is a **refinement**, not a circular dependency
   - Initial predictions work without any measurements

**Code Evidence:**
```python
# arrival_pattern_matrix.py line 24-30
# "We do NOT need historical measurements to know where to look for tones.
#  Historical data is for ARCHIVAL and POST-HOC ANALYSIS, not for operational
#  decisions."
```

**Recommendation:** Architecture is sound. No changes needed.

---

## 3. Template Matching Analysis

### Current Templates

| Station | Minute Marker | Per-Second Tick |
|---------|---------------|-----------------|
| WWV | 800ms @ 1000Hz | 5ms @ 1000Hz |
| WWVH | 800ms @ 1200Hz | 5ms @ 1200Hz |
| CHU | 500ms @ 1000Hz | 300ms @ 1000Hz (regular), 10ms (FSK/voice) |
| BPM | 300ms @ 1000Hz | 10ms (UTC), 100ms (UT1) |

### Issues Identified

**Issue 3.1: CHU Template Duration**
- `CRITIC_CONTEXT.md` states: "CHU uses 500ms template but transmits 300ms tones at most seconds"
- **Verification:** CHU transmits:
  - Second 0: 500ms tone (minute marker) ✓
  - Seconds 1-28, 30, 40-49: 300ms tones
  - Seconds 31-39: FSK (10ms ticks)
  - Seconds 50-59: Voice (10ms ticks)
  - Second 29: Silent

**Current Implementation (tick_matched_filter.py):**
```python
CHU_TEMPLATE = TickTemplate(
    tick_duration_ms=300.0,  # Default for regular seconds
    fsk_duration_ms=10.0,
    voice_duration_ms=10.0,
    regular_duration_ms=300.0,
)
```

**Assessment:** Templates are correctly configured. The 500ms is only for second 0 (minute marker), which is handled by Path A with appropriate duration.

**Issue 3.2: Template Window Function**
- Using Tukey window (α=0.1) for smooth edges
- This is appropriate for reducing spectral leakage
- **No change needed**

**Recommendation:** Templates are well-designed. Consider adding adaptive template selection based on measured signal characteristics (future enhancement).

---

## 4. SNR Threshold Analysis

### Current Thresholds

| Detection Type | Threshold | Rationale |
|----------------|-----------|-----------|
| Correlation SNR (general) | 8 dB | Lowered from 12dB for weak signals |
| BPM correlation SNR | 12 dB | Higher due to shorter template (100ms) |
| Tone SNR (FFT-based) | N/A (informational) | Not used for gating |

### Assessment: **THRESHOLDS NEED EMPIRICAL VALIDATION**

**Concerns:**
1. The 8 dB threshold was "lowered from 12dB for weak signals" - this suggests ad-hoc tuning
2. No documented false positive rate at 8 dB
3. No documented detection probability vs SNR curve

**Theoretical Analysis:**
- For matched filter with AWGN: P_fa = erfc(√(SNR_threshold))
- At 8 dB (6.3x ratio): P_fa ≈ 10^-4 per search window
- At 12 dB (15.8x ratio): P_fa ≈ 10^-8 per search window

**Recommendation:**
1. Collect empirical data on detection rate vs SNR
2. Document false positive rate at current thresholds
3. Consider adaptive thresholding based on noise floor estimation

---

## 5. Missed Opportunities

### 5.1 CHU FSK Decoding (Seconds 31-39)

**Current Status:** Separate `CHUFSKDecoder` exists but timing not integrated

**Opportunity:** FSK contains BCD-encoded time. Could provide:
- Absolute time verification (independent of propagation model)
- Timing from FSK transitions (sub-ms precision possible)

**Recommendation:** Integrate FSK timing into metrology pipeline

### 5.2 WWV/WWVH BCD Decoding

**Current Status:** Not implemented

**Opportunity:** BCD subcarrier (100 Hz) encodes time-of-year
- Fragile due to low SNR
- But could provide absolute time verification

**Recommendation:** Low priority - FSK is more robust

### 5.3 Phase Tracking

**Current Status:** Phase computed but not used for timing

**Code Evidence (metrology_engine.py):**
```python
# Phase is extracted but only logged
phase_rad = np.arctan2(corr_sin[peak_idx], corr_cos[peak_idx])
```

**Opportunity:** Phase tracking could provide:
- Sub-sample timing refinement
- Doppler estimation
- Multipath detection (phase jumps)

**Recommendation:** Implement phase-based timing refinement (medium priority)

### 5.4 Doppler Estimation

**Current Status:** Computed in tick analysis but not used for ionospheric correction

**Opportunity:** Doppler shift indicates ionospheric motion
- Could predict short-term arrival time changes
- Could improve TEC estimation

**Recommendation:** Feed Doppler into arrival matrix (low priority)

---

## 6. Edge Cases

### 6.1 Silent Seconds (29, 30, 59)

**Current Handling:**
- `skip_seconds={0, 29, 59}` for WWV/WWVH
- `skip_seconds={0, 29}` for CHU
- `skip_seconds={0}` for BPM

**Assessment:** Correctly handled. No ticks expected at these seconds.

### 6.2 Leap Seconds

**Current Handling:** Not explicitly handled

**Risk:** During leap second insertion:
- Second 60 exists (or second 59 is extended)
- Tick patterns may differ

**Recommendation:** Add leap second awareness (check UTC-TAI offset)

### 6.3 Signal Fades Mid-Minute

**Current Handling:**
- Path A: Single measurement, no recovery if minute marker missed
- Path B: 55 windows, robust to individual tick dropouts

**Assessment:** Path B provides adequate robustness

**Recommendation:** Consider fallback to tick-only timing if minute marker fails

---

## 7. Summary of Recommendations

### High Priority
1. **Empirically validate SNR thresholds** - Document detection probability and false positive rate
2. **Add leap second handling** - Check UTC-TAI offset and adjust tick patterns

### Medium Priority
3. **Integrate CHU FSK timing** - Use FSK transitions for additional timing measurements
4. **Implement phase-based timing refinement** - Sub-sample precision improvement

### Low Priority
5. **Feed Doppler into arrival matrix** - Ionospheric motion prediction
6. **Adaptive template selection** - Based on measured signal characteristics

---

## 8. Conclusion

The detection methodology is fundamentally sound:
- **No circularity** - Physics-first design
- **No harmful redundancy** - Path A and B are complementary
- **Templates are appropriate** - Station-specific configurations correct

Main areas for improvement:
- Empirical validation of thresholds
- Edge case handling (leap seconds)
- Integration of additional timing sources (FSK, phase)

---

*Review conducted as part of hf-timestd critical analysis session 2026-02-05*
