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

* **Bootstrap**: The system relies on initial tone detection to seed the Kalman filters.
* **Solver**: `TransmissionTimeSolver` is complex; a `calibration_offsets` bug was recenty fixed, but the solver remains a critical dependency.

## Next Session Goals

1. **Implement Physics-Based Fusion**: Build the service to consume the new `tof_kalman_ms` and `doppler_ms_per_min` data.
2. **TEC Estimation**: Implement the math to derive Total Electron Content from differential delays.
3. **Visualisation**: Update the web UI to display these new scientific products (ToF, Doppler, TEC).
