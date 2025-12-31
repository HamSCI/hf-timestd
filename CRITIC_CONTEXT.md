# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## ✅ SESSION COMPLETE (2025-12-30): ANALYTICS PIPELINE & CHRONY INTEGRATION FIXES

**Status:** 🟢 **RESOLVED** - Analytics producing valid measurements, HDF5 SWMR working, Fusion reading data, Chrony SHM updating

**Author:** Michael James Hauan (AC0G)  
**Date:** 2025-12-30  

### Problems Identified and Fixed

#### 1. **IRI-2020 Array Handling Incompatibility**

**Problem:** The `iri2020` Python package updated its return types from scalars to `xarray.DataArray` or NumPy arrays. Analytics was calling `float()` directly on these objects, causing:

```
ValueError: only 0-dimensional arrays can be converted to Python scalars
```

This caused IRI-2020 calculations to fail, forcing fallback to less accurate geometric models, producing "absurd D_clock values" (e.g., -36 seconds) and confidence=0 measurements that were rejected before HDF5 write.

**Fix:** Added `_extract_scalar()` helper function in `ionospheric_model.py` to normalize IRI outputs (DataArray, NumPy array, scalar, list) to floats. Updated `_get_iri_heights()` and `_estimate_vertical_tec()` to use `_extract_scalar()` for all IRI result fields.

**Files Modified:**

- `src/hf_timestd/core/ionospheric_model.py` (lines 113-116, 299-344, 910-915)

---

#### 2. **Bootstrap Second Boundary Calculation Error**

**Problem:** The propagation solver's bootstrap logic was incorrectly calculating `expected_second_rtp` by pointing to the **next minute boundary** instead of the **current second boundary**. This caused D_clock calculations like:

```
D_clock -36006.8ms exceeds plausible bounds (±1000ms)
```

The 36-second error came from pointing 36 seconds ahead to the next minute instead of the current second.

**Fix:** Modified `phase2_temporal_engine.py` bootstrap logic to calculate the nearest second boundary using RTP timestamp modulo and round to the **nearest** second (not always down) based on which boundary is closer.

**Files Modified:**

- `src/hf_timestd/core/phase2_temporal_engine.py` (lines 1640-1671)

---

#### 3. **Missing HDF5 L1A Schema Field**

**Problem:** Analytics was failing to write L1A channel observables to HDF5 with error:

```
Failed to write HDF5 L1A measurement: Required field 'processing_version' missing from measurement
```

**Fix:** Added `'processing_version': '3.2.0'` to the `l1a_measurement` dictionary in `_write_carrier_power()`.

**Files Modified:**

- `src/hf_timestd/core/phase2_analytics_service.py` (lines 729-743)

---

#### 4. **HDF5 SWMR Visibility Issue**

**Problem:** Analytics was successfully writing L2 timing measurements to HDF5 files (file size growing, modification time updating), but the data was **not visible to SWMR readers**. Fusion was reading 0 measurements despite analytics writing them. This was because in SWMR mode, calling `flush()` alone doesn't update the metadata that readers use to discover new data.

**Fix:** Added explicit `refresh()` calls after `flush()` in the HDF5 writer to make new data visible to concurrent SWMR readers:

```python
# Flush to disk and refresh SWMR metadata
hdf5_file.flush()

# Force metadata refresh for SWMR readers
if hdf5_file.swmr_mode:
    for field in self.schema['fields']:
        field_name = field['name']
        if field_name in hdf5_file:
            hdf5_file[field_name].refresh()
```

**Files Modified:**

- `src/hf_timestd/io/hdf5_writer.py` (lines 340-353)

---

### Verification Results

**Analytics Service:**

- ✅ Producing valid D_clock values: -2ms to +45ms range (plausible)
- ✅ IRI-2020 calculations working (no fallback to geometric model)
- ✅ CSV writes working
- ✅ HDF5 L1A writes working
- ✅ HDF5 L2 writes working with SWMR visibility

**Fusion Service:**

- ✅ Reading 28 L2 timing measurements from HDF5 (10-minute lookback)
- ✅ Reading 11 tone observations from HDF5 across 8 channels
- ✅ Fused D_clock: +3.325 ms ± 4.721 ms (26 broadcasts, grade D)
- ✅ Chrony SHM updated: D_clock=+3.325ms every 8 seconds

**Chrony Integration:**

