# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: Science-First Architecture (v5.0.0)

**Version**: v5.0.0
**Core Philosophy**: **Ionospheric Science First**. The system uses the local GPSDO as a "steel ruler" to measure ionospheric path dynamics. UTC recovery is a derived validation product, not the primary input.

### Architecture Overview

The system has transitioned from a "clock recovery" focus to a **Federated Two-Stage Kalman Architecture**:

#### Stage 1: Federated Analytics (COMPLETE)

* **Objective**: Track ionospheric path dynamics for every detectable signal.
* **Mechanism**: 17 independent `BroadcastKalmanFilter` instances (one per station/frequency pair, e.g., WWV-10MHz, CHU-7.85MHz).
* **State Vector**: `[ToF, Doppler]` (Time of Flight in ms, Rate of Change in ms/min).
* **Input**: Raw tone arrival times validated against the GPSDO "steel ruler".
* **Output**: HDF5 L2 Timing Measurements (Schema v1.3.0) containing `tof_kalman_ms`, `doppler_ms_per_min`, and `gpsdo_consistent` flag.
* **Key Feature**: "Coasting" (predict-only mode) during signal fades to maintain state continuity.

#### Stage 2: Physics-Based Fusion (NEXT STEP)

* **Objective**: Derive physical parameters from Stage 1 outputs.
* **Planned Products**:
  * **TEC Estimation**: Differential Time of Flight between frequencies from the same station (e.g., $ToF_{10MHz} - ToF_{20MHz}$) proportional to Total Electron Content.
  * **Triangulation**: Geometric intersection of path lengths from multiple stations (WWV, WWVH, CHU).
* **Mechanism**: A new `PhysicsFusionService` consuming Stage 1 Kalman states.

#### Stage 3: UTC Recovery & Validation (FUTURE)

* **Objective**: Validate the physics model.
* **Mechanism**: "Does the ionospheric model explain the observed delays such that the residual clock error is consistent with UTC(NIST)?"
* **Role**: UTC recovery is a quality metric for the scientific model.

### Recent Changes (v5.0.0 Release)

* **New Core**: Implemented `BroadcastKalmanFilter` module with per-probe tuning (e.g., CHU FSK vs WWV AM).
* **Integration**: Integrated filters into `phase2_analytics_service.py`.
* **Feedback Loop Removed**: Deleted the legacy auto-calibration system (`broadcast_calibration.json`) that was causing circular logic errors.
* **Data Model**: Updated HDF5 schema manually to v1.3.0 to support Kalman states.

### Important Files

* `src/hf_timestd/core/broadcast_kalman_filter.py`: The core per-probe filter logic.
* `src/hf_timestd/core/phase2_analytics_service.py`: The service hosting the federated filters.
* `src/hf_timestd/schemas/l2_timing_measurements_v1.json`: The HDF5 schema definition (v1.3.0).

### Known Issues / Active State

#### ✅ RESOLVED: Analytics Processing Loop

**Fixed in commit 7a46754** (2026-01-07)

The analytics service had two critical bugs preventing continuous operation:

1. **Bootstrap Catch-22**: Continuity check rejected initial measurements because it required a previous measurement that didn't exist during bootstrap.
2. **Processing Loop Stuck**: Service used binary file discovery instead of wall-clock time, causing it to process only once on startup then enter infinite channel discovery loop.

**Fixes Applied**:

* Added bootstrap mode handling to skip continuity check when `last_d_clock_ms is None`
* Changed `_get_latest_minute()` to use wall-clock time as primary source
* Removed `processed_minutes` check to allow continuous processing

**Result**: Analytics service now processes every ~13 seconds, minute boundary advances correctly.

#### ❌ CRITICAL ISSUE: Tone Detection Failure

**Problem**: The `MultiStationToneDetector` only finds timing tones **on service restart**, not during continuous operation.

**Evidence**:

