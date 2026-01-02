# Science Aggregator vs. Scientific Capabilities - Gap Analysis

**Date:** January 2, 2026  
**Purpose:** Correlate current science-aggregator implementation with documented scientific capabilities

---

## Executive Summary

The science-aggregator service currently implements **only 1 of 6** documented scientific capabilities from SCIENTIFIC_CAPABILITIES.md. Most advanced features described in the capabilities document are either not implemented or exist only as placeholders.

### Implementation Status

| Scientific Capability | SCIENTIFIC_CAPABILITIES.md Status | Science Aggregator Status | Gap |
|----------------------|-----------------------------------|---------------------------|-----|
| **TEC Monitoring** | ⚠️ Requires validation (lines 218-241) | ✅ Implemented, needs validation | ALIGNED |
| **D-Layer Absorption** | ✅ High confidence (lines 166-190) | ❌ Not implemented | **MAJOR GAP** |
| **Propagation Mode Stats** | ⚠️ Medium confidence (lines 192-216) | ❌ Not implemented | **MAJOR GAP** |
| **Sporadic-E Detection** | ⚠️ Detection possible (lines 243-267) | ❌ Not implemented | **MAJOR GAP** |
| **TID Detection** | ⚠️ Theoretical capability (lines 269-292) | ❌ Placeholder only | **MAJOR GAP** |
| **Ionospheric Tilt** | ⚠️ Theoretical capability (lines 294-318) | ❌ Not implemented | **MAJOR GAP** |

---

## Detailed Correlation Analysis

### 1. TEC Monitoring ✅ IMPLEMENTED

**SCIENTIFIC_CAPABILITIES.md (lines 218-241):**
```
Measurements Used:
- Multi-frequency ToA (2.5 - 25 MHz)
- Dispersion analysis (f^-2 fit)

Scientific Questions:
- What is the local TEC over the receiver?
- How does TEC vary diurnally?
- Can we validate GPS TEC maps?

Data Quality: ⚠️ Requires validation
- TEC estimation implemented but not validated
- Needs comparison with GPS TEC (IONEX)
```

**Science Aggregator Implementation:**
- ✅ **TECEstimator class** (`tec_estimator.py:73-226`)
- ✅ **Multi-frequency aggregation** (`science_aggregator.py:209-305`)
- ✅ **Least-squares f^-2 fit** (correct physics: K = 40.3 m³/s²)
- ✅ **HDF5 output** with schema validation (`l3_tec_v1.json`)
- ✅ **Quality metrics**: confidence, residuals, n_frequencies
- ✅ **Quality flags**: GOOD/MARGINAL/BAD based on fit quality

**Alignment:** ✅ **EXCELLENT**
- Implementation matches documented capability
- Both acknowledge validation needed
- Physics correctly implemented
- Data product properly structured

**Validation Gap:**
- SCIENTIFIC_CAPABILITIES.md: "Needs comparison with GPS TEC (IONEX)"
- Current status: VTEC service exists but no automated comparison
- **Recommendation:** Implement TEC validation against IONEX data

---

### 2. D-Layer Absorption Studies ❌ NOT IMPLEMENTED

**SCIENTIFIC_CAPABILITIES.md (lines 166-190):**
```
Measurements Used:
- Multi-frequency SNR (2.5 - 25 MHz)
- Solar zenith angle at path midpoint
- Time of day

Scientific Questions:
- How does D-layer absorption vary with frequency?
- What is the diurnal pattern of absorption?
- Can we detect Sudden Ionospheric Disturbances (SIDs)?

Data Quality: ✅ High confidence
- SNR measurements validated
- Frequency dependence well-established physics
```

**Science Aggregator Implementation:**
- ❌ **No SNR aggregation** across frequencies
- ❌ **No absorption calculation**
- ❌ **No SID detection**
- ❌ **No solar zenith angle calculation**

**Available Data (Not Used):**
- SNR measurements exist in L1A channel observables
- Multi-frequency coverage (2.5-25 MHz)
- Timing data for solar zenith calculation

