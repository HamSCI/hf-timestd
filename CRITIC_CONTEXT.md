# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION:

Primary Instruction:  In this context you will perform a critical review of the GRAPE Recorder project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session):

---

## 🔴 CURRENT FOCUS: GPSDO-FIRST TIMING CALIBRATION METHODOLOGY

**Purpose:** Critically examine the GPSDO-first timing calibration architecture, looking for mistakes, weaknesses, inconsistencies, and missed opportunities in the methodology.

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-13 (Updated 2025-12-14)  
**Status:** 🟡 In Progress - RTP-First Timing Implemented

---

### THE GPSDO-FIRST TIMING CALIBRATION ARCHITECTURE

#### Core Philosophy

The system leverages GPSDO-disciplined RTP timestamps as the **primary timing foundation**, then progressively refines with tone detections and multi-broadcast fusion:

```
LAYER 1: GPSDO Foundation
├─ RTP timestamps from GPS-disciplined ka9q-radio (±0.1 PPM)
├─ All 9 channels share the same master clock
└─ Sample count integrity: 1,200,000 samples = exactly 60 seconds

LAYER 2: Tone Detection
├─ WWV/WWVH 1000/1200 Hz tones at second 0 (800ms duration)
├─ CHU 1000 Hz tone at second 0 (500ms duration)
├─ Per-second tick confirmations (59 per minute, 5ms each)
└─ CHU FSK timing (seconds 31-39) for independent verification

LAYER 3: Station-Level Calibration
├─ Each station (WWV, WWVH, CHU) has ONE atomic clock
├─ Station mean is ground truth; frequency variance = propagation
└─ Calibration offset brings station mean to UTC(NIST) = 0

LAYER 4: Multi-Broadcast Fusion
├─ Weighted average across 13 broadcasts (6 WWV + 4 WWVH + 3 CHU)
├─ Kalman filter for convergence and anomaly detection
└─ Intra-station consistency checks for discrimination validation
```

#### Key Implementation Files

| File | Purpose | Critical Functions |
|------|---------|-------------------|
| `timing_calibrator.py` | Bootstrap → Calibrated → Verified phases | `predict_station()`, `update_from_detection()` |
| `phase2_temporal_engine.py` | Three-step temporal analysis | `_step1_time_snap()`, `_step2_channel_characterization()`, `_step3_transmission_time_solution()` |
| `clock_offset_series.py` | ClockOffsetEngine with RTP calibration | `_get_calibrated_rtp_offset()`, `process_minute()` |
| `multi_broadcast_fusion.py` | Station-level calibration + Kalman | `_update_calibration()`, `_kalman_update()`, `fuse()` |
| `wwvh_discrimination.py` | 8-vote weighted discrimination | `finalize_discrimination()`, `detect_tick_windows()` |
| `propagation_mode_solver.py` | Ionospheric mode identification | `solve()` |
| `pipeline_orchestrator.py` | Wires timing_calibrator to ClockOffsetEngine | `_get_calibrated_rtp_offset()` |

---

### CRITIQUE CHECKLIST: METHODOLOGY VALIDATION

#### 1. GPSDO Foundation Assumptions

**Question:** Are we correctly leveraging the GPSDO stability?

- [x] **RTP offset predictability**: Does `rtp_timestamp % 1,200,000` actually remain constant across minutes?
  - ✅ CONFIRMED: RTP offset is deterministic with GPSDO (~100ns stability)
  - ✅ IMPLEMENTED (2025-12-14): RTP-first timing uses calibrated offset as gold standard ruler
  - Validation: Check `timing_calibration.json` for RTP offset drift warnings

- [ ] **Cross-channel coherence**: Are all channels truly sharing the same clock?
  - Potential issue: Different RTP origins per channel could mask clock issues
  - Validation: Compare tone arrival times across channels for same minute

- [x] **Sample count integrity**: Is 1,200,000 samples always exactly 60 seconds?
  - ✅ With GPSDO: Yes, to ±0.1 PPM (±7.2ms/day max drift)
  - Validation: Check PPM estimates in time_snap data

#### 2. Tone Detection Accuracy

**Question:** Are we detecting tones at the correct positions?

- [ ] **Matched filter template**: Is the 800ms template correct for WWV/WWVH?
  - NIST confirms 800ms duration for timing tones
  - CHU uses 500ms (1000ms at top of hour)

