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

## 🎯 Next Session Goal: Repository Cleanup & Simplification

**Objective**: Clean up the repository to improve maintainability, reduce clutter, and ensure only active, production-ready code and documentation remain in the main tree.

### Cleanup Strategy

The repository has accumulated significant technical debt from rapid development cycles. This session will archive interim documentation, remove debug/test artifacts, eliminate obsolete code, and organize the repository for long-term maintainability.

### 📋 Cleanup Categories

#### 1. **Interim Documentation to Archive** (Root Directory → `archive/dev-history/`)

**Session Notes & Debugging Logs** (~7 files):
- `DEBUGGING_SESSION_SUMMARY.md`
- `SESSION_2025-12-27.md`
- `SESSION_2026-01-11_PROPAGATION_FIX.md`
- `SESSION_2026-01-12_SIMPLIFIED_DISCRIMINATION.md`
- `SESSION_2026-01-12_TIMING_DISCRIMINATION.md`
- `SESSION_2026-01-12_UNIFIED_PHASE_ARCHITECTURE.md`
- `SESSION_SUMMARY.md`

**Fix/Status Reports** (~17 files with dates or "FIX"/"STATUS" in name):
- `ANALYTICS_FIXES_DEPLOYED_2026-01-04.md`
- `ANALYTICS_FIXES_IMPLEMENTED_2026-01-04.md`
- `ARCHITECTURAL_FIXES_2026-01-04.md`
- `CHRONY_REACHABILITY_FIX.md`
- `CRITIQUE_FIXES_2026-01-04.md`
- `FIX_STATUS_FINAL_2026-01-11.md`
- `PHASE2_RTP_OFFSET_FIX_2026-01-10.md`
- `PROPAGATION_DELAY_FIX_2026-01-11.md`
- `PROPAGATION_FIX_STATUS_2026-01-11.md`
- `SHARED_2500_FIX_2026-01-10.md`
- `STATION_DISCRIMINATION_FIX.md`
- `TEC_FIX_COMPLETE.md`
- `TEC_FIX_STATUS.md`
- `TEC_FIX_SUMMARY.md`
- `TIERED_STORAGE_FIX.md`
- `INSTALLATION_FIXES_APPLIED.md` (just created - keep for now, archive later)
- `INSTALLATION_READINESS_REPORT.md` (just created - keep for now, archive later)

**Analysis & Critique Documents** (~10 files):
- `ANALYTICS_CRITIQUE_2026-01-04.md`
- `ANALYTICS_PIPELINE_ISSUES.md`
- `CRITICAL_ANALYSIS.md`
- `CRITIC_CONTEXT.md`
- `CRITIC_CONTEXT.md.backup`
- `CONVERGENCE_MONITORING_2026-01-10.md`
- `DEGRADATION_ROOT_CAUSE_2026-01-04.md`
- `DISCRIMINATION_FAILURE_ANALYSIS_2026-01-11.md`
- `EXPECTED_SECOND_RTP_ANALYSIS.md`
- `NEGATIVE_DCLOCK_DIAGNOSIS_2026-01-11.md`

**Phase/Implementation Summaries** (~6 files):
- `PHASE1_COMPLETION_SUMMARY.md`
- `PHASE2_CALIBRATION_DIAGNOSIS_2026-01-11.md`
- `PHASE2_PROPAGATION_STATS_SUMMARY.md`
- `PHASE2_TEC_VALIDATION_SUMMARY.md`
- `PHASE2_UNCERTAINTY_DIAGNOSIS_2026-01-11.md`
- `IMPLEMENTATION_SUMMARY.md`

**Architectural Planning** (~5 files - evaluate if still relevant):
- `ARCHITECTURAL_SEPARATION.md`
- `ARCHITECTURE_MULTI_CHANNEL_MEASUREMENT.md`
- `BOOTSTRAP_DISCRIMINATION_STRATEGY.md`
- `CANONICAL_CONTRACTS.md`
- `GRAPE_SEPARATION.md`

**Data/Science Planning** (~6 files - evaluate if still relevant):
- `DATA_INVENTORY.md`
- `DATA_LOCATION_STANDARDIZATION.md`
- `DATA_LOCATION_STANDARDIZATION_SUMMARY.md`
- `MIGRATION_PLAN.md`
- `SCIENCE_AGGREGATOR_REVIEW.md`
- `SCIENCE_AGGREGATOR_ROADMAP.md`
- `SCIENCE_AGGREGATOR_VS_CAPABILITIES.md`

**Miscellaneous** (~3 files):
- `WEB_UI_REDESIGN.md`
- `recorder_diagnostics.md`
- `DEPENDENCIES.md` (check if redundant with pyproject.toml)

**Total: ~55 markdown files to review for archival**

#### 2. **Debug/Test Scripts to Remove or Archive** (Root Directory)

**Debug Scripts** (~3 files):
- `debug_detect.py`
- `debug_hdf5.py`
- `debug_read.py`

**Verification/Inspection Scripts** (~4 files - evaluate if needed):
- `verify_shm.py`
- `verify_struct.py`
- `inspect_audio.py`
- `inspect_l2.py`

