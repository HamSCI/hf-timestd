# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: Adaptive Physics-Based Timing (v5.1.0)

**Version**: v5.1.0
**Core Philosophy**: **Ionospheric Science First**. The system uses the local GPSDO as a "steel ruler" to measure ionospheric path dynamics. UTC recovery is a derived validation product.

### Architecture Overview

The system operates as a **Federated Two-Stage Kalman Architecture**:

#### Stage 1: Federated Analytics (ACTIVE)

* **Objective**: Track ionospheric path dynamics for every detectable signal.
* **Mechanism**: 17 independent `BroadcastKalmanFilter` instances.
* **Adaptive Logic**: Filters dynamically adjust search windows (±500ms ↔ ±3ms) based on **Innovation-Based Convergence**.
* **State Vector**: `[ToF, Doppler]` (Time of Flight in ms, Rate of Change in ms/min).
* **Output**: HDF5 L2 Timing Measurements (Schema v1.3.0) containing `tof_kalman_ms`, `doppler_ms_per_min`, and `gpsdo_consistent`.
* **Key Feature**: "Coasting" (predict-only mode) with **State Persistence** to maintain long-term convergence during signal blackouts.

#### Stage 2: Physics-Based Fusion (ACTIVE)

* **Objective**: Derive physical parameters and feed Chrony.
* **Products**:
  * **Chrony Feed**: Real-time SHM updates based on statistically fused clock offset (NOW RESTORED).
  * **TEC Estimation**: Differential ToF analysis ($ToF_{Low} - ToF_{High}$).
* **Mechanism**: `timestd-fusion` service aggregating Stage 1 states.

#### Stage 3: Science Products (NEXT STEP)

* **Objective**: Extract pure science data (Doppler, Dispersion, Test Signals).
* **Mechanism**: Verify and visualize HDF5 data products.

### Recent Changes

* **v5.1.0 (2026-01-08)**:
  * **Adaptive Windows**: Kalman filters drive tone detector search windows.
  * **Chrony Restored**: Fixed `CROSS_STATION_DISAGREE` bug; Chrony now syncing to TMGR.
  * **Persistence Fix**: Added state saving during signal loss to prevent "amnesia" on restarts.
* **v5.0.1 (2026-01-08)**:
  * **Tone Detection Fix**: Solved 24kHz template regression; restored 100% detection.

### Important Files

* `src/hf_timestd/core/broadcast_kalman_filter.py`: Innovation-based convergence logic.
* `src/hf_timestd/core/phase2_analytics_service.py`: Federated filter host & HDF5 writer.
* `src/hf_timestd/core/multi_broadcast_fusion.py`: Fusion engine & Chrony interface.

### Active State / Known Issues

#### ✅ RESOLVED: Chrony Feed

The fusion service is now correctly updating the Chrony SHM. `chronyc sources` shows `TMGR` as the selected source (`#*`).

#### ⚠️ VERIFY: Kalman Persistence

A hotfix was deployed to save Kalman states during signal loss ("predict mode").
**Verification Pending**: Check `/var/lib/timestd/state/broadcast_kalman_states/` after ~10-20 minutes of operation to confirm file creation.

### Next Session Goals

#### PRIMARY OBJECTIVE: Science Product Verification

**Goal**: Validate the scientific outputs now that the pipeline is stable.

1. **TEC Estimation**:
    * Verify `verify_dispersion.py` shows positive slopes (physics compliance).
    * Confirm TEC plots in Web UI are populated.
2. **Test Signals**:
    * Inspect L2 Test Signal HDF5 files (`*_test_signal_*.h5`).
    * Verify capturing of 400Hz/1000Hz modulation tones and field strength.
3. **Doppler Analysis**:
    * Analyze `doppler_ms_per_min` in L2 files.
    * Correlate with sunrise/sunset transition.

#### SECONDARY OBJECTIVE: Web UI Science Dashboard

1. Create/Update dashboards to visualize:
    * Real-time TEC (Total Electron Content)
    * Doppler Shifts (Layer movement)
    * Signal Multipath/Fading stats
