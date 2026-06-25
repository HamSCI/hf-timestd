# hf-timestd Integration Guide

Client API reference for wsprdaemon v4 and other external consumers.

## Quick Reference

| Command | Purpose | Machine-readable |
|---------|---------|-----------------|
| `hf-timestd version --json` | Version and schema info | JSON to stdout |
| `hf-timestd status --calib-file FILE` | Pipeline health check | JSON to stdout, exit code 0/1/2 |
| `hf-timestd calibrate --calib-file FILE` | Run fusion, write calibration JSON | Atomic JSON file |

---

## 1. Architecture Overview

hf-timestd is a 5-stage pipeline.  Each stage runs as a separate systemd
service and produces data that the next stage consumes:

```
radiod (IQ)
  └─→ [1] timestd-core-recorder      IQ → raw buffers
        └─→ [2] timestd-metrology@*   tone detection → L1 HDF5  (×9 channels)
              └─→ [3] timestd-l2-calibration   cross-station cal → L2 HDF5
                    └─→ [4] timestd-physics     propagation model (optional)
                          └─→ [5] timestd-fusion    Kalman+WLS → calibration JSON
                                                                → Chrony SHM
                                                                → HDF5
```

**The `calibrate` subcommand runs stage 5 only.**  Stages 1–4 must already
be running for the calibration file to contain meaningful data.

### Two deployment models

**Model A — Add `--calib-file` to existing fusion service (recommended)**

If hf-timestd is already installed and running, simply add the flag to the
existing `timestd-fusion.service` ExecStart line:

```ini
ExecStart=/opt/hf-timestd/venv/bin/python -m hf_timestd.core.multi_broadcast_fusion \
    --data-root /var/lib/timestd \
    --interval 8.0 \
    --enable-chrony \
    --calib-file /run/wsprdaemon/KA9Q_0/hftime.json \
    --log-level INFO
```

wsprdaemon then reads `/run/wsprdaemon/KA9Q_0/hftime.json`.  No separate
`wd-hftime@` service needed.

**Model B — wsprdaemon manages hf-timestd as a dependent service**

wsprdaemon installs hf-timestd into its own venv and manages it:

```ini
# /etc/systemd/system/wd-hftime@KA9Q_0.service
[Unit]
Description=HF Time Calibration for %i
After=wd-ka9q-radiod@%i.service
Requires=wd-ka9q-radiod@%i.service

[Service]
Type=notify
User=wsprdaemon
ExecStart=/opt/wsprdaemon/python/bin/python3 -m hf_timestd calibrate \
    --config /etc/wsprdaemon/hftime.toml \
    --calib-file /run/wsprdaemon/%i/hftime.json \
    --data-root /var/lib/wsprdaemon/hftime/%i
WatchdogSec=120
Restart=always
RestartSec=10

[Install]
WantedBy=wd-recording@%i.service
```

**Important:** Model B requires that the upstream services (recorder,
metrology, L2-calibration) are also running.  For a fresh deployment this
means installing the full hf-timestd service suite, not just the `calibrate`
subcommand.  See the install script at `scripts/install.sh`.

---

## 2. Calibration File (JSON)

Written atomically (tmp + rename) after every fusion cycle (~8 seconds).
Removed on SIGTERM to prevent stale reads.

### Schema

Full JSON Schema: `src/hf_timestd/schemas/calibration_v1.json`

### Primary fields (consumer contract)

```json
{
  "schema_version": "1.0.0",
  "source": "hf-timestd",
  "offset_ms": -1.4230,
  "uncertainty_ms": 0.2500,
  "convergence_state": "LOCKED",
  "quality_grade": "A",
  "usable": true,
  "last_update": "2026-03-27T14:02:01.003000+00:00",
  "last_update_unix": 1774803721.003
}
```

| Field | Type | Description |
|-------|------|-------------|
| `offset_ms` | float | System clock offset from UTC.  `true_time = system_time - offset_ms/1000` |
| `uncertainty_ms` | float | Combined RSS uncertainty (ISO GUM) |
| `convergence_state` | string | `ACQUIRING` → `LOCKED` → `REACQUIRING` |
| `quality_grade` | string | `A` (<0.3ms) / `B` (<1ms) / `C` (<3ms) / `D` (>3ms) |
| `usable` | bool | **Single go/no-go flag.**  True when LOCKED, uncertainty<10ms, not holdover. |
| `last_update_unix` | float | Unix epoch of last write (for staleness check) |

### Consumer pseudocode

```python
import json, time
calib = json.loads(open("/run/wsprdaemon/KA9Q_0/hftime.json").read())
if calib["usable"]:
    correction_sec = calib["offset_ms"] / 1000.0
    # Apply to wav start time
elif time.time() - calib["last_update_unix"] > 300:
    # Stale — hf-timestd may have stopped
    use_fallback()
else:
    # Not yet converged — wait or use NTP
    pass
```

### Extended fields (diagnostics)

