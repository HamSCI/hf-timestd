# Project Context: HF Time Standard (hf-timestd)

**Version:** 6.12.0  
**Last Updated:** March 16, 2026  
**Author:** Michael James Hauan (AC0G)

This file provides a quick-reference bootstrap for AI assistants and new developers.
For detailed documentation, see the canonical docs listed at the bottom.

---

## NEXT SESSION OBJECTIVE: QEX Article Preparation

**Goal:** Draft or substantially advance a QEX magazine article describing the metrology accomplishments of hf-timestd. The article has three interlocking subjects:

1. **UTC recovery from HF time signals** — the primary metrology achievement: using
   received WWV/WWVH/CHU signals with a GPSDO-locked SDR to estimate UTC to ±0.5 ms.
2. **dTEC/dt** — the first physics product: carrier-phase differential TEC rate,
   extracted as a byproduct of the timing pipeline, GNSS-anchored.
3. **Mode identification** — the second physics product: HF propagation mode (1F/2F/3F,
   E/F layer) via numerical ray tracing (PHaRLAP/pyLAP), verified operational v6.8.

### Article Arc (Narrative Spine)

> A GPSDO-locked RX888 SDR, running ka9q-radio and hf-timestd software, monitors 17
> HF time-standard broadcasts continuously from central Missouri. The system recovers
> UTC from HF alone to ±0.5 ms — competitive with legacy hardware WWVB receivers —
> while simultaneously producing two ionospheric science products that are only possible
> *because* you're doing precision timing: a carrier-phase dTEC/dt series accurate to
> ~6 mTECU/min, and numerical ray-traced propagation mode identification.

The article should make clear that the timing and the physics are **coupled** — the same
per-minute coherent phase measurements that yield D_clock also yield dTEC/dt; the same
multi-frequency path geometry that yields ionospheric correction also yields mode ID.

### QEX Audience Guidance

- **Journal:** QEX — ARRL's technical journal for sophisticated amateur radio operators.
  Peer-reviewed style but written for a technically literate ham audience, not academic.
- **Tone:** Narrative + technical. Equations welcome but must be motivated. Figures are
  essential (time series, system diagram, ionospheric plot).
- **Reproducibility emphasis:** Readers will want to know: "Can I build this?" Answer:
  yes, with a GPSDO-locked RX888 SDR + ka9q-radio + this open-source software.
- **Avoid:** Dense academic citation chains. Prioritize clarity over completeness.
- **Author callsign:** AC0G

### Key Technical Facts for the Article

**Hardware / RF chain:**
- Receiver: RX888 Mk II SDR, driven by GPSDO (GPS+PPS locked oscillator)
- Frontend: ka9q-radio (`radiod`) — SDR driver + channelizer, outputs RTP multicast IQ
- RTP timestamp accuracy: ~50 μs (GPS+PPS authoritative via `radiod`)
- IQ bandwidth: 24 kHz per channel, complex64
- Location: EM38ww grid square (~38.9°N, 92.1°W, central Missouri)

**Signals monitored:**
- 9 channels, 17 logical broadcasts across 4 stations
- WWV (Fort Collins CO): 2.5, 5, 10, 15, 20, 25 MHz
- WWVH (Kauai HI): 2.5, 5, 10, 15 MHz
- CHU (Ottawa ON): 3.330, 7.850, 14.670 MHz
- BPM (Pucheng China): 2.5, 5, 10, 15 MHz

**Timing pipeline (metrology) — what yields UTC:**
- `TickEdgeDetector`: per-minute coherent matched filter → 1 pps tick TOA, Doppler, SNR
- `BroadcastKalmanFilter`: per-broadcast state tracker (delay, drift, uncertainty)
- `multi_broadcast_fusion.py`: WLS fusion of all validated broadcasts → fused D_clock
- Chrony SHM (TSL1/TSL2): fused estimate disciplines system clock
- **Achieved accuracy: ±0.5 ms fused (1σ), improving with GNSS VTEC correction**
- D_clock = system_time − UTC; sign convention: positive = system ahead

**WWV/WWVH discrimination (shared frequencies 2.5/5/10/15 MHz):**
- Male (WWV) vs female (WWVH) voice ID by tone schedule + power ratio + Bayesian prior
- `wwvh_discrimination.py` + `bpm_discriminator.py`
- Required because WWV and WWVH occupy identical frequencies; signals overlap in MOdify

**Physics Product 1 — Carrier-phase dTEC/dt:**
- Source: `carrier_tec.py` + `physics_fusion_service.py`
- Method: differential phase between two co-path frequencies (e.g. 5 MHz and 10 MHz
  on the same WWV path) → carrier-phase TEC rate, free of absolute group-delay noise
- Precision: ~6 mTECU/min (demonstrated)
- Anchoring: GNSS ZED-F9P dual-frequency VTEC provides absolute TEC reference;
  carrier-phase gives the *rate* (dTEC/dt) with far lower noise than group delay
- Group-delay TEC (from timing dispersion): SNR ~0.13 — validation product only,
  noise-dominated; documents *why* carrier phase is necessary
- Science value: continuous, passively acquired dTEC/dt from a fixed HF path is a
  unique complement to overhead GNSS TEC (vertical vs oblique paths)

**Physics Product 2 — Mode Identification:**
- Source: `raytrace_engine.py` + `propagation_mode_solver.py`
- Method: PHaRLAP 4.7.4 numerical 2D ray tracing via pyLAP Python wrapper;
  IRI-2020 ionospheric model for electron density profile; great-circle path geometry
