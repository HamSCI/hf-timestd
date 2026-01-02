# Critic Context - HF-TimeStd Project Status

**Last Updated**: 2026-01-02 17:32 UTC  
**Session Focus**: Diagnose Fusion Service Failure After HDF5 Transition

## Current Critical Issue

**Fusion Service Not Writing HDF5 Files**

The `timestd-fusion` service is running but silent - no log output and no HDF5 writes after restart.

**Symptoms**:

- Service started: 2026-01-02 17:22:25 UTC
- Process running (PID 831451, consuming CPU)
- **Zero log output** after systemd startup messages
- Last HDF5 write: 17:11 (before restart)
- No new HDF5 files after 10+ minutes

**Context**: This occurred immediately after removing CSV writers from the fusion service as part of the HDF5 transition. The service was working correctly with HDF5-only output before the restart.

**Files Modified in Last Session**:

- `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/core/multi_broadcast_fusion.py`
  - Removed `_init_fusion_csv()` and `_init_tec_csv()` methods (97 lines)
  - Removed CSV write from `_write_fused_result()` (36 lines)
  - Updated logging to reference HDF5 output

**Likely Cause**: Python initialization error preventing main loop from starting. The complete absence of log output suggests the error occurred before logging was configured or in a critical initialization path.

**Next Steps**:

1. Check full logs: `sudo journalctl -u timestd-fusion -n 200`
2. Look for Python tracebacks or import errors
3. Verify the deployed code matches the git repository
4. Check if there are any missing dependencies or broken imports

---

## Recent Accomplishments (2026-01-02)

### ✅ HDF5 Transition Complete for Core Services

Successfully transitioned both core timing services from CSV to HDF5-only output:

**Fusion Service** (`multi_broadcast_fusion.py`):

- Removed CSV initialization methods (97 lines)
- Removed CSV write calls (36 lines)
- Verified HDF5-only operation (before restart issue)

**Analytics Service** (`phase2_analytics_service.py`):

- Removed clock_offset CSV writer (61 lines total)
- Removed `_init_clock_offset_csv()` method
- Removed CSV write block from `_write_clock_offset()`
- Removed daily rotation logic
- **Status**: ✅ Working correctly, writing HDF5-only

**Verification Script** (`scripts/verify_pipeline.sh`):

- Removed all CSV checks (60+ lines)
- Updated to verify HDF5-only operation
- Script now focuses on HDF5 file presence and freshness

### Data Coverage Audit

Performed comprehensive audit confirming all scientific data has HDF5 coverage:

**L2 Timing Measurements** (Analytics):

- Clock offset with full ISO GUM uncertainty budget (8 components)
- Carrier power, SNR, Doppler (carrier + std)
- Phase variance, coherence time
- WWV/WWVH tone power
- Quality grades and traceability chain

**L3 Fusion** (Fusion):

- Fused clock offset
- Multi-station fusion with uncertainty propagation
- Quality metadata

**L3 Science** (Science Aggregator):

- TEC estimates (ionospheric)
- GNSS VTEC

**Conclusion**: All core timing and ionospheric measurements are in HDF5. CSV files remain on disk but are no longer updated.

---

## System Architecture Overview

### Core Services

1. **timestd-core-recorder**: Records raw IQ data to Digital RF HDF5
2. **timestd-analytics**: L2 timing measurements (HDF5-only as of 2026-01-02)
3. **timestd-fusion**: L3 fused timing (HDF5-only, currently broken)
4. **timestd-science-aggregator**: TEC estimation (HDF5 + CSV fallback)
5. **timestd-vtec**: GNSS VTEC (HDF5 + CSV fallback)
6. **timestd-web-ui**: Monitoring dashboard

### Data Flow

```
Raw IQ (L0) → Analytics (L2) → Fusion (L3) → Chrony SHM
                    ↓
            Science Aggregator (L3 TEC)
```

### HDF5 Data Products

**Analytics Service** (4 HDF5 writers):

