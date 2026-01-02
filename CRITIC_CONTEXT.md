# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## ✅ SESSION COMPLETE (2026-01-02): FUSION VULNERABILITY FIXES

**Status:** 🟢 **RESOLVED** - Critical vulnerabilities in Fusion service fixed (VTEC safety, Global Solver check, HDF5 parity, Warmup penalty removal).

**Author:** AI Agent (Antigravity)
**Date:** 2026-01-02

### Main Accomplishments

1. **VTEC Safety**: Implemented consistency checks before boosting confidence in GNSS VTEC corrections.
2. **Robustness**: Removed "God Mode" immunity for Global Solver; it is now subject to outlier rejection.
3. **HDF5 Parity**: Harmonized HDF5 reader to accept Grade D measurements, preventing data starvation during fallback.
4. **Availability**: Removed artificial 3-hour warmup penalty when calibration is loaded from disk.

---

## 🔴 NEXT SESSION FOCUS: ANALYTICS CRITIQUE - TARGETED TONE SEARCH

**Purpose:** The current tone detection strategy searches wide windows (±500ms) or heuristic adaptive windows (±5-50ms) but lacks precision. We want to **improve sensitivity and specificity** by determining *exactly* where tones of interest should be based on prior knowledge (Physics + Fusion feedback).

**Objective:** "Zero-in on where we know the tones of interest will be."

### Potential Areas for Improvement

#### 1. Fusion-Aided Analytics

- **Idea**: Use the *fused* solution (L3) to feedback a precise prediction to the *individual* station analytics (L2).
- **Benefit**: If we know UTC(NIST) ±1ms from the ensemble, we shouldn't be searching ±500ms for a single station. We can search ±2ms, drastically rejecting noise.

#### 2. Physics-Aided Search Windows

- **Idea**: Use `Phase2TemporalEngine` physics (IRI-2020) not just for centering, but for *tight* window sizing.
- **Benefit**: Reject false positives that are physically impossible (e.g., propagation delay < 3ms for 1000km path).

#### 3. Signal Feature Validation

- **Idea**: Critique the current "Robust Noise Floor" implementation. Is it truly robust? Can we use spectral features (Doppler spread, spectral width) to better discriminate tones from interference?

---

## Investigation Plan for AI Agent

### Step 1: Analyze Current Analytics Logic

Review `phase2_analytics_service.py` and `tone_detector.py`.

- How is the search window currently determined?
- Is there any feedback loop from L3 (Fusion) to L2 (Analytics)?

### Step 2: Prototype Targeted Search

- Design a mechanism for Analytics to ingest the latest "Time Standard" (Fusion Clock Offset).
- Restrict search windows based on this feedback.

### Step 3: Verify Sensitivity

- Test with weak signal recordings. Does the targeted search find tones that were previously missed?

---

## Current System State (Background)

**Services:** All Active

- **Fusion**: Stabilized (Version 3.8.0), robust "Critic" logic active.
- **Analytics**: Working but potentially inefficient (wide search).
- **Chrony**: Accepting updates.

**Recent Major Changes:**

- 2026-01-02: Fusion service critical fixes (VTEC, Global Solver, HDF5 Parity).
- 2025-12-31: Analytics "Robust Noise Floor" and "Adaptive Search Windows".

---

## Success Criteria for Next Session

1. **Critique Report**: Identification of inefficiencies in current tone search.
2. **Design**: Proposal for a "Targeted Tone Search" mechanism (Fusion feedback loop).
3. **Implementation**: Update Analytics to use priors for narrowing search space.
4. **Metric**: Demonstrate improved detection rate or reduced false positives.

### Main Accomplishments

1. **Health Checks**: Created `health-check-science.sh` and `health-check-vtec.sh`.
2. **Pipeline Verification**: Updated `verify_pipeline.sh` to include **Phase 4: Science Products**.
3. **Service Management**: Added `timestd-science-aggregator` to `install.sh` and `uninstall.sh`.
4. **Integration**: All services (Recorder, Analytics, Fusion, Science, VTEC, WebUI) are running and monitored.

