# HF Time Standard (hf-timestd) - Technical Reference

**Quick reference for developers working on the HF Time Standard (hf-timestd) codebase.**

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 31, 2025

---

## Current Operational Configuration

**9 channels** monitoring 9 frequencies at 24 kHz IQ (config-driven):

- **Shared frequencies (4):** 2.5, 5, 10, 15 MHz - WWV and WWVH both transmit
- **WWV-only (2):** 20, 25 MHz
- **CHU (3):** 3.33, 7.85, 14.67 MHz

**Data products generated**:

1. **24 kHz Digital RF (HDF5)** - Phase 1 immutable raw archive (`raw_archive/{CHANNEL}/`)
2. **Phase 2 Analytics (HDF5)** - L1 Tone Detections & L2 Timing Measurements (`phase2/{CHANNEL}/`)
3. **Phase 3 Fusion (HDF5)** - L3 Fused Timing & Global Science Products (`phase2/fusion/`)
4. **Spectrograms** - Visualizations with solar zenith (`products/{CHANNEL}/spectrograms/`)

**Goal**: Archive raw 24 kHz IQ (Phase 1), perform timing analysis (Phase 2), generate derived products (Phase 3) for PSWS upload, provide WWV/WWVH discrimination on 4 shared frequencies.

---

## System Architecture: The Six Services

The system is composed of six independent systemd services, each with a specific responsibility in the data pipeline.

### 1. Core Recorder (`timestd-core-recorder`)

**Responsibility:** Reliable Data Capture

- Consumes RTP multicast streams from `ka9q-radio`.
- Writes **Digital RF** formatted HDF5 files (`.h5`).
- Maintains sample count integrity (gap filling).
- **Output:** `/var/lib/timestd/raw_archive/`

### 2. Analytics (`timestd-analytics`)

**Responsibility:** Signal Processing & Timing Extraction

- Polls for new Digital RF files.
- Performs tone detection (1000/1200 Hz), BCD decoding, and WWV/WWVH discrimination.
- Calculates `D_clock` (System - UTC) using physics propagation models.
- **Output:** `/var/lib/timestd/phase2/{CHANNEL}/` (HDF5 L1/L2)

### 3. Fusion (`timestd-fusion`)

**Responsibility:** Multi-Broadcast Synthesis

- Reads L2 HDF5 measurements from all 9 channels via **SWMR** (low latency).
- Performs weighted fusion, Kalman filtering, and global consistency checks.
- Feeds **Chrony SHM** to discipline the system clock.
- **Output:** `/var/lib/timestd/phase2/fusion/` (HDF5 L3, Fused CSV)

### 4. VTEC (`timestd-vtec`)

**Responsibility:** Ionospheric Data Acquisition

- Polls GNSS receiver (ZED-F9P) for dual-frequency observables.
- Downloads global IONEX maps from NASA CDDIS.
- **Output:** `/var/lib/timestd/gnss_vtec.h5`, `/var/lib/timestd/ionex/`

### 5. Scientific Aggregator (`timestd-science-aggregator`)

**Responsibility:** Higher-Level Science Products

- Aggregates multi-channel data for Total Electron Content (TEC) estimation.
- Generates spectrograms and summary plots.
- **Output:** `/var/lib/timestd/products/`

### 6. Web UI (`timestd-web-ui-fastapi`)

**Responsibility:** User Visualization

- FastAPI-based web server.
- Serves real-time dashboard (`metrology.html`, `ionosphere.html`).
- Reads status from all other services.

---

## Data Formats

### 1. Raw Archive: Digital RF (HDF5)

We use the **Digital RF** standard (MIT Haystack) for storing raw IQ data.

- **Format:** HDF5 with `drf_properties` attribute.
- **Structure:**
  - `/rf_data`: Dataset containing complex64 IQ samples.
  - `/rf_data_index`: Index mapping sample ranges to timestamps.
- **Metadata:** Global start time, sample rate (24 kHz), center frequency.

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

## Physics & Propagation Modeling (V5.0)

To convert "Arrival Time" to "Emission Time", we must rigorously model the flight path.

### 1. Integrated Ionospheric Model

The `PhysicsPropagationModel` class integrates three tiers of data:

- **Tier 1: IONEX (Global Ionosphere Maps):**
  - Uses `.i` files from IGS/NASA.
  - Calculates the **Ionospheric Pierce Point (IPP)** at 350km altitude.
  - Interpolates VTEC from the grid (lat/lon/time).
  - Converts VTEC to Group Delay: $\tau_{iono} \propto \frac{TEC}{f^2}$

