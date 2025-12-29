# HF-TimeStd Development Context

## Current Session Summary (2025-12-29)

### Major Accomplishment: GNSS VTEC Integration ✅

Successfully integrated real-time GNSS VTEC (Vertical Total Electron Content) measurements into the HF timing fusion pipeline. The system now uses local ionospheric measurements from a ZED-F9P GNSS receiver to improve timing accuracy.

**Production Status:**

- VTEC Service: Operational, producing 53 TECU from 6 satellites at 1 Hz
- Fusion Integration: **VERIFIED WORKING** - applying ionospheric corrections
- Timing Quality: Achieving **Grade A** with VTEC corrections
- Correction Impact: ~0.4ms ionospheric delay corrections being applied

**Example Production Output:**

```
WWV 10.0MHz: Model=20.0TECU Iono=0.269ms -> GNSS=53.3TECU Iono=0.717ms | Corr=-0.448ms
Fused D_clock: -0.565 ms ± 0.220 ms [2992 broadcasts, grade A]
```

## Next Session Objective: Complete CSV → HDF5 Conversion

### Background

The system currently uses a **hybrid data storage approach**:

- **HDF5**: L1A (channel observables), L1B (BCD timecode), L2 (timing measurements) - with metrological provenance
- **CSV**: Legacy format still being read as fallback in some code paths

The fusion service shows messages like:

```
INFO: Read 1161 L2 timing measurements from HDF5
INFO: Fallback: Read 1161 measurements from CSV
```

This indicates the system is reading HDF5 but still falling back to CSV, suggesting incomplete migration.

### Goals for Next Session

1. **Audit CSV Usage**: Identify all remaining CSV read/write operations in the codebase
2. **Complete HDF5 Migration**: Ensure all data flows use HDF5 exclusively
3. **Remove CSV Fallbacks**: Eliminate CSV fallback logic once HDF5 is proven reliable
4. **Verify Data Integrity**: Confirm no data loss during the transition
5. **Update Documentation**: Document the final HDF5-only architecture

### Key Files to Review

**Data I/O:**

- `src/hf_timestd/io/hdf5_reader.py` - HDF5 reading logic
- `src/hf_timestd/io/hdf5_writer.py` - HDF5 writing logic
- `src/hf_timestd/core/multi_broadcast_fusion.py` - Fusion data reading (lines ~1300-1400)

**Analytics:**

- `src/hf_timestd/services/phase2_analytics_service.py` - L2 measurement generation

**Schemas:**

- `schemas/l1a_channel_observables_v1.json`
- `schemas/l1b_bcd_timecode_v1.json`
- `schemas/l2_timing_measurements_v1.json`

### Known CSV/HDF5 Transition Issues

1. **Fallback Logic**: The fusion service reads HDF5 but still has CSV fallback code
2. **Dual Writes**: Analytics may be writing to both CSV and HDF5
3. **Legacy Paths**: Some services might still reference CSV file paths

### Critical Deployment Note

**IMPORTANT**: When deploying code changes to production:

1. Edit code in the **repo** (`/home/mjh/git/hf-timestd/src/`)
2. **Reinstall the package**: `sudo /opt/hf-timestd/venv/bin/pip install .`
3. **Restart services**: `sudo systemctl restart timestd-<service>`

The production services load code from `/opt/hf-timestd/venv/lib/python3.11/site-packages/`, NOT from `/opt/hf-timestd/src/`. Simply copying files to `/opt/hf-timestd/src/` has NO effect!

## System Architecture Overview

### Hardware Setup (Documented in docs/time-vtec.md)

**Split-Path Design:**

- **Time Path** (UART + PPS): Low-latency NMEA for Chrony/NTP
- **Science Path** (USB): High-bandwidth UBX binary for VTEC calculations
- **ZED-F9P Configuration**:
  - Port 2001: NMEA only (time)
  - Port 9000: UBX RXM-RAWX + NAV-SAT (science)

### Software Services

**Core Pipeline:**

1. `timestd-core-recorder` - Receives RTP from ka9q-radio, writes raw data
2. `timestd-analytics` - Processes raw data → L1A/L1B/L2 HDF5 files
3. `timestd-fusion` - Fuses multi-broadcast measurements → D_clock
4. `timestd-vtec` - Acquires GNSS VTEC data for ionospheric corrections

**Data Flow:**

```
RTP (ka9q) → Core Recorder → Raw Data (HDF5)
                                    ↓
                            Analytics Service
                                    ↓
                    L1A/L1B/L2 Measurements (HDF5)
                                    ↓
                            Fusion Service ← VTEC Data (CSV)
                                    ↓
                            D_clock → Chrony SHM
```

### Configuration

**Production Config**: `/etc/hf-timestd/timestd-config.toml`
**Data Directory**: `/var/lib/timestd/`
**Logs**: `/var/log/hf-timestd/`

## Recent Code Changes

### Files Modified This Session

1. **src/hf_timestd/core/ubx_parser.py** - Fixed RXM-RAWX struct unpacking
2. **src/hf_timestd/core/gnss_tec.py** - Fixed VTEC sign error
3. **src/hf_timestd/core/multi_broadcast_fusion.py** - Added VTEC integration (lines 1741-1815)
4. **scripts/live_vtec.py** - Enhanced debug logging
5. **scripts/install.sh** - Conditional VTEC service installation

### Deployment Status

All changes have been:

- ✅ Committed to working directory
- ✅ Installed to production virtualenv
- ✅ Services restarted
- ✅ Verified operational

**NOT YET**: Changes have not been committed to git or pushed to remote.

## Development Workflow

### Testing Changes

**Local Testing:**

```bash
cd /home/mjh/git/hf-timestd
PYTHONPATH=src venv/bin/python scripts/live_vtec.py --config config/timestd-config.toml
```

**Production Deployment:**

```bash
cd /home/mjh/git/hf-timestd
sudo /opt/hf-timestd/venv/bin/pip install .
sudo systemctl restart timestd-<service>
sudo journalctl -u timestd-<service> -f  # Monitor logs
```

### Debugging Tips

1. **Check Service Status**: `sudo systemctl status timestd-<service>`
2. **View Logs**: `sudo tail -f /var/log/hf-timestd/<service>.log`
3. **Verify Module Loading**:

   ```bash
   sudo /opt/hf-timestd/venv/bin/python -c "import hf_timestd.core.multi_broadcast_fusion as mbf; print(mbf.__file__)"
   ```

4. **Clear Python Cache**: `sudo find /opt/hf-timestd/venv -name "*.pyc" -delete`

## Key Learnings from This Session

1. **Package Installation is Critical**: Production services load from site-packages, not src/
2. **VTEC Integration Works**: Real-time ionospheric corrections are being applied
3. **Debug Logging Levels**: Fusion runs with DEBUG level, but some messages still don't appear
4. **CSV/HDF5 Hybrid**: System is in transition state - next session should complete this

## Questions for Next Session

1. Why does fusion still fall back to CSV after reading HDF5?
2. Are analytics services writing to both CSV and HDF5?
3. Can we safely remove all CSV fallback code?
4. What's the performance impact of HDF5 vs CSV?

---

**Last Updated**: 2025-12-29
**Next Session Focus**: Complete CSV → HDF5 migration
