# Session 2026-01-18: Service Resilience and Kalman State Persistence

## Summary

This session focused on fixing critical issues with service resilience, specifically:
1. TEC HDF5 data staleness (20+ hour gap)
2. D_clock discontinuities on fusion service restart
3. Adding systemd watchdog support to prevent silent service stalls

## Issues Fixed

### 1. TEC HDF5 Stale Data (20+ hours)

**Root Cause:** The `timestd-l2-calibration` service was stuck/stalled for 20+ hours without systemd detecting the issue. The service was configured as `Type=simple` with no watchdog, so systemd couldn't detect when the service became unresponsive.

**Fix:**
- Added systemd watchdog support to `l2_calibration_service.py`
- Added systemd watchdog support to `physics_fusion_service.py`
- Updated service files with `Type=notify` and `WatchdogSec`

### 2. D_clock Discontinuities on Restart

**Root Cause:** The "Steel Ruler" implementation had a bug where Kalman state was being restored to unused variables (`self.kalman_offset`, `self.kalman_drift`) instead of the actual Kalman filter state array (`self.kalman_state[0]`, `self.kalman_state[1]`).

**Fix:** Modified `multi_broadcast_fusion.py` to correctly restore Kalman state:
```python
# Before (BROKEN):
self.kalman_offset = kalman_state.get('offset_ms', 0.0)  # Unused!
self.kalman_drift = 0.0  # Unused!

# After (FIXED):
self.kalman_state[0] = restored_offset  # Actually sets the filter state
self.kalman_state[1] = 0.0  # drift forced to zero (Steel Ruler)
self.kalman_initialized = True
self.kalman_converged = True
```

## Files Modified

### Core Python Files
- `src/hf_timestd/core/l2_calibration_service.py` - Added systemd watchdog notifications
- `src/hf_timestd/core/physics_fusion_service.py` - Added systemd watchdog notifications
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Fixed Kalman state restoration bug
- `src/hf_timestd/core/metrology_engine.py` - Added CHU FSK debug logging
- `src/hf_timestd/core/metrology_service.py` - Added CHU FSK writer infrastructure
- `src/hf_timestd/core/wwvh_discrimination.py` - Skip test signal/BCD for CHU channels
- `src/hf_timestd/data_product_registry.py` - Added CHU FSK data product

### Systemd Service Files
- `systemd/timestd-l2-calibration.service`:
  - Changed `Type=simple` → `Type=notify`
  - Added `WatchdogSec=180` (3 minutes)
  - Changed `Restart=on-failure` → `Restart=always`

- `systemd/timestd-physics.service`:
  - Changed `Type=simple` → `Type=notify`
  - Added `WatchdogSec=120` (2 minutes)
  - Fixed module name: `physics_service` → `physics_fusion_service`
  - Fixed output path

### New Files
- `src/hf_timestd/schemas/l2_chu_fsk_v1.json` - Schema for CHU FSK decoded data

### Web UI
- `web-api/static/metrology.html` - Various metrology page enhancements

## Watchdog Configuration Summary

| Service | WatchdogSec | Type | Restart |
|---------|-------------|------|---------|
| timestd-l2-calibration | 180s (3 min) | notify | always |
| timestd-physics | 120s (2 min) | notify | always |
| timestd-fusion | 60s (1 min) | notify | always |

## Testing

After deployment:
1. `scripts/verify_pipeline.sh` shows 0 FAIL (was 1 FAIL for TEC stale)
2. Chrony sources show TSL1/TSL2 with reasonable offsets
3. Kalman filter reconverging after state reset

## Session 2026-01-18 (Part 2): Comprehensive Watchdog Audit

### Additional Services Updated

| Service | Before | After |
|---------|--------|-------|
| timestd-metrology | Type=forking, Restart=on-failure | Restart=always |
| timestd-vtec | Type=simple, no watchdog | Type=notify, WatchdogSec=60 |
| timestd-web-api | Type=simple, User=root | Type=notify, WatchdogSec=60, User=timestd |

### Files Modified (Part 2)

- `systemd/timestd-metrology.service` - Changed Restart=on-failure → Restart=always
- `systemd/timestd-vtec.service` - Added Type=notify, WatchdogSec=60
- `systemd/timestd-web-api.service` - Added Type=notify, WatchdogSec=60, changed User to timestd
- `systemd/timestd-ionex-download.service` - Added retry mechanism, OnFailure alert, timeout
- `scripts/live_vtec.py` - Added systemd watchdog notifications
- `web-api/main.py` - Added systemd watchdog notifications via async background task

### Complete Watchdog Status (Post-Audit)

