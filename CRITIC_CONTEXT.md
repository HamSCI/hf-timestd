# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer.  These perspectives can differ in their priorities and interests, and your critique should reflect this.  For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality and maintainability of the system.  Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🎯 NEXT SESSION OBJECTIVE: FIX ANALYTICS D_CLOCK CALCULATION FAILURE

**Status:** 🔴 **CRITICAL** - Analytics producing D_clock=+0.00ms despite valid tone detections and propagation solutions

**Author:** AI Agent (Cascade)
**Date:** 2026-01-06 16:40 UTC
**Session:** Post-ionospheric validation fix, chrony feed restoration

### Session Goal

**Primary Objective:** Diagnose and fix why analytics Phase 2 processing produces `D_clock=+0.00ms` (null measurements) instead of valid clock offset estimates, preventing fusion from producing chrony feed data.

**Critical Problem:** Analytics completes "Step 3 Solution" with valid propagation modes and confidence values, but final output shows `D_clock=+0.00ms, uncertainty=100.0ms`, which gets written as NaN to HDF5 files. This prevents fusion TEC solver from working and blocks chrony feed restoration.

### Context from Previous Session (2026-01-06 13:42-16:40 UTC)

**Chrony Feed Stoppage Root Cause Identified:**

1. **Timeline:**
   - **Jan 4th:** Strict ionospheric validation code deployed in `transmission_time_solver.py`
   - **Jan 6th, 13:42 UTC:** Chrony feed stopped - fusion ceased producing estimates
   - **Cause:** Ionospheric conditions exceeded model predictions → all measurements rejected

2. **Ionospheric Validation Fix (DEPLOYED 2026-01-06 ~16:00 UTC):**
   - **File:** `src/hf_timestd/core/transmission_time_solver.py` lines 837-848
   - **Change:** Modified validation to WARN instead of REJECT when ionospheric delays exceed model predictions
   - **Old:** `return None` when `iono_delay_ms > max_iono_expected`
   - **New:** `plausibility *= 0.5` (accept with reduced confidence)
   - **Rationale:** Measurements are ground truth; model deviations are scientific data, not errors
   - **Status:** ✅ DEPLOYED - Analytics now accepts unusual ionospheric conditions

3. **Confidence Threshold Fix (DEPLOYED 2026-01-06 ~16:30 UTC):**
   - **File:** `src/hf_timestd/core/phase2_analytics_service.py` line 2492
   - **Change:** Lowered threshold from `confidence > 0.1` to `confidence > 0.0`
   - **Rationale:** Accept all valid solutions, even low-confidence ones from unusual ionospheric conditions
   - **Status:** ✅ DEPLOYED - But measurements still showing D_clock=+0.00ms

4. **Data Product Registry:**
   - **Status:** ✅ VERIFIED WORKING - All services correctly using registry
   - Fusion reads from `clock_offset/` subdirectories
   - Analytics writes to `clock_offset/` subdirectories
   - DataProductReader automatically resolves paths

### The Core Problem: Analytics D_clock Calculation Failure

**Symptom:** Analytics logs show valid "Step 3 Solution" but final output is null measurement:

```
2026-01-06 16:36:15,128 - INFO - Step 3 Solution: D_clock=+0.00ms, station=CHU, mode=3F, confidence=0.50
2026-01-06 16:36:15,128 - INFO - Phase 2 processing complete for CHU: D_clock=+0.00ms, uncertainty=100.0ms
```

**Impact Chain:**
1. Analytics produces `D_clock=+0.00ms` → written as NaN to HDF5
2. Fusion reads L2 data → only ~30% have valid clock_offset_ms values
3. Fusion TEC solver needs multiple valid clock offsets per station → gets insufficient data
4. Fusion TEC solver produces NaN → cannot compute ionospheric corrections
5. Fusion cannot produce fused estimates → no chrony feed data
6. Chrony feed remains down (stopped at 13:42 UTC, now 3+ hours)

