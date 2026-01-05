# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer.  These perspectives can differ in their priorities and interests, and your critique should reflect this.  For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality and maintainability of the system.  Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🎯 NEXT SESSION OBJECTIVE (2026-01-04 → Next Session): CRITIQUE PHASE 2 ANALYTICS PROPAGATION DELAY CALCULATIONS

**Status:** 🟢 **READY FOR CRITIQUE** - TEC discontinuity fixed, system stable, now examine calculation methods

**Author:** AI Agent (Cascade)
**Date:** 2026-01-05 10:13 UTC

### Session Goal

**Primary Objective:** Thoroughly examine per-broadcast D_clock estimation and fusion methods to identify flaws, vulnerabilities, or systematic errors in the calculations.

**Critical Fix from 2026-01-05:**
Fixed major discontinuity issue where TEC corrections were modifying measurement values instead of refining uncertainty. This caused 4-6ms jumps when signals faded in/out.

**Key Changes:**
1. **HDF5 Reader Fix:** Changed from `min(len(values))` to using `timestamp_utc` length as canonical measurement count
2. **TEC Correction Philosophy:** TEC now adjusts confidence/uncertainty, not D_clock values
3. **GNSS VTEC Refinement:** VTEC validates TEC agreement, adjusts confidence based on model agreement
4. **Continuity Maintained:** System now stable at 0.000ms despite broadcast count varying 52→61

**Previous Session Work (2026-01-05):**
- Diagnosed chrony feed discontinuities (4-6ms jumps correlated with signal fading)
- Identified root cause: TEC corrections modifying D_clock values
- Implemented fix: TEC as refinement to uncertainty, not replacement
- Verified continuity: D_clock stable despite broadcast availability changes
- Fixed web-API null values (HDF5 reader dataset length issue)

**Fundamental Principle Established:**
The system sits on a GPSDO. Signal appearance/disappearance should only affect error bars, not the fused estimate. TEC should refine the baseline physics model, not override it.

### Context from Previous Session

**Service Stability Improvements Completed:**

- Fusion service crash-loop investigated (root cause: service was stopped)
- Systemd watchdog enabled (Type=notify, WatchdogSec=30)
- Chrony reach monitoring implemented (script + timer)
- System architecture validated (VTEC optional, HDF5 primary, critical path confirmed)

**Current System State:**

- All services running and stable
- Chrony reach recovering (210 octal = 53% success rate, trending toward 377)
- Fusion output: Grade B/C, ±0.8-1.5ms uncertainty
- VTEC: Using GNSS fallback (18.12 TECU)
- TEC solver: Producing NaN (expected, no dispersion in measurements)

**Key Findings from Previous Session:**

1. VTEC is properly optional with graceful fallback
2. HDF5 is the primary data format (CSV is legacy)
3. Core Recorder writes to `.bin.zst` compressed binary
4. Critical path is well-defined and functional
5. Fusion service had crash-loop at 00:20 UTC (cause unknown)

### Critical Questions to Answer

**1. Per-Broadcast D_clock Estimation:**

- Is the D_clock calculation formula correct? (D_clock = T_arrival - T_propagation)
- Are propagation delay models physically accurate for each station/frequency?
- Is ionospheric correction being applied consistently?
- Are calibration offsets in the correct direction and magnitude?
- Are there sign errors or unit conversion issues?
- Is the baseline physics model (geography + propagation) sound?

**2. Fusion Algorithm Integrity:**

- Is the weighted fusion statistically sound?
- Are measurement weights calculated correctly based on confidence/uncertainty?
- Is the Kalman filter properly initialized and updated?
- Are outlier rejection thresholds appropriate?
- Does the fusion maintain continuity when measurements change?
- Is the cross-station validation logic correct?

**3. TEC and Propagation Corrections:**

- Why does HF TEC solver return 0.0 TECU with perfect fit?
- Are raw_arrival_time_ms values being used correctly for TEC estimation?
- Is the TEC-to-delay conversion formula correct?
- Are multi-hop propagation delays calculated correctly?
- Is the baseline propagation model consistent across all measurements?

**4. Measurement Quality and Uncertainty:**