| Service | Type | WatchdogSec | Restart | Python Watchdog | Status |
|---------|------|-------------|---------|-----------------|--------|
| timestd-fusion | notify | 120s | always | ✅ Yes | ✅ Complete |
| timestd-l2-calibration | notify | 180s | always | ✅ Yes | ✅ Complete |
| timestd-physics | notify | 120s | always | ✅ Yes | ✅ Complete |
| timestd-core-recorder | notify | 60s | always | ✅ Yes | ✅ Complete |
| timestd-metrology | forking | N/A | always | N/A (shell) | ✅ Fixed |
| timestd-vtec | notify | 60s | always | ✅ Yes | ✅ Fixed |
| timestd-web-api | notify | 60s | always | ✅ Yes | ✅ Fixed |

### Data Pipeline Dependency Map

```
L0: Raw IQ (core-recorder)
    ↓
L1: Metrology (metrology-service) → /phase2/{CHANNEL}/metrology/
    ↓
L2: Calibration (l2-calibration) → /phase2/{CHANNEL}/clock_offset/
    ↓
L3: Fusion (multi_broadcast_fusion) → /phase2/fusion/
    ↓                                    ↓
Chrony SHM                          Physics (physics_fusion_service)
                                         ↓
                                    TEC estimates → /phase2/science/tec/

Parallel: GNSS VTEC (live_vtec) → /data/gnss_vtec/
```

### State Persistence Files

| File | Purpose | Validation |
|------|---------|------------|
| `/var/lib/timestd/state/broadcast_calibration.json` | Kalman state + calibration offsets | ✅ Has sanity checks (offset < 150ms, age < 7 days) |
| `/var/lib/timestd/state/long_term_drift_stats.json` | Long-term drift estimator | ✅ Has age check (< 7 days) |
| `/var/lib/timestd/state/radiod-status.json` | Radiod monitoring state | Informational only |

### Single Points of Failure Identified

1. **L1 Metrology stalls** → L2 has no input → L3 has no input → Chrony feed stale
   - **Mitigation:** Restart=always ensures recovery, but no graceful degradation

2. **L2 Calibration stalls** → Physics has no input → TEC stale
   - **Mitigation:** Now has watchdog (WatchdogSec=180)

3. **HDF5 write fails** → Downstream readers get stale/corrupt data
   - **Mitigation:** SWMR mode + h5clear fallback already implemented

### Data Freshness Checks (Implemented)

Added upstream data freshness monitoring to prevent downstream services from silently processing stale data:

| Service | Checks | Threshold | Behavior |
|---------|--------|-----------|----------|
| `l2_calibration_service.py` | L1 metrology HDF5 mtime | 5 minutes | Warns if stale, continues processing |
| `multi_broadcast_fusion.py` | L1/L2 HDF5 mtime | 5 minutes | Warns if stale, continues processing |
| `physics_fusion_service.py` | L2 clock_offset HDF5 mtime | 5 minutes | Warns if stale, continues processing |

**Key Design Decision:** Services continue processing stale data (graceful degradation) rather than blocking. This ensures:
- Downstream services don't crash when upstream stops
- Chrony feed continues with last-known-good data
- Clear warnings in logs identify the stalled upstream service

### Chrony Monitor Fix

Updated `scripts/check-chrony-reach.sh` to check for TSL1/TSL2 sources instead of obsolete TMGR:
- Now checks both TSL1 and TSL2 sources
- Reports OK if at least one source meets threshold
- Warns if both sources are stale

### Recommendations for Future Work

1. **Alerting** - Add OnFailure= to more services for email alerts
2. **Monitoring Dashboard** - Add data freshness metrics to web UI
3. **Automatic Recovery** - Consider restarting upstream services when stale

## Known Issues for Next Session

1. **CHU FSK Decoder** - Infrastructure complete but decoder not producing successful decodes (signal processing issue, not infrastructure)

## Files Modified This Session (Complete List)

### Systemd Service Files
- `systemd/timestd-metrology.service` - Restart=always
- `systemd/timestd-vtec.service` - Type=notify, WatchdogSec=60
- `systemd/timestd-web-api.service` - Type=notify, WatchdogSec=60, User=timestd
- `systemd/timestd-ionex-download.service` - Retry mechanism, OnFailure alert

### Python Scripts
- `scripts/live_vtec.py` - Systemd watchdog notifications
- `scripts/check-chrony-reach.sh` - TSL1/TSL2 instead of TMGR
- `web-api/main.py` - Systemd watchdog via async task

### Core Services (Data Freshness)
- `src/hf_timestd/core/l2_calibration_service.py` - Upstream freshness check
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Upstream freshness check
- `src/hf_timestd/core/physics_fusion_service.py` - Upstream freshness check

### Documentation
- `docs/changes/SESSION_2026_01_18_SERVICE_RESILIENCE.md` - This file
- `CRITIC_CONTEXT.md` - Updated for next session (greenfield installation review)
