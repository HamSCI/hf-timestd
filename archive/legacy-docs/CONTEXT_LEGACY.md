# HF Time Standard Analysis (hf-timestd) - AI Context Document
 
**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-15  
**Version:** 0.x (cleanup in progress)  
**Next Session Focus:** Pare down active code + docs to the minimal, best-supported hf-timestd feature set; archive legacy/defunct GRAPE/Phase-3/DRF/decimation/NPZ paths.

---

## Project Scope (what hf-timestd is)

`hf-timestd` records and analyzes HF time standard stations:

- WWV
- WWVH
- CHU
- BPM

The repository focus is a **two-phase** pipeline for time-transfer analytics.

**Explicit non-goals in this repo:**

- DigitalRF
- Decimation / 10 Hz products
- PSWS/GRAPE uploads
- Phase 3 derived-product generation

Those belong in a separate project (e.g. `grape-recorder`).

---

## Architecture (authoritative)

### Phase 1: Immutable raw_buffer (binary IQ)

Phase 1 is the scientific record. It stores raw complex IQ with **system time only** (no UTC correction).

Directory layout:

```
{data_root}/raw_buffer/{CHANNEL_DIR}/{YYYYMMDD}/
    {minute_boundary}.bin[.zst|.lz4]
    {minute_boundary}.json
```

Key invariants:

- The path mapping must use `channel_name_to_dir()`.
- Files are minute-aligned.
- hf-timestd does not decimate.

Implementation:

- `src/hf_timestd/core/binary_archive_writer.py` (`BinaryArchiveWriter`, `BinaryArchiveReader`)
- Orchestration: `src/hf_timestd/core/pipeline_orchestrator.py`

### Phase 2: Analytics (D_clock)

Phase 2 reads Phase 1 `raw_buffer` and produces timing products:

- `D_clock = t_system - t_UTC`
- Tone detections
- Station discrimination (WWV vs WWVH where applicable)
- Confidence/uncertainty metrics

Phase 2 output layout:

```
{data_root}/phase2/{CHANNEL_DIR}/clock_offset/
```

Key entry points:

- `src/hf_timestd/core/phase2_temporal_engine.py`
- `src/hf_timestd/core/clock_offset_series.py`
- `src/hf_timestd/core/phase2_analytics_service.py` (daemon wrapper)

CLI expectations:

- `phase2_analytics_service.py` expects `--archive-dir` to point to `raw_buffer/{CHANNEL_DIR}`.

---

## Web UI (scope + invariants)

The web UI is for monitoring Phase 1/2 status and visualizing Phase 2 outputs.

Key files:

- `web-ui/monitoring-server-v3.js`
- `web-ui/timestd-paths.js` (path helpers; must stay consistent with `src/hf_timestd/paths.py`)
- `web-ui/WEB_UI_ARCHITECTURE.md`

Invariants:

- Active web UI code must not depend on GRAPE/PSWS/DigitalRF/decimation/NPZ assumptions.
- Use `raw_buffer` naming everywhere.

---

## What changed in the most recent cleanup phase (high-level)

- Phase 3/decimation-related modules were moved under `archive/`.
- `raw_archive` naming was removed from active runtime code and replaced with `raw_buffer`.
- Monitoring server and path helpers now use `raw_buffer`.
- DigitalRF references are being removed from docs/config.

---

## Next Session: Recommended AI Task List

Goal: make the repository feel like a clean, coherent hf-timestd project.

1. Archive or delete (as appropriate) any remaining legacy GRAPE/Phase-3 code paths that are not used by hf-timestd.
2. Audit docs for GRAPE/PSWS/DRF/decimation content; keep only short stubs that point to `grape-recorder` where necessary.
3. Web UI cleanup:
   - Move `web-ui/grape-paths.js` into `web-ui/archive/` (and ensure nothing imports it).
   - Remove any remaining GRAPE tokens from active html/js.
4. Optional: decide what to do with NPZ-only tooling (e.g. `src/hf_timestd/core/gap_backfill.py`):
   - Keep as archived legacy, or
   - Port to raw_buffer/phase2-native approach.
5. Run a final repo-wide grep excluding `archive/` and `venv/`:
   - `grape`, `psws`, `digital_rf`, `raw_archive`, `decimat`, `npz`.

