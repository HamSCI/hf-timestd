# Project Context: HF Time Standard (hf-timestd)

## 🚀 Current Status: Three-Phase Operational Architecture (v5.2.0-dev)

**Version**: v5.2.0-dev (2026-01-12)
**Core Philosophy**: **Ionospheric Science First**. The system uses the local GPSDO as a "steel ruler" to measure ionospheric path dynamics. UTC recovery is a derived validation product.

### Critical Architectural Evolution (2026-01-12)

**NEW**: Unified three-phase operational model replacing fragmented phase systems:

#### Three-Phase Operational Model

**BOOTSTRAP (0-10 min)**: Establish global RTP-to-UTC offset
* Objective: Learn propagation delays from unambiguous signals
* Method: Anchor channels (CHU 3.33/7.85/14.67, WWV 20/25) + WWVH 1200 Hz tone
* Search window: ±500ms (wide, unknown propagation)
* Discrimination: Physics-based (frequency/modulation), no voting
* Output: D_clock ±5ms, initial station delay models

**REFINEMENT (10-30 min)**: Refine timing to ±1ms
* Objective: Validate and improve timing accuracy
* Method: Timing validation against expected delays
* Search window: ±5ms (narrow, centered on expected)
* Discrimination: Geographic constraints (physics-based rejection)
* Output: D_clock ±1ms, validated station assignments

**MEASUREMENT (30+ min)**: Operational ionospheric measurement
* Objective: Measure all 17 broadcasts independently
* Method: Multi-channel extraction from temporal windows
* Search window: ±1ms (tight, sub-ms precision)
* Discrimination: Not needed (temporal separation sufficient)
* Output: Independent metrics for each broadcast (variations = physics)

### Architecture Overview

The system operates as a **Three-Phase Federated Kalman Architecture**:

#### Stage 1: Federated Analytics (ACTIVE, BEING REFACTORED)

* **Objective**: Track ionospheric path dynamics for every detectable signal.
* **Mechanism**: 17 independent `BroadcastKalmanFilter` instances.
* **NEW**: Driven by `OperationalPhaseManager` for phase-dependent behavior.
* **Adaptive Logic**: Filters adjust search windows based on operational phase and innovation.
* **State Vector**: `[ToF, Doppler]` (Time of Flight in ms, Rate of Change in ms/min).
* **Output**: HDF5 L2 Timing Measurements (Schema v1.3.0) containing `tof_kalman_ms`, `doppler_ms_per_min`, and `gpsdo_consistent`.
* **Key Feature**: "Coasting" (predict-only mode) with **State Persistence** to maintain long-term convergence during signal blackouts.

#### Stage 2: Physics-Based Fusion (ACTIVE)

* **Objective**: Derive physical parameters and feed Chrony.
* **Products**:
  * **Chrony Feed**: Real-time SHM updates based on statistically fused clock offset (NOW RESTORED & SELF-HEALING).
  * **TEC Estimation**: Differential ToF analysis ($ToF_{Low} - ToF_{High}$).
* **Mechanism**: `timestd-fusion` service aggregating Stage 1 states.

#### Stage 3: Multi-Channel Science Products (NEXT STEP)

* **Objective**: Extract independent measurements for all 17 broadcasts.
* **Mechanism**: Multi-channel extraction in MEASUREMENT phase.
* **NEW**: Replaces single-station discrimination with temporal separation.

### Recent Changes

* **v5.2.0-dev (2026-01-12)**: **ARCHITECTURAL REFACTORING**
  * **Unified Phase System**: Created `OperationalPhaseManager` as single source of truth for system-wide operational phase (BOOTSTRAP → REFINEMENT → MEASUREMENT).
  * **Simplified Discrimination**: Replaced weighted voting (~650 lines) with physics-based `StationIdentifier` (~450 lines).
  * **Key Principle**: "Don't guess when you can't discriminate with certainty" - skip ambiguous measurements during bootstrap.
  * **Code Reduction**: Net ~350 lines removed (added 1200, removing ~1550 of legacy complexity).
  * **Status**: Core modules implemented, integration pending.
  * **See**: `SESSION_2026-01-12_UNIFIED_PHASE_ARCHITECTURE.md`, `SESSION_2026-01-12_SIMPLIFIED_DISCRIMINATION.md`