**What IS Working:**
- ✅ Tone detection: Tones are being detected with valid SNR
- ✅ Propagation mode selection: Multiple modes evaluated (1E, 1F, 2F, 3F)
- ✅ Ionospheric delay calculation: Values computed (0.7-1.2ms) with warnings
- ✅ Confidence values: Non-zero (0.05-0.50)
- ✅ HDF5 writing: Files being updated, propagation_delay_ms has valid values

**What is FAILING:**
- ❌ Final clock offset calculation: Produces `D_clock=+0.00ms` instead of actual offset
- ❌ Uncertainty assignment: Shows `uncertainty=100.0ms` (invalid measurement marker)
- ❌ HDF5 clock_offset_ms field: Written as NaN despite valid propagation solutions

**Critical Questions:**

1. **Where does D_clock get set to +0.00ms?**
   - Is it in the weighted average calculation across propagation modes?
   - Is it a fallback when all modes have low plausibility?
   - Is there a threshold that's rejecting all modes after Step 3?

2. **Why does Step 3 Solution show valid values but final output doesn't?**
   - What happens between "Step 3 Solution" log and "Phase 2 processing complete" log?
   - Is there a final validation step that's rejecting the solution?
   - Is the confidence threshold being applied twice?

3. **What determines uncertainty=100.0ms?**
   - This appears to be a sentinel value for "invalid measurement"
   - What condition triggers this assignment?
   - Is this related to the D_clock=+0.00ms issue?

4. **Why do some measurements succeed while most fail?**
   - Current data shows ~30% valid clock offsets, 70% NaN
   - What's different about the successful measurements?
   - Is it related to specific propagation modes or confidence levels?

### Diagnostic Methodology for Next Session

**Step 1: Trace D_clock Calculation Path (45 min)**

**Goal:** Identify where D_clock gets set to +0.00ms between Step 3 Solution and final output

**Actions:**
1. Search for "D_clock=+0.00ms" assignment in `phase2_analytics_service.py`
2. Find code between "Step 3 Solution" log (line ~2482) and "Phase 2 processing complete" log
3. Look for weighted average calculation across propagation modes
4. Check for validation thresholds that might reject all modes
5. Identify where `uncertainty=100.0ms` gets assigned

**Key Code Sections to Examine:**
- Lines 2480-2520: Post-Step 3 processing and final D_clock assignment
- Lines 700-850: L2 timing measurement construction and HDF5 writing
- Search for: `d_clock_ms`, `primary_result`, `uncertainty=100`, `D_clock=+0.00`

**Step 2: Analyze Propagation Mode Weighting (30 min)**

**Goal:** Understand how multiple propagation modes get combined into final D_clock

**Actions:**
1. Find where propagation mode solutions are weighted/averaged
2. Check if low plausibility values cause all modes to be rejected
3. Verify if the 0.5× plausibility reduction from ionospheric warnings is too aggressive
4. Look for minimum plausibility thresholds

**Hypothesis to Test:**
- After ionospheric validation reduces plausibility to 0.5×, the weighted average might produce D_clock=+0.00ms
- There may be a minimum plausibility threshold that's rejecting all modes
- The final validation step might be more strict than the Step 3 confidence check

**Step 3: Compare Successful vs Failed Measurements (30 min)**

**Goal:** Identify what makes the ~30% successful measurements different

**Actions:**
1. Extract analytics logs for both successful and failed measurements
2. Compare propagation modes, confidence values, plausibility scores
3. Check if successful measurements use specific modes (e.g., 3F vs 1E/1F/2F)
4. Determine if there's a pattern in when D_clock is valid vs +0.00ms

**Data to Collect:**
```bash
# Get recent analytics logs showing both success and failure
sudo cat /proc/$(pgrep -f "phase2_analytics.*CHU_14670")/fd/2 | grep -A 5 "Step 3 Solution"

# Check L2 HDF5 for pattern in valid vs NaN clock offsets
# Look for correlation with propagation mode, confidence, or time of day
```

**Step 4: Test Hypothesis and Apply Fix (45 min)**

**Potential Fixes Based on Root Cause:**