- [ ] **Search window**: Is ±500ms (bootstrap) → ±50ms (calibrated) appropriate?
  - Ionospheric delays range 2-60ms typically
  - Propagation mode changes can cause 5-10ms jumps

- [ ] **Per-second tick detection**: Are we using 59 ticks correctly?
  - Ticks are 5ms pulses at 1000/1200 Hz
  - Coherent integration provides √59 ≈ 7.7x SNR improvement

- [ ] **CHU FSK timing**: Is the 500ms boundary detection accurate?
  - FSK frames at seconds 31-39
  - Should provide independent timing confirmation

#### 3. Station-Level Calibration Logic

**Question:** Is station-level calibration the right abstraction?

- [ ] **Single clock assumption**: Is it true that all frequencies from one station share the same clock?
  - YES: WWV/WWVH/CHU each have one cesium/rubidium reference
  - Frequency-to-frequency variance is ionospheric, not clock

- [ ] **Station mean calculation**: Are we correctly computing the station mean?
  - Current: `station_mean = np.mean([d_clock for all frequencies])`
  - Potential issue: Should we weight by SNR or quality?

- [ ] **Calibration offset stability**: Does the offset converge or oscillate?
  - EMA smoothing: `new_offset = α × ideal + (1-α) × old_offset`
  - α = max(0.1, 10.0 / n_samples) - faster initially, slower as samples accumulate

#### 4. Multi-Broadcast Fusion

**Question:** Is the fusion algorithm optimal?

- [ ] **Weighting scheme**: Are weights appropriate?
  - Current: SNR-based + quality grade + propagation mode
  - Potential issue: Should discrimination confidence affect weight?

- [ ] **Intra-station consistency**: Are we correctly detecting discrimination errors?
  - Current: Flag DISCRIMINATION_SUSPECT if intra-station σ > 5ms
  - Potential issue: 5ms threshold may be too tight for multi-hop propagation

- [ ] **Kalman filter model**: Is the state model appropriate?
  - Current: [d_clock_offset, drift_rate]
  - Potential issue: Drift rate may not be meaningful with GPSDO

- [ ] **Suspect measurement exclusion**: Are we excluding the right measurements?
  - Current: Exclude measurements that increase intra-station variance
  - Potential issue: May exclude valid measurements during propagation mode changes

#### 5. Discrimination System

**Question:** Is the 8-vote weighted discrimination robust?

| Vote | Method | Weight | Potential Issues |
|------|--------|--------|------------------|
| 0 | Test Signal | 15 | Only minutes 8/44; may miss if signal weak |
| 1 | 440 Hz Station ID | 10 | Only minutes 1/2; harmonic contamination possible |
| 2 | BCD Amplitude | 2-10 | Requires good SNR; dual-peak detection fragile |
| 3 | 1000/1200 Hz Power | 1-10 | Affected by propagation fading |
| 4 | Tick SNR Average | 5 | **NOW CONNECTED** - 59 ticks provide robustness |
| 5 | 500/600 Hz Ground Truth | 10-15 | 14 minutes/hour; most reliable |
| 6 | Doppler Stability | 2 | Requires stable channel; may fail in disturbed conditions |
| 7 | Timing Coherence | 3 | Requires test signal + BCD agreement |

- [ ] **Vote 4 integration**: Is tick SNR now being used correctly?
  - Added: `detect_tick_windows()` call in Step 2B
  - Passed to `finalize_discrimination()` as `tick_results`

- [ ] **RTP-based station prediction**: Is it improving discrimination?
  - `predict_station()` uses RTP offset to predict expected station
  - Should reduce flip-flopping on shared frequencies

- [ ] **Low-confidence rejection**: Are we correctly rejecting low-confidence results?
  - On shared frequencies, require MEDIUM confidence minimum
  - LOW confidence falls through to RTP prediction or channel name

#### 6. Ionospheric Propagation Limits

**Question:** What is the theoretical limit of timing accuracy?

- [ ] **Propagation mode ambiguity**: Can we distinguish 1F vs 2F vs 3F hops?
  - Mode delays differ by ~1-2ms per hop
  - Current uncertainty: ±2-5ms after mode identification

- [ ] **Ionospheric jitter**: What is the irreducible variance?
  - Typical: ±0.5-2ms from ionospheric turbulence
  - Severe conditions: ±5-10ms

- [ ] **Intra-station spread**: What is the expected variance across frequencies?
  - Current observation: WWV σ=4-5ms, CHU σ=4-5ms
  - This may be the ionospheric limit, not a system error

