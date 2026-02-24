# INSTALLATION CONTRACT — hf-timestd

**Version:** 1.0.0
**Last Updated:** 2026-02-23
**Status:** Active — evolves with implementation

---

## 1. Goal

Ensure hf-timestd can be **reliably installed, configured, updated, and operated** in both production (24/7 systemd) and development (local venv) modes, with clear separation of concerns and deterministic behavior on restart.

### Performance Objectives

- **Fresh install to first data**: <15 minutes (production mode, assuming ka9q-radio already running)
- **Bootstrap to LOCKED**: ~2 minutes (with NTP available)
- **Update deployment**: <5 minutes via `scripts/update-production.sh`
- **Service restart**: deterministic resume from persisted state (no D_clock jumps, no calibration loss)
- **Disk usage**: ~2–3 GB/day/channel (raw buffer) + ~50–100 MB/day (HDF5 products)
- **CPU**: <5% per channel (core recorder); metrology is batch/bursty; radiod on CPUs 8–15

### Deliverable Products

| Artifact | Purpose |
|----------|---------|
| `scripts/install.sh` | Automated installer for test/production modes |
| `scripts/update-production.sh` | Sync repo → production (pip install, web-api rsync, systemd, schemas) |
| `scripts/ensure-venv.sh` | Create/update local venv |
| `scripts/verify_pipeline.sh` | Comprehensive pipeline health check |
| `scripts/check-freshness-alert.sh` | Freshness monitoring (cron) |
| `systemd/*.service` | 8 service units + 3 timers |
| `config/timestd-config.toml.template` | Configuration template |
| `/etc/hf-timestd/environment` | Environment file (single source of truth for paths) |

### Verification Steps

1. All 6 core services show `active (running)`: core-recorder, metrology, l2-calibration, fusion, physics, web-api
2. Raw buffer files appearing in `/var/lib/timestd/raw_buffer/` within 1 minute of start
3. HDF5 metrology files appearing in `/var/lib/timestd/phase2/*/metrology/` within 3 minutes
4. `http://localhost:8000` serves the dashboard
5. `chronyc sources` shows TSL1/TSL2 with non-zero reachability
6. `scripts/verify_pipeline.sh` passes all checks
7. `journalctl -u timestd-metrology --since "5 min ago"` shows tick analysis lines

---

## 2. Constraints

### Directory Layout (FHS-Compliant)

| Mode | Data | Logs | Config | Code |
|------|------|------|--------|------|
| Production | `/var/lib/timestd/` | `/var/log/hf-timestd/` | `/etc/hf-timestd/` | `/opt/hf-timestd/` |
| Test | `/tmp/timestd-test/` | `/tmp/timestd-test/logs/` | `config/` | `./` (git repo) |

### Production Path Architecture

```
/opt/hf-timestd/
├── venv/                          # Python virtual environment
├── src/hf_timestd/                # Core library (pip install -e)
├── web-api/                       # FastAPI application
│   ├── static/                    # HTML dashboards
│   ├── routers/                   # API endpoints
│   ├── services/                  # Data access layer
│   └── main.py                    # Application entry point
├── scripts/                       # Operational scripts
└── schemas/                       # JSON schemas (also in site-packages)
```

### Git Repo vs Production

- **Git repo**: `/home/mjh/git/hf-timestd/` — development and version control
- **Production**: `/opt/hf-timestd/` — where services actually run
- **Sync mechanism**: `sudo scripts/update-production.sh [--pull]`
- Schema files must exist in **both** venv site-packages AND `/opt/hf-timestd/src/hf_timestd/schemas/` (web API resolves from src path)
- `update-production.sh` handles: pip install, web-api rsync, schema files, systemd units, cron freshness monitor, logrotate config

### Service Dependencies and Ordering

```
ka9q-radio (radiod) — external prerequisite
    ↓
timestd-core-recorder     — Phase 1: RTP → raw_buffer
    ↓
timestd-metrology         — Phase 2: L1 measurements
    ↓
timestd-l2-calibration    — Phase 2: L2 calibrated timing
    ↓
timestd-fusion            — Phase 3: multi-broadcast fusion → Chrony SHM
timestd-physics           — Phase 3: TEC estimation
    ↓
timestd-web-api           — visualization and API
```

Optional services (independent):
- `timestd-vtec` — GNSS VTEC (requires GNSS receiver)
- `timestd-radiod-monitor` — hardware health
- `timestd-ionex-download.timer` — daily IONEX maps
- `timestd-chrony-monitor.timer` — Chrony health
- `grape-daily.timer` — daily GRAPE processing + PSWS upload (01:00 UTC)

### CPU Affinity

- All timestd Python services: CPUs 0–7 (`CPUAffinity=0-7` in systemd, `taskset 0x00ff` in shell)
- radiod: CPUs 8–15 (`ff00`)
- Ensures radiod has uncontested L3 cache for real-time USB/FFT processing

### Configuration Requirements

Minimum required in `/etc/hf-timestd/timestd-config.toml`:

```toml
[station]
callsign = "<CALLSIGN>"
grid_square = "<GRID>"
latitude = <decimal_degrees>    # Required for physics models
longitude = <decimal_degrees>   # Required for physics models

[ka9q]
status_address = "<radiod_status_mDNS>"
```

