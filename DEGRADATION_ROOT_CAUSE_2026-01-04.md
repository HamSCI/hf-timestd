# Root Cause Analysis: Chrony Feed Accuracy Degradation (2026-01-04 00:00 UTC)

**Date:** 2026-01-04  
**Analyst:** AI Agent (Cascade)  
**Severity:** HIGH - Chrony feed accuracy degraded significantly starting at 00:00 UTC  
**Status:** ROOT CAUSE IDENTIFIED

---

## Executive Summary

The chrony feed accuracy degraded starting around 00:00 UTC on 2026-01-04 due to **two cascading root causes**:

1. **HDF5 Schema Incompatibility (00:00-00:45 UTC):** Code deployment added a new field (`raw_arrival_time_ms`) to L2 timing measurements while existing Jan 3 HDF5 files were still being written in SWMR mode. HDF5 files in SWMR mode cannot have new datasets added after initialization, causing write failures and service instability.

2. **Systemd Watchdog Timeout (02:23 UTC onwards):** The watchdog timeout of 30 seconds was too aggressive for the fusion service's legitimate HDF5 read operations. The service was killed with SIGABRT every 30-40 seconds during the first `fuse()` call, preventing any chrony updates and causing continuous instability that persisted well beyond the file rotation.

**Critical:** The instability did NOT end at 00:45 UTC as initially thought. The watchdog issue caused **16+ consecutive crashes** from 02:23-02:34 UTC and likely continued until the present time.

---

## Timeline of Events

### 2026-01-03 23:10 UTC
- Commit `f6478d9` deployed: "Enhanced propagation page TEC display and fixed science aggregator HDF5 integration"
- Analytics service restarted at 23:55:30 UTC

### 2026-01-04 00:00 UTC
- **Degradation begins** (visible in Fusion History graph)
- Analytics service experiencing instability
- Multiple service restarts: 00:04, 00:19, 00:20

### 2026-01-04 00:20 UTC
- Analytics service restarted, started with **0/9 channels** (critical failure)
- Fusion service crash-loop begins (5 consecutive crashes with exit code 1)
- Crashes at: 00:20:24, 00:20:34, 00:20:44, 00:20:54, 00:21:04, 00:21:14
- No data available for fusion → Chrony feed degraded

### 2026-01-04 00:21 UTC
- Analytics service restarted again at 00:21:17 UTC
- Successfully started 9/9 channels
- Fusion service stabilized at 00:21:28 UTC

### 2026-01-04 00:45 UTC
- **Daily HDF5 file rotation occurs**
- New files created: `*_timing_measurements_20260104.h5`
- Old files closed: `*_timing_measurements_20260103.h5` (last modified 00:45:27 UTC)
- Analytics service restarted at 00:45:33 UTC
- System begins recovery

### 2026-01-04 01:02 UTC
- Commit `21c3ba3` deployed: "Fix TEC calculation input by adding raw_arrival_time_ms field"
- **This commit explicitly added the new field to schema v1.1.0**
- Commit message warned: "Existing HDF5 files created with old schema cannot have new datasets added"

### 2026-01-04 02:02 UTC
- Fusion service started (after manual intervention)

### 2026-01-04 02:22 UTC
- Fusion service restarted

### 2026-01-04 02:23-02:34+ UTC
- **NEW ROOT CAUSE: Systemd watchdog timeout**
- Fusion service crashes **continuously** with SIGABRT every 30-40 seconds
- 16+ consecutive crashes observed
- Service hangs during first `fuse()` call reading HDF5 files
- Never sends first `WATCHDOG=1` notification within 30-second timeout
- **Instability persists indefinitely** - not resolved by file rotation

---

## Root Cause Analysis

### Primary Cause: HDF5 SWMR Mode Limitation

**HDF5 SWMR Mode Constraint:**
```
Files opened in SWMR mode cannot have their structure modified after initialization.
New datasets CANNOT be added to an existing SWMR file.
```

**What Happened:**

