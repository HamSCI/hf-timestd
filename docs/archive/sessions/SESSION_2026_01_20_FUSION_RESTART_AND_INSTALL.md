# Session 2026-01-20: Fusion Restart Behavior and Installation Improvements

## Summary

This session fixed critical issues with fusion service restart behavior and improved the installation process for fresh deployments.

## Changes Made

### 1. Fusion Kalman State Persistence (Critical Fix)

**Problem**: After restarting `timestd-fusion`, the D_clock would jump significantly (e.g., from -0.463ms to +0.125ms), even when the Kalman filter had converged. The calibration file showed `converged=True` but the state wasn't being restored.

**Root Cause**: The `kalman_state` numpy array was being initialized *after* `_load_calibration()` was called, so the restore code would fail with `'MultiBroadcastFusion' object has no attribute 'kalman_state'`.

**Fix** (`src/hf_timestd/core/multi_broadcast_fusion.py`):
1. Moved Kalman state initialization (lines 511-518) to occur *before* `_load_calibration()` is called
2. Added SIGTERM signal handler for clean shutdown (lines 3946-3955)
3. Added calibration save on clean shutdown (lines 4193-4205)
4. Only save calibration when Kalman has converged (line 2146) to prevent overwriting good state with unconverged state

**Result**: D_clock now remains stable across restarts (e.g., +0.378ms → +0.378ms with no discontinuity).

### 2. Initial IONEX Download on Install

**Problem**: Fresh installations had no IONEX data, requiring manual download.

**Fix** (`scripts/install.sh`, lines 667-687):
- Creates `/var/lib/timestd/ionex` directory
- Runs `download_ionex_daily.sh` to fetch yesterday's IONEX data
- Warns if download fails (likely missing NASA CDDIS credentials)

### 3. SHM Permissions Fix

**Problem**: If chrony starts before fusion, it creates SHM segments with `root:600` permissions, preventing fusion from writing to them.

**Fix**:
- `scripts/install.sh` (lines 689-701): Clears stale SHM segments during install
- `scripts/start-services.sh` (lines 178-188): Clears stale SHM segments before starting fusion

**Manual fix for existing installations**:
```bash
sudo ipcrm -m $(ipcs -m | grep 0x4e545030 | awk '{print $2}')
sudo ipcrm -m $(ipcs -m | grep 0x4e545031 | awk '{print $2}')
sudo systemctl restart timestd-fusion
sleep 2
sudo systemctl restart chrony
```

### 4. Production Update Script

**New file**: `scripts/update-production.sh`

Provides a standard process for updating production after `git pull`:
```bash
cd /home/mjh/git/hf-timestd
git pull
sudo scripts/update-production.sh
```

The script:
- Reinstalls Python package (editable install)
- Copies updated scripts to `/opt/hf-timestd/scripts/`
- Updates systemd service files if changed
- Restarts fusion, metrology, physics, web-api services
- Does NOT restart core-recorder (to avoid data gaps)
- Verifies services are running

## Files Changed

| File | Changes |
|------|---------|
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Kalman init order, SIGTERM handler, shutdown save |
| `scripts/install.sh` | Initial IONEX download, SHM cleanup |
| `scripts/start-services.sh` | SHM cleanup before fusion start |
| `scripts/update-production.sh` | New file - production update workflow |

## Testing

Verified on B4-1:
- Fusion restart preserves D_clock offset (no discontinuity)
- Calibration file correctly shows `converged=True` after shutdown
- Kalman state restored on startup with correct offset

Verified on B3-1:
- SHM permissions fix works on fresh install
- `ipcs -m` shows `timestd:666` after fix

## Future Work

- Investigate why per-broadcast calibrations don't fully cancel systematic offset (~0.4ms residual)
- Add long-term offset tracking to understand stability