- Status: **Verified operational v6.8** (March 2026 session)
  - WWV 10 MHz: 3F mode found, 9.03 ms delay, 5.5° elevation, 92 km apogee ✓
  - WWV  5 MHz: 3F mode found, 8.98 ms delay, 5.0° elevation, 88 km apogee ✓
  - foF2 = 10.47 MHz at 291 km from IRI-2020 ✓
  - Geometric fallback active when pyLAP/PHaRLAP unavailable ✓
- Architecture: offline/advisory overlay — NOT on real-time chrony critical path
- pyLAP fork: https://github.com/mijahauan/PyLap (includes macOS + PHaRLAP 4.7.4 patches)
- Key bugs fixed in v6.8 (article should mention as methodology validation):
  - Ne units: IRI returns m⁻³; raytrace_2d expects cm⁻³ (×10⁻⁶ conversion)
  - Multi-hop stride: ray_data C-array stride was num_rays×9, corrected to num_rays×19
  - Multi-call segfault: Fortran SAVE variables; fixed by single call with nhops=max_hops

**Uncertainty budget (ISO GUM, from docs/METROLOGY.md):**
- u_rtp: ~50 μs (GPS+PPS RTP timestamp)
- u_detection: ~0.2 ms (matched-filter tick edge)
- u_propagation_model: ~5 ms geometric, ~1.5 ms with IRI, ~0.3 ms with GNSS VTEC
- u_fused: ±0.5 ms (1σ) — 17-broadcast WLS

### Article Structure (Proposed Outline)

```
1. Introduction
   - The problem: HF time signals have ionospheric delays; correcting them yields UTC
     and, as a bonus, characterises the ionosphere
   - What's new: full numerical raytrace + carrier-phase TEC + GNSS anchoring in one
     open-source system on commodity SDR hardware

2. System Description
   - Hardware: GPSDO RX888 + ka9q-radio (diagram)
   - Signals: WWV/WWVH/CHU/BPM, 17 broadcasts, discrimination problem
   - Software pipeline: 8 services, data flow (block diagram)

3. Metrology: UTC Recovery
   - TickEdgeDetector: matched filter, coherent phase, SNR 8–42 dB
   - Per-broadcast Kalman filter: delay + drift tracking
   - Multi-frequency ionospheric correction hierarchy
   - WLS fusion → Chrony SHM → ±0.5 ms

4. Physics Product 1: Carrier-Phase dTEC/dt
   - Why group-delay TEC is noise-dominated (SNR 0.13)
   - Carrier-phase differential method — theory
   - Results: ~6 mTECU/min, GNSS-anchored
   - Comparison: oblique HF path vs overhead GNSS VTEC

5. Physics Product 2: Mode Identification
   - Propagation modes (1F/2F/3F, E/F layer) and why they matter for timing
   - IRI-2020 electron density + PHaRLAP 2D ray tracing
   - Example: WWV 10 MHz, verified 3F at 9 ms
   - Integration with timing pipeline (mode confidence → uncertainty budget)

6. Discussion
   - The coupling: timing and physics share the same measurements
   - Science value: continuous oblique TEC from fixed-path monitor
   - Limitations and future work

7. Conclusion
   - Open source, reproducible, GPSDO SDR + ka9q-radio
   - GitHub: https://github.com/mijahauan/hf-timestd
```

### Recommended Pre-Session Actions

Before the article-drafting session, gather:
```bash
# On the production system (EM38ww):
# 1. Pull a representative D_clock time series (24h) for a plot
python3 -c "import h5py; ..."  # from /var/lib/timestd/phase2/fusion/

# 2. Pull a representative dTEC/dt time series (same 24h window)
# from /var/lib/timestd/phase2/science/tec/

# 3. Confirm raytrace_engine produces mode ID for current conditions
PHARLAP_HOME=... python3 -c "from hf_timestd.core.raytrace_engine import ..."

# 4. Note current foF2 and hmF2 from IRI for the article's example case
```

### Key Files for Article Writing

| File | Role in Article |
|------|----------------|
| `src/hf_timestd/core/tick_edge_detector.py` | §3 Metrology — primary timing algorithm |
| `src/hf_timestd/core/broadcast_kalman_filter.py` | §3 Metrology — per-broadcast Kalman |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | §3 Metrology — WLS fusion, Chrony SHM |
| `src/hf_timestd/core/carrier_tec.py` | §4 Physics — carrier-phase dTEC/dt |
| `src/hf_timestd/core/physics_fusion_service.py` | §4 Physics — GNSS anchoring |
| `src/hf_timestd/core/raytrace_engine.py` | §5 Physics — mode ID via PHaRLAP |
| `src/hf_timestd/core/propagation_mode_solver.py` | §5 Physics — mode classification |
| `src/hf_timestd/core/wwvh_discrimination.py` | §2 System — station discrimination |
| `docs/METROLOGY.md` | Uncertainty budget reference |
| `docs/PHYSICS.md` | Ionospheric science methodology |

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
| **Mode ID** | `raytrace_engine.py` + `propagation_mode_solver.py` | 1F/2F/3F propagation mode (PHaRLAP raytrace), advisory overlay |

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
| `src/hf_timestd/core/broadcast_kalman_filter.py` | Per-broadcast Kalman delay/drift tracker |
| `src/hf_timestd/core/carrier_tec.py` | Carrier-phase dTEC |
| `src/hf_timestd/core/physics_fusion_service.py` | dTEC anchoring + science products |
| `src/hf_timestd/core/raytrace_engine.py` | PHaRLAP/pyLAP mode ID (v6.8, verified) |
| `src/hf_timestd/core/propagation_mode_solver.py` | Mode classification + uncertainty |
| `src/hf_timestd/core/chu_fsk_decoder.py` | CHU FSK timecode decoding |
| `src/hf_timestd/grape/grape_daily.py` | GRAPE daily processing + PSWS upload |
| `web-api/main.py` | FastAPI application |
