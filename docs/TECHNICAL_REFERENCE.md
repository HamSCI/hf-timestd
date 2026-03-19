# HF Time Standard (hf-timestd) - Technical Reference

**Quick reference for developers working on the HF Time Standard (hf-timestd) codebase.**

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** March 19, 2026

---

## Current Operational Configuration

**9 channels** monitoring 9 frequencies at 24 kHz IQ (config-driven):

- **Shared frequencies (4):** 2.5, 5, 10, 15 MHz - WWV and WWVH both transmit
- **WWV-only (2):** 20, 25 MHz
- **CHU (3):** 3.33, 7.85, 14.67 MHz

**Data products generated**:

1. **24 kHz Binary IQ Archive** - Phase 1 raw recording as `.bin.zst` + JSON sidecars (`raw_buffer/{CHANNEL}/`)
2. **Phase 2 Analytics (HDF5)** - L1/L2 Metrology, Tick Timing, Detection Attempts (`phase2/{CHANNEL}/`)
3. **Phase 3 Fusion (HDF5)** - L3 Fused Timing & Global Science Products (`phase2/fusion/`)
4. **Spectrograms** - Visualizations with solar zenith (`products/{CHANNEL}/spectrograms/`)

**Goal**: Archive raw 24 kHz IQ (Phase 1), perform timing analysis (Phase 2), generate derived products (Phase 3) for PSWS upload, provide WWV/WWVH discrimination on 4 shared frequencies.

---

## System Architecture: The Eight Services

The system is composed of eight independent systemd services, each with a specific responsibility in the data pipeline.

### 1. Core Recorder (`timestd-core-recorder`)

**Responsibility:** Reliable Data Capture

- Consumes RTP multicast streams from `ka9q-radio`.
- Writes **binary IQ archive** files (`.bin.zst`) with JSON metadata sidecars.
- Maintains sample count integrity (gap filling).
- **Output:** `/var/lib/timestd/raw_buffer/`

### 2. Metrology (`timestd-metrology`)

**Responsibility:** Signal Processing & Timing Extraction

- Polls for new binary IQ files from raw_buffer.
- Performs tone detection (1000/1200 Hz), BCD decoding, and WWV/WWVH discrimination.
- Calculates `D_clock` (System - UTC) using physics propagation models.
- **Timing (v6.6):** Uses authoritative RTP timestamps from GPS+PPS via radiod (no pipeline offset correction needed).
- **Output:** `/var/lib/timestd/phase2/{CHANNEL}/` (HDF5 L1/L2)

### 3. Fusion (`timestd-fusion`)

**Responsibility:** Multi-Broadcast Synthesis (v6.1 Architecture)

- Reads L2 HDF5 measurements from all 9 channels via SWMR (`swmr=True` readers, writer holds file open with `swmr_mode=True`).
- **Per-broadcast Kalman filtering** — tracks ionospheric path dynamics for each of 17 broadcasts.
- **GNSS VTEC correction** — applies real-time ionospheric correction when local GNSS available.
- **Weighted Least Squares fusion** — optimal linear combination without temporal smoothing.
- Feeds **Chrony SHM** to discipline the system clock.
- **Output:** `/var/lib/timestd/phase2/fusion/` (HDF5 L3, Fused CSV)

**v6.1 Hierarchical Architecture:**
| Layer | Method | Purpose |
|-------|--------|---------|
| Per-Broadcast | Kalman filter | Track ionospheric dynamics |
| Per-Station | TEC validation | Multi-frequency consistency |
| GNSS VTEC | Direct correction | Remove model TEC bias |
| Multi-Station | WLS fusion | Optimal combination |

### 4. VTEC (`timestd-vtec`)

**Responsibility:** Ionospheric Data Acquisition

- Polls GNSS receiver (ZED-F9P) for dual-frequency observables.
- Downloads global IONEX maps from NASA CDDIS.
- **Output:** `/var/lib/timestd/data/gnss_vtec/GNSS_gnss_vtec_YYYYMMDD.h5`, `/var/lib/timestd/ionex/`

#### GNSS timing: dual-clock alignment (RAWX `rcvTow` vs system/RTP time)

GNSS observables in this project can be indexed by two clocks:

- **GNSS observation time** (authoritative for GNSS-derived observables):
  `(gnss_week, gnss_rcvTow_s)` from UBX-RXM-RAWX, where `rcvTow` is a fractional
  receiver time-of-week captured by the GNSS hardware measurement engine.

- **System receipt time** (secondary; used for cross-stream alignment):
  `unix_timestamp` recorded when a UBX message is received/processed on the
  host. This time base may include OS/network jitter.

To support defensible alignment between GNSS products and HF products (which are
typically indexed by radiod RTP-derived time), the VTEC monitor records the
estimated system-vs-GNSS offset per epoch:

`unix_minus_gnss_s = unix_timestamp - unix_from_gnss_s`

where `unix_from_gnss_s` is computed from GPS week + `rcvTow` using the GPS epoch
and the `leapS` (GPS-UTC) offset reported by RAWX. A running mean and standard
deviation of `unix_minus_gnss_s` are also recorded to quantify alignment jitter.

Guidance:

- Use `(gnss_week, gnss_rcvTow_s)` as the primary index for GNSS-derived rate and
  variability metrics (e.g., ROT/ROTI, phase-variation indices).
- Use `unix_minus_gnss_*` fields to align GNSS epochs with HF/RTP-derived records
  without attributing OS/network latency to ionospheric physics.

### 5. Physics Service (`timestd-physics`)

**Responsibility:** Propagation Modeling & Ionospheric Science Products

- Computes ionospheric propagation delays using IONEX/IRI-2020 models.
- Produces carrier-phase dTEC (primary science product) with GNSS VTEC anchoring.
- Estimates group-delay TEC from multi-frequency dispersion (validation product, noise-dominated).
- Computes ionospheric residual (T_iono = T_observed - T_vacuum).
- Archives dTEC, TEC, and T_iono to HDF5 for scientific analysis.
- **Output:** `/var/lib/timestd/phase2/science/tec/` (HDF5 L3)

### 6. Web UI & API (`timestd-web-api`)

**Responsibility:** User Visualization & System API

- **Service Type:** Python/FastAPI (`uvicorn`).
- **Endpoint:** Port 8000.
- **Capabilities:**
  - Serves static dashboard (`metrology.html`, `logs.html`, `ionosphere.html`).
  - Provides REST API for system status and HDF5 data access.
  - Interactive API documentation at `/api/docs`.
- **Logs Viewer:** Real-time access to systemd journals via `/api/logs` endpoint.

---

## Data Formats

