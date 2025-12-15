# HF Time Standard Analysis - AI Context Document

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-15  
**Version:** 3.14.0  
**Project:** `hf-timestd` - HF Time Signal Analysis for UTC(NIST) Extraction

---

## 🎯 PROJECT MISSION

Extract **D_clock = T_system - T_UTC(NIST)** from WWV/WWVH/CHU time signal broadcasts with sub-millisecond accuracy using a GPSDO-disciplined SDR receiver.

**Key Insight:** With a GPSDO (10⁻⁹ stability), the local clock doesn't drift measurably in hours. Minute-to-minute D_clock variations are therefore NOT clock error—they are **IONOSPHERIC PROPAGATION EFFECTS** that we want to measure!

---

## 📁 PROJECT STRUCTURE

```
hf-timestd/                          # This repository
├── src/hf_timestd/
│   ├── core/                        # ⭐ TIMING ANALYSIS MODULES (focus here)
│   │   ├── phase2_temporal_engine.py    # Central orchestrator - 3-step D_clock extraction
│   │   ├── multi_broadcast_fusion.py    # 13-broadcast weighted fusion → fused D_clock
│   │   ├── clock_convergence.py         # Kalman filter convergence model
│   │   ├── tone_detector.py             # Matched filter tone detection (1000/1200 Hz)
│   │   ├── transmission_time_solver.py  # Propagation mode disambiguation
│   │   ├── wwvh_discrimination.py       # WWV vs WWVH 8-vote weighted discrimination
│   │   └── ionospheric_model.py         # IRI-2020 propagation delay estimation
│   ├── stream/                      # ka9q-radio RTP stream handling
│   └── interfaces/                  # Data contracts
├── config/grape-config.toml         # Station config, channels, paths
└── CONTEXT.md                       # This file

grape-recorder/                      # Separate repository (Phase 3 products)
├── Decimation, spectrograms, PSWS upload
└── https://github.com/mijahauan/grape-recorder
```

---

## 🔬 TIMING ANALYSIS ARCHITECTURE

### The D_clock Equation

```
T_arrival = T_emission + T_propagation + D_clock

Where:
  T_arrival     = Observed tone arrival time (from matched filter detection)
  T_emission    = 0 (tones transmitted at exact second boundary by atomic clock)
  T_propagation = HF signal propagation delay (ionospheric path)
  D_clock       = System clock offset (THE OUTPUT WE WANT)

Rearranging:
  D_clock = T_arrival - T_propagation
```

### Three-Step Refinement Pipeline

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ STEP 1: TIME SNAP (±500ms → ±50ms)                                          │
│   File: tone_detector.py                                                    │
│   Method: Quadrature matched filter for 800ms timing tones                  │
│   Output: timing_error_ms, anchor_station, SNR                              │
│   Tones: WWV=1000Hz, WWVH=1200Hz, CHU=1000Hz (0.5s)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 2: CHANNEL CHARACTERIZATION                                            │
│   2A. BCD Correlation → differential_delay_ms (WWV vs WWVH)                 │
│   2B. Doppler Estimation → ionospheric motion, channel stability            │
│   2C. Station Discrimination → 8-vote weighted system (wwvh_discrimination) │
│   2D. Test Signal Analysis → FSS, delay spread (minutes 8/44)               │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 3: TRANSMISSION TIME SOLUTION (→ D_clock)                              │
│   File: transmission_time_solver.py                                         │
│   Method: Mode disambiguation (1E, 1F, 2F, 3F, ground wave)                 │
│   Output: D_clock, propagation_mode, confidence, uncertainty                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Multi-Broadcast Fusion

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 13 BROADCASTS → WEIGHTED FUSION → FUSED D_clock                             │
│                                                                             │
│   WWV:  2.5, 5, 10, 15, 20, 25 MHz (6 broadcasts)                          │
│   WWVH: 2.5, 5, 10, 15 MHz (4 broadcasts, shared frequencies)              │
│   CHU:  3.33, 7.85, 14.67 MHz (3 broadcasts, FSK timing reference)         │
│                                                                             │
│   Weight = confidence × grade_weight × mode_weight × snr_factor             │
│   Outlier rejection: Weighted MAD, 3σ threshold                             │
│   Auto-calibration: Station-level offsets (not per-broadcast)               │
│                                                                             │
│   Output: phase2/fusion/fused_d_clock.csv → Chrony SHM                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🔑 KEY FILES FOR TIMING ANALYSIS

