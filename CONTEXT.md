# HF Time Standard Analysis (hf-timestd) - AI Context Document

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** 2025-12-17  
**Version:** 5.2 (Chrony SHM Fix + Per-Broadcast Propagation Mode + Physical Constraints)

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

**Philosophy:** 
- **GPSDO is the timing reference**, not the loudest station
- **Detect ALL receivable stations** on each frequency
- Each station's ToA reveals **propagation conditions** on that path
- Calibrate at the **station level**, not per-broadcast
- Frequency-to-frequency variations reveal ionospheric propagation effects

---

## Key Files

| File | Purpose |
|------|---------|
| `phase2_temporal_engine.py` | Central orchestrator - 3-step D_clock extraction |
| `station_model.py` | **NEW** MLE-based StationModel + ChannelAssignment (replaces voting) |
| `multi_station_detector.py` | Physics-based multi-station detection |
| `multi_broadcast_fusion.py` | Combines 17 broadcasts into fused D_clock |
| `bpm_discriminator.py` | BPM (China) detection - 10ms ticks, UT1/UTC modes |
| `clock_convergence.py` | Kalman filter for D_clock convergence |
| `tone_detector.py` | Matched filter tone detection (1000/1200 Hz) |
| `transmission_time_solver.py` | Propagation mode disambiguation |
| `wwvh_discrimination.py` | WWV vs WWVH weighted voting + BCD correlation |
| `ionospheric_model.py` | IRI-2020 propagation delay estimation |
| `chrony_shm.py` | Write fused D_clock to Chrony SHM (rate-limited) |
| `tiered_storage.py` | RAM hot buffer + disk cold storage |
| `binary_archive_writer.py` | Phase 1 raw_buffer writer |
| `phase2_analytics_service.py` | Phase 2 daemon wrapper |

### Deprecated Files (Do Not Use for New Code)

| File | Replacement | Reason |
|------|-------------|--------|
| `global_station_voter.py` | `multi_station_detector.py` | Voting approach was flawed |
| `station_lock_coordinator.py` | `multi_station_detector.py` | Anchor selection replaced |

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

**Recent Updates (2025-12-16):**
- **BPM Station Support:** Reception matrix API now includes `bpm_detected`, `bpm_snr_db`, `bpm_timing_mode`, `bpm_usable_for_utc`
- **Multi-Station API:** New endpoint `GET /api/v1/phase2/multi-station/:channel` exposes ALL detected stations
- **Header-Based CSV Parsing:** `loadDiscriminationRecords()` now uses column name lookup (robust to schema changes)
- **Tiered Storage Status:** `GET /api/v1/system/storage` includes hot/cold buffer stats
- **Security Hardening:** Path traversal protection with `path.resolve()`, channel name validation

**Invariants:**
- Uses `raw_buffer` naming everywhere
- No GRAPE/PSWS/DigitalRF/decimation dependencies
- CSV parsing uses header-based column lookup (not hardcoded indices)

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
2. **Physics-Based Detection:** Detect ALL receivable stations, not just the loudest
3. **No Voting:** The GPSDO is the timing reference; each station's ToA reveals propagation
4. **Variations ARE Science:** Once locked, D_clock variations reveal ionospheric propagation
5. **Station-Level Truth:** Each station has one atomic clock; frequency variations are ionospheric
6. **Weighted Fusion:** Combine all broadcasts with uncertainty-based weights
7. **Robust Outliers:** MAD-based rejection prevents single-channel corruption
8. **Two-Phase Separation:** Raw data (Phase 1) is immutable; analytics (Phase 2) can be reprocessed
9. **Tiered Storage:** RAM hot buffer for low-latency access; disk cold buffer for persistence

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

## Deployment Environments

The system supports two deployment modes controlled by `timestd-config.toml`:

### Test Environment
- **Data Root:** `/tmp/timestd-test` (or configured `test_data_root`)
- **Purpose:** Development, debugging, feature testing
- **Characteristics:** Ephemeral data, can be wiped without consequence

