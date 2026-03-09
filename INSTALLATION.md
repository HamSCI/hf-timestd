# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** February 26, 2026

This guide covers installing and configuring `hf-timestd` for recording and analyzing HF time standard broadcasts (BPM, CHU, WWV, WWVH).

---

## Prerequisites

### Hardware

- GPSDO-governed SDR supported by ka9q-radio (e.g. RX888 MkII, Airspy HF+, SDRplay)
- GNSS receiver for VTEC (Optional but recommended, e.g., UBlox ZED-F9P, WaveShare LG290P)
- HF antenna covering 2.5-25 MHz
- Linux host with multicast-capable LAN

### Storage Requirements

The system generates substantial data. **The minimum practical installation requires enough disk to hold at least 2 days of raw IQ data plus derived products for the number of channels configured.**

#### Per-channel daily data volumes (measured, zstd compression, 24 kHz IQ)

| Product | Per channel/day | Reconstructible? |
|---|---|---|
| Raw IQ buffer | ~6.7 GB | — source of truth |
| `all_arrivals` | ~3.0 GB | Yes — from raw |
| `tick_phase` | ~2.0 GB | Yes — from raw |
| `tick_timing` | ~60 MB | Yes — from raw |
| `detection_attempts` | ~85 MB | Yes — from raw |
| L1 metrology | ~40 MB | Yes — from raw |
| L2 clock_offset | ~90 MB | Yes — from raw |
| L3 science (dtec/tec) | ~100 MB total | No — final product |

**Rule of thumb: ~10 GB × N_channels per day** for raw buffer plus all derived products.

#### Minimum disk by channel count

| Channels | Min disk (2-day raw+derived) | Recommended (3-day) |
|---|---|---|
| 3 | 60 GB | 90 GB |
| 6 | 120 GB | 180 GB |
| 9 | 180 GB | 270 GB |

