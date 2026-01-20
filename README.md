# HF Time Standard Analysis

**Precision WWV/CHU time-standard analysis for UTC alignment** - Captures high-precision IQ data from ka9q-radio, performs multi-method WWV/WWVH discrimination, and produces D_clock measurements for system clock discipline.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

HF Time Standard Analysis (`hf_timestd`) receives WWV/WWVH/CHU/BPM time standard broadcasts via ka9q-radio and produces precise timing measurements (D_clock) for UTC alignment and system clock discipline via Chrony.

**Key Capabilities (V5.0):**

- 📡 **Multi-channel recording** - Simultaneous WWV, WWVH, CHU, BPM (9 tuned frequencies, 17 logical broadcasts) in **Digital RF (HDF5)** format.
- 🎯 **Sub-millisecond timing** - ±0.5 ms via multi-broadcast fusion to UTC(NIST).
- 🔗 **HDF5-Native Pipeline** - High-performance SWMR (Single Writer Multiple Reader) data exchange.
- 🌍 **Physics-Informed Propagation** - Uses GNSS-derived **IONEX VTEC** maps for precise path delay correction.
- ⏱️ **HF time transfer** - D_clock measurement with ionospheric propagation mode estimation.
- 🧠 **AI Discrimination** - Probabilistic Logistic Regression + Heuristic Voting for station ID.
- 🌐 **Web UI** - Real-time monitoring via **FastAPI** dashboard with Allan Deviation, propagation analysis, and per-path TEC visualization.
- ⏰ **Chrony integration** - SHM refclock for system clock discipline.

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
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - System design philosophy ("The Why").
- **[DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md)** - File layout specification.
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/mijahauan/hf-timestd)

---

## Status

**Production Ready (V5.0)** - HDF5-native pipeline operation.

### V5.0 Capabilities (December 2025)

- **Digital RF Storage:** Replaced custom binary format with standard Digital RF.
- **HDF5-Native Analytics:** All intermediate data (L1/L2/L3) uses HDF5 for performance and metadata richness.
- **Physics-Informed Propagation:** Integration of **IONEX** maps allows for precise path delay estimation beyond simple geometric or IRI models.
- **Global Differential Fusion:** Solves for ionospheric consistency across all 9 channels simultaneously.

## Credits & Support

**Credits:** Phil Karn/KA9Q (ka9q-radio), MIT Haystack (Digital RF), Nathaniel Frissell/W2NAF (HamSCI GRAPE), Rob Robinett/AI6VN (wsprdaemon inspiration), Michael James Hauan/AC0G (this implementation).

**License:** MIT - See [LICENSE](LICENSE)

### Recent Updates

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
