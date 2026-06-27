# Carrier Frequency Variations - Scientific Interpretation
**Critical Concept:** Frequency variations are the **data**, not noise!

---

## What We're Measuring

### Ionospheric Doppler Shifts

**Source:** WWV/CHU transmit at exactly 2.5, 5, 10, 15, 20, 25 MHz (±10⁻¹¹ accuracy)

**Received frequency ≠ transmitted frequency** due to:

1. **Path length variations** (primary effect)
   - Ionospheric reflection height changes
   - Solar radiation → ionization → virtual height
   - Earth rotation → changing geometry
   - Typical variations: 50-200 km path length change

2. **Doppler shift calculation:**
   ```
   Δf/f = Δr/c
   
   For 10 MHz carrier:
   100 km path change → 3.3 Hz Doppler shift
   ±0.1 Hz variation → ±3 km path sensitivity
   ```

3. **Time scales:**
   - **Diurnal:** Hours (sunrise/sunset ionosphere changes)
   - **Solar activity:** Days to weeks (solar storms)
   - **Seasonal:** Months (sun angle variations)
   - **Short-term:** Minutes (sporadic E, aurora)

---

## Expected Frequency Variations

### Typical Patterns

**WWV 10 MHz nominal carrier: 10.000 Hz (decimated)**

**Observed variations:**
- **Nighttime (stable ionosphere):** 10.000 ± 0.02 Hz
- **Daytime (changing ionosphere):** 10.000 ± 0.10 Hz
- **Sunrise/sunset:** Rapid shifts ±0.2 Hz over 1-2 hours
- **Geomagnetic storm:** ±0.5 Hz or more

**These are REAL scientific signals!**

### What Good Data Looks Like

**Spectrogram appearance:**
- **Smooth frequency drift** over minutes to hours
- **Gradual transitions** (not abrupt jumps)
- **Correlated across frequencies** (same ionosphere affects all channels)
- **Diurnal patterns** (repeatable day-to-day under similar conditions)
- **Clean spectral line** (even if frequency is drifting)

---

## Quality Metrics for Doppler Measurement

### 1. Frequency Resolution

**Required:** ≤0.01 Hz to measure ionospheric variations

**Current system:**
- Sample rate: 10 Hz
- Integration time: Typically 1-10 seconds for FFT
- Frequency resolution: ~0.001 Hz ✅ Excellent

**Verification:**
- Can we see <0.1 Hz variations clearly in spectrogram?
- Are gradual shifts smooth and continuous?

### 2. Measurement Precision

**Goal:** Distinguish real Doppler from measurement noise

**Metrics:**
- **SNR:** Higher SNR → better frequency precision
  - >40 dB: ±0.001 Hz precision
  - 20-40 dB: ±0.01 Hz precision
  - <20 dB: ±0.1 Hz precision (still useful!)
  
- **Spectral purity:** Single clean peak (even if drifting)
- **Temporal stability:** Consistent frequency estimates over short timescales

### 3. Artifact-Free Processing

**Critical:** Don't introduce false frequency variations!

**Decimation must preserve:**
- Smooth frequency evolution (no artificial jumps)
- Phase continuity (smooth phase progression)
- Spectral purity (no spurious components)

**Red flags (artifacts, not real Doppler):**
- Frequency jumps at minute boundaries (file transitions)
- Periodic oscillations at regular intervals (filter ripple)
- Discontinuous phase (sample gaps not properly handled)
- Noise floor variations (processing gain changes)

---

## Spectrogram as Quality Tool

### Visual Inspection Guide

**✅ High Quality (trust this data):**
```
Frequency
10.1 Hz  │         ══════════
         │      ══════
         │   ══════
10.0 Hz  │══════
         │
 9.9 Hz  │
         └──────────────────────> Time
              (smooth drift)
```

**⚠️ Moderate Quality (usable with caution):**
```
Frequency
10.1 Hz  │    ════  ════
         │  ══    ══    ══
10.0 Hz  │══            ══
         │
 9.9 Hz  │
         └──────────────────────> Time
          (some gaps, but continuous)
```

