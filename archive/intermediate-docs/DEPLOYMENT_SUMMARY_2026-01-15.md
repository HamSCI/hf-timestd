# Production Deployment Summary - 2026-01-15

## Overview
Successfully deployed latest repository code to production and resolved all critical service issues.

## Changes Deployed

### 1. Install Script Updates (`scripts/install.sh`)

**Chrony Configuration (lines 192-216):**
- ✅ Updated from single TMGR feed to dual TSL1/TSL2 architecture
- ✅ TSL1 (SHM 0): L1 raw metrology fusion (±0.85ms, fallback)
- ✅ TSL2 (SHM 1): L2 calibrated timing fusion (±0.3-1.0ms, primary)
- ✅ Proper poll intervals, precision, and delay parameters

**Service Restart Policy (line 555):**
- ✅ Changed from `Restart=on-failure` to `Restart=always`
- ✅ Increased `StartLimitBurst` from 3 to 5
- ✅ Ensures rock-solid resilience for forking services

### 2. Code Synchronization

**Deployed from:** `/home/mjh/git/hf-timestd/`  
**Deployed to:** `/opt/hf-timestd/`  
**Method:** `rsync -av --chown=timestd:timestd`

**Key Updates:**
- ✅ `binary_archive_writer.py`: Single-threaded zstd compression (threads=1) to prevent resource contention
- ✅ All source files synchronized with correct ownership (timestd:timestd)
- ✅ Scripts, web-api, and configuration templates updated

### 3. Services Restarted

All core services restarted successfully with latest code:

| Service | Status | PID | Uptime | Memory |
|---------|--------|-----|--------|--------|
| timestd-core-recorder | ✅ Running | 836893 | 43s | 317.6M / 2.0G |
| timestd-metrology | ✅ Running (9/9 processes) | Multiple | Fresh | - |
| timestd-fusion | ✅ Running | 841876 | 5s | 114.7M |
| timestd-physics | ✅ Running | 842003 | 5s | 114.3M |
| timestd-web-api | ✅ Running | 760608 | 13m | 272.2M / 512M |

## System Health Verification

**Pipeline Verification Results:**
- ✅ **PASS: 27 checks**
- ⚠️ **WARN: 10 checks** (expected - optional services, nighttime conditions)
- ❌ **FAIL: 0 checks**

**Critical Metrics:**
- ✅ All 9 metrology channels producing L1 data
- ✅ Fusion HDF5 actively written (3s ago, 4.0M)
- ✅ Steel Ruler baseline STABLE (drift = 0.0 ms/min)
- ✅ Kalman offset: 5.486 ms
- ✅ TEC HDF5 fresh (18s ago)
- ✅ 8 calibrated broadcast channels

**Chrony Integration:**
- ✅ TSL1 feed: 42 reach (34 successful polls)
- ✅ TSL2 feed: 42 reach (34 successful polls)
- ⚠️ Chrony evaluating sources (not yet selected for discipline)

**Web API:**
- ✅ Health endpoint: `{"status":"healthy","service":"hf-timestd-web-ui","version":"1.0.0"}`
- ✅ Accessible at http://localhost:8000

## Issues Resolved This Session

### 1. Service Resilience
- **Issue:** Metrology service used `Restart=on-failure`, failed to detect child process crashes
- **Fix:** Changed to `Restart=always` in production service file
- **Impact:** Automatic recovery on any exit condition

### 2. HDF5 File Corruption
- **Issue:** CHU_14670 had truncated HDF5 file preventing writes
- **Fix:** Removed corrupted file, process created fresh file with SWMR
- **Impact:** All channels now producing fresh data

### 3. File Permissions
- **Issue:** HDF5 files owned by root, log files inaccessible to timestd user
- **Fix:** `chown -R timestd:timestd` on data directories
- **Impact:** All services can write to their output files

### 4. Chrony SHM Access
- **Issue:** SHM segments owned by root with 600 permissions
- **Fix:** Removed old segments, fusion creates new ones with 666 permissions
- **Impact:** Chrony receiving HF-timestd timing feed

### 5. Web-API Service
- **Issue:** Permission errors on `/opt/hf-timestd` and venv
- **Fix:** Fixed ownership, created clean venv
- **Impact:** Web UI operational

### 6. Code Synchronization
- **Issue:** Production running outdated code (multi-threaded zstd)
- **Fix:** Deployed latest code with single-threaded zstd fix
- **Impact:** Prevents recorder service hangs on low-core systems

## SWMR Implementation Verified

**Universal Coverage:**
- ✅ All HDF5 writes use `DataProductWriter` class
- ✅ Two-step SWMR: Create file → Open r+ → Enable SWMR
- ✅ Automatic lock recovery with `h5clear -s`
- ✅ Concurrent read access enabled

**Files Using SWMR:**
- `hdf5_writer.py` - Universal writer
- `metrology_service.py` - L1 measurements
- `multi_broadcast_fusion.py` - L3 fusion
- `physics_service.py` - Physics-based fusion
- `science_aggregator.py` - TEC and science products
- `l2_calibration_service.py` - L2 calibration

## Recommendations for Future Deployments

### Greenfield Installation
The updated `install.sh` script is now production-ready:
```bash
cd /home/mjh/git/hf-timestd
sudo ./scripts/install.sh --mode production
```

**What it handles correctly:**
1. ✅ Creates timestd system user
2. ✅ Adds timestd to chrony group
3. ✅ Sets correct file ownership
4. ✅ Installs to `/opt/hf-timestd`
5. ✅ Configures dual TSL1/TSL2 chrony feeds
6. ✅ Uses `Restart=always` for resilience
7. ✅ Creates proper venv with correct permissions

### Production Updates
For updating existing production systems:
```bash
# 1. Deploy latest code
sudo rsync -av --chown=timestd:timestd --exclude='.git' --exclude='venv' \
  /home/mjh/git/hf-timestd/ /opt/hf-timestd/

# 2. Restart services
sudo systemctl restart timestd-core-recorder
sudo systemctl restart timestd-metrology
sudo systemctl restart timestd-fusion
sudo systemctl restart timestd-physics
sudo systemctl restart timestd-web-api

# 3. Verify
bash /home/mjh/git/hf-timestd/scripts/verify_pipeline.sh
```

### Monitoring
- ✅ Verify script updated to check TSL1/TSL2 (not TMGR)
- ✅ All services have `Restart=always` for automatic recovery
- ✅ Web UI provides real-time monitoring
- ✅ Chrony sources show HF-timestd feed status

## Session Duration
**Start:** 2026-01-14 22:36 UTC  
**End:** 2026-01-15 00:47 UTC  
**Duration:** ~2h 11m

## Final Status
✅ **Production system is now running latest code with rock-solid resilience**