**Compiled Binaries** (~2 files):
- `check_shm_layout` (binary)
- `check_shm_layout.c` (source - move to examples/ or archive/)

#### 3. **Obsolete Directories to Remove**

- `web-ui.old/` (61 items) - Replaced by web-api/
- `MagicMock/` (empty directory)
- `node_modules/` (empty - leftover from old Node.js attempt?)
- `data/` (empty - should be in /var/lib/timestd in production)
- `logs/` (empty - should be in /var/log/hf-timestd in production)
- `.pytest_cache/` (build artifact)
- `.vscode/` (IDE settings - should be in .gitignore)

#### 4. **Miscellaneous Artifacts to Remove**

- `20260101_spectrogram.png` (test image)
- `dir_listing.html` (531KB - debug artifact)
- `package.json` (44 bytes - leftover from Node.js)
- `pnpm-lock.yaml` (leftover from Node.js)
- `.netrc` (71 bytes - credentials file, should NOT be in repo!)

#### 5. **Source Code Review** (`src/hf_timestd/`)

**Directories to Review:**
- `legacy/` (6 items) - Check if still referenced, consider removing
- `grape/` (8 items) - Old GRAPE recorder code, check if obsolete
- `wspr/` (3 items) - WSPR functionality - is this active?
- `stream/` (5 items) - Check if superseded by newer code
- `interfaces/` (9 items) - Verify all are used

**Files to Review:**
- `audio_stream.py` vs `audio_streamer.py` - Redundant?
- `data_management.py` - Still used?
- `session_tracker.py` - Active feature?
- `venv_check.py` - Needed or can be removed?

### 🎯 Cleanup Execution Plan

#### Phase 1: Archive Interim Documentation
1. Create `archive/dev-history/2026-01-fixes/` directory
2. Move all dated fix/status/session documents (2025-12 through 2026-01)
3. Create `archive/dev-history/analysis/` directory
4. Move all critique/analysis/diagnosis documents
5. Update CHANGELOG.md with summary of archived documents

#### Phase 2: Archive Planning Documents
1. Review architectural planning docs - keep if still relevant to future work
2. Move obsolete planning docs to `archive/planning/`
3. Keep only: ARCHITECTURE.md, TECHNICAL_REFERENCE.md, DIRECTORY_STRUCTURE.md in root

#### Phase 3: Clean Root Directory
1. Move debug scripts to `archive/debug-tools/` or delete if obsolete
2. Move inspection scripts to `examples/` if useful, otherwise archive
3. Delete test artifacts (PNG, HTML, binaries)
4. **CRITICAL**: Remove `.netrc` and add to .gitignore
5. Remove empty directories (MagicMock/, node_modules/, data/, logs/)
6. Remove Node.js artifacts (package.json, pnpm-lock.yaml)

#### Phase 4: Clean Source Tree
1. Review `src/hf_timestd/legacy/` - remove if unused
2. Review `src/hf_timestd/grape/` - consolidate or remove
3. Review `src/hf_timestd/wspr/` - keep if active, otherwise remove
4. Check for duplicate functionality (audio_stream vs audio_streamer)
5. Remove unused imports and dead code

#### Phase 5: Update Documentation
1. Update README.md to reflect cleaned structure
2. Update DIRECTORY_STRUCTURE.md with new organization
3. Ensure CHANGELOG.md captures what was archived and why
4. Update .gitignore to prevent future clutter

### 📊 Success Criteria

After cleanup, the repository should have:
- ✅ Root directory with <15 markdown files (core docs only)
- ✅ No debug scripts, test artifacts, or temporary files in root
- ✅ No credentials files (.netrc) in repository
- ✅ No obsolete directories (web-ui.old, MagicMock, etc.)
- ✅ Clean src/ tree with only active, production code
- ✅ All interim documentation preserved in archive/ with clear organization
- ✅ Updated .gitignore to prevent future accumulation

### ⚠️ Important Preservation Rules

**DO NOT DELETE:**
- CHANGELOG.md (historical record)
- ARCHITECTURE.md, TECHNICAL_REFERENCE.md (core docs)
- DIRECTORY_STRUCTURE.md (reference)
- README.md, INSTALLATION.md (user-facing)
- CONTEXT.md (this file)
- LICENSE, MANIFEST.in, pyproject.toml (project metadata)
- archive/ directory (historical preservation)
- docs/ directory (user documentation)

**ALWAYS ARCHIVE, NEVER DELETE:**
- Any document with dates or session information
- Any fix/status/analysis document
- Any planning or design document
- Debug scripts that might be useful for troubleshooting

### 🔍 Code Review Checklist

For each file in `src/hf_timestd/`:
1. Is it imported by any active code? (grep for imports)
2. Is it referenced in systemd services or scripts?
3. Does it have a clear, current purpose?
4. Is it documented in TECHNICAL_REFERENCE.md?
5. If "no" to all above → candidate for removal

### 📝 Documentation After Cleanup

Create a summary document: `CLEANUP_2026-01-13.md` with:
- List of all archived files and their new locations
- List of all deleted files and rationale
- Summary of source code changes
- Updated repository statistics (file count, LOC reduction)
- Lessons learned for preventing future accumulation