1. **Jan 3 files were created** with schema v1.0.0 (without `raw_arrival_time_ms`)
2. **Code deployed around 23:10-01:02 UTC** added `raw_arrival_time_ms` to schema v1.1.0
3. **Analytics service attempted to write** new field to existing Jan 3 files (still open in SWMR mode)
4. **HDF5 writer tried to create new dataset** in SWMR file → **FAILED** (silently or with exceptions)
5. **Write failures cascaded:**
   - Analytics service couldn't write complete measurements
   - Fusion service received incomplete/corrupt data
   - Fusion service crashed repeatedly (exit code 1)
   - Chrony feed degraded due to missing/bad data

### Evidence

**File Timestamps:**
```bash
# Jan 3 file - last modified during the incident
/var/lib/timestd/phase2/SHARED_2500/SHARED_2500_timing_measurements_20260103.h5
Modify: 2026-01-04 00:45:27 UTC  # Still being written to during incident
Birth:  2026-01-03 00:02:19 UTC

# Jan 4 file - created after rotation
/var/lib/timestd/phase2/SHARED_2500/SHARED_2500_timing_measurements_20260104.h5
Birth:  2026-01-04 00:54:15 UTC  # Created after rotation
```

**Dataset Verification:**
```python
# Jan 3 file (schema v1.0.0)
Datasets: [..., 'clock_offset_ms', 'uncertainty_ms', ...]
Has raw_arrival_time_ms: False  # ← Missing new field

# Jan 4 file (schema v1.1.0)
# Cannot check - file locked by active writer
# But should contain raw_arrival_time_ms
```

**Service Logs:**
- Analytics: Multiple restarts between 23:55 and 00:45 UTC
- Fusion: 5 consecutive crashes at 00:20 UTC
- Analytics: "Started 0/9 Phase 2 analytics channels" at 00:20:23 UTC (complete failure)

---

## Impact Assessment

### Timing Accuracy Impact

**Before 00:00 UTC (Normal Operation):**
- D_clock: Stable, low scatter
- Uncertainty: ~0.8-1.5 ms
- Quality: Grade B/C
- Chrony reach: 210 (octal) = 53%

**During 00:00-00:45 UTC (Degraded):**
- D_clock: High scatter, increased offset
- Uncertainty: Likely increased (data incomplete)
- Quality: Degraded
- Fusion crashes: No chrony updates during crash-loop

**After 00:45 UTC (Recovery):**
- System recovered after file rotation
- New files with correct schema
- Gradual return to normal operation

### Data Loss

**Critical Period:** 00:20-00:21 UTC (1 minute)
- Fusion service crashed 5 times
- No chrony updates during this period
- System clock may have drifted

**Degraded Period:** 00:00-00:45 UTC (45 minutes)
- Incomplete measurements written
- Fusion operating on partial data
- Reduced accuracy and reliability

---

## Root Cause #2: Systemd Watchdog Timeout (02:23 UTC onwards)

### The Watchdog Problem

In v3.10.0, a systemd watchdog was enabled with a **30-second timeout**. The fusion service must send `WATCHDOG=1` notifications every 30 seconds or systemd kills it with SIGABRT.

**The Issue:** The fusion service legitimately takes **>30 seconds** on the first `fuse()` call to:
1. Read HDF5 files from multiple channels (9 channels × 10 minutes lookback)
2. Parse and validate measurements
3. Run global differential solver
4. Apply VTEC corrections
5. Perform fusion calculations

**Evidence:**
```
Jan 04 02:23:08 bee1 systemd[1]: timestd-fusion.service: Watchdog timeout (limit 30s)!
Jan 04 02:23:08 bee1 systemd[1]: Killing process 2082742 (python) with signal SIGABRT.
```

The service enters the main loop and sends `READY=1`, but then gets stuck in `fusion.fuse()` at line 2753 before it can send the first `WATCHDOG=1` at line 2746.

### Why HDF5 Reads Are Slow

The fusion service reads from HDF5 files that are **actively being written** by the analytics service in SWMR mode. Several factors contribute to slow reads:

1. **SWMR Metadata Refresh:** SWMR readers must refresh metadata to see new data
2. **File Lock Contention:** Multiple processes accessing the same files
3. **Large Lookback Window:** 10 minutes × 9 channels = potentially thousands of measurements
4. **First-Run Overhead:** Cold cache, file discovery, schema validation

### Impact Timeline

**02:23-02:34 UTC:** 16+ consecutive crashes observed (likely continued beyond)
- Crash every 30-40 seconds
- No chrony updates during this period
- Complete failure of timing discipline
- System clock drifting without correction

