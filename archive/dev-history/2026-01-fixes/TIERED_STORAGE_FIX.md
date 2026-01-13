# Tiered Storage Fix - OOM Issue Resolution

**Date:** 2026-01-06  
**Status:** ⚠ In Progress - Archiver not yet running  
**Priority:** Critical - System experiencing OOM kills every 20-25 minutes

---

## Problem Summary

The core recorder is being OOM-killed every 20-25 minutes because the tiered storage archiver is not running. Files accumulate in `/dev/shm` (RAM) instead of being moved to disk after 5 minutes.

**Current State:**
- Hot buffer (`/dev/shm/timestd`): **11GB** (should be ~260MB)
- Files in hot buffer: **153** (should be ~45)
- Oldest file age: **16+ minutes** (should be <5 minutes)
- Files that should be archived: **117**
- Memory usage: 7.7GB / 8GB (was hitting 4GB limit before)

**Impact:**
- Core recorder OOM-killed every 20-25 minutes since Jan 2
- Raw IQ recording interrupted continuously
- GRAPE uploads failing (no recent raw data)
- System instability

---

## Root Cause Analysis

### Issue 1: Tiered Storage Archiver Never Started
The `TieredStorageManager` archiver thread that moves old files from hot (RAM) to cold (disk) storage was never being initialized by the core recorder.

**Why:**
- `core_recorder_v2.py` had no code to call `init_tiered_storage()`
- Each `BinaryArchiveWriter` tried to get the singleton manager via `get_tiered_storage_manager()`
- Since nothing initialized it, the archiver thread never started
- Files accumulated indefinitely in `/dev/shm`

### Issue 2: Channel Configuration Path
The core recorder was reading `config.get('channels', [])` but channels are actually in `config['recorder']['channels']`, resulting in 0 channels detected and tiered storage not initializing.

### Issue 3: Insufficient Memory Limit
The systemd service had `MemoryMax=4G` which was too low for 9 channels with accumulated hot buffer data.

---

## Fixes Deployed

### Fix 1: Add Tiered Storage Initialization (Lines 214-242)
```python
# Initialize tiered storage if enabled
tiered_enabled = self.recorder_config.get('tiered_storage', False)
logger.info(f"Tiered storage config check: enabled={tiered_enabled}")

if tiered_enabled:
    try:
        from .tiered_storage import init_tiered_storage
        num_channels = len(self.channel_specs)
        hot_buffer_root = self.recorder_config.get('hot_buffer_root', '/dev/shm/timestd')
        ram_percent = self.recorder_config.get('ram_percent', 20)
        
        tiered_manager = init_tiered_storage(
            cold_buffer_root=str(self.output_dir),
            num_channels=num_channels,
            hot_buffer_root=hot_buffer_root,
            ram_percent=ram_percent,
            auto_start=True
        )
        
        logger.info(f"✓ Tiered storage ACTIVE: hot_minutes={tiered_manager.hot_minutes}")
    except Exception as e:
        logger.error(f"Failed to initialize tiered storage: {e}", exc_info=True)
```

### Fix 2: Fix Channel Configuration Loading (Line 154)
```python
# Channels can be at top level or in recorder section
self.channel_specs = config.get('channels', []) or self.recorder_config.get('channels', [])
```

### Fix 3: Increase Memory Limit
Changed `/etc/systemd/system/timestd-core-recorder.service`:
```
MemoryMax=8G  # Was 4G
```

### Fix 4: Create Symlink for GRAPE
```bash
ln -s /var/lib/timestd/raw_buffer /var/lib/timestd/raw_archive
```

---

## Tiered Storage Design

### Purpose
Optimize for **real-time analytics and fusion pipeline**, not long-term storage:

**Real-Time Needs:**
- Analytics service: 1-2 minutes (current + previous minute)
- Fusion service: 2-3 minutes (multi-station aggregation)
- Safety margin: 1-2 minutes for processing delays
- **Total: 3-5 minutes retention in RAM**

