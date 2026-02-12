# Project Context: HF Time Standard (hf-timestd)

## 🎯 Next Session Goal: Deploy & Validate Real-Time Ionospheric Propagation Model

Deploy the new v6.7 propagation model to production, wire `IonoDataService` into the metrology lifecycle, and validate that multi-hop arrivals (CHU 7.85 MHz 2F/3F at night) are now accepted instead of rejected.

---

## 📋 What Was Built (v6.7.0, February 12, 2026)

### New Modules

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/propagation_model.py` | `HFPropagationModel` — multi-mode delay prediction, Ne(h) integration, self-consistency |
| `src/hf_timestd/core/iono_data_service.py` | `IonoDataService` — WAM-IPE/GIRO fetch, cache, interpolation, climatological fallback |
| `tests/test_propagation_model.py` | 23 tests, all passing |

### Modified Modules

| File | Change |
|------|--------|
| `arrival_pattern_matrix.py` | Multi-mode arrivals, adaptive uncertainty, `HFPropagationModel` integration |
| `metrology_engine.py` | `_predict_geometric_delay()` uses `HFPropagationModel` as tier-2 fallback |
| `pyproject.toml` | Added `[iono]` optional deps: `netCDF4>=1.6.0`, `boto3>=1.28.0` |

### Architecture

```
MetrologyEngine._predict_geometric_delay()
    ├── ArrivalPatternMatrix.get_expected_arrivals()
    │       └── HFPropagationModel.predict()
    │               ├── IonoDataService.get_iono_params()
    │               │       ├── WAM-IPE grid (NOAA S3/NOMADS)
    │               │       ├── GIRO corrections
    │               │       └── Climatological fallback
    │               ├── _evaluate_mode() × [1F, 2F, 3F, 1E]
    │               └── _estimate_uncertainty()
    ├── HFPropagationModel.predict() (direct fallback)
    └── Vacuum × 1.15 (last resort)
```

---

## 🚀 Deployment Steps

### 1. Install optional dependencies on production

```bash
sudo /opt/hf-timestd/venv/bin/pip install netCDF4>=1.6.0 boto3>=1.28.0
# Or: sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd[iono]
```

### 2. Sync code to production

```bash
sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd
```

### 3. Create iono cache directory

```bash
sudo mkdir -p /var/lib/timestd/iono_cache
sudo chown timestd:timestd /var/lib/timestd/iono_cache
```

### 4. Wire IonoDataService into metrology startup

In `metrology_service.py`, add to the startup sequence:
```python
from hf_timestd.core.iono_data_service import IonoDataService
iono_svc = IonoDataService.get_instance()
iono_svc.start()  # Starts background fetch thread
```

### 5. Restart services

```bash
sudo systemctl restart timestd-metrology.service
sudo systemctl restart timestd-fusion.service
```

### 6. Verify

```bash
# Check IonoDataService started
journalctl -u timestd-metrology --since "5 min ago" | grep -i "iono"

# Check WAM-IPE data fetched
ls -la /var/lib/timestd/iono_cache/

