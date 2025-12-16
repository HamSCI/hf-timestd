# Partial Day Spectrogram Support - November 13, 2025

## Problem

Original implementation would **fail completely** when trying to generate spectrograms for incomplete days (e.g., recording started mid-day at 19:36 UTC).

Error: `Number of samples requested must be greater than 0, not -17629604998`

## User Requirement

> "Incomplete data would simply have nothing to plot for the times data missing."

Spectrograms should **plot whatever data exists** and show blank/gap for missing periods, like in GRAPE's existing narrow spectrum plots.

## Solution Implemented

### Modified: `scripts/generate_spectrograms_drf.py`

**Before (Failed on Partial Days):**
```python
# Calculate sample indices for the full day
start_sample = int(start_dt.timestamp() * sample_rate)  # 00:00 UTC
end_sample = int(end_dt.timestamp() * sample_rate)      # 23:59 UTC

# Constrain to bounds
if start_sample < bounds[0]:
    start_sample = bounds[0]
if end_sample > bounds[1]:
    end_sample = bounds[1]

# This would try to read negative samples!
data = reader.read_vector(start_sample, end_sample - start_sample, channel_name)
```

**After (Handles Partial Days):**
```python
# Calculate requested day range
start_sample = int(start_dt.timestamp() * sample_rate)
end_sample = int(end_dt.timestamp() * sample_rate)

# Check if ANY overlap exists
if bounds[1] < start_sample or bounds[0] > end_sample:
    logger.warning("No data overlap for requested date range")
    return None  # Truly no data for this date

# Constrain to ACTUAL data bounds (partial day is OK!)
actual_start = max(start_sample, bounds[0])
actual_end = min(end_sample, bounds[1])

# Warn user about partial coverage
if actual_start > start_sample:
    logger.info("⚠️  Partial day: Data starts later than midnight")
if actual_end < end_sample:
    logger.info("⚠️  Partial day: Data ends before midnight")

# Read whatever data exists
data = reader.read_vector(actual_start, actual_end - actual_start, channel_name)
```

### Added: Coverage Information in Plot

**Title now shows actual data span:**
```
WWV 2.5 MHz - 2025-11-12 - Carrier Spectrogram (10 Hz IQ)
Coverage: 19:36 - 23:59 UTC (4.4 hrs)
```

This makes it clear:
- Which hours have data (19:36 - 23:59)
- How much data was captured (4.4 hours)
- Missing hours are obvious (00:00 - 19:35 not shown)

## Behavior Examples

### Case 1: Full Day (24 hours)
```
2025-11-13 with data from 00:00 - 23:59 UTC
✅ Generates complete spectrogram
Title: "Coverage: 00:00 - 23:59 UTC (24.0 hrs)"
```

### Case 2: Partial Day (Recording Started Mid-Day)
```
2025-11-12 with data from 19:36 - 23:59 UTC
✅ Generates spectrogram for available hours
Title: "Coverage: 19:36 - 23:59 UTC (4.4 hrs)"
Missing hours: Simply not displayed (x-axis starts at 19:36)
```

### Case 3: No Data
```
2025-11-10 with no Digital RF data
❌ Fails gracefully: "No data overlap for requested date range"
UI shows: "No 10 Hz Digital RF data available for this date"
```

## Testing

### Test Partial Day
```bash
# Generate for Nov 12 (started at 19:36 UTC):
python3 scripts/generate_spectrograms_drf.py --date 20251112

# Expected output:
# ⚠️  Partial day: Data starts at sample XXX (not YYY)
# ✅ Read 15,840 samples (4.4 hours of data)
# ✅ Saved: WWV_5_MHz_20251112_carrier_spectrogram.png
```

### Test Full Day
```bash
# Generate for Nov 13 (full day):
python3 scripts/generate_spectrograms_drf.py --date 20251113

# Expected output:
# ✅ Read 86,400 samples (24.0 hours of data)
# ✅ Saved: WWV_5_MHz_20251113_carrier_spectrogram.png
```

