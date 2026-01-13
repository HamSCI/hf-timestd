# Science Aggregator Service - HDF5 Migration Review & Science Data Inventory

**Date:** January 2, 2026  
**Status:** HDF5 Migration Complete with Bug Fix Required  
**Reviewer:** AI Assistant

---

## Executive Summary

The science-aggregator service has **partial HDF5 support** but contains a critical bug preventing it from reading timing measurements. The service successfully writes TEC estimates to HDF5 but was reading 0 measurements due to incorrect directory paths.

### Key Findings

1. ✅ **TEC Output:** Fully migrated to HDF5 with proper schema validation
2. ❌ **Input Reading:** Bug in directory path - reads from wrong location
3. ✅ **HDF5 Schema:** Well-designed L3A TEC schema with quality flags
4. ⚠️ **CSV Fallback:** Still active for both input and output (transition period)
5. 📊 **Science Data:** Limited to TEC estimation; event detection not implemented

---

## Bug Analysis: TEC Staleness Issue

### Root Cause

The science aggregator was reading **0 measurements** from all channels because:

**Line 137 (BEFORE FIX):**
```python
clock_offset_dir = self.paths.get_clock_offset_dir(channel_name)
# Returns: /var/lib/timestd/phase2/{CHANNEL}/clock_offset/
```

**Actual HDF5 Location:**
```
/var/lib/timestd/phase2/{CHANNEL}/{CHANNEL}_timing_measurements_{DATE}.h5
```

The HDF5 timing measurements are stored in the **channel root directory**, not the `clock_offset` subdirectory. The `clock_offset` subdirectory only contains legacy CSV files.

### Fix Applied

**File:** `src/hf_timestd/core/science_aggregator.py`

Changed from:
```python
clock_offset_dir = self.paths.get_clock_offset_dir(channel_name)
```

To:
```python
channel_dir = self.paths.get_phase2_dir(channel_name)
```

This aligns with how analytics writes the files (channel root) vs. where the aggregator was looking (clock_offset subdirectory).

### Verification

Tested HDF5 file access in SWMR mode:
```
File: /var/lib/timestd/phase2/CHU_3330/CHU_3330_timing_measurements_20260102.h5
Status: ✅ Readable in SWMR mode
Measurements: 942 records
Latest: 2026-01-02T18:37:00Z (CHU 3.33 MHz)
```

---

## HDF5 Migration Status

### ✅ Completed Components

#### 1. TEC Output (L3A)

**Writer:** `DataProductWriter` with schema validation  
**Schema:** `l3_tec_v1.json`  
**Location:** `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_{DATE}.h5`

**Schema Fields:**
- `timestamp_utc` (ISO 8601) - Required
- `minute_boundary` (Unix epoch) - Required
- `station` (WWV/WWVH/CHU/BPM) - Required
- `tec_tecu` (TECU units) - Required, no NaN
- `t_vacuum_error_ms` - Vacuum propagation time
- `confidence` (0-1) - Fit quality
- `n_frequencies` - Number of frequencies used
- `residuals_ms` - RMS residuals
- `frequencies_mhz` - Comma-separated list
- `quality_flag` (GOOD/MARGINAL/BAD/MISSING) - Required
- `processing_version` - Software version

**Quality Criteria:**
- **GOOD:** n_freq ≥ 4, confidence > 0.8, residuals < 1 ms
- **MARGINAL:** n_freq ≥ 3, 0.5 < confidence ≤ 0.8, residuals < 2 ms
- **BAD:** n_freq < 3, confidence ≤ 0.5, or residuals ≥ 2 ms

**Features:**
- ✅ SWMR mode enabled for concurrent reads
- ✅ Daily file rotation
- ✅ Schema validation with NaN/inf rejection
- ✅ ISO GUM uncertainty propagation
- ✅ Gzip compression (level 4)

#### 2. Input Reading (L2)

**Reader:** `DataProductReader` with SWMR support  
**Schema:** `l2_timing_measurements_v1.json`  
**Location:** `/var/lib/timestd/phase2/{CHANNEL}/{CHANNEL}_timing_measurements_{DATE}.h5`

**Features:**
- ✅ SWMR read mode for concurrent access
- ✅ Time range queries
- ✅ Quality filtering (grade, flag, confidence)
- ✅ Station filtering
- ✅ Race condition mitigation (minimum length across datasets)

### ⚠️ Transition Period

**CSV Fallback Active:**
- Input: Falls back to CSV if HDF5 read fails
- Output: Writes both HDF5 and CSV (lines 361-415)

