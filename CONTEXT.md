# CONTEXT.md - AI Agent Briefing for Next Session

**Last Updated:** 2025-12-31 00:00 UTC  
**Current Version:** hf-timestd-3.2.0  
**System Status:** ✅ Healthy, all services running

---

## PRIMARY OBJECTIVE FOR NEXT SESSION

**Implement Phase 4: Tone Detection Selectivity & Sensitivity Improvements**

Enhance the tone detection system to improve selectivity (reduce false positives) and sensitivity (detect weaker signals) by implementing three high-priority improvements identified through critical analysis.

---

## BACKGROUND: WHAT WAS ACCOMPLISHED IN PREVIOUS SESSION

### Session 2025-12-30 Summary

**Completed:** Phases 1-3 of HDF5 Robustness & Ionospheric Model Improvements

**Code Changes:**
- `phase2_analytics_service.py`: +172 lines, -6 lines
- `hdf5_writer.py`: +87 lines  
- `ionospheric_model.py`: +54 lines, -9 lines
- **Total:** +313 lines, -15 lines

**Deployed:** 2025-12-30 23:49 UTC, all 9 analytics channels running successfully

**Key Improvements:**
1. **Input Validation** - Archive directory validation on startup
2. **HDF5 Failure Tracking** - Critical alerts after 10 consecutive failures
3. **Startup Health Checks** - Verify all HDF5 writers operational
4. **Calibration Memory Bounds** - LRU eviction (max 10 locations)
5. **Adaptive IRI Cache TTL** - 30 min daytime, 5 min nighttime

**Documentation Created:**
- `docs/HDF5_ROBUSTNESS_IMPROVEMENTS.md` - Comprehensive documentation
- Critical analysis of tone detection (8 improvements identified)
- Implementation plan for Phase 4

---

## PHASE 4: TONE DETECTION IMPROVEMENTS

### Critical Analysis Results

**Current Strengths:**
- ✅ Two-stage detection (matched filter + onset)
- ✅ Phase-invariant quadrature detection
- ✅ Sub-sample timing precision (~5μs at 20 kHz)
- ✅ Propagation bounds checking

**Identified Gaps:**
1. ❌ No SNR-based gating on search window
2. ❌ No multi-minute coherent integration
3. ❌ No Doppler compensation
4. ❌ No adaptive noise floor estimation
5. ❌ No ionospheric prediction for search window
6. ❌ No multipath discrimination
7. ❌ No frequency-domain validation
8. ❌ No temporal consistency check

### Three High-Priority Improvements to Implement

#### 1. SNR-Based Adaptive Search Windows

**Current:** Fixed ±500ms search window regardless of signal quality

**Problem:** Wide windows increase false positive rate in low SNR

**Solution:**
```python
def _calculate_adaptive_search_window(
    self, 
    recent_snr_db: Optional[float],
    detection_stability: str  # 'ACQUIRING', 'TRACKING', 'LOCKED'
) -> float:
    """
    Narrow search window as SNR improves and system converges.
    
    LOCKED + High SNR (>20 dB): ±5ms (very tight)
    TRACKING + Good SNR (>15 dB): ±15ms
    TRACKING + Medium SNR (>10 dB): ±50ms
    ACQUIRING or Low SNR: ±500ms (wide search)
    """
    if detection_stability == 'LOCKED' and recent_snr_db and recent_snr_db > 20:
        return 5.0
    elif detection_stability == 'TRACKING':
        if recent_snr_db and recent_snr_db > 15:
            return 15.0
        elif recent_snr_db and recent_snr_db > 10:
            return 50.0
    return 500.0
```

**Files to Modify:**
- `tone_detector.py`: Add `_calculate_adaptive_search_window()` method
- `phase2_temporal_engine.py`: Pass convergence state and SNR to detector

**Expected Impact:** 10-20% reduction in false positives

---

#### 2. Ionospheric Prediction for Search Window Centering

**Current:** Search centered at minute boundary (0ms offset)

**Problem:** We have IRI-2020 that predicts layer heights, but don't use it to predict propagation delay

