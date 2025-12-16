# Digital RF Timestamp Reprocessing - November 13, 2025

## Problem

Digital RF data written **before time_snap is established** has incorrect timestamps based on wall clock instead of precise RTP-to-UTC conversion.

### Example
- **Nov 12, 2025 19:36 UTC**: Recording starts
- **Nov 12, 2025 19:38 UTC**: First WWV tone detected, time_snap established
- **2 minutes of data**: Written with **wall clock timestamps** (inaccurate by potentially seconds)
- **All data after**: Written with **precise RTP timestamps** (accurate to milliseconds)

## Solution: Option B (Write Now, Reprocess Later)

### Architecture Decision

**Write immediately with best available timestamp, then correct once time_snap available.**

This approach:
✅ Allows real-time monitoring even before WWV detection  
✅ No data gaps in Digital RF output  
✅ Maintains sample continuity  
✅ Aligns with dual-service architecture (analytics can reprocess)  
❌ Requires reprocessing step  
❌ Temporary inaccuracy in early data  

### Alternative (Option A - Wait for time_snap)

❌ Delays Digital RF by minutes or hours  
❌ Creates data gaps  
❌ Blocks spectrogram generation  
✅ No reprocessing needed  
✅ All data has correct timestamps from start  

**Decision**: Option B chosen for operational flexibility.

---

## How It Works

### Phase 1: Before time_snap (Cold Start)

```
┌─────────────┐
│ NPZ Archive │ RTP: 12345678, wall_clock: 1731445080.5
└──────┬──────┘
       │
       ▼
┌─────────────────────────────┐
│ calculate_utc_timestamp()   │
│ time_snap = None            │
│ → Use wall_clock: 1731445080.5  │ ⚠️ INACCURATE
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│ Digital RF Writer           │
│ start_global_index =        │
│   wall_clock * sample_rate  │
│ → 17314450805 (WRONG!)      │
└─────────────────────────────┘
```

**Result**: Data written to Digital RF with approximate timestamps.

### Phase 2: time_snap Established

```
┌─────────────────────────────┐
│ WWV Tone Detected!          │
│ timing_error: +2.3ms        │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│ _update_time_snap()         │
│ RTP 12346000 → UTC 14:00:00.000 │
│ Establishes RTP↔UTC mapping │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│ Logger: ⚠️ TIME_SNAP ESTABLISHED │
│ Consider reprocessing...    │
└─────────────────────────────┘
```

**Trigger**: First WWV/CHU tone detection with confidence > threshold.

### Phase 3: After time_snap (Normal Operation)

```
┌─────────────┐
│ NPZ Archive │ RTP: 12346320
└──────┬──────┘
       │
       ▼
┌─────────────────────────────┐
│ calculate_utc_timestamp()   │
│ time_snap.calculate_sample  │
│ → Precise RTP→UTC conversion │ ✅ ACCURATE
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│ Digital RF Writer           │
│ start_global_index =        │
│   correct_utc * sample_rate │
│ → 17629761600 (CORRECT!)    │
└─────────────────────────────┘
```

**Result**: All new data has correct timestamps.

### Phase 4: Reprocessing (Manual/Automatic)

```
┌─────────────────────────────┐
│ Reprocess Script            │
│ - Load NPZ archives         │
│ - Load time_snap from state │
│ - Recalculate UTC timestamps│
│ - Regenerate Digital RF     │
└─────────────────────────────┘
```

**Purpose**: Correct the early data that was written with wall clock timestamps.

---

## Implementation Details

### Code Changes

#### 1. Allow Writing Without time_snap
**File**: `src/signal_recorder/analytics_service.py`

**Before**:
```python
# Step 3: Decimation and Digital RF output (if time_snap available)
if self.state.time_snap:
    decimated_count = self._decimate_and_write_drf(archive, quality)
else:
    logger.debug("Skipping Digital RF - time_snap not yet established")
```

**After**:
```python
# Step 3: Decimation and Digital RF output
# Write immediately even without time_snap (uses wall clock approximation)
# Data will be reprocessed once time_snap is established
decimated_count = self._decimate_and_write_drf(archive, quality)

if not self.state.time_snap:
    results['needs_reprocessing'] = True
```

#### 2. Add Reprocessing Warning
**File**: `src/signal_recorder/analytics_service.py`

```python
# If this is the FIRST time_snap (transitioning from None), trigger reprocessing
if not old_time_snap:
    logger.info("⚠️  TIME_SNAP ESTABLISHED - Digital RF data written before now used wall clock")
    logger.info("⚠️  Consider reprocessing recent Digital RF data with correct timestamps")
    logger.info("⚠️  Run: python3 scripts/reprocess_drf_timestamps.py")
```

#### 3. Document Fallback Behavior
**File**: `src/signal_recorder/analytics_service.py`

```python
def calculate_utc_timestamp(self, time_snap: Optional[TimeSnapReference]) -> float:
    if time_snap:
        # Use precise RTP-to-UTC conversion
        return time_snap.calculate_sample_time(self.rtp_timestamp)
    else:
        # TEMPORARY: Use wall clock approximation until time_snap established
        # This data will need reprocessing once time_snap is available
        logger.debug(f"Using wall clock timestamp (no time_snap yet) - will need reprocessing")
        return self.unix_timestamp
```