**Gap Analysis:**
- **Severity:** HIGH - Marked as "high confidence" in capabilities doc
- **Effort:** MEDIUM - Data exists, needs aggregation + analysis
- **Scientific Value:** HIGH - SID detection valuable for space weather

**Implementation Path:**
1. Aggregate SNR across frequencies per minute
2. Calculate solar zenith angle for each path
3. Compute absorption using frequency dependence (f^-n)
4. Detect SIDs as sudden SNR drops
5. Create L3B schema for absorption events

---

### 3. Propagation Mode Statistics ❌ NOT IMPLEMENTED

**SCIENTIFIC_CAPABILITIES.md (lines 192-216):**
```
Measurements Used:
- Propagation mode classification (1E, 1F, 2F, etc.)
- Time of day
- Frequency
- Propagation delay

Scientific Questions:
- Probability of E-layer vs F-layer propagation?
- How often does multi-hop occur?
- Can we estimate MUF?

Data Quality: ⚠️ Medium confidence
- Mode classification based on delay heuristics
```

**Science Aggregator Implementation:**
- ❌ **No mode aggregation** across stations/frequencies
- ❌ **No mode statistics**
- ❌ **No MUF estimation**

**Available Data (Not Used):**
- Propagation mode in L2 timing measurements
- Propagation delay measurements
- Multi-frequency observations

**Gap Analysis:**
- **Severity:** MEDIUM - Marked as "medium confidence"
- **Effort:** LOW - Data exists in L2, just needs aggregation
- **Scientific Value:** MEDIUM - Useful for propagation prediction

**Implementation Path:**
1. Read propagation_mode from L2 timing measurements
2. Aggregate by time-of-day, frequency, season
3. Calculate mode probabilities
4. Estimate MUF from highest frequency with F-layer propagation
5. Create L3C schema for propagation statistics

---

### 4. Sporadic-E Detection ❌ NOT IMPLEMENTED

**SCIENTIFIC_CAPABILITIES.md (lines 243-267):**
```
Measurements Used:
- SNR sudden increases at 10-15 MHz
- Mode change to 1E
- Event timing and duration

Scientific Questions:
- When do sporadic-E events occur?
- Seasonal/diurnal pattern?
- Critical frequency (foEs)?

Data Quality: ⚠️ Detection possible, characterization needs work
```

**Science Aggregator Implementation:**
- ❌ **No Es detection algorithm**
- ❌ **No SNR anomaly detection**
- ❌ **No mode change tracking**

**Available Data (Not Used):**
- SNR time series in L1A
- Propagation mode in L2
- Multi-frequency observations

**Gap Analysis:**
- **Severity:** MEDIUM - Marked as "detection possible"
- **Effort:** MEDIUM - Requires time series analysis + thresholds
- **Scientific Value:** HIGH - Es events important for HF propagation

**Implementation Path:**
1. Track SNR time series for sudden increases (>10 dB)
2. Detect mode changes to 1E at 10-15 MHz
3. Measure event duration
4. Estimate foEs from highest frequency with Es propagation
5. Create L3B schema for Es events

---

### 5. Ionospheric Dynamics (TIDs) ❌ PLACEHOLDER ONLY

**SCIENTIFIC_CAPABILITIES.md (lines 269-292):**
```
Measurements Used:
- Doppler shift time series
- Coherent oscillations across frequencies
- Phase velocity estimation

Scientific Questions:
- Can we detect TIDs?
- What are TID periods and wavelengths?
- Correlation with geomagnetic activity?

Data Quality: ⚠️ Theoretical capability, needs implementation
- Doppler measured but TID detection not automated
```

**Science Aggregator Implementation:**
- ⚠️ **Placeholder function** (`science_aggregator.py:417-427`)
- ❌ **No Doppler aggregation**
- ❌ **No oscillation detection**
- ❌ **No period/wavelength extraction**

**Code Evidence:**
```python
def _detect_events(self):
    """
    Detect ionospheric events from TEC and Doppler anomalies.
    
    Future implementation: Analyze time series for:
    - Traveling Ionospheric Disturbances (TIDs)
    - Spread-F events
    - Solar flare absorption
    """
    # Placeholder for future implementation
    pass
```