---

## Known pitfalls / “don’t break these”

- Multicast IP stability: do not change the hash key inputs used to derive multicast destinations.
- Path mapping: always use `channel_name_to_dir()` consistently across Python and JS.

---

## Legacy (archived) context

The remainder of this file is historical context from the earlier GRAPE/three-phase implementation.
It is kept temporarily for reference, but should be treated as **archived** and not as authoritative hf-timestd behavior.

---

## LEGACY (pre-cleanup) - Archived Session Notes

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

2. **Optional zstd/lz4 Compression**
- **Feature**: Raw IQ files can now be compressed to reduce disk I/O by 2-3x
- **Configuration** (in `timestd-config.toml`):
  ```toml
  [recorder]
  compression = "zstd"  # 'none', 'zstd', or 'lz4'
  compression_level = 3  # zstd: 1-22, lz4: 1-12
  ```
- **Storage Impact**:
  | Mode | Rate | Daily | Monthly |
  |------|------|-------|---------|
  | none | 86 MB/min | 124 GB | 3.7 TB |
  | zstd | ~35 MB/min | ~50 GB | ~1.5 TB |
  | lz4 | ~50 MB/min | ~72 GB | ~2.2 TB |
- **Dependencies** (optional):
  ```bash
  pip install zstandard  # for zstd
  pip install lz4        # for lz4
  ```
- **Files Changed**: `binary_archive_writer.py`, `phase2_analytics_service.py`, `pipeline_orchestrator.py`, `stream_recorder_v2.py`, `core_recorder_v2.py`

3. **Mode Disambiguation Accuracy**
   - 1E (single-hop E-layer): ~3-5 ms delay
   - 1F (single-hop F-layer): ~5-10 ms delay
   - Multi-hop: 2F, 3F with increasing uncertainty
   - Key file: `transmission_time_solver.py`

4. **Station Discrimination on Shared Frequencies**
   - 2.5, 5, 10, 15 MHz have both WWV and WWVH
   - 8-vote weighted discrimination system
   - Key file: `wwvh_discrimination.py`

5. **Convergence Model Tuning**
   - Kalman filter parameters (process noise, measurement noise)
   - Lock threshold: currently 1.0 ms uncertainty
   - Key file: `clock_convergence.py`

### Verification Commands

```bash
# Check current D_clock convergence state
cat /tmp/timestd-test/phase2/*/status/convergence_state.json | jq .

# View fused D_clock output
tail -20 /tmp/timestd-test/phase2/fusion/fused_d_clock.csv

# Check calibration state
cat /tmp/timestd-test/phase2/fusion/calibration_state.json | jq .

# View per-channel clock offsets
tail -5 /tmp/timestd-test/phase2/WWV_10_MHz/clock_offset/*.csv
```

---

## 🏗️ DATA FLOW

### Pre-Installation Checklist

| Area | Status | Reference |
|------|--------|-----------|
| **Core Recording** | ✅ Stable | `core/` modules, `core-recorder.toml` |
| **Phase 2 Analytics** | ✅ Stable | `docs/PHASE2_CRITIQUE.md` - 16 fixes applied |
| **Systemd Services** | ✅ Updated | All use `EnvironmentFile=` |
| **Install Script** | ✅ Updated | `scripts/install.sh` supports test/production modes |
| **Web UI** | ✅ Working | `timestd-ui.sh`, port 3000 |

### Key Configuration Files

```
config/
├── timestd-config.toml     # Main config: station, channels, paths
├── core-recorder.toml    # Core recorder: ka9q connection, raw_buffer writer
└── environment           # Environment variables (create from .template)
```

### Production Path Structure

```toml
# timestd-config.toml - PRODUCTION settings
[recorder]
mode = "production"
data_root = "/var/lib/timestd"   # vs /tmp/timestd-test

[station]
callsign = "AC0G"
grid_square = "EM38ww40pk"
latitude = 38.xxxx
longitude = -90.xxxx
```

### Systemd Services

**Continuous Services** (run 24/7):

| Service | Purpose |
|---------|---------|
| `timestd-core-recorder.service` | Phase 1: RTP → raw_buffer (binary IQ) |
| `timestd-analytics.service` | Phase 2: Timing analysis (9 channels + fusion) |
| `timestd-web-ui.service` | Web monitoring UI |

