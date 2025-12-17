# Screen 2: Carrier Analysis - Quality Assurance Dashboard
**Purpose:** Monitor quality of 10 Hz Digital RF data uploaded to HamSCI/GRAPE  
**Primary Goal:** Reassure quality is good OR alert to problems  
**Update:** Real-time (auto-refresh every 30s) + manual refresh

---

## Design Philosophy

**Question this screen answers:**
> "Is the data we're sending to HamSCI scientifically valid?"

**Quality assurance focus:**
1. **Visual verification** - Spectrograms show clean 10 Hz carrier
2. **Quantitative metrics** - Packet loss, completeness, timing quality
3. **Decimation validation** - 16 kHz → 10 Hz process integrity
4. **Upload verification** - Successful Digital RF writes
5. **Alert system** - Immediate notification of degradation

---

## Screen Layout

```
┌─────────────────────────────────────────────────────────────┐
│ 🎯 Carrier Analysis - Data Quality Dashboard               │
│ Last updated: 2024-11-15 19:31:45                          │
│ [Today] [Yesterday] [Last 7 Days]  [Refresh]               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 📊 System-Wide Quality Summary                          │ │
│ │                                                          │ │
│ │  Overall Status: ✅ GOOD                                │ │
│ │  Channels Operating: 9/9                                │ │
│ │  Average Completeness: 96.8%                            │ │
│ │  Timing Quality: 100% NTP_SYNCED (No TONE_LOCKED)       │ │
│ │  Upload Status: ✅ All current                          │ │
│ │  ⚠️  Alerts: 1 - WWV 20 MHz low SNR (<10 dB)           │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ ┌──────────────┬──────────────┬──────────────┐             │
│ │ WWV 2.5 MHz  │ WWV 5 MHz    │ WWV 10 MHz   │             │
│ │ ┌──────────┐ │ ┌──────────┐ │ ┌──────────┐ │             │
│ │ │[SPEC IMG]│ │ │[SPEC IMG]│ │ │[SPEC IMG]│ │             │
│ │ │  10 Hz   │ │ │  10 Hz   │ │ │  10 Hz   │ │             │
│ │ └──────────┘ │ └──────────┘ │ └──────────┘ │             │
│ │ ✅ 97.2%     │ ✅ 96.5%     │ ✅ 98.1%     │             │
│ │ TONE 2h ago  │ NTP synced   │ TONE 1h ago  │             │
│ │ SNR: 45 dB   │ SNR: 38 dB   │ SNR: 52 dB   │             │
│ ├──────────────┼──────────────┼──────────────┤             │
│ │ WWV 15 MHz   │ WWV 20 MHz   │ WWV 25 MHz   │             │
│ │ [Similar]    │ [Similar]    │ [Similar]    │             │
│ ├──────────────┼──────────────┼──────────────┤             │
│ │ CHU 3.33 MHz │ CHU 7.85 MHz │ CHU 14.67MHz │             │
│ │ [Similar]    │ [Similar]    │ [Similar]    │             │
│ └──────────────┴──────────────┴──────────────┘             │
│                                                             │
│ Click any channel for detailed analysis →                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Key Metrics Per Channel

### 1. Spectrogram Display (Primary Quality Indicator)

**Source:** `/data/grape-test/analytics/{CHANNEL}/spectrograms/{YYYYMMDD}/{YYYYMMDD}_spectrogram.png`

**What it shows:**
- 10 Hz carrier stability over 24 hours
- Frequency drift (should be stable line)
- Phase coherence (clean vs noisy)
- Gaps (white spaces = missing data)
- Artifacts from decimation (ripples, aliasing)

**Visual quality assessment:**
- ✅ **Good:** Clean horizontal line, no gaps, stable frequency
- ⚠️ **Warning:** Minor gaps, slight drift, low SNR periods
- ❌ **Bad:** Major gaps, frequency jumps, severe artifacts

### 2. Completeness Percentage

**Calculation:**
```
completeness = (samples_written / expected_samples) * 100
expected_samples = 960,000 samples/minute * 1440 minutes/day = 1,382,400,000
```

**Color coding:**
- **Green (≥95%):** Excellent - minor gaps acceptable
- **Yellow (90-95%):** Good - some gaps but usable
- **Red (<90%):** Poor - significant data loss

**Source:** Analytics service quality metrics

### 3. Timing Quality Badge

**Display:**
- 🟢 **TONE** - Tone-locked within last 5 min (best)
- 🟡 **NTP** - NTP synchronized (good)
- 🔴 **WALL** - Wall clock (reprocessing recommended)

**Age display:**
- If TONE_LOCKED: Show when established
- If NTP: Show "NTP synced"
- If WALL: Show warning icon

### 4. SNR (Signal-to-Noise Ratio)

**Display:**
- **Current SNR:** From latest quality metric
- **Color coding:**
  - Green: >40 dB (excellent)
  - Yellow: 20-40 dB (good)
  - Orange: 10-20 dB (marginal)
  - Red: <10 dB (poor)

**Alert threshold:** <10 dB = flag for investigation

### 5. Packet Loss Rate

**Display:** Percentage of lost RTP packets
- **Green:** <1% loss
- **Yellow:** 1-5% loss
- **Red:** >5% loss

**Source:** Core recorder status (`packet_loss_pct`)

### 6. Upload Status

**Indicator:**
- ✅ **Current** - Latest data uploaded (lag <10 min)
- ⏳ **Delayed** - Upload lag 10-60 min
- ❌ **Stalled** - No upload >60 min

**Source:** Digital RF last write time

---

## Decimation Quality Validation

**Challenge:** 16 kHz → 10 Hz decimation must preserve signal integrity

### Validation Metrics

**IMPORTANT:** Frequency variations are the **scientific signal** (Doppler shifts from ionospheric path changes), NOT a quality problem!

**1. Measurement Precision**
- **Goal:** Accurately capture Doppler shifts from ionospheric propagation
- **Expected variations:** ±0.1 Hz or more (diurnal/solar effects on path length)
- **Required precision:** ≤0.01 Hz resolution to measure these variations
- **Detection:** FFT spectral resolution and peak estimation accuracy
- **Quality metric:** Can we resolve sub-Hz Doppler shifts reliably?

**2. Spectral Purity (Artifact Detection)**
- **Expected:** Single clean peak (may vary in frequency - that's the data!)
- **Problem artifacts:** Aliasing, filter ripple, spurious sidebands
- **Visual:** Spectrogram should show smooth frequency variations, not jagged/noisy
- **Quality check:** Are frequency variations smooth and continuous (real) or discontinuous/noisy (artifacts)?
- **Alert:** Spurious spectral components suggest decimation issues

**3. Phase Coherence (Continuity Check)**
- **Measurement:** Smoothness of phase evolution over time
- **Expected:** Continuous phase tracking (even as frequency varies)
- **Real variations:** Smooth frequency changes → smooth phase changes
- **Problem artifacts:** Phase jumps/discontinuities indicate sample gaps or processing errors
- **Quality metric:** Phase continuity across file boundaries and decimation blocks
- **Alert:** Abrupt phase discontinuities (not explained by gaps) suggest artifacts

**4. Sample Count Validation**
- **16 kHz rate:** 960,000 samples/minute
- **10 Hz rate:** 600 samples/minute (decimation factor = 1600)
- **Check:** Exact integer relationship maintained
- **Alert:** Sample count mismatch suggests dropped samples

### Decimation Artifacts to Watch For

**Remember:** Frequency variations = ionospheric Doppler = GOOD DATA!

**Real quality problems (artifacts that corrupt Doppler measurements):**

1. **Aliasing** - High frequencies folding into band
   - **Effect:** False frequency variations (not from ionosphere)
   - **Detection:** Spurious spectral components, discontinuous jumps

2. **Filter ripple** - Passband oscillations
   - **Effect:** Artificial frequency modulation
   - **Detection:** Regular periodic variations unrelated to ionosphere

3. **Transition artifacts** - Discontinuities at file/block boundaries
   - **Effect:** False frequency/phase jumps every 60 seconds
   - **Detection:** Regular discontinuities in spectrogram at minute boundaries

4. **Quantization noise** - Excessive rounding errors
   - **Effect:** Reduced precision in measuring small Doppler shifts
   - **Detection:** Noisy frequency estimates, reduced SNR

5. **Phase distortion** - Non-linear phase response
   - **Effect:** False time-varying frequency shifts
   - **Detection:** Frequency variations that don't match expected ionospheric patterns

**Key distinction:**
- ✅ **Smooth frequency drift over minutes/hours** = Real ionospheric Doppler
- ❌ **Abrupt jumps, noise, periodic artifacts** = Decimation problems

**Detection methods:**
- **Visual inspection** - Spectrograms should show smooth, natural-looking variations
- **Cross-channel comparison** - Similar frequencies should show correlated Doppler patterns
- **Time-scale analysis** - Ionospheric changes are gradual (minutes), artifacts are abrupt
- **Automated anomaly detection** (future) - Statistical outlier detection

---

## Alert System

### Alert Types

**🔴 CRITICAL (Red):**
- Completeness <90%
- No upload for >1 hour
- Timing: WALL_CLOCK mode
- SNR <10 dB on multiple channels
- Decimation artifacts detected (aliasing, discontinuities)

**🟡 WARNING (Yellow):**
- Completeness 90-95%
- Upload lag 10-60 min
- Timing: NTP only (no recent TONE)
- SNR 10-20 dB
- Packet loss 1-5%

**🟢 INFO (Green):**
- New TONE_LOCKED established
- Quality improved after issue
- Upload resumed after delay

### Alert Display

**System-wide banner:**
```
⚠️  2 Warnings, 1 Critical Alert
- 🔴 WWV 20 MHz: Low SNR (8 dB) - signal quality degraded
- 🟡 WWV 25 MHz: Upload delayed (15 min lag)
- 🟡 CHU 14.67 MHz: Packet loss 3.2%
```

**Per-channel indicators:**
- Alert icon on affected channel cards
- Color-coded borders
- Tooltip with details

---

## API Endpoints

### 1. Channel Quality Summary
```
GET /api/v1/carrier/quality?date=YYYYMMDD

