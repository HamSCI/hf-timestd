# HF Time Standard Analysis

**Precision WWV/CHU time-standard analysis for UTC alignment** - Captures high-precision IQ data from ka9q-radio, performs multi-method WWV/WWVH discrimination, and produces D_clock measurements for system clock discipline.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

HF Time Standard Analysis (`hf_timestd`) receives WWV/WWVH/CHU/BPM time standard broadcasts via ka9q-radio and produces precise timing measurements (D_clock) for UTC alignment and system clock discipline via Chrony.

**Key Capabilities:**

- 📡 **Multi-channel recording** - Simultaneous WWV, WWVH, CHU, BPM (9 tuned frequencies, 17 logical broadcasts) in **binary IQ archive** format with JSON metadata sidecars.
- 🎯 **Sub-millisecond timing** - ±0.5 ms via multi-broadcast fusion to UTC(NIST), with theoretical floor of ±0.036 ms (Cramér-Rao bound).
- 🌐 **Real-time ionospheric model (v6.7)** - WAM-IPE + GIRO data for frequency-dependent, time-varying group delay predictions with multi-hop support (1F, 2F, 3F).
- 🔗 **HDF5 SWMR Pipeline** - Single Writer Multiple Reader protocol with `h5clear` crash recovery. Writers keep files open and flush after each append; readers use `swmr=True`. Zero write/read contention.
- 🌍 **Real-time GNSS VTEC Correction** - Local dual-frequency GPS provides direct ionospheric correction.
- 🔬 **Hierarchical Estimation** - Per-broadcast Kalman filtering + WLS fusion for deterministic restart behavior.
- ⏱️ **NTP-Based Bootstrap (v6.4)** - Fast RTP-to-UTC calibration using GPSDO wallclock (~2 min to LOCKED).
- 🧠 **AI Discrimination** - Probabilistic Logistic Regression + Heuristic Voting for station ID.
- 🌐 **Web UI** - Real-time monitoring via **FastAPI** dashboard with Allan Deviation, propagation analysis, and per-path dTEC visualization.
- ⏰ **Dual Chrony feeds** - Independent L1 (geometric) and L2 (physics-corrected) SHM refclocks with separate Kalman filters.
- 📊 **Metrological Rigor (v6.2)** - Cramér-Rao uncertainty, multipath detection, Doppler correction, adaptive thresholds.

### Complete Feature Inventory

**Signal Reception & Recording:**
- **ka9q-python integration** — Python interface to Phil Karn's radiod for channel creation, RTP reception, resequencing, gap detection
- **Multi-channel IQ recording** — 9 channels × 24 kHz IQ, binary `.bin.zst` + JSON metadata sidecars
- **RTP timestamp preservation** — GPS+PPS authoritative timestamps (~50 μs) via radiod's `GPS_TIME`/`RTP_TIMESNAP`

**Time Signal Detection:**
- **Tick edge detection** — Quadrature matched filter, sub-sample parabolic interpolation, SNR-weighted robust median ensemble (up to 57 ticks/min)
- **Carrier-phase Doppler** — Phase slope across ticks → Hz per minute
- **Multipath detection** — Peak broadening, secondary peaks, phase stability analysis

**Station Identification:**
- **WWV/WWVH discrimination** — Weighted voting (BCD correlation, 1000/1200 Hz tone ratio, station ID tones, test signal detection)
- **BPM discrimination** — Tick duration (10 ms UTC vs 100 ms UT1), minute gating
- **Probabilistic discriminator** — Logistic regression model for station ID confidence scoring

**Time Code Decoding:**
- **CHU FSK** — Bell 103 demodulation (300 baud), Frame A (UTC) + Frame B (DUT1, TAI-UTC, year), multi-second consensus, cross-validation against RTP
- **WWV/WWVH BCD** — 100 Hz subcarrier extraction for station identification and time confirmation
- **Leap second awareness** — TAI-UTC monitoring via CHU FSK, Kalman hold during transitions

**Ionospheric Science:**
- **Carrier-phase dTEC** — Primary product (~6 mTECU/min sensitivity, ~250K records/day), GNSS VTEC-anchored
- **Group-delay TEC** — 1/f² dispersion fit (validation product)
- **Local GNSS VTEC** — ZED-F9P dual-frequency GPS, ~1 Hz, DCB-corrected, ±1 TECU
- **Propagation mode identification** — GW, 1E, 1F2, 2F2, 3F2 from delay matching
- **TID detection** — Cross-path timing residual correlation
- **Scintillation indices** — S4 (amplitude) and σ_φ (phase) per ITU-R P.531
- **Sporadic-E detection** — SNR anomaly + mode change + foEs estimation

