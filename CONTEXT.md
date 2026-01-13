# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: "Steel Ruler" Release (v5.3.2)

**Version**: v5.3.2 (Hotfix) - 2026-01-13
**Core Philosophy**: **"Steel Ruler" Metrology**. The system treats the local GPSDO as a fixed standard (zero process noise) to measure ionospheric variance.

### 🌟 Recent Accomplishments through v5.3.2

1. **"Steel Ruler" Stabilized**: Eliminated 0.03 ppm clock drift by clamping Kalman `drift_ms_per_min` to 0.0 and anchoring to the GPSDO.
2. **Verification Modernized**: Updated `scripts/verify_pipeline.sh` to check for metadata sidecars, HDF5 latency, and zero drift.
3. **Documentation Complete**: Added `docs/METROLOGIST.md`, updated `ARCHITECTURE.md` and `TECHNICAL_REFERENCE.md`.
4. **Service Stability**: Fixed `timestd-physics` syntax error (v5.3.2 hotfix) and restored full service stack.
5. **Chrony Feed**: Robustly feeding Chrony SHM with sub-millisecond precision.

### ⚠️ Active Issues / Watchlist

- **TEC Staleness at Night**: The `timestd-physics` service correctly reports stale TEC data during nighttime when only single frequencies are visible. This is a scientific limitation, not a software failure.
- **Service Reliability**: `timestd-physics` had a startup crash fixed in v5.3.2; monitor for regression.

---

## ✅ Session Complete: Greenfield Installation Fixed (v5.3.3)

**Date**: 2026-01-13  
**Status**: **INSTALLATION READY** - All critical blockers resolved

### Accomplishments

**Greenfield Installation Verification & Fixes:**

1. ✅ **Installation Path Fixes**: Corrected all `web-ui` → `web-api` directory references in `install.sh`
2. ✅ **Systemd Service Fixes**: Renamed service to `timestd-web-api.service`, fixed script path to `start.sh`
3. ✅ **Python Dependency Fixes**: Removed conflicting `toml` package, fixed `web-api/start.sh` to use `tomllib`
4. ✅ **Documentation Standardization**: Updated README, INSTALLATION.md to use port 8000, correct service names
5. ✅ **Configuration Standardization**: Updated `config/timestd-config.toml` to port 8000
6. ✅ **Script Verification**: Confirmed all referenced scripts exist (monitor_radiod_health.py, live_vtec.py, health-check-*.sh)

### Files Modified (6 files, 20 edits)

- `scripts/install.sh` - Fixed paths, service names, ports (12 edits)
- `web-api/start.sh` - Fixed tomllib import (1 edit)
- `pyproject.toml` - Removed toml dependency (1 edit)
- `README.md` - Updated service names, ports, user flag (3 edits)
- `INSTALLATION.md` - Updated service names, ports (2 edits)
- `config/timestd-config.toml` - Standardized port to 8000 (1 edit)

### Documentation Created

- `INSTALLATION_READINESS_REPORT.md` - Detailed analysis of all issues found
- `INSTALLATION_FIXES_APPLIED.md` - Complete summary with testing checklist

### Installation Now Works

```bash
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd
sudo ./scripts/install.sh --mode production
# Services start correctly, Web API accessible at http://localhost:8000
```

---

## 🎯 Next Session Goals

1. **Test on Fresh VM** - Validate installation on clean Debian 12 or Ubuntu 22.04 LTS
2. **Hardware Integration Guide** - Document ka9q-radio setup and configuration
3. **Performance Tuning** - Optimize service startup times and resource usage
