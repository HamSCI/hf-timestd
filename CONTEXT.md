# Project Context: HF Time Standard (hf-timestd)

## 🎯 Next Session Goal: 24-Hour UTC Visualization Dashboard

Build 24-hour UTC graphs showing features from all 17 broadcasts, revealing ionospheric behavior between each transmitter and the receiver.

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

## 📊 Features to Visualize (Per Broadcast, 24-hr UTC)

### 1. Solar Zenith Angle Overlay
- Compute for **midpoint of each propagation path** (not receiver location)
- Shows day/night transition along the path
- Critical for understanding MUF/LUF variations
- Different for each of 17 broadcasts (different paths)

### 2. Carrier Power / Signal Strength
- Already measured: SNR from tone detection
- Need: Absolute or relative carrier power estimate
- Shows: Fadeouts, enhancements, D-layer absorption

### 3. Timing Error (ToA - Expected)
- Already measured: `raw_toa` from tick analysis
- Shows: Ionospheric delay variations, mode changes
- Expected: Diurnal pattern following solar zenith

### 4. Doppler Shift
- Need to implement: Carrier frequency offset measurement
- Shows: Ionospheric motion, TID signatures
- Typical: ±0.1-1 Hz at HF

### 5. Test Signal Detection (WWV/WWVH only)
- WWV: Minute 8 of each hour
- WWVH: Minute 44 of each hour
- Confirms station identity on shared frequencies
- Shows: Which station is receivable at each hour

### 6. TEC (Total Electron Content)
- Derived from: Multi-frequency timing differences
- Formula: TEC ∝ (delay_f1 - delay_f2) / (1/f1² - 1/f2²)
- Requires: Same station on multiple frequencies
- Best candidates: WWV (6 freqs), WWVH (4 freqs)

### 7. Station-Specific Features
- **CHU FSK**: Decode timing from Bell 103 FSK (seconds 31-39)
- **CHU DUT1**: Split-second encoding (seconds 1-16)
- **BPM UT1**: 100ms ticks (seconds 25-29, 55-59)
- **WWV/WWVH BCD**: Time code decoding

---

## 🌍 Propagation Path Midpoints

For solar zenith calculation, use path midpoint (not receiver):

| Broadcast | TX Location | RX (AC0G) | Midpoint (approx) |
|-----------|-------------|-----------|-------------------|
| WWV | 40.68°N, 105.04°W | 38.92°N, 92.13°W | 39.8°N, 98.6°W |
| WWVH | 21.99°N, 159.76°W | 38.92°N, 92.13°W | 30.5°N, 126.0°W |
| CHU | 45.30°N, 75.75°W | 38.92°N, 92.13°W | 42.1°N, 83.9°W |
| BPM | 34.95°N, 109.54°E | 38.92°N, 92.13°W | 36.9°N, 171.3°W |

---

## 📈 Visualization Approach

### Panel Layout (per broadcast)
```
┌─────────────────────────────────────────────────────────┐
│ WWV_10000 - Fort Collins to AC0G (1300 km)              │
├─────────────────────────────────────────────────────────┤
│ [Solar Zenith Angle - shaded day/night]                 │
│ ████████░░░░░░░░░░░░░░░░████████████████████████████   │
├─────────────────────────────────────────────────────────┤
│ [Signal Strength / SNR]                                 │
│ ▁▂▃▅▇█████▇▅▃▂▁▁▁▁▁▁▁▁▁▁▁▂▃▅▇████▇▅▃▂▁                │
├─────────────────────────────────────────────────────────┤
│ [Timing Error (ms)]                                     │
│ ─────────╱╲────────────────────────╱╲─────────         │
├─────────────────────────────────────────────────────────┤
│ [Doppler Shift (Hz)]                                    │
│ ───────╱──────────────────────────╲───────────         │
├─────────────────────────────────────────────────────────┤
│ [Test Signal Detected] (WWV/WWVH only)                  │
│ ● ● ● ● ● ● ● ● ○ ○ ○ ○ ○ ○ ○ ○ ● ● ● ● ● ● ● ●       │
└─────────────────────────────────────────────────────────┘
     00  02  04  06  08  10  12  14  16  18  20  22  24 UTC
```

### Multi-Broadcast Comparison View
- Stack same-frequency broadcasts (WWV vs WWVH vs BPM on 10 MHz)
- Overlay same-station broadcasts (WWV on 5/10/15/20/25 MHz)
- TEC panel derived from multi-frequency timing

---

## 🔧 Implementation Components

### Data Sources
| Data | Source | Status |
|------|--------|--------|
| ToA / timing error | `L1TickAnalysis` | ✅ Available |
| SNR | `ToneDetectionResult` | ✅ Available |
| Carrier power | Need to extract from IQ | ⚠️ To implement |
| Doppler | Need carrier tracking | ⚠️ To implement |
| Test signal | Minute 8/44 detection | ⚠️ To implement |
| TEC | Multi-freq timing fusion | ⚠️ To implement |
| Solar zenith | `astropy` or `pvlib` | ⚠️ To implement |

### Key Files
| File | Purpose |
|------|---------|
| `src/hf_timestd/core/broadcast_specs.py` | 17 broadcast definitions |
| `src/hf_timestd/models/broadcast_measurement.py` | L1/L2/L3 data models |
| `src/hf_timestd/core/metrology_engine.py` | DSP and detection |
| `web-api/` | FastAPI backend for dashboard |

---

## ✅ Completed: Radiod Timing Fix (2026-02-05)

Fixed ~70ms systematic timing offset in radiod GPS_TIME/RTP_TIMESNAP mapping.

**Changes (ka9q-radio branch `fix-gps-rtp-timing-alignment`):**
1. `radio.h`: Added `gps_time_snapshot`, `samples_at_snapshot` to frontend
2. All frontend drivers: Capture atomic GPS time snapshot every second
3. `radio_status.c`: Report uniform (GPS_TIME, RTP_TIMESNAP) for all channels
4. `linear.c`: Initialize RTP from `filter.out.sample_index / decimation`

**Results:**
- GPS_TIME spread across channels: **0.0ms** (all identical)
- Between-channel timing consistency: **<2ms**
- WWV consistently arrives before WWVH ✅

---

## � Current Timing Architecture

```
radiod (GPS+PPS) → GPS_TIME/RTP_TIMESNAP → BinaryArchiveWriter
                   (uniform across channels)        ↓
                                              MetrologyService
                                                    ↓
                                              L1 Measurements
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

# Check between-channel consistency
python3 -c "from ka9q import discover_channels; print(discover_channels('bee1-status.local'))"
```

---

## 📚 Reference Documentation

| Document | Purpose |
|----------|---------|
| `src/hf_timestd/core/broadcast_specs.py` | 17 broadcast definitions |
| `docs/design/TIMING_AUTHORITY_ARCHITECTURE.md` | RTP vs Fusion modes |
| `docs/design/ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md` | Physics-based validation |
| `docs/changes/SESSION_2026_02_05_TIMING_FIX.md` | Timing fix details |
| `GPS_TIME_TIMING_FIX.md` (ka9q-radio) | Radiod patch documentation |