| File | Purpose | Key Functions |
|------|---------|---------------|
| `phase2_temporal_engine.py` | **Central orchestrator** - coordinates 3-step pipeline | `process_minute()`, `_step1_time_snap()`, `_step2_characterize()`, `_step3_solve()` |
| `multi_broadcast_fusion.py` | Combines 13 broadcasts into fused D_clock | `fuse_minute()`, `_weighted_fusion()`, `_update_calibration()` |
| `clock_convergence.py` | Kalman filter for D_clock convergence | `ClockConvergenceModel`, `update()`, `get_state()` |
| `tone_detector.py` | Matched filter tone detection | `ToneDetector`, `detect_tones()`, `detect_tick_windows()` |
| `transmission_time_solver.py` | Propagation mode disambiguation | `TransmissionTimeSolver`, `solve()`, `_identify_mode()` |
| `wwvh_discrimination.py` | WWV vs WWVH 8-vote system | `WWVHDiscriminator`, `finalize_discrimination()` |
| `ionospheric_model.py` | IRI-2020 propagation delay | `IonosphericModel`, `get_propagation_delay()` |
| `chrony_shm.py` | Write fused D_clock to Chrony | `ChronySHM`, `write_sample()` |

---

## 📊 CURRENT CALIBRATION STATE

```json
{
  "WWV":  {"offset_ms": 3.3, "uncertainty_ms": 0.47, "n_samples": 94},
  "CHU":  {"offset_ms": 8.4, "uncertainty_ms": 0.58, "n_samples": 64},
  "WWVH": {"offset_ms": 9.5, "uncertainty_ms": 1.05, "n_samples": 12}
}
```

**Philosophy:** Calibrate at the **station level**, not per-broadcast. Each station transmits from a single location using a single atomic clock. Frequency-to-frequency variations reveal ionospheric propagation effects.

---

## 🧪 NEXT SESSION FOCUS: Timing Analysis Refinement

### Areas to Explore

1. **Ionospheric Propagation Limits**
   - Current uncertainty: ~0.5-1.0 ms
   - Theoretical limit: ~0.1 ms (with perfect mode identification)
   - Key file: `ionospheric_model.py` (IRI-2020)

2. **Mode Disambiguation Accuracy**
   - 1E (single-hop E-layer): ~3-5 ms delay
   - 1F (single-hop F-layer): ~5-10 ms delay
   - Multi-hop: 2F, 3F with increasing uncertainty
   - Key file: `transmission_time_solver.py`

3. **Station Discrimination on Shared Frequencies**
   - 2.5, 5, 10, 15 MHz have both WWV and WWVH
   - 8-vote weighted discrimination system
   - Key file: `wwvh_discrimination.py`

4. **Convergence Model Tuning**
   - Kalman filter parameters (process noise, measurement noise)
   - Lock threshold: currently 1.0 ms uncertainty
   - Key file: `clock_convergence.py`

### Verification Commands

```bash
# Check current D_clock convergence state
cat /tmp/grape-test/phase2/*/status/convergence_state.json | jq .

# View fused D_clock output
tail -20 /tmp/grape-test/phase2/fusion/fused_d_clock.csv

# Check calibration state
cat /tmp/grape-test/phase2/fusion/calibration_state.json | jq .

# View per-channel clock offsets
tail -5 /tmp/grape-test/phase2/WWV_10_MHz/clock_offset/*.csv
```

---