**Propagation Modeling:**
- **HFPropagationModel** — Multi-mode delay prediction with Ne(h) integration
- **IonoDataService** — WAM-IPE (NOAA S3) + GIRO ionosonde real-time data
- **IRI-2020 / IONEX / parametric** — Tiered fallback chain for ionospheric parameters
- **PHaRLAP ray tracing** — 2D numerical ray tracing via pyLAP with spatially varying IRI-2020 Ne(h) grid (auto-scaled, 1 sample per 500 km)

**WWV/WWVH Test Signal Analysis:**
- **Minutes :08/:44** — Multi-tone power (2, 3, 4, 5 kHz), Frequency Selectivity Score, chirp delay spread, transient detection

**HamSCI GRAPE:**
- **10 Hz IQ decimation** — All 9 channels decimated from 24 kHz for GRAPE compatibility
- **Digital RF packaging** — PSWS/wsprdaemon-compatible DRF format
- **Automated daily upload** — SFTP to HamSCI PSWS network
- **Spectrograms** — Daily spectrogram generation from decimated data

**Web UI & API:**
- **FastAPI dashboard** — Metrology, dTEC, ionogram, GRAPE, logs, propagation conditions pages
- **Custom date range** — Browse any historical day on all time-selector pages
- **Solar elevation overlay** — Ionogram and dTEC time series

**Infrastructure:**
- **systemd services** — 8+ services with dependency management, watchdog, CPU affinity
- **Service profiles** — Four operational profiles (archive, rtp, fusion, full) with per-service overrides via `hf-timestd profile` CLI
- **ResourceGuardian** — Auto-sizing disk management (80% cap, day-level eviction, preflight + watchdog)
- **Unified installer** — `scripts/deploy.sh` (idempotent install + update, creates timestd user, venv, config, services)
- **Log rotation** — Daily rotation with 14-day retention
- **Freshness monitoring** — Cron-based alerts on stale data

---

## Why HF Time Standards?

The system serves a dual purpose:

1. **RTP Mode (with GPSDO):** GPS+PPS provides authoritative timing. The metrology pipeline tests and refines detection algorithms, calibration models, and ionospheric corrections against a known-good reference.

2. **FUSION Mode (without GPSDO):** When GPS, GPSDO, or network access is unavailable, the system derives UTC solely from HF time standard receptions.

FUSION mode addresses real operational scenarios: remote/off-grid installations, disaster situations where GPS and network infrastructure are disrupted, intentional GPS denial, backup timing when GNSS fails, and scientific stations where only HF propagation is available.

**Expected FUSION mode accuracy** (multi-station, typical SDR with TCXO):

| Configuration | Accuracy | Time to Lock |
|--------------|----------|-------------|
| Multi-station + NTP available | ±2-5 ms | 2-3 min |
| Multi-station, no network | ±2-5 ms | 5-10 min |
| Single station | ±5-15 ms | 2-3 min |

The ionosphere is the dominant error in all cases. Oscillator quality affects time-to-lock and holdover, but not steady-state accuracy once locked. See **[METROLOGY.md](docs/METROLOGY.md)** for the full error budget and analysis.

---

## Quick Start

**Prerequisites:** ka9q-radio running, Linux with multicast networking, Python 3.11+, HDF5 libraries.

### Production Mode (24/7 Operation)

```bash
# Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# Install (idempotent — safe to re-run for updates)
sudo ./scripts/deploy.sh
```

The deploy script creates the `timestd` user, installs dependencies, runs the
configuration wizard, and enables services according to the configured profile.
See **[INSTALLATION.md](INSTALLATION.md)** for details.

### Test/Development Mode

```bash
pip install -e ".[dev]"
python -m hf_timestd daemon --config config/timestd-config.toml
```

### Updating

```bash
cd /path/to/hf-timestd
sudo ./scripts/deploy.sh --pull
```

Pulls latest code, syncs to `/opt/hf-timestd`, updates the venv, and restarts
affected services (core-recorder is not restarted unless `--restart-all` is
passed, to avoid a data gap).

### Service Profiles

Which services run is controlled by a **profile** in the config file.
Core-recorder is always on — it is the irreplaceable raw data source.

| Profile | Services | Use case |
|---------|----------|----------|
| `archive` | core-recorder, prune | Raw IQ preservation, minimal resources |
| `rtp` | archive + web-api, monitoring, GRAPE | Standard GPSDO timing mode |
| `fusion` | rtp + metrology, fusion, chrony-monitor | GPS-denied timing from HF |
| `full` | fusion + physics, ionex, iono-reanalysis | Full science + timing |