---

### KNOWN ISSUES AND LIMITATIONS

#### 1. Bootstrap Phase Sensitivity

**Issue:** Bootstrap requires high-SNR detections (>15 dB) with high confidence (>0.7).

**Impact:** May take longer to exit bootstrap in weak signal conditions.

**Mitigation:** Consider lowering thresholds or using cross-channel voting.

#### 2. Multi-Process State Coordination

**Issue:** 9 channel recorder processes share one state file.

**Current Fix:** Reload state before update, save after every detection during bootstrap.

**Potential Issue:** Race conditions possible if two processes update simultaneously.

**Mitigation:** Consider file locking or centralized state manager.

#### 3. CHU FSK Detection Sensitivity

**Issue:** CHU FSK decoder may not detect signal in weak conditions.

**Impact:** CHU timing confirmation unavailable when FSK not detected.

**Mitigation:** FSK is optional confirmation; system works without it.

#### 4. Discrimination Flip-Flopping

**Issue:** On shared frequencies, discrimination can flip between WWV and WWVH.

**Current Fix:** 
- Reject low-confidence discrimination
- Use RTP-based station prediction
- Store detected_station in RTP calibration

**Remaining Issue:** First detection on a new channel has no RTP history.

---

### VALIDATION COMMANDS

```bash
# Check timing calibration state
cat /tmp/timestd-test/state/timing_calibration.json | python3 -m json.tool

# Check broadcast calibration state
cat /tmp/timestd-test/state/broadcast_calibration.json | python3 -m json.tool

# Monitor fusion convergence
tail -f /tmp/timestd-test/logs/phase2-fusion.log

# Check intra-station spread
grep "intra-station" /tmp/timestd-test/logs/phase2-fusion.log | tail -10

# Verify discrimination corrections
grep "RTP prediction overrides" /tmp/timestd-test/logs/phase1-*.log
```

---

### SUCCESS CRITERIA

| Metric | Target | Current Status |
|--------|--------|----------------|
| D_clock accuracy | ±1 ms | ~±5 ms (discrimination errors dominate) |
| Intra-station spread | <5 ms | ~7-10 ms (discrimination + ionospheric) |
| Discrimination stability | No flip-flopping | 🔴 MAIN ISSUE - misidentification causes ~50ms errors |
| Bootstrap exit | <10 minutes | ✅ ~3-5 minutes |
| Kalman convergence | Grade A/B | Grade C/D (blocked by discrimination) |

### 2025-12-14 UPDATE: RTP-First Timing Implemented

**Root Cause Analysis Complete:**

The D_clock instability is **NOT** caused by RTP timing jitter. The RTP-first timing implementation confirmed that:

1. **RTP offset is stable**: Calibrated offset (e.g., `411038` for WWV 10 MHz) is deterministic
2. **D_clock clusters correctly by station**:
   - WWV: clusters around `-4ms` to `-6.5ms`
   - WWVH: clusters around `-27.9ms`
3. **Outliers are misidentified stations**: When WWVH is misidentified as WWV, wrong propagation delay is applied, causing ~50ms errors

**Remaining Issue: Station Discrimination**

The discrimination system is the bottleneck. When WWVH signals are misidentified as WWV (or vice versa), the wrong propagation delay is used, causing large D_clock errors. This is particularly problematic on shared frequencies (2.5, 5, 10, 15 MHz).

---

## ✅ COMPLETED: DATA FLOW CONTRACT ENFORCEMENT

**Purpose:** Critically examine how data is written and read across the GRAPE Recorder system, identifying mismatches between producers and consumers.

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-08  
**Status:** ✅ Complete - 10 Issues Identified and Fixed

### Summary (Session 2025-12-08)

| ID | Severity | Status | Description |
|----|----------|--------|-------------|
| 1.1 | HIGH | ✅ FIXED | Calibration per-station vs per-broadcast key mismatch |
| 1.2 | HIGH | ✅ FIXED | State file version not validated on load |
| 1.3 | HIGH | ✅ FIXED | Kalman state loaded without sanity checks |
| 2.1 | MEDIUM | ✅ FIXED | CSV column vs API field name documented |
| 2.2 | MEDIUM | ✅ FIXED | Python discover_channels() now checks all phases |
| 2.3 | MEDIUM | ✅ FIXED | PathResolver deprecated with warning |
| 2.4 | MEDIUM | ✅ FIXED | Mode coordination documented, reset script added |
| 2.5 | MEDIUM | ✅ FIXED | Storage quota implications documented |
| 3.1 | LOW | ✅ FIXED | Centralized version module created |
| 3.2 | LOW | ✅ FIXED | Standardized UTC timestamps |