**Recommendation:** Remove CSV output after confirming HDF5 stability (1-2 weeks).

---

## Science Data Inventory

### Currently Collected

#### 1. Total Electron Content (TEC)

**Source:** Multi-frequency timing measurements  
**Method:** Least-squares fit of group delay vs. 1/f²  
**Cadence:** ~60 seconds (when ≥2 frequencies available)  
**Stations:** WWV, WWVH, CHU, BPM

**Physics:**
```
τ(f) = K · TEC / f²    where K = 40.3 m³/s²
T_obs(f) = T_vacuum + τ(f)
```

**Derived Products:**
- TEC in TECU (10^16 electrons/m²)
- Vacuum propagation time (timing error without ionosphere)
- Per-frequency group delays
- Fit confidence and residuals

**Quality Metrics:**
- Confidence (R² of linear fit)
- RMS residuals
- Number of frequencies used

**Current Status:** ✅ Operational, writing to HDF5

#### 2. Group Delay Corrections

**Stored in TEC Results:**
- Per-frequency ionospheric delay
- Mapped to standard frequencies: 2.5, 5, 10, 15, 20, 25 MHz
- Used for vacuum timing correction

**Format:** Dictionary in `TECResult.group_delay_ms`

---

### Not Yet Collected (Opportunities)

#### 1. Event Detection (Placeholder Only)

**Code Location:** `science_aggregator.py:417-427`  
**Status:** 🚧 Not implemented (placeholder function)

**Potential Science Products:**

##### A. Traveling Ionospheric Disturbances (TIDs)
- **Detection:** Time series analysis of TEC variations
- **Signatures:** Periodic oscillations (15-60 min periods)
- **Sources:** Geomagnetic storms, auroral activity
- **Value:** Space weather monitoring

##### B. Spread-F Events
- **Detection:** Rapid TEC fluctuations + Doppler spread
- **Signatures:** High-frequency variability
- **Sources:** Equatorial plasma instabilities
- **Value:** HF communication prediction

##### C. Solar Flare Absorption
- **Detection:** Sudden TEC increases + signal strength drops
- **Signatures:** Daytime absorption events
- **Sources:** X-ray/EUV flux increases
- **Value:** Solar activity monitoring

##### D. Ionospheric Scintillation
- **Detection:** Amplitude/phase fluctuations
- **Signatures:** S4 index, phase variance
- **Sources:** Plasma irregularities
- **Value:** GNSS/HF link quality prediction

#### 2. Multi-Station Correlation

**Not Currently Implemented:**
- Cross-station TEC comparison
- Propagation path analysis
- Spatial gradients
- Ionospheric mapping

**Potential Value:**
- Regional ionospheric models
- Propagation prediction
- Station discrimination validation

#### 3. Long-Term Statistics

**Not Currently Implemented:**
- Diurnal TEC patterns
- Seasonal variations
- Solar cycle trends
- Geomagnetic storm responses

**Potential Value:**
- Climatological models
- Anomaly detection baselines
- System performance metrics

#### 4. Doppler Analysis

**Available but Not Aggregated:**
- Per-channel Doppler measurements exist in Phase 2
- Not currently used for science products
- Could detect:
  - Ionospheric motion
  - Gravity waves
  - Geomagnetic disturbances

**Location:** `/var/lib/timestd/phase2/{CHANNEL}/doppler/`

#### 5. Signal Quality Metrics

**Available but Not Aggregated:**
- Delay spread (multipath)
- Coherence time
- SNR variations
- Discrimination confidence

**Potential Science Use:**
- Channel characterization
- Propagation mode identification
- Space weather correlation

---

## Data Product Hierarchy

### Current Implementation

```
L0: Digital RF (HDF5)
  └─> L1: Channel Observables (HDF5)
       └─> L2: Timing Measurements (HDF5)
            └─> L3A: TEC Estimates (HDF5)
            └─> L3: Fusion Timing (HDF5)
                 └─> Chrony (SHM)

L3A: GNSS VTEC (HDF5) - Independent input
```

### Recommended Additions

```
L3B: Ionospheric Events (HDF5) - NEW
  - TID detections
  - Spread-F events
  - Solar flare absorption
  - Scintillation indices

L3C: Multi-Station Products (HDF5) - NEW
  - Cross-station TEC comparison
  - Spatial gradients
  - Propagation maps

L4: Long-Term Statistics (HDF5) - NEW
  - Daily/monthly aggregates
  - Climatological models
  - Anomaly detection
```

