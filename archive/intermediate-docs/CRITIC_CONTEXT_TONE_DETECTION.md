# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION!!!

**Primary Instruction:** In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user. This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation. It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation. It should also look for obsolete, deprecated, or "zombie" code that should be removed. Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

Make your criticism from the perspective of 1) a user of the system, 2) a metrologist, 3) a ionospheric scientist, and 4) a software engineer. These perspectives can differ in their priorities and interests, and your critique should reflect this. For example, a user of the system will be most interested in the accuracy and reliability of the system, while a metrologist will be most interested in the precision and traceability of the system, while an ionospheric scientist will be most interested in the ionospheric conditions and their impact on the system, and a software engineer will be most interested in the code quality and maintainability of the system. Ultimately, however, a consensus of these perspectives should guide your critique in service of the meeting the application's objectives.

# NEVER CHANGE THE PRECEEDING PRIMARY INSTRUCTION!!!

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🎯 NEXT SESSION OBJECTIVE: TONE DETECTION OPTIMIZATION

**Status:** ⚠️ **CRITICAL** - Low L1 metrology detection rate blocking dual Chrony feed research
**Author:** AI Agent (Cascade)
**Date:** 2026-01-14 23:34 UTC
**Session:** Post dual Chrony feed implementation

### Session Goal

**Primary Objective:** Diagnose and optimize tone detection pipeline to resolve low detection rates despite strong signal strength.

**Critical Issue:** 15 MHz WWV showing 23.6dB SNR but L1 metrology file only 61K today vs 344K yesterday (82% reduction in detections).

**Impact:** Without L1 detections → no L2 calibrations → dual Chrony feeds (TSL1/TSL2) show identical values → research objective cannot be achieved.

### Previous Session Accomplishments (2026-01-14)

**✅ Dual Chrony Feed Architecture Implemented:**
1. Fixed L2 calibration service integration (path and field name mismatches)
2. Implemented separate L1-only and L2-calibrated fusion paths
3. Both TSL1 and TSL2 feeds operational and updating Chrony
4. Current status: TSL1=-445us, TSL2=-437us (only 8us difference)

**Key Code Changes:**
- `multi_broadcast_fusion.py`: Added `force_l1_only` parameter, dual fusion execution, separate SHM updates
- L2 data reading path fixed: `clock_offset/timing_measurements` instead of `physics_interpretation`
- L2 field name corrected: `station` instead of `station_id`

**Documentation Created:**
- `/home/mjh/git/hf-timestd/docs/SESSION_2026-01-14_DUAL_CHRONY_FEEDS.md` - Complete session summary
- `/home/mjh/git/hf-timestd/docs/DUAL_CHRONY_FEED_ARCHITECTURE.md` - Architecture documentation

### Current System State (2026-01-14 23:34 UTC)

**Services Status:**
- ✅ `timestd-metrology.service` - Running (active exited since 13:28:30)
- ✅ `timestd-l2-calibration.service` - Running, processing CHU channels every minute
- ✅ `timestd-fusion.service` - Running, dual feeds operational
- ✅ `chronyd.service` - Receiving both TSL1 and TSL2

**Chrony Feed Status:**
```
#- TSL1    0   4   252    41   -445us[-445us] +/-   51ms  (L1-only fusion)
#- TSL2    0   4   252    41   -437us[-437us] +/-   51ms  (L2-calibrated fusion)
```

**L1 Metrology File Sizes (Today vs Yesterday):**
- SHARED_15000: **61K** (2026-01-14) vs **344K** (2026-01-13) - **82% reduction** ⚠️
- SHARED_10000: **86K** (2026-01-14) vs **157K** (2026-01-13) - **45% reduction** ⚠️
- CHU channels: Processing normally

**L2 Calibration Processing (Last 10 minutes):**
- CHU_7850: Processing 1 measurement/minute ✅
- CHU_14670: Processing 1 measurement/minute ✅
- SHARED_15000: Only 1 measurement at 18:42:42, then nothing ❌
- SHARED_10000: Only 1 measurement at 18:39:42, then nothing ❌

**File Ownership Issue:**
- SHARED_15000 metrology file owned by `root` instead of `timestd` ⚠️
- SHARED_10000 metrology file owned by `root` instead of `timestd` ⚠️

### Critical Questions for This Session

**1. Tone Detection Performance:**
- Why is 15 MHz WWV with 23.6dB SNR not being detected/recorded?
- What changed between 2026-01-13 (344K) and 2026-01-14 (61K)?
- Are detection thresholds too aggressive for certain frequencies?
- Is SNR calculation accurate across all frequencies?

**2. Detection Pipeline Analysis:**
- Is raw IQ data present for 15 MHz and 10 MHz channels?
- Are tone detections happening but being filtered out?
- Are detections happening but not being written to HDF5?
- What quality criteria are rejecting good detections?

**3. Frequency-Dependent Patterns:**
- Why do CHU channels (3.3, 7.85, 14.67 MHz) work consistently?
- Why do SHARED channels show variable performance?
- Is there a frequency-dependent detection threshold issue?
- Are template matching parameters optimized for all frequencies?