---

## 🔴 NEXT SESSION FOCUS: INVESTIGATE DEGRADED TIMING QUALITY (GRADE D)

**Purpose:** The system is operational and processing data, but the **Timing Quality Grade has dropped to 'D'** across the board. We need to diagnose why the system considers the timing estimate to be of poor quality despite having valid inputs.

**Trigger:** User report "Grades all 'D'" and uploaded image `uploaded_image_1767312248802.png` showing a timeline dominated by red (Grade D) bars.

### Context & visual cues

![Quality Timeline](/home/mjh/.gemini/antigravity/brain/6b23b02b-ff90-4674-b970-ab973963ca8f/uploaded_image_1767312248802.png)
*Figure: Metrics showing degraded quality grades.*

### Potential Causes to Investigate

#### 1. Fusion / Calibration Logic (High Probability)

- **Problem**: The fused solution might have high variance or uncertainty, leading to a downgrade.
- **Check**: Look at `fused_d_clock.csv` for `uncertainty_ms` and `consistency_flag`.
- **Hypothesis**: The recent HDF5 migration might have altered how uncertainty is read or calculated.

#### 2. Chrony Integration & Reachability

- **Note**: `verify_pipeline.sh` showed `TMGR source reachable (reach: 3)`.
- **Problem**: `reach=3` (binary 00000011) means only the last 2 polls succeeded. It takes 8 successful polls (reach 377) to be fully stable.
- **Hypothesis**: The system might just be warming up, OR Chrony is rejecting samples due to high variance.

#### 3. Analytics Quality (Input Garbage?)

- **Problem**: If the individual station measurements (L2) are noisy or wrong, Fusion will have high uncertainty.
- **Check**: Compare individual channel plots. Are they tight or scattered?
- **Hypothesis**: Recent changes to "Robust Noise Floor" or "Adaptive Search Windows" (mentioned in CHANGELOG 3.3.0) might be rejecting good signals or letting noise in.

#### 4. Timestamp / Datatype Issues

- **Problem**: Recent fixes to IRI-2020 array handling or HDF5 schemas might have introduced subtle value errors (e.g. seconds vs milliseconds, or integer truncation).

---

## Investigation Plan for AI Agent

### Step 1: Analyze Fusion Output (The Symptom)

Check the raw numbers driving the "Grade D" classification.

```bash
# Get recent fusion records
tail -20 /var/lib/timestd/phase2/fusion/fused_d_clock.csv

# Questions to answer:
# 1. What is the 'uncertainty_ms'? (Grade A < 1ms, Grade D > ???)
# 2. What is the 'quality_flag'?
# 3. How many 'num_stations' are contributing? (Low count = low confidence)
```

### Step 2: Check Individual Station Inputs

Is the input to fusion garbage?

```bash
# Check L2 for a stable station (e.g., WWV 10MHz)
tail -20 /var/lib/timestd/phase2/WWV_10MHz/clock_offset/WWV_10MHz_clock_offset.csv

# Questions:
# 1. Are 'd_clock' values stable?
# 2. Is 'confidence' high?
```

### Step 3: Check Logs for Logic Errors

```bash
sudo journalctl -u timestd-fusion --since "1 hour ago" | grep -i "warning\|error\|reject"
sudo journalctl -u timestd-analytics --since "1 hour ago" | grep -i "warning\|error"
```

---

## Current System State (Background)

**Services:** All Active (systemd managed)
**Data Pipeline:**

- L0 (Digital RF/Binary): Active
- L2 (Timing): Active (HDF5+CSV)
- L3 (Fusion): Active (HDF5+CSV)
- Chrony: **Connected** (Reach 3, climbing)

**Recent Major Changes:**

- HDF5 Migration (Phase 1-3 complete)
- Health Checks implementation
- Science Aggregator & VTEC integration

---

## Success Criteria for Next Session

1. **Identify the Root Cause** of the "Grade D" status.
2. **Proposed Fix** (Configuration tune, code fix, or waiting for warm-up).
3. **Verify Grade Recovery** (Return to A/B grades).
