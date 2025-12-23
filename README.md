# HF Time Standard Analysis

**Precision WWV/CHU time-standard analysis for UTC alignment** - Captures high-precision IQ data from ka9q-radio, performs multi-method WWV/WWVH discrimination, and produces D_clock measurements for system clock discipline.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

HF Time Standard Analysis (`hf_timestd`) receives WWV/WWVH/CHU/BPM time standard broadcasts via ka9q-radio and produces precise timing measurements (D_clock) for UTC alignment and system clock discipline via Chrony.

**Key Capabilities:**
- 📡 **Multi-channel recording** - Simultaneous WWV, WWVH, CHU, BPM (9 tuned frequencies, 17 logical broadcasts)
- 🎯 **Sub-millisecond timing** - ±0.5 ms via multi-broadcast fusion to UTC(NIST)
- 🔗 **Multi-broadcast fusion** - Combines WWV/WWVH/CHU/BPM with per-station calibration
- 🌍 **Cross-frequency global differential solve** - Physics-verified timing constraint across all channels (WWV/WWVH/BPM/CHU)
- ⏱️ **HF time transfer** - D_clock measurement with ionospheric propagation mode estimation
- 🧠 **AI Discrimination** - Probabilistic Logistic Regression + Heuristic Voting for station ID
- 🔬 **Shared Frequency Analysis** - Resolves WWV/WWVH/BPM on 2.5, 5, 10, 15 MHz
- 🌐 **Web UI** - Real-time monitoring, timing visualizations, quality metrics
- ⏰ **Chrony integration** - SHM refclock for system clock discipline

**Note:** Decimation to 10 Hz, spectrograms, and PSWS upload are in a separate GRAPE application.

## Quick Start

**Prerequisites:** ka9q-radio running, Linux with multicast networking, Python 3.10+, Node.js 18+

### Test Mode (Development)

```bash
# Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# Run installer in test mode
./scripts/install.sh --mode test

# Edit configuration with your station details
nano config/timestd-config.toml

# Start all services
./scripts/timestd-all.sh -start

# Check status
./scripts/timestd-all.sh -status
```

### Production Mode (24/7 Operation)

```bash
# Run installer in production mode
sudo ./scripts/install.sh --mode production --user $USER

# Edit configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# Start and enable services
sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
```

### Service Control

**Test Mode (scripts):**
```bash
./scripts/timestd-all.sh -start|-stop|-status    # All services
./scripts/timestd-core.sh -start|-stop|-status   # Core recorder only
./scripts/timestd-analytics.sh -start|-stop|-status  # Analytics (9 channels)
./scripts/timestd-ui.sh -start|-stop|-status     # Web UI only
```

**Production Mode (systemd):**
```bash
sudo systemctl start|stop|status timestd-core-recorder
sudo systemctl start|stop|status timestd-analytics
sudo systemctl start|stop|status timestd-web-ui
journalctl -u timestd-core-recorder -f    # View logs
```

### Directory Structure

| Mode | Data | Logs | Config |
|------|------|------|--------|
| **Test** | `/tmp/timestd-test/` | `/tmp/timestd-test/logs/` | `config/timestd-config.toml` |
| **Production** | `/var/lib/hf-timestd/` | `/var/log/hf-timestd/` | `/etc/hf-timestd/` |

**Monitor:** Open `http://localhost:3000` for real-time channel health, quality metrics, and logs.

## Architecture

**Two-phase design** built on a **generic recording infrastructure**:

ka9q-radio (RTP multicast) → Core Recorder → Analytics → Chrony SHM
                              20kHz Binary   D_clock    System clock
                              raw_buffer/    phase2/    discipline

### ka9q-python Integration (V3.11)

The recording layer leverages **ka9q-python** for all RTP and channel management:

```
Application Layer (CoreRecorderV2, StreamRecorderV2)
        ↓
PipelineOrchestrator (Phase 1/2/3 coordination)
        ↓
ka9q-python RadiodStream (RTP reception, resequencing, decoding)
        ↓
ka9q-python RadiodControl (channel creation, configuration)
        ↓
ka9q-radio (radiod) via multicast
```

**ka9q-python provides:**
- **RadiodStream** - RTP reception, packet resequencing, gap detection, sample decoding
- **RadiodControl** - Channel creation, configuration, tune commands
- **discover_channels()** - Enumerate existing channels from radiod status
- **StreamQuality** - Completeness, packets lost/resequenced, gap metrics

