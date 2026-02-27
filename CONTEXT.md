# Project Context: HF Time Standard (hf-timestd)

**Version:** 6.8.0  
**Last Updated:** February 27, 2026  
**Author:** Michael James Hauan (AC0G)

This file provides a quick-reference bootstrap for AI assistants and new developers.
For detailed documentation, see the canonical docs listed at the bottom.

---

## System Overview

**hf-timestd** is a dual-purpose HF time transfer and ionospheric measurement system operating 24/7 from grid square EM38ww (~38.9°N, ~92.1°W, central Missouri).

- **Receiver:** GPSDO-locked RX888 SDR via KA9Q-radio (`radiod`)
- **Input:** RTP-timestamped IQ at 24 kHz/channel (GPS+PPS authoritative, ~50 μs)
- **Stations:** WWV, WWVH, CHU, BPM — 9 frequencies, 17 logical broadcasts
- **Modes:** RTP Mode (ionospheric science) and Fusion Mode (UTC recovery from HF alone)

---

## The 17 Broadcasts (4 Stations)

| Station | Location | Frequencies (kHz) | Count |
|---------|----------|-------------------|-------|
| **WWV** | Fort Collins, CO (40.68°N, 105.04°W) | 2500, 5000, 10000, 15000, 20000, 25000 | 6 |
| **WWVH** | Kauai, HI (21.99°N, 159.76°W) | 2500, 5000, 10000, 15000 | 4 |
| **CHU** | Ottawa, Canada (45.30°N, 75.75°W) | 3330, 7850, 14670 | 3 |
| **BPM** | Pucheng, China (34.95°N, 109.54°E) | 2500, 5000, 10000, 15000 | 4 |

**Shared frequencies** (require discrimination): 2500, 5000, 10000, 15000 kHz  
**Unique frequencies** (single station): 20000, 25000 (WWV), 3330, 7850, 14670 (CHU)

---

## Eight Services (systemd)

| Service | Responsibility |
|---------|---------------|
| `timestd-core-recorder` | Binary IQ archive (`.bin.zst` + JSON sidecars) |
| `timestd-metrology` | Tone detection, TickEdgeDetector, D_clock extraction |
| `timestd-l2-calibration` | Geometric + ionospheric corrections → L2 timing |
| `timestd-fusion` | Per-broadcast Kalman + WLS fusion → Chrony SHM (TSL1/TSL2) |
| `timestd-vtec` | GNSS VTEC (ZED-F9P) + IONEX download |
| `timestd-physics` | Carrier-phase dTEC, group-delay TEC validation, T_iono |
| `timestd-web-api` | FastAPI dashboard & REST API (port 8000) |
| `timestd-radiod-monitor` | Hardware health monitoring |

**Optional:** `grape-daily.timer` (daily GRAPE DRF packaging + PSWS upload)

---

## Key Data Products

| Product | Source | Description |
|---------|--------|-------------|
| **D_clock** | TickEdgeDetector | System-UTC offset, ±0.5 ms fused |
| **Carrier-phase dTEC** | `carrier_tec.py` + `physics_fusion_service.py` | Primary ionospheric product (~6 mTECU/min), GNSS-anchored |
| **Group-delay TEC** | `tec_estimator.py` | Validation product (noise-dominated, SNR ~0.13) |
| **Doppler** | TickEdgeDetector (carrier phase slope) | Ionospheric dynamics |
| **SNR** | TickEdgeDetector (per-tick matched filter) | 8–42 dB dynamic range |
| **CHU FSK timecode** | `chu_fsk_decoder.py` | TAI-UTC, DUT1, UTC cross-validation |
| **GRAPE** | `grape_daily.py` | 10 Hz decimation → DRF → PSWS upload |

---

## Directory Layout

| Component | Path |
|-----------|------|
| **Git repo** | `/home/mjh/git/hf-timestd/` |
| **Production install** | `/opt/hf-timestd/` |
| **Config** | `/etc/hf-timestd/timestd-config.toml` |
| **Data root** | `/var/lib/timestd/` |
| **Raw IQ** | `/var/lib/timestd/raw_buffer/{CHANNEL}/{YYYYMMDD}/` |
| **L2 products** | `/var/lib/timestd/phase2/{CHANNEL}/` |
| **Fusion** | `/var/lib/timestd/phase2/fusion/` |
| **Logs** | `/var/log/hf-timestd/` |

**Deploy:** `sudo scripts/update-production.sh [--pull]`

---

## Quick Commands

```bash
# Service status
sudo systemctl status timestd-metrology timestd-fusion timestd-physics

# Recent logs
journalctl -u timestd-fusion --since "5 min ago" --no-pager

# Pipeline health
sudo scripts/verify_pipeline.sh

# Web UI
curl http://localhost:8000/api/health | python3 -m json.tool

# Deploy changes from git to production
sudo scripts/update-production.sh

# Run tests
source venv/bin/activate
python -m pytest tests/ -v
```

---

## Canonical Documentation

| Document | Audience | Purpose |
|----------|----------|---------|
| `README.md` | Users | Overview, quick start, feature list |
| `INSTALLATION.md` | Users | Setup guide |
| `docs/ARCHITECTURE.md` | Engineers | Design philosophy ("the why") |
| `docs/TECHNICAL_REFERENCE.md` | Developers | Algorithms, data formats, release notes |
| `docs/METROLOGY.md` | Metrologists | Uncertainty budgets, ISO GUM, validation |
| `docs/PHYSICS.md` | Scientists | Ionospheric measurements, TEC, propagation |
| `CHANGELOG.md` | All | Version history |

---

## Key Source Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/tick_edge_detector.py` | Primary timing source (D_clock, Doppler, SNR) |
| `src/hf_timestd/core/metrology_engine.py` | Per-minute DSP pipeline |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | WLS fusion → Chrony SHM |
| `src/hf_timestd/core/propagation_model.py` | HFPropagationModel (multi-mode delay) |
| `src/hf_timestd/core/iono_data_service.py` | WAM-IPE/GIRO real-time ionospheric data |
| `src/hf_timestd/core/carrier_tec.py` | Carrier-phase dTEC |
| `src/hf_timestd/core/physics_fusion_service.py` | dTEC anchoring + science products |
| `src/hf_timestd/core/chu_fsk_decoder.py` | CHU FSK timecode decoding |
| `src/hf_timestd/grape/grape_daily.py` | GRAPE daily processing + PSWS upload |
| `web-api/main.py` | FastAPI application |
