# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** April 8, 2026

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
- **Station location** (either one):
  - Maidenhead grid square (6 or 10 characters, e.g., `FN31pr` or `FN31pr42ab`)
  - Latitude and longitude (decimal degrees)
- **ka9q-radio status address** (e.g., `hf-status.local` — find via `avahi-browse -rt _ka9q-ctl._udp`)
- **PSWS station ID and instrument ID** (if uploading to the HamSCI PSWS network)
- **GNSS receiver address and port** (if using a ZED-F9P for VTEC monitoring)

---

## Installation

```bash
# 1. Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# 2. Run deploy script (idempotent install/update)
sudo ./scripts/deploy.sh
```

The deploy script:

1. Installs all apt dependencies and verifies Python 3.10+
2. Installs and configures chrony for SHM clock discipline
3. Configures UDP receive buffers for RTP packet handling
4. Creates the `timestd` system user and production directories
5. Sets up the Python virtual environment (`/opt/hf-timestd/venv`)
6. Copies web-api, scripts, and systemd service files
7. **Runs the setup wizard** (`setup-station.sh`) — an interactive prompt that collects your station identity, location (grid square or lat/lon), ka9q-radio address, timing mode, GNSS VTEC settings, and PSWS upload credentials, then generates `/etc/hf-timestd/timestd-config.toml`
8. Enables systemd services according to the configured **service profile** (see [Service Profiles](#service-profiles) below)

The installation is **idempotent** — safe to re-run for updates. On re-run, it will skip steps that are already complete and offer to re-run the configuration wizard if a config already exists.

**Options:**

| Flag | Purpose |
|------|---------|
| `--pull` | Run `git pull` before deploying |
| `--reconfig` | Re-run the station configuration wizard |
| `--restart-all` | Also restart core-recorder (causes a brief data gap) |
| `--no-restart` | Sync everything but don't restart services |
| `--yes` / `-y` | Accept defaults, no interactive prompts |

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
sudo ./scripts/deploy.sh --pull
```

This pulls the latest code, syncs `/opt/hf-timestd`, updates the Python venv, syncs scripts/web-api/systemd files, and restarts affected services.

---

## Re-running the Configuration Wizard

To change station settings after installation:

```bash
sudo ./scripts/setup-station.sh --config /etc/hf-timestd/timestd-config.toml --reconfig
```

Or re-run the full installer, which will offer to re-run the wizard:

```bash
sudo ./scripts/deploy.sh --reconfig
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

## Service Profiles

Which services run is controlled by a **profile** in `[services]` of the config
file.  The core-recorder is always on — it is the irreplaceable raw data source.

| Profile | What runs | Use case |
|---------|-----------|----------|
| **archive** | core-recorder, prune | Raw IQ preservation, minimal resources |
| **rtp** | archive + web-api, radiod-monitor, pipeline-watchdog, GRAPE | Standard GPSDO timing (default) |
| **fusion** | rtp + metrology, l2-calibration, fusion, chrony-monitor | GPS-denied timing from HF broadcasts |
| **full** | fusion + physics, ionex-download, iono-reanalysis | Full science + timing |

Per-service bool overrides in `[services]` layer on top of the profile.  For
example, `profile = "rtp"` with `metrology = true` enables metrology for
study without switching to the full fusion profile.

```bash
# View current profile and per-service state
hf-timestd profile show

# Switch profile (updates config + enables/disables systemd units)
sudo hf-timestd profile set fusion

# Toggle individual services on top of the current profile
sudo hf-timestd service enable metrology
sudo hf-timestd service disable physics

# Live status of all services (config vs systemd)
hf-timestd service status
```

The profile is set during initial installation (the setup wizard defaults to
`rtp`) and can be changed at any time.  `deploy.sh` reads the profile from
config and applies it on every run.

### Service Reference

| Service | Description |
|---------|-------------|
| **`timestd-core-recorder`** | Records IQ from radiod RTP streams, writes binary archive + JSON metadata |
| **`timestd-metrology@{CHANNEL}`** | Per-channel tone detection, tick timing, L1/L2 measurements (HDF5) |
| **`timestd-l2-calibration`** | Cross-station geometric + ionospheric corrections |
| **`timestd-fusion`** | Multi-broadcast Kalman fusion, feeds Chrony SHM (TSL1/TSL2) |
| **`timestd-physics`** | Carrier-phase dTEC, group-delay TEC, propagation mode ID |
| **`timestd-vtec`** | GNSS VTEC monitoring (requires `gnss_vtec.enabled = true`) |
| **`timestd-web-api`** | FastAPI dashboard on port 8000 |
| **`timestd-radiod-monitor`** | Hardware health monitoring |
| **`timestd-chrony-monitor.timer`** | Chrony reachability watchdog |
| **`timestd-pipeline-watchdog.timer`** | Pipeline health watchdog |
| **`timestd-ionex-download.timer`** | Daily IONEX map download from NASA CDDIS |
| **`timestd-iono-reanalysis.timer`** | Ionospheric reanalysis |
| **`grape-daily.timer`** | Daily GRAPE decimation, spectrograms, DRF packaging, PSWS upload |
| **`timestd-prune.timer`** | Nightly data retention enforcement |

---

## Verifying Operation

### Check Service Status

```bash
# Overview of all services (config state + live systemd state)
hf-timestd service status

# Or check individual services
sudo systemctl status timestd-core-recorder
```

### Verify Data Flow

1. **Raw Buffer:** Check binary archives: `ls -lh /var/lib/timestd/raw_buffer/` (expect 10-min chunk files by default)
2. **L1 Metrology:** `ls -lh /var/lib/timestd/phase2/*/metrology/` — HDF5 files (if metrology is enabled)
3. **L2 Calibration:** `ls -lh /var/lib/timestd/phase2/*/clock_offset/`
4. **Fusion:** `ls -lh /var/lib/timestd/phase2/fusion/`
5. **Web API:** Open `http://localhost:8000` in browser
6. **Chrony:** `chronyc sources` — look for TSL1/TSL2 references (if fusion is enabled)

### Pipeline Health Check

```bash
sudo ./scripts/verify_pipeline.sh
```

---