- **Tier 2: IRI-2020:**
  - Uses the International Reference Ionosphere model when IONEX is unavailable.
  - Estimates `hmF2` (layer height) and statistical monthly VTEC.

- **Tier 3: Geometric/Empirical:**
  - Fallback model based on path distance and solar zenith angle.

### 2. Path Mid-Point Correction

Ionospheric delay is determined by the electron density at the **reflection point**, not the transmitter or receiver.

- **Algorithm:**
    1. Calculate Great Circle path.
    2. Determine path midpoint (for 1-hop) or reflection points (for N-hop).
    3. Query IONEX/IRI at those specific coordinates.

---

## Critical Design Principles

### 1. RTP Timestamp is Primary Reference

**Not wall clock.** System time is derived from RTP via `time_snap`.

```python
# Precise time reconstruction:
utc = time_snap_utc + (rtp_ts - time_snap_rtp) / sample_rate
```

### 2. Sample Count Integrity

**Invariant**: 24 kHz × 60 sec = 1,440,000 samples (exactly).

- Gaps filled with zeros.
- Sample count never adjusted.

### 3. HDF5 SWMR (Single Writer, Multiple Reader)

To achieve low latency while maintaining archival integrity, we use HDF5's SWMR feature.

- **Analytics** creates the file and switches to SWMR mode.
- **Fusion** opens the file in SWMR read mode.
- Analytics periodically calls `.flush()` and `.refresh()` to make new rows visible to Fusion within milliseconds.

---

## System Locations

| Component | Path |
|-----------|------|
| **Code** | `/opt/hf-timestd/venv/lib/python3.11/site-packages/hf_timestd/` |
| **Config** | `/etc/hf-timestd/` |
| **Logs** | `/var/log/hf-timestd/` |
| **Data Root** | `/var/lib/timestd/` |
| **Raw Data** | `/var/lib/timestd/raw_archive/` |
| **L2 Data** | `/var/lib/timestd/phase2/{CHANNEL}/timing_measurements/` |
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

## File Paths: Python/JavaScript Sync

**Problem**: Dual-language system needs identical paths.

**Solution**: Centralized APIs

**Python** (`src/grape_recorder/paths.py`):

```python
class GRAPEPaths:
    def get_quality_csv_path(self, channel):
        return self.analytics_dir / channel / "quality" / f"{channel}_quality.csv"
```

**JavaScript** (`web-ui/grape-paths.js`):

```javascript
class GRAPEPaths {
    getQualityCSVPath(channel) {
        return path.join(this.analyticsDir, channel, 'quality', `${channel}_quality.csv`);
    }
}
```

**Validation**: `./scripts/validate-paths-sync.sh`

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
GRAPE_MODE=production
GRAPE_DATA_ROOT=/var/lib/timestd
GRAPE_LOG_DIR=/var/log/grape-recorder
GRAPE_CONFIG=/etc/hf-timestd/timestd-config.toml
GRAPE_VENV=/opt/grape-recorder/venv
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
./scripts/timestd-all.sh -start

# Production mode (24/7 operation)
sudo ./scripts/install.sh --mode production --user $USER
sudo systemctl start grape-recorder timestd-analytics grape-webui
sudo systemctl enable grape-recorder timestd-analytics grape-webui
```

### Manual Startup (Development)

```bash
cd ~/grape-recorder
source venv/bin/activate
python -m grape_recorder.grape.core_recorder --config config/timestd-config.toml
```

### Production (systemd)

```bash
# Service control
sudo systemctl start|stop|status grape-recorder
sudo systemctl start|stop|status timestd-analytics
sudo systemctl start|stop|status grape-webui

# View logs
journalctl -u grape-recorder -f
journalctl -u timestd-analytics -f

# Enable daily uploads
sudo systemctl enable --now grape-upload.timer
```

### Directory Structure

| Mode | Data | Logs | Config |
|------|------|------|--------|
| Test | `/tmp/timestd-test/` | `/tmp/timestd-test/logs/` | `config/` |
| Production | `/var/lib/timestd/` | `/var/log/grape-recorder/` | `/etc/hf-timestd/` |

---

## Data Flow (Three-Phase Architecture)

```
ka9q-radio (radiod)
    ↓ RTP multicast (mDNS discovery via ka9q-python)
PHASE 1: Core Recorder (core_recorder.py)
    ↓ 24 kHz DRF archive
    ↓ {data_root}/raw_archive/{channel}/
    ↓ {data_root}/raw_buffer/{channel}/ (binary minute buffers)