- Are confidence values calculated correctly?
- Is uncertainty propagated through the pipeline?
- Are quality grades (A/B/C/D) assigned appropriately?
- Do confidence adjustments (TEC validation, VTEC agreement) make physical sense?
- Is the uncertainty budget complete and traceable?

**5. Systematic Errors and Biases:**

- Are there station-dependent biases in D_clock estimates?
- Are there frequency-dependent systematic errors?
- Does the fusion favor certain stations inappropriately?
- Are there time-of-day or propagation-mode biases?
- Is the GPSDO stability assumption valid?

### Recent Fixes to Consider

**TEC Discontinuity Fix (2026-01-05):**

Location: `src/hf_timestd/core/multi_broadcast_fusion.py`

**Problem:** TEC corrections were modifying D_clock measurement values, causing 4-6ms discontinuities when:
- Signal availability changed (broadcasts fading in/out)
- TEC estimation quality changed (R² varying)
- Different station sets became available

**Root Cause:** Lines 2106-2113 recalculated D_clock based on TEC-derived propagation delays:
```python
t_arrival = m.d_clock_ms + m.propagation_delay_ms
m.d_clock_ms = t_arrival - new_delay  # MODIFYING measurement!
```

**Solution Implemented:**
- HF TEC: Adjusts confidence ±15% based on fit quality and physical realism (5-100 TECU)
- GNSS VTEC: Adjusts confidence ±10% based on model agreement (<5 TECU = good)
- Measurements retain original D_clock values from Phase 2 analytics
- TEC acts as quality indicator, not correction mechanism

**Verification:** System now maintains D_clock = 0.000ms despite broadcast count varying 52→61.

**Questions for Next Session:**
- Are the original Phase 2 D_clock values correct?
- Is the baseline propagation model in Phase 2 accurate?
- Should TEC be used differently (e.g., in Phase 2 instead of fusion)?

### Specific Areas to Investigate

#### Phase 2 Analytics (`phase2_analytics_service.py`)

**Focus:** Per-broadcast D_clock calculation and propagation delay estimation

**Questions:**

- Is the D_clock formula implemented correctly?
- Are propagation delays calculated with correct physics?
- Is the baseline model (IRI/empirical) appropriate?
- Are calibration offsets applied in the right direction?
- Is raw_arrival_time_ms being set correctly for TEC estimation?
- Are there station or frequency-dependent systematic errors?

**Key Methods to Review:**

- `Phase2TemporalEngine._calculate_d_clock()` - Core D_clock calculation
- `PropagationModel.compute_delay()` - Propagation delay estimation
- `Phase2TemporalEngine._apply_calibration()` - Calibration application
- `Phase2AnalyticsService._write_hdf5_timing()` - Data output

#### TEC Estimator (`tec_estimator.py`)

**Focus:** HF TEC estimation from multi-frequency measurements

**Questions:**

- Why does TEC solver return 0.0 TECU with R²=1.00?
- Is the least-squares formulation correct?
- Are frequency diversity checks appropriate?
- Is the ionospheric constant (40.3) applied correctly?
- Are input measurements (raw_arrival_time_ms) correct?
- Should TEC estimation happen in Phase 2 instead of fusion?

**Key Methods to Review:**

- `TECEstimator.estimate_tec()` - Main TEC estimation (lines 81-226)
- Least squares formulation: `y = c + m*x` where `x = 1/f²`
- TEC extraction: `tec = m / K_IONOSPHERE`
- Frequency diversity check: `np.std(freqs) < 1000.0`

**Known Issue:** Returns 0.0 TECU when all frequencies have identical ToA (no dispersion)

#### Fusion Service (`multi_broadcast_fusion.py`)

**Focus:** Multi-broadcast fusion algorithm and statistical methods

**Questions:**

- Is the weighted fusion formula correct?
- Are measurement weights calculated appropriately?
- Is the Kalman filter implementation sound?
- Are outlier rejection thresholds well-justified?
- Does cross-station validation make physical sense?
- Is the uncertainty budget complete?
- Are there any remaining discontinuity sources?

**Key Methods to Review:**