Response:
{
  "date": "2024-11-15",
  "channels": [
    {
      "name": "WWV 10 MHz",
      "completeness_pct": 98.1,
      "timing_quality": "NTP_SYNCED",
      "time_snap_age_minutes": 98,
      "snr_db": 52.3,
      "packet_loss_pct": 0.8,
      "upload_status": "current",
      "upload_lag_seconds": 120,
      "alerts": [],
      "spectrogram_url": "/spectrograms/WWV_10_MHz/20241115/20241115_spectrogram.png"
    },
    // ... 8 more channels
  ],
  "system_summary": {
    "overall_status": "good",
    "channels_active": 9,
    "average_completeness": 96.8,
    "critical_alerts": 0,
    "warnings": 2
  }
}
```

### 2. Spectrogram Image
```
GET /spectrograms/{channel}/{date}/{filename}

Returns: PNG image (already generated by analytics service)
```

### 3. Decimation Validation
```
GET /api/v1/carrier/decimation-check?channel={name}&date=YYYYMMDD

Response:
{
  "channel": "WWV 10 MHz",
  "date": "2024-11-15",
  "carrier_frequency_hz": 10.0002,  // Should be ~10.0
  "frequency_stability_hz": 0.0015,  // Std dev
  "phase_coherence": 0.98,           // 0-1 scale
  "sample_count_16khz": 1382400000,
  "sample_count_10hz": 864000,
  "decimation_ratio": 1600.0,        // Should be exactly 1600
  "artifacts_detected": false,
  "quality_score": 0.95              // 0-1 overall score
}
```

### 4. Alert Feed
```
GET /api/v1/carrier/alerts?hours=24

