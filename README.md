# HF Time Standard Analysis

**Precision WWV/CHU time-standard analysis for UTC alignment** - Captures high-precision IQ data from ka9q-radio, performs multi-method WWV/WWVH discrimination, and produces D_clock measurements for system clock discipline.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

HF Time Standard Analysis (`hf_timestd`) receives WWV/WWVH/CHU/BPM time standard broadcasts via ka9q-radio and produces precise timing measurements (D_clock) for UTC alignment and system clock discipline via Chrony.

**Key Capabilities (V6.7.1):**

- 📡 **Multi-channel recording** - Simultaneous WWV, WWVH, CHU, BPM (9 tuned frequencies, 17 logical broadcasts) in **Digital RF (HDF5)** format.
- 🎯 **Sub-millisecond timing** - ±0.5 ms via multi-broadcast fusion to UTC(NIST), with theoretical floor of ±0.036 ms (Cramér-Rao bound).
- 🌐 **Real-time ionospheric model (v6.7)** - WAM-IPE + GIRO data for frequency-dependent, time-varying group delay predictions with multi-hop support (1F, 2F, 3F).
- 🔗 **HDF5-Native Pipeline** - High-performance crash-safe data exchange.
- 🌍 **Real-time GNSS VTEC Correction** - Local dual-frequency GPS provides direct ionospheric correction.
- 🔬 **Hierarchical Estimation** - Per-broadcast Kalman filtering + WLS fusion for deterministic restart behavior.
- ⏱️ **NTP-Based Bootstrap (v6.4)** - Fast RTP-to-UTC calibration using GPSDO wallclock (~2 min to LOCKED).
- 🧠 **AI Discrimination** - Probabilistic Logistic Regression + Heuristic Voting for station ID.
- 🌐 **Web UI** - Real-time monitoring via **FastAPI** dashboard with Allan Deviation, propagation analysis, and per-path TEC visualization.
- ⏰ **Dual Chrony feeds** - Independent L1 (geometric) and L2 (physics-corrected) SHM refclocks with separate Kalman filters.
- 📊 **Metrological Rigor (v6.2)** - Cramér-Rao uncertainty, multipath detection, Doppler correction, adaptive thresholds.

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

The ionosphere is the dominant error in all cases. Oscillator quality affects time-to-lock and holdover, but not steady-state accuracy once locked. See **[METROLOGY.md](METROLOGY.md)** for the full error budget and analysis.

---

## Quick Start

**Prerequisites:** ka9q-radio running, Linux with multicast networking, Python 3.11+, HDF5 libraries.

### Production Mode (24/7 Operation)

```bash
# Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# Run installer in production mode (creates 'timestd' system user automatically)
sudo ./scripts/install.sh --mode production

# Edit configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# Start and enable all services
sudo systemctl enable --now timestd-core-recorder
sudo systemctl enable --now timestd-metrology
sudo systemctl enable --now timestd-fusion
sudo systemctl enable --now timestd-web-api
```

### Production Updates (Repo -> Live System)

- Use **`sudo scripts/update-production.sh [--pull]`** for production updates.
- This script syncs package code, scripts, web-api assets, systemd units, cron freshness monitor, and logrotate config.
- Follow **[docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md](docs/DEPLOYMENT_CORRESPONDENCE_CHECKLIST.md)** after each update to verify correspondence and freshness gates.

### Service Control

| Service | Command |
|---------|---------|
| Core Recorder | `sudo systemctl status timestd-core-recorder` |
| Metrology | `sudo systemctl status timestd-metrology` |
| Fusion | `sudo systemctl status timestd-fusion` |
| Web API | `sudo systemctl status timestd-web-api` |
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
- **Propagation Analysis** - Per-broadcast modes, multi-frequency comparison, per-path TEC with error bars

---

## Architecture (The Eight Services)

The system is composed of eight independent services that form a pipeline:

```text
[ka9q-radio] (RTP Multicast)
     ↓
1. CORE RECORDER (timestd-core-recorder)
   • Writes Binary IQ + JSON sidecars (Reliable Capture)
     ↓
2. METROLOGY (timestd-metrology)
   • Reads Raw IQ -> Detects Tones -> L1 Measurements
     ↓
3. L2 CALIBRATION (timestd-l2-calibration)
   • Applies geometric + TEC corrections -> L2 Timing
     ↓
4. FUSION (timestd-fusion) <------- 5. VTEC (timestd-vtec)
   • Reads L2 HDF5 (SWMR)           • Downloads IONEX Maps
   • Kalman filtering               • GNSS Observables
   • Feeds Chrony SHM (TSL1/TSL2)
     ↓
6. PHYSICS (timestd-physics)
   • TEC Estimation from multi-frequency
     ↓
7. WEB API (timestd-web-api)
   • FastAPI dashboard & REST API
     ↓
8. RADIOD MONITOR (timestd-radiod-monitor)
   • Hardware health monitoring
```

### Key Technologies

- **Digital RF:** Efficient HDF5-based format for continuous IQ recording (MIT Haystack).
- **HDF5 SWMR:** Allows Fusion to read analytics results milliseconds after they are written, enabling real-time clock discipline.
- **IONEX VTEC:** Incorporates global ionospheric maps (NASA/IGS) to correct for group delay ($\tau_{iono} \propto TEC/f^2$).

---

## Detailed Documentation

- **[INSTALLATION.md](INSTALLATION.md)** - Detailed setup guide.
- **[TECHNICAL_REFERENCE.md](TECHNICAL_REFERENCE.md)** - Deep dive into algorithms, data formats, and physics models.
- **[METROLOGY.md](METROLOGY.md)** - RTP-to-UTC calibration methodology and timing bootstrap.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System design philosophy ("The Why").
- **[DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md)** - File layout specification.
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/mijahauan/hf-timestd)

---

## Status

**Testing (V6.7.1)** - Active development and field testing in progress.

## Credits & Support

**Credits:** Phil Karn/KA9Q (ka9q-radio), MIT Haystack (Digital RF), Nathaniel Frissell/W2NAF (HamSCI GRAPE), Rob Robinett/AI6VN (wsprdaemon inspiration), Michael James Hauan/AC0G (this implementation).

**License:** MIT - See [LICENSE](LICENSE)

### Recent Updates

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
- ✅ **FUSION mode documentation** - Comprehensive accuracy analysis and error budget in METROLOGY.md

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
- ✅ **Update Script:** `scripts/update-production.sh` for easy updates after `git pull`

**v5.3.1 (January 12, 2026) - "Steel Ruler" & Drift Elimination**

- ✅ **"Steel Ruler" Metrology:** Strict GPSDO-anchored Kalman tuning (Q ≈ 0)
- ✅ **Drift Elimination:** Hard-clamped drift to 0.0 after convergence

**v5.0.0 (January 7, 2026) - HDF5-Native Pipeline**

- ✅ **Digital RF Storage:** Standard HDF5 format for raw IQ
- ✅ **HDF5-Native Analytics:** All L1/L2/L3 data uses HDF5 with SWMR
- ✅ **Physics-Informed Propagation:** IONEX VTEC maps for precise path delay
