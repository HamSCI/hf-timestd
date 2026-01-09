# NEVER CHANGE THE FOLLOWING PRIMARY INSTRUCTION

Primary Instruction:  In this context you will perform a critical review of the HF Time Standard (hf-timestd) project, either in its entirety or in a specific component, as specified by the user.  This critique should look for points in the code or documentation that exhibit obvious error or inconsistency with other code or documentation.  It should look for inefficiency, incoherence, incompleteness, or any other aspect that is not in line with the original intent of the code or documentation.  It should also look for obsolete, deprecated, or "zombie" code that should be removed.  Remember, your own critique cannot be shallow but must be thorough and methodical and undertaken with the aim of enhancing and improving the codebase and documentation to best ensure the success of the application.

# The following secondary instruction and information will guide your critique in this particular session (the instructions below will vary from session to session)

---

## 🔴 CURRENT FOCUS: PIPELINE TRACING & VERIFICATION (RTP -> CHRONY)

**Purpose:** Carefully trace the entire data and calculation pipeline from the raw RTP stream input through to the final Chrony feed output to verify data integrity, timestamp accuracy, and processing efficiency.

**Author:** Michael James Hauan (AC0G) / AI Assistant
**Date:** 2026-01-09
**Status:** 🟡 Ready for Pipeline Audit

---

### SESSION OBJECTIVES

1. **Trace Data Flow**: Follow the path of a signle second of data:
    - `RTP Stream` -> `Audio Buffer` -> `L1 Tone Detection` -> `L2 Timing Measurement` -> `L3 Physics (TEC)` -> `Fusion` -> `Chrony SHM`.
2. **Verify Timestamp Integrity**: Ensure UTC alignment is maintained at every serialization/deserialization step (especially HDF5).
3. **Validate Mode-Aware Logic**: Confirm that the recent "Mode-Aware TEC" fix is correctly propagating `propagation_mode` metadata through to Fusion.
4. **Audit Constraints**: Verify that `TEC >= 0` clamps and other physical constraints are active and logged correctly.
5. **Check Latency Handling**: Validate that the new 3-minute polling lag in `timestd-physics` is sufficient and not excessive.

---

### DATA PIPELINE ARCHITECTURE OVERVIEW

The `hf_timestd.core` module implements a multi-phase data pipeline:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 1: RAW DATA CAPTURE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  RTP Stream (ka9q-radio)                                                    │
│       ↓                                                                     │
│  RTPReceiver → PacketResequencer → RecordingSession → BinaryArchiveWriter   │
│       ↓              ↓                    ↓                  ↓              │
│  UDP packets    Gap detection      Minute boundaries    raw_buffer/*.bin    │
│                 Reordering         Session state        + metadata.json     │
└─────────────────────────────────────────────────────────────────────────────┘
       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 2: ANALYTICAL ENGINE                           │
├─────────────────────────────────────────────────────────────────────────────┤
│  Phase2AnalyticsService (Latency: ~3 mins)                                  │
│       ↓                                                                     │
│  Phase2TemporalEngine (3-step analysis)                                     │
│       ├─ Step 1: Time Snap (tone detection, minute boundary)                │
│       ├─ Step 2: Channel Characterization (discrimination, SNR)             │
│       └─ Step 3: Transmission Time Solution (D_clock calculation)           │
│       ↓                                                                     │
│  HDF5 L2 Writer (Schema v1.3.0)                                             │
│       ↓                                                                     │
│  /var/lib/timestd/phase2/{CHANNEL}/clock_offset/*_timing_measurements_*.h5  │
└─────────────────────────────────────────────────────────────────────────────┘
       ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                        PHASE 3: PHYSICS & FUSION                            │
├─────────────────────────────────────────────────────────────────────────────┤
│  PhysicsFusionService (Latency: ~3-6 mins)                                  │
│       ↓                                                                     │
│  TECEstimator (Mode-Aware)                                                  │
│       ├─ Input: L2 data grouped by (Station, PropagationMode)               │
│       ├─ Process: Linear regression on delay vs 1/f^2                       │
│       └─ Output: valid TEC >= 0, or clamped to 0.0                          │
│       ↓                                                                     │
│  MultiBroadcastFusion                                                       │
│       ├─ Input: L3 TEC data + L2 Timing                                     │
│       ├─ Process: Inverse variance weighting, outlier rejection             │
│       └─ Output: Final D_clock estimate                                     │
│       ↓                                                                     │
│  ChronySHM -> /dev/shm/chrony.shm (System Clock Discipline)                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### CRITICAL FILES FOR REVIEW

#### 1. Input & L1 Processing

- `src/hf_timestd/core/stream_recorder_v2.py`: RTP handling and audio buffering.
- `src/hf_timestd/core/tone_detector.py`: Digital Down Conversion (DDC) and matched filtering.

#### 2. L2 Analysis & Timestamping

- `src/hf_timestd/core/phase2_temporal_engine.py`: Core timing logic.
- `src/hf_timestd/core/phase2_analytics_service.py`: Service orchestration and HDF5 writing.

#### 3. L3 Physics & Fusion

- `src/hf_timestd/core/physics_fusion_service.py`: **[RECENTLY MODIFIED]** Mode grouping and polling logic.
- `src/hf_timestd/core/tec_estimator.py`: **[RECENTLY MODIFIED]** TEC calculation and zero-clamping.
- `src/hf_timestd/core/multi_broadcast_fusion.py`: Final fusion and Chrony feed.

---

### RECENT CHANGES TO VERIFY (v5.2.0)

1. **Mode-Aware Grouping**:
    - `PhysicsFusionService.process_minute` now groups by `(station, mode)`.
    - Check that this grouping preserves valid data and doesn't discard legitimate mixed-mode signals (though they shouldn't be mixed in calculation).

2. **Zero-TEC Clamping**:
    - `TECEstimator.estimate_tec` forces `m = 0.0` if regression slope is negative.
    - Verify this logic isn't masking actual system errors (e.g., inverted timestamps).

3. **Polling Lag**:
    - `PhysicsFusionService` now looks back `range(6, 2, -1)` minutes.
    - Confirm this window is optimal—too short = gaps, too long = latency.

---

### KNOWN ISSUES / WATCHLIST

- **Analytics Latency**: The 3-minute lag is a heuristic. If analytics slows down (CPU load), physics might miss data again.
- **HDF5 Locking**: Ensure SWMR (Single Writer Multiple Reader) is working correctly during high-concurrency trace.

---

**Last Updated:** 2026-01-09 19:50 UTC