Response:
{
  "alerts": [
    {
      "timestamp": "2024-11-15T19:15:00Z",
      "severity": "critical",
      "channel": "WWV 20 MHz",
      "type": "low_snr",
      "message": "SNR dropped below 10 dB (current: 8.2 dB)",
      "auto_clear": false
    },
    // ... more alerts
  ]
}
```

---

## Frontend Behavior

### Auto-Refresh
- **Interval:** 30 seconds (configurable: 10s/30s/60s)
- **Updates:** Quality metrics, upload status, alerts
- **No refresh:** Spectrograms (cached, refresh on date change)

### Date Selection
- **Default:** Today
- **Options:** Today, Yesterday, Last 7 days (carousel view)
- **Custom:** Date picker for historical analysis

### Channel Detail View
**Click channel card → Expanded view:**
- Full-size spectrogram
- Time-series plots (SNR, packet loss, completeness over day)
- Detailed metrics table
- Quality log (issues detected)
- Reprocessing status

### Download/Export
- **Download spectrogram:** PNG file
- **Export metrics:** CSV for analysis
- **Quality report:** PDF summary

---

## Implementation Priority

### Phase 1 (MVP)
- [x] System-wide quality summary
- [x] 9-channel grid with spectrograms
- [x] Basic metrics (completeness, timing, SNR)
- [x] Alert system
- [x] Auto-refresh

### Phase 2 (Enhanced)
- [ ] Decimation validation metrics
- [ ] Channel detail drill-down
- [ ] Time-series plots
- [ ] Historical comparison
- [ ] Export functionality

### Phase 3 (Advanced)
- [ ] Automated anomaly detection
- [ ] Trend analysis
- [ ] Predictive alerts
- [ ] Multi-day comparison views

---

## Data Sources

**Analytics Service Status:**
- `/data/grape-test/analytics/{CHANNEL}/status/analytics-service-status.json`
- Provides: completeness, packet loss, SNR, timing quality, upload status

**Spectrograms:**
- `/data/grape-test/analytics/{CHANNEL}/spectrograms/{YYYYMMDD}/`
- Already generated daily by analytics service

**Core Recorder Status:**
- `/data/grape-test/status/core-recorder-status.json`
- Provides: RTP streaming status, packet counts

**Digital RF Metadata:**
- `/data/grape-test/analytics/{CHANNEL}/digital_rf/`
- Provides: Sample counts, timing metadata, quality annotations

---

## Success Criteria

**This screen succeeds if:**
1. ✅ User can glance and know "everything is good"
2. ✅ Alerts immediately draw attention to problems
3. ✅ Spectrograms provide visual confirmation of quality
4. ✅ Metrics enable root cause diagnosis
5. ✅ Scientists trust data uploaded to HamSCI/GRAPE

**Key insight:** Visual + Quantitative = Confidence in data quality