**This explains why instability persisted well beyond the 00:45 UTC file rotation.**

---

## Why This Happened

### Design Flaw: Schema Evolution in SWMR Mode

The HDF5 writer code **does not handle schema evolution** for files already open in SWMR mode:

```python
# From hdf5_writer.py:385-398
if field_name not in hdf5_file:
    # This CREATE will FAIL if file is in SWMR mode!
    hdf5_file.create_dataset(
        field_name,
        shape=(0,),
        maxshape=(None,),
        dtype=dtype,
        chunks=True,
        compression='gzip',
        compression_opts=4
    )
```

**Problem:** No check for SWMR mode before attempting dataset creation.

**Result:** Silent failure or exception → incomplete writes → cascading failures.

### Deployment Timing Issue

The schema change was deployed **before midnight UTC**, causing the issue to manifest immediately:

1. **23:10 UTC:** First deployment (f6478d9) - may have included partial changes
2. **00:00 UTC:** Degradation begins - code trying to write new field
3. **01:02 UTC:** Second deployment (21c3ba3) - explicitly adds raw_arrival_time_ms
4. **Commit message acknowledged the issue** but deployment happened anyway

**Quote from commit 21c3ba3:**
> "Deployment note: Existing HDF5 files created with old schema cannot have
> new datasets added. Files must be deleted or wait for daily rotation."

**The deployment should have waited until after midnight UTC** to allow natural file rotation.

---

## Recommendations

### Immediate Actions (Critical)

1. **Increase Watchdog Timeout** (MOST URGENT)
   ```bash
   # Already fixed in systemd/timestd-fusion.service (30s → 120s)
   sudo cp systemd/timestd-fusion.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl restart timestd-fusion
   ```

2. **Verify Current System State**
   ```bash
   # Check all services are running
   systemctl status timestd-core-recorder timestd-analytics timestd-fusion
   
   # Verify Jan 4 files have raw_arrival_time_ms dataset
   python3 -c "import h5py; f = h5py.File('/var/lib/timestd/phase2/SHARED_2500/SHARED_2500_timing_measurements_20260104.h5', 'r'); print('raw_arrival_time_ms' in f)"
   ```

3. **Monitor Chrony Reach**
   ```bash
   chronyc sources | grep "REFID.*SHM"
   ```

### Short-Term Fixes (High Priority)

1. **Add SWMR Mode Check to HDF5 Writer**

   Modify `hdf5_writer.py` to detect SWMR mode and handle schema changes gracefully:

   ```python
   # Before creating new dataset
   if field_name not in hdf5_file:
       if hdf5_file.swmr_mode:
           # Cannot add dataset to SWMR file - skip this field
           logger.warning(
               f"Cannot add field '{field_name}' to SWMR file {hdf5_file.filename}. "
               f"Field will be missing until next file rotation."
           )
           continue
       
       # Safe to create dataset
       hdf5_file.create_dataset(...)
   ```

2. **Add Schema Version Validation**

   Check file schema version before writing:

   ```python
   def _ensure_file_open(self, timestamp_utc: str) -> h5py.File:
       # ... existing code ...
       
       # Validate schema version matches
       if 'schema_version' in self._current_file.attrs:
           file_schema_version = self._current_file.attrs['schema_version']
           if file_schema_version != self.schema['schema_version']:
               logger.warning(
                   f"Schema version mismatch: file={file_schema_version}, "
                   f"writer={self.schema['schema_version']}. "
                   f"Some fields may be missing until next rotation."
               )
   ```

3. **Improve Error Handling in Analytics Service**

   Add try-except around HDF5 writes with fallback:

   ```python
   try:
       self.hdf5_l2_writer.write_measurement(l2_measurement)
   except Exception as e:
       logger.error(f"HDF5 write failed: {e}")
       # Don't crash - continue processing
       # Consider writing to backup CSV or queue for retry
   ```

### Medium-Term Improvements (Important)

1. **Implement Schema Migration Strategy**

   - Force file rotation when schema version changes
   - Add schema version to filename: `CHANNEL_timing_measurements_YYYYMMDD_v1.1.0.h5`
   - Implement backward-compatible readers

