# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer.  These perspectives can differ in their priorities and interests, and your critique should reflect this.  For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality and maintainability of the system.  Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🎯 NEXT SESSION OBJECTIVE: DIAGNOSE & FIX CHRONY FEED AND TEC CALCULATIONS

**Status:** 🔴 **CRITICAL** - Both chrony feed and TEC calculations not working despite deployed fixes

**Author:** AI Agent (Cascade)
**Date:** 2026-01-06 10:52 UTC
**Session:** Post-tiered storage fix, TEC fix verification

### Session Goal

**Primary Objective:** Diagnose and fix the measurement and data pipeline issues affecting:

1. **Chrony Feed:** Verify fusion service is producing valid timing measurements and feeding chrony correctly
2. **TEC Calculations:** Determine why TEC values are still not realistic (2-50 TECU) despite fixes to `raw_arrival_time_ms`
3. **Data Pipeline Integrity:** Trace the complete path from L1 tone detection → L2 timing → Science aggregator → TEC output
4. **Root Cause Analysis:** Identify whether the issue is in data generation, data reading, or calculation logic

### Context from Previous Session (2026-01-06)

**Major Changes Deployed:**

1. **TEC Fix - Science Aggregator Path (DEPLOYED 2026-01-06 01:35 UTC):**
    * Modified `science_aggregator.py` to read L2 timing measurements from `clock_offset/` subdirectory
    * **Issue:** Science aggregator was reading placeholder data with all zeros from main directory
    * **Fix:** Lines 149-167 now use `clock_offset_dir = self.paths.get_clock_offset_dir(channel_name)`
    * **Status:** ⚠️ UNVERIFIED - Need to check if TEC values are now realistic

2. **TEC Fix - Raw Arrival Time Calculation (DEPLOYED 2026-01-06 01:43 UTC):**
    * Modified `phase2_analytics_service.py` to use actual tone timing for `raw_arrival_time_ms`
    * **Old Behavior:** Used tiny clock offset residuals (0.001 ms) → TEC ≈ 0.0
    * **New Behavior:** Priority system: `solution.t_arrival_ms` → `time_snap` tone timing → `clock_offset + propagation_delay`
    * **Fix:** Lines 747-770 implement proper fallback logic
    * **Status:** ⚠️ UNVERIFIED - Need to verify HDF5 files have realistic raw_arrival_time_ms values

3. **Tiered Storage Fix (DEPLOYED 2026-01-06 03:29 UTC):**
    * Fixed OOM kills by initializing tiered storage archiver with 5-minute retention
    * **Status:** ✅ VERIFIED WORKING - Hot buffer stable at 36 files, oldest 4.5 min
    * **Impact:** Raw IQ recording now stable, no more OOM kills

### Critical Questions to Answer

**1. Chrony Feed Status:**

* Is the fusion service running and producing L3 fusion timing measurements?
* Are fusion measurements being written to HDF5 files correctly?
* Is the chrony SHM writer actually feeding chrony with valid data?
* Current chrony status: `#* TMGR stratum 0, reach 124, offset +1354ns` - Is this correct?
* Why is reach only 124 (octal) instead of 377 (all 8 samples)?

**2. TEC Calculation Pipeline:**

* Are L2 timing measurements in `clock_offset/` subdirectory being written with realistic `raw_arrival_time_ms` values?
* Is the science aggregator successfully reading these measurements?
* Is the TEC estimator receiving multi-frequency data with proper dispersion?
* What are the actual TEC values being calculated? (Expected: 2-50 TECU, likely getting: ~0.0 TECU)
* Is the issue in data generation, data reading, or TEC calculation logic?

**3. Data Pipeline Verification:**

* **L1 → L2:** Are tone detections being converted to timing measurements correctly?
* **L2 → Science:** Is science aggregator finding and reading the correct HDF5 files?
* **Science → TEC:** Is TEC estimator receiving valid multi-frequency measurements?
* **TEC → HDF5:** Are TEC results being written to `/var/lib/timestd/phase2/science/tec/` correctly?
* **HDF5 → Web UI:** Can `propagation.html` read and display TEC data?

**4. Specific Data Validation:**

* Check `/var/lib/timestd/phase2/CHU_3330/clock_offset/CHU_3330_timing_measurements_20260106.h5`
* Verify `raw_arrival_time_ms` values are in 4-35 ms range (not 0.001 ms)
* Check `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_20260106.h5`
* Verify TEC values are in 2-50 TECU range (not 1e-08 TECU)