## 🏗️ DATA FLOW

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ PHASE 1: RAW ARCHIVE (core_recorder_v2.py)                                  │
│   ka9q-radio RTP → Binary IQ @ 20 kHz (complex64)                           │
│   Output: raw_buffer/{CHANNEL}/{YYYYMMDD}/{timestamp}.bin                   │
├─────────────────────────────────────────────────────────────────────────────┤
│ PHASE 2: TIMING ANALYSIS (phase2_analytics_service.py × 9 channels)         │
│   Tone detection → D_clock → Discrimination → Fusion                        │
│   Output: phase2/{CHANNEL}/clock_offset/*.csv                               │
│   Fusion: phase2/fusion/fused_d_clock.csv → Chrony SHM                      │
├─────────────────────────────────────────────────────────────────────────────┤
│ PHASE 3: DATA PRODUCTS (grape-recorder - separate repo)                     │
│   Decimation 20kHz→10Hz, spectrograms, PSWS upload                          │
│   See: https://github.com/mijahauan/grape-recorder                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📡 CHANNELS (9 Total)

| Station | Location | Frequencies | Notes |
|---------|----------|-------------|-------|
| **WWV** | Ft. Collins, CO | 2.5, 5, 10, 15, 20, 25 MHz | 1000 Hz tone, BCD subcarrier |
| **WWVH** | Kauai, HI | 2.5, 5, 10, 15 MHz | 1200 Hz tone, shares 4 frequencies with WWV |
| **CHU** | Ottawa, Canada | 3.33, 7.85, 14.67 MHz | FSK time code (seconds 31-39), unique frequencies |

**Shared Frequency Challenge:** On 2.5, 5, 10, 15 MHz, both WWV and WWVH are present. Discrimination uses 8-vote weighted system based on tone frequency, BCD correlation, geographic prediction, and RTP calibration history.

---

## 🔧 RECENT FIXES (Dec 13-15, 2025)

| Fix | Problem | Solution | File |
|-----|---------|----------|------|
| Station-level calibration | Per-broadcast calibration introduced artificial variance | Aggregate by station, not broadcast | `multi_broadcast_fusion.py` |
| Per-second tick SNR | Vote 4 not connected | Added `detect_tick_windows()` call | `phase2_temporal_engine.py` |
| CHU FSK timing | FSK decoder not confirming D_clock | Compare FSK offset with D_clock | `phase2_temporal_engine.py` |
| RTP station prediction | Low-confidence discrimination flip-flopping | Use RTP history to predict station | `timing_calibrator.py` |
| Phase 3 separation | Codebase too large | Moved decimation/spectrograms to grape-recorder | Multiple files |

---

## 📚 KEY DOCUMENTATION

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | System design, data flow, module responsibilities |
| `TECHNICAL_REFERENCE.md` | API details, data formats, algorithms |
| `CRITIC_CONTEXT.md` | Deep technical context for code review |
| `GRAPE_SEPARATION.md` | Phase 3 separation to grape-recorder |

---

## 🚀 STARTUP COMMANDS

```bash
# Start Phase 1 (recording)
./scripts/grape-core.sh -start

# Start Phase 2 (timing analysis)
./scripts/grape-analytics.sh -start

# Start Web UI (port 3000)
./scripts/grape-ui.sh -start

# Check Chrony integration
chronyc sources  # Look for TMGR with Reach > 0
```

---

## 🔄 RECOVERY COMMANDS

```bash
# Reset D_clock convergence (if Kalman state corrupted)
./scripts/grape-analytics.sh -stop
rm -f /tmp/grape-test/phase2/*/status/convergence_state.json
rm -f /tmp/grape-test/phase2/fusion/calibration_state.json
./scripts/grape-analytics.sh -start

# View real-time logs
journalctl -u grape-analytics -f
```

---

## 💡 DESIGN PRINCIPLES

1. **GPSDO-First:** Trust the GPSDO-disciplined RTP timestamps as primary reference
2. **Variations ARE Science:** Once locked, D_clock variations reveal ionospheric propagation
3. **Station-Level Truth:** Each station has one atomic clock; frequency variations are ionospheric
4. **Weighted Fusion:** Combine all broadcasts with confidence-based weights
5. **Robust Outliers:** MAD-based rejection prevents single-channel corruption