### 1. Raw Archive: Binary IQ + JSON

Raw IQ data is stored as compressed binary files with JSON metadata sidecars.

- **Format:** `.bin.zst` (zstd-compressed binary) + `.json` sidecar.
- **Structure:**
  - Binary file: 1,440,000 complex64 IQ samples per minute.
  - JSON sidecar: RTP timestamps, gap info, system time, quality metrics.
- **Metadata:** Start RTP timestamp, start system time, sample rate (24 kHz), center frequency, gap count.

> **Note:** Digital RF (MIT Haystack) is used only for GRAPE DRF packaging/upload, not for raw recording.

### 2. Analytics Output: HDF5 L1/L2

Phase 2 produces hierarchical HDF5 files.

- **L1A (Tone Detections):**
  - `feature_extraction` group.
  - Contains raw SNR, tone power, BCD correlation metrics.
- **L2 (Timing Measurements):**
  - `timing_solution` group.
  - `d_clock`: The calculated clock offset.
  - `uncertainty`: 1-sigma confidence.
  - `propagation_model`: Which physics model was used (IONEX, IRI, etc.).

### 3. Fusion Output: HDF5 L3

Phase 3 fusion results.

- **Structure:**
  - `/fused_solution`: Time series of the weighted mean offset.
  - `/residuals`: Per-station residuals from the mean.
  - `/calibration`: Current calibration state for each station.

---

## Real-Time Core vs Physics Overlay — Separation of Concerns

### What is strictly necessary for UTC recapture (GPSDO mode)

The real-time chrony feed requires **no ionospheric physics model** to produce a valid, converged UTC estimate. The following are sufficient:

| Component | What it needs | Module |
|---|---|---|
| Raw TOA detection | RTP timestamp + vacuum geometric window | `metrology_engine.py` |
| Geometric delay | Station coordinates + great-circle distance | `wwv_constants.py` + stdlib `math` |
| Per-broadcast Kalman tracking | Repeated observations (self-correcting) | `broadcast_kalman_filter.py` |
| WLS multi-broadcast fusion | Statistical combination only | `multi_broadcast_fusion.py` |
| GNSS VTEC correction | Optional — already degrades gracefully | `gnss_tec.py` |

**The per-broadcast Kalman filter is the key resilience mechanism.** After ~10–20 minutes of observations it has *learned* the actual delay per path from data, making any ionospheric model advisory rather than mandatory. Physics models only matter for cold-start initialization and long-outage recovery — both handled by inflated uncertainty windows.

### What is NOT required for the chrony feed

- IRI-2020 / WAM-IPE / GIRO / IONEX
- Mode identification (1F / 2F / 3F)
- `ArrivalPatternMatrix` or `HFPropagationModel`
- `PropagationModeSolver`
- PHaRLAP / pyLAP ray tracing

These are **physics overlay** components that improve accuracy when available but must not be on the critical path.

### Module-level resilience (implemented v6.8)

All physics imports in the real-time core are now soft (try/except):

| Module | Import | Failure behaviour |
|---|---|---|
| `metrology_engine.py` | `ArrivalPatternMatrix` | Falls through to vacuum/geometric 3rd-tier fallback |
| `multi_broadcast_fusion.py` | `ArrivalPatternMatrix` | `self.arrival_matrix = None`; validation step skipped |
| `multi_broadcast_fusion.py` | `HFPropagationModel` | Already try/except since v6.7 |
| `l2_calibration_service.py` | `PropagationModeSolver` | Geometric-only delay, `mode_label="geometric"`, `mode_confidence=0.0` |

The `l2_calibration_service` geometric fallback uses the Haversine formula (great-circle, vacuum speed-of-light). `mode_confidence=0.0` propagates through `_calculate_uncertainty()` to produce a conservatively wide `u_propagation_model_ms` (~5 ms), keeping the ISO GUM budget honest.

### Physics overlay — when and how it helps

| Overlay | Benefit | Activation |
|---|---|---|
| WAM-IPE + GIRO | ±1.5 ms (3σ) window; mode scoring | `IonoDataService` running |
| IRI-2020 | ±4.5 ms (3σ); cold-start initialization | `iri2020` package installed |
| GNSS VTEC | Direct dTEC/dt anchor; scintillation metrics | ZED-F9P connected |
| PHaRLAP / pyLAP | 2D ray tracing with spatially varying IRI grid; mode-ID for science products | `raytrace_engine.py` (offline/batch) |

### What L2 calibration service is responsible for

`l2_calibration_service.py` is a **physics annotation layer**, not a real-time necessity. It converts L1 raw-TOA to L2 calibrated timing by applying geometric + ionospheric corrections and ISO GUM uncertainty budgets. Fusion reads both L1 and L2; it falls back to L1-only mode automatically (`force_l1_only`) if no L2 data is available. The TSL1 chrony feed therefore continues uninterrupted even if the L2 service is stopped.

---

## Physics & Propagation Modeling (V6.7)

To convert "Arrival Time" to "Emission Time", we must rigorously model the flight path.

### 1. Real-Time Ionospheric Model (v6.7)

The `HFPropagationModel` class (`propagation_model.py`) computes frequency-dependent, time-varying group delay predictions using real-time ionospheric data:

- **Tier 0: WAM-IPE + GIRO (Real-Time):**
  - `IonoDataService` fetches WAM-IPE 2D products (TEC, NmF2, HmF2) from NOAA AWS S3 (`noaa-nws-wam-ipe-pds`) and NOMADS.
  - GIRO ionosonde corrections provide ground-truth hmF2/foF2 at path midpoints.
  - Numerically integrates group delay through Chapman electron density profiles.
  - Evaluates 4 propagation modes (1F, 2F, 3F, 1E) with MUF and geometry checks.
  - Great-circle path TEC sampling via `_gc_intermediate()` (spherical trigonometry, not linear lat/lon).
  - Altitude-dependent obliquity mapping: `M(h) = 1/sqrt(1 - (R·cos(e)/(R+h))²)` replaces `1/sin(e)`.
  - Group delay formula: `Δτ = 40.3 × sTEC / (c × f²)`

- **Tier 1: IONEX (Global Ionosphere Maps):**
  - Uses `.i` files from IGS/NASA.
  - Calculates the **Ionospheric Pierce Point (IPP)** at 350km altitude.
  - Interpolates VTEC from the grid (lat/lon/time).
  - Converts VTEC to Group Delay: $\tau_{iono} \propto \frac{TEC}{f^2}$

- **Tier 2: IRI-2020:**
  - Uses the International Reference Ionosphere model when real-time data is unavailable.
  - Estimates `hmF2` (layer height) and statistical monthly VTEC.

