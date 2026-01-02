# HF Time Standard Analysis - Project Context

**Last Updated:** January 2, 2026  
**Version:** 3.8.1 (Post-HDF5 Transition & Verification Enhancement)  
**Status:** Production (9 channels running at AC0G, 24kHz Sample Rate)

## Quick Reference

**What:** Precision HF timing system extracting D_clock measurements from WWV/WWVH/CHU/BPM broadcasts  
**Where:** `/opt/hf-timestd` (production) or `/home/mjh/git/hf-timestd` (development)  
**Services:** `timestd-core-recorder`, `timestd-analytics`, `timestd-fusion`, `timestd-vtec`, `timestd-web-ui`, `timestd-science-aggregator`, `timestd-radiod-monitor`  
**Web UI:** <http://localhost:3000>

---

## Current State (Jan 2, 2026)

### ✅ Recently Completed (v3.8.1)

1. **HDF5 Transition Complete**
    * **Fusion Service:** HDF5-only output (CSV writers removed)
    * **Analytics Service:** HDF5-only output (CSV writers removed)
    * **Verification:** All core timing data confirmed in HDF5
    * **Status:** CSV files remain on disk but are no longer updated

2. **Fusion Service Bug Fixes**
    * **Issue:** Service running but silent after HDF5 transition
    * **Root Causes:**
        * Logger used before definition (NameError during import)
        * Orphaned call to deleted `_write_tec_result()` method
    * **Fix:** Moved logger initialization, removed orphaned method call
    * **Status:** ✅ Service operational, writing HDF5, Chrony integration working (reach=3)
    * **Commit:** `5dd74cf`

3. **Enhanced Pipeline Verification**
    * **Problem:** Original `verify_pipeline.sh` failed to detect fusion service silent failure
    * **Enhancements:**
        * Fusion: Log analysis, HDF5 write verification, error scanning
        * Calibration: GPSDO-aware context (2h age is normal)
        * TEC: Actionable diagnostics with service-specific remediation
        * Chrony: Reach=0 is now FAIL, low reach flagged
    * **Impact:** All warnings now include root cause, diagnostic commands, and fix instructions
    * **Commit:** `466c15c`

### 📊 Deployment Status

* **Services:** All 7 services active
* **Pipeline:** HDF5-native flow functional from L0 to L3
* **Verification:** Enhanced `verify_pipeline.sh` provides actionable diagnostics
* **Known Issue:** TEC HDF5 stale (55m) - science_aggregator investigation needed

---

## 🎯 Next Session Priority: Science Aggregator Investigation

**Goal:** Verify science_aggregator is functioning properly, recording features of interest, and storing them in HDF5.

**Context:**  
The `timestd-science-aggregator` service runs every 5 minutes to aggregate multi-frequency timing data and produce TEC estimates. The enhanced verification script detected that TEC HDF5 files are stale (>30 minutes), indicating a potential issue.

### Objectives

1. **Diagnose TEC Staleness**
    * Check `timestd-science-aggregator` service status and logs
    * Verify service is running and executing aggregation cycles
    * Identify why TEC files aren't being updated

2. **Verify Data Flow**
    * Confirm analytics service is producing multi-frequency timing measurements
    * Verify science_aggregator can read HDF5 timing data (or falls back to CSV)
    * Check TEC estimation is working (multi-frequency analysis)

3. **Validate HDF5 Output**
    * Verify TEC HDF5 schema is correct and complete
    * Check if CSV fallback is being used (and why)
    * Ensure HDF5 writes are atomic and SWMR-compatible

4. **Identify Features of Interest**
    * Review what science products are being generated
    * Verify ionospheric event detection (if implemented)
    * Check if all relevant science data is captured in HDF5

### Key Files to Investigate

* **Service:** `src/hf_timestd/core/science_aggregator.py`
  * Main aggregation logic
  * TEC estimation integration
  * HDF5 writer usage (with CSV fallback)
  * Poll interval: 300s (5 minutes)

* **HDF5 Schema:** `src/hf_timestd/io/schemas/L3_tec.yaml`
  * TEC data product schema
  * Fields: tec_tecu, confidence, n_frequencies, residuals_ms, etc.

* **TEC Estimator:** `src/hf_timestd/core/tec_estimator.py`
  * Multi-frequency analysis
  * Group delay calculation

* **Service Logs:**

    ```bash
    sudo journalctl -u timestd-science-aggregator -n 100
    sudo systemctl status timestd-science-aggregator
    ```

* **Data Locations:**
  * TEC HDF5: `/var/lib/timestd/phase2/science/tec/*.h5`
  * TEC CSV: `/var/lib/timestd/phase2/science/tec/*.csv` (fallback)

### Diagnostic Commands

```bash
# Check service status
sudo systemctl status timestd-science-aggregator

# View recent logs
sudo journalctl -u timestd-science-aggregator -n 100

# Check TEC file freshness
ls -lht /var/lib/timestd/phase2/science/tec/*.h5 | head -5

# Verify analytics is producing multi-frequency data
find /var/lib/timestd/phase2 -name "*_timing_measurements_*.h5" -mmin -10

# Run verification script
./scripts/verify_pipeline.sh
```

### Expected Behavior

* **TEC Update Frequency:** Every ~5 minutes (300s poll interval)
* **Input Requirements:** Multi-frequency timing measurements from analytics
* **Output:** HDF5 files with TEC estimates (CSV as fallback)
* **Normal Conditions:** TEC files updated within 15 minutes

### Potential Issues to Check

1. **Service Not Running:** Check systemd status
2. **No Multi-Frequency Data:** Verify analytics producing timing on multiple bands
3. **HDF5 Write Failures:** Check for exceptions in logs
4. **TEC Estimation Failures:** Check for poor fits or insufficient frequencies
5. **CSV Fallback Active:** Verify HDF5 writer is being used

---

## System Architecture

### The Seven Services

1. **Core Recorder:** Digital RF capture (`timestd-core-recorder`)
2. **Analytics:** Signal processing (`timestd-analytics`)
3. **Fusion:** Multi-broadcast timing solve (`timestd-fusion`)
4. **VTEC:** GNSS/IONEX data manager (`timestd-vtec`)
5. **Science Aggregator:** TEC estimation (`timestd-science-aggregator`) ← **NEXT FOCUS**
6. **Web UI:** Visualization dashboard (`timestd-web-ui`)
7. **Radiod Monitor:** Hardware watchdog (`timestd-radiod-monitor`)

### Data Flow (HDF5-Native)

```
RTP (UDP) → Core (Digital RF .h5) → Analytics (L2 .h5) → Fusion (L3 .h5) → Chrony (SHM)
                                           ↓
                                    Science Aggregator (L3 TEC .h5)
                                           ↑
                                      VTEC (L3A .h5)
```

## AI Agent Guidance for Next Session

**Preparation:**

* You are investigating why TEC HDF5 files are stale (>30 minutes)
* The science_aggregator service should update TEC every 5 minutes
* **Do not restart services** until you understand the root cause
* Check logs first, then verify data flow, then examine code

**Investigation Steps:**

1. Check service status and logs for errors
2. Verify analytics is producing multi-frequency timing data
3. Check if HDF5 writer is working or falling back to CSV
4. Examine TEC estimation logic for failures
5. Validate HDF5 schema and output format

**Success Criteria:**

* TEC HDF5 files updated within 15 minutes
* Service logs show successful aggregation cycles
* HDF5 writer being used (not CSV fallback)
* Clear understanding of what science features are being captured