- ✅ TMGR source configured and active
- 🟡 Reachability: `21` (octal) = 2/8 successful polls (climbing, not yet 377)
- ✅ Offset: +925us to +3318us (consistent with fusion D_clock)
- ✅ Poll interval: 8 seconds (as configured)

**Data Pipeline Status:**

```
Recorder → Raw Buffer (SigMF) → Analytics → HDF5 (SWMR) → Fusion → Chrony SHM
   ✅           ✅                  ✅         ✅            ✅         ✅
```

---

## 🔴 NEXT SESSION FOCUS: ANALYTICS & TONE DETECTION METHODOLOGY

**Purpose:** Perform a critical review of the analytics service methodology, focusing on the quality and provenance of tone detections and their contribution to the final offset determination.

**Trigger:** A ~33ms "frame slip" observed in CHU data indicates potential flaws in the decoding or synchronization logic.

### Key Areas to Investigate

#### 1. CHU Decoding Robustness (High Priority)

- **Incident**: CHU offsets jumped from +19.45ms to -13.21ms (Delta: 32.66ms).
- **Analysis**: This is physically impossible for ionospheric propagation but matches the 33.33ms duration of a 300-baud character (10 bits).
- **Task**: Review `phase2_analytics_service.py` and `chu_decoder.py` (if split) to identify why the decoder lost sync and slipped by exactly one character. Implement stricter frame boundary checks.

#### 2. Tone Detection Quality

- Audit `tone_detector.py` algorithms for 1000/600/500 Hz tones.
- Evaluate SNR estimation and false-positive rejection logic.
- Verify how detections are weighted: is a noisy tone detection gaining too much influence in the final solution?

#### 3. Offset Determination Logic

- Trace the calculation path: `Raw Audio -> Detection -> L1 Measure -> L2 Timing`.
- Ensure uncertainty estimates accurately reflect the signal quality. A "slipped" frame should ideally have high uncertainty or be rejected.

### Success Criteria

- [ ] Root cause of CHU frame slip identified and fixed.
- [ ] Tone detection methodology verified or improved.
- [ ] Confidence scoring logic updated to penalize ambiguous decodes.

The analytics service is the core timing analysis engine. This review should examine:

#### 1. **Data Ingestion & Processing Pipeline**

- **File Discovery:** Is the tiered storage manager efficiently finding minute files?
- **Data Completeness:** Are incomplete minutes being handled correctly?
- **Processing Latency:** Is analytics keeping up with real-time data flow?
- **Error Recovery:** Does it gracefully handle corrupted or missing data?

**Key Files:**

- `src/hf_timestd/core/phase2_analytics_service.py` (main service, 2212 lines)
- `src/hf_timestd/core/tiered_storage.py` (hot/cold buffer management)

#### 2. **Phase 2 Temporal Engine (The "Brain")**

- **Step 1 - Tone Detection:** Is the matched filter detector optimal?
- **Step 2 - Channel Characterization:** Are BCD, Doppler, and discrimination robust?
- **Step 3 - Transmission Time Solver:** Is the propagation solver accurate?
- **Bootstrap Logic:** Are there edge cases in the RTP timestamp handling?
- **Calibration Convergence:** Does the timing calibrator converge reliably?

**Key Files:**

- `src/hf_timestd/core/phase2_temporal_engine.py` (2158 lines)
- `src/hf_timestd/core/transmission_time_solver.py`
- `src/hf_timestd/core/tone_detector.py`
- `src/hf_timestd/core/wwvh_discrimination.py`

#### 3. **Ionospheric Modeling**

- **IRI-2020 Integration:** Is the recently fixed `_extract_scalar()` handling all edge cases?
- **Fallback Logic:** Are geometric and heuristic models appropriate?
- **TEC Estimation:** Is vertical TEC calculation accurate?
- **Caching:** Is the location/time cache effective?

**Key Files:**

- `src/hf_timestd/core/ionospheric_model.py` (recently modified)
- `src/hf_timestd/core/physics_propagation.py`

#### 4. **HDF5 Data Product Writing**

- **SWMR Mode:** Is the recently added `refresh()` call sufficient?
- **Schema Compliance:** Are all required fields being populated?
- **Error Handling:** Are write failures being logged and recovered?
- **Performance:** Is the write rate keeping up with data generation?

**Key Files:**

- `src/hf_timestd/io/hdf5_writer.py` (recently modified)
- `src/hf_timestd/io/data_product_writer.py`
- `src/hf_timestd/schemas/*.json`