- **Tier 3: Parametric/Empirical:**
  - Diurnal/seasonal parametric model with latitude dependence.
  - Fallback model based on path distance and solar zenith angle.

### 2. Path Mid-Point Correction

Ionospheric delay is determined by the electron density at the **reflection point**, not the transmitter or receiver.

- **Algorithm:**
    1. Calculate Great Circle path.
    2. Determine path midpoint (for 1-hop) or reflection points (for N-hop).
    3. Query WAM-IPE/GIRO/IONEX/IRI at those specific coordinates.

### 3. Multi-Mode Arrival Prediction (v6.7)

The `ArrivalPatternMatrix` now predicts multiple propagation modes per (station, frequency):

| Mode | Layer | Typical Distance | Example |
|------|-------|------------------|---------|
| 1F | F2 | < 3000 km | CHU→AC0G (1522 km) |
| 2F | F2 | 3000–6000 km | WWVH→AC0G (6600 km) |
| 3F | F2 | > 6000 km | BPM→AC0G (11504 km) |
| 1E | E | < 2000 km | Daytime, lower frequencies |

Each mode has independent delay, uncertainty, and search window. The primary arrival (lowest delay) is backward-compatible with existing callers.

### 4. Adaptive Uncertainty (v6.7)

Uncertainty adapts based on data source quality and observed variance:
- WAM-IPE + GIRO: ±1.5 ms (3σ)
- IRI-2020: ±4.5 ms (3σ)
- Parametric: ±9.0 ms (3σ)
- Blended with tracked observational variance, floored at ±5 ms (3σ)

### 5. Full Pipeline Integration (v6.7.1)

The `HFPropagationModel` is now the sole propagation model throughout the pipeline:

| Consumer | Usage | Previous |
|----------|-------|----------|
| `MetrologyEngine._predict_geometric_delay()` | Centers detection search window | `ArrivalPatternMatrix` (unchanged) |
| `MultiBroadcastFusion` | Mode ambiguity scoring, GNSS VTEC correction | `PhysicsPropagationModel` (migrated) |
| `BootstrapValidator._get_expected_delay()` | Physics-based expected delay | Static `EXPECTED_DELAYS_MS` dict (replaced) |
| `ArrivalPatternMatrix` | Self-consistency check, multi-mode windows | Already integrated |

**Deprecated:** `PhysicsPropagationModel` in `physics_propagation.py` is retained for backward compatibility but all callers have been migrated.

### 6. Web API Propagation Endpoints (v6.7.1)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/model/predict` | GET | Single station/frequency delay prediction with all feasible modes |
| `/model/all-stations` | GET | Predictions for all 17 broadcasts at current UTC |
| `/model/iono-status` | GET | `IonoDataService` health: data source, cache age, last fetch status |

These endpoints use a lazy-initialized `HFPropagationModel` with receiver coordinates from config.

---

## The "Steel Ruler" Architecture

The system implements a three-layer metrological architecture that distinguishes between **Frequency Stability** (Slope) and **Time Accuracy** (Offset):

| Layer | Name | Provides | Function |
|-------|------|----------|----------|
| 1 | Single Broadcast | "Floating Ruler" | Measures tick rate stability, but NOT anchored to UTC |
| 2 | Multi-Frequency | "Dispersion Anchor" | TEC calculation → ionospheric delay correction |
| 3 | Multi-Station | "Geometry Lock" | Cross-validation → integrity across hemisphere |

**The GPSDO provides the Slope (Rate)** — it ensures the ruler is straight and rigid.  
**Multi-frequency dispersion provides the Vertical Shift** — it calibrates the zero-point per station.  
**Multi-station fusion provides Integrity** — it ensures the zero-point is consistent globally.

**Key Insight**: The combined regression of 17 broadcasts doesn't just average noise — it **solves the geometry** of the ionosphere to find the true UTC origin point.

For detailed metrological description, see `docs/METROLOGY.md`.

---

## Timing Bootstrap System (v6.3.0)

The Timing Bootstrap establishes the critical RTP-to-UTC offset that allows conversion between SDR sample counts and Coordinated Universal Time.

### The Problem

RTP timestamps are arbitrary 32-bit counters that start at a random value when the SDR begins streaming. To convert detected tone arrivals to UTC, we need:

```
UTC = RTP_timestamp / sample_rate + offset
```

### Two-Phase Solution

#### Phase 1: Metadata-Based Offset

Each minute buffer includes metadata with both RTP and system time:

```json
{
  "start_rtp_timestamp": 164520840,
  "start_system_time": 1769306160.0665529
}
```

The offset is calculated directly:
```python
offset = system_time - (rtp_timestamp / sample_rate)
```

#### Phase 2: Broadcast Validation

Once established, the offset is validated using discriminating features:

| Feature | Purpose |
|---------|---------|
| **Tone frequency** | WWV=1000Hz, WWVH=1200Hz |
| **Tone schedule** | Ground-truth minutes (WWV-only: 1,16,17,19) |
| **Geographic ordering** | WWVH always arrives after WWV |
| **Unambiguous channels** | CHU, WWV 20/25 MHz |

### State Machine

```
ACQUIRING → CORRELATING → TRACKING → LOCKED
    ↓           ↓            ↓          ↓
  1 min      3 min       10 min    Continuous
```

### Implementation

Key class: `TimingBootstrap` in `src/hf_timestd/core/timing_bootstrap.py`

```python
bootstrap = TimingBootstrap(receiver_lat=38.9, receiver_lon=-92.1)
result = bootstrap.establish_offset_from_metadata(rtp_start, system_time, channel)
# Returns: "TRACKING" or "LOCKED" when validated
```

For complete methodology, see `docs/METROLOGY.md`.

---

## Critical Design Principles

### 1. RTP Timestamp is Authoritative Reference

**Not wall clock.** System time is derived from RTP timestamps. radiod's `GPS_TIME` and `RTP_TIMESNAP` are both derived from `input_sample_index / decimation` (same counter space). No pipeline offset correction is needed.

```python
# Authoritative time reconstruction (~50 μs accuracy):
utc = gps_time_unix + (rtp_ts - rtp_timesnap) / sample_rate
```

### 2. Sample Count Integrity

**Invariant**: 24 kHz × 60 sec = 1,440,000 samples (exactly).

- Gaps filled with zeros.
- Sample count never adjusted.

### 3. HDF5 Concurrent Access

All `h5py.File()` calls use `locking=False` to prevent HDF5 library-level file lock contention (errno=11) between concurrent readers/writers. The environment variable `HDF5_USE_FILE_LOCKING=FALSE` is set **before** `import h5py` in services that import h5py at module level.

