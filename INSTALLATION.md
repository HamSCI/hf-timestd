# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 31, 2025

This guide covers installing and configuring `hf-timestd` for recording and analyzing HF time standard broadcasts (BPM, CHU, WWV, WWVH).

---

## Prerequisites

### Hardware

- GPSDO-governed SDR supported by ka9q-radio (e.g. RX888 MkII, Airspy HF+, SDRplay)
- GNSS receiver for VTEC (Optional but recommended, e.g., ZED-F9P)
- HF antenna covering 2.5-25 MHz
- Linux host with multicast-capable LAN

### Software

- Linux (Debian/Ubuntu class)
- Python 3.11+
- **ka9q-radio** installed and running (`radiod`)
- **HDF5 Libraries** (`libhdf5-dev`)

---

## Install (Production Mode)

This is the recommended installation for 24/7 operation.

```bash
# 1. Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# 2. Run installer (installs all dependencies, creates 'timestd' system user)
sudo ./scripts/install.sh --mode production

# 3. Edit global configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# 4. Enable and start core services
sudo systemctl enable --now timestd-core-recorder
sudo systemctl enable --now timestd-analytics
sudo systemctl enable --now timestd-physics
sudo systemctl enable --now timestd-fusion
sudo systemctl enable --now timestd-web-api

# 5. Enable optional services
sudo systemctl enable --now timestd-ionex-download.timer  # Daily IONEX downloads
sudo systemctl enable --now timestd-chrony-monitor.timer  # Chrony health monitoring
sudo systemctl enable --now timestd-radiod-monitor    # Radiod health monitoring
```

### Production Paths

- **Data:** `/var/lib/timestd/`
- **Logs:** `/var/log/hf-timestd/`
- **Config:** `/etc/hf-timestd/`

---

## Install (Test/Development Mode)

For temporary capability testing or development.

```bash
# 1. Run installer in test mode
./scripts/install.sh --mode test

# 2. Edit local configuration
nano config/timestd-config.toml

# 3. Start services (interactive scripts)
./scripts/timestd-all.sh -start

# 4. Check status
./scripts/timestd-all.sh -status
```

### Test Paths

- **Data:** `/tmp/timestd-test/`
- **Logs:** `/tmp/timestd-test/logs/`
- **Config:** `config/timestd-config.toml`

---

## Configuration Overview

The configuration file controls which stations are recorded.

### `[station]`

Your geographic details (essential for physics propagation model).

```toml
[station]
callsign = "AC0G"
lat = 38.9
lon = -94.6
```

### `[recorder.channels]`

Define each frequency you want to monitor.

```toml
[[recorder.channels]]
description = "WWV 10 MHz"
frequency_hz = 10000000
enabled = true
```

### `[vtec]` (Optional)

If you have a local GNSS receiver for local VTEC corrections.

```toml
[vtec]
enabled = true
gnss_device = "/dev/ttyACM0"
```

---

## Service Overview

### Core Services (Required)

- **`timestd-core-recorder`** - Records RTP audio streams from radiod, writes Digital RF archives
- **`timestd-analytics`** - Phase 2 timing analysis: tone detection, BCD decoding, timing solution
- **`timestd-physics`** - Propagation modeling using IONEX/IRI-2020, TEC estimation
- **`timestd-fusion`** - Multi-broadcast Kalman fusion, feeds Chrony SHM for system clock discipline
- **`timestd-web-api`** - FastAPI web server on port 8000 (metrology dashboard, logs, API)

### Optional Services

- **`timestd-ionex-download.timer`** - Daily download of global IONEX maps from NASA CDDIS
- **`timestd-chrony-monitor.timer`** - Monitors Chrony reachability and alerts on issues
- **`timestd-radiod-monitor`** - Monitors radiod health and restarts channels if needed
- **`grape-daily.timer`** - Daily GRAPE processing: decimation, spectrograms, packaging

**Note:** VTEC monitoring is handled by the `timestd-physics` service using scripts like `live_vtec.py` and `zedf9p_tec_client.py`, not a separate service.

---

## Verifying Operation

### Check Core Services

```bash
# All should show "active (running)"
sudo systemctl status timestd-core-recorder
sudo systemctl status timestd-analytics
sudo systemctl status timestd-physics
sudo systemctl status timestd-fusion
sudo systemctl status timestd-web-api
```

### Check Optional Services

```bash
sudo systemctl status timestd-ionex-download.timer    # Should be active
sudo systemctl status timestd-chrony-monitor.timer    # Should be active
sudo systemctl status timestd-radiod-monitor          # If enabled
```

### Verify Data Flow

1. **Raw Data:** Check Digital RF files: `ls -lh /var/lib/timestd/raw_archive/`
2. **Analytics:** Check L2 HDF5 files: `ls -lh /var/lib/timestd/phase2/l2/`
3. **Fusion:** Check fused output: `ls -lh /var/lib/timestd/phase2/fusion/`
4. **Web API:** Open `http://localhost:8000` in browser
5. **Chrony:** Run `chronyc sources` and look for SHM reference (should show reachability)

---
