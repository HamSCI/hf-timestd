# Digital RF Timestamp Corruption Bug Fix
*Fixed: November 16, 2025, 5:40 AM*

## Root Cause Analysis

### The Bug
Digital RF writes were failing with backwards-time errors:
```
ValueError: Trying to write at sample 17632913654, but next available sample is 17632913704
```

This created corrupt DRF data with timestamps from year 2081 instead of 2025.

### Why It Happened

**Archive Processing Order Issue:**
1. Analytics service discovers NPZ files using `rglob('*.npz')` - sorted alphabetically by path
2. Filters files using `st_mtime` (file modification time) for "new files"
3. If files have out-of-order mtimes (clock changes, copies, etc.), archives process non-chronologically

**Monotonic Index Conflict:**
1. `DigitalRFWriter` initializes `next_index` based on first archive's timestamp
2. Advances monotonically: `next_index += len(decimated)` after each write
3. If next archive has OLDER timestamp → calculated index < next_index → DRF rejects write
4. Some writes succeed, some fail → corrupt/incomplete DRF data with wrong timestamps

### The Chain of Events
```
NPZ Archive 1: 2025-11-16T11:31:00 → sample index 17632913704 → writes OK
NPZ Archive 2: 2025-11-16T11:30:55 → sample index 17632913654 → CONFLICT!
  (Archive 2 processed after Archive 1 due to mtime ordering)
```

---

## The Fix

### 1. Strict Chronological Processing (analytics_service.py)
**Changed:** File discovery now uses filename comparison instead of mtime
**Why:** NPZ filenames contain ISO timestamps (YYYYMMDDTHHMMSSZ) which sort chronologically

```python
# OLD: Filter by mtime (can be out of order)
new_files = [f for f in all_files if f.stat().st_mtime > self.state.last_processed_time]

# NEW: Filter by filename (strict chronological)
new_files = [f for f in all_files if f.name > last_processed_name]
new_files = sorted(new_files, key=lambda f: f.name)  # Explicit sort
```

### 2. Backwards-Time Detection (digital_rf_writer.py)
**Added:** Safety check before every DRF write
**Why:** Catches remaining edge cases and prevents corruption

```python
# Safety check: Detect backwards time jump
calculated_index = int(chunk_timestamp * self.output_sample_rate)
if calculated_index < self.next_index:
    logger.warning(f"⚠️  Archive out of order! Skipping to maintain monotonic sequence.")
    continue  # Skip chunk rather than corrupt timeline
```

---

## Impact Assessment

### NPZ Archives (Source Data)
✅ **NOT AFFECTED** - Archives have correct timestamps  
✅ **Can regenerate DRF** from these clean source files

### Digital RF Output
❌ **CORRUPTED** - Needs deletion and regeneration  
❌ **Timestamps wrong** (year 2081 for Nov 12-16 data)  
❌ **Incomplete** (many writes failed/skipped)

### Spectrograms
⚠️ **AFFECTED** - Generated from corrupt DRF  
✅ **Can regenerate** once DRF is fixed

---

## Cleanup & Recovery Plan

### Step 1: Stop Analytics Services
```bash
# Find and stop all analytics processes
pkill -f "analytics_service"

# Or individually per channel
ps aux | grep analytics_service
kill <PID>
```

### Step 2: Delete Corrupt Digital RF Data
```bash
# Backup first (optional - it's corrupt but may want to analyze)
mv /tmp/grape-test/analytics /tmp/grape-test/analytics.corrupt.backup

# Or delete specific date ranges
rm -rf /tmp/grape-test/analytics/*/digital_rf/202511{12,13,14,15,16}
```

### Step 3: Reset Analytics State (Optional)
```bash
# Reset to reprocess from beginning
rm /tmp/grape-test/state/analytics-*.json

# Or just update last_processed_file to start from clean point
# Edit state files manually if you want to start from specific date
```

### Step 4: Restart Analytics Services
```bash
cd /home/mjh/git/signal-recorder
./start-analytics-services.sh  # Or however you start them
```

### Step 5: Monitor Logs for Issues
```bash
# Watch for backwards-time warnings (should be rare/none now)
tail -f /tmp/grape-test/logs/analytics-wwv5.log | grep "backwards\|out of order"

# Check DRF write progress
tail -f /tmp/grape-test/logs/analytics-wwv5.log | grep "Wrote.*decimated"
```

### Step 6: Regenerate Spectrograms
```bash
# Once DRF is rebuilt, regenerate spectrograms
for date in 20251112 20251113 20251114 20251115 20251116; do
    python3 scripts/generate_spectrograms_drf.py --date $date
done
```

---

## Verification

### Check Digital RF Timestamps Are Correct
```python
from pathlib import Path
from digital_rf import DigitalRFReader
from datetime import datetime, timezone

drf_path = Path('/tmp/grape-test/analytics/WWV_5_MHz/digital_rf/20251116')
for obs_dir in drf_path.glob('*/*/OBS*'):
    reader = DigitalRFReader(str(obs_dir))
    channels = reader.get_channels()
    if channels:
        start, end = reader.get_bounds(channels[0])
        props = reader.get_properties(channels[0])
        fs = props['sample_rate_numerator'] / props['sample_rate_denominator']
        
        start_dt = datetime.fromtimestamp(start / fs, tz=timezone.utc)
        end_dt = datetime.fromtimestamp(end / fs, tz=timezone.utc)
        
        print(f"{obs_dir.parent.parent.name}:")
        print(f"  {start_dt} to {end_dt}")
        print(f"  Year: {start_dt.year} (should be 2025, not 2081!)")
```

### Check for Backwards-Time Warnings
```bash
# Should see few/none of these after fix
grep "out of order" /tmp/grape-test/logs/analytics-*.log
```

---

## Prevention

### Going Forward
1. ✅ **Filename-based ordering** prevents mtime issues
2. ✅ **Safety checks** catch remaining edge cases  
3. ✅ **Skip corrupted data** rather than write it
4. ✅ **Clear logging** when archives processed out of order

### If Issue Recurs
1. Check core recorder - is it creating archives in order?
2. Check filesystem - clock changes/NTP jumps?
3. Check logs for "out of order" warnings
4. If many warnings → investigate why archives have wrong timestamps

---

## Files Modified

1. **src/signal_recorder/analytics_service.py** (lines 452-486)
   - Changed file discovery to use filename ordering
   - Added explicit sort by filename
   - Use filename comparison instead of mtime for filtering

2. **src/signal_recorder/digital_rf_writer.py** (lines 250-257, 289-294)
   - Added backwards-time detection in `add_samples()`
   - Added backwards-time detection in `flush()`
   - Skip corrupted chunks rather than write them

---

## Technical Details

### Sample Index Calculation
```
sample_index = unix_timestamp * sample_rate
```

For 10 Hz decimated data:
```
Nov 16 11:31:00 UTC = 1763292660 seconds
sample_index = 1763292660 * 10 = 17632926600
```

### Digital RF Invariant
Digital RF requires **strictly monotonic** sample indices. Once you write sample N, you can only write N+1, N+2, etc. You CANNOT go backwards or overlap.

### Why Corruption Happened
When Analytics tried to write earlier samples after later ones, Digital RF rejected the write but didn't clear internal state properly, leading to garbled timestamps in the HDF5 metadata.

---

## Lessons Learned

1. **Trust the filename timestamp** over filesystem metadata
2. **Validate monotonicity** before every write
3. **Fail gracefully** - skip bad data rather than corrupt good data
4. **NPZ archives are truth** - DRF is derived and can be regenerated
