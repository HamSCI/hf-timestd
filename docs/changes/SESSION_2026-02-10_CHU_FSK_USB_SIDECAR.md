# Session Summary: CHU FSK Decoder — USB Sidecar Channel Architecture
**Date**: 2026-02-10  
**Objective**: Fix the CHU FSK decoder, which was producing zero decoded frames, by diagnosing the root cause and implementing a robust demodulation strategy.

## Problem Statement

The CHU FSK time code decoder had never successfully decoded data in production. CHU transmits 300-baud Bell 103 FSK on seconds 31–39 of each minute, encoding DUT1, TAI-UTC, year, and time-of-day. The FSK tones sit at +2025 Hz (space) and +2225 Hz (mark) above the carrier. Despite the decoder logic being implemented, the soft decision output was pure noise — no frames were ever recovered.

## Root Cause: radiod IQ Decimation Filter Roll-Off

### The Investigation

The CHU channels were configured as `preset=iq` with `sample_rate=24000`, giving a Nyquist of 12 kHz — seemingly plenty of bandwidth for tones at 2.0–2.2 kHz. Several hypotheses were tested and eliminated:

1. **`HIGH_EDGE` command** — Plumbed `low_edge`/`high_edge` parameters through the entire config → StreamRecorderConfig → radiod command chain. Setting `high_edge=2500` had no effect because the IQ preset's filter shape is determined at channel creation, not by subsequent edge commands.

2. **Increasing sample rate to 48 kHz** — Fixed a bug where per-channel `sample_rate` wasn't being read from config (only `channel_defaults` was checked). After the fix, channels were created at 48 kHz, but the filter shape remained narrow.

3. **Source code analysis** — Deep dive into `ka9q-radio` source (`radio.c`, `modes.c`, `radio_status.c`, `presets.conf`, `radiod.conf`) revealed the true mechanism:

### The Filter Roll-Off

The radiod configuration at `/etc/radio/radiod@ac0g-bee1-rx888.conf` contains:

```ini
[global]
mode = usb
samprate = 24000
```

When `preset=iq` is requested via ka9q-python, radiod's `loadpreset()` applies settings in this order:
1. Compiled-in defaults (`low=-5000`, `high=+5000`)
2. `[global]` section — sets `mode=usb` (overrides to `low=+50`, `high=+3000`)
3. `[iq]` preset from `presets.conf` — sets `low=-5000`, `high=+5000`
4. Channel-specific section (none for dynamic channels)

The `[iq]` preset *should* override `[global]`, but the effective filter after decimation has a **steep roll-off that attenuates signals above ~1.5 kHz by 40+ dB**. This was confirmed by FFT analysis of captured IQ data:

- **Carrier (0 Hz baseband)**: dominant peak
- **1000 Hz tick**: visible but attenuated
- **2025/2225 Hz FSK tones**: buried 43 dB below the noise floor

The FSK tones in the raw RF spectrum are already ~32 dB below the carrier. Combined with 43 dB of filter attenuation, they are **75 dB below the carrier** in the IQ output — completely unrecoverable.

### Why USB Works

The `preset=usb` channel performs AM demodulation inside radiod, outputting real audio at 12 kHz. The USB filter passband is +50 to +3000 Hz, which passes the FSK tones at their full strength. FFT of USB audio showed clear peaks at 2025 and 2225 Hz with good SNR.

## Solution: USB Sidecar Channels

Rather than converting the IQ archive channels to USB (which would break the raw IQ archive used for other metrology), we added **three dedicated USB channels** solely for FSK decoding:

### Architecture

```
radiod
  ├── CHU_3330    (preset=iq, sr=24000)  → archive to disk, tone detection, tick timing
  ├── CHU_7850    (preset=iq, sr=24000)  → archive to disk, tone detection, tick timing
  ├── CHU_14670   (preset=iq, sr=24000)  → archive to disk, tone detection, tick timing
  ├── CHU_3330_FSK  (preset=usb, sr=12000)  → FSK decode only, no disk
  ├── CHU_7850_FSK  (preset=usb, sr=12000)  → FSK decode only, no disk
  └── CHU_14670_FSK (preset=usb, sr=12000)  → FSK decode only, no disk
```

