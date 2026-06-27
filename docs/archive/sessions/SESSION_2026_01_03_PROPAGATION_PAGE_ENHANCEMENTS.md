# Session 2026-01-03: Propagation Page Enhancements

**Date:** January 3, 2026  
**Focus:** Enhanced web-api propagation page with improved TEC display and fixed HDF5 data pipeline integration

## Summary

Enhanced the ionospheric/propagation page of the web-api to better display pairwise TEC (Total Electron Content) calculations. Fixed critical issue where science aggregator service was running old code that couldn't read HDF5 timing measurement files, resulting in zero TEC calculations.

## Changes Made

### 1. Fixed Import Error in Test Signal Module

**File:** `src/hf_timestd/core/wwv_test_signal.py`

**Issue:** Missing `List` type import causing NameError on service startup.

**Fix:** Added `List` to typing imports:
```python
from typing import Tuple, Optional, Dict, List
```

**Impact:** Resolved server crash on reload, allowing web-api to start properly.

---

### 2. Enhanced TEC Display in Propagation Page

**File:** `web-api/static/propagation.html`

**Changes:**

#### Added More Time Range Options
- Expanded from 2 options (6h, 24h) to 4 options (6h, 24h, 3d, 7d)
- Changed default from 6h to 7d to show available historical data
- Users can now view longer-term TEC trends

#### Enhanced Quality Summary Section
Replaced simple quality metrics with detailed per-path information:
- Measurement counts per station
- Mean TEC with min/max range in TECU
- Quality percentage (color-coded: green ≥80%, yellow ≥50%, red <50%)
- Mean timing uncertainty propagated to TEC uncertainty
- Explanatory text emphasizing pairwise TEC from multi-frequency dispersion

#### Added Per-Station Breakdown Cards
New detailed visualization showing for each propagation path (WWV, WWVH, CHU, BPM):
- Total measurements count
- Mean TEC value with range
- Mean uncertainty (±ms)
- Quality metrics with color indicators
- Frequency usage distribution (e.g., "6 freqs: 120×, 4 freqs: 45×")
- Educational note about ionospheric dispersion

**Example Output:**
```
WWV Propagation Path
Total Measurements: 692
Mean TEC: 0.00 TECU
TEC Range: -0.00 – 0.00 TECU
Mean Uncertainty: ±8.953 ms
Quality: 0% (good quality)
Frequency Usage: 6 freqs: 120×, 4 freqs: 45×, 3 freqs: 89×
```

---

### 3. Removed Test Signal Section (Temporarily)

**Reason:** User requested to hold off on test signal display until HDF5 storage implementation is complete (currently using CSV).

**Removed:**
- Test signal plot section
- Test signal summary display
- Test signal metrics cards
- JavaScript functions: `loadTestSignalData()`
- Test signal time range buttons and handlers

**Backend Preserved:**
- `PropagationService.get_test_signal_summary()` method remains
- `/api/propagation/test-signals` endpoint remains
- Can be re-added once HDF5 storage is implemented

---

### 4. Fixed Science Aggregator HDF5 Integration

**Critical Issue Discovered:**

The science aggregator service was reporting "Grouped into 0 (station, timestamp) pairs" despite HDF5 timing measurement files containing valid multi-frequency data.

**Root Cause:**
- Science aggregator service (PID 979873) started: Jan 2, 21:15 UTC
- HDF5-enabled code deployed: Jan 3, 11:58 UTC  
- Running service was using old code that only read CSV files
- CSV files don't exist for recent data (everything is in HDF5)
- Result: No measurements found, no TEC calculations

**Investigation Process:**
1. Verified HDF5 files exist with 35MB of data for today
2. Confirmed code has `DataProductReader` for HDF5 support
3. Tested HDF5 reading manually - worked perfectly (39 measurements, 10 multi-frequency groups)
4. Identified service was running pre-HDF5 code loaded in memory
5. Confirmed installed package was updated but service not restarted

**Solution:**
```bash
sudo systemctl restart timestd-science-aggregator.service
```

**Result After Restart:**
- ✅ Reading from HDF5 timing measurement files successfully
- ✅ Collecting 529 measurements per hour from all frequency channels
- ✅ Creating 24 TEC calculations per aggregation cycle
- ✅ Writing 14 propagation statistics records
- ✅ Data pipeline fully operational with HDF5

**Log Evidence:**
```
INFO - Collected 39 measurements from 4 channels
INFO - Grouped into 12 (station, timestamp) pairs
INFO - TEC: WWV @ 1767480300: 0.00 TECU (n_freq=2, conf=0.00)
INFO - Wrote 24 TEC results to HDF5
INFO - Collected 529 measurements for propagation stats
```