| Field | Description |
|-------|-------------|
| `n_broadcasts` | Number of time-standard broadcasts in this cycle |
| `n_stations` | Unique stations (max 4: WWV, WWVH, CHU, BPM) |
| `stations_used` | List of station names |
| `uncertainty_budget` | `{statistical_ms, systematic_ms, propagation_ms}` |
| `station_detail` | Per-station `{count, mean_ms, intra_std_ms}` |
| `consistency_flag` | `OK` / `CROSS_STATION_DISAGREE` / `INTRA_ANOMALY` / ... |
| `single_station_mode` | True if no cross-validation possible |
| `holdover_mode` | True if Kalman is coasting (no fresh measurements) |
| `dominant_propagation_mode` | e.g. `2F2` |
| `adev_60s`, `adev_1000s` | Allan deviation (stability metrics) |

---

## 3. Version Query

```bash
# Human-readable
$ hf-timestd version
hf-timestd 6.12.0
  Python: 3.11.2
  Calibration schema: 1.0.0
  Components:
    core_recorder: 2.0
    multi_broadcast_fusion: 1.1
    ...

# Machine-readable (for components.ini)
$ hf-timestd version --json
{
  "name": "hf-timestd",
  "version": "6.12.0",
  "schemas": {"calibration": "1.0.0"},
  ...
}
```

---

## 4. Health Check

```bash
$ hf-timestd status --calib-file /run/wsprdaemon/KA9Q_0/hftime.json
{
  "status": "OK",
  "exit_code": 0,
  "calibration": {
    "file": "/run/wsprdaemon/KA9Q_0/hftime.json",
    "usable": true,
    "convergence_state": "LOCKED",
    "offset_ms": -1.423,
    "uncertainty_ms": 0.25,
    "quality_grade": "A",
    "age_seconds": 4.2,
    "stale": false
  },
  "data_freshness": {
    "fusion_hdf5": {"file": "fusion_fusion_timing_20260327.h5", "age_seconds": 4.2, "stale": false},
    "active_metrology_channels": 9,
    "total_metrology_channels": 9
  }
}
```

### Exit codes

| Code | Status | Meaning |
|------|--------|---------|
| 0 | `OK` | Pipeline healthy, calibration usable |
| 1 | `WARN` | Pipeline running but calibration not yet usable (acquiring) |
| 2 | `CRIT` | Pipeline stale or not running |

Use in systemd health checks or wsprdaemon's `wd-ctl status`:

```bash
if hf-timestd status --calib-file /run/wsprdaemon/KA9Q_0/hftime.json >/dev/null 2>&1; then
    echo "HF time calibration: OK"
else
    echo "HF time calibration: degraded or down"
fi
```

---

## 5. Configuration (TOML)

The `calibrate` and `daemon` subcommands read a TOML configuration file.
The minimum required keys for wsprdaemon integration:

```toml
[station]
latitude = 38.92      # Receiver latitude (decimal degrees, north positive)
longitude = -95.28    # Receiver longitude (decimal degrees, east positive)
callsign = "AC0G"
grid_square = "EM38ww"

[ka9q]
status_address = "239.192.152.141"   # radiod status multicast
data_address = "239.103.26.231"      # radiod data multicast

[timing]
authority = "fusion"   # "rtp" if GPS+PPS present, "fusion" otherwise

[fusion]
timing_authority_level = "L5"  # L1-L6 (see grade thresholds below)
```

### Timing authority levels

| Level | Hardware | Grade A threshold |
|-------|----------|------------------|
| L1 | GPS+PPS + GPSDO | 0.1 ms |
| L2 | GPS+PPS, no GPSDO | 0.2 ms |
| L3 | GPSDO only (no PPS) | 0.3 ms |
| L4 | GPS (no PPS/GPSDO) | 0.5 ms |
| L5 | NTP only | 1.0 ms |
| L6 | Uncalibrated | 3.0 ms |

---

## 6. Service Dependencies for Fresh Deployments

If deploying hf-timestd from scratch for wsprdaemon, you need the full
service suite.  The install script handles this:

```bash
# Clone and install
git clone https://github.com/HamSCI/hf-timestd.git
cd hf-timestd
sudo scripts/install.sh

# Verify
hf-timestd version --json
hf-timestd status --data-root /var/lib/timestd
```

### Required services (start order)

1. `radiod` — KA9Q software-defined radio daemon (external)
2. `timestd-core-recorder` — IQ capture → raw buffers
3. `timestd-metrology@{channel}` — tone detection → L1 HDF5 (one per channel)
4. `timestd-l2-calibration` — cross-station calibration → L2 HDF5
5. `timestd-fusion` (with `--calib-file`) — Kalman+WLS → calibration JSON

### Optional services

- `timestd-physics` — propagation model, science products (improves grade)
- `timestd-vtec` — GNSS VTEC for ionospheric correction
- `timestd-web-api` — real-time dashboard

---

## 7. Forward Compatibility

The calibration JSON uses `additionalProperties: true` in its schema.
Consumers should:

1. Check `schema_version` — major version bump = breaking change
2. Ignore unknown fields — new fields may appear in minor versions
3. Use `usable` as the primary go/no-go — its semantics are stable