#### 5. **Multi-Station Discrimination**

- **WWV/WWVH Separation:** Is discrimination accurate on shared frequencies (5/10/15/20 MHz)?
- **BPM Integration:** Is BPM UT1 pulse detection robust?
- **CHU Handling:** Is CHU tick detection working correctly?
- **Confidence Scoring:** Are confidence thresholds appropriate?

**Key Files:**

- `src/hf_timestd/core/wwvh_discrimination.py`
- `src/hf_timestd/core/bpm_discriminator.py`
- `src/hf_timestd/core/station_model.py`

---

### Critical Review Checklist

#### Performance & Efficiency

- [ ] Are there any unnecessary computations in the hot path?
- [ ] Is memory usage reasonable for 24/7 operation?
- [ ] Are there any potential memory leaks?
- [ ] Is CPU usage appropriate for the workload?
- [ ] Are there opportunities for parallelization?

#### Correctness & Robustness

- [ ] Are edge cases handled (missing data, corrupted files, etc.)?
- [ ] Is error handling comprehensive and appropriate?
- [ ] Are there any race conditions in multi-threaded code?
- [ ] Are floating-point operations numerically stable?
- [ ] Are there any off-by-one errors in timestamp calculations?

#### Code Quality & Maintainability

- [ ] Is the code well-structured and modular?
- [ ] Are variable names clear and consistent?
- [ ] Is there adequate logging for debugging?
- [ ] Are there any "zombie" code paths that should be removed?
- [ ] Is documentation accurate and up-to-date?

#### Data Integrity

- [ ] Are timestamps being preserved correctly through the pipeline?
- [ ] Is RTP timestamp handling correct (especially around wraparound)?
- [ ] Are phase measurements being unwrapped correctly?
- [ ] Is data provenance being tracked (processing_version, etc.)?

#### Missed Opportunities

- [ ] Could calibration converge faster with better initialization?
- [ ] Could discrimination be improved with additional features?
- [ ] Could uncertainty estimates be more accurate?
- [ ] Are there unused channel metrics that could improve quality?

---

### Known Issues to Investigate

Based on recent fixes, these areas deserve extra scrutiny:

1. **Bootstrap Logic** - Recently fixed second boundary calculation. Are there other edge cases?
2. **IRI-2020 Integration** - Recently fixed array handling. Are there other type mismatches?
3. **HDF5 SWMR** - Recently fixed visibility. Are there performance implications?
4. **Schema Compliance** - Recently fixed missing field. Are there other schema issues?

---

### Diagnostic Commands for Next Session

```bash
# Check analytics service health
sudo systemctl status timestd-analytics
sudo journalctl -u timestd-analytics --since "1 hour ago" | grep -E "(ERROR|WARNING|Exception)"

# Verify all channels are processing
find /var/lib/timestd/phase2 -name "*clock_offset*.csv" -mmin -10 | wc -l  # Should be 9

# Check HDF5 write rates
for channel in WWV_5MHz WWV_10MHz WWV_15MHz WWV_20MHz WWV_25MHz WWVH_5MHz WWVH_10MHz WWVH_15MHz CHU_14670; do
    echo "$channel:"
    find /var/lib/timestd/phase2/$channel -name "*.h5" -mmin -10 | wc -l
done

# Check for schema validation errors
sudo journalctl -u timestd-analytics --since "1 hour ago" | grep -i "schema"

# Monitor processing latency
tail -f /var/log/hf-timestd/phase2-*.log | grep "Processing minute"

# Check discrimination accuracy
tail -100 /var/lib/timestd/phase2/WWV_10MHz/discrimination/*.csv | grep -E "WWV|WWVH"
```

---

### Success Criteria for Analytics Review

**Minimum Viable Outcome:**

- [ ] Identify top 5 potential issues or inefficiencies
- [ ] Document any correctness concerns with evidence
- [ ] Highlight missed optimization opportunities

**Ideal Outcome:**

- [ ] Fix any critical bugs found
- [ ] Implement performance optimizations
- [ ] Improve error handling and logging
- [ ] Update documentation to reflect current behavior
- [ ] Add regression tests for fixed issues

---

## Current System State

### Services Running

### Services Running ✅

All services are operational with new watchdog support:

```bash
# Check status
systemctl status timestd-core-recorder  # Running 59m
systemctl status timestd-analytics      # Running 10h29m
systemctl status timestd-fusion         # Running 12h6m
systemctl status timestd-web-ui-fastapi # Running
```