---

## Reprocessing Procedure

### Automatic (Future)
When time_snap transitions from None → Established:
1. Analytics service logs warning
2. Triggers background reprocessing task
3. Scans recent NPZ archives (last 24 hours)
4. Regenerates Digital RF with corrected timestamps
5. Overwrites old files
6. Updates metadata

### Manual (Current)
```bash
# Check if time_snap is established
grep "time_snap established" /var/log/analytics-service.log

# If established, reprocess recent data
python3 scripts/reprocess_drf_timestamps.py --hours 24

# Or reprocess specific date
python3 scripts/reprocess_drf_timestamps.py --date 20251112

# Or specific channel
python3 scripts/reprocess_drf_timestamps.py --channel "WWV 5 MHz" --hours 6
```

---

## Impact Analysis

### Accuracy Comparison

| Time Period | Timestamp Source | Accuracy | Spectrogram Quality |
|-------------|-----------------|----------|---------------------|
| **Before time_snap** (wall clock) | System clock | ±1-5 seconds | ❌ Wrong time axis |
| **After time_snap** (RTP→UTC) | WWV tone | ±1 millisecond | ✅ Correct |
| **After reprocessing** | WWV tone (retroactive) | ±1 millisecond | ✅ Correct |

### Example Timestamps

**Nov 12, 2025 19:36-19:38 (before time_snap)**:
```
NPZ Archive: RTP 3525952408, wall_clock 1731445080.5
Digital RF written: sample 17314450805
Actual time: 2025-11-12 19:36:20.5 UTC
DRF directory created: 2025-11-12T19-36
```

**After reprocessing with time_snap**:
```
NPZ Archive: RTP 3525952408 (same)
time_snap: RTP 3525952400 → UTC 1731445080.0
Corrected UTC: 1731445080.8
Digital RF rewritten: sample 17314450808
DRF directory: 2025-11-12T19-36 (same, content updated)
```

**Difference**: ~0.3 seconds (wall clock was close but not exact)

---

## Operational Guidance

### Startup Behavior

**First 2-10 minutes after startup**:
- ⚠️  Digital RF timestamps approximate (wall clock)
- ✅ NPZ archives have precise RTP timestamps
- ⚠️  Spectrogram time axis may be off by seconds
- ✅ Data is still usable, just not perfectly aligned

**After first WWV/CHU detection**:
- ✅ Digital RF timestamps precise (RTP→UTC)
- ✅ Spectrogram time axis accurate
- ⚠️  Early data still has old timestamps (needs reprocessing)

### Monitoring

**Check time_snap status**:
```bash
# Via web UI
http://localhost:3000/channels.html
# Look for "time_snap established" badge

# Via logs
tail -f /var/log/analytics-service.log | grep "time_snap"

# Via API
curl http://localhost:3000/api/v1/channels/details | jq '.channels[] | {name, time_snap}'
```

**Expected output**:
```json
{
  "name": "WWV 5 MHz",
  "time_snap": {
    "established": true,
    "source": "wwv_verified",
    "confidence": 0.95,
    "age_minutes": 120
  }
}
```

### Best Practices

1. **For new deployments**: Wait 10-15 minutes after startup before generating spectrograms (let time_snap establish)

2. **For existing systems**: time_snap should be established from previous runs (persists in state file)

3. **After power outage/restart**: May take 2-10 minutes to re-establish time_snap

4. **Reprocessing schedule**: Run reprocessing script once daily:
   ```bash
   # Cron job: daily at 01:00 UTC
   0 1 * * * cd /home/mjh/git/signal-recorder && python3 scripts/reprocess_drf_timestamps.py --hours 24
   ```

---

## Future Enhancements

### Automatic Reprocessing
```python
# In _update_time_snap():
if not old_time_snap:
    logger.info("⚠️  TIME_SNAP ESTABLISHED - triggering automatic reprocessing")
    self._trigger_background_reprocessing()
```

### Progressive Timestamps
Use interpolation between wall clock and time_snap:
```python
if not time_snap:
    # Use wall clock but mark with lower confidence
    return (self.unix_timestamp, confidence=0.5)
else:
    # Use precise RTP→UTC conversion
    return (time_snap.calculate_sample_time(self.rtp_timestamp), confidence=0.95)
```

### Spectrogram Warnings
Display warning on spectrograms generated from pre-time_snap data:
```python
if data_has_pre_timesnap:
    plt.text(0.5, 0.95, "⚠️ Timestamps approximate (pre-time_snap data)",
             transform=ax.transAxes, ha='center', color='yellow')
```

---

## Summary

**Option B (Write Now, Reprocess Later)** provides:
- ✅ Immediate Digital RF availability
- ✅ No data gaps
- ✅ Operational flexibility
- ⚠️  Requires reprocessing for perfect accuracy
- ⚠️  First few minutes may have ~1-5 second timestamp errors

This approach aligns with the **dual-service architecture** where:
- Core recorder maintains perfect RTP timestamps in NPZ
- Analytics service generates derived products (Digital RF)
- Analytics can reprocess with improved algorithms
- No data loss, only timing refinement

**Trade-off**: Temporary inaccuracy (seconds) vs operational complexity (waiting for time_snap).

**Decision**: Acceptable trade-off for real-time monitoring use case.
