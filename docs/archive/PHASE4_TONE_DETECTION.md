# Phase 4: Tone Detection Improvements - Technical Summary

**Added:** December 31, 2025  
**Status:** Deployed to production  
**Impact:** Improved selectivity (reduce false positives) and sensitivity (detect weaker signals)

## Three Core Improvements

### 1. Robust Noise Floor Estimation

**File:** `src/hf_timestd/core/tone_detector.py` (+75 lines)  
**Method:** `MultiStationToneDetector._estimate_robust_noise_floor()`

**Problem:** Traditional noise floor estimation uses all correlation samples, which can be contaminated by interference in the search region.

**Solution:** Use Median Absolute Deviation (MAD) on samples OUTSIDE the search region only.

```python
# Exclude search region
mask = np.ones(len(correlation), dtype=bool)
mask[search_start_idx:search_end_idx] = False
noise_samples = correlation[mask]

# MAD for outlier robustness
median = np.median(noise_samples)
mad = np.median(np.abs(noise_samples - median))
sigma_equivalent = 1.4826 * mad  # Convert to std equivalent
noise_floor = median + 3.0 * sigma_equivalent
```

**Expected Impact:** 5-10% improvement in weak signal detection

### 2. Adaptive Search Windows

**File:** `src/hf_timestd/core/tone_detector.py` (+72 lines)  
**Method:** `MultiStationToneDetector._calculate_adaptive_search_window()`

**Problem:** Wide search windows (±500ms) needed during acquisition but cause high false positive rates when system is locked.

**Solution:** Dynamically adjust window based on SNR and convergence state.

**Window Sizing Strategy:**

- ACQUIRING: ±500ms (no prior knowledge)
- LOCKED + High SNR (>20dB): ±5ms (100x narrower)
- LOCKED + Good SNR (>15dB): ±15ms (33x narrower)
- LOCKED + Medium SNR (>10dB): ±50ms (10x narrower)

**Expected Impact:** 10-20% reduction in false positives, faster convergence

### 3. Ionospheric Propagation Prediction

**File:** `src/hf_timestd/core/phase2_temporal_engine.py` (+105 lines)  
**Method:** `Phase2TemporalEngine._predict_propagation_delay()`

**Problem:** Search windows centered at minute boundary don't account for ionospheric propagation delay.

**Solution:** Use IRI-2020 model to predict F2 layer height and calculate expected arrival time.

**Physics:**

```
For 1-hop F-layer propagation:
    path_length = 2 × sqrt(hmF2² + (distance/2)²)
    delay = path_length / c
```

**Station Distances:**

- WWV (Fort Collins): ~1500 km → 5-10ms delay
- WWVH (Kauai): ~6000 km → 20-30ms delay
- CHU (Ottawa): ~1200 km → 4-8ms delay

**Expected Impact:** Search window centering within ±10ms, 15-25% reduction in false positives

## Combined Effect

**Initial Acquisition:**

- Search window: ±500ms around minute boundary
- Noise floor: Standard percentile method

**After Lock (High SNR):**

- Search window: ±5ms centered at predicted delay
- Noise floor: Robust MAD-based estimation
- **Total improvement:** 100x reduction in search space with better sensitivity

## Integration Points

### Convergence State (TODO)

The adaptive window method requires convergence state from `clock_convergence.py`:

```python
# In phase2_temporal_engine.py or analytics service:
convergence_state = self.clock_convergence.get_state()  # 'ACQUIRING', 'LOCKED', etc.
recent_snr_db = self.clock_convergence.get_recent_snr()

adaptive_window_ms = self.tone_detector._calculate_adaptive_search_window(
    recent_snr_db=recent_snr_db,
    convergence_state=convergence_state
)
```

### Ionospheric Prediction Usage (TODO)

The temporal engine should pass predicted delays to the detector:

```python
# Get ionospheric prediction
expected_offset_ms, uncertainty_ms = self._predict_propagation_delay(
    station='WWV',
    timestamp=datetime.now(timezone.utc)
)

# Pass to detector
detection = self.tone_detector.process_samples(
    timestamp=timestamp,
    samples=iq_samples,
    search_window_ms=adaptive_window_ms,
    expected_offset_ms=expected_offset_ms
)
```

## Testing

**Unit Tests:** `tests/test_tone_detector_improvements.py` (15 test cases)

- Robust noise floor with synthetic interference
- Adaptive windows for all SNR/state combinations
- Ionospheric prediction for all stations

**Integration Testing:** Process 24 hours of historical data to measure:

- Detection rate (should remain ≥95% of baseline)
- False positive rate (target: ≥20% reduction)
- Timing accuracy (target: ≥2ms improvement)

## References

- Rousseeuw, P.J. & Croux, C. (1993). "Alternatives to the Median Absolute Deviation." JASA.
- Kay, S.M. (1998). "Fundamentals of Statistical Signal Processing: Detection Theory."
- Davies, K. (1990). "Ionospheric Radio." Chapter 6: HF Propagation Prediction.
- Bilitza, D. et al. (2017). "International Reference Ionosphere 2016." Space Weather.
