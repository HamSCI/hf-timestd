# GRAPE State Files Reference

This document describes all persistent state files in the GRAPE Recorder system,
their purpose, and how to reset them safely.

**Created**: 2025-12-08  
**Issue**: 2.5 - Storage quota implications for state files

---

## Overview

GRAPE uses persistent state files to:
1. Resume processing after service restarts
2. Maintain calibration learned from data
3. Track convergence of timing estimates

**Important**: When the storage quota system ages out raw data from Phase 1,
the corresponding state files should also be reset to avoid orphaned state
that references deleted data.

---

## State File Locations

### Per-Channel State (Phase 2)

| File | Location | Producer | Consumer |
|------|----------|----------|----------|
| `convergence_state.json` | `phase2/{CHANNEL}/status/` | `clock_convergence.py` | `phase2_analytics_service.py` |
| `analytics-service-status.json` | `phase2/{CHANNEL}/status/` | `phase2_analytics_service.py` | `monitoring-server-v3.js` |
| `channel-status.json` | `phase2/{CHANNEL}/state/` | `phase2_analytics_service.py` | `monitoring-server-v3.js` |

### Global State

| File | Location | Producer | Consumer |
|------|----------|----------|----------|
| `broadcast_calibration.json` | `state/` | `multi_broadcast_fusion.py` | `multi_broadcast_fusion.py` |
| `radiod-status.json` | `state/` | `radiod_health_monitor.py` | `monitoring-server-v3.js` |
| `gpsdo_status.json` | `status/` | `gpsdo_monitor.py` | `monitoring-server-v3.js` |
| `timing_status.json` | `status/` | `phase2_analytics_service.py` | `monitoring-server-v3.js` |
| `core-recorder-status.json` | `status/` | `core_recorder.py` | `monitoring-server-v3.js` |

---

## State File Schemas

### convergence_state.json

Contains Kalman filter state for D_clock tracking.

```json
{
  "version": 2,
  "timestamp": "2025-12-08T21:05:00Z",
  "accumulators": {
    "WWV_10.0": {
      "station": "WWV",
      "frequency_mhz": 10.0,
      "count": 150,
      "mean_ms": 15.234,
      "state": "locked",
      "locked_mean_ms": 15.234,
      "locked_uncertainty_ms": 0.5,
      "kalman": {
        "x": [15.234, 0.001],
        "P": [[0.25, 0.0], [0.0, 0.0001]],
        "count": 150
      }
    }
  }
}
```

**Validation on load**:
- Version must match `STATE_FILE_VERSION` (currently 2)
- Kalman drift rate must be < 0.1 ms/min
- Covariance matrix must be positive definite
- State age must be < 24 hours

**Reset procedure**: Delete file and restart analytics service.

### broadcast_calibration.json

Contains per-broadcast calibration offsets learned from data.

```json
{
  "WWV_10.00": {
    "station": "WWV",
    "frequency_mhz": 10.0,
    "offset_ms": -2.5,
    "uncertainty_ms": 0.8,
    "n_samples": 1440,
    "last_updated": 1733688300.0,
    "reference_station": "CHU"
  }
}
```

**Reset procedure**: Delete file. Calibration will re-learn from scratch.

---

## Reset Procedures

### Full State Reset (All Channels)

Use this when switching modes or after major configuration changes:

```bash
./scripts/reset-state.sh --all
```

Or manually:

```bash
# Stop services
./scripts/grape-analytics.sh -stop
./scripts/grape-recorder.sh -stop

# Clear all state
DATA_ROOT=$(grep -A5 '[recorder]' config/grape-config.toml | grep test_data_root | cut -d'"' -f2)
rm -f "$DATA_ROOT"/phase2/*/status/convergence_state.json
rm -f "$DATA_ROOT"/phase2/*/clock_offset/*.csv
rm -f "$DATA_ROOT"/state/broadcast_calibration.json

# Restart services
./scripts/grape-recorder.sh -start
./scripts/grape-analytics.sh -start
```

### Per-Channel Reset

```bash
./scripts/reset-state.sh --channel "WWV 10 MHz"
```

Or manually:

```bash
CHANNEL="WWV_10_MHz"
rm -f "$DATA_ROOT/phase2/$CHANNEL/status/convergence_state.json"
rm -f "$DATA_ROOT/phase2/$CHANNEL/clock_offset/*.csv"
```

### Calibration Reset Only

```bash
rm -f "$DATA_ROOT/state/broadcast_calibration.json"
```

---

## Storage Quota Implications

When the storage quota system (not yet implemented) ages out raw Phase 1 data:

1. **Orphaned State Risk**: State files may reference measurements that no longer exist
2. **Stale Calibration**: Calibration learned from deleted data may be incorrect
3. **Channel Discovery**: Channels may appear in Phase 2 but have no raw data

### Recommended Approach

When implementing storage quota:

1. Track `data_start_time` in each state file
2. When deleting Phase 1 data older than X, also:
   - Delete corresponding Phase 2 CSV rows before that time
   - Reset convergence state if all data was deleted
   - Recalculate calibration from remaining data
3. Add `data_availability` field to status files indicating time range

---

## Troubleshooting

### D_clock Shows Linear Drift

**Symptom**: D_clock values drift at ~6.5 ms/minute

**Cause**: Corrupted Kalman filter state (e.g., wrong drift_rate persisted)

**Solution**:
```bash
rm -f /tmp/grape-test/phase2/*/status/convergence_state.json
./scripts/grape-analytics.sh -stop && ./scripts/grape-analytics.sh -start
```

### Web UI Shows Stale Data

**Symptom**: UI shows old timestamps or missing channels

**Cause**: Status files not being updated

**Solution**: Check analytics service is running:
```bash
./scripts/grape-analytics.sh -status
cat /tmp/grape-test/status/analytics-service-status.json | jq '.timestamp'
```

### Calibration Not Converging

**Symptom**: Fused D_clock doesn't approach 0

**Cause**: Calibration learned from bad data

**Solution**:
```bash
rm -f /tmp/grape-test/state/broadcast_calibration.json
# Wait 30+ minutes for re-learning
```

---

## Version History

- **v2** (2025-12-08): Added version validation, Kalman sanity checks
- **v1** (2025-12-01): Initial state persistence