### Recent Improvements (2025-12-30)

**Service Management System Implemented:**

- ✅ Systemd watchdog support in `core_recorder_v2.py` and `multi_broadcast_fusion.py`
- ✅ Health check scripts for all services
- ✅ Email alerting on failures (`service-alert.sh`)
- ✅ Coordinated restarts via `PartOf=` directives
- ✅ Differentiated restart policies by criticality

**Key Files Modified:**

- `requirements.txt` - Added `systemd-python>=235`
- `src/hf_timestd/core/core_recorder_v2.py` - Watchdog notifications
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Watchdog notifications
- All systemd service files updated with watchdog config

**Deployment:**

- Ready to deploy via `scripts/deploy-service-management.sh`
- See `walkthrough.md` for complete documentation

---

## Known Issues to Investigate

### 1. Fusion Service Issues (PRIORITY: HIGH)

**Symptoms to Check:**

- Is fusion producing valid D_clock estimates?
- Are Chrony SHM updates consistent?
- Is calibration converging properly?
- Are there any NaN or infinite values in output?
- Is the global differential solver working correctly?

**Diagnostic Commands:**

```bash
# Check fusion logs for errors
sudo journalctl -u timestd-fusion -n 100 | grep -E "(ERROR|WARNING|CRASH)"

# Verify Chrony SHM updates
chronyc sources | grep TMGR

# Check fusion output
tail -20 /var/lib/timestd/phase2/fusion/fused_d_clock.csv

# Look for recent HDF5 files
ls -lth /var/lib/timestd/phase2/fusion/*.h5 | head -5
```

**Files to Review:**

- `src/hf_timestd/core/multi_broadcast_fusion.py` (2780 lines)
- `src/hf_timestd/core/differential_time_solver.py`
- `src/hf_timestd/core/tec_estimator.py`
- `/var/lib/timestd/phase2/fusion/fused_d_clock.csv`

**Common Issues:**

- HDF5 file locking conflicts (should be resolved with SWMR)
- Calibration not converging (check `broadcast_calibration.json`)
- Missing data from analytics causing fusion failures
- TEC estimation errors
- Global solver verification failures

### 2. Analytics Service Issues (PRIORITY: MEDIUM)

**Symptoms to Check:**

- Are all 9 channels processing data?
- Are HDF5 files being written correctly?
- Are there schema validation errors?
- Is WWV/WWVH discrimination working?
- Are timing measurements accurate?

**Diagnostic Commands:**

```bash
# Check analytics logs
journalctl -u timestd-analytics -n 100 | grep -E "(ERROR|WARNING)"

# Verify HDF5 output for all channels
find /var/lib/timestd/phase2 -name "*.h5" -mmin -10 | wc -l

# Check specific channel
ls -lth /var/lib/timestd/phase2/WWV_10MHz/timing_measurements/*.h5 | head -5

# Verify CSV fallback is working
find /var/lib/timestd/phase2 -name "*.csv" -mmin -10 | head -10
```

**Files to Review:**

- `src/hf_timestd/core/phase2_analytics_service.py`
- `src/hf_timestd/core/phase2_temporal_engine.py`
- `src/hf_timestd/core/wwvh_discrimination.py`
- `src/hf_timestd/io/data_product_writer.py`

**Common Issues:**

- Schema validation failures
- WWV/WWVH discrimination errors on shared frequencies
- BCD correlation issues
- Propagation mode estimation errors
- HDF5 write failures

### 3. Science Aggregator Issues (PRIORITY: LOW)

**Symptoms to Check:**

- Is TEC data being generated?
- Are aggregated products being created?
- Is GNSS VTEC integration working?

**Diagnostic Commands:**

```bash
# Check science aggregator logs
journalctl -u timestd-science-aggregator -n 50

# Verify TEC output
tail -20 /var/lib/timestd/phase2/fusion/tec_estimates.csv

# Check GNSS VTEC data
tail -20 /var/lib/timestd/gnss_vtec.csv
```

**Files to Review:**

- `src/hf_timestd/core/science_aggregator.py`
- `src/hf_timestd/core/tec_estimator.py`
- `src/hf_timestd/core/physics_propagation.py`

---

## Debugging Strategy

### Phase 1: Data Collection (15 minutes)

1. **Capture current system state:**