**Available Data (Not Used):**
- Doppler measurements in L1A channel observables
- Multi-frequency Doppler for coherence analysis
- TEC time series (after fix) for correlation

**Gap Analysis:**
- **Severity:** MEDIUM - Both doc and code acknowledge "needs implementation"
- **Effort:** HIGH - Requires sophisticated time series analysis
- **Scientific Value:** HIGH - TIDs important for space weather

**Implementation Path:**
1. Aggregate Doppler time series across frequencies
2. Apply bandpass filter for TID periods (15-60 min)
3. Detect coherent oscillations using cross-correlation
4. Estimate phase velocity from multi-frequency delays
5. Correlate with geomagnetic indices (Kp)
6. Create L3B schema for TID events

---

### 6. Ionospheric Tilt ❌ NOT IMPLEMENTED

**SCIENTIFIC_CAPABILITIES.md (lines 294-318):**
```
Measurements Used:
- TEC from multiple transmitter paths (WWV, WWVH, CHU, BPM)
- Different azimuths from receiver
- TEC gradient calculation

Scientific Questions:
- Large-scale ionospheric structure?
- Can we detect ionospheric tilts?
- How does TEC vary with azimuth?

Data Quality: ⚠️ Theoretical capability
- Requires validated TEC from multiple paths
- Gradient calculation not implemented
```

**Science Aggregator Implementation:**
- ❌ **No multi-path TEC comparison**
- ❌ **No gradient calculation**
- ❌ **No azimuthal analysis**

**Available Data (After Fix):**
- TEC from multiple stations (WWV, WWVH, CHU, BPM)
- Different azimuths from receiver
- Simultaneous measurements

**Gap Analysis:**
- **Severity:** LOW - Both doc and code acknowledge "theoretical"
- **Effort:** MEDIUM - Requires validated TEC first
- **Scientific Value:** MEDIUM - Limited by 4 transmitters

**Implementation Path:**
1. Ensure TEC validated for all stations
2. Calculate TEC gradients between station pairs
3. Fit linear tilt model (requires ≥3 stations)
4. Analyze azimuthal dependence
5. Create L3C schema for ionospheric structure

---

## Advanced Features Comparison

### SCIENTIFIC_CAPABILITIES.md Lists (lines 322-354):

**Could Be Added with Current Hardware:**
1. Amplitude Scintillation Index (S4)
2. Phase Scintillation Index (σ_φ)
3. Fading Rate
4. Critical Frequency (foF2) Estimation
5. Intermodulation Products

**Science Aggregator Status:**
- ❌ **None implemented**
- ❌ **No scintillation indices**
- ❌ **No fading analysis**
- ❌ **No foF2 estimation**
- ❌ **No intermodulation detection**

**Available Data:**
- Amplitude/phase in L1A channel observables
- SNR time series for fading rate
- Multi-frequency for foF2 estimation

---

## Data Product Alignment

### SCIENTIFIC_CAPABILITIES.md Data Products (lines 411-463):

| Level | Documented | Science Aggregator | Status |
|-------|-----------|-------------------|--------|
| **L1A** | Channel Observables (HDF5) | Not in scope | ✅ Analytics handles |
| **L1B** | BCD Timecode (HDF5) | Not in scope | ✅ Analytics handles |
| **L2** | Timing Measurements (HDF5) | **Input source** | ✅ Reads correctly (after fix) |
| **L3** | Fused Timing (HDF5) | Not in scope | ✅ Fusion service handles |
| **Science Products** | TEC, Es, TIDs (HDF5) | **Only TEC** | ⚠️ Partial implementation |

**Gap:** Science Products section says "TEC estimates (when validated)" but doesn't mention that other products (Es, TIDs) are completely missing.

---

## Validation Requirements Comparison

### SCIENTIFIC_CAPABILITIES.md Requirements (lines 466-490):