---

## Schema Review

### L3A TEC Schema (l3_tec_v1.json)

**Assessment:** ✅ Well-designed, production-ready

**Strengths:**
- Comprehensive metadata (station, timestamp, frequencies)
- Quality metrics (confidence, residuals, n_frequencies)
- Physical units clearly specified (TECU, milliseconds)
- Quality flags with clear criteria
- Standards compliance (CEDAR Madrigal, NetCDF CF)

**Recommendations:**

1. **Add Optional Fields:**
   ```json
   {
     "name": "solar_flux_f107",
     "type": "float",
     "required": false,
     "description": "Solar flux index (context for TEC levels)"
   },
   {
     "name": "geomagnetic_kp",
     "type": "float",
     "required": false,
     "description": "Geomagnetic Kp index (context for disturbances)"
   },
   {
     "name": "elevation_angle_deg",
     "type": "float",
     "required": false,
     "description": "Station elevation angle (for obliquity correction)"
   }
   ```

2. **Consider Adding:**
   - Ionospheric pierce point coordinates
   - Obliquity factor (slant vs. vertical TEC)
   - Data source flags (HDF5 vs. CSV input)

### Missing Schemas

**Recommended New Schemas:**

1. **L3B Ionospheric Events** (`l3b_iono_events_v1.json`)
   - Event type (TID, spread-F, absorption, scintillation)
   - Start/end timestamps
   - Severity/magnitude
   - Affected stations/frequencies
   - Detection confidence

2. **L3C Multi-Station Products** (`l3c_multi_station_v1.json`)
   - Station pair
   - TEC difference/gradient
   - Correlation coefficient
   - Spatial separation

3. **L4 Statistics** (`l4_statistics_v1.json`)
   - Aggregation period (daily, monthly)
   - Mean/median/std TEC
   - Event counts
   - Data quality metrics

---

## Code Quality Assessment

### Strengths

1. **HDF5 Integration:** Proper use of `DataProductWriter`/`DataProductReader`
2. **Schema Validation:** All writes validated against JSON schemas
3. **SWMR Support:** Concurrent read/write capability
4. **Error Handling:** Graceful fallback to CSV on HDF5 failures
5. **Path Management:** Uses `TimeStdPaths` for consistency
6. **Physics:** Correct TEC estimation algorithm (least-squares fit)

### Issues Fixed

1. ✅ **Directory Path Bug:** Fixed HDF5 read location (channel root vs. subdirectory)

### Remaining Issues

1. **CSV Dual-Write:** Still writing both HDF5 and CSV (line 361-415)
   - **Impact:** Disk I/O overhead, potential inconsistency
   - **Recommendation:** Remove after 1-2 weeks of HDF5 stability

2. **Event Detection Stub:** Placeholder function not implemented
   - **Impact:** Missing science value
   - **Recommendation:** Prioritize TID detection (highest value)

3. **No Multi-Station Analysis:** Each station processed independently
   - **Impact:** Missing spatial context
   - **Recommendation:** Add cross-station correlation

4. **Limited Metadata:** No solar/geomagnetic context in TEC records
   - **Impact:** Harder to interpret TEC variations
   - **Recommendation:** Add F10.7, Kp indices to schema

---

## Performance Characteristics

### Current Behavior

**Poll Interval:** 300 seconds (5 minutes)  
**Lookback Window:** 10 minutes  
**Processing Time:** ~2 seconds per cycle (9 channels)

**Observed (from logs):**
```
2026-01-02 18:32:16 - Aggregating TEC for 18:20:16 to 18:30:16
2026-01-02 18:32:18 - Grouped into 0 (station, timestamp) pairs
```

**After Fix (Expected):**
- Should group into 50-100 pairs per cycle (9 channels × 10 minutes)
- TEC files updated every 5 minutes
- ~10-20 TEC estimates per cycle (multi-frequency groups)

### Resource Usage

**Memory:** 134 MB (reasonable for Python service)  
**CPU:** 1 min 967ms over 18 hours (very low)  
**Disk I/O:** Minimal (HDF5 writes are buffered)

---

## Recommendations

### Immediate Actions (This Session)

1. ✅ **Fix Directory Bug:** Applied (channel root vs. clock_offset)
2. 🔄 **Test Fix:** Restart service and verify TEC updates
3. 📝 **Document Fix:** This review document

### Short-Term (Next 1-2 Weeks)

