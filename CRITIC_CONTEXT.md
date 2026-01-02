# Critic Context - HF-TimeStd Project Status

**Last Updated**: 2026-01-02 17:43 UTC  
**Session Focus**: ✅ RESOLVED - Fusion Service Failure After HDF5 Transition

## Recent Accomplishments (2026-01-02)

### ✅ Fusion Service Failure RESOLVED

**Issue**: Service running but silent - no log output and no HDF5 writes after HDF5 transition.

**Root Causes Identified**:

1. **Logger Initialization Order Bug** (Line 155 before 161)
   - `logger.warning()` called before `logger = logging.getLogger(__name__)`
   - Caused `NameError` during module import
   - Error occurred before logging configured → complete silence

2. **Orphaned Method Call** (Line 2007)
   - Call to deleted `_write_tec_result()` method
   - Method removed during CSV cleanup
   - TEC writing is handled by `science_aggregator` service

**Fixes Applied**:

- Moved logger initialization before HDF5 import block
- Removed orphaned `_write_tec_result()` call
- Committed: `5dd74cf`

**Verification**:

- ✅ Service running and logging properly
- ✅ HDF5 files being written (last write: 17:42:07)
- ✅ Chrony SHM updates working (reach=3, offset=-2.4ms)
- ✅ Fusion calculations completing (38 broadcasts, grade D)

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

### Active

1. **Calibration State May Be Stale**
   - Check if calibration needs refresh after service restarts
   - Monitor calibration age in logs

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

1. **HIGH**: Update CHANGELOG.md with HDF5 transition and fusion fixes
   - Document CSV removal from fusion and analytics services
   - Document fusion service bug fixes (logger init, orphaned method call)
   - Note breaking changes (CSV no longer updated)

2. **MEDIUM**: Monitor Chrony integration stability
   - Verify TMGR reach continues to increase
   - Monitor fusion service logs for errors
   - Check calibration state freshness

3. **LOW**: Optional cleanup
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