### Diagnostic Methodology

**Step 1: Verify Chrony Feed (30 min)**

* Check fusion service status: `systemctl status timestd-fusion`
* Verify fusion is writing to HDF5: `ls -lht /var/lib/timestd/phase2/fusion/`
* Check chrony SHM: `ipcs -m` and verify timestd SHM segment exists
* Trace fusion → chrony data flow in `multi_broadcast_fusion.py`
* Verify chrony is reading from correct SHM segment

**Step 2: Trace TEC Data Pipeline (1 hour)**

* **L2 Timing Measurements:** Check if `raw_arrival_time_ms` is being written correctly
  - Read HDF5: `/var/lib/timestd/phase2/CHU_3330/clock_offset/CHU_3330_timing_measurements_20260106.h5`
  - Verify values are 4-35 ms (not 0.001 ms)
  - Check multiple frequencies: 3.33, 7.85, 14.67 MHz

* **Science Aggregator:** Verify it's reading from correct directory
  - Check logs: `journalctl -u timestd-science-aggregator --since "1 hour ago"`
  - Verify it finds multi-frequency measurements
  - Check if TEC estimator is being called

* **TEC Output:** Verify TEC values are realistic
  - Read HDF5: `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_20260106.h5`
  - Check TEC values: should be 2-50 TECU, not 1e-08 TECU
  - Verify quality flags: should be "GOOD" not "BAD"/"MARGINAL"

**Step 3: Root Cause Analysis (1 hour)**

* If `raw_arrival_time_ms` is still wrong → Issue in analytics service
* If `raw_arrival_time_ms` is correct but TEC is wrong → Issue in science aggregator or TEC estimator
* If TEC is correct but not displayed → Issue in web API or frontend
* Use debug logging and manual HDF5 inspection to isolate the failure point

### Critical Files for Investigation

**Data Generation:**
* `src/hf_timestd/core/phase2_analytics_service.py`: Lines 747-770 (raw_arrival_time_ms calculation)
* `src/hf_timestd/core/phase2_temporal_engine.py`: Lines 2067-2076 (t_arrival_ms from time_snap)
* `src/hf_timestd/io/data_product_writer.py`: HDF5 writer for L2 timing measurements

**Data Reading & TEC Calculation:**
* `src/hf_timestd/core/science_aggregator.py`: Lines 149-167 (clock_offset directory), 276-286 (TEC calculation)
* `src/hf_timestd/core/tec_estimator.py`: Lines 73-235 (TEC calculation from multi-frequency data)
* `src/hf_timestd/io/hdf5_reader.py`: DataProductReader for reading L2 measurements

**Chrony Feed:**
* `src/hf_timestd/core/multi_broadcast_fusion.py`: Fusion service and chrony SHM writer
* `src/hf_timestd/io/chrony_shm.py`: Chrony shared memory interface

**Data Locations:**
* L2 Timing: `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/{CHANNEL}_timing_measurements_YYYYMMDD.h5`
* TEC Output: `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_YYYYMMDD.h5`
* Fusion Output: `/var/lib/timestd/phase2/fusion/AGGREGATED_fusion_timing_YYYYMMDD.h5`

**Verification Scripts:**
* `scripts/check_tec_values.py`: Check TEC values (currently has numpy error)
* `scripts/verify_tec_fix.py`: Verify raw_arrival_time_ms in HDF5 files

### Known Issues to Address

**1. TEC Values Still Incorrect:**
* Deployed fixes on 2026-01-06 but not yet verified
* Need to check if science aggregator is running and processing data
* May need to wait for next hourly run or manually trigger

**2. Chrony Reach Incomplete:**
* Current reach: 124 (octal) = 5/8 samples
* Expected: 377 (octal) = 8/8 samples
* May indicate intermittent fusion service issues

**3. Verification Script Error:**
* `scripts/check_tec_values.py` has numpy error with quality flag comparison
* Need to fix before using for verification

**4. Data Pipeline Uncertainty:**
* Unknown if analytics service is actually writing corrected `raw_arrival_time_ms` values
* Unknown if science aggregator is successfully reading from `clock_offset/` subdirectory
* Need manual HDF5 inspection to verify

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