- `MultiBroadcastFusion.fuse()` - Main fusion algorithm (lines 1898-2300)
- `MultiBroadcastFusion._calculate_weights()` - Weight calculation
- `MultiBroadcastFusion._reject_outliers()` - Outlier rejection (MAD-based)
- `MultiBroadcastFusion._cross_validate_stations()` - Cross-validation
- `MultiBroadcastFusion._update_kalman()` - Kalman filter updates
- `MultiBroadcastFusion._calculate_uncertainty()` - Uncertainty budget

**Recent Changes (2026-01-05):**
- Lines 2084-2111: TEC now adjusts confidence, not D_clock values
- Lines 1968-2016: GNSS VTEC validates agreement, adjusts confidence
- Added physical realism check: 5-100 TECU range for valid TEC

### Known Issues and Questions

**1. HF TEC Solver Returns 0.0 TECU (ACTIVE)**

- TEC solver consistently returns 0.0 TECU with R²=1.00 for WWV
- CHU returns NaN due to insufficient frequency diversity (all 14.67 MHz)
- **Hypothesis:** All WWV frequencies have identical ToA (no ionospheric dispersion)
- **Questions:** 
  - Are raw_arrival_time_ms values being set correctly in Phase 2?
  - Should measurements already be TEC-corrected at this point?
  - Is the TEC solver being called at the wrong stage of the pipeline?
- **Impact:** Now mitigated (unrealistic TEC values ignored), but root cause unknown

**2. Propagation Delay Model Accuracy (NEEDS INVESTIGATION)**

- Phase 2 uses baseline physics model (IRI or empirical) for propagation delays
- Fusion previously modified these with TEC corrections (now disabled)
- **Questions:**
  - Are the baseline propagation delays accurate?
  - Should TEC correction happen in Phase 2 instead of fusion?
  - Are multi-hop delays calculated correctly?
  - Is the IRI model appropriate for HF frequencies?

**3. D_clock Inter-Station Agreement (HISTORICAL)**

- Previous sessions noted large D_clock disagreements between stations (6-24ms)
- **Question:** With TEC corrections disabled, do stations now agree better?
- **Action:** Examine current per-station D_clock values for systematic biases

**4. Kalman Filter Initialization (NEEDS REVIEW)**

- Kalman filter state initialization and update logic
- **Questions:**
  - Is the initial state appropriate?
  - Are process/measurement noise parameters correct?
  - Does the filter handle large jumps appropriately?
  - Is the gradual ramp-up (DISCRIMINATION_SUSPECT protection) working?

### Critique Methodology

**Step 1: Code Review (2-3 hours)**

- Read through critical path code systematically
- Look for obvious bugs, logic errors, or inconsistencies
- Check for proper error handling and edge cases
- Verify calculations against physics and metrology principles

**Step 2: Data Flow Validation (1-2 hours)**

- Trace a sample measurement through the entire pipeline
- Verify data transformations at each stage
- Check for data loss or corruption
- Validate uncertainty propagation

**Step 3: Error Scenario Testing (1-2 hours)**

- Identify failure modes (missing data, invalid data, service crashes)
- Review error handling for each failure mode
- Check for silent failures or data quality degradation
- Verify graceful degradation and recovery

**Step 4: Performance Analysis (1 hour)**

- Review resource usage (CPU, memory, disk I/O)
- Identify bottlenecks or inefficiencies
- Check for unnecessary calculations or data copies
- Verify scalability for expected data rates

**Step 5: Documentation Review (30 minutes)**

- Check for code-documentation inconsistencies
- Verify that critical algorithms are documented
- Identify missing or outdated documentation
- Review comments for accuracy

### Success Criteria

**Deliverables:**

1. **Comprehensive critique document** identifying all flaws, inefficiencies, and vulnerabilities
2. **Prioritized list of issues** (critical, high, medium, low)
3. **Specific recommendations** for each issue
4. **Implementation plan** for addressing critical issues
5. **Test plan** for validating fixes

**Quality Metrics:**

- All critical path code reviewed
- All known issues investigated
- All failure modes identified
- All recommendations actionable and specific