### New Files Created
- `src/grape_recorder/version.py` - Centralized version and timestamp utilities
- `docs/STATE_FILES.md` - State file documentation and reset procedures
- `scripts/reset-state.sh` - Safe state reset script

---

## THE CORE PROBLEM

**Too often, the parts that WRITE important info to files (analytics services) and the parts that READ them (other analytics, web-ui) change the destination or expected source of information WITHOUT notifying the rest of the system.**

This leads to:
1. **Silent failures** - Readers find empty/missing data, return nulls, UI shows blanks
2. **Stale data** - State files persist incorrect values across restarts
3. **Hidden coupling** - No explicit contracts between producers and consumers
4. **Debugging hell** - Tracing where data comes from requires reading multiple files

---

## EXAMPLES FROM DEC 8 SESSION

### Example 1: Kalman State Persistence Poisoning

**Bug**: D_clock showed linear drift of ~6.5 ms/minute despite GPSDO discipline.

**Root Cause Chain**:
1. `phase2_analytics_service.py` was synthesizing RTP timestamps from Unix time instead of reading `start_rtp_timestamp` from metadata JSON
2. This produced incorrect D_clock values that were fed to the Kalman filter
3. The Kalman filter in `clock_convergence.py` persisted state to `convergence_state.json`
4. When the RTP bug was fixed, the Kalman filter CONTINUED using the corrupted state from the JSON file
5. New correct measurements were rejected as "5-sigma outliers" because the filter's prediction was 900+ ms off

**Hidden Data Contract**:
```
PRODUCER: phase2_analytics_service.py writes convergence_state.json
CONSUMER: phase2_analytics_service.py reads convergence_state.json on restart
CONTRACT: State must be valid when loaded - but NO VALIDATION exists
```

**Fix Required**: State files need versioning and sanity checks before loading.

### Example 2: Channel Discovery Across Data Directories

**Bug**: Channels appeared in Phase 2 output but `discoverChannels()` couldn't find them.

**Root Cause**: The discovery function only checked `raw_archive/` but channels may only exist in `phase2/` or `products/`.

**Hidden Data Contract**:
```
PRODUCER: Phase 1 creates raw_archive/{CHANNEL}/
PRODUCER: Phase 2 creates phase2/{CHANNEL}/
CONSUMER: grape-paths.js discoverChannels() assumes raw_archive is canonical
CONTRACT: UNDEFINED - no single source of truth for "what channels exist"
```

### Example 3: RTP Timestamp Metadata

**Bug**: Timing calculations drifted because RTP timestamps weren't being used.

**Root Cause**: `_read_binary_minute()` synthesized timestamps instead of reading them from the JSON metadata that sits alongside the binary data.

**Hidden Data Contract**:
```
PRODUCER: Binary writer creates {minute}.bin + {minute}.json
CONSUMER: _read_binary_minute() should read BOTH files
CONTRACT: IMPLICIT - metadata fields like start_rtp_timestamp are optional
```

---

## DATA FLOW INVENTORY TO AUDIT

### Phase 1 → Phase 2 Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `raw_archive_writer.py` | `raw_archive/{CH}/` | `phase2_analytics_service.py` | DRF format |
| `binary_minute_writer.py` | `raw_buffer/{CH}/{minute}.bin` | `phase2_analytics_service.py` | Binary IQ + JSON metadata |
| `binary_minute_writer.py` | `raw_buffer/{CH}/{minute}.json` | `phase2_analytics_service.py` | **start_rtp_timestamp required** |

### Phase 2 Internal Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `phase2_analytics_service.py` | `phase2/{CH}/status/analytics-service-status.json` | `monitoring-server-v3.js` | Status JSON schema |
| `phase2_analytics_service.py` | `phase2/{CH}/status/convergence_state.json` | `phase2_analytics_service.py` | Kalman state |
| `phase2_analytics_service.py` | `phase2/{CH}/clock_offset/*.csv` | `multi_broadcast_fusion.py` | CSV with d_clock_ms column |
| `multi_broadcast_fusion.py` | `state/broadcast_calibration.json` | `monitoring-server-v3.js` | Calibration per station |
| `multi_broadcast_fusion.py` | `phase2/fusion/fused_d_clock.csv` | `monitoring-server-v3.js` | Fused output CSV |