### Generic Recording Infrastructure (V3 - Adapter Pattern)

The recording layer uses a **protocol-based design** enabling multiple applications:

```
Application Layer (TimeStdRecorder, WsprRecorder, etc.)
        ↓
SegmentWriter Protocol (GrapeNPZWriter, WAVWriter, etc.)
        ↓
RecordingSession (generic packet flow, resequencing, segmentation)
        ↓
Protocol Adapter (RadiodStream / RTPReceiver)
```

**Key Abstractions:**
- **SegmentWriter Protocol** - Apps implement storage format
- **RecordingSession** - Generic packet flow, resequencing, segmentation
- **Protocol Adapter** - Abstracts transport (ka9q-python RadiodStream or legacy RTP)

### 1. Core Recorder (`src/hf_timestd/core/core_recorder_v2.py`)

Rock-solid RTP capture using ka9q-python RadiodStream:
- Uses `RadiodStream` for RTP reception, resequencing, and sample decoding
- Uses `RadiodControl` for channel creation with anti-hijacking protection
- Deterministic multicast IP from station_id + instrument_id
- 20 kHz complex64 IQ → binary archive (1,200,000 samples/minute)

**Anti-hijacking:** Only modifies channels with our multicast destination. Creates new channels at same frequency if others exist (radiod supports multiple clients).

**20 kHz Archive Metadata** (self-contained scientific record):
- **IQ Data:** Complex64 samples, gap-filled with zeros
- **Timing Reference:** RTP timestamp of first sample, sample rate, SSRC
- **Time_snap Anchor:** RTP/UTC calibration from WWV/CHU tone detection
- **Tone Powers:** 1000 Hz (WWV) and 1200 Hz (WWVH) power levels in dB
- **NTP Status:** Wall clock time, NTP offset at minute boundary
- **Gap Provenance:** Detailed gap locations, sizes, and packet loss counts
- **Quality Indicators:** Packets received vs expected, completeness

### 2. Analytics Service (`src/hf_timestd/core/phase2_temporal_engine.py`)

Processes 20 kHz archives to derived products:

**Discrimination Methods** (each writes independent CSV):
- **Timing Tones:** 1000/1200 Hz power ratio (1/min)
- **Tick Windows:** 5ms coherent/incoherent SNR analysis (6/min)
- **440 Hz Station ID:** Unambiguous WWV/WWVH identification (2/hour)
- **BCD Correlation:** 100 Hz time code dual-peak detection (15/min)
- **Test Signals:** Minutes :08/:44 channel sounding (FSS, delay spread, noise coherence, ToA)
- **Weighted Voting:** Combines all methods for final determination

**Additional Analytics:**
- **Doppler Estimation:** Per-tick frequency shift measurement
- **Timing Metrics:** Time_snap quality, NTP drift, timing accuracy

**Output:** Separated CSVs per method + D_clock time series for system clock discipline

**Note:** Phase 3 products (decimation/10 Hz, spectrogram products, PSWS/GRAPE uploads) are handled by the separate `grape-recorder` project.

### Design Principles

- **Reprocessability:** 20 kHz archives preserved, analytics can rerun
- **Self-Contained Files:** Each NPZ contains all metadata for standalone analysis
- **Separation:** Core never stops, analytics restartable, DRF independent
- **Time_snap:** WWV/CHU tones anchor RTP to UTC (±1ms GPS-quality timing)

## Quality Metrics

- **Completeness:** 🟢 ≥99% | 🟡 95-99% | 🔴 <95%
- **Timing Drift:** RTP vs system clock (<±50ms healthy)
- **Packet Loss:** <1% healthy (indicates network issues)
- **Time_snap Quality:** TONE_LOCKED (±1ms) > NTP_SYNCED (±10ms) > WALL_CLOCK (±seconds)

## 🎯 Cross-Channel Coherent Timing

### Global Station Lock
Because radiod's RTP timestamps are **GPS-disciplined**, all 9 channels share a common timing reference. This enables treating multiple receivers as a **single coherent sensor array**.

### Cross-Frequency Global Differential Fusion
Fusion performs a cross-frequency physics solve inside `src/hf_timestd/core/multi_broadcast_fusion.py` using `GlobalDifferentialSolver`. When the solution is verified, it is injected as a trusted synthetic measurement (`GLOBAL_DIFF`) so the fusion/Kalman convergence can “snap” to the physics-based constraint.

