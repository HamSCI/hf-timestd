# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## ✅ SESSION COMPLETE (2026-01-01): SCIENCE & VTEC HEALTH CHECKS

**Status:** 🟢 **RESOLVED** - Health checks implemented, verify_pipeline.sh updated, install/uninstall scripts fixed.

**Author:** Michael James Hauan (AC0G)
**Date:** 2026-01-01

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