* **v5.1.1 (2026-01-09)**:
  * **Chrony Feed**: Restored by allowing Grade C (Uncertainty < 2.0ms) results.
  * **Self-Healing**: Added `timestd-chrony-monitor` service to auto-restart Chrony on SHM failure.
  * **Pipeline Check**: Updated `verify_pipeline.sh` (Added Web API, Fixed Chrony Reach Bug).
  * **Architecture**: Replaced deprecated `timestd-science-aggregator` with active `timestd-physics` in startup scripts (`install.sh`).

* **v5.1.0 (2026-01-08)**:
  * **Adaptive Windows**: Kalman filters drive tone detector search windows.
  * **Persistence Fix**: Added state saving during signal loss to prevent "amnesia" on restarts.

### Important Files

**NEW (2026-01-12)**:
* `src/hf_timestd/core/operational_phase_manager.py`: Unified phase system (750 lines).
* `src/hf_timestd/core/station_identifier.py`: Physics-based discrimination (450 lines).
* `docs/design/UNIFIED_OPERATIONAL_PHASE_SYSTEM.md`: Complete design specification.
* `docs/design/SIMPLIFIED_DISCRIMINATION_ARCHITECTURE.md`: Discrimination simplification design.

**Existing**:
* `src/hf_timestd/core/broadcast_kalman_filter.py`: Innovation-based convergence logic.
* `src/hf_timestd/core/phase2_analytics_service.py`: Federated filter host & HDF5 writer (TO BE REFACTORED).
* `src/hf_timestd/core/multi_broadcast_fusion.py`: Fusion engine & Chrony interface.
* `src/hf_timestd/core/wwvh_discrimination.py`: Weighted voting (TO BE DEPRECATED).
* `src/hf_timestd/core/timing_discrimination.py`: Timing validation (TO BE SIMPLIFIED).

### Active State / Known Issues

#### ✅ RESOLVED: Chrony Feed

The fusion service is now correctly updating the Chrony SHM using Grade C measurements.
**Self-Healing**: System will auto-restart `chronyd` if Reach drops to 0.

#### ⚠️ VERIFY: Kalman Persistence

A hotfix was deployed to save Kalman states during signal loss ("predict mode").
**Verification Pending**: Check `/var/lib/timestd/state/broadcast_kalman_states/` after ~10-20 minutes of operation to confirm file creation.

### Next Session Goals

#### PRIMARY OBJECTIVE: Integrate Unified Phase Architecture

**Goal**: Wire up `OperationalPhaseManager` and `StationIdentifier` into `Phase2AnalyticsService` for phase-dependent behavior.

**Phase 1: Non-Breaking Integration (Observe Mode)**

1. **Initialize Phase Manager**:
   * Add `OperationalPhaseManager` to `Phase2AnalyticsService.__init__`
   * State file: `/var/lib/timestd/state/operational_phase.json`
   * Wire up to existing subsystems (timing_calibrator, engine)

2. **Initialize Station Identifier**:
   * Add `StationIdentifier` to `Phase2AnalyticsService.__init__`
   * Pass `operational_phase_manager` for phase-dependent behavior
   * Run in parallel with existing `wwvh_discrimination` for validation

3. **Wire Up Metric Updates**:
   * After each measurement, call `operational_phase_manager.update_metrics()`
   * Track station detections, D_clock convergence, delay model quality
   * Log phase transitions (BOOTSTRAP → REFINEMENT → MEASUREMENT)

4. **Validation**:
   * Compare `StationIdentifier` results with existing discrimination
   * Monitor phase transitions on live data
   * Verify no regressions in timing accuracy

**Phase 2: Cutover (Breaking Changes - Future Session)**

1. Replace existing discrimination with `StationIdentifier`
2. Remove weighted voting from `wwvh_discrimination.py`
3. Simplify `timing_discrimination.py` to timing validation only
4. Remove duplicate phase tracking from `TimingCalibrator`

**Phase 3: Multi-Channel Extraction (Future Session)**

1. Implement multi-channel extraction for MEASUREMENT phase
2. Extract all 17 broadcasts independently from temporal windows
3. Update data products schema: `DiscriminationResult` → `MultiChannelMeasurement`

**Key Files to Modify**:
* `src/hf_timestd/core/phase2_analytics_service.py` (main integration point)
* `src/hf_timestd/core/phase2_temporal_engine.py` (add identifier alongside discriminator)

**Key Principle**: Start in observe mode, validate behavior, then cutover. Don't break existing functionality.