**Outputs:** `phase2/fusion/fused_d_clock.csv` includes:
- `global_solve_verified`
- `global_solve_consistency_ms`
- `global_solve_n_obs`

**Logs:** look for:
- `Global solve context: target_minute=... mix=[...] dropped_channels=[...]`
- `Global solve: cross-agency triangulation active (NIST+NRC) ...`
- `Injecting GLOBAL_DIFF: ... force_weight=... kalman_floor_ms=...`

**Implementation:**
- **Shared Filesystem IPC:** Uses `/dev/shm` (RAM disk) to share "Anchor" detections between isolated channel processes in real-time.
- **Unambiguous Anchors:** Strong stations (e.g., CHU, WWV 20 MHz) publish their precise timing.
- **Ambiguity Resolution:** Ambiguous stations (e.g., WWV vs WWVH on 10 MHz) use the Anchor to resolve the 15ms station separation.

**Three-Phase Detection:**
1. **Anchor Discovery** - Find high-confidence locks (SNR > 15 dB) across all channels
2. **Guided Search** - Use anchor timing to narrow weak-channel search from ±500 ms to **±6.5 ms** (Dispersion + Safety Margin)
   - *Result: 99.4% noise rejection and guaranteed station lock.*
3. **Coherent Stacking** - Virtual channel with SNR improvement of 10·log₁₀(N) dB

### Primary Time Standard (HF Time Transfer)

Back-calculate emission time from GPS-locked arrival time to **verify UTC(NIST)**:

```
T_emit = T_arrival - (τ_geo + τ_iono + τ_mode)
```

| Component | Description |
|-----------|-------------|
| **T_arrival** | GPS-disciplined RTP timestamp |
| **τ_geo** | Great-circle speed-of-light delay |
| **τ_iono** | Ionospheric group delay (frequency-dependent) |
| **τ_mode** | Extra path from N ionospheric hops |

**Propagation Mode Identification** (quantized by layer heights):

| Mode | Typical Delay | Uncertainty |
|------|---------------|-------------|
| 1-hop E | ~3.8 ms | ±0.20 ms |
| 1-hop F2 | ~4.3 ms | ±0.17 ms |
| 2-hop F2 | ~5.5 ms | ±0.33 ms |

**Accuracy Progression:**
- Raw arrival time: ±10 ms
- \+ Mode identification: ±2 ms
- \+ Cross-channel consensus: ±1 ms
- \+ Cross-station verification: ±0.5 ms

### PPM-Corrected Timing

ADC clock drift compensation with **sub-sample precision**:

- **Tone-to-tone PPM measurement** - Measures actual ADC clock vs nominal
- **Exponential smoothing** - Filters PPM estimates for stability
- **Parabolic interpolation** - Sub-sample peak detection (±10-25 μs at 20 kHz)
- **Clock ratio correction** - `elapsed_seconds = (rtp_elapsed / sample_rate) × clock_ratio`

**Ensemble Anchor Selection** - Cross-channel voting selects best time_snap source based on SNR, timing preference (WWV/CHU over WWVH), and quality metrics.

## 🧠 Hybrid Station Discrimination (AI + Heuristic)

Distinguishes WWV, WWVH, and BPM on shared frequencies (2.5, 5, 10, 15 MHz) using a **Hybrid Intelligence** approach:

1.  **Probabilistic Classifier (Primary):** Logistic Regression model trained on ground truth features. Outputs $P(WWV)$ vs $P(WWVH)$.
    -   Handles mixed/balanced signals ($P \approx 0.5$) by triggering component decomposition to measure BOTH signals.
2.  **Heuristic Voting (Feature Extraction):** 12 independent signal analysis methods provide the feature vectors for the AI model:

### Voting Methods

| Vote | Method | Weight | Description |
|------|--------|--------|-------------|
| 0 | **Test Signal** | 15 | Minutes :08/:44 scientific modulation test |
| 1 | **440 Hz Station ID** | 10 | WWVH min 1, WWV min 2 |
| 2 | **BCD Amplitude Ratio** | 2-10 | 100 Hz time code dual-peak detection |
| 3 | **1000/1200 Hz Power** | 1-10 | Timing tone power ratio |
| 4 | **Tick SNR Average** | 5 | 59-tick coherent integration |
| 5 | **500/600 Hz Ground Truth** | 10-15 | 12 exclusive minutes/hour (weight=15 for M16-19, M43-51) |
| 6 | **Doppler Stability** | 2 | Lower std = cleaner path (independent of power) |
| 7 | **Timing Coherence** | 3 | Test signal + BCD ToA agreement |
| 8 | **Harmonic Ratio** | 1.5 | 500→1000 Hz, 600→1200 Hz ratios |
| 9 | **FSS Path Signature** | 2 | Frequency Selectivity Score geographic validator |
| 10 | **Noise Coherence** | flag | Transient interference detection |
| 11 | **Burst ToA** | validation | High-precision timing cross-check |
| 12 | **Spreading Factor** | flag | Channel physics L = τ_D × f_D |

