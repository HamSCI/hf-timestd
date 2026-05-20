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
- **First-run installer** — `scripts/install.sh` (idempotent install: creates timestd user, venv, config, services). After first install, `scripts/deploy.sh` is the small Pattern A reload that refreshes the editable venv install and restarts services.
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
# Clone canonical repo (must be reachable by the timestd service user;
# typical layout is /opt/git/sigmond/hf-timestd, owned by timestd:timestd)
sudo git clone https://github.com/mijahauan/hf-timestd.git /opt/git/sigmond/hf-timestd
sudo chown -R timestd:timestd /opt/git/sigmond/hf-timestd
cd /opt/git/sigmond/hf-timestd

# First-run install (apt deps, user, dirs, venv, services)
sudo ./scripts/install.sh
```

The install script creates the `timestd` user, installs dependencies, runs the
configuration wizard, sets up an **editable** venv install pointing back at
this repo (Pattern A), and enables services according to the configured
profile. See **[INSTALLATION.md](INSTALLATION.md)** for details.

### Test/Development Mode

```bash
uv sync --extra dev                   # standard; creates .venv/ and locks via uv.lock
uv run python -m hf_timestd daemon --config config/timestd-config.toml

# pip fallback (if uv is unavailable):
pip install -e ".[dev]"
python -m hf_timestd daemon --config config/timestd-config.toml
```

### Updating (Pattern A — editable venv install)

```bash
cd /opt/git/sigmond/hf-timestd
git pull                              # or use deploy.sh --pull
sudo ./scripts/deploy.sh
```

`scripts/deploy.sh` is a small reload script:
1. Refuses to deploy if the working tree is dirty (use `--force-dirty` to
   override). This is the single rule that keeps production from drifting
   away from the git history.
2. Optional `git pull` (`--pull`).
3. `pip install -e .` into `/opt/hf-timestd/venv` — no-op unless
   `pyproject.toml` changed; refreshes entry-point shims.
4. Restarts the units listed in `deploy.toml [systemd]`. core-recorder is
   held back unless `--restart-recorder` is passed (causes a brief data
   gap).
5. Prints the new git SHA.

Because the venv is an editable install, source files in this repo *are*
the production code path. After deploy, `hf-timestd version --json`'s
`git.short` field matches `git rev-parse --short HEAD` here.

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
| **Live logs (all)** | `journalctl -u 'timestd-*' -f` |
| **Errors, last hour** | `journalctl -u 'timestd-*' -p warning..err --since -1h` |
| **Single service** | `journalctl -u timestd-fusion -f` |
| **Templated (per-channel)** | `journalctl -u 'timestd-metrology@*' -f` |

All `timestd-*` units log to journald — there are no per-service log files
to hunt for. See [docs/DEBUGGING.md](docs/DEBUGGING.md) for the full log
cookbook and stage-by-stage troubleshooting.

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

## Architecture

The system has an eight-service core pipeline plus a set of housekeeping units (timers, maintenance, monitoring, GRAPE subsystem). In total [systemd/](systemd/) carries ~25 unit files; [docs/SERVICES.md](docs/SERVICES.md) (when present — see also [systemd/](systemd/)) covers the full set. The eight core services:

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

- **[INSTALLATION.md](INSTALLATION.md)** — setup and deployment (standalone + Pattern A editable-venv).
- **[docs/DEBUGGING.md](docs/DEBUGGING.md)** — operator troubleshooting runbook: log access, stage-by-stage triage, failure recipes, diagnostic bundle.
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — system design and data flow.
- **[docs/METROLOGY.md](docs/METROLOGY.md)** — RTP-to-UTC calibration, timing bootstrap, error budget.
- **[docs/TECHNICAL_REFERENCE.md](docs/TECHNICAL_REFERENCE.md)** — algorithms, data formats, physics models.
- **[docs/PIPELINE_VERIFICATION.md](docs/PIPELINE_VERIFICATION.md)** — end-to-end health gates.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/mijahauan/hf-timestd)

---

## Status

**Production (7.0.0)** — canonical version source is `pyproject.toml`.
Active development and field testing in progress.

## Credits & Support

**Credits:** Phil Karn/KA9Q (ka9q-radio), MIT Haystack (Digital RF), Nathaniel Frissell/W2NAF (HamSCI GRAPE), Rob Robinett/AI6VN (wsprdaemon inspiration), Michael James Hauan/AC0G (this implementation).

**License:** MIT - See [LICENSE](LICENSE)

### Recent changes

The full version history lives in [CHANGELOG.md](CHANGELOG.md). Highlights of recent cycles:

- **v6.12** — Unified journald logging across every `timestd-*` unit (core-recorder, fusion, and physics moved off file sinks). Web UI Logs page and the `/api/living-docs/evidence/*` endpoint now read a single source and stay in sync.
- **v6.11** — Unified measurement path (RTP/Fusion fork eliminated), adaptive search windows driven by physics-model 3σ uncertainty, multipath-aware uncertainty widening, edge detection in both modes.
- **v6.10** — HDF5 SWMR throughout (h5clear on every open-for-write), 32-bit RTP wraparound fix, recorder crash-loop prevention, GRAPE pipeline robustness, QuotaManager rewrite.
- **v6.8** — Physics service crash-loop protection (`_timed_write` + watchdog pets), web-UI custom date range on all time-selector pages.
- **v6.7** — Real-time ionospheric propagation model (WAM-IPE + GIRO), multi-mode prediction, self-consistency check.
- **v6.5** — Dual Kalman architecture (independent L1/L2 filters feeding Chrony), SHM permission cleanup.
- **v6.3** — RTP-to-UTC bootstrap state machine (ACQUIRING → CORRELATING → TRACKING → LOCKED) with geographic priors.
- **v6.2** — Cramér-Rao uncertainty, multipath detection, Doppler correction, CFAR-like adaptive SNR.
- **v5.0** — HDF5-native pipeline; replaced Digital RF with `.bin.zst` binary IQ archive.