The USB sidecar channels:
- Are **not archived** to disk — only the decoded FSK data is kept
- Run in the core recorder process alongside the IQ channels
- Accumulate 60 seconds of audio in a wall-clock-aligned ring buffer
- Decode FSK at each minute boundary + 2 seconds
- Write results to `/dev/shm/timestd/fsk_results/{iq_channel}.json`
- The metrology engine for each IQ channel reads these JSON files

### FSK Demodulation: Hilbert Frequency Discriminator

The previous demodulator (Bell 103 frequency-translate + LPF + quadrature demod) was replaced with a Hilbert transform frequency discriminator proven to achieve **5/5 byte redundancy** on live CHU 7850 kHz USB audio during good propagation:

1. **Bandpass filter** 1875–2375 Hz (6th-order Butterworth, 500 Hz centered on 2125 Hz)
2. **Hilbert transform** → analytic signal
3. **Instantaneous frequency** via phase derivative: `Δθ × sr / 2π`
4. **Normalize**: `(inst_freq - 2125) / 100` → mark = +1, space = -1
5. **Clip** to ±3 (removes phase discontinuity spikes at low-amplitude regions)
6. **Smooth** with moving average (1/4 bit period) to reduce noise

### UART Framing Relaxation

The byte extraction was relaxed to tolerate parity and framing errors in marginal signal conditions. CHU's built-in redundancy (bytes 0–4 repeated as bytes 5–9 in Frame A, or complemented in Frame B) provides the error protection. Multi-second consensus across 9 FSK seconds adds further robustness.

## Files Changed

| File | Change |
|------|--------|
| `/etc/hf-timestd/timestd-config.toml` | Reverted CHU channels to default IQ; added `[recorder.chu_fsk]` section with 3 USB channels |
| `src/hf_timestd/core/chu_fsk_listener.py` | **NEW** — `CHUFSKListener` class: USB channel management, ring buffer, decode thread, JSON result writing |
| `src/hf_timestd/core/chu_fsk_decoder.py` | Replaced Bell 103 demod with Hilbert discriminator; added BPF pre-filter; added clipping/smoothing; relaxed UART framing; added `_fsk_demodulate_iq()` for future IQ-direct path |
| `src/hf_timestd/core/core_recorder_v2.py` | Integrated `CHUFSKListener` init/start/stop; fixed per-channel `sample_rate` override bug |
| `src/hf_timestd/core/metrology_engine.py` | Replaced inline IQ FSK decode with `_read_fsk_result()` reading from shared JSON |
| `src/hf_timestd/core/stream_recorder_v2.py` | Added `low_edge`/`high_edge` plumbing to `StreamRecorderConfig` and `RobustManagedStream` (infrastructure for future use) |

## Verification

- **USB channels created**: All 3 FSK channels start successfully with correct SSRCs
- **Audio streaming**: Callbacks receive real float32 audio at 12 kHz
- **Decode attempts**: Running every minute on all 3 channels
- **5/5 redundancy proven**: Standalone test on CHU 7850 kHz during good propagation achieved perfect decode
- **Nighttime limitation**: At 01:00 UTC, all 3 CHU frequencies were too weak for reliable FSK decode (max 2/5 redundancy on 14670 kHz). This is expected — CHU propagation from Ottawa to EM38 varies with ionospheric conditions.

## Remaining Work

1. **Daytime verification** — 7850 and 14670 kHz should decode reliably during daytime propagation (~13:00–23:00 UTC)
2. **Decoder refinement** — The Hilbert discriminator works but could benefit from:
   - Adaptive threshold based on SNR estimation
   - Better start-bit search using correlation with known Frame A marker (0x06)
   - Edge-effect mitigation when operating on short (1.1s) slices
3. **Timing correlation** — Wire FSK timing offsets from USB channels back to IQ channel metrology for sub-millisecond timing from FSK 500ms boundaries
4. **Clean up** — The `low_edge`/`high_edge` plumbing in `stream_recorder_v2.py` is infrastructure that proved unnecessary for the USB sidecar approach but may be useful for other channels in the future