**❌ Poor Quality (artifacts present):**
```
Frequency
10.1 Hz  │  ║  ║  ║  ║  ║
         │  ║  ║  ║  ║  ║
10.0 Hz  │  ║  ║  ║  ║  ║
         │
 9.9 Hz  │
         └──────────────────────> Time
         (discontinuous jumps)
```

### What to Look For

**Good signs:**
- Continuous spectral line (may curve, that's OK!)
- Smooth variations over time
- Similar patterns across frequencies
- Gaps are clearly marked (white space)
- Gradual diurnal patterns

**Bad signs (processing artifacts):**
- Regular jumps at fixed intervals
- Noisy/fuzzy spectral line
- Discontinuities not explained by gaps
- Spurious frequency components
- Inconsistent between similar channels

---

## Quality Assurance for HamSCI Upload

### What Scientists Need

**Primary requirement:** Trust that frequency variations are REAL

**Quality criteria:**
1. **Accurate timing** (TONE_LOCKED or NTP_SYNCED)
2. **Complete data** (minimal gaps, all gaps documented)
3. **Clean decimation** (no processing artifacts)
4. **Known precision** (SNR, spectral resolution documented)
5. **Provenance** (timing quality, processing parameters preserved)

**Scientists will:**
- Compare multiple stations (correlate ionospheric patterns)
- Analyze long-term trends (days, months, years)
- Study specific events (solar storms, eclipses)
- Correlate with other data (magnetometers, ionosondes)

**Our job:** Provide highest quality carrier data with complete metadata

---

## Display Priorities for Carrier Screen

### Primary Purpose

**Answer:** "Can we trust the Doppler variations in this data?"

### Key Indicators

1. **Spectrogram** - Visual confirmation of smooth, artifact-free carrier
2. **SNR** - Measurement precision indicator
3. **Completeness** - Data coverage (gaps clearly marked)
4. **Timing quality** - TONE_LOCKED/NTP_SYNCED/WALL_CLOCK
5. **Upload status** - Data available to HamSCI

### Quality Interpretation

**Green (excellent):**
- Clean spectrogram (smooth variations)
- SNR >40 dB
- Completeness >95%
- TONE_LOCKED timing
- Upload current

**Yellow (good/usable):**
- Minor artifacts or gaps
- SNR 20-40 dB
- Completeness 90-95%
- NTP_SYNCED timing
- Upload delayed <30 min

**Red (problematic):**
- Obvious artifacts or major gaps
- SNR <20 dB
- Completeness <90%
- WALL_CLOCK timing
- Upload stalled

---

## Future Enhancements

### Automated Quality Metrics

**Doppler variation analysis:**
- Extract carrier frequency vs time
- Calculate rate of change (ionospheric dynamics)
- Flag anomalous variations (potential artifacts)
- Cross-correlate channels (expect similar patterns)

**Artifact detection:**
- Discontinuity detection at file boundaries
- Spectral purity analysis (sideband detection)
- Phase continuity verification
- Statistical outlier detection

**Validation:**
- Compare to ionospheric models (IRI, NeQuick)
- Correlate with solar/geomagnetic indices
- Multi-station comparison (HamSCI network)
- Known event verification (solar eclipses, etc.)

---

## Summary

**Key Insight:** Frequency is not a constant - it's the measurement!

**Quality = Sensitivity + Accuracy**
- **Sensitivity:** Can we resolve small Doppler shifts? (precision)
- **Accuracy:** Are variations real or artifacts? (clean processing)

**Spectrogram is the primary QA tool** - scientists can visually verify data quality

**Our goal:** Maximum confidence that uploaded data accurately represents ionospheric Doppler shifts

---

## References

1. Davies, K. (1990). "Ionospheric Radio." Peter Peregrinus Ltd. — Doppler shift to path-length / phase-path-rate relation (Δf/f = Δr/c) and the link between phase-path variation and TEC.
2. Bilitza, D. et al. (2022). "The International Reference Ionosphere model: A review and description of an ionospheric benchmark." *Rev. Geophys.*, 60. (IRI / NeQuick climatology referenced above)
