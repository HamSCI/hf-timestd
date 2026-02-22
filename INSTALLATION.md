# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** February 14, 2026

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

# 4. Enable and start core services (in dependency order)
sudo systemctl enable --now timestd-core-recorder    # Phase 1: RTP → Raw Buffer
sudo systemctl enable --now timestd-metrology        # Phase 2: L1 Raw Measurements
sudo systemctl enable --now timestd-l2-calibration   # Phase 2: L2 Calibrated Timing
sudo systemctl enable --now timestd-fusion           # Phase 3: Fusion → Chrony SHM
sudo systemctl enable --now timestd-physics          # Phase 3: TEC Estimation
sudo systemctl enable --now timestd-web-api          # Web API & Dashboard

# 5. Enable optional services
sudo systemctl enable --now timestd-vtec                    # GNSS VTEC (if enabled in config)
sudo systemctl enable --now timestd-ionex-download.timer    # Daily IONEX downloads
sudo systemctl enable --now timestd-chrony-monitor.timer    # Chrony health monitoring
sudo systemctl enable --now timestd-radiod-monitor          # Radiod health monitoring
sudo systemctl enable --now grape-daily.timer                # Daily GRAPE processing + PSWS upload
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

# (If you are not using install.sh) Create/update local venv
./scripts/ensure-venv.sh --mode test --venv ./venv --python python3

# 2. Edit local configuration
nano config/timestd-config.toml

# 3. Start core recorder
source venv/bin/activate
python -m hf_timestd --config config/timestd-config.toml

# 4. In another terminal, start web API
cd web-api && ../venv/bin/python main.py
```

### Test Paths

- **Data:** `/tmp/timestd-test/`
- **Logs:** `/tmp/timestd-test/logs/`
- **Config:** `config/timestd-config.toml`

---

## Configuration Overview

After installation, configure your station-specific settings.

**📖 See [docs/STATION_SETUP_GUIDE.md](docs/STATION_SETUP_GUIDE.md) for detailed configuration instructions.**

### Required Settings

Edit `/etc/hf-timestd/timestd-config.toml`:

```toml
[station]
callsign = "<YOUR_CALLSIGN>"      # e.g., "W1ABC"
grid_square = "<YOUR_GRID>"       # 10-char Maidenhead, e.g., "FN42ab12cd"
latitude = 0.0                    # Decimal degrees (required for physics)
longitude = 0.0                   # Decimal degrees (required for physics)
id = "<PSWS_STATION_ID>"          # e.g., "S000171" (if uploading to PSWS)
instrument_id = "<PSWS_INSTR_ID>" # e.g., "172" (if uploading to PSWS)

[ka9q]
status_address = "<YOUR_RADIOD>"  # e.g., "hf-status.local"
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
