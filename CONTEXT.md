# HF-TimeStd AI Agent Context

**Last Updated**: 2026-01-04 23:00 UTC  
**System Version**: 4.1.0  
**Current Focus**: Web UI Enhancements - Individual Station Pages & Ionosphere Characterization  
**System Status**: Stable, HDF5-native, Modernized Web API

---

## Executive Summary

The `hf-timestd` system is a high-precision time transfer system receiving WWV/WWVH/CHU/BPM time signals. The critical path (Recorder → Analytics → Fusion → Chrony) is fully HDF5-native and stable.

**Recent Achievements (Web UI Modernization):**

- ✅ **New Service**: `timestd-web-api` (FastAPI) replaced legacy `monitor-server.js`.
- ✅ **Logs Viewer**: Real-time access to systemd journals via Web UI (`/static/logs.html`).
- ✅ **System Health**: Refined dashboard with true process uptime and improved UX.
- ✅ **API Docs**: Interactive Swagger UI at `/api/docs`.

**Next Priority**: Develop specialized dashboard pages for each transmitting station (WWV, WWVH, CHU, BPM) to visualize their unique characteristics and ionospheric paths.

---

## Session Summary (Web UI Modernization)

**Objective**: Replace the legacy Node.js monitoring server with a robust Python/FastAPI service and fix UI deficiencies.

**Accomplishments:**

1. **Service Migration**:
    - Created `timestd-web-api` service (Python/FastAPI).
    - Ported functionality from Node.js, eliminating subprocess overhead.
    - Seamlessly migrated systemd service (`timestd-web-ui` → `timestd-web-api`) with migration script.

2. **Logs Viewer Implementation**:
    - **Backend**: Implemented `/api/logs` endpoint using `journalctl` with filtering (service, time, level).
    - **Frontend**: Created `/static/logs.html` with search, auto-refresh, and filtering.
    - **Integration**: Fixed broken "System Logs" link in main dashboard.

3. **System Health Improvements**:
    - Implemented true process uptime calculation using `ps -o etime`.
    - Removed redundant "Channel Status Matrix".
    - Standardized font sizes for better readability.

4. **Deployment**:
    - Verified proper operation on `bee1`.
    - Confirmed correct service mapping (`web-api`, `grape`, etc.) in logs router.

---

## Next Session Priority: Individual Station Pages

### Objective

Create dedicated dashboard pages for each of the four monitored stations: **WWV**, **WWVH**, **CHU**, and **BPM**.

### Motivation

Currently, data is aggregated by channel (frequency). However, users think in terms of **Stations** and **Paths**. Each station has unique characteristics that need specialized visualization.

### Requirements

#### 1. Station-Specific Metrics

- **WWV (Fort Collins, CO)**:
  - Short path (stable).
  - High SNR focus.
  - Multi-frequency comparison (2.5, 5, 10, 15, 20, 25 MHz).

- **WWVH (Kauai, HI)**:
  - Long path (variable).
  - Acoustic signature (1200 Hz tone) reliability.
  - Interference analysis (WWV vs WWVH dominance).

- **CHU (Ottawa, Canada)**:
  - Unique Bell 103 digital code decoding stats.
  - Reliable anchoring (3.33, 7.85, 14.67 MHz exclusive frequencies).
  - Frame slip detection (500ms jumps).

- **BPM (Pucheng, China)**:
  - Very long path (sporadic).
  - Exclusion zone visualization (rejection of WWV aliases).
  - UT1 tick detection (100ms vs 10ms).

#### 2. Ionospheric Characterization (Per-Path)

- **TEC Analysis**: Display the specific Total Electron Content along the Great Circle Period (GCP) for that station.
- **Ray Tracing**: Visualize predicted hop structure (1F2, 2F2, etc.) based on current IONEX maps.
- **Delay Budget**: Breakdown of geometric delay vs. ionospheric group delay.

### Implementation Plan