* L2 HDF5 shows measurements only at restart times: 16:10, 17:10, 17:48, 17:53, 18:35 UTC
* All measurements have `TOA: 0.0` (Kalman filter initialization value, not real measurement)
* Between restarts: `Grade: C | Flag: MISSING | TOA: nan`
* Channel SNR is excellent (1.2-10.1 dB across all frequencies)
* Audio tone analysis shows power at 1000/1200 Hz bins (20-30 dB)

**Key Detection Point** (line 1043 in `phase2_temporal_engine.py`):

```python
detections = self.tone_detector.process_samples(
    timestamp=buffer_mid_time,
    samples=iq_samples,
    rtp_timestamp=rtp_timestamp,
    search_window_ms=adaptive_window_ms,
    expected_offset_ms=expected_offset_ms
)
```

This call returns **empty list or None** during continuous operation, but succeeds on service restart.

**Files Involved in Tone Detection**:

1. `src/hf_timestd/core/multi_station_detector.py` - Primary tone detector
2. `src/hf_timestd/core/tone_detector.py` - Lower-level detection logic
3. `src/hf_timestd/core/audio_tone_monitor.py` - 500/600 Hz station ID tones
4. `src/hf_timestd/interfaces/tone_detection.py` - ToneDetector interface
5. `src/hf_timestd/models/tone_detection.py` - Data models
6. `src/hf_timestd/core/phase2_temporal_engine.py` - Orchestrates detection

**Hypotheses**:

1. Search window becomes too narrow after bootstrap, missing tones
2. Detection thresholds too strict for continuous operation
3. Matched filter templates misaligned with actual signal
4. State variable (e.g., `adaptive_window_ms`, `expected_offset_ms`) corrupted after first detection

**Diagnostic Commands**:

```bash
# Check L2 data
python3 inspect_l2.py

# Monitor analytics processing
sudo tail -f /var/log/hf-timestd/phase2-wwv20.log | grep "Calling process_minute"

# Check audio tone analysis
tail -20 /var/lib/timestd/phase2/WWV_20000/audio_tones/WWV_20000_audio_tones_20260107.csv
```

**Impact**: Without continuous tone detection, the entire pipeline stalls:

* No valid L2 timing measurements
* No TEC estimation (Physics Fusion has no input data)
* No L3 physics products
* Web UI dashboard shows "NO DATA"

## Next Session Goals

### PRIMARY OBJECTIVE: Restore Continuous Tone Detection

**Goal**: Fix `MultiStationToneDetector.process_samples()` to find 1000/1200 Hz timing tones continuously, not just on restart.

**Approach**:

1. **Add Debug Logging**: Instrument `process_samples()` to log search windows, thresholds, and detection results
2. **Compare Restart vs Continuous**: Identify what differs between successful (restart) and failed (continuous) detection calls
3. **Check Search Windows**: Verify `adaptive_window_ms` and `expected_offset_ms` are reasonable during continuous operation
4. **Inspect Thresholds**: Review SNR and correlation thresholds in tone detector
5. **Test with Relaxed Thresholds**: Temporarily lower thresholds to confirm tones are present but being rejected

**Success Criteria**:

* L2 HDF5 shows new measurements every minute (not just on restart)
* `raw_arrival_time_ms` contains valid values (not `nan` or `0.0`)
* `quality_flag` shows `GOOD` or `MARGINAL` (not `MISSING`)
* Physics Fusion Service receives valid L2 data and produces TEC estimates

### SECONDARY OBJECTIVE: Enable Physics Fusion Pipeline

Once tone detection is fixed:

1. Verify Physics Fusion Service processes L2 data
2. Confirm TEC estimates appear in L3 HDF5 files
3. Validate Web UI dashboard displays TEC and UTC consistency metrics

## Important Files

* `src/hf_timestd/schemas/l2_timing_measurements_v1.json`: L2 HDF5 schema (v1.3.0)
* `web-api/static/physics.html`: Physics dashboard UI (ready, awaiting data)
