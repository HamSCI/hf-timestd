# Session 2026-01-27: TSL Unreachable Diagnosis and Recovery

## Problem Statement
TSL sources (TSL1/TSL2) were unreachable in Chrony, and this failure occurred **silently** without raising any alarms. Additionally, the core recorder was consuming excessive memory (~4GB), causing repeated service failures.

## Root Cause Analysis

### Failure Chain (7 days undetected)

| Stage | What Failed | Duration | Why Silent |
|-------|-------------|----------|------------|
| **1. Core Recorder** | Memory at 3.9G/4G limit, only 40MB free | Since ~Jan 20 | No memory monitoring |
| **2. RTP Stream** | Stalled (`advancing: false` in radiod-rtp-state.json) | 7 days | RTP state file not monitored |
| **3. Bootstrap** | RTP-to-UTC mapping became stale | 7 days | No bootstrap health check |
| **4. Calibrations** | Drifted to 115-384ms (vs ±80ms limit) | Since Jan 25 | No calibration freshness alert |
| **5. Fusion** | Single-station mode, Chrony feed disabled | Ongoing | Logged as WARNING but no external alert |
| **6. Monitor Timer** | Stuck in "elapsed" state (systemd bug) | Since Jan 20 | No meta-monitoring of timers |

### Key Finding: Self-Protective Behavior Masked Failure
The fusion service correctly disabled the Chrony feed when entering single-station mode (safety feature), but this "correct" behavior hid the underlying problem from operators.

## Fixes Applied

### 1. Timer Fix (systemd/timestd-chrony-monitor.timer)
**Problem**: `OnUnitActiveSec` timers can get stuck in "elapsed" state if the service fails.

**Fix**: Changed to `OnCalendar=*:0/5` for reliable 5-minute scheduling.

```diff
-OnUnitActiveSec=5min
+OnCalendar=*:0/5
+AccuracySec=30s
```

### 2. Enhanced Monitoring (scripts/check-chrony-reach.sh)
Added checks for:
- **SHM segment existence** - Detects when fusion is in single-station mode
- **Calibration freshness** - Alerts if calibration state is >48h old
- **Single-station mode detection** - Scans fusion logs for warnings

### 3. Calibration Sanity Check Fix (core/multi_broadcast_fusion.py)
**Problem**: After bootstrap re-lock, raw d_clock values can be 100-200ms off until calibrations converge. The strict ±80ms sanity check prevented recovery.

**Fix**: Allow 3x the normal limit (±240ms) during initial convergence, then enforce strict ±80ms limit once Kalman converges.

```python
# During initial convergence, allow 3x the normal limit to permit recovery
# after bootstrap re-lock. Once converged, enforce strict limit.
effective_limit = MAX_CALIBRATION_OFFSET_MS if self.kalman_converged else MAX_CALIBRATION_OFFSET_MS * 3
```

## Recovery Steps Performed

1. **Restarted core recorder** - Freed memory, re-established RTP stream
2. **Bootstrap re-locked** - Acquired new RTP-to-UTC offset (~1769464124s)
3. **Reset calibrations to zero** - Allowed fresh convergence
4. **Restarted fusion service** - Picked up code fix for sanity check
5. **Verified Chrony receiving samples** - TSL2 now selected as active source

## Monitoring Gaps Identified (Future Work)

| Gap | Proposed Solution |
|-----|-------------------|
| Core recorder memory usage | Add memory threshold alert to systemd unit |
| RTP stream health | Monitor `radiod-rtp-state.json` freshness and `advancing` flag |
| Bootstrap lock status | Expose bootstrap state via health endpoint |
| Calibration convergence | Alert if calibrations not updated for >24h |
| Timer health | Add meta-monitoring for critical timers |
| Single-station mode duration | Alert if in single-station mode for >10 minutes |

## Files Modified

- `systemd/timestd-chrony-monitor.timer` - Fixed timer scheduling
- `scripts/check-chrony-reach.sh` - Added SHM/calibration/single-station checks
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Relaxed sanity check and discontinuity threshold during convergence
- `src/hf_timestd/core/bootstrap_service.py` - Added buffer cleanup after lock, two-tier bootstrap config
- `src/hf_timestd/core/timing_bootstrap.py` - Fixed memory leak in all_candidates list (capped at 500)

## Memory Analysis

The core recorder memory usage during bootstrap:
- **Bootstrap rolling buffers**: 9 channels × 150s × 24kHz × 8 bytes = ~250 MB (fixed, circular)
- **Tone detectors**: ~50 MB (FFT templates per channel)
- **all_candidates list**: Now capped at 500 entries (~5 MB)
- **Python/ka9q overhead**: ~100 MB
- **Baseline during bootstrap**: ~1.9 GB (acceptable with 4GB limit)

After bootstrap lock, the `_free_bootstrap_buffers()` method clears ~250 MB of rolling buffers.

## Additional Fixes (Late Session)

### 5. Phase Update Logic Fix (bootstrap_service.py)
**Problem**: `_update_phase_from_bootstrap()` was only called when `search_and_process()` returned a result, so the phase transition to `PROVISIONAL_LOCK` was often missed.

**Fix**: Always call `_update_phase_from_bootstrap()` after the search loop, not just when there's a result.

### 6. Buffer Cleanup Timing Fix (bootstrap_service.py)
**Problem**: Buffer cleanup was only triggered on `LOCKED` state, but archiving starts at `PROVISIONAL_LOCK`. The `TimingBootstrap` rarely reaches `LOCKED` (requires 10+ consecutive validations AND 5+ minutes).

**Fix**: Trigger `_free_bootstrap_buffers()` on `PROVISIONAL_LOCK` instead of waiting for `LOCKED`.

### 7. ka9q-python Upgrade
Upgraded to ka9q-python 3.4.1 which fixes RTP stream deduplication to prevent duplicate channels in radiod.

## Lessons Learned

1. **Self-protective behaviors need external alerting** - When a system correctly degrades for safety, operators must be notified.

2. **Systemd timers with `OnUnitActiveSec` are fragile** - Prefer `OnCalendar` for critical monitoring tasks.

3. **Sanity checks must account for recovery scenarios** - A check that prevents bad data can also prevent recovery if too strict.

4. **State file freshness is a critical health indicator** - Stale state files often indicate upstream failures.