### Phase 2 → Web UI Data Contracts

| Producer File | Output Path | Consumer File | Contract |
|---------------|-------------|---------------|----------|
| `phase2_analytics_service.py` | `phase2/{CH}/status/*.json` | `transmission-time-helpers.js` | Status JSON fields |
| `multi_broadcast_fusion.py` | `state/broadcast_calibration.json` | `timing-dashboard-enhanced.html` | Calibration keys |
| Multiple | Various CSVs | `monitoring-server-v3.js` | Column names must match |

---

## CRITIQUE CHECKLIST

For each data producer/consumer pair, verify:

### 1. Schema Documentation
- [ ] Is the output format documented?
- [ ] Are required vs optional fields explicit?
- [ ] Is there a version number for the schema?

### 2. Validation on Read
- [ ] Does the consumer validate data before using it?
- [ ] Are there sanity checks for numeric ranges?
- [ ] Does it fail gracefully if data is missing/corrupt?

### 3. State File Hygiene
- [ ] Is persisted state versioned?
- [ ] Can stale state poison fresh calculations?
- [ ] Is there a mechanism to reset corrupted state?

### 4. Path Consistency
- [ ] Are paths constructed the same way in producer and consumer?
- [ ] Are there hardcoded paths that diverge from config?
- [ ] Does channel name sanitization match (`WWV 10 MHz` vs `WWV_10_MHz`)?

### 5. Timestamp Consistency
- [ ] Are timestamps Unix epoch, ISO string, or other?
- [ ] Are timezones explicit?
- [ ] Do column names match (`system_time` vs `timestamp` vs `utc_time`)?

---

## SPECIFIC FILES TO AUDIT

### High Priority (State Persistence)
1. `clock_convergence.py` - Kalman state save/load
2. `multi_broadcast_fusion.py` - Calibration state save/load
3. `phase2_analytics_service.py` - All file writes

### Medium Priority (CSV Contracts)
4. `clock_offset_series.py` - CSV column definitions
5. `carrier_power_writer.py` - CSV format
6. `monitoring-server-v3.js` - CSV parsing logic

### Lower Priority (Path Management)
7. `grape-paths.js` - Path construction functions
8. `phase2_analytics_service.py` - Output directory creation
9. Various HTML files - Hardcoded API endpoints

---

## PREVIOUS SESSION: Phase 2 Analytics Critique (Dec 7-8)

**Status:** ✅ Critique Complete - 16 Issues Addressed

The critical review identified **17 issues**, of which **16 were fixed** and **1 was invalidated**:

### Summary Table

| ID | Category | Severity | Status | Description |
|----|----------|----------|--------|-------------|
| 1.1 | Methodology | High | ✅ FIXED | Matched filter template length mismatch |
| 1.2 | Methodology | High | ✅ FIXED | Fixed ionospheric layer heights |
| 1.3 | Methodology | Medium | ✅ FIXED | Ionospheric delay model oversimplified |
| 2.1 | Discrimination | Medium | ✅ FIXED | Unvalidated voting weights |
| 2.2 | Discrimination | Medium | ✅ FIXED | Correlation between methods not modeled |
| 2.3 | Discrimination | Low | ✅ FIXED | Binary classification loses information |
| 3.1 | Statistics | High | ✅ FIXED | Wrong model for non-stationary data |
| 3.2 | Statistics | Medium | ⚠️ PARTIAL | Multi-broadcast fusion (per-station, not per-broadcast) |
| 4.1 | Bug | Medium | ✅ FIXED | Inconsistent station coordinates |
| 4.2 | Bug | Low | ❌ INVALID | Tone duration - NIST confirms 800ms correct |
| 4.3 | Bug | Low | ✅ FIXED | Hardcoded default calibration offsets |
| 5.1 | Enhancement | Medium | ✅ FIXED | No use of phase information |
| 5.2 | Enhancement | Medium | ✅ FIXED | No multipath detection |
| 5.3 | Enhancement | Low | ✅ FIXED | No cross-correlation WWV/WWVH |
| 5.4 | Enhancement | Low | ✅ FIXED | No CHU FSK time code exploitation |
| 6.1 | Validation | High | ✅ FIXED | No ground truth validation mechanism |
| 6.2 | Validation | Low | ✅ FIXED | Quality grades are arbitrary |