Per-service overrides layer on top (e.g., `metrology = true` in `[services]`
enables metrology for study even in `rtp` profile).

```bash
# View profiles and current state
hf-timestd profile list
hf-timestd profile show
hf-timestd service status

# Switch profile (updates config + systemd)
sudo hf-timestd profile set fusion

# Toggle individual services
sudo hf-timestd service enable metrology
sudo hf-timestd service disable physics
```

### Service Control

| Action | Command |
|--------|---------|
| Start all | `sudo ./scripts/start-services.sh` |
| Stop all | `sudo ./scripts/stop-services.sh` |
| Status overview | `hf-timestd service status` |
| **All Logs** | `journalctl -u timestd-* -f` |

### Data Locations

| Data Type | Path |
|-----------|------|
| **Raw IQ** | `/var/lib/timestd/raw_buffer/{CHANNEL}/` (Binary + JSON) |
| **L2 Timing** | `/var/lib/timestd/phase2/{CHANNEL}/` (HDF5) |
| **L3 Fusion** | `/var/lib/timestd/phase2/fusion/` (HDF5) |
| **IONEX** | `/var/lib/timestd/ionex/` |

**Monitor:** Open `http://localhost:8000` for real-time monitoring (FastAPI Web API):

- **Station Overview** - System metadata and recent activity
- **System Health** - Process status and true uptime
- **System Logs** - Real-time service logs viewer (`/static/logs.html`)
- **API Docs** - Interactive API documentation (`/api/docs`)
- **Metrology Dashboard** - Fusion timing, ISO GUM uncertainty, Allan Deviation analysis
- **Propagation Analysis** - Per-broadcast modes, multi-frequency comparison, per-path dTEC with error bars

---

## Architecture (The Eight Services)

The system is composed of eight independent services that form a pipeline:

```text
[ka9q-radio] (RTP Multicast)
     ↓
1. CORE RECORDER (timestd-core-recorder)
   • Writes Binary IQ (.bin.zst) + JSON sidecars (Reliable Capture)
     ↓
2. METROLOGY (timestd-metrology)
   • Reads Raw IQ -> Detects Tones -> L1/L2 Measurements (HDF5)
     ↓
3. L2 CALIBRATION (timestd-l2-calibration)
   • Applies geometric + ionospheric corrections -> L2 Timing
     ↓
4. FUSION (timestd-fusion) <------- 5. VTEC (timestd-vtec)
   • Reads L2 HDF5 (crash-safe)     • GNSS VTEC monitoring
   • Dual Kalman filtering          • (optional, requires GNSS)
   • Feeds Chrony SHM (TSL1/TSL2)
     ↓
6. PHYSICS (timestd-physics)
   • Carrier-phase dTEC + group-delay TEC validation
     ↓
7. WEB API (timestd-web-api)
   • FastAPI dashboard & REST API (port 8000)
     ↓
8. RADIOD MONITOR (timestd-radiod-monitor)
   • Hardware health monitoring
```

### Key Technologies

- **Binary IQ Archive:** Compressed `.bin.zst` files with JSON metadata sidecars for raw 24 kHz IQ recording. Digital RF (MIT Haystack) is used for GRAPE packaging/upload only.
- **HDF5 SWMR Pipeline:** Single Writer Multiple Reader protocol for all inter-service data exchange. Writer keeps the daily file open (`swmr_mode=True`) and flushes after each append; readers open with `swmr=True`. `h5clear -s` is run unconditionally on every open of an existing file, providing automatic crash recovery without manual intervention.
- **Ionospheric Correction:** GNSS VTEC (primary) and IONEX maps (fallback) correct for group delay ($\tau_{iono} \propto TEC/f^2$). Carrier-phase dTEC is the primary ionospheric science product.

---

## Detailed Documentation

- **[INSTALLATION.md](INSTALLATION.md)** - Detailed setup guide.
- **[TECHNICAL_REFERENCE.md](docs/TECHNICAL_REFERENCE.md)** - Deep dive into algorithms, data formats, and physics models.
- **[METROLOGY.md](docs/METROLOGY.md)** - RTP-to-UTC calibration methodology and timing bootstrap.
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** - System design philosophy ("The Why").
- **[docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md](docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md)** - Production deployment and verification gates.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/mijahauan/hf-timestd)

---

## Status

**Production (V6.11.0)** - Active development and field testing in progress.

## Credits & Support