**Tier 1: Basic Validation** (Required for all features)
- Compare carrier SNR with radiod
- Verify Doppler range
- Check tone detection
- Validate ToA

**Science Aggregator:**
- ❌ **No validation checks implemented**
- ❌ **No automated comparison with radiod**
- ❌ **No sanity checks on input data**

**Tier 2: Cross-Validation** (Required for ionospheric features)
- Compare TEC with GPS TEC maps (IONEX)
- Validate propagation modes
- Correlate absorption with solar zenith

**Science Aggregator:**
- ❌ **No TEC validation against IONEX**
- ❌ **No automated validation**
- ⚠️ VTEC service exists but not integrated

**Tier 3: Scientific Validation** (Required for publication)
- Compare Es with ionosonde data
- Validate TID periods
- Compare MUF with VOACAP

**Science Aggregator:**
- ❌ **No scientific validation implemented**

---

## Recommendations Summary

### Critical Gaps (High Priority)

1. **TEC Validation** ⚠️ URGENT
   - SCIENTIFIC_CAPABILITIES.md: "Needs comparison with GPS TEC (IONEX)"
   - Science Aggregator: Produces TEC but never validates
   - **Action:** Integrate with VTEC service for automated validation
   - **Effort:** LOW (VTEC service already exists)

2. **D-Layer Absorption** ❌ HIGH VALUE
   - SCIENTIFIC_CAPABILITIES.md: "High confidence" capability
   - Science Aggregator: Not implemented
   - **Action:** Implement SNR aggregation + absorption calculation
   - **Effort:** MEDIUM

3. **Sporadic-E Detection** ❌ HIGH VALUE
   - SCIENTIFIC_CAPABILITIES.md: "Detection possible"
   - Science Aggregator: Not implemented
   - **Action:** Implement SNR anomaly detection + mode tracking
   - **Effort:** MEDIUM

### Major Gaps (Medium Priority)

4. **TID Detection** ⚠️ PLACEHOLDER
   - SCIENTIFIC_CAPABILITIES.md: "Needs implementation"
   - Science Aggregator: Placeholder function exists
   - **Action:** Implement Doppler time series analysis
   - **Effort:** HIGH

5. **Propagation Mode Statistics** ❌ EASY WIN
   - SCIENTIFIC_CAPABILITIES.md: "Medium confidence"
   - Science Aggregator: Not implemented
   - **Action:** Aggregate existing mode data from L2
   - **Effort:** LOW (data already exists)

6. **Validation Framework** ❌ FOUNDATIONAL
   - SCIENTIFIC_CAPABILITIES.md: Defines 3 tiers of validation
   - Science Aggregator: No validation implemented
   - **Action:** Implement automated validation checks
   - **Effort:** MEDIUM

### Future Enhancements (Low Priority)

7. **Scintillation Indices** (S4, σ_φ)
8. **Fading Rate Analysis**
9. **Critical Frequency Estimation**
10. **Ionospheric Tilt Analysis**

---

## Documentation Consistency Issues

### Misleading Statements

1. **SCIENTIFIC_CAPABILITIES.md line 458:**
   ```
   Science Products:
   - TEC estimates (when validated)
   - Sporadic-E events
   - TID detections
   ```
   **Reality:** Only TEC is implemented. Es and TID are not implemented.
   **Fix:** Update to clarify implementation status

2. **SCIENTIFIC_CAPABILITIES.md line 141:**
   ```
   Status: TECEstimator class exists, needs validation
   ```
   **Reality:** Correct, but doesn't mention validation is not automated
   **Fix:** Add note about manual validation requirement

3. **SCIENTIFIC_CAPABILITIES.md line 284:**
   ```
   Doppler measured but TID detection not automated
   ```
   **Reality:** TID detection is not implemented at all (not just "not automated")
   **Fix:** Change to "TID detection not implemented"

### Missing Information

1. **No mention of science-aggregator service**
   - Document describes capabilities but not which service implements them
   - **Fix:** Add section mapping capabilities to services

2. **No implementation roadmap**
   - Document lists features but not priority or timeline
   - **Fix:** Add implementation status table (like this document)