**Non-Real-Time Needs:**
- GRAPE uploads: Can wait for data on disk
- NASA data correlation: Can use cold storage
- Historical analysis: Disk-based access is fine

### Configuration
```toml
[recorder]
tiered_storage = true
hot_buffer_root = "/dev/shm/timestd"
ram_percent = 20  # 20% of available RAM
```

### Expected Behavior
- **Hot buffer (RAM):** Last 5 minutes × 9 channels = ~45 files = ~260MB
- **Archiver runs:** Every 30 seconds
- **Moves to cold:** Files older than 5 minutes
- **Cold storage (disk):** All older data for GRAPE/analysis

### Actual Calculation
```
Per channel per minute: 24kHz × 8 bytes × 60s = 11.5MB raw
With zstd compression (2.5x): ~4.6MB per minute per channel
9 channels × 5 minutes × 4.6MB = ~207MB expected hot buffer size
```

---

## Current Status

### Verification Results

**Tiered Storage Manager:**
```bash
$ python3 -c "from hf_timestd.core.tiered_storage import _manager; print(_manager)"
None  # ✗ Not initialized
```

**Hot Buffer State:**
- Size: 1.4GB (should be 207MB)
- Files: 153 (should be 45)
- Age distribution:
  - 0-2 min: 9 files ✓
  - 2-5 min: 27 files ✓
  - 5-10 min: 45 files ✗ (should be archived)
  - 10-15 min: 45 files ✗ (should be archived)
  - 15+ min: 27 files ✗ (should be archived)

**Cold Storage Activity:**
- Last archived file: 16-17 minutes ago (before restart)
- Files archived in last 5 minutes: 0
- **Archiver is NOT running**

---

## Troubleshooting

### Why Archiver Isn't Running

Despite deploying the initialization code, the tiered storage manager is still not initialized. Possible causes:

1. **Logging not visible:** Core recorder logs aren't appearing in journalctl
   - May be going to a different location
   - Can't verify if initialization code is executing

2. **Code path not reached:** The initialization code may not be executing
   - Need to verify with direct process inspection
   - May need to add file-based logging to debug

3. **Import error:** The `init_tiered_storage` import may be failing silently
   - Exception handling may be swallowing errors
   - Need to check for import issues

4. **Config issue:** Despite fixes, config may not be loading correctly
   - Manual test shows config is correct
   - But core recorder may be using different config loading

### Next Steps

1. **Add file-based logging** to verify initialization code executes
2. **Check process environment** to see if imports are working
3. **Manual initialization** as workaround if needed
4. **Consider alternative approach:** Initialize in `__init__` instead of `run()`

---

## Workaround (Temporary)

If archiver can't be started automatically, manual cleanup:

```bash
# Move old files to cold storage (run every 5 minutes)
find /dev/shm/timestd/raw_buffer -name "*.bin.zst" -type f -mmin +5 \
  -exec sh -c 'for f; do 
    channel=$(basename $(dirname $(dirname "$f")))
    date=$(basename $(dirname "$f"))
    mkdir -p "/var/lib/timestd/raw_buffer/$channel/$date"
    mv "$f" "/var/lib/timestd/raw_buffer/$channel/$date/"
    mv "${f%.bin.zst}.json" "/var/lib/timestd/raw_buffer/$channel/$date/" 2>/dev/null || true
  done' sh {} +
```

---

## Success Criteria

- ✗ Tiered storage manager initialized
- ✗ Archiver thread running
- ✗ Hot buffer size < 500MB
- ✗ Files older than 5 min moved to cold storage
- ✓ MemoryMax increased to 8GB
- ✗ No OOM kills for 24 hours
- ✓ GRAPE symlink created

---

## References

- Config: `/etc/hf-timestd/timestd-config.toml`
- Service: `/etc/systemd/system/timestd-core-recorder.service`
- Code: `src/hf_timestd/core/core_recorder_v2.py`
- Tiered storage: `src/hf_timestd/core/tiered_storage.py`
- Hot buffer: `/dev/shm/timestd/raw_buffer/`
- Cold storage: `/var/lib/timestd/raw_buffer/`
