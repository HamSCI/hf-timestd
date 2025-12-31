# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 31, 2025

This guide covers installing and configuring `hf-timestd` for recording and analyzing HF time standard broadcasts (BPM, CHU, WWV, WWVH).

---

## Prerequisites

### Hardware

- SDR supported by ka9q-radio (e.g. RX888 MkII, Airspy HF+, SDRplay)
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

# 2. Run installer (installs all dependencies)
sudo ./scripts/install.sh --mode production --user $USER

# 3. Edit global configuration
sudo nano /etc/hf-timestd/timestd-config.toml

# 4. Enable and start services
sudo systemctl enable --now timestd-core-recorder
sudo systemctl enable --now timestd-analytics
sudo systemctl enable --now timestd-fusion
sudo systemctl enable --now timestd-web-ui-fastapi
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

## Verifying Operation

1. **Check Services:** `systemctl status timestd-fusion` (should be Active)
2. **Check Web UI:** Open `http://localhost:3000` (or configured port)
3. **Check Data:** Verify Digital RF files are appearing in `/var/lib/timestd/raw_archive/`
4. **Check Chrony:** Run `chronyc sources` and look for the SHM reference.

---
