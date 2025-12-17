# HF Time Standard Analysis (hf-timestd) - AI Context Document

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-17  
**Version:** 5.3 (Unified Propagation Model + Calibration Persistence + Web UI Prep)

---

## Project Scope

`hf-timestd` records and analyzes HF time standard stations:

- **WWV** (Ft. Collins, CO) - 2.5, 5, 10, 15, 20, 25 MHz
- **WWVH** (Kauai, HI) - 2.5, 5, 10, 15 MHz (shared with WWV)
- **CHU** (Ottawa, Canada) - 3.33, 7.85, 14.67 MHz
- **BPM** (Pucheng, China) - 2.5, 5, 10, 15 MHz (shared with WWV/WWVH)

The repository implements a **two-phase** pipeline for time-transfer analytics.

**Primary Output:** `D_clock = T_system - T_UTC(NIST)` with sub-millisecond accuracy.

**17 Broadcasts Total:**
- WWV: 6 frequencies (2 unique: 20, 25 MHz; 4 shared: 2.5, 5, 10, 15 MHz)
- WWVH: 4 frequencies (all shared: 2.5, 5, 10, 15 MHz)
- CHU: 3 frequencies (unique: 3.33, 7.85, 14.67 MHz)
- BPM: 4 frequencies (all shared: 2.5, 5, 10, 15 MHz)

**Channel Naming Convention:**
- `SHARED X MHz` - Frequencies where WWV, WWVH, and BPM all broadcast (2.5, 5, 10, 15 MHz)
- `WWV X MHz` - WWV-only frequencies (20, 25 MHz)
- `CHU X MHz` - CHU-only frequencies (3.33, 7.85, 14.67 MHz)

**Explicit Non-Goals:**
- DigitalRF format conversion
- Decimation / 10 Hz products
- PSWS/HamSCI uploads
- Phase 3 derived-product generation

---

## Architecture

### Phase 1: Immutable raw_buffer (Binary IQ)

Phase 1 is the scientific record. It stores raw complex IQ with **system time only** (no UTC correction).

**Directory layout:**
```
{data_root}/raw_buffer/{CHANNEL_DIR}/{YYYYMMDD}/
    {minute_boundary}.bin[.zst|.lz4]
    {minute_boundary}.json
```

**Key invariants:**
- Path mapping uses `channel_name_to_dir()`
- Files are minute-aligned
- No decimation

**Implementation:**
- `src/hf_timestd/core/binary_archive_writer.py`
- `src/hf_timestd/core/pipeline_orchestrator.py`

### Phase 2: Analytics (D_clock Extraction)

Phase 2 reads Phase 1 `raw_buffer` and produces timing products.

**Output layout:**
```
{data_root}/phase2/{CHANNEL_DIR}/
    clock_offset/       # D_clock time series (PRIMARY OUTPUT)
    discrimination/     # WWV vs WWVH results
    tone_detections/    # 1000/1200 Hz detection
    bcd_correlation/    # BCD subcarrier analysis
    status/             # Service state for web UI
```

**Key entry points:**
- `src/hf_timestd/core/phase2_temporal_engine.py` - Central orchestrator
- `src/hf_timestd/core/phase2_analytics_service.py` - Daemon wrapper
- `src/hf_timestd/core/multi_broadcast_fusion.py` - 13-broadcast fusion

---

## The D_clock Equation

```
T_arrival = T_emission + T_propagation + D_clock

Where:
  T_arrival     = Observed tone arrival time (matched filter detection)
  T_emission    = 0 (tones transmitted at exact second boundary)
  T_propagation = HF signal propagation delay (ionospheric path)
  D_clock       = System clock offset (THE OUTPUT WE WANT)

Rearranging:
  D_clock = T_arrival - T_propagation
```

**Key Insight:** With a GPSDO (10⁻⁹ stability), the local clock doesn't drift measurably in hours. Minute-to-minute D_clock variations are therefore NOT clock error—they are **IONOSPHERIC PROPAGATION EFFECTS** that we want to measure!

---

