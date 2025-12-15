# HF Time Standard Analysis (hf-timestd) - Installation Guide

**Author:** Michael James Hauan (AC0G)  
**Last Updated:** December 15, 2025

This guide covers installing and configuring `hf-timestd` for recording and analyzing HF time standard broadcasts (BPM, CHU, WWV, WWVH).

**Scope note:** Decimation/10 Hz products and any PSWS/GRAPE upload workflows are handled in the separate `grape-recorder` project.

---

## Prerequisites

### Hardware

- SDR supported by ka9q-radio (e.g. RX888 MkII, Airspy HF+, SDRplay)
- HF antenna covering 2.5-25 MHz
- Linux host with multicast-capable LAN

### Software

- Linux (Debian/Ubuntu class)
- Python 3.10+
- Node.js 18+ (for the Web UI)
- ka9q-radio installed and running (`radiod`)

---

## Install (development/test mode)

```bash
# Clone repository
git clone https://github.com/mijahauan/hf-timestd.git
cd hf-timestd

# Run installer in test mode
./scripts/install.sh --mode test

# Edit configuration
nano config/timestd-config.toml

# Start services (test scripts)
./scripts/timestd-all.sh -start

# Check status
./scripts/timestd-all.sh -status
```

Test mode data root defaults to:

- `/tmp/timestd-test/`

---

## Install (production mode)

```bash
sudo ./scripts/install.sh --mode production --user $USER
sudo nano /etc/hf-timestd/timestd-config.toml

sudo systemctl start timestd-core-recorder timestd-analytics timestd-web-ui
sudo systemctl enable timestd-core-recorder timestd-analytics timestd-web-ui
```

Production mode paths:

- Data: `/var/lib/hf-timestd/`
- Logs: `/var/log/hf-timestd/`
- Config: `/etc/hf-timestd/`

---

## Configuration overview

The primary configuration file is:

- test: `config/timestd-config.toml`
- production: `/etc/hf-timestd/timestd-config.toml`

At minimum configure:

- `[station]` (callsign, grid square)
- `[ka9q]` (radiod status/data addresses)
- `[[recorder.channels]]` entries for the time standard stations/frequencies you want

---

## Verifying operation

- Confirm `radiod` is running and discoverable on the network.
- Confirm the Web UI loads at `http://localhost:3000`.
- Confirm new data is being written under the configured data root.

---

## Phase 3 / GRAPE products

If you need 10 Hz products or PSWS/GRAPE uploads, use the separate `grape-recorder` project. `hf-timestd` focuses on time-standard recording and time-transfer analytics.
