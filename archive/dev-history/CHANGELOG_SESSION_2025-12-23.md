# Changelog - Session 2025-12-23

## Receiver Proliferation Fix

**Date**: 2025-12-23  
**Version**: 3.0.1  
**Type**: Bug Fix (Critical)

### Problem
The `hf-timestd` application was creating duplicate radiod channels on every service restart, leading to resource exhaustion and system instability. Expected 9 channels, but saw 18+ channels accumulating over time.

### Root Cause
- Application was using `ManagedStream` which calls `ensure_channel()`
- `ensure_channel()` computes SSRCs from ALL parameters including `destination` and `encoding`
- Parameter inconsistencies across restarts caused different SSRCs and duplicate channels
- No discovery before channel creation meant duplicates were always created

### Solution
Implemented three-part fix based on wspr-recorder pattern:

1. **Discovery-Before-Create Pattern**
   - Check for existing channels before creating new ones
   - Match by frequency, preset, and sample rate
   - Reuse existing channels when found
   - Only create if no match exists

2. **Health Monitoring & Auto-Recovery**
   - Monitor data flow every 5 seconds
   - Detect silence after 10 seconds
   - Automatically recreate channels (handles radiod restarts)
   - Recovery time: 10-15 seconds

3. **Direct RadiodStream Usage**
   - Replaced `ManagedStream` with direct `RadiodStream`
   - Explicit channel management via `create_channel()`
   - Clear separation of concerns

### Changes

#### Modified Files
- `src/hf_timestd/core/stream_recorder_v2.py` (+189/-75 lines)
  - Added `_create_channel()` with discovery-before-create logic
  - Implemented `_health_monitor_loop()` for automatic recovery
  - Replaced `ManagedStream` with `RadiodStream`
  - Health check every 5s, silence threshold 10s

- `src/hf_timestd/core/core_recorder_v2.py` (+51/-75 lines)
  - Simplified to delegate to `StreamRecorderV2`
  - Removed client-side discovery and matching logic
  - Cleaner initialization flow

- `src/hf_timestd/core/tiered_storage.py` (+6/-6 lines)
  - Fixed dataclass parameter ordering (unrelated bug)
  - Made `cold_buffer_root` optional with default

#### New Files
- `scripts/verify_channel_count.py`
  - Verification script for testing channel count
  - Detects duplicates by frequency

### Test Results
- ✅ Clean slate: 9 channels created
- ✅ Service restart: 9 channels (reused existing)
- ✅ Radiod restart: Auto-recovery in 10-15 seconds
- ✅ Idempotency: No duplicates across multiple restarts

### Migration
No breaking changes. Deployment steps:
1. Stop service
2. Update code: `pip install -e .`
3. Optionally clean old duplicate channels
4. Start service
5. Verify channel count remains at 9

### Performance Impact
- **Startup time**: +3 seconds (discovery step)
- **Memory**: Reduced (no duplicate channels)
- **Recovery time**: 10-15 seconds (from radiod restart)
- **CPU**: Minimal (5s health check interval)

### Key Learnings
1. Always check for existing resources before creating new ones
2. Reference implementations (wspr-recorder) provide valuable patterns
3. Fast failure detection minimizes data loss
4. Health monitoring is essential for production reliability