**Solution:**
```python
def _predict_propagation_delay(
    self,
    station: StationType,
    timestamp: datetime
) -> Tuple[float, float]:
    """
    Predict propagation delay using IRI-2020 ionospheric model.
    
    Uses predicted hmF2 and station geometry to calculate expected delay.
    Centers search window at predicted arrival time.
    
    Returns:
        (expected_delay_ms, uncertainty_ms)
    """
    # Get predicted layer height from IRI-2020
    heights = self.iono_model.get_heights(timestamp, self.receiver_lat, self.receiver_lon)
    hmF2_km = heights.hmF2
    
    # Station distances (great circle)
    station_distances = {
        StationType.WWV: 1500,   # Fort Collins to central US (km)
        StationType.WWVH: 6000,  # Hawaii to central US (km)
        StationType.CHU: 1200    # Ottawa to central US (km)
    }
    distance_km = station_distances.get(station, 1500)
    
    # 1-hop F-layer geometry: path_length = 2 * sqrt(h² + (d/2)²)
    path_length_km = 2 * np.sqrt(hmF2_km**2 + (distance_km/2)**2)
    
    # Propagation delay
    c_km_per_ms = 299.792458
    expected_delay_ms = path_length_km / c_km_per_ms
    
    # Uncertainty from hmF2 uncertainty (±30km typical)
    uncertainty_ms = max(5.0, hmF2_uncertainty_calculation)
    
    return expected_delay_ms, uncertainty_ms
```

**Files to Modify:**
- `phase2_temporal_engine.py`: Add `_predict_propagation_delay()` method
- Update detector calls to pass `expected_offset_ms`

**Expected Impact:** 
- Tighter search windows (±5-15ms instead of ±500ms)
- 15-25% reduction in false positives
- Better multipath rejection

---

#### 3. Robust Noise Floor Estimation

**Current:** Fixed percentile of entire correlation output

**Problem:** Interference in search region elevates noise floor estimate

**Solution:**
```python
def _estimate_robust_noise_floor(
    self,
    correlation: np.ndarray,
    search_start_idx: int,
    search_end_idx: int
) -> float:
    """
    Robust noise floor using samples OUTSIDE search region.
    
    Uses Median Absolute Deviation (MAD) - robust to outliers.
    """
    # Exclude search region
    mask = np.ones(len(correlation), dtype=bool)
    mask[search_start_idx:search_end_idx] = False
    noise_samples = correlation[mask]
    
    # Use MAD for robustness
    median = np.median(noise_samples)
    mad = np.median(np.abs(noise_samples - median))
    sigma_equivalent = 1.4826 * mad
    
    # Noise floor = median + 3σ
    return median + 3 * sigma_equivalent
```

**Files to Modify:**
- `tone_detector.py`: Add `_estimate_robust_noise_floor()` method
- Update `_correlate_with_template()` to use robust estimation

**Expected Impact:** 5-10% improvement in weak signal detection

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (Day 1)
1. Add `_calculate_adaptive_search_window()` to `MultiStationToneDetector`
2. Add `_estimate_robust_noise_floor()` to `MultiStationToneDetector`
3. Update `_correlate_with_template()` to use robust noise floor

### Phase 2: Ionospheric Integration (Day 2)
4. Add `_predict_propagation_delay()` to `Phase2TemporalEngine`
5. Update temporal engine to pass predicted delays to detector
6. Test with historical data

### Phase 3: Adaptive Windows (Day 2-3)
7. Add convergence state tracking to temporal engine
8. Update detector calls to use adaptive search windows
9. Test and tune window thresholds

---

## KEY FILES TO UNDERSTAND

### Primary Implementation Files

1. **`src/hf_timestd/core/tone_detector.py`** (1497 lines)
   - `MultiStationToneDetector` class
   - Two-stage detection (matched filter + onset)
   - Quadrature templates for phase-invariant detection
   - **Add methods here:** `_calculate_adaptive_search_window()`, `_estimate_robust_noise_floor()`

2. **`src/hf_timestd/core/phase2_temporal_engine.py`** (2200+ lines)
   - `Phase2TemporalEngine` class
   - Convergence state machine ('ACQUIRING', 'TRACKING', 'LOCKED')
   - Calls tone detector with search windows
   - **Add method here:** `_predict_propagation_delay()`

3. **`src/hf_timestd/core/ionospheric_model.py`** (1224 lines)
   - `IonosphericModel` class
   - IRI-2020 integration for layer height prediction
   - **Use existing:** `get_heights()` method returns `LayerHeights` with `hmF2`