> **Note:** A 120 GB disk with 9 channels is at the absolute margin — the system will fill the disk within a single day if retention is not set aggressively. See [Storage Retention Configuration](#storage-retention-configuration) below.

#### Storage retention configuration

The nightly prune timer (`timestd-prune.timer`, runs at 03:00 UTC) manages retention automatically. The default retention policy keeps derived products for the same number of days as the raw buffer, since **all L1/L2 derived products can be reconstructed from the raw IQ data**. Only L3 science outputs (dtec, tec) are kept longer as the final irreducible product.

Default retention (sufficient for disks that comfortably hold 3+ days of data per channel):
```
RAW_BUFFER_DAYS=3        # and all derived products
SCIENCE_DTEC_TS_DAYS=7
SCIENCE_DTEC_DAYS=30
SCIENCE_TEC_DAYS=90
```

For smaller disks, create `/etc/hf-timestd/prune.conf` with tighter values. Example for a 120 GB disk with 9 channels (`RAW_BUFFER_DAYS=1` is the minimum safe value):
```bash
# /etc/hf-timestd/prune.conf — tight retention for small disk
RAW_BUFFER_DAYS=1
ALL_ARRIVALS_DAYS=1
TICK_PHASE_DAYS=1
L1_DAYS=1
L2_DAYS=1
SCIENCE_DTEC_TS_DAYS=3
SCIENCE_DTEC_DAYS=15
SCIENCE_TEC_DAYS=30
DISK_WARN_PCT=80
DISK_CRIT_PCT=88
```

On constrained disks also change the prune timer to run hourly rather than daily, since the disk can fill within a single day:
```bash
sudo mkdir -p /etc/systemd/system/timestd-prune.timer.d
sudo tee /etc/systemd/system/timestd-prune.timer.d/override.conf <<EOF
[Timer]
OnCalendar=
OnCalendar=hourly
RandomizedDelaySec=120
EOF
sudo systemctl daemon-reload && sudo systemctl restart timestd-prune.timer
```

When disk usage exceeds `DISK_CRIT_PCT`, the prune script automatically flushes all `all_arrivals` and `tick_phase` files regardless of age, since these are reconstructible and are the largest products after raw IQ data.

### Software

- Linux (Debian/Ubuntu class)
- Python 3.10+
- **ka9q-radio** installed and running (`radiod`)
- **HDF5 Libraries** (installed automatically by the installer)

### Information You Will Need

Before running the installer, have the following ready:

- **Station callsign** (e.g., `W1ABC`)
- **Maidenhead grid square** (6 or 10 characters, e.g., `FN31pr` or `FN31pr42ab`)
- **Station latitude and longitude** (decimal degrees)
- **ka9q-radio status address** (e.g., `hf-status.local` — find via `avahi-browse -rt _ka9q-ctl._udp`)
- **PSWS station ID and instrument ID** (if uploading to the HamSCI PSWS network)
- **GNSS receiver address and port** (if using a ZED-F9P for VTEC monitoring)

---

## Installation

```bash
# 1. Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# 2. Run installer (interactive wizard guides configuration)
sudo ./scripts/install.sh
```

The installer:

1. Installs all apt dependencies and verifies Python 3.10+
2. Installs and configures chrony for SHM clock discipline
3. Configures UDP receive buffers for RTP packet handling
4. Creates the `timestd` system user and production directories
5. Sets up the Python virtual environment (`/opt/hf-timestd/venv`)
6. Copies web-api, scripts, and systemd service files
7. **Runs the setup wizard** (`setup-station.sh`) — an interactive prompt that collects your station identity, ka9q-radio address, timing mode, GNSS VTEC settings, and PSWS upload credentials, then generates `/etc/hf-timestd/timestd-config.toml`
8. Installs and enables all systemd services and timers

The installation is **idempotent** — safe to re-run. On re-run, it will skip steps that are already complete and offer to re-run the configuration wizard if a config already exists.

### After Installation

The installer will offer to start all services automatically. If you decline, or need to start them later:

```bash
# Start all services, timers, and run health check
sudo ./scripts/start-services.sh

# Check status only
sudo ./scripts/start-services.sh --status
```

### Production Paths

- **Data:** `/var/lib/timestd/`
- **Logs:** `/var/log/hf-timestd/`
- **Config:** `/etc/hf-timestd/timestd-config.toml`
- **Venv:** `/opt/hf-timestd/venv/`
- **Web API:** `/opt/hf-timestd/web-api/`

---

## Updating

```bash
cd /path/to/hf-timestd
sudo ./scripts/update-production.sh --pull
```

This pulls the latest code, reinstalls the Python package, syncs scripts/web-api/systemd files, and restarts affected services. The core recorder is **not** restarted automatically to avoid data gaps.

---

## Re-running the Configuration Wizard

To change station settings after installation:

```bash
sudo ./scripts/setup-station.sh --config /etc/hf-timestd/timestd-config.toml --reconfig
```

Or re-run the full installer, which will offer to re-run the wizard:

```bash
sudo ./scripts/install.sh
```

---

## Configuration Overview

The setup wizard populates `/etc/hf-timestd/timestd-config.toml` from a template. You can also edit it manually.

**📖 See [docs/STATION_SETUP_GUIDE.md](docs/STATION_SETUP_GUIDE.md) for detailed configuration instructions.**

### Key Sections

```toml
[station]
callsign = "W1ABC"
grid_square = "FN31pr42ab"
latitude = 42.123
longitude = -71.456
id = "S000171"              # PSWS station ID
instrument_id = "172"       # PSWS instrument ID

[ka9q]
status_address = "hf-status.local"
```

### Optional: GNSS VTEC Monitoring

If you have a u-blox ZED-F9P or similar dual-frequency GNSS receiver:

```toml
[gnss_vtec]
enabled = true
host = "192.168.0.202"   # IP of GNSS receiver or ser2net bridge
port = 9000              # TCP port for UBX data stream
```

See [docs/ZED_F9P_TEC_CONFIGURATION.md](docs/ZED_F9P_TEC_CONFIGURATION.md) for receiver setup.

---

## Service Overview

### Core Services (Required)

- **`timestd-core-recorder`** - Phase 1: Records RTP audio streams from radiod, writes raw buffer archives
- **`timestd-metrology`** - Phase 2: L1 raw measurements (tone detection, BCD decoding, timing extraction)
- **`timestd-l2-calibration`** - Phase 2: L2 calibrated timing (geometric + TEC + system corrections)
- **`timestd-fusion`** - Phase 3: Multi-broadcast Kalman fusion, feeds Chrony SHM (TSL1/TSL2)
- **`timestd-physics`** - Phase 3: TEC estimation from multi-frequency measurements
- **`timestd-web-api`** - FastAPI web server on port 8000 (metrology dashboard, logs, API)

### Optional Services

- **`timestd-vtec`** - GNSS VTEC monitoring (requires GNSS receiver, enabled via config)
- **`timestd-ionex-download.timer`** - Daily download of global IONEX maps from NASA CDDIS
- **`timestd-chrony-monitor.timer`** - Monitors Chrony reachability and alerts on issues
- **`timestd-radiod-monitor`** - Monitors radiod health and restarts channels if needed
- **`grape-daily.timer`** - Daily GRAPE processing: decimation, spectrograms, DRF packaging, PSWS upload (01:00 UTC)

---

## Verifying Operation

### Check Core Services

```bash
# All should show "active (running)"
sudo systemctl status timestd-core-recorder
sudo systemctl status timestd-metrology
sudo systemctl status timestd-l2-calibration
sudo systemctl status timestd-fusion
sudo systemctl status timestd-physics
sudo systemctl status timestd-web-api
```

### Check Optional Services

```bash
sudo systemctl status timestd-ionex-download.timer    # Should be active
sudo systemctl status timestd-chrony-monitor.timer    # Should be active
sudo systemctl status timestd-radiod-monitor          # If enabled
```

### Verify Data Flow

1. **Raw Buffer:** Check binary archives: `ls -lh /var/lib/timestd/raw_buffer/`
2. **L1 Metrology:** Check L1 HDF5 files: `ls -lh /var/lib/timestd/phase2/*/metrology/`
3. **L2 Calibration:** Check L2 HDF5 files: `ls -lh /var/lib/timestd/phase2/*/clock_offset/`
4. **Fusion:** Check fused output: `ls -lh /var/lib/timestd/phase2/fusion/`
5. **Web API:** Open `http://localhost:8000` in browser
6. **Chrony:** Run `chronyc sources` and look for TSL1/TSL2 references (should show reachability)

---
