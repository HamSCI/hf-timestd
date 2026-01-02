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
- 🌐 **Web UI** - Real-time monitoring via **FastAPI** dashboard.
- ⏰ **Chrony integration** - SHM refclock for system clock discipline.

---

## Quick Start

**Prerequisites:** ka9q-radio running, Linux with multicast networking, Python 3.11+, HDF5 libraries.

### Production Mode (24/7 Operation)

```bash
# Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# Run installer in production mode
sudo ./scripts/install.sh --mode production --user $USER

# Edit configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# Start and enable all services
sudo systemctl enable --now timestd-core-recorder
sudo systemctl enable --now timestd-analytics
sudo systemctl enable --now timestd-fusion
sudo systemctl enable --now timestd-web-ui-fastapi
```

### Service Control

| Service | Command |
|---------|---------|
| Core Recorder | `sudo systemctl status timestd-core-recorder` |
| Analytics | `sudo systemctl status timestd-analytics` |
| Fusion | `sudo systemctl status timestd-fusion` |
| Web UI | `sudo systemctl status timestd-web-ui-fastapi` |
| **All Logs** | `journalctl -u timestd-* -f` |

### Data Locations

| Data Type | Path |
|-----------|------|
| **Raw IQ** | `/var/lib/timestd/raw_archive/{CHANNEL}/` (Digital RF .h5) |
| **L2 Timing** | `/var/lib/timestd/phase2/{CHANNEL}/` (HDF5) |
| **L3 Fusion** | `/var/lib/timestd/phase2/fusion/` (HDF5) |
| **IONEX** | `/var/lib/timestd/ionex/` |

**Monitor:** Open `http://localhost:3000` for real-time channel health, quality metrics, and logs.

---

## Architecture (The Six Services)

The system is composed of six independent services that form a pipeline:

```text
[ka9q-radio] (RTP Multicast)
     ↓
1. CORE RECORDER (timestd-core-recorder)
   • Writes Digital RF HDF5 (Reliable Capture)
     ↓
2. ANALYTICS (timestd-analytics)
   • Reads Raw HDF5 -> Detects Tones -> Solves Timing
   • Writes L2 HDF5 (Timing Measurements)
     ↓
3. FUSION (timestd-fusion) <------- 4. VTEC (timestd-vtec)
   • Reads L2 HDF5 (SWMR)           • Downloads IONEX Maps
   • Applies Physics Corrections    • GNSS Observables
   • Feeds Chrony SHM
     ↓
5. SCIENCE AGGREGATOR (timestd-science-aggregator)
   • TEC Estimation, Spectrograms
     ↓
6. WEB UI (timestd-web-ui-fastapi)
   • Visualization & Monitoring
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

### Recent Updates (v3.8-3.9, January 2026)

- **v3.9.0:** Adaptive search window system - Bootstrap → Orient → Focus progression using GPSDO stability
- **v3.8.2:** Calibration sanity checks - Prevents loading corrupted state files
- **v3.8.1:** Fixed calibration semantic bug - Removed incorrect use of offsets as search priors
