# TEC Data Fix Summary - 2026-01-05

## Problem Identified

The propagation.html page shows extremely small TEC values (e-08 to e-06 TECU instead of 2-50 TECU) because the `raw_arrival_time_ms` field is missing from HDF5 files.

## Root Cause

1. **Schema Update Timing**: Schema v1.1.0 added `raw_arrival_time_ms` field on 2026-01-04
2. **HDF5 File Creation**: Files created at midnight 2026-01-05 00:00 UTC were initialized **before** the schema update was fully deployed
3. **SWMR Mode Limitation**: HDF5 files in SWMR mode cannot have new datasets added after initialization
4. **Result**: The `raw_arrival_time_ms` dataset doesn't exist in current files, so TEC calculations fall back to using `clock_offset_ms` which has ionospheric delays already removed

## What `raw_arrival_time_ms` Is

- **Purpose**: Uncalibrated time-of-arrival that preserves frequency-dependent ionospheric dispersion
- **Formula**: `raw_arrival_time_ms = effective_d_clock + propagation_delay_ms`
- **Critical for TEC**: TEC estimation requires the raw dispersion signal across frequencies
- **Schema**: Defined in `l2_timing_measurements_v1.json` as optional float field

## Verification

```bash
# Check if field exists in HDF5 file
python3 -c "
import h5py
with h5py.File('/var/lib/timestd/phase2/CHU_3330/CHU_3330_timing_measurements_20260104.h5', 'r') as f:
    print('raw_arrival_time_ms' in f)  # Returns False
"
```

## Fixes Deployed

### 1. Science Aggregator Fix (DEPLOYED)
**File**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/science_aggregator.py`

**Change**: Added `raw_arrival_time_ms` to HDF5 data conversion (lines 188-191)

```python
# Include raw_arrival_time_ms if present (schema v1.1.0+)
# This is critical for TEC estimation
if 'raw_arrival_time_ms' in m and m['raw_arrival_time_ms'] is not None:
    meas_dict['raw_arrival_time_ms'] = str(m['raw_arrival_time_ms'])
```

**Status**: ✅ Deployed and service restarted

### 2. Analytics Service (ALREADY DEPLOYED)
**File**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/phase2_analytics_service.py`

**Status**: ✅ Already writing `raw_arrival_time_ms` to L2 models (line 762)

### 3. HDF5 Writer (ALREADY DEPLOYED)
**File**: `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/io/hdf5_writer.py`

**Status**: ✅ Properly initializes all schema fields including `raw_arrival_time_ms`

## Solution Timeline

### Immediate (Today - 2026-01-05)
- ✅ Science aggregator fix deployed
- ⏳ Current HDF5 files still missing `raw_arrival_time_ms` dataset
- ⏳ TEC calculations will continue using fallback (poor quality)

### Tomorrow (2026-01-06 00:00 UTC)
- ✅ New HDF5 files will be created with schema v1.1.0
- ✅ `raw_arrival_time_ms` dataset will be properly initialized
- ✅ Analytics service will write correct values
- ✅ TEC calculations will use proper raw arrival times
- ✅ TEC values should be in 2-50 TECU range

### Verification Steps (After 2026-01-06 00:00 UTC)

```bash
# 1. Check new file has the field
python3 -c "
import h5py
with h5py.File('/var/lib/timestd/phase2/CHU_3330/CHU_3330_timing_measurements_20260106.h5', 'r') as f:
    print('✓ raw_arrival_time_ms exists:', 'raw_arrival_time_ms' in f)
    if 'raw_arrival_time_ms' in f:
        print('  Sample values:', f['raw_arrival_time_ms'][:5])
"

# 2. Check TEC values are realistic
tail -20 /var/lib/timestd/phase2/science/tec/tec_20260106.csv

# 3. Check propagation.html displays correct TEC
# Open http://bee1:8000/static/propagation.html
# Look for TEC values in 2-50 TECU range
```

## Manual File Rotation (Optional - Not Recommended)

If you need TEC data immediately, you can manually rotate files:

```bash
# WARNING: This will lose data from today
# Only do this if you understand the consequences

# Stop analytics service
sudo systemctl stop timestd-analytics

# Move today's files to backup
sudo mkdir -p /var/lib/timestd/backup/20260105
sudo mv /var/lib/timestd/phase2/*/*_20260105.h5 /var/lib/timestd/backup/20260105/

# Restart analytics service (will create new files)
sudo systemctl start timestd-analytics

# Verify new files have raw_arrival_time_ms
# (wait 1 minute for data to be written)
```

**Recommendation**: Wait for natural file rotation at midnight UTC.

## Expected Results After Fix

### TEC Values
- **Before**: 1.2e-08 TECU (incorrect)
- **After**: 15-35 TECU (realistic for daytime ionosphere)

### Quality Flags
- **Before**: Most measurements marked as "BAD" or "MARGINAL"
- **After**: More "GOOD" quality measurements with confidence > 0.8

### Propagation.html Display
- **Before**: "No TEC data available" or extremely small values
- **After**: Proper TEC time series with error bars, color-coded by quality

## Technical Details

### TEC Calculation Physics
```
Ionospheric delay: τ(f) = (40.3 × TEC) / f²

Observed arrival time: T_obs(f) = T_vacuum + τ(f)

Linear regression:
  y = T_obs(f)
  x = 1/f²
  slope = 40.3 × TEC
  intercept = T_vacuum
```

### Why clock_offset_ms Doesn't Work
- `clock_offset_ms` = `effective_d_clock` (already has propagation delays removed)
- Ionospheric dispersion signal is lost
- TEC calculation gets near-zero slope → near-zero TEC

### Why raw_arrival_time_ms Works
- `raw_arrival_time_ms` = `effective_d_clock + propagation_delay_ms`
- Preserves frequency-dependent dispersion
- TEC calculation gets proper slope → realistic TEC values

## Files Modified

### Git Repository
- ✅ `/home/mjh/git/hf-timestd/src/hf_timestd/core/science_aggregator.py`

### Production
- ✅ `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/science_aggregator.py`

## Service Status

```bash
# Check service is running
sudo systemctl status timestd-science-aggregator

# Check recent logs
sudo journalctl -u timestd-science-aggregator -n 50 --no-pager
```

## Next Steps

1. ⏳ Wait for midnight UTC (2026-01-06 00:00) for file rotation
2. ✅ Verify new files have `raw_arrival_time_ms` dataset
3. ✅ Monitor TEC values in CSV files and HDF5
4. ✅ Check propagation.html displays correct data
5. ✅ Update CONTEXT.md with resolution

## Contact

If TEC values are still incorrect after 2026-01-06 00:00 UTC, check:
1. Analytics service logs for `raw_arrival_time_ms` values
2. HDF5 files have the dataset initialized
3. Science aggregator is reading the field correctly
4. TEC estimator is receiving non-zero input values