**Credits:** Phil Karn/KA9Q (ka9q-radio), MIT Haystack (Digital RF), Nathaniel Frissell/W2NAF (HamSCI GRAPE), Rob Robinett/AI6VN (wsprdaemon inspiration), Michael James Hauan/AC0G (this implementation).

**License:** MIT - See [LICENSE](LICENSE)

### Recent Updates

**v6.11.0 (March 9, 2026) - Unified Measurement Path + Adaptive Windowing + Multipath-Aware Uncertainty**

- ✅ **Unified detection path** — Eliminated RTP/Fusion fork in `process_minute()`. Both modes now use the same per-second correlator with `BufferTiming` when available; legacy `tone_detector.process_samples()` retained as Fusion fallback only when `BufferTiming` is missing
- ✅ **Adaptive search windows** — Physics-model-derived 3σ uncertainty passed to `_measure_tone_at_known_time()`. Fusion mode adds UTC estimate uncertainty in quadrature: `σ = √(σ_physics² + σ_utc²)`
- ✅ **Window safeguards** in `BroadcastWindowState` — staleness decay (exponential widening after 5 min), consecutive miss counter (resets after 5 misses), model floor rule (tracked variance can only narrow below model at confidence ≥ 0.95 with ≥ 30 observations)
- ✅ **Continuous physics confidence** — Binary physics gate replaced with Gaussian confidence weighting. `FusionTimingState` feeds at confidence > 0.1 threshold, allowing marginal detections to contribute with appropriately low weight
- ✅ **Multipath-aware uncertainty widening** — CLEAN deconvolution delay spread and per-second timing spread inflate `BroadcastWindowState` tracked variance (quadrature). Multipath-affected detections get degraded Kalman confidence
- ✅ **Edge detection in both modes** — Tick edge ensemble now runs in Fusion mode (was RTP-only), enabling edge ensemble recovery and cross-check in all operating modes
- 📄 **Design doc** — `docs/design/UNIFIED_MEASUREMENT_PATH.md` documents the 5-step implementation with adaptive window methodology, failure modes, and safeguards

**v6.10.0 (March 7, 2026) - HDF5 SWMR + Web UI + Robustness**

- ✅ **HDF5 SWMR throughout** — Replaced `open-write-close` + `locking=False` with proper SWMR across all 13 reader/writer sites. Writer keeps daily file open in SWMR mode, flushes after each append. `h5clear -s` called unconditionally on every open-for-write — this is the key fix that makes SWMR robust to unclean shutdowns (previous SWMR attempt abandoned Feb 6 due to this)
- ✅ **32-bit RTP counter wraparound** fix (49.7 hr period at 24 kHz)
- ✅ **Recorder crash-loop prevention** on restart (uptime-gated freshness self-restart)
- ✅ **GRAPE pipeline** robustness: tiered storage search, 2-day retention, non-fatal SFTP upload
- ✅ **GNSS TEC**: correct DCB sign, C1C-C2L synthesis, receiver DCB estimation, plausibility gate
- ✅ **QuotaManager** rewritten: day-level circular buffer deletion with priority ordering
- ✅ **Phase page**: dynamic channel/station filters, solar zenith overlay, gap line breaks
- ✅ **Logs page**: all services grouped with error guidance panel
- ✅ **Dead code archived** from `wwvh_discrimination.py` (3918 → 1237 lines)

**v6.8.0 (February 26, 2026) - Web UI Polish & Physics Service Resilience**

- ✅ **Custom date range** on all time-selector pages (ionogram, dTEC, metrology, logs) — browse any historical day
- ✅ **Solar elevation overlay** on ionogram ToF/SNR time series — curve-only, no shading, consistent across all pages
- ✅ **Brighter color scheme** for dark backgrounds — station colors and secondary markers improved on ionogram and dTEC pages
- ✅ **Removed integrated dTEC panel** from dTEC page (rarely detectable, compressed useful plots)
- ✅ **Physics service crash-loop protection** — `_timed_write()` wraps all HDF5 writes with 30s timeout; `_pet_watchdog()` called 17× throughout `process_minute()` to prevent watchdog kills during heavy I/O
- ✅ **Logs API `until` parameter** — enables custom date range queries for journal logs

**v6.7.1 (February 12, 2026) - Propagation Model Full Integration**