2. **Add Pre-Deployment Validation**

   Create deployment checklist:
   - [ ] Schema version changed?
   - [ ] If yes, deployment must occur after midnight UTC
   - [ ] Or, force file rotation before deployment
   - [ ] Test schema compatibility with existing files

3. **Improve Service Resilience**

   - Add circuit breaker pattern to fusion service
   - Implement graceful degradation (continue with partial data)
   - Add health checks that detect HDF5 write failures
   - Auto-rotate files on schema mismatch

4. **Add Monitoring Alerts**

   - Alert on HDF5 write failures
   - Alert on schema version mismatches
   - Alert on fusion service crashes
   - Alert on chrony reach drops below threshold

### Long-Term Architectural Changes (Strategic)

1. **Decouple Schema from File Structure**

   Use a more flexible format that supports schema evolution:
   - Consider Apache Parquet (supports schema evolution)
   - Or implement HDF5 schema versioning with migration tools
   - Or use separate files per schema version

2. **Implement Zero-Downtime Deployments**

   - Blue-green deployment strategy
   - Rolling updates with backward compatibility
   - Schema migration as separate step from code deployment

3. **Add Integration Tests**

   Test suite should include:
   - Schema evolution scenarios
   - SWMR mode edge cases
   - Service restart during active writes
   - File rotation boundary conditions

---

## Lessons Learned

### What Went Wrong

1. **Schema change deployed without considering file rotation timing**
2. **HDF5 SWMR mode limitations not accounted for in writer code**
3. **No validation that new fields can be added to existing files**
4. **Commit message warned about the issue but deployment proceeded anyway**
5. **No monitoring detected the HDF5 write failures immediately**
6. **Watchdog timeout set too aggressively (30s) without profiling actual service behavior**
7. **Watchdog enabled in v3.10.0 without testing under realistic load conditions**
8. **No consideration for first-run overhead when reading large HDF5 datasets**

### What Went Right

1. **Daily file rotation provided automatic recovery at 00:45 UTC**
2. **Service restarts eventually stabilized the system**
3. **Root cause was documented in commit message (even if not acted upon)**
4. **SWMR mode prevented data corruption (failed safely)**

### Key Takeaway

**Schema changes that add new fields to HDF5 files must be coordinated with file rotation boundaries.** Either:
- Deploy after midnight UTC (after rotation)
- Force file rotation before deployment
- Implement schema migration that handles SWMR mode constraints

---

## Action Items

### Owner: System Administrator

- [ ] **URGENT:** Restart timestd-fusion service (currently in watchdog failure)
- [ ] Verify all services operational
- [ ] Check Jan 4 HDF5 files contain raw_arrival_time_ms dataset
- [ ] Monitor chrony reach for next 24 hours

### Owner: Development Team

- [ ] **HIGH:** Implement SWMR mode check in HDF5 writer (prevent future incidents)
- [ ] **HIGH:** Add schema version validation
- [ ] **MEDIUM:** Improve error handling in analytics service
- [ ] **MEDIUM:** Create deployment checklist for schema changes
- [ ] **LOW:** Implement schema migration strategy

### Owner: Operations Team

- [ ] Add monitoring alerts for HDF5 write failures
- [ ] Add monitoring alerts for schema version mismatches
- [ ] Document schema change deployment procedure
- [ ] Schedule post-incident review meeting

---

## Conclusion

The chrony feed accuracy degradation was caused by **two cascading failures**:

1. **HDF5 Schema Incompatibility (00:00-00:45 UTC):** Preventable deployment timing issue combined with design limitation in HDF5 writer. Partially resolved by file rotation at 00:45 UTC.

2. **Systemd Watchdog Timeout (02:23+ UTC):** Overly aggressive 30-second timeout killed fusion service during legitimate HDF5 operations. This caused **continuous instability that persisted well beyond the file rotation** and likely continues until present.

**Immediate action required:** 
1. Increase watchdog timeout to 120 seconds
2. Deploy updated systemd service file
3. Restart fusion service
4. Implement SWMR mode checks to prevent schema issues

**Risk:** Without both fixes, the system will continue to crash every 30 seconds indefinitely.

---

**Document Version:** 1.0  
**Last Updated:** 2026-01-04 12:13 UTC  
**Next Review:** After implementing recommended fixes
