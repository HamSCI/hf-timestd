# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: Science-First Architecture (Phase 2 Complete)

**Version**: v5.0.0 "Science-First"
**Core Philosophy**: The system uses the localized GPSDO as a "steel ruler" to measure the ionosphere. UTC recovery is a derived validation product, not the primary input.

### Recent Achievements (Session 2026-01-07)
1.  **Architecture Redesign**: Shifted from "clock recovery first" to "ionospheric science first".
2.  **BroadcastKalmanFilter**: Implemented 17 independent Kalman filters (one per broadcast) to track `[ToF, Doppler]`.
3.  **Feedback Loop Removed**: Deleted the legacy auto-calibration system that was learning incorrect offsets.
4.  **Integration**: Fully integrated into `phase2_analytics_service.py` with valid HDF5 output (v1.3.0 schema).
5.  **Per-Probe Tuning**: Each filter is tuned for its specific path (e.g., CHU vs WWV) and modulation characteristics.

### Active Architecture (v5.0.0)

**Stage 1: Federated Analytics (COMPLETE)**
- **Input**: Raw tone arrivals from `phase2_temporal_engine`.
- **Processing**: 17 independent `BroadcastKalmanFilter` instances.
- **Output**: `[ToF, Doppler]` state vectors for each broadcast path.
- **Storage**: HDF5 L2 files (Schema v1.3.0).

**Stage 2: Physics-Based Fusion (NEXT)**
- **Goal**: Derive physical parameters from Stage 1 outputs.
- **Products**:
    - **TEC Estimation**: Differential delay between frequencies (`ToF_f1 - ToF_f2`).
    - **Triangulation**: Multi-station geometric intersection.
    - **UTC Validation**: "Does this solution agree with UTC(NIST)?"

**Stage 3: Clock Recovery (FUTURE)**
- **Goal**: Discipline the local clock (if GPSDO fails) using the validated physics model.

## Known Issues
- **Bootstrap Sensitivity**: System relies on initial tone detection to seed the Kalman filters.
- **Solver Complexity**: `TransmissionTimeSolver` is complex; `calibration_offsets` bug was recently fixed but legacy code remains.

## Next Session Goals
1.  **Implement Physics-Based Fusion**: Create the `PhysicsFusionService` (or refactor `FusionService`) to consume Stage 1 Kalman states.
2.  **TEC Estimation**: Implement Total Electron Content calculation using differential ToF from the same station (e.g., WWV 10MHz vs 20MHz).
3.  **UTC Recovery Validation**: Implement the logic to use UTC consistency as a quality metric for the ionospheric model.

---