### Reference Documentation

1. **`docs/HDF5_ROBUSTNESS_IMPROVEMENTS.md`**
   - Comprehensive documentation of Phase 1-3 improvements
   - Code examples and rationale

2. **Artifacts in `.gemini/antigravity/brain/`:**
   - `tone_detection_critique.md` - Critical analysis (8 improvements)
   - `tone_detection_plan.md` - Detailed implementation plan
   - `implementation_plan.md` - Phase 1-3 plan (reference)

---

## CURRENT SYSTEM STATE

### Services Running
- ✅ `timestd-core-recorder` - Recording IQ samples to Digital RF
- ✅ `timestd-analytics` - 9 channels processing (WWV, WWVH, CHU)
- ✅ `timestd-fusion` - Fusing measurements, feeding Chrony
- ✅ Chrony TMGR source active (reach=37, offset=+151us)

### Data Flow
```
Radio → Core Recorder → Digital RF (L0)
                      ↓
              Analytics Service (9 channels)
                      ↓
              HDF5 Files (L1A, L1B, L2)
                      ↓
              Fusion Service
                      ↓
              Chrony SHM (TMGR source)
```

### Recent Changes (Deployed)
- Input validation on archive directory
- HDF5 write failure tracking with alerts
- Startup health checks for HDF5 writers
- Calibration memory bounds (LRU, max 10 locations)
- Adaptive IRI cache TTL (30 min / 5 min)

---

## VERIFICATION APPROACH

### Unit Tests to Write
1. Test adaptive search window calculation with various SNR/states
2. Test ionospheric delay prediction with known hmF2 values
3. Test robust noise floor with synthetic interference

### Integration Tests
1. Process 24 hours of historical data
2. Compare detection rates before/after
3. Measure false positive rate reduction
4. Verify timing accuracy improvement

### Metrics to Track
- **Detection Rate:** Should remain ≥95% of baseline
- **False Positive Rate:** Target 20-30% reduction
- **Search Window Size:** Should narrow to ±5-15ms when locked
- **Timing Accuracy:** Should improve by 2-5ms

---

## SUCCESS CRITERIA

**Minimum Viable:**
- [ ] Adaptive search windows implemented and working
- [ ] Robust noise floor reduces false positives by ≥10%
- [ ] No regression in detection rate

**Ideal:**
- [ ] Ionospheric prediction centers search within ±10ms
- [ ] False positive rate reduced by ≥20%
- [ ] Timing accuracy improved by ≥2ms
- [ ] System locks faster (fewer minutes to convergence)

---

## IMPORTANT NOTES

### Code Style
- Use type hints for all new methods
- Add comprehensive docstrings with theory/rationale
- Include debug logging for troubleshooting
- Follow existing patterns in tone_detector.py

### Testing Strategy
- Test each improvement independently first
- Use historical data for regression testing
- Monitor for 24 hours before declaring success

### Rollback Plan
- Keep changes modular and feature-flaggable
- Document baseline metrics before changes
- Have clear rollback procedure if issues arise

---

## QUESTIONS TO RESOLVE DURING IMPLEMENTATION

1. **Convergence State:** Where is it tracked in `phase2_temporal_engine.py`?
2. **SNR History:** How to access recent SNR values for adaptive windows?
3. **Receiver Location:** Where are lat/lon stored for ionospheric prediction?
4. **Search Window API:** Exact parameter names in `process_samples()`?

**Approach:** Grep for these patterns, examine existing code, then implement.

---

## ESTIMATED EFFORT

**Total:** 2-3 days, ~430 lines of new code

**Breakdown:**
- Day 1: Adaptive windows + robust noise floor (~150 lines)
- Day 2: Ionospheric prediction integration (~80 lines)
- Day 3: Testing, tuning, documentation (~200 lines tests)

---

## FINAL CHECKLIST BEFORE STARTING

- [ ] Read `tone_detection_critique.md` for full context
- [ ] Read `tone_detection_plan.md` for detailed algorithms
- [ ] Examine `tone_detector.py` structure and existing methods
- [ ] Examine `phase2_temporal_engine.py` detector call sites
- [ ] Understand `ionospheric_model.py` API for hmF2 prediction
- [ ] Have historical data ready for testing

**Ready to implement Phase 4!**