### Production Environment
- **Data Root:** `/var/lib/timestd` (or configured `production_data_root`)
- **Purpose:** Continuous scientific data collection
- **Characteristics:** Persistent data, requires careful management

### Key Deployment Files to Review

| Location | Purpose |
|----------|---------|
| `config/timestd-config.toml` | Mode selection, data roots, channel definitions |
| `config/environment` | Environment variables for services |
| `systemd/timestd-*.service` | Systemd unit files |
| `scripts/` | Start/stop/status scripts |

---

## Recent Session Gains (2025-12-17)

### Chrony SHM Integration Fixed
- **Issue:** Chrony wasn't receiving samples from fusion service (Reach=0)
- **Root Cause:** Struct packing format used `xxxx` instead of `4x` for padding
- **Fix:** `chrony_shm.py` line 235: `'@ii q i 4x q i 4x iiii II 8i'`
- **Result:** Chrony now receiving samples with +15μs offset, Reach=4+

### Physical Constraint Validation Added
- **File:** `timing_calibrator.py` - `_validate_physical_constraints()` method
- **Constraints enforced:**
  - Minimum light-speed propagation delay per station
  - Expected ionospheric propagation delay ranges with margin
  - D_clock bounds ±50ms after calibration
- **Station-specific delays (from EM38ww):**
  - WWV: 3.5-15ms (1,119 km)
  - WWVH: 22-60ms (6,600 km)
  - CHU: 5-20ms (1,522 km)
  - BPM: 38-80ms (11,504 km)

### Dynamic Search Window Narrowing
- **Methods added to `timing_calibrator.py`:**
  - `get_station_search_window()` - per-station window based on calibration
  - `get_calibrated_search_window_ms()` - 10-20ms after calibration vs 500ms bootstrap
- **Integration:** `pipeline_orchestrator.py` wires calibrator to Phase 2 engine

### Fallback Propagation Delay Fix
- **Issue:** `TransmissionTimeSolver` fallback used `t_propagation_ms=0.0`
- **Fix:** `phase2_temporal_engine.py` now uses station-typical values:
  - WWV: 8ms, WWVH: 35ms, CHU: 10ms, BPM: 50ms

### Per-Broadcast Propagation Mode Visualization
- **Issue:** Propagation mode dropdown showed 9 channels instead of 17 broadcasts
- **Fix:** `timing-advanced.html` dropdown now shows all 17 broadcasts grouped by station
- **API:** `/api/v1/timing/mode-probability?station=WWV&freq=10` accepts station/freq params
- **Backend:** `transmission-time-helpers.js` calculates station-specific mode delays based on geographic distance

### Storage Quota Auto-Cleanup
- **File:** `binary_archive_writer.py` - `_remove_oldest_files()` method
- **Behavior:** Automatically removes oldest .bin/.json files when quota exceeded

---

## Current Focus: MLE-Based Multi-Station Discrimination

### The Problem: Three-Station Superposition

The 4 SHARED channels (2.5, 5, 10, 15 MHz) carry signals from **3 stations** simultaneously:

| Station | Tone (Hz) | Tick Duration | Timing Offset | Distance from EM38 |
|---------|-----------|---------------|---------------|-------------------|
| WWV     | 1000      | 5 ms          | 0 ms (UTC)    | 1,119 km (~4 ms)  |
| WWVH    | 1200      | 5 ms          | 0 ms (UTC)    | 6,600 km (~25 ms) |
| BPM     | 1000      | 10 ms (UTC)   | **-20 ms**    | 11,504 km (~44 ms)|

**The BPM Challenge:** BPM pulses are emitted 20 ms BEFORE UTC. Combined with ~44 ms propagation:
- BPM arrival: -20 ms + 44 ms = **+24 ms after UTC**
- WWV arrival: 0 ms + 4 ms = **+4 ms after UTC**
- Separation: ~20 ms (exploitable with sub-ms ToA resolution)