1. **Verify HDF5 Stability:**
   - Monitor TEC file updates (should be every 5 min)
   - Check for HDF5 read/write errors
   - Validate TEC estimates against expected values

2. **Remove CSV Dual-Write:**
   - After confirming HDF5 stability
   - Keep CSV fallback for reading (legacy data)
   - Update lines 361-415 in `science_aggregator.py`

3. **Add Monitoring:**
   - TEC update frequency
   - Number of measurements per cycle
   - HDF5 file sizes
   - Quality flag distribution

### Medium-Term (Next 1-3 Months)

1. **Implement Event Detection:**
   - Start with TID detection (highest value)
   - Create L3B schema for events
   - Add alerting for significant events

2. **Add Multi-Station Analysis:**
   - Cross-station TEC comparison
   - Spatial gradient calculation
   - Create L3C schema

3. **Enhance Metadata:**
   - Add solar flux (F10.7) to TEC records
   - Add geomagnetic indices (Kp, Ap)
   - Add elevation angles for obliquity correction

4. **Implement Statistics:**
   - Daily/monthly TEC aggregates
   - Baseline models for anomaly detection
   - Create L4 schema

### Long-Term (3-6 Months)

1. **Doppler Integration:**
   - Aggregate Doppler measurements
   - Correlate with TEC variations
   - Detect ionospheric motion

2. **Signal Quality Analysis:**
   - Aggregate delay spread, coherence time
   - Correlate with space weather
   - Propagation mode identification

3. **Visualization:**
   - Real-time TEC maps
   - Event timeline
   - Long-term trends

---

## Testing Plan

### Unit Tests Needed

1. **TEC Estimation:**
   - Test with known multi-frequency data
   - Verify physics (K = 40.3 m³/s²)
   - Test edge cases (2 frequencies, negative TEC)

2. **HDF5 I/O:**
   - Test SWMR read during write
   - Test daily file rotation
   - Test schema validation

3. **Quality Flags:**
   - Verify GOOD/MARGINAL/BAD criteria
   - Test edge cases

### Integration Tests Needed

1. **End-to-End Pipeline:**
   - Analytics → Science Aggregator → HDF5
   - Verify data flow
   - Check latency

2. **Multi-Channel:**
   - Test with all 9 channels
   - Verify station grouping
   - Check for race conditions

### Validation Tests Needed

1. **Physics Validation:**
   - Compare TEC with GNSS VTEC
   - Check diurnal patterns
   - Verify solar cycle correlation

2. **Quality Validation:**
   - Analyze quality flag distribution
   - Check confidence vs. residuals
   - Validate uncertainty estimates

---

## File Locations

### Source Code
- **Main Service:** `src/hf_timestd/core/science_aggregator.py`
- **TEC Estimator:** `src/hf_timestd/core/tec_estimator.py`
- **HDF5 Writer:** `src/hf_timestd/io/hdf5_writer.py`
- **HDF5 Reader:** `src/hf_timestd/io/hdf5_reader.py`
- **TEC Schema:** `src/hf_timestd/schemas/l3_tec_v1.json`

### Data Locations
- **TEC HDF5:** `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_{DATE}.h5`
- **TEC CSV:** `/var/lib/timestd/phase2/science/tec/tec_{DATE}.csv` (fallback)
- **Input HDF5:** `/var/lib/timestd/phase2/{CHANNEL}/{CHANNEL}_timing_measurements_{DATE}.h5`
- **Input CSV:** `/var/lib/timestd/phase2/{CHANNEL}/clock_offset/*_clock_offset_{DATE}.csv` (legacy)

### Service
- **Systemd Unit:** `/etc/systemd/system/timestd-science-aggregator.service`
- **Logs:** `sudo journalctl -u timestd-science-aggregator`

---

## Conclusion

The science-aggregator service has **successfully migrated to HDF5** for TEC output with proper schema validation, SWMR support, and quality metrics. However, a **critical bug in the input path** prevented it from reading timing measurements, causing TEC staleness.

**Bug Fixed:** Changed from `get_clock_offset_dir()` to `get_phase2_dir()` to read HDF5 files from correct location.

**Next Steps:**
1. Restart service to apply fix
2. Monitor TEC updates (should be every 5 min)
3. Remove CSV dual-write after stability confirmation
4. Implement event detection for additional science value

**Science Data Opportunities:**
- Event detection (TIDs, spread-F, absorption)
- Multi-station correlation
- Long-term statistics
- Doppler integration
- Signal quality analysis

The HDF5 infrastructure is solid and ready for expansion into additional science products.
