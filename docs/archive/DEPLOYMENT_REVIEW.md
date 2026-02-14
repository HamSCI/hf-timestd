# HF Time Standard Deployment Review

**Date:** 2025-12-16  
**Reviewer:** AI Assistant (Cascade)  
**Version:** 1.0

---

## Executive Summary

This document addresses the deployment review checklist from `CONTEXT.md`. The deployment infrastructure is **functional but has naming inconsistencies** that should be resolved before production deployment.

**Key Findings:**
1. ✅ Mode switching works correctly via config file
2. ⚠️ Environment variable naming is inconsistent (GRAPE_* vs TIMESTD_*)
3. ✅ Service scripts exist and are well-structured
4. ✅ Hot buffer uses `/dev/shm/timestd` in both modes
5. ✅ Log locations are correctly differentiated by mode
6. ✅ Service dependencies are properly defined
7. ✅ Health check procedures exist

---

## 1. Mode Switching: Test vs Production

### How It Works

The **config file is authoritative** for mode selection. The `mode` setting in `timestd-config.toml` determines which data root is used:

```toml
[recorder]
mode = "test"                              # or "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"
```

### Path Resolution Flow

```
timestd-config.toml
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│  mode = "test"                  mode = "production"          │
│  ─────────────                  ────────────────────         │
│  Data:  /tmp/timestd-test       Data:  /var/lib/timestd      │
│  Logs:  {data_root}/logs        Logs:  /var/log/hf-timestd   │
│  Config: {project}/config/      Config: /etc/hf-timestd/     │
│  Venv:   {project}/venv         Venv:   /opt/hf-timestd/venv │
└──────────────────────────────────────────────────────────────┘
```

### Implementation

- **Shell scripts:** `common.sh` provides `get_mode()` and `get_data_root()` functions that parse the config file
- **Python code:** `paths.py::load_paths_from_config()` reads mode and selects appropriate data root
- **Systemd services:** Read config path from environment file, config file determines mode

### Current State

| File | Mode |
|------|------|
| `config/timestd-config.toml` | `test` |
| `config/environment` | Uses GRAPE_* variables pointing to test paths |

---

## 2. Service Scripts

### Available Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `timestd-all.sh` | Start/stop/status all services | `-start`, `-stop`, `-status` |
| `timestd-core.sh` | Phase 1 core recorder (legacy) | `-start`, `-stop`, `-status` |
| `timestd-core.sh` | Phase 1 per-channel processes | `-start`, `-stop`, `-status`, `-restart` |
| `timestd-analytics.sh` | Phase 2 analytics (9 channels + fusion) | `-start`, `-stop`, `-status` |
| `timestd-ui.sh` | Web UI server | `-start`, `-stop`, `-status` |

### Script Features

- **Config auto-detection:** All scripts source `common.sh` which finds config
- **Mode awareness:** Scripts read mode from config to determine paths
- **Graceful shutdown:** SIGTERM first, then SIGKILL after timeout
- **Status reporting:** Shows running processes and data locations

---

## 3. Path Consistency

### ⚠️ Issue: Environment Variable Naming

The current `config/environment` uses **legacy GRAPE_*** variables:

```bash
GRAPE_MODE=test
GRAPE_DATA_ROOT=/tmp/timestd-test
GRAPE_CONFIG=/home/mjh/git/hf-timestd/config/timestd-config.toml
```

But templates show **new TIMESTD_*** variables:

```bash
TIMESTD_MODE=test
TIMESTD_DATA_ROOT=/tmp/timestd-test
TIMESTD_CONFIG=...
```

### Impact

- **Shell scripts:** `common.sh` checks for `TIMESTD_*` first, falls back to config file (works)
- **Systemd services:** Reference `${TIMESTD_PROJECT}`, `${TIMESTD_VENV}` which may not be set
- **Web UI:** Uses `TIMESTD_CONFIG` environment variable

### Recommendation

Regenerate `config/environment` using the `environment.timestd.template` format which defines both `TIMESTD_*` and `GRAPE_*` for backward compatibility.

---

## 4. Hot Buffer Location

### Configuration

```toml
[recorder]
tiered_storage = true
hot_buffer_root = "/dev/shm/timestd"
ram_percent = 20
```

### Behavior