**4. Code Review Focus Areas:**
- `tone_detector.py`: Detection thresholds, SNR calculation, template matching
- `metrology_engine.py`: Quality filtering, measurement packaging
- `metrology_service.py`: HDF5 writing logic, error handling
- Detection algorithm performance across frequency bands

### Data Locations and Key Files

**Raw IQ Data:**
- `/dev/shm/timestd/raw_buffer/SHARED_15000/` - Check for recent `.bin.zst` files
- `/dev/shm/timestd/raw_buffer/SHARED_10000/` - Check for recent `.bin.zst` files

**L1 Metrology Output:**
- `/var/lib/timestd/phase2/SHARED_15000/metrology/` - 61K file (low)
- `/var/lib/timestd/phase2/SHARED_10000/metrology/` - 86K file (low)
- `/var/lib/timestd/phase2/CHU_7850/metrology/` - Normal size (baseline)

**L2 Calibration Output:**
- `/var/lib/timestd/phase2/SHARED_15000/clock_offset/` - Sparse data
- `/var/lib/timestd/phase2/SHARED_10000/clock_offset/` - Sparse data

**Logs:**
- Metrology service: Check `journalctl -u timestd-metrology` for errors
- Individual channel logs: Look for channel-specific processing issues

**Key Source Files to Review:**
1. `/home/mjh/git/hf-timestd/src/hf_timestd/core/tone_detector.py`
   - Lines 786-1671: Detection algorithms, thresholds, template matching
   - Focus: `_detect_tones_internal`, `_correlate_with_template`, SNR calculation

2. `/home/mjh/git/hf-timestd/src/hf_timestd/core/metrology_engine.py`
   - Lines 260-354: Detection processing, quality filtering
   - Focus: `process_minute`, measurement packaging, filtering criteria

3. `/home/mjh/git/hf-timestd/src/hf_timestd/core/metrology_service.py`
   - Lines 150-249: HDF5 writing, error handling
   - Focus: Why detections might not be written

### Diagnostic Approach

**Phase 1: Verify Signal Presence**
1. Check raw IQ data exists for SHARED_15000 and SHARED_10000
2. Verify file sizes and timestamps are recent
3. Confirm GPSDO lock and RTP timestamps are valid

**Phase 2: Trace Detection Pipeline**
1. Enable DEBUG logging on metrology service for SHARED_15000
2. Monitor tone detector output - are detections happening?
3. Check if detections are being filtered/rejected
4. Verify HDF5 writer is being called

**Phase 3: Compare Working vs Failing Channels**
1. Compare CHU (working) vs SHARED (failing) detection parameters
2. Analyze SNR thresholds across frequencies
3. Review template matching sensitivity
4. Check for frequency-dependent bugs

**Phase 4: Code Review**
1. Review tone detection thresholds and noise floor estimation
2. Analyze quality filtering criteria in metrology_engine
3. Check for edge cases in SNR calculation
4. Look for frequency-dependent parameter scaling

### Known System Behavior

**Fallback Logic in Fusion:**
When L2 data unavailable, fusion uses:
```
D_clock = Raw_TOA - (geometric_light_time + 1.5ms)
```

This is why TSL1 and TSL2 show similar values - both are using the same fallback approximation due to limited L2 coverage.

**Expected Behavior After Fix:**
- Increased L1 detection rate on 15 MHz and 10 MHz
- More L2 calibrations produced
- Greater offset difference between TSL1 and TSL2
- Visible impact of ionospheric corrections in TSL2

### Success Criteria

After this session, we should:
- ✅ Identify root cause of low detection rate on 15 MHz
- ✅ Implement fix to restore normal detection rates
- ✅ Verify 15 MHz metrology file size returns to ~300K+ range
- ✅ Confirm L2 calibration processing increases
- ✅ Observe TSL1 vs TSL2 offset differentiation
- ✅ Document findings and prevention measures

### Hypotheses to Test

1. **Detection threshold too high**: Noise floor estimation may be inflated for certain frequencies
2. **SNR calculation error**: SNR might be miscalculated for specific frequency bands
3. **Quality filtering too aggressive**: Good detections being rejected by quality criteria
4. **Template mismatch**: Template parameters not optimized for 15 MHz propagation
5. **File permission issue**: Root ownership preventing proper HDF5 writes
6. **Service restart needed**: Metrology service may need restart after code changes

### Notes

- Dual Chrony feed architecture is complete and functional
- The research objective (L1 vs L2 comparison) is blocked by lack of L1 data
- CHU channels provide a working baseline for comparison
- File ownership issues (root vs timestd) may indicate permission problems
- System is otherwise stable and healthy

---

## Reference: Dual Chrony Feed Architecture

**TSL1 (SHM unit 0)**: L1-only fusion using raw metrology with geometric approximation
**TSL2 (SHM unit 1)**: L2-calibrated fusion using propagation delay corrections when available

**Current Implementation:**
- Fusion runs twice per cycle: `force_l1_only=True` for TSL1, `force_l1_only=False` for TSL2
- Separate SHM updates with different precision values (-10 for L1, -11 for L2)
- L2 falls back to L1 approximation when calibrated data unavailable
