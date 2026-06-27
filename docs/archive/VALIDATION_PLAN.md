# hf-timestd Validation Plan

## Purpose

Establish scientific credibility through systematic validation of ionospheric measurement capabilities against established ground truth sources.

## Validation Status

### ✅ Implemented (Code Complete)

- TEC estimation from multi-frequency measurements
- Doppler velocity/spread extraction
- FSS (D-layer absorption) measurement
- Propagation mode identification
- Science Aggregator service

### ⚠️ Pending Validation (No Ground Truth Comparison)

- TEC accuracy vs GPS TEC
- Doppler precision vs known sources
- Solar flare detection sensitivity/specificity
- TID detection threshold tuning
- Layer altitude accuracy

### ❌ Not Yet Implemented

- Automated event detection
- O/X mode splitting analysis
- HamSCI data export API

---

## Validation Methodology

### 1. TEC Accuracy Validation

**Objective**: Verify TEC estimates are within ±10 TECU of GPS-derived TEC

**Ground Truth Source**: NOAA SWPC GPS TEC maps  
**URL**: <https://www.swpc.noaa.gov/products/us-total-electron-content>

**Method**:

1. Collect 7 days of hf-timestd TEC data (WWV, WWVH, CHU, BPM)
2. Download corresponding GPS TEC maps for same time period
3. Extract GPS TEC at ionospheric reflection points (midpoint between station and receiver)
4. Calculate correlation and RMS error

**Success Criteria**:

- Correlation coefficient: R² > 0.7
- RMS error: < 10 TECU
- Diurnal pattern match: Peak within ±1 hour of GPS TEC peak

**Expected Accuracy**: ±5-10 TECU (vs ±1-2 TECU for GPS)

**Error Sources**:

- Multipath contamination
- Fewer frequencies than GPS (6 vs 2, but lower frequency range)
- D-layer absorption on lower bands
- Propagation mode uncertainty (1-hop vs 2-hop affects path length)

---

### 2. Doppler Velocity Validation

**Objective**: Confirm Doppler measurements reflect physical ionospheric motion

**Ground Truth Source**: Known stable transmitters (e.g., WWV carrier during non-modulated periods)

**Method**:

1. Identify "quiet" ionospheric periods (low geomagnetic activity)
2. Measure Doppler spread during these periods
3. Compare to expected thermal noise floor: σ_doppler ≈ 0.02 Hz

**Success Criteria**:

- Quiet period Doppler spread: < 0.05 Hz
- Diurnal pattern: Negative Doppler at sunset (layer rising)
- Correlation with solar zenith angle: R² > 0.6

**Validation Events**:

- Dawn transition: Expect negative Doppler (layer ascending)
- Dusk transition: Expect positive Doppler (layer descending)
- Geomagnetic storm: Expect increased spread (>0.5 Hz)

---

### 3. Solar Flare Detection Validation

**Objective**: Verify SID (Sudden Ionospheric Disturbance) detection matches GOES X-ray events

**Ground Truth Source**: NOAA GOES X-ray flux data  
**URL**: <https://www.swpc.noaa.gov/products/goes-x-ray-flux>

**Method**:

1. Collect 30 days of FSS (Frequency Selective Fading) data from test signals
2. Identify FSS spikes: >5 dB increase in <5 minutes
3. Cross-reference with GOES X-ray events (M-class or above)
4. Calculate detection rate and false positive rate

**Success Criteria**:

- Detection rate: >80% for M-class flares
- False positive rate: <10%
- Latency: Detection within 5 minutes of X-ray peak

**Expected Signature**:

- FSS increase on 2.5/5 MHz (D-layer absorption)
- SNR drop on lower frequencies
- Doppler anomaly (sudden frequency deviation)

---

### 4. Layer Altitude Validation

**Objective**: Verify propagation delay corresponds to realistic F-layer altitudes

**Ground Truth Source**: NOAA ionosonde data (nearest station)  
**URL**: <https://www.ngdc.noaa.gov/stp/iono/ionogram.html>

**Method**:

1. Compare hf-timestd propagation delay with ionosonde virtual height (h'F2)
2. Calculate implied layer altitude from delay: h = (c × τ) / (2 × cos(elevation))
3. Validate against ionosonde measurements

**Success Criteria**:

- Altitude agreement: ±50 km
- Diurnal pattern match: Layer rises during day, falls at night
- Seasonal variation: Higher in summer, lower in winter

**Expected Range**: 200-400 km (F-layer typical)

---

### 5. TID Detection Validation

**Objective**: Confirm Traveling Ionospheric Disturbance detection

**Ground Truth Source**: Cross-correlation with other HamSCI stations or GPS TEC maps

**Method**:

1. Identify periodic Doppler oscillations (10-60 minute period)
2. Verify spatial coherence across multiple stations
3. Calculate propagation velocity and direction

**Success Criteria**:

- Periodicity: 10-60 minutes (typical TID range)
- Amplitude: 0.3-1.0 Hz Doppler excursion
- Spatial coherence: Visible on multiple propagation paths
- Velocity: 100-500 m/s (typical gravity wave speed)

**Validation Events**:

- Geomagnetic storms (Kp > 5)
- Solar flares (M-class or above)
- Auroral activity

---

## Data Quality Metrics

### Continuous Monitoring

**Automated Quality Checks** (to be implemented in Science Aggregator):

1. **TEC Sanity Checks**:
   - Range: 5-100 TECU (reject outliers)
   - Confidence: >0.7 (reject poor fits)
   - n_frequencies: ≥3 (require sufficient data)

2. **Doppler Sanity Checks**:
   - Range: ±2 Hz (reject extreme values)
   - Coherence time: >10 seconds (reject noisy data)
   - Phase variance: <1 radian (reject incoherent signals)

3. **SNR Thresholds**:
   - Minimum SNR: 10 dB (reject weak signals)
   - Maximum SNR: 60 dB (reject saturation)

4. **Propagation Mode Validation**:
   - Delay consistency: 1-hop vs 2-hop separation >2 ms
   - Mode stability: Same mode for >5 consecutive minutes

---

## Validation Timeline

### Phase 1: Initial Validation (Week 1-2)

- [ ] Deploy Science Aggregator
- [ ] Collect 7 days of TEC data
- [ ] Compare with GPS TEC maps
- [ ] Document accuracy and error sources

### Phase 2: Event Validation (Week 3-4)

- [ ] Monitor for solar flares (M-class or above)
- [ ] Validate FSS response
- [ ] Monitor for geomagnetic storms (Kp > 5)
- [ ] Validate TID detection

### Phase 3: Long-Term Stability (Month 2-3)

- [ ] Collect 30 days of continuous data
- [ ] Analyze diurnal/seasonal patterns
- [ ] Cross-validate with ionosonde data
- [ ] Publish validation report

---

## Reporting Standards

### Data Quality Indicators

All scientific data products must include:

1. **Confidence Score**: 0-1 scale based on:
   - Number of frequencies used
   - SNR of measurements
   - Fit residuals (for TEC)
   - Coherence time (for Doppler)

2. **Uncertainty Estimate**: ±X units
   - TEC: ±5-10 TECU
   - Doppler: ±0.05 Hz
   - Layer altitude: ±50 km

3. **Quality Flags**:
   - `GOOD`: All quality checks passed
   - `MARGINAL`: Some checks failed, use with caution
   - `BAD`: Multiple checks failed, do not use

### Publication Guidelines

**Before claiming scientific capabilities**:

1. Complete Phase 1 validation (TEC vs GPS)
2. Document accuracy with confidence intervals
3. Publish validation methodology and results
4. Peer review by ionospheric physics community

**Acceptable Claims** (after validation):

- "TEC estimates agree with GPS TEC within ±X TECU (R²=Y)"
- "Solar flare detection rate: X% (M-class or above)"
- "Layer altitude accuracy: ±X km vs ionosonde"

**Unacceptable Claims** (without validation):

- "Precision TEC measurements" (without error bars)
- "Real-time space weather monitoring" (without validation)
- "Ionosonde-quality data" (without comparison)

---

## Validation Artifacts

### Required Documentation

1. **Validation Report** (`docs/VALIDATION_REPORT.md`)
   - Methodology
   - Results with error bars
   - Comparison plots
   - Limitations and error sources

2. **Quality Metrics Dashboard** (Web UI)
   - Real-time confidence scores
   - Data availability statistics
   - Outlier rejection rates

3. **Validation Notebooks** (`validation/`)
   - Jupyter notebooks with analysis code
   - Reproducible validation scripts
   - Ground truth data sources

---

## Summary

**Current Status**: Implementation complete, validation pending

**Next Steps**:

1. Deploy Science Aggregator
2. Collect 7 days of data
3. Compare with GPS TEC
4. Document results

**Scientific Integrity**: No claims without validation data.