| Mode | Hot Buffer | Cold Buffer |
|------|------------|-------------|
| Test | `/dev/shm/timestd/raw_buffer/` | `/tmp/timestd-test/raw_buffer/` |
| Production | `/dev/shm/timestd/raw_buffer/` | `/var/lib/timestd/raw_buffer/` |

**Note:** Hot buffer location is **mode-independent** (always `/dev/shm/timestd`). Only the cold buffer destination changes based on mode.

### RAM Budget

The tiered storage manager auto-configures based on available RAM:

```
Available RAM    Channels    Hot Minutes    RAM Used
─────────────    ────────    ───────────    ────────
1 GB             9           2              180 MB (18%)
4 GB             9           10             900 MB (22%)
8 GB             9           20             1.8 GB (22%)
16 GB            9           30             2.7 GB (17%)
```

---

## 5. Log Locations

### By Mode

| Mode | Log Location | Method |
|------|--------------|--------|
| Test | `/tmp/timestd-test/logs/` | Logs stored with data |
| Production | `/var/log/hf-timestd/` | FHS-compliant separate location |

### Log Files

| Service | Log File |
|---------|----------|
| Phase 1 Core | `phase1-WWV_10_MHz.log`, etc. |
| Phase 2 Analytics | `phase2-wwv10.log`, `phase2-fusion.log`, etc. |
| Web UI | `webui.log` |

### Systemd Logging

In production mode, services also log to journald:

```bash
journalctl -u timestd-core-recorder -f
journalctl -u timestd-analytics -f
journalctl -u timestd-web-ui -f
```

### Logrotate

Production install creates `/etc/logrotate.d/grape-recorder`:
- Daily rotation
- 14 days retention
- Compression enabled

---

## 6. Service Dependencies and Start Order

### Dependency Graph

```
                    ┌─────────────────────┐
                    │  ka9q-radio.service │
                    │  (external radiod)  │
                    └──────────┬──────────┘
                               │ After/Wants
                               ▼
                    ┌─────────────────────────────┐
                    │ timestd-core-recorder.service│
                    │ Phase 1: RTP → raw_buffer    │
                    └──────────┬──────────────────┘
                               │ After/Requires
                    ┌──────────┴──────────┐
                    ▼                     ▼
    ┌───────────────────────┐  ┌─────────────────────┐
    │timestd-analytics.service│  │timestd-web-ui.service│
    │Phase 2: Timing analysis │  │Monitoring dashboard  │
    └───────────────────────┘  └─────────────────────┘
```

### Start Order

1. **ka9q-radio** (external) - Must be running first
2. **timestd-core-recorder** - Phase 1 data capture
3. **timestd-analytics** - Phase 2 processing (requires core-recorder)
4. **timestd-web-ui** - Monitoring (can start independently)

### Stop Order (Reverse)

1. timestd-web-ui
2. timestd-analytics
3. timestd-core-recorder

### Systemd Commands

```bash
# Start all
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui

# Stop all
sudo systemctl stop timestd-web-ui timestd-analytics timestd-core-recorder

# Enable auto-start
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
```

---

## 7. Health Check Procedures

### Quick Status Check

```bash
# All-in-one status
./scripts/timestd-all.sh -status

# Individual service status
./scripts/timestd-core.sh -status
./scripts/timestd-analytics.sh -status
./scripts/timestd-ui.sh -status
```

### Process Verification

```bash
# Phase 1: Core recorder processes (one per channel)
pgrep -af "hf_timestd.core.channel_recorder"

# Phase 2: Analytics processes (one per channel + fusion)
pgrep -af "hf_timestd.core.phase2_analytics_service"
pgrep -af "hf_timestd.core.multi_broadcast_fusion"

# Web UI
pgrep -af "monitoring-server"
```

### Data Flow Verification

```bash
# Check raw_buffer is being written (Phase 1)
ls -la /tmp/timestd-test/raw_buffer/WWV_10_MHz/$(date +%Y%m%d)/

# Check Phase 2 outputs
ls -la /tmp/timestd-test/phase2/WWV_10_MHz/clock_offset/

# Check fusion output
tail -5 /tmp/timestd-test/phase2/fusion/fused_d_clock.csv

# Check convergence state
cat /tmp/timestd-test/phase2/*/status/convergence_state.json | jq .
```