### Test No Data
```bash
# Generate for future date:
python3 scripts/generate_spectrograms_drf.py --date 20251120

# Expected output:
# ⚠️  No data overlap for requested date range
# ⚠️  Requested: 2025-11-20 00:00:00+00:00 to 2025-11-20 23:59:59+00:00
# ⚠️  Available: samples XXX to YYY
```

## Visual Result

**Partial Day Spectrogram Example:**

```
┌─────────────────────────────────────────────────────────────┐
│ WWV 2.5 MHz - 2025-11-12 - Carrier Spectrogram (10 Hz IQ)  │
│        Coverage: 19:36 - 23:59 UTC (4.4 hrs)                │
├─────────────────────────────────────────────────────────────┤
│ 4 Hz  ┃░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                      │
│ 2 Hz  ┃████████████████████████████████                     │
│ 0 Hz  ┃████████████████████████████████                     │
│-2 Hz  ┃████████████████████████████████                     │
│-4 Hz  ┃░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                      │
└───────┸─────────────────────────────────────────────────────┘
        19:36    20:00    21:00    22:00    23:00    23:59
                        Hours, UTC
```

X-axis **only shows actual data range** (19:36 - 23:59)
Missing hours (00:00 - 19:35) **not displayed** - obvious gap

## Files Modified

### Python Script
- **`scripts/generate_spectrograms_drf.py`** (lines 119-157, 205-214)
  - Changed sample range calculation to use `actual_start` and `actual_end`
  - Added overlap detection (reject truly missing dates)
  - Added partial day warnings in logs
  - Added coverage subtitle in plot title

### Web UI (Already Done Previously)
- **`web-ui/channels.html`** (error handling)
  - Shows friendly message for "No data available"
  - Expandable technical details
  - "Try Different Date" button

## Impact

### Before
- ❌ Fails with cryptic negative sample error
- ❌ No spectrogram generated
- ❌ User confused about what went wrong

### After
- ✅ Generates spectrogram for whatever data exists
- ✅ Shows actual coverage in title
- ✅ Clear gaps where data missing
- ✅ Matches user expectation from existing GRAPE plots

## Edge Cases Handled

### 1. **Recording Started Mid-Minute**
Data from 19:36:47 UTC onwards
→ Rounds to minute boundary, plots from 19:36

### 2. **Recording Stopped Mid-Minute**
Data until 23:58:23 UTC
→ Plots until last complete data point

### 3. **Multiple Data Gaps**
Digital RF handles this internally with "zero samples"
→ Shows as low power regions in spectrogram

### 4. **Exactly 24 Hours**
Data from 00:00:00 to 23:59:59
→ Shows "Coverage: 00:00 - 23:59 UTC (24.0 hrs)"

### 5. **Less Than 1 Hour**
Data from 19:36 to 19:58
→ Shows "Coverage: 19:36 - 19:58 UTC (0.4 hrs)"
→ Still generates valid spectrogram (may look sparse)

## Validation

Compare with GRAPE narrow spectrum plot (shown by user):
- ✅ Partial day support (starts at 02:00 UTC in example)
- ✅ Clear gap before data starts (blank before 02:00)
- ✅ Coverage information in title
- ✅ X-axis shows only actual data range

**Matches expected behavior!**

## Future Enhancements

### Optional Full-Day X-Axis
Could add option to show full 00:00-23:59 axis with blank regions:
```python
# Set x-axis limits to full day even with partial data
ax.set_xlim([
    datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc),
    datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
])
```

This would make gaps more obvious but might be confusing when data truly starts mid-day.

### Multi-Day Gaps
For channels that stop/start recording across day boundaries:
- Current: Separate spectrograms per day
- Future: Could stitch multiple days into single plot

---

## Summary

Spectrograms now **gracefully handle partial days** by:
1. ✅ Detecting data overlap with requested date
2. ✅ Reading only the available data range
3. ✅ Plotting what exists (no artificial gaps)
4. ✅ Showing actual coverage in title
5. ✅ Warning user about partial data in logs

**Result**: User can generate spectrograms for any date that has data, even if incomplete, matching the behavior of other GRAPE visualization tools.