1. **Backend**: Create `/api/stations/{station_id}` endpoints in FastAPI.
2. **Frontend**: Create generic `station.html` template or individual pages.
3. **Visualization**: Use Plotly.js for station-specific time series (SNR, Delay, TEC).

---

## System Architecture Overview

### Key Services

1. **timestd-core-recorder**: Receives IQ from radiod, writes Digital RF
2. **timestd-analytics**: Processes IQ → timing measurements (9 channels)
3. **timestd-fusion**: Fuses measurements → Chrony SHM updates
4. **timestd-science-aggregator**: Generates science products (TEC, propagation stats)
5. **timestd-vtec**: Downloads and processes GNSS VTEC data
6. **timestd-web-api**: **[NEW]** FastAPI Dashboard & API (Port 8000)

### Critical File Locations

- **Production Code**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/`
- **Web API**: `/opt/hf-timestd/web-api/` (Deployed from `git/hf-timestd/web-api/`)
- **Data Root**: `/var/lib/timestd/`
- **Logs**: `journalctl -u timestd-*`

---

## Important Notes for AI Agents

- **Web UI**: backend is now **FastAPI** (`web-api/main.py`), not Express/Node.js.
- **Logs**: Access via `/api/logs` instead of reading files directly if possible.
- **Service Restart**: `sudo systemctl restart timestd-web-api` for UI changes.
- **Deployment**: Use `scripts/migrate_web_service.sh` or manual copy to `/opt/hf-timestd/web-api/`.

**Last Updated**: 2026-01-04 20:20 UTC  
**System Version**: 4.0.0  
**Current Focus**: Web UI Enhancements - ISO GUM Reporting & Science Data Visualization  
**System Status**: Stable, HDF5-native for all critical path data

---

## Executive Summary

The `hf-timestd` system is a high-precision time transfer system that receives WWV/WWVH/CHU/BPM time signals via HF radio, processes them through a multi-stage pipeline, and provides UTC time corrections to the system clock via Chrony. The system is currently operational and stable with comprehensive HDF5 data storage for all critical path components.

**Current System State:**

- ✅ Analytics pipeline stable with D_clock centered at 0ms
- ✅ Critical validation fixes deployed (propagation delays, inter-station consistency, continuity)
- ✅ Fusion service tracking well with tight uncertainty (±1-2ms)
- ✅ **HDF5 Migration Complete**: All critical path data now in HDF5 format
- 🎯 **Next Priority**: Enhance web UI with ISO GUM uncertainty reporting and science data visualization

---

## Session Summary (2026-01-04 - Latest)

### Test Signal HDF5 Migration - COMPLETED ✅

Successfully migrated WWV/WWVH scientific test signal analysis from CSV-only to parallel CSV+HDF5 writes, completing the HDF5 migration for all critical path data.

**Implementation:**

- **HDF5 Writer Added**: Initialized `hdf5_l2_test_signal_writer` in `Phase2AnalyticsService`
- **Schema**: Using existing `l2_test_signal_v1.json` (38 comprehensive fields)
- **Parallel Writes**: Test signal data written to both CSV and HDF5 simultaneously
- **Bug Fixed**: Corrected `AttributeError` where code referenced non-existent `self.frequency_mhz`
  - Changed to use `self._get_frequency_mhz()` method in `_is_chu_channel()` and `_write_test_signal()`

**Data Enrichment (HDF5 vs CSV):**
HDF5 captures 3x more data than legacy CSV format:

- **CSV**: 13 fields (basic detection metrics)
- **HDF5**: 38 fields including time-series data, anomaly detection, noise analysis, scintillation metrics, quality assessment

**HDF5 Data Pipeline - COMPLETE:**
All data products now use HDF5 storage:

- ✅ L0 (Raw): Digital RF HDF5
- ✅ L1A (Observables): Channel observables HDF5
- ✅ L1A (Tones): Tone detections HDF5
- ✅ L1B (Timecode): BCD timecode HDF5
- ✅ L2 (Timing): Timing measurements HDF5
- ✅ **L2 (Test Signals): Test signal analysis HDF5** ← NEW
- ✅ L3 (Fusion): Fusion results HDF5
- ✅ L3 (TEC): Ionospheric TEC HDF5
- ✅ L3 (VTEC): GNSS VTEC HDF5

**CSV Status:**

- **Critical path**: HDF5 primary, CSV parallel (validation period)
- **Auxiliary monitoring**: CSV only (doppler, 440hz, discrimination, audio tones, timing/quality metrics)
- **Recommendation**: Keep auxiliary CSVs for operational convenience and human readability

**Deployment:**

- ✅ Code deployed to production (hf-timestd 4.0.0)
- ✅ Services restarted successfully
- ✅ Committed and pushed to GitHub (commit 233d6fd)
- ⏳ Awaiting test signal at minute :44 for HDF5 file verification

---

## Session Summary (2026-01-04 - Earlier)

### TEC Fix Implementation - COMPLETED ✅

Successfully implemented and deployed the fix for Total Electron Content (TEC) calculations:

**Problem Solved:**

- TEC estimators were receiving calibrated `clock_offset_ms` values that had ionospheric delays removed
- This eliminated the frequency-dependent dispersion signal needed for TEC estimation
- Result: Near-zero or NaN TEC values

**Solution Implemented:**

- Added `raw_arrival_time_ms` field to L2 timing measurements schema (v1.0.0 → v1.1.0)
- Modified Analytics Service to calculate and write uncalibrated ToA: `raw_arrival_time_ms = effective_d_clock + propagation_delay_ms`
- Updated Science Aggregator and Fusion Service to use `raw_arrival_time_ms` for TEC calculations
- Implemented backward compatibility fallback for older data

**Deployment Status:**

- ✅ Code deployed to production venv (`/opt/hf-timestd/venv/`)
- ✅ Services restarted and operational
- ✅ Field being written to HDF5 files in `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/`
- ✅ Verified 36+ measurements with `raw_arrival_time_ms` values (e.g., 22.761 ms, 111.359 ms)
- ⏳ Monitoring TEC output for non-zero values (2-50 TECU range expected)

**Key Lesson Learned:**
HDF5 files created with old schema cannot have new datasets added retroactively. Schema updates require file deletion/recreation or daily rotation to new files.

**Cleanup Needed:**

- Remove debug logging added during troubleshooting (search for "DEBUG TEC FIX" in codebase)

### Critical Analytics Fixes - COMPLETED ✅

Successfully implemented and deployed comprehensive validation and discrimination fixes to address the ~18ms D_clock spread between stations:

**Problems Identified:**

1. **Inter-station D_clock inconsistency** - Different stations showed D_clock values ranging from 6.3ms (CHU) to 23.9ms (WWVH), which is physically impossible since D_clock is a property of the receiver, not the station
2. **Missing propagation delay validation** - No bounds checking on calculated delays
3. **No ionospheric delay validation** - Corrupted IRI-2020 data could produce invalid delays
4. **No D_clock continuity checking** - Frame slips and mode errors went undetected
5. **Multi-station timing not extracted** - CorrelatorBank detected multiple stations but timing wasn't used for validation
6. **No cross-frequency guidance** - Strong detections on one frequency weren't helping weak channels

**Solutions Implemented:**

1. **Station-Specific Propagation Delay Validation** (`transmission_time_solver.py`)
   - Added physical bounds for each station: WWV (4-12ms), WWVH (15-30ms), CHU (6-15ms), BPM (40-70ms)
   - Modes with delays outside bounds have plausibility reduced by 70%
   - Prevents physically impossible propagation paths

2. **Ionospheric Delay Validation** (`transmission_time_solver.py`)
   - Validates 1/f² relationship for ionospheric delay
   - Per-hop, per-frequency maximum delay thresholds
   - Rejects negative or excessive ionospheric delays
   - Catches corrupted IRI-2020 model output

3. **Inter-Station D_clock Consistency Checking** (`phase2_temporal_engine.py`)
   - New method `_validate_inter_station_dclock_consistency()`
   - Validates D_clock spread < 5ms (CRITICAL threshold), < 3ms (WARNING threshold)
   - Logs detailed diagnostics when validation fails
   - Prevents bad data from reaching fusion

4. **D_clock Continuity Validation** (`phase2_temporal_engine.py`)
   - Tracks `_last_d_clock_ms` between consecutive minutes
   - Flags jumps > 5ms as discontinuities
   - Detects CHU frame slips (500ms jumps)
   - Reduces confidence for discontinuous measurements

5. **Multi-Station Timing Extraction** (`phase2_temporal_engine.py`)
   - Extracts timing from `multi_station_result.get_all_usable_detections()`
   - Populates `wwv_timing_ms`, `wwvh_timing_ms`, `chu_timing_ms` from CorrelatorBank
   - Enables inter-station validation when multiple stations detected
   - Currently executing (logs show "🔍 Multi-station detector found X usable detections")

6. **Cross-Frequency Guidance Integration** (`phase2_temporal_engine.py`)
   - Uses `get_cross_freq_guidance()` to find strong detections on other frequencies
   - Narrows search window from ±500ms to ±3-5ms when guidance available
   - Key insight: WWVH ToA across frequencies correlates tighter than WWV vs WWVH on same frequency
   - Improves station discrimination on shared channels

**Deployment Status:**

- ✅ All fixes implemented in git repository
- ✅ Deployed to production: `sudo cp src/hf_timestd/core/*.py /opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/`
- ✅ Services restarted at 18:40 UTC
- ✅ System now stable with D_clock centered at 0ms
- ✅ Fusion history shows tight tracking (±1-2ms uncertainty)
- ⏳ Waiting for multi-station detections to trigger inter-station validation

**Key Files Modified:**

- `src/hf_timestd/core/transmission_time_solver.py` - Propagation and ionospheric delay validation
- `src/hf_timestd/core/phase2_temporal_engine.py` - Inter-station validation, continuity, multi-station extraction, cross-frequency guidance

**Documentation Created:**

- `ANALYTICS_FIXES_DEPLOYED_2026-01-04.md` - Complete deployment summary
- `BOOTSTRAP_DISCRIMINATION_STRATEGY.md` - Comprehensive bootstrap and station discrimination documentation

### Bootstrap and Station Discrimination Strategy - DOCUMENTED ✅

**Key Architectural Insight:**

The system correctly implements a prioritized bootstrap strategy that uses a priori knowledge (geography, physics, propagation models) to guide detection and discrimination:

**Phase 1: Bootstrap from Anchor Channels**

Priority channels for unambiguous station identification:

```python
ANCHOR_CHANNELS = {
    'CHU 3.33 MHz',    # CHU-only frequency
    'CHU 7.85 MHz',    # CHU-only frequency  
    'CHU 14.67 MHz',   # CHU-only frequency
    'WWV 20 MHz',      # WWV-only frequency
    'WWV 25 MHz',      # WWV-only frequency
}
```

**Why anchor channels are optimal:**

- No station ambiguity (only one station broadcasts)
- Detection = certain station identification
- Provides clean RTP offset measurement
- Establishes preliminary D_clock for all channels

**Phase 2: Calibration (Narrow Search Windows)**

Once anchor channel provides D_clock:

- Adjust RTP expectations for all channels
- Narrow search windows from ±500ms to ±5ms
- Update continuously as more detections arrive

**Phase 3: Shared Channel Discrimination**

Tackle shared channels (2.5, 5, 10, 15 MHz) with hierarchy:

1. **Primary: ToA Separation** (with calibrated D_clock)
   - WWV: 8±2ms (short path from Colorado)
   - WWVH: 23±3ms (medium path from Hawaii)
   - BPM: 45±5ms (long path from China)
   - Separation: 15ms between WWV/WWVH, 22ms between WWVH/BPM

2. **Secondary: Acoustic Discrimination**
   - WWVH unique: 1200 Hz tone (strong indicator)
   - WWV/WWVH: 500/600 Hz tones (both have these)
   - BPM vs WWV: Both use 1000 Hz, discriminate by ToA and BCD pattern

3. **Tertiary: Cross-Frequency Correlation** (validation, not primary)
   - WWVH consistency: ToA at 2.5, 5, 10, 15 MHz should agree within ±3ms
   - WWV consistency: ToA at 2.5, 5, 10, 15 MHz should agree within ±3ms
   - Validation: If ToA varies >5ms across frequencies, suspect misidentification

**Why Cross-Frequency Alone Isn't Sufficient:**

- Frequency-dependent fading (10 MHz strong, 5 MHz weak)
- Mode changes (different frequencies use different propagation modes)
- Ionospheric disturbances (affect frequencies differently)
- Time of day (some frequencies unusable at certain times)

**Correct Hierarchy for Station Identification:**

1. **Primary:** Anchor channels (unambiguous)
2. **Secondary:** ToA separation (with calibrated D_clock)
3. **Tertiary:** Acoustic discrimination (1200 Hz, BCD, etc.)
4. **Quaternary:** Cross-frequency correlation (validation)

**System Implementation:**

The `timing_calibrator.py` module correctly implements this strategy with:

- `ANCHOR_CHANNELS` definition
- Bootstrap phase tracking (BOOTSTRAP → PROVISIONAL → CALIBRATED → VERIFIED)
- RTP offset calibration from anchor detections
- Search window adaptation (500ms → 5ms)

**Current System Status:**

- System is stable with D_clock = +0.00ms (recent measurements)
- Fusion tracking centered at 0ms with ±1-2ms uncertainty
- Earlier discontinuities (18:20-18:40) were due to service restarts during deployment
- All validation infrastructure is deployed and monitoring

---

## Next Session Priority: HDF5 Migration & CSV Removal

### Objective

Migrate test signal analysis code in the Science Aggregator to use HDF5 instead of CSV files, continuing the system-wide migration away from CSV-based data storage.

### Background

**Current State:**

- Most of the system has been migrated to HDF5 (L2 timing measurements, L3 fusion data)
- Science Aggregator still uses CSV for some products, particularly test signal analysis
- CSV files are less efficient, harder to query, and don't support concurrent access as well as HDF5

**Migration Goals:**

1. Convert test signal analysis to read/write HDF5
2. Remove remaining CSV usage throughout the codebase
3. Maintain backward compatibility during transition
4. Ensure schema versioning for future evolution

### Core Principles

1. **HDF5 for all persistent data** - CSV only for human-readable exports if needed
2. **Schema versioning** - All HDF5 files must have version metadata
3. **SWMR mode** - Enable Single-Writer-Multiple-Reader for concurrent access
4. **Atomic writes** - Use temp files and rename for crash safety

### Test Signal Analysis Overview

**Purpose:** Analyze test signals broadcast by WWV/WWVH to validate system performance and timing accuracy.

**Test Signals:**

- WWV: 440 Hz tone at 45 minutes past the hour (45:00-45:05)
- WWVH: 600 Hz tone at 45 minutes past the hour (45:00-45:05)
- Used for system validation and propagation analysis

**Current Implementation:**

- Located in `science_aggregator.py`
- Uses CSV files for storage (needs migration to HDF5)
- Analyzes test signal timing, SNR, and consistency

**HDF5 Schema Requirements:**

```python
# Test signal measurements schema
{
    "version": "1.0.0",
    "datasets": {
        "timestamp": "int64",           # Unix timestamp
        "station": "string",            # WWV, WWVH, CHU, BPM
        "frequency_mhz": "float32",     # Broadcast frequency
        "test_tone_hz": "float32",      # 440, 600, etc.
        "detected": "bool",             # Test signal detected
        "snr_db": "float32",            # Signal-to-noise ratio
        "timing_error_ms": "float32",   # Timing error vs expected
        "confidence": "float32",        # Detection confidence
        "notes": "string"               # Optional notes
    }
}
```

**Migration Strategy:**

1. Create new HDF5 schema for test signal data
2. Update Science Aggregator to write HDF5 instead of CSV
3. Add backward compatibility to read existing CSV files
4. Test with single channel before full deployment
5. Document schema and access patterns

### CSV Files to Migrate

**Identify remaining CSV usage:**

```bash
# Find CSV-related code
grep -r "\.csv" /home/mjh/git/hf-timestd/src/hf_timestd/ --include="*.py"
grep -r "csv\." /home/mjh/git/hf-timestd/src/hf_timestd/ --include="*.py"
```

**Known CSV usage locations:**

1. **Science Aggregator** (`science_aggregator.py`)
   - Test signal analysis
   - TEC estimation output (may already be migrated)
   - Propagation statistics

2. **Legacy Code** (check if still used)
   - Any remaining CSV exports
   - Backup/archive functionality

**Migration Checklist:**

- [ ] Identify all CSV read/write operations
- [ ] Design HDF5 schemas for each data type
- [ ] Implement HDF5 writers with schema versioning
- [ ] Add backward compatibility for existing CSV files
- [ ] Test with single channel/product
- [ ] Deploy to production
- [ ] Verify data integrity
- [ ] Remove CSV code after validation period

### HDF5 Best Practices (Learned from TEC Fix)

**Schema Evolution:**

- Existing HDF5 files cannot have new datasets added retroactively
- Increment schema version when adding fields
- Either delete old files or wait for daily rotation
- Test schema changes with a single channel first

**SWMR Mode:**

- Enable Single-Writer-Multiple-Reader for concurrent access
- Writer must open with `libver='latest'`, `swmr=True`
- Readers can attach to active files
- Flush after each write for readers to see updates

**Atomic Writes:**

- Write to temporary file first
- Use `os.rename()` for atomic replacement
- Prevents corruption if process crashes mid-write

**Error Handling:**

- Always close HDF5 files in finally blocks
- Check for file locks before writing
- Implement retry logic for transient failures
- Log all HDF5 operations for debugging

### Science Aggregator Code Locations

**Main File:** `/home/mjh/git/hf-timestd/src/hf_timestd/core/science_aggregator.py`

**Key Methods to Review:**

1. **Test Signal Analysis**
   - Look for methods processing 440 Hz (WWV) and 600 Hz (WWVH) test tones
   - Identify CSV write operations
   - Check data structures used

2. **Data Reading**
   - Methods that read L2 timing measurements
   - Methods that aggregate across channels
   - CSV vs HDF5 usage patterns

3. **Data Writing**
   - Output file paths and formats
   - Schema definitions (if any)
   - Error handling

**Related Files:**

- `src/hf_timestd/schemas/` - Schema definitions
- `src/hf_timestd/core/hdf5_io.py` - HDF5 utilities (if exists)
- `src/hf_timestd/core/timestd_paths.py` - Path management

### Example HDF5 Migration Pattern

**Before (CSV):**

```python
import csv

with open('test_signals.csv', 'a') as f:
    writer = csv.writer(f)
    writer.writerow([timestamp, station, snr_db, ...])
```

**After (HDF5):**

```python
import h5py
import numpy as np

# Open in SWMR mode
with h5py.File('test_signals.h5', 'a', libver='latest') as f:
    f.swmr_mode = True
    
    # Append to dataset
    dset = f['measurements']
    new_data = np.array([(timestamp, station, snr_db, ...)], 
                        dtype=dset.dtype)
    dset.resize((dset.shape[0] + 1,))
    dset[-1] = new_data
    f.flush()
```

**Schema Definition:**

```python
# In schemas/test_signals_schema.json
{
    "version": "1.0.0",
    "description": "Test signal analysis measurements",
    "datasets": {
        "measurements": {
            "dtype": [
                ("timestamp", "i8"),
                ("station", "S10"),
                ("frequency_mhz", "f4"),
                ("snr_db", "f4"),
                ("timing_error_ms", "f4"),
                ("confidence", "f4")
            ],
            "chunks": true,
            "compression": "gzip",
            "maxshape": (null,)
        }
    },
    "attributes": {
        "schema_version": "1.0.0",
        "created_by": "science_aggregator",
        "description": "WWV/WWVH test signal analysis"
    }
}
```

### Success Criteria for Next Session

1. ✅ Identify all CSV usage in Science Aggregator
2. ✅ Design HDF5 schemas for test signal analysis
3. ✅ Implement HDF5 writers with proper schema versioning
4. ✅ Add backward compatibility for existing CSV data
5. ✅ Test migration with single channel
6. ✅ Deploy to production and verify data integrity
7. ✅ Remove CSV code after validation period
8. ✅ Document HDF5 access patterns for future reference

---

## System Architecture Overview

### Data Processing Levels

- **L0 (Raw)**: Digital RF IQ samples from radiod (24 kHz, 16-bit)
- **L1 (Processed)**: Tone detections, BCD decoding, signal quality metrics
- **L2 (Calibrated)**: Station-assigned timing measurements with uncertainty budgets
- **L3 (Fused)**: Multi-station, multi-frequency fusion for optimal UTC estimate
- **L3C (Science)**: TEC, propagation statistics, ionospheric products

### Key Services

1. **timestd-core-recorder**: Receives IQ from radiod, writes Digital RF
2. **timestd-analytics**: Processes IQ → timing measurements (9 channels)
3. **timestd-fusion**: Fuses measurements → Chrony SHM updates
4. **timestd-science-aggregator**: Generates science products (TEC, propagation stats)
5. **timestd-vtec**: Downloads and processes GNSS VTEC data
6. **timestd-web-ui**: Monitoring dashboard

### Critical File Locations

- **Production Code**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/`
- **Data Root**: `/var/lib/timestd/`
- **Logs**: `/var/log/hf-timestd/`
- **Config**: `/etc/hf-timestd/timestd-config.toml`
- **Git Repository**: `/home/mjh/git/hf-timestd/` (source code, not used by production)

### HDF5 Data Locations

- **L2 Timing**: `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/{CHANNEL}_timing_measurements_YYYYMMDD.h5`
- **L3 Fusion**: `/var/lib/timestd/phase2/fusion/FUSED_timing_YYYYMMDD.h5`
- **Science Products**: `/var/lib/timestd/phase2/science/{PRODUCT}/`

---

## Important Notes for AI Agents

### Production Code Management

**CRITICAL**: Production services run from `/opt/hf-timestd/venv/`, NOT from the git repository.

**Method 1: Direct file copy (fastest for testing):**

```bash
cd /home/mjh/git/hf-timestd
sudo cp src/hf_timestd/core/{file}.py /opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/
sudo systemctl restart timestd-{service-name}
```

**Method 2: Full package install (for schema changes):**

```bash
cd /home/mjh/git/hf-timestd
sudo /opt/hf-timestd/venv/bin/pip install . --no-deps
sudo systemctl restart timestd-{service-name}
```

**Note:** Method 1 was used for recent analytics fixes deployment. Use Method 2 when schemas or package structure changes.

### HDF5 Schema Evolution

When updating HDF5 schemas:

1. Increment schema version in JSON file
2. Existing HDF5 files will NOT get new datasets automatically
3. Either delete old files or wait for daily rotation to new files
4. Test with a single channel before deploying to all channels

### Service Restart Best Practices

1. Stop service: `sudo systemctl stop timestd-{service}`
2. Clear Python cache if code changed: `sudo find /opt/hf-timestd/venv -name "*.pyc" -delete`
3. Reinstall package: `sudo /opt/hf-timestd/venv/bin/pip install /home/mjh/git/hf-timestd --no-deps`
4. Start service: `sudo systemctl start timestd-{service}`
5. Verify: `systemctl is-active timestd-{service}`

### Debugging Workflow

1. Check service status: `systemctl status timestd-{service}`
2. View recent logs: `sudo journalctl -u timestd-{service} -n 100 --no-pager`
3. Check channel-specific logs: `/var/log/hf-timestd/phase2-{channel}.log`
4. Verify HDF5 files: Check `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/` for active files
5. Monitor Chrony: `chronyc sources -v` to check TMGR source

---

## TEC Calculation Details (For Reference)

**Physics**: Ionospheric delay τ(f) ∝ TEC / f²

**Model**: T_obs(f) = T_vacuum + (40.3 · TEC) / f²

**Input Required**: Raw, uncalibrated time-of-arrival (ToA) that preserves frequency-dependent dispersion

**Current Implementation**:

- `raw_arrival_time_ms` = `effective_d_clock` + `propagation_delay_ms`
- This is the total observed arrival time before calibration removes ionospheric component
- TEC estimator performs linear regression: y = T_obs, x = 1/f², slope = 40.3 · TEC

**Note**: TEC calculations are **NOT** in the critical path to Chrony. They are science products only.

---

## Key Insights from This Session

### Bootstrap and Discrimination Strategy

**Critical Principle:** Use a priori knowledge (geography, physics, propagation models) to guide detection, not just acoustic features.

**Hierarchy for Station Identification:**

1. **Anchor channels** (CHU-only, WWV-only frequencies) - Unambiguous
2. **ToA separation** (with calibrated D_clock) - Primary discriminator on shared channels
3. **Acoustic features** (1200 Hz, BCD patterns) - Secondary validation
4. **Cross-frequency correlation** - Tertiary validation, not primary

**Why this works:**

- BPM vs WWV conflict resolved by 37ms ToA separation
- Propagation-robust (doesn't require all frequencies available)
- Rapid convergence (first anchor detection enables narrow windows everywhere)
- Multi-layer validation (inter-station + cross-frequency + acoustic)

### Validation Architecture

**Inter-Station D_clock Consistency:**

- D_clock is a property of the RECEIVER, not the station
- All stations should measure same D_clock (within measurement noise)
- Spread > 5ms indicates propagation delay calculation errors
- Deployed and monitoring, waiting for multi-station detections

**D_clock Continuity:**

- Tracks jumps between consecutive minutes
- Flags discontinuities > 5ms
- Detects CHU frame slips (500ms jumps)
- Currently active and monitoring

**Cross-Frequency Guidance:**

- Strong detection on one frequency helps weak channels
- Narrows search window from ±500ms to ±3-5ms
- Key insight: WWVH ToA across frequencies correlates tighter than WWV vs WWVH on same frequency
- Deployed and active

### System Stability

**Current Status (19:10 UTC):**

- D_clock centered at 0ms (recent measurements: +0.00ms)
- Fusion tracking well with ±1-2ms uncertainty
- No discontinuities since deployment at 18:40 UTC
- All validation infrastructure deployed and monitoring

**Earlier Issues (18:20-18:40):**

- Multiple discontinuities due to service restarts during deployment
- System was in bootstrap/transition mode
- Now resolved with stable tracking

## Questions for Next Session

1. Where is test signal analysis code in Science Aggregator?
2. What CSV files are currently being written?
3. What is the data structure for test signal measurements?
4. Are there existing HDF5 utilities we can leverage?
5. What is the daily data volume for test signals?
6. Should we keep CSV export capability for human readability?

---

## End of Context Document

This document should be updated at the end of each session to reflect current system state and priorities.
