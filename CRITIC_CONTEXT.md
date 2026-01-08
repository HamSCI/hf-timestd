# CRITIC_CONTEXT: Fusion Pipeline Review

**DO NOT ALTER THIS HEADER OR THESE INSTRUCTIONS**

This document is specifically designed to prepare an AI agent for a critical review session. The goal is to scrutinize the L2→Fusion→Chrony pipeline for correctness, proper uncertainty accounting, and appropriate clock discipline methodology.

---

## Current System Status (2026-01-08)

### What's Working

✅ **Tone Detection**: Restored with 100ms edge-detection templates (v5.0.1)  
✅ **L2 Data Generation**: Per-broadcast Kalman filters producing valid timing measurements  
✅ **Fusion Service**: Running and feeding Chrony (Reach: 252, LastRx: <60s)  
✅ **Chrony Feed**: System clock synchronized via SHM (~700 μs offset)

### What Needs Critical Review

⚠️ **Fusion Methodology**: Is the weighting scheme statistically correct?  
⚠️ **Uncertainty Propagation**: Are we properly accounting for all error sources?  
⚠️ **Clock Discipline**: Are we feeding Chrony appropriately given our uncertainty?  
⚠️ **Physics Validation**: Is the TEC estimation and propagation modeling sound?

---

## Pipeline Overview: L2 → Fusion → Chrony

### Stage 1: L2 Timing Measurements (Analytics Service)

**File**: `src/hf_timestd/core/phase2_analytics_service.py`

**Per-Broadcast Kalman Filters** (17 independent filters):

- **State**: `[ToF, Doppler]` for each station/frequency combination
- **Input**: Raw tone arrival times from `Phase2TemporalEngine`
- **Output**: Filtered ToF with uncertainty estimates
- **Storage**: HDF5 L2 files (schema v1.3.0)

**Key Fields in L2 Data**:

- `d_clock_ms`: GPSDO-relative clock offset
- `tof_kalman_ms`: Filtered Time of Flight (ionospheric delay)
- `tof_uncertainty_ms`: Kalman filter uncertainty
- `uncertainty_ms`: Total measurement uncertainty
- `confidence`: Detection quality (0-1)
- `quality_grade`: A/B/C/D grading
- `gpsdo_locked`: GPSDO lock status flag

### Stage 2: Multi-Broadcast Fusion (Fusion Service)

**File**: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Fusion Algorithm** (`MultiBroadcastFusion.fuse()`):

1. **Read Measurements**: Load last 10 minutes of L2 data from all channels
2. **Filter Invalid Data**: Remove NaN, unlocked GPSDO, failed detections
3. **Calculate Weights**: Inverse variance weighting with quality scaling
4. **Combine Measurements**: Weighted average of D_clock estimates
5. **Compute Uncertainty**: Propagate uncertainties through fusion
6. **Grade Result**: A/B/C/D based on number of stations and agreement
7. **Feed Chrony**: Write to SHM if Grade A or B

**Weight Calculation** (`_calculate_weights()`):

```python
# Base weight: Inverse variance (statistically optimal)
base_weight = 1.0 / (uncertainty_ms ** 2)

# Quality scaling factors:
confidence_scale = measurement.confidence  # Detection quality
grade_scale = {'A': 1.0, 'B': 0.9, 'C': 0.7, 'D': 0.5}
mode_scale = {'1E': 1.0, '1F': 0.95, '2F': 0.85, '3F': 0.7}
snr_scale = f(snr_db)  # 1.0 for SNR > 15 dB

# Final weight:
w = base_weight × confidence_scale × grade_scale × mode_scale × snr_scale
```

**Fused Result**:

- `d_clock_ms`: Weighted average clock offset
- `uncertainty_ms`: Combined uncertainty
- `grade`: Quality grade (A/B/C/D)
- `n_stations`: Number of contributing stations

### Stage 3: Chrony Feed (SHM Interface)

**File**: `src/hf_timestd/core/multi_broadcast_fusion.py` (lines 2500-2562)

**Feed Logic**:

```python
if grade in ['A', 'B']:
    chrony_shm.write(
        offset_seconds=d_clock_ms / 1000.0,
        leap_status=0,  # No leap second
        precision=-6    # ~1 μs precision claim
    )
```

**Current Chrony Configuration**:

- Source: `TMGR` (SHM unit 0)
- Stratum: 0 (reference clock)
- Poll: 4 (16 seconds)
- Reach: 252 (continuous updates)

---

## Critical Questions for Review

### 1. Fusion Methodology

**Question**: Is inverse variance weighting the correct approach?

**Current Implementation**:

- Base weight = `1 / σ²` (inverse variance)
- Scaled by confidence, grade, mode, SNR

**Concerns**:

- Are we double-counting uncertainty? (σ in weight + σ in final uncertainty)
- Should confidence be part of the weight or the uncertainty?
- Are the quality scaling factors justified or arbitrary?

**ISO GUM Reference**: Section 5.1.2 - "Weighted mean of correlated input quantities"

### 2. Uncertainty Accounting

**Question**: Are we properly propagating all error sources?

**L2 Uncertainty Components** (should include):

- ✅ `u_rtp_timestamp_ms`: RTP timestamp quantization
- ✅ `u_gpsdo_ms`: GPSDO stability
- ✅ `u_discrimination_ms`: WWV/WWVH separation
- ✅ `u_propagation_model_ms`: IRI-2020 model error
- ✅ `u_ionospheric_ms`: TEC variability
- ✅ `u_multipath_ms`: Multipath/fading effects

**Fusion Uncertainty** (should include):