### New Modules Created

| Module | Lines | Purpose |
|--------|-------|---------|
| `ionospheric_model.py` | ~600 | Dynamic layer heights (IRI-2016/parametric) |
| `ground_truth_validator.py` | ~700 | GPS PPS, silent minute, mode validation |
| `probabilistic_discriminator.py` | ~750 | Logistic regression with L2 regularization |
| `advanced_signal_analysis.py` | ~900 | Phase correlation, multipath, CHU FSK |

### Key Changes

1. **Issue 6.2**: `quality_grade` (A/B/C/D) replaced with `uncertainty_ms` + `confidence`
2. **Backwards Compatibility**: Grade computed from uncertainty for web UI:
   - A: < 1 ms
   - B: < 3 ms
   - C: < 10 ms
   - D: ≥ 10 ms

**Full details**: `docs/PHASE2_CRITIQUE.md`

---

## 🚨 NEXT PRIORITY: PHASE 3 PIPELINE IMPLEMENTATION

**Purpose:** Implement the Phase 3 derived products pipeline - decimation, spectrograms, power graphs, and GRAPE/PSWS upload.

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-08 (Next Session)  
**Status:** 🔴 Not Started

---

### PHASE 3 OVERVIEW

Phase 3 produces derived products from Phase 2 analytical data:

```
Phase 2 Output (20 kHz IQ)
    ↓
┌─────────────────────────────────────────────────────────────┐
│                    PHASE 3 PIPELINE                         │
├─────────────────────────────────────────────────────────────┤
│  1. Decimation: 20 kHz → 10 Hz (carrier amplitude/phase)    │
│  2. Spectrogram: Daily carrier frequency/amplitude plot      │
│  3. Power Graphs: Carrier power with solar zenith overlay   │
│  4. Digital RF Product: 24-hour UTC day archive (HamSCI)    │
│  5. Upload: GRAPE/PSWS data repository submission           │
└─────────────────────────────────────────────────────────────┘
    ↓
products/{CHANNEL}/
├── decimated/YYYYMMDD.bin         # 10 Hz carrier data
├── spectrograms/YYYYMMDD.png      # Daily spectrogram
├── power/YYYYMMDD_power.png       # Power graph + solar zenith
└── drf/YYYYMMDD/                  # Digital RF for upload
```

---

### COMPONENT 1: DECIMATION (20 kHz → 10 Hz)

**Goal:** Extract carrier amplitude and phase at 10 Hz for efficient storage and analysis.

**Input:**
- `raw_archive/{CHANNEL}/` - 20 kHz complex IQ from Phase 1

**Output:**
- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data

**Implementation Notes:**
- Use scipy.signal.decimate or polyphase filter
- Extract carrier: mix to baseband, lowpass filter, decimate
- Output format: binary float32 (amplitude, phase) pairs
- File size: ~7 MB/day/channel (10 Hz × 86400 sec × 8 bytes)

**Existing Code to Review:**
| File | Status | Notes |
|------|--------|-------|
| `archive/legacy-grape-modules/decimator.py` | ⚠️ Legacy | May have useful algorithms |
| `scripts/analyze_decimation_quality.py` | ✅ Active | Quality analysis script |

---

### COMPONENT 2: CARRIER SPECTROGRAM

**Goal:** Generate daily spectrogram showing carrier frequency/amplitude variations.

**Input:**
- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data

**Output:**
- `products/{CHANNEL}/spectrograms/YYYYMMDD_spectrogram.png`

**Implementation Notes:**
- X-axis: UTC time (00:00 - 24:00)
- Y-axis: Frequency offset from carrier (±0.5 Hz typical)
- Color: Signal amplitude (dB)
- Show ionospheric Doppler shifts, propagation mode changes

**Existing Code to Review:**
| File | Status | Notes |
|------|--------|-------|
| `scripts/generate_spectrograms.py` | ⚠️ Archive | Check for reuse |
| `scripts/auto-generate-spectrograms.sh` | ✅ Active | Automation script |
| `docs/features/AUTOMATIC_SPECTROGRAM_GENERATION.md` | ✅ Reference | Design doc |

---

### COMPONENT 3: POWER GRAPHS WITH SOLAR ZENITH OVERLAY

**Goal:** Visualize carrier power alongside solar zenith angle for ionospheric correlation.

