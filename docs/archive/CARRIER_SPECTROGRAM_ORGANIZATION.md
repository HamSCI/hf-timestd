# Carrier Spectrogram Organization

## Directory Structure

Carrier spectrograms are now organized by **data source** to enable direct comparison and validation:

```
/tmp/grape-test/spectrograms/
└── 20251117/                          # Date
    ├── wide-decimated/                # 16 kHz → 10 Hz decimation path
    │   ├── WWV_2.5_MHz_10Hz_from_16kHz.png
    │   ├── WWV_5_MHz_10Hz_from_16kHz.png
    │   ├── WWV_10_MHz_10Hz_from_16kHz.png
    │   ├── WWV_15_MHz_10Hz_from_16kHz.png
    │   ├── WWV_20_MHz_10Hz_from_16kHz.png
    │   ├── WWV_25_MHz_10Hz_from_16kHz.png
    │   ├── CHU_3.33_MHz_10Hz_from_16kHz.png
    │   ├── CHU_7.85_MHz_10Hz_from_16kHz.png
    │   └── CHU_14.67_MHz_10Hz_from_16kHz.png
    │
    └── native-carrier/                # 200 Hz → 10 Hz decimation path
        ├── WWV_2.5_MHz_carrier_10Hz_from_200Hz.png
        ├── WWV_5_MHz_carrier_10Hz_from_200Hz.png
        ├── WWV_10_MHz_carrier_10Hz_from_200Hz.png
        ├── WWV_15_MHz_carrier_10Hz_from_200Hz.png
        ├── WWV_20_MHz_carrier_10Hz_from_200Hz.png
        ├── WWV_25_MHz_carrier_10Hz_from_200Hz.png
        ├── CHU_3.33_MHz_carrier_10Hz_from_200Hz.png
        ├── CHU_7.85_MHz_carrier_10Hz_from_200Hz.png
        └── CHU_14.67_MHz_carrier_10Hz_from_200Hz.png
```

## Data Processing Paths

### Path 1: Wide-Decimated (Legacy)
**Source**: 16 kHz wide-band IQ channels  
**Processing**: `generate_spectrograms_from_10hz_npz.py`

1. RTP → 16 kHz IQ samples (wide channel)
2. Analytics → Decimation to 10 Hz
3. NPZ → `analytics/decimated/10hz/{channel}/YYYYMMDDTHHMMSSZ_freq_iq_10hz.npz`
4. Spectrogram → `spectrograms/{date}/wide-decimated/{channel}_10Hz_from_16kHz.png`

**Advantages**:
- Full-bandwidth capture (can analyze other features)
- Supports WWV tone detection for GPS timing
- Proven decimation pipeline

**Disadvantages**:
- Potential decimation artifacts
- Larger data files (16 kHz)
- More processing overhead

### Path 2: Native-Carrier (New)
**Source**: 200 Hz native carrier channels  
**Processing**: `generate_spectrograms_from_carrier.py`

1. RTP → 200 Hz IQ samples (carrier channel from radiod)
2. Decimation to 10 Hz (simpler: 200 Hz → 10 Hz vs 16 kHz → 10 Hz)
3. NPZ → `data/{date}/{station}/{doy}/{channel}/YYYYMMDDTHHMMSSZ_freq_iq.npz`
4. Spectrogram → `spectrograms/{date}/native-carrier/{channel}_10Hz_from_200Hz.png`

**Advantages**:
- Radiod's superior filtering at 200 Hz bandwidth
- Minimal decimation artifacts (20:1 vs 1600:1)
- Smaller data files
- Cleaner spectrograms for Doppler analysis

**Disadvantages**:
- No tone detection (200 Hz too narrow for 1000 Hz tone)
- NTP timing only (±10ms vs ±1ms GPS)
- Cannot analyze wide-band features

## Comparison Use Cases

### 1. Quality Validation
Compare spectrograms side-by-side to verify:
- ✅ Similar Doppler patterns (ionospheric effects)
- ⚠️ Differences indicate decimation artifacts or filtering issues

### 2. Artifact Detection
**Wide-decimated** may show:
- Aliasing from imperfect decimation
- Short-term noise spikes
- Processing glitches

**Native-carrier** should show:
- Smoother frequency evolution
- Cleaner spectral lines
- Better SNR for carrier analysis

### 3. Scientific Analysis
For **Doppler studies**, prefer **native-carrier** (cleaner data)  
For **timing studies**, prefer **wide-decimated** (GPS-locked via tone)

## Web UI Display

Carrier Analysis page (`/carrier.html`) now displays both versions grouped by frequency:

```
📻 WWV 5 MHz — 10 Hz Carrier Comparison
┌─────────────────────────────────┬─────────────────────────────────┐
│ 📶 WWV 5 MHz                    │ 📡 WWV 5 MHz carrier            │
│ Wide 16 kHz decimated           │ Native 200 Hz carrier           │
│ Source: 16 kHz → 10 Hz          │ Source: 200 Hz → 10 Hz          │
│ [Spectrogram Image]             │ [Spectrogram Image]             │
│ Timing: GPS_LOCKED              │ Timing: NTP_SYNCED              │
│ Completeness: 97.2%             │ Completeness: 97.8%             │
└─────────────────────────────────┴─────────────────────────────────┘
```

## Generation Commands

### Generate Wide-Decimated Spectrograms
```bash
python3 scripts/generate_spectrograms_from_10hz_npz.py --date 20251117 --data-root /tmp/grape-test
```

### Generate Native-Carrier Spectrograms
```bash
python3 scripts/generate_spectrograms_from_carrier.py --date 20251117 --data-root /tmp/grape-test
```

### Generate Both (Full Comparison)
```bash
# Wide-decimated first
python3 scripts/generate_spectrograms_from_10hz_npz.py --date 20251117 --data-root /tmp/grape-test

# Native-carrier second
python3 scripts/generate_spectrograms_from_carrier.py --date 20251117 --data-root /tmp/grape-test
```

## Expected Differences

### Normal Variations
- **Timing jitter**: Wide uses GPS, carrier uses NTP (±10ms offset acceptable)
- **Amplitude differences**: Different AGC in radiod channels
- **Packet loss patterns**: Independent RTP streams

### Quality Indicators
- **Frequency tracking**: Should match within ±0.1 Hz
- **Spectral purity**: Carrier should be cleaner (less noise)
- **Phase continuity**: Both should show smooth evolution

### Red Flags
- ❌ Frequency differences > 0.5 Hz → Check decimation
- ❌ Different Doppler patterns → Processing error
- ❌ Periodic artifacts in wide → Decimation aliasing
- ❌ Gaps at different times → RTP reception issue

## Migration Notes

**Old paths** (deprecated):
- `spectrograms/20251117/WWV_5_MHz_20251117_carrier_spectrogram.png`
- `spectrograms/20251117/WWV_5_MHz_carrier_20251117_carrier_spectrogram.png`

**New paths** (current):
- `spectrograms/20251117/wide-decimated/WWV_5_MHz_10Hz_from_16kHz.png`
- `spectrograms/20251117/native-carrier/WWV_5_MHz_carrier_10Hz_from_200Hz.png`

Web UI automatically updated to use new paths.
