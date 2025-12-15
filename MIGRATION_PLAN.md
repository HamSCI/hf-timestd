# Migration Plan: grape-recorder → HF Time Standard Analysis

**Date**: 2025-12-14  
**New Project Name**: HF Time Standard Analysis  
**New Package Name**: `hf_timestd`  
**Script Prefix**: `timestd-`  
**Config Prefix**: `timestd-`

---

## Overview

This document outlines the migration from "grape-recorder" to "HF Time Standard Analysis" (`hf_timestd`).

### Naming Conventions
| Old | New |
|-----|-----|
| `grape-recorder` (project) | `hf-timestd` (project slug) |
| `grape_recorder` (Python package) | `hf_timestd` |
| `grape_recorder/grape/` (core module) | `hf_timestd/core/` |
| `grape-*.sh` (scripts) | `timestd-*.sh` |
| `grape-*.service` (systemd) | `timestd-*.service` |
| `timestd-config.toml` | `timestd-config.toml` |
| `/etc/hf-timestd/` | `/etc/hf-timestd/` |
| `/var/log/grape-recorder/` | `/var/log/hf-timestd/` |
| `GRAPE_*` (env vars) | `TIMESTD_*` |

### Functions to Separate (Future Work - Separate GRAPE App)
These GRAPE-specific functions will be moved to a separate application:
1. **Decimation to 10 Hz** - `decimation.py`, `decimated_buffer.py`
2. **Spectrogram generation** - `spectrogram_generator.py`, `carrier_spectrogram.py`
3. **Power graphs** - visualization code
4. **Digital RF product preparation** - `daily_drf_packager.py`, `drf_batch_writer.py`
5. **PSWS upload** - `uploader.py`, `upload_tracker.py`
6. **Phase 3 products** - `phase3_product_engine.py`, `phase3_products_service.py`

**Scripts NOT migrated to hf_timestd** (stay with grape app):
- `grape-spectrogram.sh` - Spectrogram generation
- `grape-daily-upload.sh` - PSWS upload packaging
- `grape-products.sh` - Phase 3 products service
- `grape-phase3.sh` - Phase 3 batch processing

---

## Phase 1: Scripts (5 files for hf_timestd)

### Files to Rename
| Old Name | New Name |
|----------|----------|
| `scripts/timestd-all.sh` | `scripts/timestd-all.sh` |
| `scripts/timestd-analytics.sh` | `scripts/timestd-analytics.sh` |
| `scripts/grape-core.sh` | `scripts/timestd-core.sh` |
| `scripts/grape-core-v3.sh` | `scripts/timestd-core-v3.sh` |
| `scripts/grape-ui.sh` | `scripts/timestd-ui.sh` |

### Scripts Staying with GRAPE App (not migrated)
- `scripts/grape-daily-upload.sh`
- `scripts/grape-phase3.sh`
- `scripts/grape-products.sh`
- `scripts/grape-spectrogram.sh`

### Internal Changes in Scripts
- `common.sh`: Update all `GRAPE_*` env vars to `TIMESTD_*`
- Update references to `timestd-config.toml` → `timestd-config.toml`
- Update references to `/etc/hf-timestd/` → `/etc/hf-timestd/`
- Update references to `/var/log/grape-recorder/` → `/var/log/hf-timestd/`
- Update Python module references `grape_recorder` → `hf_timestd`

---

## Phase 2: Systemd Services (4 files for hf_timestd)

### Files to Rename
| Old Name | New Name |
|----------|----------|
| `systemd/timestd-analytics.service` | `systemd/timestd-analytics.service` |
| `systemd/timestd-core-recorder.service` | `systemd/timestd-core-recorder.service` |
| `systemd/grape-radiod-monitor.service` | `systemd/timestd-radiod-monitor.service` |
| `systemd/timestd-web-ui.service` | `systemd/timestd-web-ui.service` |

### Services Staying with GRAPE App (not migrated)
- `systemd/grape-daily-upload.service`
- `systemd/grape-daily-upload.timer`
- `systemd/grape-spectrograms.service`
- `systemd/grape-spectrograms.timer`

### Internal Changes
- Update `Description=` fields
- Update `ExecStart=` paths to use new script names
- Update `EnvironmentFile=` to `/etc/hf-timestd/environment`
- Update `WorkingDirectory=` if needed
- Update timer `Unit=` references

---

## Phase 3: Config Files (6 files)

### Files to Rename
| Old Name | New Name |
|----------|----------|
| `config/timestd-config.toml` | `config/timestd-config.toml` |
| `config/timestd-config.toml.template` | `config/timestd-config.toml.template` |
| `config/environment` | `config/environment` (update contents) |
| `config/environment.template` | `config/environment.template` (update contents) |
| `config/environment.production` | `config/environment.production` (update contents) |

### Internal Changes
- Update all `GRAPE_*` env vars to `TIMESTD_*`
- Update paths referencing `grape-recorder` → `hf-timestd`

---

## Phase 4: Python Package Structure

### Directory Renames
```
src/grape_recorder/          →  src/hf_timestd/
src/grape_recorder/grape/    →  src/hf_timestd/core/
```

### Key Files to Update
1. `setup.py` - Package name, entry points
2. `src/hf_timestd/__init__.py` - Imports, docstrings
3. `src/hf_timestd/paths.py` - Path references
4. `web-ui/grape-paths.js` → `web-ui/timestd-paths.js`

### Import Updates (all .py files)
- `from grape_recorder` → `from hf_timestd`
- `from grape_recorder.grape` → `from hf_timestd.core`
- `import grape_recorder` → `import hf_timestd`

---

## Phase 5: Internal References

### Python Files (~100 files)
- Update all imports
- Update docstrings mentioning "grape"
- Update logging prefixes
- Update class/function names if they contain "grape" (case by case)

### Shell Scripts (~54 files)
- Update references to renamed scripts
- Update Python module invocations

### JavaScript Files (web-ui/)
- Rename `grape-paths.js` → `timestd-paths.js`
- Update imports and references

---

## Phase 6: Documentation

### Files to Update
- `README.md` - Project name, description
- `ARCHITECTURE.md` - Package references
- `INSTALLATION.md` - All paths and commands
- `docs/*.md` - All documentation files
- `CONTEXT.md`, `TECHNICAL_REFERENCE.md`, etc.

---

## Execution Order

1. **Phase 1**: Rename scripts, update `common.sh`
2. **Phase 2**: Rename systemd services, update contents
3. **Phase 3**: Rename config files, update contents
4. **Phase 4**: Rename Python package directories
5. **Phase 5**: Update all imports and internal references
6. **Phase 6**: Update documentation

---

## Verification Checklist

After migration:
- [ ] `pip install -e .` succeeds
- [ ] `python -c "import hf_timestd"` works
- [ ] `timestd-core.sh -status` works
- [ ] `timestd-analytics.sh -status` works
- [ ] Web UI loads correctly
- [ ] All tests pass
- [ ] No remaining references to `grape_recorder` in Python imports
- [ ] No remaining references to `grape-` in script names (except archive/)

---

## Notes

- The `archive/` directory contains historical files and will NOT be migrated
- The `wsprdaemon/` directory is external and will NOT be migrated
- Memory entries referencing old paths will need manual updates