**If issue is plausibility threshold:**
- Lower minimum plausibility threshold for mode acceptance
- Adjust ionospheric warning plausibility penalty (0.5× → 0.7×)

**If issue is weighted average logic:**
- Fix calculation to handle low-plausibility modes correctly
- Ensure at least one mode contributes to final D_clock

**If issue is post-Step 3 validation:**
- Remove or relax final validation step
- Ensure confidence > 0.0 threshold is actually being applied

**If issue is uncertainty calculation:**
- Fix logic that assigns uncertainty=100.0ms
- Ensure valid D_clock values don't get marked as invalid

### Critical Files for Investigation

**PRIMARY FOCUS - Analytics D_clock Calculation:**
* `src/hf_timestd/core/phase2_analytics_service.py`:
  - Lines 2480-2520: Post-Step 3 processing, final D_clock assignment
  - Lines 700-850: L2 timing measurement construction
  - Line 2492: Confidence threshold (changed from 0.1 to 0.0)
  - Search for: "D_clock=+0.00ms", "uncertainty=100", weighted average logic

* `src/hf_timestd/core/phase2_temporal_engine.py`:
  - Step 3 solution logic and propagation mode weighting
  - Plausibility calculation and mode selection
  - Lines where final D_clock is computed from multiple modes

* `src/hf_timestd/core/transmission_time_solver.py`:
  - Lines 837-848: Ionospheric validation (modified to warn vs reject)
  - Plausibility reduction logic (currently 0.5× for unusual delays)
  - Check if plausibility thresholds are too strict

**SECONDARY - Fusion TEC Solver:**
* `src/hf_timestd/core/multi_broadcast_fusion.py`:
  - TEC solver that's producing NaN values
  - Requires multiple valid clock_offset_ms per station
  - Currently failing due to insufficient valid L2 data

**Data Locations:**
* L2 Timing: `/var/lib/timestd/phase2/CHU_14670/clock_offset/CHU_14670_timing_measurements_20260106.h5`
  - Check: ~30% have valid clock_offset_ms, 70% are NaN
  - Valid: propagation_delay_ms values present
  - Invalid: clock_offset_ms = NaN despite valid propagation solutions

**Data Locations:**
* L2 Timing: `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/{CHANNEL}_timing_measurements_YYYYMMDD.h5`
* TEC Output: `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_YYYYMMDD.h5`
* Fusion Output: `/var/lib/timestd/phase2/fusion/AGGREGATED_fusion_timing_YYYYMMDD.h5`

**Verification Scripts:**
* `scripts/check_tec_values.py`: Check TEC values (currently has numpy error)
* `scripts/verify_tec_fix.py`: Verify raw_arrival_time_ms in HDF5 files

### Current System State (2026-01-06 16:40 UTC)

**Services Running:**
- ✅ timestd-core-recorder: Active, producing L0 raw IQ data
- ✅ timestd-analytics: Active, but producing mostly D_clock=+0.00ms
- ✅ timestd-fusion: Active, but not producing estimates (TEC solver fails)
- ✅ timestd-vtec: Active (restarted)
- ✅ timestd-science-aggregator: Active (restarted)
- ✅ timestd-web-api: Active

**Data Quality:**
- ✅ L1 Tone Detection: Working, tones detected with valid SNR
- ⚠️ L2 Timing Measurements: ~30% valid clock_offset_ms, 70% NaN
- ❌ L3 Fusion: No estimates since 13:42 UTC (3+ hours ago)
- ❌ Chrony Feed: Down, no data since 13:42 UTC

**Recent Fixes Applied:**
1. ✅ Ionospheric validation: Changed to warn instead of reject (lines 837-848)
2. ✅ Confidence threshold: Lowered from 0.1 to 0.0 (line 2492)
3. ✅ Data Product Registry: Verified all services using correctly

**Blocking Issue:**
Analytics produces `D_clock=+0.00ms, uncertainty=100.0ms` for most measurements, preventing fusion from working. Must fix analytics D_clock calculation before chrony feed can be restored.