# Check propagation model predictions in logs
journalctl -u timestd-metrology --since "5 min ago" | grep "HFPropagationModel\|propagation_mode\|multi_mode"
```

---

## ✅ Validation Checklist

| Test | How to Verify | Expected |
|------|--------------|----------|
| IonoDataService starts | `journalctl` grep for "IonoDataService" | Background thread running |
| WAM-IPE data fetched | `ls /var/lib/timestd/iono_cache/` | NetCDF files present |
| Climatological fallback works | Model produces predictions even without WAM-IPE | Delays > 0, uncertainty > 0 |
| CHU 3.33 MHz single-hop | Timing errors +0.3 to +20 ms | Validated (same as before) |
| CHU 7.85 MHz multi-hop (night) | Timing errors +110 to +312 ms | **Now validated** (was rejected) |
| Frequency dependence | 5 MHz delay > 10 MHz delay (same station) | 1/f² scaling visible |
| Multi-mode arrivals | `get_all_mode_arrivals()` returns >1 mode | 2F/3F modes present for long paths |
| Adaptive uncertainty | Window narrows with WAM-IPE data | < ±5 ms with good data |
| Self-consistency | Differential delay matches model TEC | RMS < 1 ms |

---

## 📡 The 17 Broadcasts (4 Stations)

| Station | Location | Frequencies (kHz) | Count |
|---------|----------|-------------------|-------|
| **WWV** | Fort Collins, CO (40.68°N, 105.04°W) | 2500, 5000, 10000, 15000, 20000, 25000 | 6 |
| **WWVH** | Kauai, HI (21.99°N, 159.76°W) | 2500, 5000, 10000, 15000 | 4 |
| **CHU** | Ottawa, Canada (45.30°N, 75.75°W) | 3330, 7850, 14670 | 3 |
| **BPM** | Pucheng, China (34.95°N, 109.54°E) | 2500, 5000, 10000, 15000 | 4 |

**Shared frequencies** (require station discrimination): 2500, 5000, 10000, 15000 kHz
**Unique frequencies** (single station): 20000, 25000 (WWV), 3330, 7850, 14670 (CHU)

---

## � Key Files

| File | Purpose |
|------|---------|
| `src/hf_timestd/core/propagation_model.py` | `HFPropagationModel` — multi-mode delay prediction |
| `src/hf_timestd/core/iono_data_service.py` | `IonoDataService` — WAM-IPE/GIRO data service |
| `src/hf_timestd/core/arrival_pattern_matrix.py` | `ArrivalPatternMatrix` — physics validation with multi-mode |
| `src/hf_timestd/core/metrology_engine.py` | DSP, detection, `_predict_geometric_delay()` |
| `src/hf_timestd/core/metrology_service.py` | Service lifecycle — **wire IonoDataService here** |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | Multi-station fusion → Chrony |
| `src/hf_timestd/core/wwv_constants.py` | Station locations, frequencies, physical constants |
| `web-api/` | FastAPI backend — add `/api/propagation/matrix` endpoint |
| `tests/test_propagation_model.py` | 23 tests for new model |

---

## 🏗 Current Timing Architecture

```
radiod (GPS+PPS) → GPS_TIME/RTP_TIMESNAP → BinaryArchiveWriter
                   (uniform across channels)        ↓
                                              MetrologyService
                                                    ↓
                                              MetrologyEngine
                                              ├── ArrivalPatternMatrix
                                              │   └── HFPropagationModel (v6.7)
                                              │       └── IonoDataService
                                              └── L1 Measurements → Fusion → Chrony
```

**RTP Mode**: Trust GPS_TIME, measure tones at known times
**Fusion Mode**: Search for tones, establish timing lock, then measure

---

## 🔍 Quick Commands

```bash
# Check metrology status
sudo systemctl status timestd-metrology

# View metrology logs (10 MHz channel)
tail -f /var/log/hf-timestd/phase2-shared10.log

# Check timing detections
grep "tick analysis" /var/log/hf-timestd/phase2-shared10.log | tail -10

# Reinstall after code changes
sudo /opt/hf-timestd/venv/bin/pip install -e /home/mjh/git/hf-timestd
sudo systemctl restart timestd-metrology

# Run propagation model tests
/home/mjh/git/hf-timestd/venv/bin/python -m pytest tests/test_propagation_model.py -v

# Check between-channel consistency
python3 -c "from ka9q import discover_channels; print(discover_channels('bee1-status.local'))"
```

---

## 📚 Reference Documentation

| Document | Purpose |
|----------|---------|
| `METROLOGY.md` | Propagation model physics, uncertainty analysis |
| `ARCHITECTURE.md` | System design, data flow, service inventory |
| `TECHNICAL_REFERENCE.md` | Algorithms, data formats, release notes |
| `docs/changes/SESSION_2026_02_12_PROPAGATION_MODEL.md` | This session's changes |
| `docs/design/ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md` | Physics-based validation |
| `CRITIC_CONTEXT.md` | Next session critique focus |