### Web UI Health

```bash
# Check if responding
curl -s http://localhost:3000/api/v1/system/status | jq .

# Check channel discovery
curl -s http://localhost:3000/api/v1/channels | jq .
```

### Log Inspection

```bash
# Recent errors across all logs
grep -i error /tmp/timestd-test/logs/*.log | tail -20

# Phase 2 processing activity
tail -f /tmp/timestd-test/logs/phase2-wwv10.log

# Fusion activity
tail -f /tmp/timestd-test/logs/phase2-fusion.log
```

---

## Issues Found

### Issue 1: Environment Variable Naming Inconsistency

**Severity:** Medium  
**Location:** `config/environment`

**Problem:** Current environment file uses `GRAPE_*` variables, but:
- Systemd service files reference `${TIMESTD_PROJECT}`, `${TIMESTD_VENV}`, etc.
- `common.sh` checks for `TIMESTD_*` first

**Solution:** Update `config/environment` to use the template format:

```bash
# Primary variables (new naming)
TIMESTD_MODE=test
TIMESTD_DATA_ROOT=/tmp/timestd-test
TIMESTD_CONFIG=/home/mjh/git/hf-timestd/config/timestd-config.toml
TIMESTD_VENV=/home/mjh/git/hf-timestd/venv
TIMESTD_PROJECT=/home/mjh/git/hf-timestd
TIMESTD_WEBUI=/home/mjh/git/hf-timestd/web-ui
TIMESTD_LOG_DIR=/tmp/timestd-test/logs
TIMESTD_LOG_LEVEL=DEBUG

# Legacy compatibility
GRAPE_MODE=${TIMESTD_MODE}
GRAPE_DATA_ROOT=${TIMESTD_DATA_ROOT}
GRAPE_CONFIG=${TIMESTD_CONFIG}
GRAPE_VENV=${TIMESTD_VENV}
GRAPE_PROJECT=${TIMESTD_PROJECT}
GRAPE_WEBUI=${TIMESTD_WEBUI}
GRAPE_LOG_DIR=${TIMESTD_LOG_DIR}
GRAPE_LOG_LEVEL=${TIMESTD_LOG_LEVEL}
```

### Issue 2: Systemd Service File Variable References

**Severity:** Low (only affects production systemd deployment)  
**Location:** `systemd/*.service` files

**Problem:** Service files use `${TIMESTD_*}` variables but the environment file only defines `GRAPE_*`.

**Solution:** Either:
1. Update environment file (recommended, see Issue 1)
2. Or update service files to use `GRAPE_*` variables

### Issue 3: Install Script Creates raw_archive Instead of raw_buffer

**Severity:** Low  
**Location:** `scripts/install.sh:177`

**Problem:** Install script creates `$DATA_ROOT/raw_archive` but the system uses `raw_buffer`:

```bash
create_dir "$DATA_ROOT/raw_archive"   # Wrong name
```

**Solution:** Change to:
```bash
create_dir "$DATA_ROOT/raw_buffer"    # Correct name
```

---

## Recommendations

### Immediate Actions

1. **Fix environment file** - Regenerate with both TIMESTD_* and GRAPE_* variables
2. **Fix install.sh** - Change `raw_archive` to `raw_buffer`
3. **Test systemd deployment** - Run `install.sh --mode production` in a test environment

### Before Production Deployment

1. Run full install in production mode
2. Verify all services start correctly via systemd
3. Confirm data flows through hot buffer → cold buffer
4. Test service recovery after reboot
5. Verify log rotation is working

---

## Quick Reference

### Test Mode Commands

```bash
# Start all services
./scripts/timestd-all.sh -start

# Check status
./scripts/timestd-all.sh -status

# Stop all services
./scripts/timestd-all.sh -stop

# View logs
tail -f /tmp/timestd-test/logs/*.log
```

### Production Mode Commands

```bash
# Start services
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui

# Check status
sudo systemctl status timestd-core-recorder timestd-analytics timestd-web-ui

# View logs
journalctl -u timestd-core-recorder -f

# Stop services
sudo systemctl stop timestd-web-ui timestd-analytics timestd-core-recorder
```

---

*End of Deployment Review*