- **Writers** (`hdf5_writer.py`) open, append, and close per write cycle.
- **Readers** (`hdf5_reader.py`, fusion service) open with `locking=False` for concurrent access.
- Services use open-write-close patterns to minimize dirty flags on unclean shutdown.

---

## System Locations

| Component | Path |
|-----------|------|
| **Code** | `/opt/hf-timestd/venv/lib/python3.*/site-packages/hf_timestd/` |
| **Config** | `/etc/hf-timestd/` |
| **Logs** | `/var/log/hf-timestd/` |
| **Data Root** | `/var/lib/timestd/` |
| **Raw Data** | `/var/lib/timestd/raw_buffer/` |
| **L2 Data** | `/var/lib/timestd/phase2/{CHANNEL}/metrology/` |
| **L3 Data** | `/var/lib/timestd/phase2/fusion/` |
| **IONEX** | `/var/lib/timestd/ionex/` |

---

- This advance partially compensates for propagation delay to users in China.
- For US receivers, this results in a net arrival time that often overlaps with WWV (~8ms delay) or WWVH (~15ms delay).

### Marker Formats & Schedule

BPM alternates between two formats based on the minute of the hour:

| Minutes | Content | Tick Duration | Standard |
|---------|---------|---------------|----------|
| **00-10** | UTC Time | **10 ms** | UTC |
| **10-15** | Carrier Only | *None* | - |
| **15-25** | UTC Time | **10 ms** | UTC |
| **25-29** | **UT1 Time** | **100 ms** | **UT1** |
| **29-30** | Station ID | *None* | - |
| **30-40** | UTC Time | **10 ms** | UTC |
| **40-45** | Carrier Only | *None* | - |
| **45-55** | UTC Time | **10 ms** | UTC |
| **55-59** | **UT1 Time** | **100 ms** | **UT1** |
| **59-60** | Station ID | *None* | - |

> [!NOTE]
> **Detection Impact**:
>
> - **UT1 Minutes (25-29, 55-59):** The 100ms ticks are unique and easy to distinguish from WWV's 5ms ticks. However, they encode UT1, which drifts relative to UTC.
> - **"Flam" Effect:** During UT1 minutes, you may hear a double-tick (click-beep) because the BPM UT1 tick (100ms) drifts against the WWV UTC tick (5ms).

> - **UTC Minutes:** Use 10ms ticks (vs WWV 5ms). This duration difference helps discrimination.

### Search Window Exclusion Zones (v3.14.0)

To prevent the **BPM Discriminator** from incorrectly locking onto a strong **WWV** signal (aliasing), we employ **Exclusion Zones**.

- **Problem**: WWV (Colorado) often arrives ~25ms after the BPM window center (relative time). If BPM is weak, the detector might "find" WWV and claim it is BPM.
- **Solution**: Since we know `time_snap` is accurate (±1ms), we calculate exactly where WWV should appear in the BPM search window and **mask it out**.
- **Implementation**: `StationModel` defines `exclusion_zones` (e.g., `[(22.5, 27.5)]` ms). Any correlation peak within this zone is assigned 0.0 confidence.

---

--------------------------------------------------------------------------------

## International Stations: CHU (Canada)

The CHU digital time code is transmitted from **second 31 to second 39** of every minute. It allows systems to automatically decode the precise time, separate from the primary 1000Hz ticks.

### Transmission Format

The digital code uses the **Bell 103 Audio Frequency-Shift Keying (AFSK)** standard.

- **Data Rate:** 300 baud
- **Modulation:** Frequency-Shift Keying (FSK)
- **Frequencies:**
  - **Mark (bit 1):** 2225 Hz
  - **Space (bit 0):** 2025 Hz
- **Serial Coding:** 8N2 (1 Start, 8 Data, 2 Stop)

During seconds 31-39, the standard pulses are reduced to very short ticks to accommodate the data. The entire 110-bit packet (10 \text{ chars} \times 11 \text{ bits/char}) takes about **0.367 seconds** and ends precisely at **0.500 seconds** past the second marker.

### Time Code Content (Formats)

The 10-byte packet alternates between **Format A** and **Format B**. Each format repeats its data for redundancy (except for the ancillary data distinction).

#### 1. Format A (Seconds 32-39)

Sent 8 times per minute. Contains **UTC Time and Day of Year**. Bytes 1-5 are repeated in Bytes 6-10.

| Field | Content |
|-------|---------|
| **6** | Framing constant (6) |
| **ddd** | Day of Year (001-366) |
| **hh** | UTC Hour |
| **mm** | UTC Minute |
| **ss** | UTC Second |

#### 2. Format B (Second 31 Only)

