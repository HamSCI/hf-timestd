# HF Time Standard Analysis (hf-timestd) - AI Context Document

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-16  
**Version:** 4.0 (Two-Phase Pipeline)

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
- WWV: 6 frequencies
- WWVH: 4 frequencies (shared with WWV)
- CHU: 3 frequencies (unique)
- BPM: 4 frequencies (shared with WWV/WWVH)

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
│ STEP 1: TIME SNAP (±500ms → ±50ms)                                          │
│   File: tone_detector.py                                                    │
│   Method: Quadrature matched filter for 800ms timing tones                  │
│   Output: timing_error_ms, anchor_station, SNR                              │
│   Tones: WWV=1000Hz, WWVH=1200Hz, CHU=1000Hz (0.5s)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 2: CHANNEL CHARACTERIZATION                                            │
│   2A. BCD Correlation → differential_delay_ms (WWV vs WWVH)                 │
│   2B. Doppler Estimation → ionospheric motion, channel stability            │
│   2C. Station Discrimination → 8-vote weighted system                       │
│   2D. Test Signal Analysis → FSS, delay spread (minutes 8/44)               │
├─────────────────────────────────────────────────────────────────────────────┤
│ STEP 3: TRANSMISSION TIME SOLUTION (→ D_clock)                              │
│   File: transmission_time_solver.py                                         │
│   Method: Mode disambiguation (1E, 1F, 2F, 3F, ground wave)                 │
│   Output: D_clock, propagation_mode, confidence, uncertainty                │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Multi-Broadcast Fusion

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

**Philosophy:** Calibrate at the **station level**, not per-broadcast. Each station transmits from a single location using a single atomic clock. Frequency-to-frequency variations reveal ionospheric propagation effects.

---

## Key Files

| File | Purpose |
|------|---------|
| `phase2_temporal_engine.py` | Central orchestrator - 3-step D_clock extraction |
| `multi_broadcast_fusion.py` | Combines 13 broadcasts into fused D_clock |
| `clock_convergence.py` | Kalman filter for D_clock convergence |
| `tone_detector.py` | Matched filter tone detection (1000/1200 Hz) |
| `transmission_time_solver.py` | Propagation mode disambiguation |
| `wwvh_discrimination.py` | WWV vs WWVH 8-vote weighted system |
| `ionospheric_model.py` | IRI-2020 propagation delay estimation |
| `chrony_shm.py` | Write fused D_clock to Chrony SHM |
| `binary_archive_writer.py` | Phase 1 raw_buffer writer |
| `phase2_analytics_service.py` | Phase 2 daemon wrapper |

---

## Project Structure

```
hf-timestd/
├── src/hf_timestd/
│   ├── core/                        # Timing analysis modules
│   │   ├── phase2_temporal_engine.py
│   │   ├── multi_broadcast_fusion.py
│   │   ├── tone_detector.py
│   │   ├── transmission_time_solver.py
│   │   ├── wwvh_discrimination.py
│   │   └── binary_archive_writer.py
│   ├── stream/                      # ka9q-radio RTP stream handling
│   └── interfaces/                  # Data contracts
├── config/
│   ├── timestd-config.toml          # Main config: station, channels, paths
│   └── core-recorder.toml           # Core recorder settings
├── web-ui/                          # Monitoring dashboard
├── systemd/                         # Service files
└── docs/                            # Additional documentation
```

---

## Web UI

The web UI monitors Phase 1/2 status and visualizes Phase 2 outputs.

**Key files:**
- `web-ui/monitoring-server-v3.js`
- `web-ui/timestd-paths.js` (must stay consistent with `src/hf_timestd/paths.py`)

**Invariants:**
- Uses `raw_buffer` naming everywhere
- No GRAPE/PSWS/DigitalRF/decimation dependencies

---

## Configuration

**timestd-config.toml** is the single source of truth for:
- `mode` ("test" or "production")
- `test_data_root` / `production_data_root`
- All channel definitions

```toml
[recorder]
mode = "production"
data_root = "/var/lib/timestd"

[station]
callsign = "AC0G"
grid_square = "EM38ww40pk"
latitude = 38.xxxx
longitude = -90.xxxx
```

---

## Systemd Services

| Service | Purpose |
|---------|---------|
| `timestd-core-recorder.service` | Phase 1: RTP → raw_buffer (binary IQ) |
| `timestd-analytics.service` | Phase 2: Timing analysis (all channels + fusion) |
| `timestd-web-ui.service` | Web monitoring UI |

```bash
# Start all services
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
```

---

## Design Principles

1. **GPSDO-First:** Trust the GPSDO-disciplined RTP timestamps as primary reference
2. **Variations ARE Science:** Once locked, D_clock variations reveal ionospheric propagation
3. **Station-Level Truth:** Each station has one atomic clock; frequency variations are ionospheric
4. **Weighted Fusion:** Combine all broadcasts with confidence-based weights
5. **Robust Outliers:** MAD-based rejection prevents single-channel corruption
6. **Two-Phase Separation:** Raw data (Phase 1) is immutable; analytics (Phase 2) can be reprocessed

---

## Verification Commands

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

## Environment Variables

```bash
export TIMESTD_DATA_ROOT=/tmp/timestd-test          # or /var/lib/timestd
export TIMESTD_CONFIG=/etc/hf-timestd/timestd-config.toml
export TIMESTD_VENV=/home/wsprdaemon/hf-timestd/venv
```

---

## Dependencies

```bash
pip install numpy scipy
pip install sysv_ipc                                    # Chrony SHM
pip install git+https://github.com/mijahauan/ka9q-python.git  # ka9q-python

# Optional compression
pip install zstandard  # for zstd
pip install lz4        # for lz4
```

---

## Known Pitfalls

- **Multicast IP stability:** Do not change the hash key inputs used to derive multicast destinations
- **Path mapping:** Always use `channel_name_to_dir()` consistently across Python and JS
- **Channel naming:** Directory format `WWV_10_MHz`, display format `WWV 10 MHz`

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | System design, data flow, module responsibilities |
| `TECHNICAL_REFERENCE.md` | API details, data formats, algorithms |
| `DIRECTORY_STRUCTURE.md` | Complete path specifications |