**Input:**
- `products/{CHANNEL}/decimated/YYYYMMDD.bin` - 10 Hz carrier data
- Station coordinates from `timestd-config.toml`
- Transmitter coordinates (WWV: 40.68°N, 105.04°W)

**Output:**
- `products/{CHANNEL}/power/YYYYMMDD_power.png`

**Implementation Notes:**
- Primary Y-axis: Carrier power (dB)
- Secondary Y-axis: Solar zenith angle (degrees)
- Solar zenith calculation: Uses NOAA algorithms via `solar_zenith_calculator.py`
- Calculates at **path midpoint** (halfway between receiver and transmitter)
- Show sunrise/sunset transitions, D-layer absorption effects

**Solar Zenith Calculation (Already Implemented):**
```python
from grape_recorder.grape.solar_zenith_calculator import (
    calculate_solar_zenith_for_day,
    calculate_midpoint,
    solar_position
)

# Get solar elevation at path midpoints for all stations
solar_data = calculate_solar_zenith_for_day(date_str, receiver_grid)
# Returns: wwv_solar_elevation, wwvh_solar_elevation, chu_solar_elevation arrays
```

**Dependencies:** None required (uses pure Python NOAA algorithms)

---

### COMPONENT 4: DIGITAL RF PRODUCT (24-HOUR UTC DAY)

**Goal:** Package 24-hour UTC day of data in Digital RF format for HamSCI PSWS compatibility.

**Input:**
- `raw_archive/{CHANNEL}/` - 20 kHz complex IQ

**Output:**
- `products/{CHANNEL}/drf/YYYYMMDD/` - Digital RF directory structure

**Implementation Notes:**
- Digital RF format: HDF5 files with specific structure
- Time boundary: 00:00:00 UTC to 23:59:59 UTC
- Metadata: Station info, receiver config, GPSDO status
- Use existing `digital_rf` library

**Existing Code to Review:**
| File | Status | Notes |
|------|--------|-------|
| `src/grape_recorder/core/drf_writer.py` | ✅ Active | Real-time DRF writer |
| `archive/legacy-grape-modules/core_npz_writer.py` | ⚠️ Legacy | NPZ alternative |

---

### COMPONENT 5: GRAPE/PSWS UPLOAD

**Goal:** Upload completed daily products to the GRAPE data repository.

**Input:**
- `products/{CHANNEL}/drf/YYYYMMDD/` - Digital RF package
- `products/{CHANNEL}/spectrograms/YYYYMMDD.png`

**Destination:**
- GRAPE/PSWS data repository (TBD - endpoint configuration)

**Implementation Notes:**
- Upload after 00:00 UTC (previous day complete)
- Verify file integrity before upload (checksums)
- Track upload state to prevent duplicates
- Retry logic for network failures

**Existing Code to Review:**
| File | Status | Notes |
|------|--------|-------|
| `wsprdaemon/upload-client-utils.sh` | ✅ Active | Upload utilities |
| `systemd/grape-daily-upload.service` | ✅ Active | Systemd timer |
| `systemd/grape-daily-upload.timer` | ✅ Active | Daily trigger |

**Configuration to Check:**
| File | Setting | Purpose |
|------|---------|---------|
| `timestd-config.toml` | `[uploader]` section | Upload credentials/endpoint |
| `config/environment` | `GRAPE_UPLOAD_*` | Environment variables |

---

### DATA CONTRACTS FOR PHASE 3

| Producer | Output Path | Consumer | Contract |
|----------|-------------|----------|----------|
| Decimator | `products/{CH}/decimated/YYYYMMDD.bin` | Spectrogram Generator | Binary float32 pairs |
| Decimator | `products/{CH}/decimated/YYYYMMDD.json` | Uploader | Metadata (sample rate, start time) |
| Spectrogram Gen | `products/{CH}/spectrograms/YYYYMMDD.png` | Web UI, Uploader | PNG image |
| Power Graph Gen | `products/{CH}/power/YYYYMMDD_power.png` | Web UI | PNG with solar overlay |
| DRF Packager | `products/{CH}/drf/YYYYMMDD/` | Uploader | Digital RF structure |
| Uploader | `products/{CH}/upload_state.json` | Uploader | Prevents re-upload |

---

### CRITIQUE CHECKLIST FOR PHASE 3