- ✅ **Full pipeline migration** - `multi_broadcast_fusion.py` and `bootstrap_validator.py` migrated from `PhysicsPropagationModel` to `HFPropagationModel`
- ✅ **Great-circle TEC sampling** - `IonoDataService._gc_intermediate()` uses spherical trigonometry for accurate path TEC
- ✅ **Altitude-dependent obliquity** - Thin-shell mapping `M(h)` replaces `1/sin(e)` for low-elevation accuracy
- ✅ **Web API endpoints** - `/model/predict`, `/model/all-stations`, `/model/iono-status` for live model observability
- ✅ **Self-consistency check wired** - `HFPropagationModel.self_consistency_check()` integrated into `ArrivalPatternMatrix`
- ✅ **Deprecated** - `physics_propagation.py` retained for backward compatibility; all callers migrated

**v6.7.0 (February 12, 2026) - Real-Time Ionospheric Propagation Model**

- ✅ **Real-time ionospheric data** - `IonoDataService` fetches WAM-IPE grids from NOAA S3 and GIRO ionosonde data for real-time hmF2/foF2 corrections
- ✅ **Physics-based group delay** - `HFPropagationModel` computes frequency-dependent ionospheric delay via numerical Ne(h) integration
- ✅ **Multi-mode predictions** - Evaluates 1F, 2F, 3F, 1E propagation modes with MUF checks and geometric feasibility
- ✅ **Adaptive uncertainty** - Windows adapt from ±1.5 ms (WAM-IPE+GIRO) to ±15 ms (no model), blended with tracked variance
- ✅ **Self-consistency check** - Multi-frequency differential delay validates model TEC predictions
- ✅ **23 new tests** - All passing, 0 regressions in existing test suite

**v6.5.1 (February 7, 2026) - Dual Kalman & Chrony Feed Fixes**

- ✅ **Dual Kalman architecture** - Independent L1 and L2 Kalman filters so TSL1 and TSL2 carry genuinely different estimates to chrony
- ✅ **Chrony SHM reachability fix** - Discontinuity filter no longer permanently latches; threshold scales with measurement uncertainty
- ✅ **HDF5 file lock fix** - Moved `HDF5_USE_FILE_LOCKING=FALSE` before h5py import; added `locking=False` to all h5py.File() calls
- ✅ **Silent exception fix** - Upgraded HDF5 read error logging from DEBUG to WARNING to prevent invisible data starvation
- ✅ **FUSION mode documentation** - Comprehensive accuracy analysis and error budget in docs/METROLOGY.md

**v6.3.0 (January 25, 2026) - Timing Bootstrap System**

- ✅ **RTP-to-UTC Bootstrap:** Two-phase calibration using metadata + broadcast validation
- ✅ **Discriminating Features:** Tone frequency, schedule, geographic ordering validation
- ✅ **State Machine:** ACQUIRING → CORRELATING → TRACKING → LOCKED progression
- ✅ **Geographic Priors:** Expected propagation delays based on transmitter/receiver locations
- ✅ **Unambiguous Channels:** High-confidence station ID on CHU and WWV 20/25 MHz

**v6.2.0 (January 24, 2026) - Metrological Enhancements**

- ✅ **Cramér-Rao Uncertainty:** Rigorous ToA uncertainty from SNR, bandwidth, duration
- ✅ **Multipath Detection:** Integrated into tone detector with uncertainty inflation
- ✅ **Doppler Correction:** Removes systematic timing bias from ionospheric motion
- ✅ **Adaptive SNR Threshold:** CFAR-like approach improves sensitivity 10-20%
- ✅ **CHU Tick Timing:** High-precision timing from 1000 Hz tick (~0.05 ms)
- ✅ **Complex Correlation:** Phase-preserving correlation for sub-sample refinement

**v5.3.2 (January 20, 2026) - Fusion Restart & Install Improvements**

- ✅ **Kalman State Persistence:** Fixed state restore on service restart (no more D_clock jumps)
- ✅ **SHM Permissions:** Automatic cleanup of stale Chrony SHM segments
- ✅ **Install Process:** Initial IONEX download, proper service ordering
- ✅ **Deploy Script:** `scripts/deploy.sh` — unified idempotent installer and updater

**v5.3.1 (January 12, 2026) - "Steel Ruler" & Drift Elimination**

- ✅ **"Steel Ruler" Metrology:** Strict GPSDO-anchored Kalman tuning (Q ≈ 0)
- ✅ **Drift Elimination:** Hard-clamped drift to 0.0 after convergence

**v5.0.0 (January 7, 2026) - HDF5-Native Pipeline**

- ✅ **Binary IQ Archive:** Compressed `.bin.zst` + JSON sidecars for raw IQ (replaced Digital RF)
- ✅ **HDF5-Native Analytics:** All L1/L2/L3 data uses HDF5 (crash-safe open-write-close)
- ✅ **Physics-Informed Propagation:** IONEX VTEC maps for precise path delay
