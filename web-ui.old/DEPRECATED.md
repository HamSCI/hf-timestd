# DEPRECATED - Old Web UI

**Date:** 2026-01-03  
**Status:** DEPRECATED - Do not use

---

## Notice

This directory contains the **old Node.js/Express-based web UI** which has been replaced by the new **FastAPI-based web UI** located in `/web-api/`.

**Use the new implementation instead:**
```bash
cd /home/mjh/git/hf-timestd/web-api
./start.sh
```

---

## Why Deprecated

The old web UI had several limitations:
- Node.js server separate from Python codebase
- Manual path synchronization required (`timestd-paths.js`)
- No direct integration with `DataProductReader`
- Monolithic JavaScript files (180KB+)
- Difficult to extend incrementally

The new FastAPI web UI provides:
- Direct Python integration with existing codebase
- Native HDF5 access via `DataProductReader`
- Modular, incremental development
- Auto-generated API documentation
- Better performance and maintainability

---

## Migration

All functionality from the old web UI is being reimplemented in the new FastAPI version:

**Phase 1 (Complete):**
- ✅ Station Overview
- ✅ System Health
- ✅ UTC Offset Dashboard

**Phase 2 (Planned):**
- Fusion timing details
- Station timing analysis
- Channel details
- Propagation overview
- Propagation modes

**Phase 3 (Planned):**
- TEC analysis
- Ionospheric conditions
- Uncertainty analysis
- Traceability

---

## Preservation

This directory is preserved for reference only. Key files:
- `monitoring-server-v3.js` - Old Express server
- `timestd-paths.js` - Path configuration
- `*.html` - Old page implementations

**Do not start or modify files in this directory.**

---

## Removal

This directory can be safely deleted after verifying the new web UI meets all requirements.

To remove:
```bash
rm -rf /home/mjh/git/hf-timestd/web-ui.old
```
