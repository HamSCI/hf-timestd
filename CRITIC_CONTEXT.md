# CRITIC CONTEXT - Next Session Preparation

**Date:** 2025-12-30  
**Session Focus:** Identify and Fix Issues in Fusion, Analytics, and Science-Aggregator Services  
**Status:** Service management improvements complete, system running with watchdog support

---

## Session Objective

**PRIMARY GOAL:** Systematically identify and fix problem spots in the data pipeline services, prioritizing:

1. **Fusion Service** (`multi_broadcast_fusion.py`) - Highest priority
2. **Analytics Service** (`phase2_analytics_service.py`) - Medium priority  
3. **Science Aggregator** (`science_aggregator.py`) - Lower priority

**APPROACH:** Data-driven debugging using live system logs, service status, and output validation.

---

## Current System State

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
