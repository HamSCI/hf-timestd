# Session Summary: Dual Chrony Feed Implementation
**Date**: 2026-01-14  
**Objective**: Deploy dual Chrony feed architecture (TSL1 and TSL2) for L1 vs L2 timing comparison

## Accomplishments

### 1. Fixed L2 Calibration Service Integration
- **Issue**: Fusion service couldn't read L2 calibrated timing data
- **Root Cause**: Path mismatch - fusion looking for `physics_interpretation` but L2 writing `timing_measurements`
- **Fix**: Updated fusion to read from `clock_offset/timing_measurements` directories
- **Fix**: Corrected field name from `station_id` to `station` in L2 data parsing

### 2. Implemented Separate L1 and L2 Fusion Paths
- **Objective**: Compare raw L1 metrology vs L2 calibrated timing for research
- **Implementation**:
  - Added `force_l1_only` parameter to `fuse()` method
  - Run fusion twice per cycle: once for L1-only, once for L2-calibrated
  - TSL1 (SHM unit 0): Uses only L1 metrology data with geometric approximation
  - TSL2 (SHM unit 1): Uses L2 calibrated propagation delays when available
- **Files Modified**:
  - `/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py`

### 3. Dual Chrony SHM Feed Architecture Operational
- **TSL1 Feed**: Raw L1 metrology fusion (precision -10)
- **TSL2 Feed**: L2 calibrated fusion (precision -11)
- **Status**: Both feeds active and updating Chrony
- **Current Offsets**: TSL1=-445us, TSL2=-437us (8us difference)

## Key Code Changes

### multi_broadcast_fusion.py

**1. Dual fusion execution (lines 3353-3375)**:
```python
# L1-only fusion: Force use of raw L1 metrology only
result_l1 = fusion.fuse(lookback_minutes=lookback_minutes, force_l1_only=True)

# L2 fusion: Use L2 calibrated data (current behavior)
result_l2 = fusion.fuse(lookback_minutes=lookback_minutes, force_l1_only=False)
```

**2. Separate SHM updates (lines 3491-3522)**:
```python
# Update L1 feed (SHM 0) - raw L1 metrology fusion only
if chrony_shm_l1 and result_l1:
    reference_time_l1 = system_time - (result_l1.d_clock_fused_ms / 1000.0)
    # ... precision -10

# Update L2 feed (SHM 1) - calibrated L2 timing fusion
if chrony_shm_l2 and result_l2:
    reference_time_l2 = system_time - (result_l2.d_clock_fused_ms / 1000.0)
    # ... precision -11
```

**3. L1-only mode in data reading (lines 1494-1505)**:
```python
if force_l1_only:
    l2_map = {}  # Skip L2 data in L1-only mode
else:
    l2_map = self._read_l2_physics(lookback_minutes)
```

**4. L2 data path fix (lines 1441-1452)**:
```python
# L2 calibrated timing outputs are in clock_offset directory
channel_dir = self.phase2_dir / channel / "clock_offset"
reader = DataProductReader(
    data_dir=channel_dir,
    product_level='L2',
    product_name='timing_measurements',
    channel=channel
)
```

## Current System Status

### Services Running
- ✅ `timestd-metrology.service` - L1 metrology measurements
- ✅ `timestd-l2-calibration.service` - L2 calibrated timing
- ✅ `timestd-fusion.service` - Dual feed fusion
- ✅ `chronyd.service` - Receiving both TSL1 and TSL2 feeds

### Chrony Feed Status
```
#- TSL1    0   4   252    41   -445us[-445us] +/-   51ms
#- TSL2    0   4   252    41   -437us[-437us] +/-   51ms
```

Both feeds operational but showing similar offsets due to limited L2 coverage.

## Discovered Issues

### 1. Limited L2 Calibration Coverage
- **Symptom**: TSL1 and TSL2 show nearly identical offsets
- **Cause**: L2 calibration service has limited data to process
- **Impact**: L2 feed falls back to L1 geometric approximation for most broadcasts

### 2. Low L1 Detection Rate on 15 MHz
- **Evidence**: 
  - 15 MHz metrology file only 61K today vs 344K yesterday
  - L2 calibration processing only 1 measurement occasionally for SHARED_15000
  - Strong SNR (23.6dB) but few detections being written
- **Impact**: Insufficient L1 data → insufficient L2 calibrations → both feeds use same fallback

### 3. File Ownership Issues
- Some metrology files owned by `root` instead of `timestd`
- May indicate permission problems in metrology service

## Fallback Behavior

When L2 calibrated data is unavailable, fusion uses:
```
D_clock = Raw_TOA - (geometric_light_time + 1.5ms)
```

This 1.5ms is a rough ionospheric delay estimate. With limited L2 coverage:
- **TSL1**: Always uses this approximation
- **TSL2**: Falls back to this approximation when L2 data missing
- **Result**: Both feeds show similar values

## Expected Evolution

As L2 calibration accumulates more data:
1. Greater offset difference between TSL1 and TSL2
2. Better TSL2 precision (lower uncertainty)
3. Chrony preference for TSL2 due to better statistics
4. Visible impact of ionospheric calibration corrections

## Files Modified This Session

1. `/home/mjh/git/hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py`
   - Added `force_l1_only` parameter to `fuse()` method
   - Implemented dual fusion execution (L1-only and L2-calibrated)
   - Fixed L2 data reading path (clock_offset directory)
   - Fixed L2 field name (station vs station_id)
   - Updated SHM feeds to use separate fusion results

## Next Session Priority

**Critical Issue**: Investigate why L1 metrology detection rate dropped significantly on 15 MHz despite strong signals (23.6dB SNR).

Potential areas to investigate:
- Tone detector thresholds and sensitivity
- SNR calculation accuracy
- Quality filtering criteria
- Raw IQ data integrity on 15 MHz channel
- Detection algorithm performance on different frequencies