### Test Signal Channel Sounding (Minutes :08/:44)

The WWV/WWVH scientific test signal is fully exploited as a **channel sounding instrument**:

| Segment | Metric | Use |
|---------|--------|-----|
| **Multi-tone** (13-23s) | FSS = 10·log₁₀((P₂ₖ+P₃ₖ)/(P₄ₖ+P₅ₖ)) | Geographic path validation |
| **White Noise** (10-12s, 37-39s) | N1 vs N2 coherence | Transient interference detection |
| **Chirps** (24-32s) | Delay spread τ_D | Multipath characterization |
| **Bursts** (34-36s) | High-precision ToA | Sub-ms timing reference |

### Inter-Method Cross-Validation (12 checks)

Beyond voting, independent measurements validate each other:
- **Power vs Timing** - BCD delay should match power ratio direction
- **Geographic Delay** - Measured delay vs predicted from receiver location
- **Doppler-Power Agreement** - Δf_D magnitude correlates with power
- **Coherence Quality** - Low coherence (<0.3) downgrades confidence
- **Harmonic Signature** - 500→1000 Hz, 600→1200 Hz ratios confirm station
- **FSS Geographic** - Path fingerprint matches scheduled station
- **Noise Transient** - N1/N2 difference flags interference events
- **Spreading Factor** - L = τ_D × f_D validates channel physics

### Why 12 Methods?
- **Redundancy:** Multiple independent measurements validate each other
- **Adaptability:** Different methods excel under different propagation conditions  
- **Temporal Coverage:** From hourly calibration to sub-second dynamics
- **Ground Truth:** 14 minutes/hour with 440/500/600 Hz exclusive broadcasts
- **Channel Sounding:** Test signal provides complete ionospheric characterization
- **Cross-Validation:** Agreement confirms accuracy, disagreement reveals mixed propagation

**Quick Check:** View `http://localhost:3000/discrimination.html` for 7-panel analysis with method labels and performance statistics.

**Learn More:** [docs/WWV_WWVH_DISCRIMINATION_USER_GUIDE.md](docs/WWV_WWVH_DISCRIMINATION_USER_GUIDE.md)

## Web UI

**🔊 Live Audio:** Click any channel to stream WWV/CHU audio directly in the browser  
**Monitoring:** Real-time health, metrics, logs (auto-refresh 30s)  
**Discrimination:** 7-panel analysis with per-method visualization  
**Stack:** Node.js/Express, vanilla JS, WebSocket audio streaming

## Documentation

**Essential:**
- [INSTALLATION.md](INSTALLATION.md) - Detailed setup guide
- [docs/PRODUCTION.md](docs/PRODUCTION.md) - Production deployment (systemd, 24/7, uploads)
- [docs/THREE_PHASE_PIPELINE.md](docs/THREE_PHASE_PIPELINE.md) - System architecture
- [DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md) - Data paths and file formats

**Timing & Metrology:**
- [docs/TIMING_METROLOGY.md](docs/TIMING_METROLOGY.md) - Technical reference for metrologists
- [docs/TIMING_METHODOLOGY.md](docs/TIMING_METHODOLOGY.md) - D_clock measurement and fusion
- [docs/DISCRIMINATION_SYSTEM.md](docs/DISCRIMINATION_SYSTEM.md) - WWV/WWVH discrimination

**Reference:**
- [docs/](docs/) - Feature documentation and guides
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) - REST API documentation
- [CANONICAL_CONTRACTS.md](CANONICAL_CONTRACTS.md) - API standards


## Requirements

**Hardware:** SDR (RX888/Airspy/SDRPlay), HF antenna (2.5-25 MHz), Linux 2+ GB RAM  
**Software:** ka9q-radio, Python 3.10+, Node.js 18+, rsync, libhdf5-dev  
**Network:** Multicast-capable LAN, internet for PSWS upload

## Configuration Example

Edit `config/timestd-config.toml` or use Web UI (recommended):