Phase 3 products (decimation, spectrograms, PSWS uploads) are out of scope for `hf-timestd`.
If you need them, use the separate `grape-recorder` project.

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
| Phase 3 separation | Codebase too large | Moved Phase 3 responsibilities to grape-recorder | Multiple files |

---

## 📚 KEY DOCUMENTATION

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | System design, data flow, module responsibilities |
| `TECHNICAL_REFERENCE.md` | API details, data formats, algorithms |
| `CRITIC_CONTEXT.md` | Deep technical context for code review |

---

## 🚀 STARTUP COMMANDS

```bash
# Start all services
./scripts/timestd-all.sh -start
```

### Production Mode (after installation)
```bash
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
```

---

## 🔄 RECOVERY COMMANDS

```bash
./scripts/timestd-all.sh -stop
rm -f /tmp/timestd-test/phase2/*/status/convergence_state.json
rm -f /tmp/timestd-test/phase2/*/clock_offset/*.csv
./scripts/timestd-all.sh -start
```

---

## 💡 DESIGN PRINCIPLES

1. **GPSDO-First:** Trust the GPSDO-disciplined RTP timestamps as primary reference
2. **Variations ARE Science:** Once locked, D_clock variations reveal ionospheric propagation
3. **Station-Level Truth:** Each station has one atomic clock; frequency variations are ionospheric
4. **Weighted Fusion:** Combine all broadcasts with confidence-based weights
5. **Robust Outliers:** MAD-based rejection prevents single-channel corruption

---

## 📊 STANDARD ENVIRONMENT

```bash
# Standard hf-timestd environment (set in systemd or shell)
export TIMESTD_DATA_ROOT=/tmp/timestd-test          # or /var/lib/timestd
export TIMESTD_CONFIG=/etc/hf-timestd/timestd-config.toml
export TIMESTD_VENV=/home/wsprdaemon/hf-timestd/venv
```

---

## SESSION HISTORY (Recent)

| Date | Focus | Key Changes |
|------|-------|-------------|
| Dec 13 PM | Chrony Integration | ka9q-python 3.2.2, Chrony SHM refclock, web UI fix, raw_buffer paths |
| Dec 13 AM | IRI-2020 Upgrade | Ionospheric model upgraded, mode switching cleanup |
| Dec 8 Night | Production Deployed | Systemd services running, matplotlib added, docs updated |
| Dec 8 Eve | Production Mode | TEST/PRODUCTION architecture, install.sh, systemd services |
| Dec 8 AM | Clock Drift | RTP timestamp bug, Kalman state reset, channel discovery |
| Dec 7 PM | Phase 2 Critique | 16 methodology fixes, uncertainty replaces grades |
| Dec 7 AM | BCD Correlation | Fixed BCD detection, 440Hz filtering, noise floor band |

---

## ARCHITECTURE CHANGES (Dec 13, 2025)

### Data Storage Format Change

| Component | Before | After |
|-----------|--------|-------|
| Phase 1 Output | `raw_archive/{CH}/` (DRF HDF5) | `raw_buffer/{CH}/` (binary IQ) |
| File Format | Digital RF HDF5 | Raw complex64 binary + JSON sidecar |
| Analytics Input | Read from DRF | Read from raw_buffer binary |

### New Dependencies

```bash
pip install sysv_ipc                                    # Chrony SHM
pip install git+https://github.com/mijahauan/ka9q-python.git  # ka9q-python 3.2.2
```

### Key Configuration

**timestd-config.toml** is now the single source of truth for:
- `mode` ("test" or "production")
- `test_data_root` / `production_data_root`
- All channel definitions

---

## CHANNELS (9 Total)

| Station | Frequencies |
|---------|-------------|
| WWV (Ft. Collins, CO) | 2.5, 5, 10, 15, 20, 25 MHz |
| WWVH (Kauai, HI) | 2.5, 5, 10, 15 MHz (shared with WWV) |
| CHU (Ottawa, Canada) | 3.33, 7.85, 14.67 MHz |

**Channel naming convention:**
- Directory format: `WWV_10_MHz`, `CHU_7.85_MHz`
- Display format: `WWV 10 MHz`, `CHU 7.85 MHz`
