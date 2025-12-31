# HF Time Standard Analysis - Project Context

**Last Updated:** December 31, 2025  
**Version:** 5.0.0 (HDF5-Native)  
**Status:** Production (9 channels running at AC0G)

## Quick Reference

**What:** Precision HF timing system extracting D_clock measurements from WWV/WWVH/CHU/BPM broadcasts  
**Where:** `/opt/hf-timestd` (production) or `/home/mjh/git/hf-timestd` (development)  
**Services:** `timestd-core-recorder`, `timestd-analytics`, `timestd-fusion`, `timestd-vtec`, `timestd-web-ui-fastapi`  
**Web UI:** <http://localhost:3000>

---

## Current State (Dec 31, 2025)

### ✅ Recently Completed (V5.0 Documentation Overhaul)

1. **System Documentation Updated**
    - Revised `ARCHITECTURE.md` to V5.0 (HDF5-Native Pipeline).
    - Detailed the **6-Service Architecture**: Core, Analytics, Fusion, VTEC, Science, Web UI.
    - Documented **Digital RF** adoption for L0 raw data.
    - Documented **HDF5 L1/L2/L3** schema.
    - Documented **Physics-Informed Propagation** (IONEX VTEC integration).

2. **Installation & Usage Guides**
    - Updated `README.md` and `INSTALLATION.md` to match the current codebase.
    - Added dependencies: `libhdf5-dev`, `python3-tables`.
    - Clarified 6 service names and roles.

3. **Linting & Cleanup**
    - Resolved markdown lint errors in all documentation.
    - Committed and pushed all changes to `main`.

### 📊 Deployment Status

- **Services:** All 6 services are installed and active on the current machine (AC0G).
- **Pipeline:** Functioning HDF5-native flow (Wave 3).
- **Data:** Digital RF archives appearing in `/var/lib/timestd/raw_archive/`.

---

## 🎯 Next Session Priority: Fresh Install Verification

**Goal:** Carefully review the project installation process and documentation, then attempt to install it on a brand new computer on the LAN.

**Objectives:**

1. **Documentation Audit:** Verify `INSTALLATION.md` instructions are 100% accurate against the `scripts/install.sh` reality.
2. **Dependency Check:** Confirm all system (`apt`) and python (`pip`) dependencies are explicitly listed.
3. **Dry Run / Simulation:** Inspect the installation scripts for hardcoded paths or user assumptions.
4. **Actual Install:** Perform the install on the target new machine.
5. **Validation:** Ensure all 6 services start, config generation works, and Web UI is accessible.

---

## System Architecture (V5.0)

### The Six Services

1. **Core Recorder:** Digital RF capture (`timestd-core-recorder`)
2. **Analytics:** Signal processing (`timestd-analytics`)
3. **Fusion:** Multi-broadcast timing solve (`timestd-fusion`)
4. **VTEC:** GNSS/IONEX data manager (`timestd-vtec`)
5. **Science Aggregator:** Spectrograms (`timestd-science-aggregator`)
6. **Web UI:** Visualization dashboard (`timestd-web-ui-fastapi`)

### Data Flow

```
RTP (UDP) -> Core (Digital RF .h5) -> Analytics (L2 .h5) -> Fusion (L3 .h5) -> Chrony (SHM)
                                           ^
                                           |
                                      VTEC (IONEX)
```

## AI Agent Guidance for Next Session

**Context to provide:**

1. `scripts/install.sh` - The master installer script.
2. `packages/python/pyproject.toml` - Python dependencies.
3. `packages/debian/control` (if applicable) or `apt` requirements.
4. `INSTALLATION.md` - The guide to be verified.

**Key questions to ask:**

- Does `install.sh` blindly assume `sudo`?
- Are there non-standard system dependencies (e.g., specific HDF5 versions)?
- Does the default config file generation allow for immediate "Test Mode" start?
- Are service files (`.service`) using absolute paths compatible with the target system?

**Constraints:**

- The target machine is "brand new" - assume minimal pre-installed software.
- Network access is available (LAN/Internet).