3. **No data flow diagram**
   - Document doesn't show how data flows from L1 → L2 → L3 → Science
   - **Fix:** Add data flow diagram

---

## Proposed Updates to SCIENTIFIC_CAPABILITIES.md

### Add Implementation Status Section

```markdown
## Implementation Status (as of January 2026)

| Scientific Capability | Service | Status | Validation |
|----------------------|---------|--------|-----------|
| TEC Monitoring | science-aggregator | ✅ Implemented | ⚠️ Needs IONEX comparison |
| D-Layer Absorption | - | ❌ Not implemented | N/A |
| Propagation Mode Stats | - | ❌ Not implemented | N/A |
| Sporadic-E Detection | - | ❌ Not implemented | N/A |
| TID Detection | science-aggregator | ⚠️ Placeholder only | N/A |
| Ionospheric Tilt | - | ❌ Not implemented | N/A |
```

### Update Science Products Section (line 454)

**Current:**
```markdown
### Science Products

Derived ionospheric parameters:
- TEC estimates (when validated)
- Sporadic-E events
- TID detections
```

**Proposed:**
```markdown
### Science Products (L3A/L3B)

**Currently Implemented:**
- TEC estimates (L3A, HDF5) - Needs validation against IONEX

**Planned (Not Yet Implemented):**
- Sporadic-E events (L3B)
- TID detections (L3B)
- D-layer absorption (L3B)
- Propagation mode statistics (L3C)
```

### Add Service Mapping Section

```markdown
## Service Responsibilities

| Service | Data Products | Scientific Capabilities |
|---------|--------------|------------------------|
| **core-recorder** | L0 (Digital RF) | Raw IQ capture |
| **analytics** | L1A, L1B, L2 | Signal features, timing |
| **fusion** | L3 | Multi-station timing |
| **science-aggregator** | L3A (TEC only) | TEC monitoring |
| **vtec** | L3A (GNSS) | External TEC reference |
```

---

## Conclusion

The science-aggregator service implements **only 17% (1 of 6)** of the scientific capabilities documented in SCIENTIFIC_CAPABILITIES.md. While the TEC implementation is solid and well-designed, the vast majority of documented capabilities are missing.

### Key Findings

1. ✅ **TEC Monitoring:** Well implemented, needs validation
2. ❌ **D-Layer Absorption:** Not implemented despite "high confidence" status
3. ❌ **Propagation Mode Stats:** Not implemented (easy win - data exists)
4. ❌ **Sporadic-E Detection:** Not implemented despite "detection possible"
5. ⚠️ **TID Detection:** Placeholder only
6. ❌ **Ionospheric Tilt:** Not implemented
7. ❌ **Advanced Features:** None implemented (S4, σ_φ, fading, foF2)
8. ❌ **Validation Framework:** Not implemented

### Recommendations

**Immediate (This Week):**
1. Fix TEC input bug (✅ DONE)
2. Validate TEC against IONEX data
3. Update SCIENTIFIC_CAPABILITIES.md with implementation status

**Short-Term (1-3 Months):**
1. Implement D-layer absorption analysis
2. Implement sporadic-E detection
3. Implement propagation mode statistics
4. Add validation framework

**Medium-Term (3-6 Months):**
1. Implement TID detection
2. Add scintillation indices
3. Implement ionospheric tilt analysis

**Documentation:**
1. Add implementation status table to SCIENTIFIC_CAPABILITIES.md
2. Add service mapping section
3. Clarify what's implemented vs. planned
4. Add data flow diagram

The gap between documented capabilities and actual implementation suggests either:
- Documentation was aspirational (roadmap, not current state)
- Implementation fell behind documentation
- Need to clarify "capability" vs. "implemented feature"

**Recommendation:** Update SCIENTIFIC_CAPABILITIES.md to clearly distinguish between:
- ✅ **Validated and Implemented**
- ⚠️ **Implemented but Needs Validation**
- 🚧 **Partially Implemented**
- 📋 **Planned but Not Implemented**
- 💡 **Theoretical Capability**