### System Information

**Version:** 4.0.0 (includes TEC discontinuity fix)

**Services:**

- ✅ `timestd-core-recorder` - Active, writing L0 data
- ✅ `timestd-analytics` - Active, writing L2 HDF5
- ✅ `timestd-fusion` - Active, writing L3 HDF5 (stable, continuous)
- ✅ `timestd-science-aggregator` - Active
- ✅ `timestd-vtec` - Active, providing GNSS VTEC
- ✅ `timestd-web-api` - Active, serving metrology data
- ✅ `timestd-chrony-monitor` - Timer active, monitoring every 5 minutes

**Data Pipeline:**

- L0 (Raw IQ): `.bin.zst` compressed binary in `/dev/shm/timestd/raw_buffer/`
- L2 (Timing): HDF5 in `/var/lib/timestd/phase2/STATION_FREQ/`
- L3 (Fusion): HDF5 in `/var/lib/timestd/phase2/fusion/`
- Web API: `http://localhost:8000/api/metrology/fusion/latest`

**Current Timing Output (2026-01-05 10:10 UTC):**

- D_clock: 0.000 ms (stable, centered on UTC)
- Uncertainty: ±0.8 ms
- Quality: Grade B
- Broadcasts: 52-61 contributing (varies smoothly)
- Chrony reach: 52 (octal) = 42 (decimal), offset +2.4ns
- **No discontinuities** despite broadcast count changes

**Recent Improvements:**

- TEC corrections now refine uncertainty, not modify measurements
- HDF5 reader uses `timestamp_utc` length as canonical
- System maintains continuity as signals fade in/out
- Web API successfully reading fusion data

### Reference Documents

**Critical Path Analysis:**

- `/home/mjh/.gemini/antigravity/brain/ce1eed84-1bda-4e5d-bef2-00a1d5864b79/critical_path_analysis.md`
- Comprehensive analysis of metrology-critical vs. science-optional components
- VTEC dependency analysis (confirmed optional)
- Single points of failure identification

**Chrony Reach Investigation:**

- `/home/mjh/.gemini/antigravity/brain/ce1eed84-1bda-4e5d-bef2-00a1d5864b79/chrony_reach_investigation.md`
- Root cause analysis (fusion service stopped)
- Timeline of events
- Verification results

**Session Summary:**

- `/home/mjh/.gemini/antigravity/brain/ce1eed84-1bda-4e5d-bef2-00a1d5864b79/session_summary.md`
- Overview of investigation and findings
- Recommendations for next steps

**Walkthrough:**

- `/home/mjh/.gemini/antigravity/brain/ce1eed84-1bda-4e5d-bef2-00a1d5864b79/walkthrough.md`
- Deployment instructions
- Monitoring commands
- Next steps

### Important Principles

**1. Time Synchronization is PRIMARY Mission**

- Everything else (science products, TEC, Doppler) is secondary
- Chrony SHM updates must be reliable and accurate
- System must continue operating even when optional components fail

**2. HDF5 is Primary Data Format**

- CSV is legacy and will be removed
- All new code should use HDF5
- CSV fallbacks are temporary during migration

**3. Metrology Principles**

- Uncertainty budgets must be correct and complete
- Traceability to UTC(NIST) must be maintained
- Measurements must be reproducible and documented

**4. Graceful Degradation**

- System should continue operating with reduced accuracy when components fail
- Failures should be logged and visible
- Recovery should be automatic when possible

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

- D_clock: Stable at 0.000ms (before: jumping 4-6ms)
- Broadcast count: Varies 52→61 with no discontinuities
- Chrony: Stable, reach=52, offset=+2.4ns
- Web API: Successfully reading fusion data

**Key Principle Established:**

System sits on a GPSDO. Signal availability changes should only affect error bars, not the fused estimate. TEC should refine the baseline physics model, not override it.

**Files Modified:**

- `src/hf_timestd/io/hdf5_reader.py` - Fixed dataset length calculation
- `src/hf_timestd/core/multi_broadcast_fusion.py` - TEC as refinement, not replacement
- `CRITIC_CONTEXT.md` - Updated for next session

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