#### 1. Decimation Quality
- [ ] Does the filter preserve carrier phase information?
- [ ] Is anti-aliasing sufficient (stopband attenuation > 60 dB)?
- [ ] Are edge effects handled at day boundaries?

#### 2. Spectrogram Accuracy
- [ ] Is time axis aligned to UTC?
- [ ] Does frequency axis match actual carrier offset range?
- [ ] Are colormaps appropriate for the data range?

#### 3. Solar Zenith Calculation
- [ ] Are coordinates correct for both receiver AND transmitter?
- [ ] Is the calculation for the MIDPOINT of the propagation path?
- [ ] Are time zones handled correctly (UTC throughout)?

#### 4. Digital RF Compliance
- [ ] Does output match HamSCI PSWS format specification?
- [ ] Are all required metadata fields present?
- [ ] Is the file structure compatible with existing tools?

#### 5. Upload Robustness
- [ ] Is there retry logic for transient failures?
- [ ] Are credentials stored securely (not in code)?
- [ ] Is upload state persisted across service restarts?

---

### FILES TO CREATE/MODIFY

**New Files:**
| File | Purpose |
|------|---------|
| `src/grape_recorder/grape/decimator.py` | 20 kHz → 10 Hz decimation |
| `src/grape_recorder/grape/spectrogram_generator.py` | Daily spectrogram creation |
| `src/grape_recorder/grape/power_graph_generator.py` | Power + solar zenith plots |
| `src/grape_recorder/grape/drf_packager.py` | 24-hour DRF packaging |
| `src/grape_recorder/grape/uploader.py` | GRAPE repository upload |
| `src/grape_recorder/grape/phase3_pipeline.py` | Pipeline orchestration |

**Files to Update:**
| File | Change |
|------|--------|
| `requirements.txt` | Add `astropy` for solar calculations |
| `systemd/grape-daily-upload.service` | Update for new pipeline |
| `timestd-config.toml` | Add Phase 3 configuration section |

---

## ORIGINAL CRITIQUE CONTEXT (Reference)

The sections below document the original problem being solved and the theoretical framework used for the critique. Preserved for future reference.

### 1. THE PROBLEM BEING SOLVED

**Objective**: Extract precise UTC(NIST) time from HF radio signals transmitted by WWV, WWVH, and CHU time signal stations, achieving sub-millisecond accuracy despite ionospheric propagation delays of 2-60 ms.

**The Fundamental Equation**:
```
T_arrival = T_emission + T_propagation + D_clock

Where:
  T_arrival = Detected tone time (from matched filter)
  T_emission = 0 (by definition - tones at second boundary)
  T_propagation = Ionospheric path delay (ESTIMATED)
  D_clock = System clock offset from UTC(NIST) (DESIRED OUTPUT)

Therefore:
  D_clock = T_arrival - T_propagation
```

### 2. THREE-STEP TEMPORAL ANALYSIS

| Step | Method | Window | Output |
|------|--------|--------|--------|
| 1 | Tone Detection | ±500 ms | Time snap anchor |
| 2 | Channel Characterization | ±50 ms | Station ID, mode hints |
| 3 | Transmission Time Solution | — | D_clock value |

### 3. THEORETICAL REFERENCES

- **NIST Special Publication 432** - WWV/WWVH specifications
- **ITU-R P.531** - Ionospheric propagation prediction
- **ITU-R P.533** - HF propagation method
- **IRI-2016** - International Reference Ionosphere model

### 4. KEY FILES FOR PHASE 2 ANALYTICS

| File | Purpose |
|------|---------|
| `tone_detector.py` | Matched filter, correlation |
| `transmission_time_solver.py` | Mode scoring, D_clock |
| `phase2_temporal_engine.py` | Pipeline orchestration |
| `wwvh_discrimination.py` | Station discrimination |
| `clock_convergence.py` | Kalman filter tracking |
| `multi_broadcast_fusion.py` | 13-broadcast fusion |

### 5. SUCCESS CRITERIA

| Metric | Target | Status |
|--------|--------|--------|
| D_clock accuracy | ±1 ms | ✅ Framework in place |
| Station discrimination | 95% | ✅ Probabilistic model added |
| Propagation mode ID | 80% | ✅ Dynamic iono model added |
| Time to lock | < 30 min | ✅ Kalman filter added |
| Validation | Ground truth | ✅ GPS PPS + silent minutes |

---

*This document has been updated to reflect the completed critique. The next priority is ensuring web UI correctly interfaces with the updated analytics modules.*