PHASE 2: Analytics Service (per channel)
    ├→ D_clock: phase2/{channel}/clock_offset/
    ├→ Discrimination: phase2/{channel}/discrimination/
    ├→ BCD correlation: phase2/{channel}/bcd_correlation/
    ├→ Carrier analysis: phase2/{channel}/carrier_analysis/
    └→ State: phase2/{channel}/state/
PHASE 3: Derived Products
    ├→ Decimated 10 Hz: products/{channel}/decimated/
    ├→ Spectrograms: products/{channel}/spectrograms/
    └→ SFTP upload to PSWS
```

---

## Key Modules

### Core Infrastructure (`src/grape_recorder/core/`)

- `recording_session.py` - Generic RTP→segments session manager
- `rtp_receiver.py` - Multi-SSRC RTP demultiplexer
- `packet_resequencer.py` - RTP packet ordering & gap detection

### Stream API (`src/grape_recorder/stream/`)

- `stream_api.py` - `subscribe_stream()` and convenience functions
- `stream_manager.py` - SSRC allocation, lifecycle, stream sharing
- `stream_spec.py` - Content-based stream identity
- `stream_handle.py` - Opaque handle returned to applications

### GRAPE Application (`src/grape_recorder/grape/`)

**Core Recording:**

- `grape_recorder.py` - Two-phase recorder (startup → recording)
- `grape_npz_writer.py` - SegmentWriter for NPZ output
- `core_recorder.py` - Top-level GRAPE orchestration
- `analytics_service.py` - NPZ watcher, 12-method processor

**Timing (Advanced):**

- `time_snap_reference.py` - Immutable timing anchor with PPM correction
- `ppm_estimator.py` - ADC clock drift measurement, exponential smoothing
- `tone_detector.py` - 1000/1200 Hz timing tones with sub-sample peak detection
- `startup_tone_detector.py` - Initial time_snap establishment
- `global_station_voter.py` - Cross-channel anchor tracking
- `station_lock_coordinator.py` - Three-phase coherent detection
- `propagation_mode_solver.py` - N-hop geometry, mode identification
- `primary_time_standard.py` - UTC(NIST) back-calculation

**Discrimination:**

- `wwvh_discrimination.py` - 12 voting methods, cross-validation
- `discrimination_csv_writers.py` - Per-method CSV output
- `bcd_discriminator.py` - 100 Hz time code dual-peak detection
- `tick_analyzer.py` - 5ms tick coherent/incoherent analysis
- `test_signal_analyzer.py` - Minutes :08/:44 channel sounding

**Processing:**

- `decimation.py` - 24 kHz → 10 Hz (multi-stage CIC+FIR)
- `doppler_estimator.py` - Per-tick frequency shift measurement

### WSPR Application (`src/grape_recorder/wspr/`)

- `wspr_recorder.py` - Simple recorder for WSPR
- `wspr_wav_writer.py` - SegmentWriter for 16-bit WAV output

### DRF & Upload

- `drf_batch_writer.py` - 10 Hz NPZ → Digital RF HDF5
- Wsprdaemon-compatible multi-subchannel format

### Infrastructure

- `paths.py` - Centralized path management (GRAPEPaths API)
- `channel_manager.py` - Channel configuration

### Web UI (`web-ui/`)

- `monitoring-server-v3.js` - Express API server
- `grape-paths.js` - JavaScript path management (synced with Python)

---

## Dependencies

**Python 3.10+** (installed via `install.sh` or `pip install -e .`):

- `ka9q-python` - Interface to ka9q-radio (from github.com/mijahauan/ka9q-python)
- `numpy>=1.24.0` - Array operations
- `scipy>=1.10.0` - Signal processing, decimation
- `digital_rf>=2.6.0` - Digital RF HDF5 format
- `zeroconf` - mDNS discovery for radiod
- `toml` - Configuration parsing
- `soundfile` - Audio file I/O (compatibility)

**Node.js 18+** (for web-ui):

- `express` - API server
- `ws` - WebSocket support
- See `web-ui/package.json` for full list

**System**:

- `avahi-utils` - mDNS resolution
- `libhdf5-dev` - Required for digital_rf

**Installation** (automated):

```bash
./scripts/install.sh --mode test      # Development
sudo ./scripts/install.sh --mode production --user $USER  # Production
```

---

## Testing

### Verify Installation

```bash
source venv/bin/activate  # or /opt/grape-recorder/venv/bin/activate
python3 -c "import digital_rf; print('Digital RF OK')"
python3 -c "from ka9q import discover_channels; print('ka9q-python OK')"
python3 -c "from grape_recorder.grape.time_snap_reference import TimeSnapReference; print('TimeSnapReference OK')"
```

### Test Recorder

```bash
./scripts/timestd-all.sh -start
# Should see: channel connections, NPZ file writes
```

### Verify Output Files

```bash
ls /tmp/timestd-test/archives/WWV_10_MHz/*.npz
# Should show timestamped NPZ files
```

### Verify Timing

```bash
python3 -c "
import numpy as np
from pathlib import Path
f = sorted(Path('/tmp/timestd-test/archives/WWV_10_MHz/').glob('*.npz'))[-1]
d = np.load(f, allow_pickle=True)
print(f'Time_snap source: {d[\"time_snap_source\"]}')
print(f'PPM: {d.get(\"ppm\", \"N/A\")}')
print(f'Clock ratio: {d.get(\"clock_ratio\", \"N/A\")}')
"
```

---

## Debugging

### Check NPZ Contents

```bash
python3 -c "
import numpy as np
from pathlib import Path
f = sorted(Path('/tmp/timestd-test/archives/WWV_10_MHz/').glob('*.npz'))[-1]
d = np.load(f, allow_pickle=True)
print(f'File: {f.name}')
print(f'Samples: {len(d[\"iq\"])}')
print(f'Gaps: {d[\"gaps_count\"]}')
print(f'Completeness: {100*(1 - d[\"gaps_filled\"]/len(d[\"iq\"])):.1f}%')
print(f'Time_snap source: {d[\"time_snap_source\"]}')
print(f'1000 Hz power: {d[\"tone_power_1000_hz_db\"]:.1f} dB')
"
```

### Check Web UI API

```bash
curl http://localhost:3000/api/v1/summary | jq
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

**Symptom**: time_snap_source shows "ntp" instead of "wwv_startup"

**Causes**:

1. Poor propagation (no WWV/CHU signal)
2. Startup tone detection failed

**Fix**: Normal during poor propagation. System falls back to NTP timing (±10ms vs ±1ms).

---

## Performance Targets

### Core Recorder

- CPU: <5% per channel
- Memory: ~100 MB total
- Disk write: ~2 MB/min per channel (compressed NPZ)
- Latency: <100 ms (RTP → disk)

### Analytics

- CPU: Variable (batch processing)
- Processing: Can lag behind real-time
- 6 discrimination methods per minute per channel

---

## Quality Metrics

### Timing Quality Levels

| Level | Accuracy | Source | Description |
|-------|----------|--------|-------------|
| **TONE_LOCKED** | ±25 μs | WWV/CHU tone + PPM | Sub-sample peak detection with ADC drift correction |
| **TONE_LOCKED** | ±1 ms | WWV/CHU tone | Standard tone detection without PPM |
| **NTP_SYNCED** | ±10 ms | System NTP | NTP fallback when no tone detected |
| **INTERPOLATED** | ±1 ms/hr | Aged time_snap | Drifts ~1 ms/hour without refresh |
| **WALL_CLOCK** | ±seconds | System clock | Unsynchronized, mark for reprocessing |

### Cross-Channel Timing Quality

| Metric | Target | Description |
|--------|--------|-------------|
| **Station Lock** | >90% channels | High-confidence tone detection across array |
| **Anchor Consensus** | <1 ms spread | All channels agree on station arrival time |
| **PPM Consistency** | <10 ppm | ADC drift should be stable across session |

### Data Completeness

- **Target:** >99% samples received
- **Gaps:** Zero-filled, logged in NPZ metadata
- **Packet loss:** <1% healthy
- **Completeness colors:** 🟢 ≥99% | 🟡 95-99% | 🔴 <95%

---

## References

### Key Documents

- `ARCHITECTURE.md` - System design decisions
- `DIRECTORY_STRUCTURE.md` - Path conventions
- `CANONICAL_CONTRACTS.md` - API standards
- `INSTALLATION.md` - Setup guide
- `docs/PRODUCTION.md` - Production deployment with systemd

### External

- ka9q-radio: <https://github.com/ka9q/ka9q-radio>
- ka9q-python: <https://github.com/mijahauan/ka9q-python>
- Digital RF: MIT Haystack Observatory

---

**Version**: 2.2.0  
**Last Updated**: December 2, 2025  
**Purpose**: Technical reference for GRAPE Signal Recorder developers

**v2.2.0 Release (Dec 2, 2025):**

- **Unified Install Script** - `install.sh` for test/production modes
- **FHS-Compliant Paths** - `/var/lib/timestd/`, `/var/log/grape-recorder/`
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

