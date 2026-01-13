# Station Discrimination Fix - Implementation Summary

**Date:** January 3, 2026  
**Status:** ✅ Complete - Ready for Testing  
**Priority:** HIGH

## Problem Statement

The analytics service was performing BCD discrimination on **all channels**, including station-specific frequencies where the station is known a priori. This caused:

1. **Invalid WWVH detections at 20/25 MHz** (WWVH doesn't broadcast there)
2. **Unnecessary CPU usage** on 5 of 9 channels
3. **Data quality issues** with physically impossible station/frequency combinations

## Solution Implemented

### 1. Added Broadcast Schedule Constants (`wwv_constants.py`)

Added frequency-to-station mappings to establish single source of truth:

```python
# Valid station/frequency combinations (MHz)
WWV_FREQUENCIES = [2.5, 5.0, 10.0, 15.0, 20.0, 25.0]
WWVH_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]  # NOT 20/25 MHz
CHU_FREQUENCIES = [3.33, 7.85, 14.67]
BPM_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]

# Shared frequencies requiring discrimination (WWV vs WWVH vs BPM)
SHARED_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]

# Station-specific frequencies (no discrimination needed)
STATION_SPECIFIC_FREQ = {
    20.0: 'WWV',
    25.0: 'WWV',
    3.33: 'CHU',
    7.85: 'CHU',
    14.67: 'CHU'
}
```

### 2. Modified Analytics Service (`phase2_analytics_service.py`)

Added helper methods to check if discrimination is needed:

```python
def _get_frequency_mhz(self) -> float:
    """Get channel frequency in MHz."""
    return self.frequency_hz / 1_000_000

def _should_discriminate(self) -> bool:
    """Check if this frequency requires BCD discrimination."""
    from .wwv_constants import SHARED_FREQUENCIES
    freq_mhz = self._get_frequency_mhz()
    return freq_mhz in SHARED_FREQUENCIES

def _get_station_from_frequency(self) -> Optional[str]:
    """Get station name from frequency for station-specific frequencies."""
    from .wwv_constants import STATION_SPECIFIC_FREQ
    freq_mhz = self._get_frequency_mhz()
    return STATION_SPECIFIC_FREQ.get(freq_mhz)
```

Updated `_write_bcd_discrimination()` to:
- Skip BCD discrimination on station-specific frequencies
- Directly label station from frequency (20/25 MHz → WWV, 3.33/7.85/14.67 MHz → CHU)
- Write high-confidence (1.0) L1B measurements for station-specific frequencies
- Add validation to reject WWVH detections at invalid frequencies

### 3. Modified Temporal Engine (`phase2_temporal_engine.py`)

Updated Step 2A (BCD Correlation) to skip discrimination on station-specific frequencies:

```python
# Skip BCD discrimination on station-specific frequencies
from .wwv_constants import STATION_SPECIFIC_FREQ
is_station_specific = self.frequency_mhz in STATION_SPECIFIC_FREQ

if is_station_specific:
    # Direct station labeling for station-specific frequencies
    station_name = STATION_SPECIFIC_FREQ[self.frequency_mhz]
    logger.debug(f"Skipping BCD discrimination for {station_name}-specific frequency {self.frequency_mhz} MHz")
    # Set high-confidence single-station result
    if station_name == 'WWV':
        result.bcd_wwv_amplitude = 1.0
        result.bcd_wwvh_amplitude = 0.0
    elif station_name == 'CHU':
        result.bcd_wwv_amplitude = 0.0
        result.bcd_wwvh_amplitude = 0.0
    result.bcd_correlation_quality = 1.0
elif (not is_bpm_pure_carrier and ...):
    # Perform normal BCD discrimination for shared frequencies
```

### 4. Created Validation Script

`scripts/validate_station_discrimination.py` checks:
- No WWVH detections at 20/25 MHz
- CHU channels labeled correctly
- Shared frequencies still perform discrimination
- Per-channel station distribution statistics

## Files Modified

1. `src/hf_timestd/core/wwv_constants.py` - Added broadcast schedule constants
2. `src/hf_timestd/core/phase2_analytics_service.py` - Added frequency-aware discrimination logic
3. `src/hf_timestd/core/phase2_temporal_engine.py` - Skip BCD on station-specific frequencies
4. `scripts/validate_station_discrimination.py` - Validation script (NEW)

## Expected Behavior After Fix

### Station-Specific Frequencies (No Discrimination)
- **20 MHz, 25 MHz:** All measurements labeled as WWV (confidence = 1.0)
- **3.33 MHz, 7.85 MHz, 14.67 MHz:** All measurements labeled as CHU (confidence = 1.0)
- **CPU savings:** ~40% reduction in BCD correlation processing (5 of 9 channels skip it)

### Shared Frequencies (Discrimination Required)
- **2.5 MHz, 5 MHz, 10 MHz, 15 MHz:** Continue performing BCD discrimination
- **Validation:** Reject any WWVH detections at frequencies outside [2.5, 5, 10, 15] MHz

## Testing Instructions

### 1. Validate Current Data (Before Restart)

Check for existing invalid combinations in recent data:

```bash
python3 scripts/validate_station_discrimination.py --hours 24
```

Expected output: Will show any invalid WWVH @ 20/25 MHz detections in existing data.

### 2. Restart Analytics Service

```bash
sudo systemctl restart timestd-analytics
```

### 3. Monitor Service Logs

```bash
sudo journalctl -u timestd-analytics -f
```

Look for:
- `"Skipping BCD discrimination for WWV-specific frequency 20.0 MHz"`
- `"Skipping BCD discrimination for CHU-specific frequency 3.33 MHz"`
- No errors or warnings about invalid station/frequency combinations

### 4. Validate New Data (After Restart)

Wait 1 hour for new data, then run validation:

```bash
python3 scripts/validate_station_discrimination.py --hours 1
```

Expected output:
```
✅ VALIDATION PASSED: All station/frequency combinations are valid!

Station-Specific Frequency Check:
  ✅ 20.0 MHz: All measurements labeled as WWV
  ✅ 25.0 MHz: All measurements labeled as WWV
  ✅ 3.33 MHz: All measurements labeled as CHU
  ✅ 7.85 MHz: All measurements labeled as CHU
  ✅ 14.67 MHz: All measurements labeled as CHU
```

### 5. Check L2 HDF5 Data

Verify station labels in recent L2 timing measurements:

```bash
python3 -c "
from hf_timestd.io.hdf5_reader import DataProductReader
from pathlib import Path
from datetime import datetime, timedelta

# Check WWV 20 MHz channel
reader = DataProductReader(
    data_dir=Path('/var/lib/timestd/phase2/WWV_20000'),
    product_level='L2',
    product_name='timing_measurements',
    channel='WWV_20000'
)

end = datetime.utcnow()
start = end - timedelta(hours=1)
measurements = reader.read_time_range(start.isoformat()+'Z', end.isoformat()+'Z')

print(f'WWV 20 MHz: {len(measurements)} measurements')
stations = [m.get('station') for m in measurements]
print(f'  WWV: {stations.count(\"WWV\")}')
print(f'  WWVH: {stations.count(\"WWVH\")} (should be 0)')
print(f'  Other: {len(stations) - stations.count(\"WWV\") - stations.count(\"WWVH\")}')
"
```

## Success Criteria

✅ **Primary Goals:**
1. Zero WWVH detections at 20 MHz or 25 MHz
2. All CHU channel measurements labeled as CHU
3. Shared frequencies (2.5, 5, 10, 15 MHz) continue to discriminate between WWV/WWVH/BPM

✅ **Secondary Goals:**
1. ~40% reduction in BCD correlation CPU usage (5 of 9 channels skip it)
2. Improved data quality and scientific validity
3. No impact on shared frequency discrimination accuracy

## Rollback Plan

If issues arise, revert changes:

```bash
cd /home/mjh/git/hf-timestd
git diff HEAD~1 src/hf_timestd/core/wwv_constants.py
git diff HEAD~1 src/hf_timestd/core/phase2_analytics_service.py
git diff HEAD~1 src/hf_timestd/core/phase2_temporal_engine.py

# If needed:
git checkout HEAD~1 src/hf_timestd/core/wwv_constants.py
git checkout HEAD~1 src/hf_timestd/core/phase2_analytics_service.py
git checkout HEAD~1 src/hf_timestd/core/phase2_temporal_engine.py

sudo systemctl restart timestd-analytics
```

## Next Steps

After validating this fix:
1. **TEC Pairwise Calculation** (Medium Priority) - Calculate TEC for each frequency pair separately
2. **Update HDF5 Schema** - Add frequency pair fields to L3 TEC data
3. **Web UI Enhancement** - Add "TEC vs Frequency" plots using per-pair data

## References

- CONTEXT.md lines 76-99: Station discrimination issue description
- CONTEXT.md lines 100-127: TEC calculation issue (next priority)
- wwv_constants.py lines 42-57: Broadcast schedule documentation