- `L1A: channel_observables` - Carrier power, SNR, Doppler, tones
- `L1A: tone_detections` - Station ID tone timing
- `L1B: bcd_timecode` - BCD discrimination
- `L2: timing_measurements` - Clock offset + ISO GUM uncertainty

**Fusion Service** (1 HDF5 writer):

- `L3: fusion_timing` - Multi-station fused timing

**Science Aggregator** (1 HDF5 writer):

- `L3: tec` - Ionospheric TEC estimates

---

## Known Issues

### Critical

1. **Fusion Service Silent Failure** (NEW - 2026-01-02)
   - Service running but not producing output
   - No log entries after startup
   - Likely Python initialization error
   - **Priority**: CRITICAL - blocks Chrony integration

### Active

1. **Chrony Integration Not Working**
   - TMGR source configured but Reach=0
   - Fusion service not updating Chrony SHM
   - Related to issue #1 above

2. **Calibration State Stale**
   - Last updated 1+ hour ago
   - May be related to analytics service restart

---

## Development Guidelines

### HDF5 Best Practices

1. **SWMR Mode**: All HDF5 files use Single-Writer Multiple-Reader mode
2. **Atomic Writes**: Use temp files + rename for atomic updates
3. **Schema Validation**: DataProductWriter validates against schemas
4. **Error Handling**: Track HDF5 write failures with counters

### Code Deployment

**Production Environment**:

- Installed package: `/opt/hf-timestd/venv/lib/python3.11/site-packages/`
- Services run as `timestd` user
- Data root: `/var/lib/timestd/`

**Deployment Process**:

1. Edit files in `/home/mjh/git/hf-timestd/`
2. Copy to production: `sudo cp <src> /opt/hf-timestd/venv/lib/python3.11/site-packages/<dst>`
3. Restart service: `sudo systemctl restart timestd-<service>`
4. Verify: Check logs and HDF5 file timestamps

### Debugging Services

**View Logs**:

```bash
sudo journalctl -u timestd-<service> -n 100
sudo journalctl -u timestd-<service> --since "10 minutes ago"
```

**Check HDF5 Files**:

```bash
ls -lh /var/lib/timestd/phase2/fusion/*.h5
stat <file.h5> | grep Modify
```

**Verify Service Status**:

```bash
systemctl status timestd-<service>
ps aux | grep <service>
```

---

## Next Session Priorities

1. **CRITICAL**: Diagnose and fix fusion service silent failure
   - Check for Python tracebacks in logs
   - Verify code deployment
   - Test fusion service initialization
   - Restore HDF5 writing capability

2. **HIGH**: Fix Chrony integration (depends on #1)
   - Verify Chrony SHM updates after fusion fix
   - Check reach value increases

3. **MEDIUM**: Update CHANGELOG.md with HDF5 transition
   - Document CSV removal
   - Note breaking changes (CSV no longer updated)

4. **LOW**: Optional cleanup
   - Delete legacy CSV writer files if desired
   - Archive old CSV data

---

## File Locations

**Core Services**:

- Fusion: `src/hf_timestd/core/multi_broadcast_fusion.py`
- Analytics: `src/hf_timestd/core/phase2_analytics_service.py`
- Science Aggregator: `src/hf_timestd/core/science_aggregator.py`

**HDF5 Infrastructure**:

- Writers: `src/hf_timestd/io/data_product_writer.py`
- Readers: `src/hf_timestd/io/data_product_reader.py`
- Schemas: `src/hf_timestd/io/schemas/`

**Scripts**:

- Verification: `scripts/verify_pipeline.sh`
- Health checks: `scripts/health-check-*.sh`

**Data Directories**:

- Raw IQ: `/var/lib/timestd/raw_buffer/`
- L2 Analytics: `/var/lib/timestd/phase2/{CHANNEL}/`
- L3 Fusion: `/var/lib/timestd/phase2/fusion/`
- L3 Science: `/var/lib/timestd/phase2/science/`
