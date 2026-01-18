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

## Known Issues for Next Session

1. **CHU FSK Decoder** - Infrastructure complete but decoder not producing successful decodes (signal processing issue, not infrastructure)

2. **Service Resilience** - Need comprehensive review of all systemd services to ensure:
   - All services have appropriate watchdog timeouts
   - Data write failures don't clobber downstream data
   - Services recover gracefully from transient errors

3. **Pipeline Data Dependencies** - Need to ensure upstream service failures don't corrupt downstream data products