### System User

- Production services run as `timestd` system user (created by `install.sh`)
- Data directories owned by `timestd:timestd`
- Chrony SHM segments require appropriate permissions

### Python Environment

- Python 3.11+
- Core deps: `ka9q-python`, `numpy>=1.24`, `scipy>=1.10`, `h5py>=3.8`, `toml`, `zstandard`
- Web API deps: `fastapi`, `uvicorn`, `jinja2`
- Optional (ionospheric): `netCDF4>=1.6`, `boto3>=1.28`, `xarray`
- Optional (GRAPE): `digital_rf`
- System deps: `avahi-utils`, `libhdf5-dev`, `systemd-python`

### State Persistence

| State File | Location | Purpose |
|-----------|----------|---------|
| `broadcast_kalman_state.json` | `/var/lib/timestd/state/` | Per-broadcast Kalman filter states (17 filters) |
| `broadcast_calibration.json` | `/var/lib/timestd/state/` | Calibration offsets + hardware convergence |
| Per-channel state | `phase2/{CHANNEL}/state/` | Service state JSON |

- Kalman state persists across restarts — no D_clock jumps
- Hardware calibration persists `hardware_offset_ms` and `hardware_converged`
- Stale calibration files should be backed up before major changes

### Resilience

- **Watchdogs**: all Python services send `WATCHDOG=1` heartbeat via `systemd-python`
- **OnFailure**: email alerts on service failure
- **Backfill**: if metrology is down, it processes raw buffer backlog on restart
- **Crash-safe HDF5**: open-write-close per measurement — no dirty flags on SIGKILL

---

## 3. Format

### install.sh Interface

```bash
# Production (creates timestd user, installs to /opt/hf-timestd)
sudo ./scripts/install.sh --mode production

# Test/Development (local venv, temp data dirs)
./scripts/install.sh --mode test
```

### update-production.sh Interface

```bash
# Sync current repo state to production
sudo scripts/update-production.sh

# Pull from git first, then sync
sudo scripts/update-production.sh --pull
```

Actions performed:
1. `pip install -e /home/mjh/git/hf-timestd` (core library)
2. rsync `web-api/` → `/opt/hf-timestd/web-api/`
3. Copy schema files to site-packages
4. Copy systemd units → `/etc/systemd/system/`
5. `systemctl daemon-reload`
6. Copy cron freshness monitor
7. Copy logrotate config
8. Restart affected services

### Environment File

```bash
# /etc/hf-timestd/environment
TIMESTD_MODE=production
TIMESTD_DATA_ROOT=/var/lib/timestd
TIMESTD_LOG_DIR=/var/log/hf-timestd
TIMESTD_CONFIG=/etc/hf-timestd/timestd-config.toml
```

### Service Control Commands

```bash
# Status
sudo systemctl status timestd-{core-recorder,metrology,l2-calibration,fusion,physics,web-api}

# Logs
journalctl -u timestd-* -f                    # All services
tail -f /var/log/hf-timestd/phase2-shared10.log  # Per-channel metrology

# Restart single service
sudo systemctl restart timestd-metrology

# Full pipeline restart
sudo systemctl restart timestd-core-recorder timestd-metrology timestd-l2-calibration timestd-fusion timestd-physics timestd-web-api
```

### Logging

- Core recorder: `/var/log/hf-timestd/core-recorder.log`
- Metrology: `/var/log/hf-timestd/phase2-{channel}.log` (per-channel)
- Fusion, L2 calibration, physics, web-api: journalctl (`-u timestd-*`)
- Logrotate configured for `/var/log/hf-timestd/`

---

## 4. Failure Conditions

- **Running `install.sh --mode production` without sudo** — cannot create system user or write to `/opt/`
- **Missing `latitude`/`longitude` in config** — physics models produce incorrect predictions; `ArrivalPatternMatrix` fails
- **Missing `status_address` in `[ka9q]` config** — core recorder cannot discover channels
- **Editing files in git repo without running `update-production.sh`** — changes not reflected in production
- **Forgetting to restart services after Python changes** — only static HTML is hot-reloaded
- **Schema files missing from site-packages** — web API cannot resolve schemas; `update-production.sh` handles this
- **Stale calibration files after major algorithm changes** — back up and reset `/var/lib/timestd/state/broadcast_calibration.json`
- **radiod not running or mDNS not resolving** — core recorder fails to discover channels; check `avahi-resolve -n <status_address>`
- **HDF5 library version mismatch** — `libhdf5-dev` must match the version h5py was compiled against
- **Chrony SHM permissions** — `timestd` user must have access to shared memory segments; stale segments need cleanup
- **CPU affinity not set** — radiod and timestd competing for same cores causes RTP packet loss
- **Network buffer too small** — `net.core.rmem_max` should be ≥26214400 for reliable multicast reception
- **Starting services out of order** — core-recorder must be running before metrology can process data
- **Deleting raw buffer files while metrology is processing** — causes file-not-found errors; use retention policies, not manual deletion
- **Running multiple instances of the same service** — HDF5 write contention; systemd prevents this but manual runs can conflict