- ❓ Weighted combination of L2 uncertainties
- ❓ Cross-station validation residuals
- ❓ TEC estimation error
- ❓ Temporal correlation between measurements

**Concerns**:

- Are we treating measurements as independent when they're not?
- Does the 10-minute lookback introduce correlation?
- Should we use Type A (statistical) vs Type B (systematic) uncertainty?

### 3. Clock Discipline Appropriateness

**Question**: Should we be feeding Chrony given our uncertainty?

**Current Behavior**:

- Feed Chrony if Grade A or B
- Claim `-6` precision (1 μs)
- Typical offset: ~700 μs
- Typical uncertainty: ~1000 μs

**Concerns**:

- **Precision claim**: Is `-6` (1 μs) justified when uncertainty is ~1000 μs?
- **Stratum 0**: Should we claim reference clock status?
- **Update rate**: Is 16s appropriate for our stability?
- **Uncertainty communication**: Chrony doesn't see our uncertainty budget

**Chrony Documentation**: Should we use `refclock SHM` with `offset` and `delay` parameters?

### 4. Physics Validation

**Question**: Is our TEC estimation and propagation modeling sound?

**TEC Estimation** (`TECEstimator`):

- Uses differential delay: `τ(f₁) - τ(f₂)`
- Assumes `τ ∝ TEC/f²` (ionospheric dispersion)
- Fits linear regression to estimate TEC

**Propagation Modeling** (`IonosphericPhysicsModel`):

- Uses IRI-2020 for predicted delays
- Computes hop count (1F, 2F, 3F)
- Estimates TEC from model

**Concerns**:

- Are we validating TEC estimates against GPS IONEX?
- Do we handle sporadic E and other anomalies?
- Is the hop count detection reliable?

---

## Specific Code to Review

### Priority 1: Weight Calculation

**File**: `multi_broadcast_fusion.py:1493-1568`
**Function**: `_calculate_weights()`

**Review Points**:

1. Is `1/σ²` the correct base weight?
2. Should confidence multiply the weight or modify the uncertainty?
3. Are the scaling factors (grade, mode, SNR) justified?
4. Should we normalize weights to sum to 1?

### Priority 2: Uncertainty Propagation

**File**: `multi_broadcast_fusion.py:2100-2200` (approximate)
**Function**: `fuse()` - uncertainty calculation section

**Review Points**:

1. How is fused uncertainty calculated?
2. Are we using weighted variance formula correctly?
3. Do we account for correlation between measurements?
4. Should we use GUM-S1 (Monte Carlo) for complex cases?

### Priority 3: Chrony Feed Logic

**File**: `multi_broadcast_fusion.py:2500-2562`
**Function**: `_write_to_chrony_shm()`

**Review Points**:

1. Is precision claim `-6` appropriate?
2. Should we communicate uncertainty to Chrony?
3. Is Grade A/B threshold correct for feeding?
4. Should we use `delay` parameter in SHM?

### Priority 4: GPSDO Continuity Check

**File**: `phase2_analytics_service.py:2576`
**Function**: `_process_minute()` - D_clock continuity validation

**Review Points**:

1. Is the continuity threshold appropriate?
2. Should we reject measurements or just flag them?
3. How do we handle GPSDO drift vs ionospheric changes?

---

## Data to Examine

### L2 HDF5 Files

**Location**: `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/*.h5`

**Key Datasets to Inspect**:

- `d_clock_ms`: Should be stable over time
- `tof_kalman_ms`: Should track ionospheric changes
- `uncertainty_ms`: Should be realistic (0.1-10 ms range)
- `quality_grade`: Distribution of A/B/C/D
- `gpsdo_locked`: Should be True always

**Validation Script**: `inspect_l2.py`

### Fusion Logs

**Command**: `sudo journalctl -u timestd-fusion -f`

**Look For**:

- "Fused result: Grade X, offset=Y ms, uncertainty=Z ms"
- "Writing to Chrony SHM"
- Number of stations contributing
- Weight distribution across measurements

### Chrony Status

**Command**: `chronyc sources -v`

**Check**:

- Reach (should be 252 = all updates successful)
- LastRx (should be <60s)
- Offset (should be stable)
- Jitter (should be <2ms)

---

## Expected Outcomes from Review

### If Methodology is Correct

- [ ] Confirm inverse variance weighting is appropriate
- [ ] Validate uncertainty propagation follows ISO GUM
- [ ] Verify Chrony feed parameters are justified
- [ ] Document any assumptions or limitations

### If Issues Found

- [ ] Identify specific mathematical errors
- [ ] Propose corrections with references
- [ ] Estimate impact on clock accuracy
- [ ] Implement fixes and re-validate

---

## References

### Standards

- **ISO GUM** (2008): Guide to the Expression of Uncertainty in Measurement
- **GUM-S1** (2008): Propagation of distributions using Monte Carlo
- **ITU-R P.531**: Ionospheric propagation data and prediction methods

### Code Files

- `src/hf_timestd/core/multi_broadcast_fusion.py` - Main fusion logic
- `src/hf_timestd/core/phase2_analytics_service.py` - L2 data generation
- `src/hf_timestd/core/tec_estimator.py` - TEC calculation
- `src/hf_timestd/core/ionospheric_physics_model.py` - IRI-2020 integration

### Documentation

- `CHANGELOG.md` - Recent changes and fixes
- `CONTEXT.md` - Overall system architecture
- `TECHNICAL_REFERENCE.md` - Detailed technical documentation

---

**CRITICAL REMINDER**: This review should be rigorous and mathematical. Question every assumption. Demand references for statistical methods. Validate against standards (ISO GUM, ITU-R). The goal is correctness, not convenience.