## Three-Step Refinement Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ STEP 1: TIME SNAP + MULTI-STATION DETECTION                                 │
│   Files: tone_detector.py, multi_station_detector.py                        │
│   Method: Quadrature matched filter for 800ms timing tones                  │
│   Output: ALL detected stations (WWV, WWVH, BPM, CHU) with ToA/SNR          │
│   Key: GPSDO is timing reference, not loudest station                       │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 2: CHANNEL CHARACTERIZATION                                            │
│   2A. BCD Correlation → differential_delay_ms (WWV vs WWVH vs BPM)          │
│   2B. Doppler Estimation → ionospheric motion, channel stability            │
│   2C. Station Discrimination → 8-vote weighted system                       │
│   2D. Test Signal Analysis → FSS, delay spread (minutes 8/44)               │
│   2E. BPM Detection → 10ms tick duration, UT1/UTC mode                      │
│   Note: BCD disabled for BPM pure carrier minutes (10-15, 40-45)            │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 3: TRANSMISSION TIME SOLUTION (→ D_clock)                              │
│   File: transmission_time_solver.py                                         │
│   Method: Mode disambiguation (1E, 1F, 2F, 3F, ground wave)                 │
│   Output: D_clock, propagation_mode, confidence, uncertainty                │
│   All stations passed to fusion with uncertainty weighting                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Multi-Broadcast Fusion

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 17 BROADCASTS → WEIGHTED FUSION → FUSED D_clock                             │
│                                                                             │
│   WWV:  2.5, 5, 10, 15, 20, 25 MHz (6 broadcasts)                          │
│   WWVH: 2.5, 5, 10, 15 MHz (4 broadcasts, shared frequencies)              │
│   CHU:  3.33, 7.85, 14.67 MHz (3 broadcasts, FSK timing reference)         │
│   BPM:  2.5, 5, 10, 15 MHz (4 broadcasts, shared with WWV/WWVH)            │
│                                                                             │
│   Weight = confidence × uncertainty_weight × mode_weight × snr_factor       │
│   Outlier rejection: Weighted MAD, 3σ threshold                             │
│   Auto-calibration: Station-level offsets (not per-broadcast)               │
│                                                                             │
│   Output: phase2/fusion/fused_d_clock.csv → Chrony SHM (rate-limited 8s)   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Files

| File | Purpose |
|------|---------|
| `phase2_temporal_engine.py` | Central orchestrator - 3-step D_clock extraction |
| `station_model.py` | **UPDATED** MLE-based StationModel + ChannelAssignment |
| `multi_station_detector.py` | Physics-based multi-station detection |
| `multi_broadcast_fusion.py` | Combines 17 broadcasts into fused D_clock |
| `bpm_discriminator.py` | **UPDATED** BPM detection - 10ms ticks, UT1/UTC modes |
| `correlator_bank.py` | Parallel matched filtering with predicted ToA windows |
| `transmission_time_solver.py` | Propagation mode disambiguation |
| `verification_script.py` | (Ephemeral) Used to verify calibration persistence |

---

## Recent Session Gains (2025-12-17)

### 1. Unified Propagation Delay Model
- **Problem:** `StationModelFactory` used varying delay logic vs `BPMDiscriminator`, causing 5ms discrepancy.
- **Fix:** Implemented unified **Distance-Dependent Heuristic** in both:
  - Dist < 3000 km: **1.15×** overhead (High angle)
  - Dist > 10000 km: **1.05×** overhead (Grazing incidence)
- **Result:** 0.00 ms discrepancy. BPM search window centered correctly at ~40ms (expected) vs 44ms (old).

### 2. Reliability Enhancements
- **Calibration Persistence:** `Phase2TemporalEngine` now saves/loads BPM calibration (delay/gain) to `timing_calibration.json`. Prevents loss of accumulated data on restart.
- **Pure Carrier Optimization:** Steps skipping BCD correlation (Step 2A) during BPM's pure carrier minutes (10-15, 40-45), reducing noise and CPU usage.

### 3. Chrony SHM Integration Fixed
- **Issue:** Padding error in struct packing.
- **Fix:** Corrected struct format string.

### 4. Documentation Cleanup (2025-12-17)
- **Legacy Archive:** Moved obsolete design docs and migration notes to `docs/archive/`.
- **Deprecations:** Marked `RTPReceiver` and `CoreRecorder` (v1) as deprecated in favor of `CoreRecorderV2`.
- **Docstrings:** Verified Google-style docstrings in active core modules.

---

## Current Focus: Web UI Integration & Concordia

**NEXT SESSION GOAL:** Bring the Web UI into better concordance with the current time analysis state (Phase 2).

### Specific Objectives for Next Session:
1.  **Visualize Multi-Station Detection:**
    - Update `monitoring-server-v3.js` to serve the new multi-station detection data structure.
    - Create/Update UI components to display detected stations (WWV/WWVH/BPM/CHU) concurrently.
    - Show `StationModel` predicted windows vs actual arrivals.

2.  **BPM Status Integration:**
    - Expose BPM-specific fields (`bpm_timing_mode`, `bpm_detected`, `bpm_snr`) in the main dashboard.
    - Visualize the "UT1 Mode" vs "UTC Mode" status.

3.  **Mode Probability Visualization:**
    - Ensure the per-broadcast mode probability (`1F`, `2F`, etc.) is correctly visualized for *each* detected station, not just the dominant one.

### Relevant API Endpoints (To Verify/Implement):
- `GET /api/v1/phase2/multi-station/:channel` (The multi-station data source)
- `GET /api/v1/phase2/status` (Global status)

---

## Configuration & Environment

**timestd-config.toml** is the single source of truth.

**Environment Variables:**
```bash
export TIMESTD_DATA_ROOT=/tmp/timestd-test          # or /var/lib/timestd
export TIMESTD_CONFIG=/etc/hf-timestd/timestd-config.toml
```

**Systemd Services:**
- `timestd-core-recorder.service`
- `timestd-analytics.service`
- `timestd-web-ui.service`