### New Architecture: MLE Component Decomposition

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ CORRELATOR BANK (per minute)                                                 │
│                                                                             │
│   IQ Samples ──┬──► WWV Correlator (1000 Hz, 5ms) ──► WWV_power, WWV_ToA    │
│                │    [search: expected_delay ± 10ms]                         │
│                │                                                             │
│                ├──► WWVH Correlator (1200 Hz, 5ms) ──► WWVH_power, WWVH_ToA │
│                │    [search: expected_delay ± 10ms]                         │
│                │                                                             │
│                └──► BPM Correlator (1000 Hz, 10ms) ──► BPM_power, BPM_ToA   │
│                     [search: expected_delay - 20ms ± 10ms]                  │
│                                                                             │
│   Output: ChannelAssignment {                                               │
│       wwv_component_power_db, wwvh_component_power_db, bpm_component_power_db│
│       wwv_toa_ms, wwvh_toa_ms, bpm_toa_ms                                   │
│       residual_noise_db, cross_validation_error_ms                          │
│   }                                                                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### BPM Calibration Windows

BPM has unique features that provide unambiguous calibration:

| Feature | Minutes | Description |
|---------|---------|-------------|
| **UT1 Pulses** | 25-29, 55-59 | 100 ms pulses (10× longer than WWV) |
| **Pure Carrier** | 10-15, 40-45 | No time code modulation |
| **Tick Duration** | All UTC minutes | 10 ms vs WWV's 5 ms |

**Key Insight:** During UT1 minutes, BPM's 100 ms pulses are **unambiguous** - use these to calibrate BPM path gain and delay.

### Key Files for Discrimination

| File | Purpose |
|------|---------|
| `station_model.py` | **NEW** StationModel + ChannelAssignment for MLE approach |
| `correlator_bank.py` | **NEW** Parallel matched filtering with predicted ToA windows |
| `wwvh_discrimination.py` | WWV vs WWVH weighted voting + BCD correlation + Doppler compensation |
| `bpm_discriminator.py` | BPM detection via tick duration + UT1 pulse calibration |
| `multi_station_detector.py` | Physics-based multi-station detection |
| `timing_calibrator.py` | Physical constraint validation per station |

### Implementation Phases

1. **Phase 1 (Immediate):** BPM UT1 pulse detection at minutes :25-:29
2. **Phase 2:** Correlator bank with predicted ToA windows per station
3. **Phase 3:** Doppler-compensated BCD (10s windows instead of 60s)
4. **Phase 4:** Super-resolution ToA via parabolic interpolation

### Design Document

See `docs/design/MULTI_STATION_MLE_DESIGN.md` for full architecture details.

### API Endpoints for Discrimination

```bash
# Per-channel discrimination data
GET /api/v1/channels/:channelName/discrimination/:date

# Multi-station detection results
GET /api/v1/phase2/multi-station/:channel

# Propagation mode per broadcast
GET /api/v1/timing/mode-probability?station=WWV&freq=10
```

### Test Commands

```bash
# Check which station is detected on SHARED 10 MHz
curl -s http://localhost:3000/api/v1/timing/phase2-status/SHARED%2010%20MHz | jq '.station'

# View discrimination results
cat /tmp/timestd-test/phase2/SHARED_10_MHz/discrimination/*.csv | tail -5

# Check BPM detection (including UT1 mode)
cat /tmp/timestd-test/phase2/SHARED_10_MHz/status/phase2_status.json | jq '.bpm_detected, .bpm_timing_mode'

# Test StationModel factory
python3 -c "from src.hf_timestd.core.station_model import StationModelFactory; \
  f = StationModelFactory(38.918, -92.128); \
  [print(f'{k.value}: {v.expected_delay_ms:.1f}ms') for k,v in f.create_all_models().items()]"
```

---

## Related Documentation

| Document | Purpose |
|----------|---------|
| `ARCHITECTURE.md` | System design, data flow, module responsibilities |
| `TECHNICAL_REFERENCE.md` | API details, data formats, algorithms |
| `DIRECTORY_STRUCTURE.md` | Complete path specifications |
| `INSTALLATION.md` | Deployment and setup instructions |
