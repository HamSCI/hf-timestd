# Chrony Reachability Issue - Root Cause and Solution

**Date:** 2025-12-30  
**Issue:** Chrony SHM refclock showing low Reachability (40 octal instead of 377)

## Root Cause Analysis

### The Problem

Chrony's Reachability register is an **octal bitmask** of the last 8 poll attempts:
- `377` (octal) = `11111111` (binary) = all 8 polls successful ✅
- `40` (octal) = `00100000` (binary) = only 1 of last 8 polls successful ❌

### Why It Was Happening

**Timing Mismatch:**
- Fusion service interval: 60 seconds (from `--interval 60.0`)
- Chrony poll interval: 8 seconds (from `poll 3` in chrony.conf)
- Rate limiter in code: 8 seconds

**The Issue:**
1. Fusion computes new D_clock estimate every **60 seconds**
2. Chrony polls SHM every **8 seconds** expecting fresh data
3. Between fusion updates (60s gap), chrony gets **stale data** (same `count` value)
4. Chrony rejects stale samples → low Reachability

**From chrony.conf:**
```
refclock SHM 0 refid TMGR poll 3 precision 1e-3 offset 0.0
```
- `poll 3` = 2^3 = 8 seconds

## Solution Implemented

### Option 3: Threaded SHM Updater (RECOMMENDED)

**Implementation:** `ChronySHMUpdater` class in `multi_broadcast_fusion.py`

**How it works:**
1. Fusion loop runs at configurable interval (e.g., 60s) - computes expensive D_clock estimates
2. Separate background thread updates Chrony SHM every 8 seconds
3. Thread reuses the latest fusion result until a new one is available
4. Chrony gets fresh updates (incrementing `count`) every 8 seconds → Reachability = 377

**Key Features:**
- Thread-safe with `threading.Lock()` for result sharing
- Automatic reconnection on SHM failures
- Logging of update statistics
- Graceful shutdown on service stop

**Code Location:**
- `src/hf_timestd/core/multi_broadcast_fusion.py:2476-2583` - ChronySHMUpdater class
- `src/hf_timestd/core/multi_broadcast_fusion.py:2612-2621` - Initialization
- `src/hf_timestd/core/multi_broadcast_fusion.py:2694-2696` - Update call

## Testing Status

### Completed ✅
1. Threaded implementation coded and tested for import
2. Service file updated with proper timeouts
3. Code deployed to venv

### Blocked ⚠️
**Initialization Bug:** Service is stuck in infinite HDF5 reading loop during startup:
- Takes >120 seconds to initialize
- Hits watchdog timeout before sending `READY=1`
- Prevents testing of threaded SHM updater

**Log Evidence:**
```
2025-12-30 13:02:43,XXX INFO:hf_timestd.io.hdf5_reader:Initialized L2 timing_measurements reader for CHU_14670 (schema v1.0.0)
[Repeats hundreds of times]
```

This is a **separate bug** in `_read_latest_measurements_hdf5()` that needs to be fixed before the threaded SHM updater can be properly tested.

## Expected Results (Once Initialization Bug Fixed)

### Before Fix
```bash
$ chronyc sources | grep TMGR
#- TMGR    0   3    40   145   +371us[ +373us] +/- 1000us
```
- Reachability: `40` (octal) = sporadic updates

### After Fix
```bash
$ chronyc sources | grep TMGR
#- TMGR    0   3   377     5   +371us[ +373us] +/- 1000us
```
- Reachability: `377` (octal) = all polls successful
- LastRx: Low value (recent update)

## Alternative Solutions (Not Implemented)

### Option 1: Match Fusion Interval to Chrony Poll
- Change `--interval 60.0` → `--interval 8.0`
- **Downside:** Wastes CPU computing fusion 7.5x more often than needed

### Option 2: Increase Chrony Poll Interval
- Change `poll 3` → `poll 6` (64 seconds) in chrony.conf
- **Downside:** Reduces time discipline update rate, less responsive to clock drift

## Next Steps

1. **Fix initialization bug** in HDF5 reader (separate issue)
2. **Test threaded implementation** once service starts properly
3. **Monitor Reachability** - should reach 377 within ~64 seconds (8 polls × 8s)
4. **Verify SHM updates** in logs every 8 seconds
5. **Update documentation** with threading architecture

## Files Modified

- `src/hf_timestd/core/multi_broadcast_fusion.py` - Added ChronySHMUpdater class
- `systemd/timestd-fusion.service` - Increased timeouts (temporary workaround)

## References

- Chrony refclock documentation: https://chrony.tuxfamily.org/doc/4.0/chrony.conf.html#refclock
- Reachability register format: 8-bit octal bitmask of recent polls
- SHM protocol: `count` field must increment for chrony to accept new samples