**Example Log Showing Problem:**
```
2026-01-06 16:36:14,855 - WARNING - UNUSUAL: Ionospheric delay 0.852ms exceeds model prediction 0.480ms for 14.67MHz, 1 hops - accepting with reduced confidence
2026-01-06 16:36:14,920 - WARNING - UNUSUAL: Ionospheric delay 0.726ms exceeds model prediction 0.480ms for 14.67MHz, 1 hops - accepting with reduced confidence
2026-01-06 16:36:14,991 - WARNING - UNUSUAL: Ionospheric delay 1.058ms exceeds model prediction 0.960ms for 14.67MHz, 2 hops - accepting with reduced confidence
2026-01-06 16:36:15,128 - WARNING - UNUSUAL: Ionospheric delay 1.213ms exceeds model prediction 0.960ms for 14.67MHz, 2 hops - accepting with reduced confidence
2026-01-06 16:36:15,128 - INFO - Step 3 Solution: D_clock=+0.00ms, station=CHU, mode=3F, confidence=0.50
2026-01-06 16:36:15,128 - INFO - Phase 2 processing complete for CHU: D_clock=+0.00ms, uncertainty=100.0ms
```

Note: Multiple propagation modes evaluated (1E, 1F, 2F, 3F), all accepted with warnings, but final D_clock is still +0.00ms.

### Design Considerations for Fix

**Bootstrap vs Operational Validation:**

User insight: The system needs two validation modes:

1. **Bootstrap Mode:** Use propagation models as hard boundaries to filter false detections
   - Strict validation appropriate when we don't have confident offset estimates
   - Reject measurements that clearly violate physical constraints

2. **Operational Mode:** Once we have confident estimates, treat model deviations as scientific data
   - Accept measurements that exceed model predictions
   - Reduce confidence/uncertainty instead of rejecting
   - Model deviations become ionospheric science, not errors

**Current State:** System is stuck in bootstrap mode with overly strict validation, rejecting legitimate measurements during unusual ionospheric conditions.

**Ideal Solution:** Implement mode detection - switch from bootstrap to operational when we have stable, confident offset estimates. Until then, the current fix (warn instead of reject) is appropriate for operational mode.

**Measurement Corroboration:**

User requirement: Multiple measurements per broadcast should corroborate each other:
- CHU: Per-minute tone + AFSK in same minute
- WWV/WWVH: Per-minute tone + BCD correlation + test signal (hourly) + per-second correlations

This multi-method validation is more reliable than propagation model validation once the system is operational.

---

## ✅ SESSION COMPLETE (2026-01-06 16:40 UTC): IONOSPHERIC VALIDATION FIX & DIAGNOSTIC PREP

**Status:** 🟡 **PARTIAL** - Validation fixed, but chrony feed still down due to analytics D_clock issue

**Author:** AI Agent (Cascade)
**Date:** 2026-01-06 16:40 UTC
**Session:** Chrony feed restoration attempt

### Summary

Identified and partially fixed chrony feed stoppage that occurred at 13:42 UTC. Root cause was overly strict ionospheric validation rejecting all measurements during unusual ionospheric conditions. Applied fixes to accept measurements with reduced confidence, but discovered deeper issue in analytics D_clock calculation.

**Root Cause Timeline:**
1. Jan 4th: Strict ionospheric validation deployed
2. Jan 6th 13:42 UTC: Ionospheric conditions exceeded model predictions → all measurements rejected → chrony feed stopped
3. Jan 6th 16:00-16:40 UTC: Fixed validation, but analytics still producing D_clock=+0.00ms

**Fixes Applied:**
1. ✅ Ionospheric validation: Warn instead of reject (transmission_time_solver.py:837-848)
2. ✅ Confidence threshold: Lowered from 0.1 to 0.0 (phase2_analytics_service.py:2492)
3. ✅ Verified Data Product Registry working correctly across all services

**Remaining Issue:**
Analytics produces `D_clock=+0.00ms, uncertainty=100.0ms` for ~70% of measurements despite:
- Valid tone detections
- Valid propagation mode solutions
- Non-zero confidence values (0.05-0.50)
- Accepted ionospheric delays (with warnings)

