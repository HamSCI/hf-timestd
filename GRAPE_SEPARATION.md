# GRAPE Application Separation Guide

**Date**: 2025-12-15  
**Status**: ✅ COMPLETE - Files removed from hf-timestd

Phase 3 functionality (decimation, spectrograms, PSWS upload) has been fully separated
to the grape-recorder package. The files have been **removed** from this repository.

**New Repository**: https://github.com/mijahauan/grape-recorder  
**Local Path**: `/home/wsprdaemon/grape-recorder`

---

## Files Removed from hf-timestd (Dec 15, 2025)

### Python Modules Removed (were in src/hf_timestd/core/)

**Phase 3 Products (decimation, spectrograms, upload):**
- `decimation.py` - 20 kHz → 10 Hz decimation filters ❌ REMOVED
- `decimated_buffer.py` - 10 Hz binary buffer storage ❌ REMOVED
- `spectrogram_generator.py` - Legacy spectrogram generator ❌ REMOVED
- `carrier_spectrogram.py` - Carrier spectrogram generator ❌ REMOVED
- `phase3_product_engine.py` - Phase 3 product generation ❌ REMOVED
- `phase3_products_service.py` - Phase 3 real-time service ❌ REMOVED
- `daily_drf_packager.py` - PSWS DRF packaging ❌ REMOVED
- `drf_batch_writer.py` - DRF batch writing ❌ REMOVED

**Upload functionality (were in src/hf_timestd/):**
- `uploader.py` - SFTP upload manager ❌ REMOVED
- `upload_tracker.py` - Upload state tracking ❌ REMOVED

### Scripts (scripts/)

These scripts were NOT renamed to timestd-* and should move to grape app:
- `grape-spectrogram.sh` - (deleted, needs recreation in grape app)
- `grape-daily-upload.sh` - (deleted, needs recreation in grape app)
- `grape-products.sh` - (deleted, needs recreation in grape app)
- `grape-phase3.sh` - (deleted, needs recreation in grape app)

Supporting scripts that may need copies:
- `daily-drf-upload.sh` - Daily DRF upload script
- `run_phase3_processor.py` - Phase 3 batch processor
- `generate_daily_spectrograms.sh` - Daily spectrogram generation
- `auto-generate-spectrograms.sh` - Auto spectrogram generation

### Systemd Services (systemd/)

These services were NOT renamed and should move to grape app:
- `grape-spectrograms.service` - (deleted, needs recreation)
- `grape-spectrograms.timer` - (deleted, needs recreation)
- `grape-daily-upload.service` - (deleted, needs recreation)
- `grape-daily-upload.timer` - (deleted, needs recreation)

---

## Files Staying in hf_timestd

### Core Timing Analysis
- `tone_detector.py` - WWV/WWVH/CHU tone detection
- `phase2_temporal_engine.py` - Phase 2 timing analysis
- `phase2_analytics_service.py` - Phase 2 real-time service
- `transmission_time_solver.py` - D_clock calculation
- `wwvh_discrimination.py` - WWV/WWVH discrimination
- `clock_convergence.py` - Kalman filter convergence
- `multi_broadcast_fusion.py` - 13-broadcast fusion
- `chrony_shm.py` - Chrony SHM integration

### Recording Infrastructure
- `core_recorder.py` - Core recorder
- `core_recorder_v2.py` - V2 core recorder
- `raw_archive_writer.py` - Phase 1 DRF writer
- `binary_archive_writer.py` - Binary archive writer

### Supporting
- `wwv_constants.py` - WWV/WWVH/CHU constants
- `ionospheric_model.py` - Propagation modeling
- `gpsdo_monitor.py` - GPSDO state machine
- `quality_metrics.py` - Quality tracking

---

## Separation Complete (Dec 15, 2025)

### What Was Done

1. ✅ Created grape-recorder repository at https://github.com/mijahauan/grape-recorder
2. ✅ Copied Phase 3 files to grape-recorder
3. ✅ Updated imports in grape-recorder to use `grape_recorder` package
4. ✅ **Removed Phase 3 files from hf-timestd** (this session)
5. ✅ Updated stub methods in `analytics_service.py` and `phase2_analytics_service.py`

### Dependency Direction
```
grape-recorder (decimation, spectrograms, upload)
    ↓ depends on
hf-timestd (recording, timing analysis, D_clock)
```

grape-recorder imports from hf-timestd:
```python
from hf_timestd.core import Phase2TemporalEngine, ClockOffsetSeries
```

---

## Notes

- Decimation methods in `analytics_service.py` and `phase2_analytics_service.py` are now
  no-op stubs that return immediately. For 10 Hz output, use grape-recorder.
- Upload functionality requires SSH keys and PSWS credentials (configured in grape-recorder).
- The interfaces in `hf_timestd/interfaces/decimation.py` remain as abstract contracts.