```bash
# Service status
systemctl status timestd-* > /tmp/service-status.txt

# Recent logs (last hour)
sudo journalctl -u timestd-fusion --since "1 hour ago" > /tmp/fusion-logs.txt
sudo journalctl -u timestd-analytics --since "1 hour ago" > /tmp/analytics-logs.txt

# Output validation
tail -100 /var/lib/timestd/phase2/fusion/fused_d_clock.csv > /tmp/fusion-output.csv
```

1. **Check for obvious errors:**

```bash
# Look for Python exceptions
grep -i "traceback\|exception\|error" /tmp/fusion-logs.txt
grep -i "traceback\|exception\|error" /tmp/analytics-logs.txt

# Look for NaN or invalid values
grep -E "nan|inf|-inf" /tmp/fusion-output.csv
```

### Phase 2: Fusion Service Deep Dive (30 minutes)

**Priority Order:**

1. Verify fusion is reading analytics data correctly
2. Check calibration state and convergence
3. Validate Chrony SHM updates
4. Inspect global differential solver results
5. Review TEC estimation

**Key Questions:**

- Is `_read_latest_tone_observations()` finding data?
- Are measurements being filtered correctly (BPM UT1 minutes)?
- Is weighted fusion producing reasonable results?
- Is Kalman filter converging?
- Are uncertainty components calculated correctly?

### Phase 3: Analytics Service Deep Dive (30 minutes)

**Priority Order:**

1. Verify all channels are processing
2. Check HDF5 schema compliance
3. Validate WWV/WWVH discrimination
4. Review timing measurement accuracy
5. Inspect propagation mode estimation

**Key Questions:**

- Are all 9 channels writing HDF5 files?
- Are schema validation errors occurring?
- Is discrimination confidence high enough?
- Are timing measurements within expected ranges?
- Is the BCD correlator working correctly?

### Phase 4: Fix Implementation (60 minutes)

**Approach:**

1. Start with highest-impact issues
2. Fix one issue at a time
3. Test each fix before moving to next
4. Document root cause and solution
5. Update tests if needed

---

## Critical Files Reference

### Fusion Pipeline

```
src/hf_timestd/core/
├── multi_broadcast_fusion.py      # Main fusion engine (2780 lines)
├── differential_time_solver.py    # Global differential solver
├── tec_estimator.py               # TEC estimation
├── chrony_shm.py                  # Chrony SHM interface
└── wwv_constants.py               # Station metadata
```

### Analytics Pipeline

```
src/hf_timestd/core/
├── phase2_analytics_service.py    # Main analytics service
├── phase2_temporal_engine.py      # Timing analysis
├── wwvh_discrimination.py         # Station discrimination
├── tone_detector.py               # Tone detection
└── bcd_correlator.py              # BCD time code
```

### Data I/O

```
src/hf_timestd/io/
├── data_product_writer.py         # HDF5 writer
├── data_product_reader.py         # HDF5 reader
└── schemas/                       # JSON schemas
    ├── l1_tone_detections_v1.json
    ├── l2_timing_measurements_v1.json
    └── l3_fusion_timing_v1.json
```

---

## Data Locations

### Input Data (Analytics reads from here)

```
/var/lib/timestd/raw_buffer/{CHANNEL}/{YYYYMMDD}/{minute}.bin
```

### Analytics Output (Fusion reads from here)

```
/var/lib/timestd/phase2/{CHANNEL}/
├── timing_measurements/           # L2 HDF5 files
├── tone_detections/              # L1A HDF5 files
├── clock_offset/                 # CSV (legacy)
└── discrimination/               # CSV (legacy)
```

### Fusion Output (Web UI reads from here)

```
/var/lib/timestd/phase2/fusion/
├── fused_d_clock.csv             # Main output
├── fusion_fusion_timing_*.h5     # HDF5 output
├── tec_estimates.csv             # TEC data
└── broadcast_calibration.json    # Calibration state
```

---

## Performance Baselines

### Expected Metrics (from CONTEXT.md)

**Fusion Performance:**

- D_clock fused: Should converge to ~0 ms (UTC alignment)
- Uncertainty: ~0.5-2.0 ms depending on conditions
- Allan deviation σ_y(τ=1000s): ~10⁻⁶ to 10⁻⁷
- Broadcasts used: 8-15 (out of 17 possible)
- Quality grade: A or B most of the time

**Chrony Integration:**