---

## Current Status

### ✅ Working
- Web-api propagation page displays fresh TEC data
- Science aggregator reading HDF5 timing measurements
- Multi-frequency grouping operational (2-4 frequencies per minute)
- TEC calculations running and writing to HDF5
- Propagation statistics aggregation working

### ⚠️ Known Issue - TEC Values Near Zero

**Observation:** All TEC calculations are producing ~0.00 TECU with BAD/MARGINAL quality and low confidence (0.00-0.10).

**Data Available:**
- 78.7% of measurements have 2+ frequencies (sufficient for TEC calculation)
- Multi-frequency data: 2.5, 5, 10, 15, 20, 25 MHz
- Clock offset measurements being recorded
- Timing measurements have valid propagation delays

**Possible Causes:**
1. **Nighttime ionosphere:** Low TEC during nighttime hours (session was ~22:00-23:00 UTC)
2. **TEC estimator calibration:** Algorithm may need adjustment
3. **Clock offset interpretation:** Ionospheric dispersion not being extracted correctly from measurements
4. **Frequency-dependent delays:** Insufficient differential delay between frequencies

**Next Session Goal:** Debug TEC calculation algorithm to determine why values are zero.

---

## Files Modified

1. `src/hf_timestd/core/wwv_test_signal.py` - Added List import
2. `web-api/static/propagation.html` - Enhanced TEC display, removed test signal section
3. Service restart: `timestd-science-aggregator.service`

## Files to Review for Next Session

### TEC Calculation Pipeline
1. `src/hf_timestd/core/tec_estimator.py` - Core TEC calculation algorithm
2. `src/hf_timestd/core/science_aggregator.py` - Data collection and grouping
3. `src/hf_timestd/core/multi_broadcast_fusion.py` - Clock offset measurements

### Data Files
1. `/var/lib/timestd/phase2/SHARED_*/SHARED_*_timing_measurements_20260103.h5` - Input data
2. `/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_20260103.h5` - Output TEC data
3. Clock offset values in timing measurements

### Key Questions for Next Session
1. Are clock offset measurements showing ionospheric dispersion?
2. Is the TEC estimator correctly extracting frequency-dependent delays?
3. What are the actual group delay values being used in TEC calculation?
4. Is the linear regression fit failing due to insufficient frequency separation?
5. Should we validate against GPS VTEC data?

---

## Technical Notes

### TEC Calculation Requirements
- Requires ≥2 frequencies per station per minute
- Uses ionospheric dispersion: lower frequencies delayed more than higher frequencies
- Formula: TEC derived from differential group delay across frequencies
- Quality depends on: number of frequencies, frequency separation, measurement uncertainty

### Data Pipeline Flow
```
Timing Measurements (HDF5)
  ↓ (per channel: SHARED_2500, SHARED_5000, etc.)
Science Aggregator
  ↓ (groups by station + minute)
TEC Estimator
  ↓ (calculates from multi-frequency dispersion)
AGGREGATED_tec_*.h5
  ↓
Web API PropagationService
  ↓
Propagation Page Display
```

### HDF5 Schema
**L2 Timing Measurements:**
- `clock_offset_ms` - D_clock measurement
- `frequency_mhz` - Broadcast frequency
- `station` - WWV, WWVH, CHU, BPM
- `minute_boundary_utc` - Grouping key
- `propagation_delay_ms` - Total path delay
- `uncertainty_ms` - Measurement quality

**L3 TEC Data:**
- `tec_tecu` - Total Electron Content in TECU
- `t_vacuum_error_ms` - Timing uncertainty
- `confidence` - Fit quality (0.0-1.0)
- `quality_flag` - GOOD/MARGINAL/BAD
- `n_frequencies` - Number of frequencies used
- `group_delay_ms` - Per-frequency ionospheric delay

---

## Lessons Learned

1. **Service Restarts Required:** After deploying code changes to installed packages, systemd services must be restarted to load new code. Python bytecode caching is not the issue - it's the in-memory loaded modules.

2. **Debug Logging Levels:** HDF5 read failures were only logged at DEBUG level, making them invisible when service runs at INFO level. Consider promoting critical fallback warnings to INFO or WARNING level.

3. **Data Format Transitions:** When transitioning from CSV to HDF5, ensure all services are updated and restarted. Old code silently falls back to CSV, appearing to work but finding no data.

4. **Multi-Frequency Requirements:** TEC calculation is fundamentally different from single-frequency timing. Requires simultaneous measurements across multiple frequencies, which necessitates aggregating data from multiple channel recorders.

5. **Default Time Ranges:** When displaying historical data, default time ranges should be set to show available data rather than recent empty periods.
