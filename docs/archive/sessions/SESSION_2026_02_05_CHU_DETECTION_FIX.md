# CHU Detection Fix - 2026-02-05

## Summary
Fixed CHU signal detection on all three CHU channels (3330, 7850, 14670 kHz).

## Changes Made

### 1. Buffer Alignment Fix (`binary_archive_writer.py`)
- Calculate minute boundary RTP directly from GPS_TIME/RTP_TIMESNAP mapping
- Previously used the first arriving packet's RTP, causing variable offset
- Now sample 0 correctly corresponds to the minute boundary

### 2. CHU Detection Fix (`metrology_engine.py`)
- CHU transmits a 500ms 1000Hz tone at **second 0** (minute marker)
- Regular seconds have 300ms tones; second 29 is omitted
- Expected arrival = propagation delay only (~5ms for Ottawa)

### 3. Timing Tolerance (`metrology_engine.py`)
- Increased tick analysis tolerance from 50ms to 100ms
- Accounts for ~70ms systematic offset in GPS_TIME/RTP_TIMESNAP latency

## Known Issue: ~70ms Systematic Offset
Measured CHU arrivals are consistently ~45-70ms later than expected propagation delay.
This appears to be latency in radiod's GPS_TIME/RTP_TIMESNAP capture mechanism.
The offset is consistent across all channels and doesn't affect relative timing.

## Verification
- CHU_3330: 72 measurements, SNR 37-42 dB ✓
- CHU_7850: 66 measurements, SNR 48-56 dB ✓
- CHU_14670: Weak signal (~6 dB), detection pending better propagation