This prevents fusion TEC solver from getting sufficient valid clock offset data, blocking chrony feed restoration.

**Next Session Focus:** Fix analytics D_clock calculation to produce valid clock offsets instead of +0.00ms.

---

## ✅ SESSION COMPLETE (2026-01-06 03:29 UTC): TIERED STORAGE ARCHIVER FIX

**Status:** 🟢 **COMPLETE** - Archiver working with 5-minute retention

**Author:** AI Agent (Cascade)
**Date:** 2026-01-06 03:29 UTC

### Summary

Fixed critical OOM issue where core recorder was being killed every 20-25 minutes due to 11GB of files accumulating in `/dev/shm`.

**Root Cause:** Tiered storage archiver was never initialized by core recorder.

**Resolution:** 
1. Added tiered storage initialization to `core_recorder_v2.py`
2. Fixed channel config loading from `recorder.channels`
3. Override auto-config to use fixed 5-minute retention (not 32 minutes)
4. Increased MemoryMax from 4GB to 8GB

**Verification Results:**
* Hot buffer: 36 files, oldest 4.5 min ✓
* Memory usage: ~400MB (was 7.7GB) ✓
* No OOM kills since deployment ✓
* Archiver running every 30 seconds ✓

**Design:** Hot buffer optimized for real-time analytics/fusion (3-5 min retention), non-real-time processing uses cold storage on disk.

---

## ✅ SESSION COMPLETE (2026-01-05 10:13 UTC): TEC DISCONTINUITY FIX

**Status:** 🟢 **COMPLETE** - TEC discontinuities eliminated, system maintains continuity

**Author:** AI Agent (Cascade)
**Date:** 2026-01-05 10:13 UTC

### Summary

Diagnosed and fixed major discontinuity issue where TEC corrections were modifying measurement values, causing 4-6ms jumps when signals faded in/out.

**Root Cause:** TEC estimation (both HF and GNSS VTEC) was modifying D_clock measurement values based on propagation delay corrections. When signal availability or TEC quality changed, this caused discontinuities.

**Resolution:** Changed TEC from modifying measurements to refining confidence/uncertainty.

**Fixes Implemented:**

1. ✅ HDF5 Reader: Use `timestamp_utc` length as canonical (not `min(len(values))`)
2. ✅ HF TEC: Adjust confidence ±15% based on fit quality and physical realism
3. ✅ GNSS VTEC: Adjust confidence ±10% based on model agreement
4. ✅ Physical realism check: Reject TEC outside 5-100 TECU range
5. ✅ Measurements retain original Phase 2 D_clock values

**Verification Results:**

* D_clock: Stable at 0.000ms (before: jumping 4-6ms)
* Broadcast count: Varies 52→61 with no discontinuities
* Chrony: Stable, reach=52, offset=+2.4ns
* Web API: Successfully reading fusion data

**Key Principle Established:**

System sits on a GPSDO. Signal availability changes should only affect error bars, not the fused estimate. TEC should refine the baseline physics model, not override it.

**Files Modified:**

* `src/hf_timestd/io/hdf5_reader.py` - Fixed dataset length calculation
* `src/hf_timestd/core/multi_broadcast_fusion.py` - TEC as refinement, not replacement
* `CRITIC_CONTEXT.md` - Updated for next session

**Next Session:** Critique per-broadcast D_clock estimation and fusion methods for flaws and vulnerabilities.

---

## ✅ SESSION COMPLETE (2026-01-04 02:16 UTC): SERVICE STABILITY IMPROVEMENTS

**Status:** 🟢 **COMPLETE** - Monitoring tools implemented, service stable

**Author:** AI Agent (Antigravity)
**Date:** 2026-01-04 02:16 UTC

### Summary

Investigated Chrony reach issue, implemented service stability improvements.

**Root Cause:** `timestd-fusion` service was stopped (inactive).

**Resolution:** Service restarted, Chrony reach recovered from 0 → 210 (octal).

**Improvements:** Systemd watchdog, monitoring scripts, periodic timers.

---