Sent once per minute. Contains **DUT1, Year, and TAI offset**. Bytes 6-10 are the bitwise inversion (one's complement) of Bytes 1-5.

| Field | Content |
|-------|---------|
| **x** | Leap Second Warning & DUT1 Sign |
| **z** | DUT1 (tenths of a second) |
| **yyyy** | Year (e.g., 2025) |
| **tt** | TAI-UTC Difference (currently 37s) |
| **aa** | Canadian Daylight Time Indicator |

### Time Standards & Corrections

CHU broadcasts key corrections needed for scientific timing:

#### TAI-UTC (Leap Second Offset)

- **TAI (Atomic):** Continuous atomic time scale (never skips).

- **UTC (Civil):** Adjusted with **Leap Seconds** to track Earth's rotation.
- **Difference:** TAI is currently **37 seconds ahead** of UTC.
- **Usage:** Format B allows automatic conversion from UTC to linear TAI.

#### DUT1 (Rotation Correction)

- **UT1 (Astronomical):** True solar time based on Earth's varying rotation.

- **DUT1:** The fine difference $DUT1 = UT1 - UTC$.
- **Values:** Broadcast in **0.1s increments**. International standards keep $|UTC - UT1| < 0.9s$.

---

## Path Management

All file paths are derived from the data root (`/var/lib/timestd/` in production, `/tmp/timestd-test/` in test mode) using consistent conventions:

```python
# Path construction follows a simple pattern:
data_root / "raw_buffer" / channel / date_str / f"{minute}.bin.zst"
data_root / "phase2" / channel / "metrology" / f"{date}_metrology_measurements.h5"
data_root / "phase2" / "fusion" / f"fusion_timing_{date}.h5"
data_root / "products" / channel / "spectrograms" / f"{date}_spectrogram.png"
```

The web API (FastAPI/Python) reads the same HDF5 files directly — no path synchronization needed since both the core library and web API are Python.

---

## Configuration

### Environment-Based Configuration

**Environment File** (single source of truth for paths):

| Mode | Environment File | Data Root |
|------|-----------------|-----------|
| Test | `config/environment` | `/tmp/timestd-test/` |
| Production | `/etc/hf-timestd/environment` | `/var/lib/timestd/` |

```bash
# Production environment file
TIMESTD_MODE=production
TIMESTD_DATA_ROOT=/var/lib/timestd
TIMESTD_LOG_DIR=/var/log/hf-timestd
TIMESTD_CONFIG=/etc/hf-timestd/timestd-config.toml
```

### Config File

**File**: `config/timestd-config.toml` (or `/etc/hf-timestd/timestd-config.toml` in production)

```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"

[ka9q]
status_address = "myhost-hf-status.local"  # mDNS name from radiod config

[recorder]
mode = "test"                              # "test" or "production"
test_data_root = "/tmp/timestd-test"
production_data_root = "/var/lib/timestd"
sample_rate = 24000                        # Config-driven (default 24 kHz)

[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
preset = "iq"
description = "WWV 10 MHz"
enabled = true
processor = "grape"
```

---

## Installation & Startup

### Using install.sh (Recommended)

```bash
# Test mode (development)
./scripts/install.sh --mode test
source venv/bin/activate
python -m hf_timestd --config config/timestd-config.toml

# Production mode (24/7 operation)
sudo ./scripts/install.sh --mode production
```

### Production (systemd)

```bash
# Enable and start all core services
sudo systemctl enable --now timestd-core-recorder
sudo systemctl enable --now timestd-metrology
sudo systemctl enable --now timestd-l2-calibration
sudo systemctl enable --now timestd-fusion
sudo systemctl enable --now timestd-physics
sudo systemctl enable --now timestd-web-api

# Optional services
sudo systemctl enable --now timestd-vtec
sudo systemctl enable --now timestd-radiod-monitor
sudo systemctl enable --now grape-daily.timer

# View logs
journalctl -u timestd-core-recorder -f
journalctl -u timestd-metrology -f

# Deploy updates from git repo
sudo ./scripts/update-production.sh
```

### Directory Structure

| Mode | Data | Logs | Config |
|------|------|------|--------|
| Test | `/tmp/timestd-test/` | `/tmp/timestd-test/logs/` | `config/` |
| Production | `/var/lib/timestd/` | `/var/log/hf-timestd/` | `/etc/hf-timestd/` |

---

## Data Flow (Three-Phase Architecture)

```
ka9q-radio (radiod)
    ↓ RTP multicast (mDNS discovery via ka9q-python)
PHASE 1: Core Recorder (binary_archive_writer.py)
    ↓ 24 kHz binary IQ (.bin.zst) + JSON sidecars
    ↓ {data_root}/raw_buffer/{channel}/{YYYYMMDD}/
PHASE 2: Metrology Service (per channel)
    ├→ Metrology: phase2/{channel}/metrology/ (HDF5 L1/L2)
    ├→ Tick timing: phase2/{channel}/tick_timing/ (HDF5)
    ├→ Detection attempts: phase2/{channel}/detection_attempts/ (HDF5)
    └→ State: phase2/{channel}/state/
PHASE 3: Fusion + Science
    ├→ Fusion: phase2/fusion/ (HDF5 L3, Chrony SHM)
    ├→ TEC: phase2/science/tec/ (HDF5)
    └→ GRAPE: products/ (DRF packaging, spectrograms, PSWS upload)
```

---

## Key Modules

The current package is `hf_timestd` under `src/hf_timestd/`.

### Phase 1: Recording (`src/hf_timestd/core/`)

| Module | Class/Function | Purpose |
|--------|---------------|--------|
| `core_recorder.py` | `CoreRecorder` | Top-level orchestration: channel discovery, per-channel recording via ka9q-python `RadiodStream` |
| `binary_archive_writer.py` | `BinaryArchiveWriter` | Writes `.bin.zst` + `.json` sidecars per minute. Gap filling, RTP timestamp preservation |
| `audio_stream.py` | `AudioStream` | Per-channel RTP reception wrapper around ka9q-python |

### Phase 2: Metrology (`src/hf_timestd/core/`)

| Module | Class/Function | Purpose |
|--------|---------------|--------|
| `metrology_service.py` | `MetrologyService` | Polls raw_buffer for new files, dispatches to MetrologyEngine, writes HDF5 products |
| `metrology_engine.py` | `MetrologyEngine` | Per-minute processing: unified per-second correlator (v6.11), adaptive windowing, multipath-aware uncertainty, D_clock extraction, tick edge ensemble, cross-freq discrimination |
| `tone_detector.py` | `ToneDetector` | 1000/1200 Hz matched filter detection with Cramér-Rao uncertainty, multipath detection, Doppler correction |
| `tick_matched_filter.py` | `TickMatchedFilter` | Per-second tick detection (legacy — no longer used for tick_timing product) |
| `tick_edge_detector.py` | `TickEdgeDetector` | **Primary timing source**: D_clock (front-edge ensemble), Doppler (carrier phase slope), SNR. 50-57 ticks/minute. Inspired by ntpd refclock_wwv.c |
| `timing_bootstrap.py` | `TimingBootstrap` | RTP-to-UTC offset establishment via metadata + NTP confirmation |
| `arrival_pattern_matrix.py` | `ArrivalPatternMatrix` | Physics-based expected arrival predictions using IRI-2020/HFPropagationModel. `BroadcastWindowState` tracks adaptive windows with safeguards (v6.11) |
| `chu_fsk_decoder.py` | `CHUFSKDecoder` | Bell 103 AFSK demodulation for CHU time code (seconds 31-39) |

### Phase 2: Propagation Modeling (`src/hf_timestd/core/`)

| Module | Class/Function | Purpose |
|--------|---------------|--------|
| `propagation_model.py` | `HFPropagationModel` | Multi-mode delay prediction (1F/2F/3F/1E) with numerical Ne(h) integration, adaptive uncertainty |
| `raytrace_engine.py` | `RaytraceEngine` | PHaRLAP 2D ray tracing with spatially varying IRI-2020 Ne(h) grid (science/batch) |
| `iono_data_service.py` | `IonoDataService` | Background thread: WAM-IPE/GIRO data fetching, caching, great-circle TEC sampling |

### Phase 3: Fusion (`src/hf_timestd/core/`)

| Module | Class/Function | Purpose |
|--------|---------------|--------|
| `multi_broadcast_fusion.py` | `MultiBroadcastFusion` | Dual Kalman filtering (L1 geometric + L2 physics), WLS fusion, Chrony SHM feed (TSL1/TSL2) |
| `physics_fusion_service.py` | `PhysicsFusionService` | TEC estimation from multi-frequency measurements, T_iono archival |
| `l2_calibration_service.py` | `L2CalibrationService` | Applies geometric + TEC corrections to produce L2 calibrated timing |
| `timing_validation_service.py` | `TimingValidationService` | GPS ground-truth validation (RTP mode only) |

### I/O Layer (`src/hf_timestd/io/`)

| Module | Class/Function | Purpose |
|--------|---------------|--------|
| `hdf5_writer.py` | `HDF5Writer` | Schema-validated crash-safe HDF5 writes with `locking=False` |
| `hdf5_reader.py` | `HDF5Reader` | Time-range queries on HDF5 datasets |
| `data_product_registry.py` | `DataProductRegistry` | Schema registry for L1/L2/L3 HDF5 products |
| `core/resource_guardian.py` | `ResourceGuardian` | Auto-sizing disk management: 80% cap, day-level eviction, preflight + 60s watchdog |

### GRAPE Pipeline (`src/hf_timestd/grape/`)

| Module | Purpose |
|--------|--------|
| `grape_daily.py` | Daily processing: 24kHz→10Hz decimation, spectrogram generation, DRF packaging |
| `drf_writer.py` | Digital RF HDF5 packaging for PSWS upload |
| `psws_uploader.py` | SFTP upload to HamSCI PSWS network |

### Web API (`web-api/`)

| Component | Purpose |
|-----------|--------|
| `main.py` | FastAPI application with uvicorn, systemd watchdog integration |
| `routers/` | REST API endpoints: dashboard, metrology, phase, propagation, logs, correlations |
| `services/` | Data access layer: reads HDF5 products, computes derived views |
| `static/` | HTML dashboards: metrology, phase/Doppler, ionosphere, Allan deviation, logs |

---

## Dependencies

**Python 3.11+** (installed via `install.sh` or `pip install -e .`):

**Core:**
- `ka9q-python` - Interface to ka9q-radio (from github.com/mijahauan/ka9q-python)
- `numpy>=1.24.0` - Array operations
- `scipy>=1.10.0` - Signal processing, matched filtering
- `h5py>=3.8.0` - HDF5 read/write (all inter-service data exchange)
- `toml` - Configuration parsing
- `zstandard` - Zstd compression for binary IQ archives

**Web API:**
- `fastapi` - REST API framework
- `uvicorn` - ASGI server with systemd watchdog support
- `jinja2` - HTML template rendering

**Optional (GRAPE/ionospheric):**
- `digital_rf` - Digital RF HDF5 packaging (GRAPE upload only)
- `netCDF4` / `boto3` - WAM-IPE data fetching from NOAA S3
- `xarray` - WAM-IPE grid parsing

**System:**
- `avahi-utils` - mDNS resolution for radiod discovery
- `libhdf5-dev` - HDF5 C library (required by h5py)
- `systemd-python` - Watchdog heartbeat integration

**Installation** (automated):

```bash
./scripts/install.sh --mode test      # Development
sudo ./scripts/install.sh --mode production --user $USER  # Production
```

---

## Testing

### Verify Installation

```bash
source venv/bin/activate  # or /opt/hf-timestd/venv/bin/activate
python3 -c "from ka9q import discover_channels; print('ka9q-python OK')"
python3 -c "import h5py; print('h5py OK')"
python3 -c "from hf_timestd.core.metrology_engine import MetrologyEngine; print('MetrologyEngine OK')"
```

### Test Recorder

```bash
source venv/bin/activate
python -m hf_timestd --config config/timestd-config.toml
# Should see: channel connections, raw_buffer file writes
```

### Verify Output Files

```bash
# Raw IQ archives (Phase 1)
ls /var/lib/timestd/raw_buffer/SHARED_10000/$(date +%Y%m%d)/
# Should show .bin.zst + .json files per minute

# HDF5 metrology products (Phase 2)
ls /var/lib/timestd/phase2/SHARED_10000/metrology/
# Should show dated HDF5 files
```

### Verify Pipeline Health

```bash
# Comprehensive pipeline check
sudo ./scripts/verify_pipeline.sh

# Freshness monitoring
sudo ./scripts/check-freshness-alert.sh
```

---

## Debugging

### Check HDF5 Contents

```bash
python3 -c "
import h5py
from pathlib import Path
files = sorted(Path('/var/lib/timestd/phase2/SHARED_10000/metrology/').glob('*.h5'))
if files:
    with h5py.File(files[-1], 'r', locking=False) as f:
        for key in f.keys():
            print(f'{key}: {f[key].shape} {f[key].dtype}')
        print(f'Rows: {f[list(f.keys())[0]].shape[0]}')
else:
    print('No HDF5 files found')
"
```

### Check Raw Buffer Metadata

```bash
# View latest JSON sidecar
cat $(ls -t /var/lib/timestd/raw_buffer/SHARED_10000/$(date +%Y%m%d)/*.json | head -1) | python3 -m json.tool
```

### Check Web API

```bash
curl http://localhost:8000/api/health | python3 -m json.tool
curl http://localhost:8000/api/dashboard/summary | python3 -m json.tool
```

### Check Service Logs

```bash
journalctl -u timestd-fusion --since '5 min ago' --no-pager
journalctl -u timestd-metrology --since '5 min ago' --no-pager
```

---

## Common Issues

### Issue: Cannot connect to radiod

**Symptom**: "Failed to discover channels" error

**Causes**:

1. radiod not running
2. mDNS name not resolving
3. Multicast network issue

**Fix**:

```bash
# Check radiod is running
sudo systemctl status radiod@rx888

# Test mDNS resolution
avahi-resolve -n myhost-hf-status.local

# Test ka9q-python discovery
python3 -c "from ka9q import discover_channels; print(discover_channels('myhost-hf-status.local'))"
```

### Issue: High packet loss

**Symptom**: Completeness < 95%, many gaps

**Causes**:

1. Network congestion
2. CPU overload
3. radiod issues

**Fix**: Check network buffers, reduce channel count if needed:

```bash
sudo sysctl -w net.core.rmem_max=26214400
```

### Issue: Timing quality degraded

**Symptom**: Bootstrap stuck in ACQUIRING or CORRELATING state

**Causes**:

1. Poor propagation (no WWV/CHU signal)
2. radiod not providing GPS+PPS timestamps
3. NTP not synchronized on the host

**Fix**: Check `journalctl -u timestd-metrology` for bootstrap state transitions. Normal during poor propagation — system will lock when signals return.

### Issue: HDF5 file lock errors

**Symptom**: `OSError: [Errno 11] Unable to synchronously open file (unable to lock file)`

**Causes**:

1. `HDF5_USE_FILE_LOCKING=FALSE` not set before h5py import
2. Missing `locking=False` on `h5py.File()` calls

**Fix**: Ensure `os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"` is set BEFORE `import h5py`. All `h5py.File()` calls must use `locking=False`.

---

## Performance Targets

### Core Recorder

- CPU: <5% per channel
- Memory: ~100 MB total
- Disk write: ~2-3 MB/min per channel (zstd-compressed binary IQ)
- Latency: <100 ms (RTP → disk)

### Metrology

- CPU: Variable (batch processing, per-minute)
- Processing: Can lag behind real-time; processes backlog on restart
- Per-minute: tone detection (up to 57 ticks), D_clock extraction, tick edge ensemble

### Fusion

- Cycle interval: 8 seconds (configurable via `--interval`)
- Reads L2 HDF5 from all 9 channels per cycle
- Dual Kalman update + WLS fusion + Chrony SHM write

---

## Quality Metrics

### Bootstrap State Progression

| State | Description | Typical Time |
|-------|-------------|-------------|
| **ACQUIRING** | Searching for tone clusters | 0-1 min |
| **CORRELATING** | Validating cluster consistency at 60s intervals | 1-2 min |
| **TRACKING** | Clusters validated, awaiting NTP confirmation | 2 min |
| **LOCKED** | Time confirmed, offset stable, continuous validation | 2+ min |

### Fusion Quality Grades

| Grade | Fused Uncertainty | Description |
|-------|-------------------|-------------|
| **A** | < 0.5 ms | Excellent — requires L5/L6 hardware + long averaging |
| **B** | < 1.0 ms | Good — achievable with GPSDO + multi-station fusion |
| **C** | < 2.0 ms | Typical — standard operation |
| **D** | ≥ 2.0 ms | Degraded — limited stations or high ionospheric activity |

### Data Completeness

- **Target:** >99% samples received
- **Gaps:** Zero-filled, logged in JSON sidecar metadata
- **Packet loss:** <1% healthy
- **Completeness colors:** 🟢 ≥99% | 🟡 95-99% | 🔴 <95%

---

## References

### Key Documents

- `docs/ARCHITECTURE.md` - System design decisions
- `docs/METROLOGY.md` - Metrological description and uncertainty budgets
- `docs/PHYSICS.md` - Ionospheric physics capabilities
- `INSTALLATION.md` - Setup guide
- `docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md` - Production deployment and verification gates
- `docs/GPS_TEC_OPTIONAL.md` - Optional GNSS TEC validation

### External

- ka9q-radio: <https://github.com/ka9q/ka9q-radio>
- ka9q-python: <https://github.com/mijahauan/ka9q-python>
- Digital RF: MIT Haystack Observatory

---

**Version**: 6.11.0  
**Last Updated**: March 19, 2026  
**Purpose**: Technical reference for HF Time Standard developers

**v6.7.1 Release (February 12, 2026) - Propagation Model Full Integration:**

- **Full pipeline migration** — `multi_broadcast_fusion.py` and `bootstrap_validator.py` migrated from `PhysicsPropagationModel` to `HFPropagationModel`.
- **Great-circle TEC sampling** — `IonoDataService._gc_intermediate()` uses spherical trigonometry for accurate path TEC.
- **Altitude-dependent obliquity** — Thin-shell mapping `M(h) = 1/sqrt(1-(R·cos(e)/(R+h))²)` replaces `1/sin(e)`.
- **Web API endpoints** — `/model/predict`, `/model/all-stations`, `/model/iono-status` for live model observability.
- **Self-consistency check wired** — `HFPropagationModel.self_consistency_check()` integrated into `ArrivalPatternMatrix`.
- **Deprecated** — `physics_propagation.py` (`PhysicsPropagationModel`) retained for backward compatibility only.

**v6.7.0 Release (February 12, 2026) - Real-Time Ionospheric Propagation Model:**

- **Real-time ionospheric data** — `IonoDataService` fetches WAM-IPE grids from NOAA S3 and GIRO ionosonde data for real-time hmF2/foF2 corrections.
- **Physics-based group delay** — `HFPropagationModel` computes frequency-dependent ionospheric delay via numerical Ne(h) integration or TEC-based formula (40.3×sTEC/(c×f²)).
- **Multi-mode predictions** — Evaluates 1F, 2F, 3F, 1E propagation modes with MUF checks and geometric feasibility.
- **Adaptive uncertainty** — Windows adapt from ±1.5 ms (WAM-IPE+GIRO) to ±15 ms (no model), blended with tracked variance.
- **Self-consistency check** — Multi-frequency differential delay validates model TEC predictions.
- **Backward compatible** — `ArrivalMatrix.arrivals` dict unchanged; new `multi_mode_arrivals` dict adds multi-hop support.
- **23 new tests** — All passing, 0 regressions in existing test suite.

**v6.6.0 Release (February 9, 2026) - Authoritative RTP Timestamps:**

- **Pipeline offset calibration removed** — radiod's RTP timestamps are authoritative (GPS+PPS, ~50 μs). No wall-clock calibration needed.
- **Tightened tolerances** — Arrival tolerance: ±200ms → ±100ms. Bootstrap window: ±150ms → ±50ms.
- **CPU affinity** — All timestd Python services pinned to CPUs 0-7; radiod on CPUs 8-15 for uncontested L3 cache.
- **Crash-safe HDF5** — SWMR eliminated; open-write-close per measurement prevents dirty flags on crash.
- **Dual-purpose framing** — RTP Mode (physics/ionospheric science) vs Fusion Mode (timing recovery).

**v6.5.0 Release (February 4, 2026) - Physics-Based Validation + TEC Feedback:**

- **Dual-Purpose Architecture** - System serves both timing reconstruction and ionospheric characterization
- **Physics-Based Validation** - ArrivalPatternMatrix validates detections against physics predictions, not history
- **Multi-Constraint Validation** - TimingConsistencyValidator exploits arrival sequence, cross-station, cross-frequency constraints
- **Real-Time TEC Feedback** - Measured TEC feeds back to refine arrival predictions using 1/f² law
- **TID Detection** - Cross-path correlation for traveling ionospheric disturbance detection (`tid_detector.py`)
- **Station Priority Policy** - CHU/WWV/WWVH as primary anchors, BPM at 30% weight for scientific interest
- **T_iono Archival** - Ionospheric residual products archived to HDF5

**v6.2.0 Release (January 24, 2026) - Metrological Enhancements:**

- **Cramér-Rao Bound Uncertainty** - Rigorous ToA uncertainty calculation based on SNR, bandwidth, and duration
- **Complex Correlation with Phase** - Preserves phase for sub-sample timing and Doppler estimation
- **Multipath Detection** - Integrated into tone detector with uncertainty inflation
- **Doppler Correction** - Removes systematic timing bias from ionospheric motion
- **Adaptive SNR Threshold** - CFAR-like approach improves sensitivity in varying conditions
- **CHU Tick Timing** - High-precision timing from 1000 Hz tick (~0.05 ms vs ~1-2 ms from FSK)
- **New ToneDetectionResult Fields** - `timing_uncertainty_ms`, `multipath_detected`, `multipath_delay_spread_ms`, `multipath_quality`, `doppler_hz`, `phase_at_peak_rad`

**v2.2.0 Release (Dec 2, 2025):**

- **Unified Install Script** - `install.sh` for test/production modes
- **FHS-Compliant Paths** - `/var/lib/timestd/`, `/var/log/hf-timestd/`
- **systemd Services** - Production-ready 24/7 operation
- **Cross-Channel Coherent Timing** - Global Station Lock, ensemble anchor selection
- **Primary Time Standard** - UTC(NIST) back-calculation from arrival time
- **PPM Correction** - ADC clock drift compensation (±25 μs precision)
- **Documentation Overhaul** - All root-level docs updated for consistency

**v2.1.0 (Dec 1, 2025):**

- **Package Restructure** - `core/`, `stream/`, `grape/`, `wspr/` packages
- **Stream API** - SSRC-free `subscribe_stream()` interface
- **ka9q-python 3.1.0** - Compatible SSRC allocation algorithm
- **Sample Rate** - 24 kHz (was 16 kHz)

**v2.0.0 (Nov 30, 2025):**

- **Generic Recording Infrastructure** - Protocol-based design for multi-app support
- **GRAPE Refactor** - `GrapeRecorder`, `GrapeNPZWriter`
- **12 Voting Methods** - FSS, noise coherence, spreading factor added
- **Test Signal Channel Sounding** - Full exploitation of :08/:44 minutes

**Previous (Nov 28-29, 2025):**

- 12 cross-validation checks
- 500/600 Hz weight boosted to 15 for exclusive minutes
- Doppler vote changed to std ratio
- Coherence quality check, harmonic signature validation

## Adaptive Search Window System (v3.9.0)

### Overview

Implements intelligent tone search window narrowing using GPSDO stability for rapid convergence.

### Algorithm

**Phase Detection:**

```python
if calibrator.phase == CALIBRATED and expected_toa available:
    window = ±2-5ms  # Learned ToA
elif calibrator.phase == PROVISIONAL:
    window = ±5-15ms  # Physics prediction
else:  # BOOTSTRAP
    window = ±500ms  # Wide search
```

**Convergence Criteria:**

- **Provisional**: 10+ detections, 2+ stations, <10 minutes, D_clock σ < 1ms
- **Calibrated**: 30+ detections, 60min span, RTP variance < 50²

## Steel Ruler Kalman Filter (v5.3)

### Parameters

To model a GPSDO-disciplined clock, we use extreme parameters that effectively "freeze" the clock model and force all variance into the measurement noise (ionosphere).

| Parameter | Value | Description |
|-----------|-------|-------------|
| **Initial P (Offset)** | 5.0 ms | Moderate initial trust (was 100ms) |
| **Initial P (Drift)** | 1e-7 ms/min | Very high initial trust in factory calibration |
| **Q (Offset)** | 0.01 ms | Allows filter to track real measurements (increased from 1e-10 to fix dead Kalman filter, 2026-02-06) |
| **Q (Drift)** | 1e-12 ms/min | The clock does not wander |
| **R (Measurement)** | 30.0 ms | High measurement noise to reject ionospheric turbulence |

### Logic

- **Drift Clamping:** `drift_ms_per_min` is forced to `0.0` after convergence.
- **Innovation Check:** Updates are skipped if innovation > 3σ (outlier rejection).

**Back-Off Logic:**

- Trigger: 5+ consecutive detection failures
- Action: Widen window by 1.5×, max 500ms
- Recovery: Narrow again when detections resume

### Per-Broadcast Tracking

Each station+frequency combination maintains independent state:

```python
class BroadcastState:
    mean_toa_ms: float          # Learned arrival time
    std_toa_ms: float           # Observed variance
    window_ms: float            # Current search window
    phase: CalibrationPhase     # BOOTSTRAP/PROVISIONAL/CALIBRATED
    consecutive_failures: int   # For back-off logic
```

**Example:**

- WWV @ 10MHz: 12.4±0.8ms, ±2ms window (1-hop, converged)
- WWV @ 5MHz: 24.7±1.2ms, ±5ms window (2-hop, converging)

### Shared Frequency Handling

**Anchor channels** (unique frequencies):

- CHU: 3.33, 7.85, 14.67 MHz
- WWV: 20, 25 MHz
- Used to establish initial GPSDO lock

**Contested channels** (shared frequencies):

- 2.5, 5, 10, 15 MHz (WWV + WWVH + BPM)
- Require discrimination (BCD, Doppler, timing)
- Each station tracked independently

### Performance

**Convergence timeline:**

- 0-10 min: Bootstrap (±500ms)
- 10-30 min: Provisional (±15ms → ±5ms)
- 30+ min: Calibrated (±2ms)

**Benefits:**

- Higher SNR (less noise in narrow window)
- Better sensitivity (detect weaker signals)
- Ionospheric measurements (ToA variations = propagation changes)

## Sample Rate Evolution

### Current: 24 kHz (24000 Hz)

**Rationale for 24 kHz:**

- **Test Signal Analysis**: Ensures adequate Nyquist margin for WWV/WWVH test signals (500 Hz, 600 Hz, and intermodulation products)
- **Mathematical Compatibility**: Avoids bin mismatches in FFT analysis
  - 24000 Hz is evenly divisible by common signal frequencies (1500 Hz, 500 Hz, 600 Hz)
  - Prevents fractional bin assignments that could degrade spectral analysis
- **Timing Precision**: ~42 μs per sample (1/24000 s)

**Historical Evolution:**

1. **16 kHz** (original): Adequate for 1 kHz tone detection, but marginal for test signals
2. **20 kHz** (v3.x): Improved test signal analysis with better Nyquist margin
3. **24 kHz** (v5.x): Optimal for mathematical compatibility and comprehensive signal analysis

**Key Calculations:**

- Samples per minute: 24000 × 60 = **1,440,000 samples**
- RTP timestamp wraparound: 2³² / 24000 / 3600 ≈ **49.7 hours**
- Timing resolution: 1 / 24000 ≈ **41.67 μs**