```toml
[station]
callsign = "AC0G"
grid_square = "EM38ww"

[recorder]
mode = "test"  # or "production"

[[recorder.channels]]
ssrc = 10000000
frequency_hz = 10000000
description = "WWV 10 MHz"
enabled = true

[uploader]
enabled = false  # Set true after PSWS credentials configured
```

## Workflow

**Setup:**
1. Install ka9q-radio
2. Run `./scripts/install.sh --mode test`
3. Edit `config/timestd-config.toml` with station details
4. Test with `./scripts/timestd-all.sh -start` (verify 5 min)
5. Deploy production: `sudo ./scripts/install.sh --mode production`
6. Configure PSWS SSH keys for uploads

**Daily Operation:** systemd services run 24/7, monitor web UI for 🟢 status, daily 00:30 UTC upload  
**Maintenance:** Check weekly via `journalctl -u hf-timestd`, verify uploads, review logs


## Troubleshooting

**No data:** Check `radiod` running, multicast routing, firewall (allow 5004/udp, 3000/tcp)  
**Completeness <99%:** Network congestion, multicast switch config  
**Timing drift >50ms:** Verify NTP (`timedatectl`), check radiod clock sync  
**Packet loss >1%:** Increase RTP buffer, check CPU, reduce channels

See [docs/troubleshooting.md](docs/troubleshooting.md) for details.

## Status

**Production Ready** - Core functionality complete and tested. Daily recording and PSWS upload operational at AC0G since November 2025.

### v3.13.0 (Dec 19, 2025)
- **Unified Naming Convention** - Transitioned to `Station_kHz` (e.g., `WWV_10000`) across backend, frontend, and storage.
- **Carrier Doppler Visualization** - High-precision carrier doppler (0-1 Hz) replaces legacy tone doppler in Web UI.
- **Analytics Backfill** - `Phase2AnalyticsService` now automatically recovers historical data from the raw archive on startup.
- **Tiered Storage Refactor** - Updated `TieredStorageManager` to support unified naming for both hot (RAM) and cold (disk) tiers.
- **Web UI Stability** - Fixed critical `TypeError` and `ReferenceError` bugs in the monitoring server.

