# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: Adaptive Physics-Based Timing (v5.1.0)

**Version**: v5.1.1
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
  * **Chrony Feed**: Real-time SHM updates based on statistically fused clock offset (NOW RESTORED & SELF-HEALING).
  * **TEC Estimation**: Differential ToF analysis ($ToF_{Low} - ToF_{High}$).
* **Mechanism**: `timestd-fusion` service aggregating Stage 1 states.

#### Stage 3: Science Products (NEXT STEP)

* **Objective**: Extract pure science data (Doppler, Dispersion, Test Signals).
* **Mechanism**: Verify and visualize HDF5 data products.

### Recent Changes

* **v5.1.1 (2026-01-09)**:
  * **Chrony Feed**: Restored by allowing Grade C (Uncertainty < 2.0ms) results.
  * **Self-Healing**: Added `timestd-chrony-monitor` service to auto-restart Chrony on SHM failure.
  * **Pipeline Check**: Updated `verify_pipeline.sh` (Added Web API, Fixed Chrony Reach Bug).
  * **Architecture**: Replaced deprecated `timestd-science-aggregator` with active `timestd-physics` in startup scripts (`install.sh`).
* **v5.1.0 (2026-01-08)**:
  * **Adaptive Windows**: Kalman filters drive tone detector search windows.
  * **Persistence Fix**: Added state saving during signal loss to prevent "amnesia" on restarts.

### Important Files

* `src/hf_timestd/core/broadcast_kalman_filter.py`: Innovation-based convergence logic.
* `src/hf_timestd/core/phase2_analytics_service.py`: Federated filter host & HDF5 writer.
* `src/hf_timestd/core/multi_broadcast_fusion.py`: Fusion engine & Chrony interface.

### Active State / Known Issues

#### ✅ RESOLVED: Chrony Feed

The fusion service is now correctly updating the Chrony SHM using Grade C measurements.
**Self-Healing**: System will auto-restart `chronyd` if Reach drops to 0.

#### ⚠️ VERIFY: Kalman Persistence

A hotfix was deployed to save Kalman states during signal loss ("predict mode").
**Verification Pending**: Check `/var/lib/timestd/state/broadcast_kalman_states/` after ~10-20 minutes of operation to confirm file creation.

### Next Session Goals

### Next Session Goals

#### PRIMARY OBJECTIVE: Align Web API with Physics Service

**Goal**: Bring `web-api` into accord with the new `timestd-physics` service (which replaced `timestd-science-aggregator`).

1. **Verify Physics Integration**:
    * Validate `web-api/services/physics_service.py` reads correctly from `timestd-physics` HDF5 outputs.
    * Ensure path consistency (`phase2/fusion` vs legacy locations).
    * Test `/api/physics/latest` and `/api/physics/history` endpoints.

2. **Legacy Cleanup**:
    * Remove any remaining code/config references to `timestd-science-aggregator`.
    * Ensure `timestd-physics` is the single source of truth for L3 science data (TEC, Dispersion).

3. **Science Product Verification**:
    * Once API is aligned, use it to verify scientific outputs (TEC slopes, Doppler).
    * Update frontend dashboards to consume the aligned API.