- TMGR source reachability: Should be 377 (all 8 polls successful)
- Offset: Should track fusion D_clock estimate
- Poll interval: 8 seconds (poll 3)

**Analytics Performance:**

- All 9 channels should process within 5 minutes of data arrival
- HDF5 files should update every minute
- Discrimination confidence: >0.8 for good signals

---

## Recent Changes Log

### 2025-12-30 (Today)

- **Service Management:** Implemented watchdog, health checks, email alerts
- **Watchdog Support:** Added to core_recorder_v2.py and multi_broadcast_fusion.py
- **Service Files:** Updated all with Type=notify, WatchdogSec, OnFailure handlers
- **Health Checks:** Created 4 scripts to verify data flow
- **Deployment:** Created deploy-service-management.sh script

### 2025-12-29

- **L3 Fusion HDF5:** Completed migration to HDF5 storage
- **SWMR Mode:** Fixed HDF5 locking issues with SWMR
- **Chrony SHM:** Fixed nsamples bug (0 → 1)
- **Allan Deviation:** Implemented live ADEV tracking

### 2025-12-27

- **TEC Estimator:** Integrated into fusion pipeline
- **GNSS VTEC:** Added physics propagation model integration

---

## Questions for Next Session

### Fusion Service

1. Is the fusion producing valid D_clock estimates consistently?
2. Are there any silent failures in the fusion loop?
3. Is calibration converging as expected?
4. Are all uncertainty components being calculated correctly?
5. Is the global differential solver being used effectively?

### Analytics Service

1. Are all 9 channels processing data without errors?
2. Is HDF5 schema validation passing for all data products?
3. Is WWV/WWVH discrimination accurate on shared frequencies?
4. Are there any channels with consistently poor quality?
5. Is the BCD correlator working correctly for all stations?

### Science Aggregator

1. Is TEC estimation producing reasonable values?
2. Is GNSS VTEC integration working when hardware is available?
3. Are aggregated science products being generated?

---

## Success Criteria for Next Session

### Minimum Viable Outcome

- [ ] Identify top 3 issues in fusion service
- [ ] Identify top 3 issues in analytics service
- [ ] Document root causes with evidence from logs

### Ideal Outcome

- [ ] Fix critical fusion issues (D_clock accuracy, Chrony SHM)
- [ ] Fix critical analytics issues (HDF5 writes, discrimination)
- [ ] Verify all services producing valid output
- [ ] Update tests to prevent regression
- [ ] Document fixes in CHANGELOG.md

---

## AI Agent Preparation

**What the AI should do first:**

1. **Read this entire document** to understand current state
2. **Check service status** using diagnostic commands above
3. **Review recent logs** for errors and warnings
4. **Validate output data** for NaN, missing values, schema errors
5. **Prioritize issues** by impact on system accuracy

**What the AI should NOT do:**

- Don't make changes without understanding root cause
- Don't restart services without checking logs first
- Don't modify calibration files manually
- Don't disable features to "fix" problems

**Key Principles:**

- **Data-driven:** Use logs and output to guide debugging
- **Systematic:** Fix one issue at a time, test thoroughly
- **Document:** Record root cause and solution for each fix
- **Test:** Verify fix doesn't break other functionality

---

## Useful Commands Quick Reference

```bash
# Service management
sudo systemctl status timestd-fusion
sudo systemctl restart timestd-fusion
sudo journalctl -u timestd-fusion -f

# Data validation
tail -f /var/lib/timestd/phase2/fusion/fused_d_clock.csv
ls -lth /var/lib/timestd/phase2/fusion/*.h5 | head -5
chronyc sources | grep TMGR

# Health checks
/opt/hf-timestd/scripts/health-check-fusion.sh
/opt/hf-timestd/scripts/health-check-analytics.sh

# Python debugging
python3 -m py_compile src/hf_timestd/core/multi_broadcast_fusion.py
python3 -c "from hf_timestd.core import multi_broadcast_fusion; print('OK')"

# HDF5 inspection
h5ls /var/lib/timestd/phase2/fusion/fusion_fusion_timing_20251230.h5
h5dump -H /var/lib/timestd/phase2/fusion/fusion_fusion_timing_20251230.h5
```

---

**Last Updated:** 2025-12-30 12:20 UTC  
**Next Session Focus:** Debug and fix fusion → analytics → science-aggregator  
**Current Status:** Service management complete, system stable, ready for debugging