### v3.12.0 (Dec 19, 2025)
- **Active Probabilistic Discrimination** - Logistic Regression Model promoted to primary arbiter for WWV/WWVH/BPM.
- **BPM Integration** - Full support for BPM (Xi'an, China) on 5/10/15 MHz, including UT1 tick filtering and geometric path weighting.
- **Robust Visualization** - Web UI now uses Theil-Sen Estimator for Doppler plots (breakdown point 29% vs 0% for Least Squares).
- **Hybrid Engine** - Heuristic methods now serve as feature extractors for the ML classification layer.

### v3.11.0 (Dec 13, 2025)
- **ka9q-python RadiodStream Integration** - Complete refactoring of RTP handling
  - Replaced custom `RTPReceiver` and `PacketResequencer` with `RadiodStream`
  - Replaced custom `ChannelManager` wrapper with direct `RadiodControl` usage
  - ~600 lines of custom code eliminated, leveraging ka9q-python's battle-tested implementation
  - Built-in gap detection, resequencing, and quality metrics via `StreamQuality`
- **Anti-Hijacking Channel Management** - Only modifies channels with our multicast destination
  - Deterministic multicast IP generation from station_id + instrument_id
  - Safe multi-client operation on same radiod instance
- **New Files:** `core_recorder_v2.py`, `stream_recorder_v2.py`

### v3.10.0 (Dec 6, 2025)
- **Timing Metrology Documentation** - Comprehensive technical reference for metrologists
  - [docs/TIMING_METROLOGY.md](docs/TIMING_METROLOGY.md) - Full uncertainty analysis
  - Discrimination rationale, fusion math, validation approach
  - GUM-compliant uncertainty budget (±0.55 ms 1σ fused)
- **Station Discrimination Fix** - CHU channels now correctly identified
  - Shared frequencies (2.5, 5, 10, 15 MHz): Use power ratio discrimination
  - Non-shared frequencies: Station from channel name (no ambiguity)
- **Spectrogram Auto-Regeneration** - Server-side updates every 10 minutes
  - POST `/api/v1/spectrograms/regenerate` for on-demand refresh
  - Carrier Analysis refresh button now triggers regeneration
- **Advanced Timing Charts** - Now use fused D_clock directly
  - Clock Stability Convergence shows fusion result (near 0 ms)
  - Consensus Distribution peaks at 0 ms when calibrated
  - Station Constellation applies calibration offsets

### v3.9.0 (Dec 5, 2025)
- **Multi-Broadcast Fusion** - Combines 13 broadcasts (WWV/WWVH/CHU) to converge on UTC(NIST)
  - Auto-calibration learns per-station offsets via EMA
  - Fused D_clock achieves ±0.5 ms accuracy (vs ±5-10 ms single-broadcast)
  - Real-time convergence indicators per station
- **Timing Dashboard Consolidation** - Simplified to fusion panel + 13-broadcast table
  - UTC(NIST) Alignment panel shows fused result
  - Per-broadcast raw D_clock with confidence, SNR, convergence status
- **Advanced Timing Visualizations** - Fusion-corrected graphs
  - Kalman funnel: 24-hour default, drag-to-zoom, scroll zoom
  - Constellation radar: All stations clustered at center when calibrated
  - Consensus KDE: Sharp peak at 0 ms after convergence
- **Timing Methodology Documentation** - `/docs/timing-methodology.html`
  - Info links (?) on all graphs explain calculations
  - D_clock measurement, fusion algorithm, accuracy expectations
- **UI Reorganization** - Removed redundant phase2-dashboard.html
  - Diurnal patterns → discrimination.html
  - Reception matrix → summary.html
  - Station distances → summary.html

### v3.8.0 (Dec 5, 2025)
- **Clock Convergence Model** - "Set, Monitor, Intervention" architecture for GPSDO timing
  - Converges to locked D_clock estimate, then monitors for anomalies
  - Welford's algorithm for running mean/variance
  - 3σ anomaly detection reveals real propagation events
- **Propagation Mode Discrimination** - Gaussian likelihood-based hop identification
  - Sharp mode probabilities after convergence (<3ms uncertainty)
- **UTC Time Standardization** - All web-UI displays use UTC consistently
- **Phase 2 Dashboard** - New visualization page for reception matrix, D_clock consensus

### v3.7.0 (Dec 4, 2025)
- **Three-Phase Pipeline** - Raw archive → Analytics → Products architecture
- **Advanced Timing Visualizations** - Kalman funnel, constellation radar, mode ridge
- **Audio Streaming** - Browser-based WWV/CHU audio via ka9q-python

### v2.2.0 (Dec 2, 2025)
- **Unified Installer** - `scripts/install.sh --mode test|production`
- **Systemd Services** - Production-ready with auto-restart, daily uploads
- **FHS-Compliant Paths** - `/var/lib/hf-timestd/`, `/var/log/hf-timestd/`, `/etc/hf-timestd/`
- **Environment File** - Single source of truth for all paths
- **PPM-Corrected Timing** - Sub-sample precision via ADC drift compensation
- **Config-Driven Sample Rate** - 20 kHz default, fully configurable

### v2.0.0 (Dec 1, 2025)
- **Package Restructure** - Clean separation into `core/`, `stream/`, `grape/`, `wspr/` packages
- **ka9q-python 3.1.0** - SSRC-free API integration
- **Stream API** - `subscribe_stream()` hides SSRC from applications
- **Generic Recording Infrastructure** - Protocol-based design for multi-app support
- **Standard Time Signal Generator** - High-fidelity synthesis of WWV/WWVH/CHU/BPM signals (ticks, BCD, AFSK, test modulations) for loopback verification.


### Previous (Nov 28-30, 2025)
- **12 Voting Methods** - BCD, timing tones, ticks, 440/500/600 Hz, Doppler, test signal
- **12 Cross-Validation Checks** - FSS geographic, noise transient, spreading factor
- **Test Signal Channel Sounding** - FSS, delay spread, ToA from :08/:44 minutes

## Credits & Support

**Credits:** Phil Karn/KA9Q (ka9q-radio), MIT Haystack (Digital RF), Nathaniel Frissell/W2NAF (HamSCI GRAPE), Rob Robinett/AI6VN (wsprdaemon inspiration), Michael James Hauan/AC0G (this implementation)

**Community:** [grape@hamsci.groups.io](mailto:grape@hamsci.groups.io) | [GitHub Issues](https://github.com/mijahauan/hf-timestd/issues)

**PSWS Access:** Contact HamSCI coordinators to register station and receive credentials

**License:** MIT - See [LICENSE](LICENSE)

---

**🎯 Ready to contribute to ionospheric science? Install and start monitoring!**
