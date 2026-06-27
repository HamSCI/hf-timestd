# HamSCI 2026 Workshop — Presentation Plan

**Title:** Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation
**Author:** Michael James Hauan (AC0G)
**Duration:** 15 minutes
**Date:** Prepared 2026-03-07

---

## Narrative Arc (revised 2026-03-06)

**Central question:** *With an RX888 and a GPSDO, what kind of ionospheric science can we do?*

**Central thesis:** A GPSDO-locked SDR listening to HF time standard stations is a precision ionospheric instrument.  The GPSDO provides the frequency stability that unlocks carrier-phase observables (Doppler, dTEC/dt, scintillation); recovering UTC from the time signals themselves adds absolute propagation delay (D_clock), mode identification, and absolute TEC.

**Structure — three acts:**

1. **The hardware question** (Slides 1–2): What timing infrastructure do you have? Four tiers from bare crystal to GPS+PPS. Each tier unlocks a different class of ionospheric observable. This is the organizing framework.

2. **The metrology** (Slides 3–7): How we recover UTC from HF time signals when we already have a GPSDO. The GPSDO gives us the sample clock; the time signals give us absolute time-of-day via tick detection → multi-station fusion → Chrony feed. Includes visual evidence (Kalman fusion plot, Allan deviation) and shared-channel discrimination. This is the bridge from Tier 2 (GPSDO-only, rate measurements) to Tier 3 (D_clock, absolute delay).

3. **The science payoff** (Slides 8–14): What comes into view once you have both frequency stability and time recovery. Demonstrated products from live data: 17 simultaneous paths, carrier-phase dTEC (4 slides), differential dTEC self-consistency, and the spectrogram-to-TEC physics cascade.

**The four hardware tiers:**

| Tier | Hardware | Absolute time | Sample clock | What it unlocks |
|------|----------|--------------|-------------|----------------|
| 1 | RX888 alone | NTP (±10–50 ms) | Crystal (~20 ppm drift) | Station detection, coarse propagation |
| 2 | RX888 + GPSDO | NTP (±10–50 ms) | GPSDO (1 ppb) | **Doppler, dTEC/dt, scintillation** — the rate domain |
| 3 | RX888 + GPSDO + GPS+PPS on LAN | Chrony+PPS (±0.5–1 ms) | GPSDO (1 ppb) | **D_clock, mode ID, propagation geometry** — adds absolute delay |
| 4 | RX888 + GPSDO + PPS in HF stream | GPS_TIME in RTP (±1 µs) | GPSDO (1 ppb) | **Sub-ms multipath, group-delay TEC** — full timing precision |

**Key insight for the audience:** Tier 2 adds a Leo Bodnar GPSDO (~$162). It unlocks carrier-phase Doppler and dTEC/dt — arguably the most scientifically valuable observables. You don't need PPS or GPS\_TIME integration for the rate domain. The absolute-time domain (D_clock, mode identification) requires Tier 3+, but this system can *bootstrap its own absolute time* from the HF signals themselves, lifting a Tier 2 station to effective Tier 3.

**The punchline:** This station operates at Tier 4 (GPS_TIME in RTP chain), which lets us validate the full measurement chain. But most of the science products we demonstrate — Doppler, dTEC, discrimination — are available to any Tier 2 station. The UTC recovery we demonstrate is the metrology that bridges Tier 2 → Tier 3, making absolute-delay products accessible without external PPS.

---

## Evidence Audit (2026-03-06 production data)

### Metrological Ladder — Live Chrony Comparison

| Tier | Timing Source | Offset vs GPS | Bound (±) | Factor vs GPS |
|------|-------------|---------------|-----------|---------------|
| 0 | Unsynchronized PC clock | ~50–100 ms | — | ~50,000× |
| 1 | Internet NTP (time-e-b.nist.gov) | +0.6 ms | ±4.2 ms | ~600× |
| 2 | LAN NTP (router, stratum 2) | +1.2 ms | ±1.0 ms | ~1,200× |
| 3 | **GPS+PPS** (192.168.0.203, stratum 1) | **+0.006 ms** | **±0.039 ms** | **1× (reference)** |
| 4 | **TSL1** — HF L1 geometric | +1.1 ms | ±0.6 ms | ~1,100× |
| 5 | **TSL2** — HF L2 ionospheric | −0.055 ms | ±0.5 ms | ~55× |

**Key finding:** TSL2 (ionospherically corrected) is now **−55 µs vs GPS** — nearly indistinguishable from the GPS reference at the Chrony reporting resolution. TSL1→TSL2 improvement validates that the ionospheric correction is doing real work. Both are far better than internet NTP.

**Fusion D_clock statistics (full day, 2026-03-06):**

- 4,843 valid measurements across 1,437 minutes (~3.4 per minute)
- Median offset: −0.10 ms
- MAD: 0.26 ms
- 96% within ±1 ms of GPS
- 100% within ±2 ms of GPS

### Tick Detection Performance

| Channel | Records/day | Stations | Median D_clock | Uncertainty | Median Edges | Doppler Coverage |
|---------|------------|----------|---------------|-------------|-------------|-----------------|
| CHU 3.33 | 1,379 | CHU | +2.58 ms | ±0.80 ms | 44/58 | 100.0% |
| CHU 7.85 | 1,406 | CHU | +0.79 ms | ±0.12 ms | 53/58 | 99.9% |
| CHU 14.67 | 1,393 | CHU | +2.33 ms | ±0.15 ms | 50/58 | 100.0% |
| SHARED 2.5 | 4,228 | WWV+WWVH+BPM | −0.07 ms | ±2.1 ms | 57/57 | 100.0% |
| SHARED 5.0 | 4,231 | WWV+WWVH+BPM | −0.25 ms | ±2.1 ms | 57/57 | 100.0% |
| SHARED 10.0 | 4,228 | WWV+WWVH+BPM | −0.53 ms | ±2.1 ms | 57/57 | 100.0% |
| SHARED 15.0 | 4,228 | WWV+WWVH+BPM | +0.01 ms | ±2.0 ms | 57/57 | 100.0% |
| WWV 20.0 | 1,410 | WWV | −0.72 ms | ±2.0 ms | 57/57 | 99.9% |
| WWV 25.0 | 1,407 | WWV | −0.27 ms | ±2.0 ms | 57/57 | 100.0% |

*Uncertainty: MAD-based ensemble σ/√N. CHU's 300 ms tick template provides ~18 dB more processing gain than WWV's 5 ms template, yielding tighter ensemble uncertainty. Signal-level SNR (with processing gain removed) is comparable across all channels — see fig8 bar chart.*

**Total: ~23,910 tick timing records/day across 9 channels, 17 broadcasts**

### Physics Products

| Product | Records/day | Status | Key Metric |
|---------|------------|--------|------------|
| Carrier-phase dTEC rate | 19,571 | ✅ | median ≈ 0 TECU/s, σ = 0.49 mTECU/s |
| Per-tick dTEC time series | 933,243 | ✅ | ~55 records/min/station, 1-second resolution |
| Differential dTEC | 29,312 | ✅ | RMS 0.017 TECU (98.6% GOOD quality) |
| All-arrivals (multipath) | 127,802 (CHU 7.85 alone) | ✅ | Multiple modes resolved per minute |
| Doppler shifts | 100% coverage | ✅ | CHU 7.85: ±0.38 Hz range, σ = 0.08 Hz |
| Per-tick carrier phase | 1,154,467 (all channels) | ✅ | tick-to-tick σ_φ ≈ 1.0 rad (ionospheric) |
| Fusion UTC estimate | 4,843/day | ✅ | Median offset −0.10 ms vs GPS |

### Demonstrated Claims (backed by production data, 2026-03-06)

1. **Sub-100 µs UTC recovery from HF** — TSL2 at −55 µs ± 500 µs vs GPS ground truth
2. **Far better than internet NTP** — HF-derived time at −55 µs vs NTP at +600 µs ± 4.2 ms offset
3. **L2 ionospheric correction improves L1** — TSL2 20× closer to GPS than TSL1 (+1.1 ms → −0.055 ms)
4. **44–57 ticks/min ensemble** — verified across all 9 channels, SNR-weighted robust median
5. **~1.15M per-tick carrier-phase records/day** — 1-second time resolution across 17 paths
6. **~20K per-minute dTEC records/day** — primary science product, carrier-phase derived
7. **Differential dTEC RMS 0.017 TECU** — 29K cross-frequency consistency checks/day, 98.6% GOOD quality
8. **Doppler extraction at 100% coverage** — diurnal signatures clearly resolved, 24/7
9. **Multipath mode identification** — 128K all-arrivals records/day on CHU 7.85 alone; multiple modes resolved per minute
10. **17 simultaneous sounding paths** — 4 stations × multiple frequencies, passive oblique ionosonde
11. **Shared-channel station discrimination** — 7 independent methods separate WWV, WWVH, and BPM on 2.5/5/10/15 MHz
12. **Metrological ladder** — live chronyc comparison: NTP → HF L1 → HF L2 → GPS+PPS
13. **GNSS-anchored dTEC** — local ZED-F9P VTEC provides absolute scale for carrier-phase dTEC (301 anchored records on 3/6)
14. **GRAPE-compatible data products** — standard spectrograms and decimated IQ for PSWS upload
15. **24/7 autonomous operation** — 6 systemd services, all active, continuous since January 2026

### Potential Claims Within Reach (infrastructure exists, validation pending)

1. **Scintillation monitoring** — dual-source S4 (test signal multi-tone) + σ\_φ (per-tick carrier phase) infrastructure is wired and producing data; awaiting a geomagnetic storm for real event validation (Feb 11–24 all quiet)
2. **TID detection** — Doppler signatures of TIDs should be visible in the dTEC time series; algorithm exists but no validated TID event in the current data window
3. **Sporadic-E detection** — anomalous propagation on higher frequencies would produce distinctive D\_clock and Doppler signatures; detection algorithm exists but no event observed
4. **CHU FSK time code decode** — previously demonstrated (8/9 frames decoded, confidence=1.00 on CHU 14670); write path broken by later refactoring, fixable
5. **VTEC maps from HF** — group-delay TEC has SNR ~0.13 (signal buried in propagation model noise); geometrically correct but physically unreliable without better per-path sTEC or GNSS priors
6. **Phase-array angle-of-arrival** — multiple GPSDO-locked RX888s at one site would enable direction-of-arrival separation of multipath modes
7. **Resolved multipath ionogram** — the current matched-filter correlator uses 300 ms (CHU) / 800 ms (WWV) tone templates with 100 Hz bandpass, giving ~10 ms time resolution. Multi-hop 1F→2F separation is only 3–6 ms for our geometry, below the mainlobe width. GPSDO provides µs-level absolute sample timing, so the limit is the *ambiguity function* of the matched filter, not the clock. Three paths forward: (a) carrier-domain analysis on unique channels (CHU, WWV 20/25) where wider bandpass is possible without cross-station contamination, (b) PhaRLAP ray-tracing integration for precise multi-hop delay predictions enabling constrained model fitting even within the mainlobe, (c) phased-array angle-of-arrival to separate modes geometrically rather than temporally

---

## Deep-Dive: VTEC Situation

### What the data actually shows (2026-03-06)

The CRITIC\_CONTEXT claim that "vtec_tecu is all NaN" is outdated. The TEC estimator
produced 1,995 records on 3/6 (down from 1,772 on 2/23 — reflects tighter gating).
The anchor status breakdown for the 19,571 dTEC records on 3/6:

| Anchor Status | Records | % |
|---------------|---------|---|
| ANCHORED\_GNSS | 301 | 1.5% |
| ANCHORED\_GROUP\_DELAY | 2,000 | 10.2% |
| NO\_ANCHOR | 17,270 | 88.2% |

GNSS anchoring is intermittent — the ZED-F9P VTEC writer (`live_vtec.py`) is running
but the VTEC file path changed and some days have no GNSS VTEC data available to the
dTEC pipeline. Group-delay anchoring provides fallback.

### The hard limit: group-delay TEC SNR

The TEC estimator (tec_estimator.py) fits `D_clock(f) = slope/f² + intercept`.
The physics is correct. The problem is signal-to-noise:

- **Signal:** Ionospheric dispersion between 2.5–25 MHz for 20 TECU ≈ **0.85 ms**
- **Noise:** D_clock uncertainty from propagation model errors ≈ **6.5 ms**
- **SNR ≈ 0.13** — the 1/f² signal is buried in noise

With N=2 frequencies, confidence is capped at 0.3 (MAX\_CONFIDENCE_N2). With N≥3,
the R² of the fit is typically low. The VTEC values that exist are **geometrically
correct but physically unreliable** — the thin-shell mapping, IPP computation, and
2D polynomial surface fit are all sound, but the input sTEC is noise-dominated.

### IONEX files: working fine

The ionex/ directory has 3,246 files, actively written every ~3 minutes. These are
**output** IONEX files from our VTEC mapper. External GPS IONEX download/parsing
also works (ionospheric_model.py:442).

### Role of local GNSS VTEC (ZED-F9P)

Two distinct roles:

**Role A — Anchor carrier-phase dTEC (IMPLEMENTED 2026-02-24):**
The carrier-phase dTEC product is now **anchored** to GNSS overhead VTEC. The
`_read_gnss_vtec()` method in `physics_fusion_service.py` reads the nearest VTEC
measurement (±120s window) from the HDF5 files written by `live_vtec.py`, and
applies it as the DC level for all station-channels. Priority cascade:

1. GNSS overhead VTEC (~1 TECU accuracy) → `anchor_status=ANCHORED_GNSS`
2. Group-delay TEC (SNR 0.13, rarely usable) → `anchor_status=ANCHORED_GROUP_DELAY`
3. No anchor → `anchor_status=NO_ANCHOR`

Validated: 301 dTEC records with `ANCHORED_GNSS` on 3/6 (1.5% of daily output; intermittent due to VTEC file path issue).
Schemas updated: `l3_dtec_v1.json` and `l3_dtec_timeseries_v1.json` → v1.1.0.

**Role B — Improve VTEC maps (MODERATE VALUE, HARDER):**
GNSS VTEC gives vertical TEC overhead at one point. For VTEC maps you need
spatially distributed measurements. Options:

1. Use GNSS VTEC as a Bayesian prior in the 1/f² fit → better per-path sTEC
2. Use GNSS VTEC as map background, overlay HF-derived dTEC perturbations
   (most scientifically interesting approach)

**The fundamental limit for VTEC maps from HF alone:** Each IPP's TEC value is
noise-dominated (SNR 0.13). A single GNSS receiver provides one high-quality
point but no spatial distribution. To make credible VTEC maps you need either
much better per-path sTEC or multiple GNSS receivers (which is what IGS does).

### Presentation framing

- **Drop:** "VTEC maps" as a current capability
- **Keep:** "Group-delay TEC has SNR ~0.13; carrier-phase dTEC bypasses this"
- **Add as implemented:** "GNSS-anchored dTEC — local ZED-F9P provides absolute scale (deployed 2026-02-24)"

---

## Deep-Dive: Scintillation

### Two independent implementations exist

**Implementation A — WWV/WWVH Test Signal S4 (wwv\_test_signal.py:1516):**
Extracts per-frequency power from the multi-tone segment (seconds 13–23).
Computes S4 at 2, 3, 4, 5 kHz audio tones plus frequency slope for D/F-layer
discrimination. HDF5 files exist (46 records/day).

**Bug found and fixed (2026-02-24):** The SNR gate assumed -3 dB/sec designed
attenuation survived ionospheric propagation. It doesn't — fading (~13 dB std)
completely masks the 27 dB designed range. The gate formula
`first3_mean - last3_mean - expected_drop` always yielded ~-24 dB, rejecting
all tones. **Fix:** measure noise floor from off-tone FFT bins; use data-driven
linear detrending instead of the -3 dB/sec model. `fading_variance` was already
populated (mean ~70–89 dB²); S4 will now populate after deployment.

**Implementation B — Per-Tick Phase Scintillation (tick\_phase + phase_service):**
Computes σ\_φ from tick\_phase HDF5 data using sliding windows with Doppler
detrending. Produces 1,938 σ_φ records per 2-hour window across all channels.
190K+ per-tick carrier phase records/day.

**ScintillationService rewritten (2026-02-24):** Now reads from both `test_signal`
(for S4) and `tick_phase` (for σ\_φ). Also computes tick-based amplitude S4 from
correlation_peak and provides cross-source validation. Fixed `minute_boundary_utc`
format mismatch (integer epoch, not ISO string) — led to adding schema linkage
to DataProductRegistry and structural field documentation in the data dictionary.

### What the measurements mean at HF

Tick-to-tick σ_φ ≈ 1.0 rad across all channels. This is **not** scintillation
in the GNSS sense. At HF (3–25 MHz), the ionospheric Fresnel zone is ~100 km,
so amplitude scintillation from small-scale irregularities is rare. The 1.0 rad
phase noise is dominated by:

1. Multipath interference (multiple modes beating)
2. Measurement noise (~0.5 rad floor at 7.9 dB SNR)
3. Residual ionospheric bulk motion after detrending

True HF scintillation (equatorial spread-F, polar irregularities during storms)
would show correlated amplitude AND phase fluctuations with specific spectral
characteristics. We're not currently separating these from the background.

### Validation path — status as of 2026-02-24

1. ~~Fix test signal S4 SNR gate~~ — **DONE** (off-tone noise floor + data-driven detrend)
2. ~~Wire ScintillationService to tick\_phase + test_signal~~ — **DONE** (full rewrite)
3. ~~Add schema linkage to DataProductRegistry~~ — **DONE** (get\_schema/get\_field_type)
4. **Wait for geomagnetic storm** — checked Feb 11–24 data: all quiet (σ(dTEC/dt) ≈ 0.0004 TECU/s every day, fading variance stable ~80–89 dB²). X8.1 flare from AR4366 was Feb 5, before our tick_phase data started.
5. **Cross-validate S4 vs σ_φ** — infrastructure ready; needs a real event to test

### Presentation framing

- **Brief mention:** "Dual-source scintillation infrastructure: S4 from test signal multi-tone, σ_φ from per-tick carrier phase. Groundwork is laid; validation awaits a geomagnetic event."
- **Honest:** "σ\_φ ≈ 1.0 rad during quiet conditions is multipath + noise, not ionospheric scintillation. A storm would show correlated S4 + σ_φ spikes."

---

## Deep-Dive: CHU FSK

Previously debugged and working (8/9 frames decoded, confidence=1.00 on CHU 14670).
Four bugs were fixed: quadrature demodulator, ring buffer timing, log crash,
minute boundary calculation. The fixes remain in the code.

No recent HDF5 output — the write path was likely broken by later refactoring
(TickEdgeDetector consolidation or metrology_service changes). The `chu_fsk/`
directories exist but contain no files for recent dates.

**For the presentation:** Not a 15-minute talk item. Mention as a demonstrated
capability if asked, but don't feature it.

---

## Deep-Dive: Shared-Channel Station Discrimination

### The discrimination challenge

On the four shared frequencies (2.5, 5, 10, 15 MHz), three stations transmit simultaneously:

| Station | Location | Distance | Vacuum delay | Tick freq | Tick duration |
|---------|----------|----------|-------------|-----------|---------------|
| WWV | Fort Collins, CO | 1,119 km | 3.7 ms | 1000 Hz | 5 ms (800 ms tone) |
| WWVH | Kauai, HI | 6,599 km | 22.0 ms | 1200 Hz | 5 ms (800 ms tone) |
| BPM | Xi'an, China | 11,564 km | 38.6 ms | 1000 Hz | 10 ms (300 ms marker) |

The signals arrive superimposed at the receiver. Without discrimination, timing is ambiguous and all derived products (D_clock, Doppler, dTEC) are contaminated.

### Known contamination mechanisms

**1. Harmonic intermodulation (receiver nonlinearity):**
The NIST 500/600 Hz audio tones generate harmonics in the receiver chain:
- 500 Hz × 2 = 1000 Hz — contaminates WWV tick detection
- 600 Hz × 2 = 1200 Hz — contaminates WWVH tick detection
- 440 Hz × 3 = 1320 Hz — near WWVH 1200 Hz band

**Mitigation:** IIR notch filters at 440, 500, and 600 Hz (Q=20) applied before tick correlation. The notch filters remove the fundamentals, eliminating harmonic contamination of the tick-frequency bands. The harmonic ratios (P_1000/P_500, P_1200/P_600) are also measured and used as a confirmatory vote — when harmonics are strong, that itself indicates which station's tone is present.

**2. BPM/WWV 1000 Hz overlap:**
Both WWV and BPM use 1000 Hz ticks. They cannot be separated by tone frequency alone. Three features discriminate them:
- **Tick duration:** WWV = 5 ms (within 800 ms tone), BPM = 10 ms (within 300 ms marker). The matched-filter correlator uses separate templates: 0.8s template for WWV, 0.3s template for BPM.
- **Propagation delay:** WWV arrives at ~4–8 ms after the minute boundary; BPM arrives at ~40–55 ms. The temporal windows are well-separated (>30 ms gap).
- **BPM UT1 minutes (25–29, 55–59):** BPM transmits 100 ms pulses — 10× longer than WWV's 5 ms ticks. These are unambiguous BPM markers and provide definitive path calibration.

**3. WWV/BPM timing overlap on tick edges:**
Within a single second, both WWV and BPM produce a 1000 Hz tick. Their arrival times differ by ~35–50 ms (depending on ionospheric conditions). The matched filter correlation function can, in principle, have overlapping sidelobes. In practice, the 10 ms mainlobe width and 35+ ms separation keep them resolved. During anomalous propagation (e.g., long-delayed WWV multipath), the arrivals could approach each other — this is flagged when timing falls outside the learned delay model window.

**4. WWV/WWVH BCD time code crosstalk:**
Both stations broadcast BCD amplitude modulation at 100 Hz. The BCD codes encode identical UTC time but with slightly different timing. The cross-correlation method separates them by fitting two time-shifted templates simultaneously, using the learned propagation delay difference to constrain the fit. When both are strong, the BCD correlation shows two peaks separated by the differential propagation delay.

### How we actually measure each station (parallel direct measurement)

The current architecture does **not** decide which station is present — it **measures all three in parallel** using station-specific matched-filter templates. On each shared channel, three independent measurement pipelines run every minute:

| Pipeline | Template | Tone freq | Tick duration | Expected arrival window |
|----------|----------|-----------|---------------|------------------------|
| WWV | 0.8s tone, 5ms tick | 1000 Hz | 5 ms | ~4–8 ms (1,119 km) |
| WWVH | 0.8s tone, 5ms tick | 1200 Hz | 5 ms | ~22–30 ms (6,599 km) |
| BPM | 0.3s marker, 10ms tick | 1000 Hz | 10 ms | ~40–55 ms (11,564 km) |

Each pipeline produces its own D_clock, Doppler, SNR, and confidence — independently and simultaneously. The physical separability is inherent:

- **WWV vs WWVH:** Separated by tick frequency (1000 Hz vs 1200 Hz). Different matched-filter templates; they don't compete.
- **WWV vs BPM:** Both 1000 Hz, but separated by tick duration (5 ms vs 10 ms template), marker duration (800 ms vs 300 ms), and arrival time (~35+ ms gap from propagation geometry).
- **All three:** Each pipeline searches within an expected arrival window anchored to the station's GPSDO-derived propagation delay model. Once the delay model is calibrated, geography is the discriminator.

**Calibration from ground truth minutes (14 per hour):** During exclusive broadcast minutes, only one station transmits its audio tone:
- WWV-only: minutes 1, 16, 17, 19
- WWVH-only: minutes 2, 43, 44, 45, 46, 47, 48, 49, 50, 51

These provide unambiguous calibration of the per-station delay models. The `TimingDiscriminator` uses these to bootstrap → validate → refine the expected arrival windows for each station-frequency pair.

**Additional broadcast features exploited for validation (not voting):**
- 440 Hz tone: definitive station ID in minutes 1 (WWVH) and 2 (WWV)
- Test signal: minutes 8 (WWV) and 44 (WWVH) — multi-tone + chirp for channel characterization
- BPM UT1 minutes (25–29, 55–59): 100 ms pulses, 10× longer than WWV ticks — unambiguous BPM identification
- BCD amplitude ratio: 100 Hz cross-correlation shows two peaks separated by differential propagation delay
- Harmonic power ratios (P_1000/P_500, P_1200/P_600): confirmatory when 500/600 Hz tones present

**Note on legacy code:** The `wwvh_discrimination.py` module contains a "weighted voting" system with 8+ methods that was designed before the TickEdgeDetector existed. The voting logic (`finalize_discrimination()`, `compute_discrimination()`) is **no longer called at runtime**. The active code path uses only three services from that module: `detect_bcd_discrimination()` for BCD amplitude extraction, `estimate_doppler_shift_from_ticks()` for legacy Doppler, and the `WWVTestSignalDetector` sub-object for minutes 8/44. The broadcast schedule constants and contamination knowledge embedded in that module remain valuable reference material.

### Production discrimination evidence (2026-03-06)

**D_clock separation across all 4 shared frequencies (paired by minute):**

| Freq | WWV−WWVH | WWV−BPM | WWVH−BPM |
|------|---------|---------|----------|
| 2.5 MHz | −0.34 ms (MAD 3.51) | −2.48 ms (MAD 4.03) | −1.71 ms (MAD 3.91) |
| 5.0 MHz | −2.16 ms (MAD 3.86) | −3.96 ms (MAD 4.71) | −1.52 ms (MAD 4.25) |
| 10.0 MHz | −0.44 ms (MAD 3.57) | −3.32 ms (MAD 4.34) | −3.00 ms (MAD 4.14) |
| 15.0 MHz | −0.32 ms (MAD 3.30) | −1.01 ms (MAD 3.12) | −0.65 ms (MAD 3.30) |

**Interpretation:** The WWV−BPM separation (1.0–4.0 ms median) is the most reliable discriminant — consistent negative sign across all frequencies, as expected from the 10,445 km path length difference. The WWV−WWVH separation is smaller (0.3–2.2 ms) because the ionospheric delays partially compensate the geometric path difference (WWVH's longer path has higher MUF and often fewer hops). The MAD values (3.1–4.7 ms) are comparable to the separations, which is why the parallel measurement architecture uses station-specific templates (different tick frequencies and durations) rather than relying on D_clock separation alone. The 14 ground-truth minutes per hour continuously recalibrate the per-station delay models.

**Cross-station Doppler correlation (r values):**

| Freq | WWV−WWVH | WWV−BPM | WWVH−BPM |
|------|---------|---------|----------|
| 2.5 MHz | +0.036 | −0.025 | −0.026 |
| 5.0 MHz | +0.018 | +0.128 | +0.071 |
| 10.0 MHz | +0.090 | +0.074 | −0.047 |
| 15.0 MHz | +0.182 | +0.164 | −0.037 |

All cross-station correlations are near zero (|r| < 0.2). This is the strongest physical validation: if the discrimination were producing artifacts — e.g., assigning multipath components of the same station to different labels — the Doppler would be correlated. Independent paths through different ionospheric volumes produce uncorrelated Doppler, which is exactly what we observe.

**Contrast with same-station cross-frequency Doppler:** Within a single station (e.g., WWV across 2.5/5/10/15 MHz), cross-frequency Doppler correlation is also low (r < 0.1) because different frequencies reflect at different ionospheric heights. This is expected: the ionospheric Doppler at each frequency reflects a different layer. The higher correlation reported for CHU (r ≈ 0.43 at 3.33/7.85/14.67) reflects CHU's shorter path (1,522 km, single-hop geometry) where all frequencies see similar ionospheric dynamics.

### What the audience should take away

1. **The system measures, it doesn't decide.** Three station-specific matched-filter templates run in parallel on every shared channel. WWV and WWVH separate by tick frequency (1000 vs 1200 Hz). BPM separates by tick duration (10 vs 5 ms) and arrival time (~35 ms later than WWV). No voting algorithm is needed — the physics does the work.

2. **Known contamination is explicitly handled.** Harmonic intermodulation from 500/600 Hz tones is removed with notch filters before tick detection. BPM/WWV overlap is resolved by tick duration templates and temporal windowing. BCD crosstalk is handled by dual-template fitting.

3. **Geography is the ultimate discriminator.** Once the GPSDO-anchored delay models are calibrated (14 ground-truth minutes per hour from NIST's exclusive tone schedule), each station's expected arrival window is precise enough that geography alone resolves them. The D_clock ordering (WWV < WWVH < BPM) is consistent across all four shared frequencies, matching path geometry.

4. **BPM is the most challenging station.** It shares 1000 Hz with WWV, has higher D_clock variance from its multi-hop transoceanic path, and its timing schedule is less well-documented than NIST's. The 100 ms UT1 pulses (minutes 25–29, 55–59) provide unambiguous BPM identification and calibrate the delay model.

---

## Presentation Outline (16 slides, 15 minutes) — revised 2026-03-06

---

### ACT 1: THE HARDWARE QUESTION

---

### Slide 1: Title (30 sec)
**"With an RX888 and a GPSDO, What Kind of Ionospheric Science Can We Do?"**

- AC0G, EM38, central Missouri
- 4 stations (WWV, WWVH, CHU, BPM), 9 frequencies, 17 simultaneous paths
- Single RX888 SDR + GPSDO via KA9Q-radio

Speaker notes: "This talk is organized around one question: if you have an RX888 and a GPSDO listening to time standard stations, what ionospheric science can you actually extract? The answer depends on your timing infrastructure, and it turns out the science payoff is much larger than you might expect."

### Slide 2: The Phenomena Ladder — What Each Dollar Buys (2 min)
**"Each level of timing infrastructure unlocks a new class of observable."**

Show fig15\_phenomena_ladder — the four-tier version:

| Tier | Hardware (~cost) | What it unlocks |
|------|-----------------|----------------|
| 1 | RX888 alone (~$180) | Station detection, coarse propagation mode |
| 2 | + GPSDO (~$340 total) | **Doppler, dTEC/dt, scintillation** (the rate domain) |
| 3 | + GPS+PPS on LAN (~$450 total) | **D_clock, mode ID, absolute delay** |
| 4 | + PPS in HF stream (~$620 total) | Sub-ms multipath, group-delay TEC |

**Two orthogonal axes — frequency stability vs absolute time:**

- The GPSDO provides **frequency stability** (1 ppb sample clock). This makes carrier-phase measurements metrologically coherent. Doppler, dTEC/dt, and scintillation are all *rate* measurements that only need precise sample spacing — they don't need to know what time it is.
- **Absolute time-of-day** (knowing when each sample occurred in UTC) adds D_clock — the propagation delay residual. This requires either an external PPS or self-recovery from the HF signals.

**The key insight:** The GPSDO upgrade unlocks the most scientifically valuable observables. Everything below the "rate domain" line on the figure is accessible with just an RX888 + GPSDO.

**The bridge:** This system can *bootstrap its own absolute time* from the HF signals, lifting a Tier 2 station to effective Tier 3 without external PPS. That's Act 2.

Speaker notes: "Here's the organizing framework. On the left is what hardware you have. On the right is what ionospheric phenomena you can observe. The GPSDO — around 160 dollars for a Leo Bodnar — is the single most important upgrade. It gives you a 1 part-per-billion sample clock, which makes carrier-phase measurements metrologically coherent. That unlocks Doppler, dTEC/dt, and scintillation — all rate measurements that don't need to know what time it is, only that consecutive samples are exactly spaced. The absolute delay products — D_clock, mode identification, group-delay TEC — need to know when in UTC each tick arrived. That's either an external PPS reference or, as I'll show, something we can recover from the time signals themselves."

---

### ACT 2: THE METROLOGY — Bridging Tier 2 → Tier 3

---

### Slide 3: TickEdgeDetector — The Measurement Engine (2 min)
**"50–57 ticks per minute, four observables per tick"**

Show:

- Matched filter template (WWV 5 ms/1000 Hz, WWVH 5 ms/1200 Hz, CHU 300 ms, BPM 10 ms)
- Front-edge back-calculation + sub-sample parabolic interpolation (from ntpd refclock_wwv.c)
- SNR-weighted robust median ensemble
- **From each tick: timing error (AM domain), carrier phase (IQ domain), SNR**
- **From the minute ensemble: D_clock, Doppler (phase slope), mean SNR**

**What needs the GPSDO (Tier 2+) and what needs absolute time (Tier 3+):**

- Doppler = linear fit to carrier phase vs sample index. Needs stable clock only. ✔ Tier 2
- D_clock = (observed arrival) − (expected arrival). Needs UTC. ✔ Tier 3+

Evidence table:

| Channel | Edges/min | Uncertainty | D_clock (ms) | Doppler coverage |
|---------|----------|-------------|-------------|------------------|
| CHU 7.85 | 53 | ±0.12 ms | +0.79 | 99.9% |
| SHARED 10.0 | 57 | ±2.1 ms | −0.53 | 100.0% |
| WWV 20.0 | 57 | ±2.0 ms | −0.72 | 99.9% |

~24K tick timing records/day, ~1.15M per-tick phase records/day, 9 channels, 24/7.

Speaker notes: "The measurement engine is a tick edge detector inspired by the ntpd WWV refclock driver. For every second of the minute, it cross-correlates a station-specific template against the IQ data, finds the front edge with sub-sample precision, and extracts both the timing error and the carrier phase. From 50 to 57 ticks, we build a robust median ensemble. The carrier phase across the minute gives Doppler — and notice, that only needs a stable sample clock. It doesn't need to know what time it is. But D_clock — the absolute propagation delay — does need UTC. That's the bridge we build next."

### Slide 4: UTC Recovery — Dual Kalman Fusion (1.5 min)
**"The time signals tell us what time it is."**

The metrology that lifts Tier 2 → Tier 3:

- D_clock residuals from 17 broadcasts, per minute
- L1 Kalman filter: geometric propagation model only
- L2 Kalman filter: ionospheric group-delay correction applied
- Both feed Chrony SHM as independent reference clocks

Live chronyc comparison (the metrological ladder):

| Source | Offset vs GPS | Bound (±) |
|--------|-------------|----------|
| Internet NTP | +0.6 ms | ±4.2 ms |
| **HF TSL1** (geometric) | +1.1 ms | ±0.6 ms |
| **HF TSL2** (ionospheric) | −0.055 ms | ±0.5 ms |
| GPS+PPS (ground truth) | +0.006 ms | ±0.039 ms |

**Key results:**

- TSL2 is **−55 µs vs GPS** — sub-100 µs UTC recovery from HF alone
- TSL2 is **20× closer to GPS** than TSL1 — ionospheric correction is doing real work
- Fusion D_clock: median −0.10 ms, MAD 0.26 ms, 96% within ±1 ms, 100% within ±2 ms
- **A Tier 2 station running this software bootstraps itself to effective Tier 3**

Speaker notes: "Since we're listening to time standard stations and we know their broadcast schedules, we can recover UTC from the signals themselves. We run two independent Kalman filters — L1 uses geometric path delays, L2 adds an ionospheric correction. Both feed Chrony as reference clocks. The result: TSL2 is within 55 microseconds of GPS ground truth — sub-100 microsecond UTC from HF alone. The ionospheric correction makes L2 twenty times closer to GPS than L1 — it's doing real work. This is the bridge: a station with only a GPSDO can recover absolute time from the HF signals and unlock the D_clock products."

### Slide 5: Kalman Fusion — Visual Evidence (30 sec)
**[Image: Kalman fusion plot from metrology dashboard]**

Shows the dual Kalman filter output over 24 hours: L1 (geometric-only) and L2 (ionospheric-corrected) D_clock estimates overlaid with GPS ground truth. The plot demonstrates:

- L1 (blue) tracks GPS with ±2 ms bound, visible diurnal wander from uncorrected ionospheric delay
- L2 (green) tracks GPS with ±0.6 ms bound — the ionospheric correction removes most of the diurnal structure
- GPS+PPS ground truth (red) is the flat baseline at ~0 ms
- The gap between L1 and L2 *is* the ionospheric correction — it's doing measurable work

**Current live values (2026-03-06):** TSL1 offset +1.1 ms (±0.6 ms), TSL2 offset −0.055 ms (±0.5 ms), GPS ground truth +0.006 ms (±0.039 ms).

Speaker notes: "This is the Kalman fusion in action — 24 hours of continuous UTC recovery from HF time signals. The blue trace is L1, using only geometric propagation delays. It wanders through the day as the ionosphere changes — that's the uncorrected group delay. The green trace is L2, which applies the ionospheric correction from our propagation model. Notice how much tighter it tracks GPS. The gap between L1 and L2 is the ionospheric correction itself — you can see it grow during the day and shrink at night, exactly as you'd expect. L2 is twenty times closer to GPS than L1 — just 55 microseconds. Both are far better than internet NTP."

---

### Slide 6: Allan Deviation — Timing Stability (30 sec)
**[Image: Allan deviation plot from metrology dashboard]**

Shows the Modified Allan Deviation (MDEV) for each timing source as a function of averaging time τ:

- **GPS+PPS:** Floor at ~10⁻¹¹ for τ > 100 s — the GPSDO's intrinsic stability
- **HF TSL2 (ionospheric):** ~10⁻⁸ at τ = 60 s, improving as τ⁻¹/² to ~10⁻⁹ at τ = 1000 s — white phase noise, averaging down
- **HF TSL1 (geometric):** ~3× worse than L2 at all τ, limited by unmodeled ionospheric delay
- **Internet NTP:** ~10⁻⁶ at τ = 60 s — three decades worse than HF

The Allan deviation makes the hierarchy quantitative: GPS > HF L2 > HF L1 > NTP, consistent at every timescale.

Speaker notes: "The Allan deviation gives the stability story in one plot. The x-axis is averaging time — how long you average before comparing. The y-axis is fractional frequency stability. GPS is at the bottom — that's our reference. The HF sources are three decades above GPS but three decades below NTP. And L2 consistently beats L1 at every timescale — the ionospheric correction isn't just reducing the mean offset, it's reducing the *instability*. The slope follows τ to the minus one-half, which tells us the dominant noise is white phase noise — exactly what you'd expect from independent per-minute measurements averaging down."

---

### Slide 7: Shared-Channel Discrimination (2 min)
**"Three stations on one frequency — we measure all three in parallel"**

The hardest measurement challenge: WWV, WWVH, and BPM on 2.5/5/10/15 MHz.

**Direct parallel measurement — not a decision algorithm:**

Three station-specific matched-filter pipelines run simultaneously on every shared channel:

| Pipeline | Tick freq | Tick duration | Expected arrival |
|----------|-----------|---------------|-----------------|
| WWV | 1000 Hz | 5 ms | ~4–8 ms |
| WWVH | 1200 Hz | 5 ms | ~22–30 ms |
| BPM | 1000 Hz | 10 ms | ~40–55 ms |

- WWV vs WWVH: separated by tick frequency (1000 vs 1200 Hz) — different templates, no ambiguity
- WWV vs BPM: both 1000 Hz, but separated by tick duration (5 vs 10 ms) and arrival time (~35 ms gap)
- Calibration: 14 NIST ground-truth minutes/hr refine per-station delay models

**Production evidence — all 4 shared channels (2026-03-06, 14h window):**

| Freq | Station | n | Median D\_clock | MAD | Median SNR |
|------|---------|---|----------------|-----|------------|
| 2.5 MHz | WWV | 875 | −1.01 ms | 2.35 ms | 9.6 dB |
| 2.5 MHz | WWVH | 876 | −0.20 ms | 2.35 ms | 9.6 dB |
| 2.5 MHz | BPM | 872 | +1.16 ms | 2.67 ms | 8.7 dB |
| 5.0 MHz | WWV | 876 | −1.57 ms | 2.41 ms | 9.0 dB |
| 5.0 MHz | WWVH | 876 | −0.15 ms | 2.33 ms | 8.8 dB |
| 5.0 MHz | BPM | 859 | +0.49 ms | 2.39 ms | 8.3 dB |
| 10.0 MHz | WWV | 874 | −1.84 ms | 2.31 ms | 8.1 dB |
| 10.0 MHz | WWVH | 877 | −0.71 ms | 2.51 ms | 8.1 dB |
| 10.0 MHz | BPM | 872 | +1.22 ms | 3.10 ms | 7.6 dB |
| 15.0 MHz | WWV | 876 | −0.48 ms | 2.06 ms | 8.0 dB |
| 15.0 MHz | WWVH | 876 | −0.07 ms | 2.03 ms | 8.0 dB |
| 15.0 MHz | BPM | 859 | +0.34 ms | 2.46 ms | 7.6 dB |

**Key observations:**
1. **D\_clock ordering is consistent across all 4 frequencies:** WWV < WWVH < BPM, matching path geometry (1,119 km / 6,599 km / 11,564 km).
2. **Cross-station Doppler correlation: r ≈ 0** on all channels (max |r| = 0.18 at 15 MHz). Independent ionospheric paths confirmed.
3. **~860–877 records per station per frequency per day** — all three stations maintain consistent measurement cadence, not just one dominant station.
4. **BPM has systematically higher D\_clock variance** (MAD 2.4–3.1 ms vs 2.0–2.5 ms for WWV/WWVH), consistent with its longer multi-hop path (3F/4F at 11,564 km).

[FIGURE: fig11 Doppler scatter triptych + fig_discrimination_4freq D_clock scatter across all 4 shared frequencies]

Speaker notes: "On the shared frequencies, three stations transmit simultaneously. We don't decide which one we're hearing — we measure all three in parallel. Each station has its own matched-filter template running against the same IQ buffer. WWV and WWVH separate trivially by tick frequency — 1000 versus 1200 Hz, completely different templates. BPM also uses 1000 Hz, but its 10-millisecond tick is twice as long as WWV's, and it arrives 35 to 50 milliseconds later from China versus 4 to 8 milliseconds from Colorado. Once the delay models are calibrated — and NIST gives us 14 ground-truth minutes per hour for that — geography does the rest. The table shows today's production data. Three things confirm the separation is real: the D_clock ordering is consistent on every channel, matching the geometric path lengths. The cross-station Doppler correlations are zero — these are genuinely independent ionospheric paths. And all three stations produce equal measurement rates — we're not favoring the loudest signal."

---

### ACT 3: THE SCIENCE PAYOFF — What Comes Into View

---

### Slide 8: 17 Simultaneous Ionospheric Paths (1.5 min)
**"A passive oblique ionosonde"**

[FIGURE: fig10 ionospheric fingerprint (4-panel SHARED 10 MHz) + fig14 frequency ladder]

**Three stations on 10 MHz — three independent paths through the same ionosphere:**

- D_clock: three distinct systematic offsets matching propagation geometry
- Doppler: three distinct diurnal signatures (r ≈ 0 cross-station)
- dTEC/dt: three independent TEC rate measurements

**WWV across 6 frequencies — six layers of the ionosphere:**

- 2.5–25 MHz, each reflecting at a different height
- Cross-frequency Doppler: r ≈ 0 (different layers)
- CHU cross-frequency: r = 0.43 (shared path, correlated)

[FIGURE: fig12 correlation heatmap]

127,802 all-arrivals records/day on CHU 7.85 alone. Multiple propagation modes resolved per minute.

Speaker notes: "The full system monitors 17 simultaneous paths through the ionosphere. On the shared channels, three stations at the same frequency give three independent ionospheric soundings — the Doppler correlation is zero, confirming they see different paths. Across frequencies, WWV from 2.5 to 25 MHz samples six different layers. CHU across three frequencies shows correlated Doppler — same path, same ionosphere, consistent. The correlation heatmap makes the structure clear: station clusters are independent, within-station cross-frequency is correlated. This is essentially a passive oblique ionosonde with 17 beams."

### Slides 9–12: Carrier-Phase dTEC — A Primary Science Product (2 min)
**"Bypassing the propagation model noise floor"**

*These four slides develop the dTEC argument progressively:*

**Slide 9 — The detection-limit argument:**

- Group-delay TEC: signal 0.85 ms, noise 6.5 ms → **SNR 0.13** (buried)
- Carrier-phase dTEC: ~6 mTECU/min sensitivity → **SNR 17–330×** for TIDs
- Formula: dTEC/dt = −f_D × c × f / 40.3
- **This is a Tier 2 product** — needs only the GPSDO, not absolute time

Evidence:

- 19,571 dTEC records/day (per-minute)
- 933,243 per-tick dTEC records/day (1-second resolution)
- GNSS-anchored when available (301 anchored records on 3/6; group-delay fallback)

**Slide 10 — dTEC time series (figure):**

[FIGURE: Multi-station dTEC rate overlay showing ionospheric dynamics across stations/frequencies]

Shows dTEC/dt time series for multiple paths simultaneously. The diurnal ionospheric variation is clearly visible: positive rates during sunrise (ionization building), negative during sunset (recombination). Different stations and frequencies show correlated but not identical structure — the expected signature of ionospheric dynamics sampled along different oblique paths.

**Slide 11 — dTEC detail / validation (figure):**

[FIGURE: Per-station dTEC comparison or scatter plot showing cross-frequency consistency]

Zoomed view or scatter plot demonstrating internal consistency of dTEC across frequencies for the same station. The 1/f dispersion relation predicts that dTEC/dt should be identical regardless of carrier frequency — the data confirms this to within measurement noise.

**Slide 12 — GNSS anchoring (figure):**

[FIGURE: Anchored TEC time series showing absolute scale from ZED-F9P VTEC]

Shows integrated dTEC pinned to absolute VTEC from the local GNSS receiver. Without anchoring, the integrated TEC drifts freely. With GNSS anchoring, the DC level is set to ±1 TECU accuracy. The oblique HF paths track the temporal dynamics; the GNSS provides the absolute scale.

Speaker notes (spanning slides 9–12): "Here's the payoff. Group-delay TEC — the classical approach of measuring 1/f² dispersion — is below our noise floor. The propagation model has 6.5 millisecond errors, the dispersion signal is 0.85 milliseconds. We can't see it. But carrier-phase dTEC bypasses this entirely. We measure the Doppler shift — the rate of change of carrier phase — and convert it to dTEC/dt using the ionospheric dispersion relation. The sensitivity is 6 milli-TECU per minute. And crucially, this is a Tier 2 product: it only needs the GPSDO, not absolute time. The time series shows the diurnal TEC variation across all 17 paths. To anchor the relative dTEC to absolute scale, we use a local ZED-F9P GPS receiver providing overhead VTEC at 1 TECU accuracy. The cross-frequency consistency — same station, different frequencies, same dTEC — validates that we're measuring real ionospheric physics."

### Slide 13: Differential dTEC — Self-Consistency (1 min)
**"Same ionosphere, different frequencies — do they agree?"**

Same station at multiple frequencies → same ionospheric path → dTEC should match.

| Station | Widest pair | RMS |
|---------|------------|-----|
| CHU | 3.33–14.67 MHz | 0.005–0.007 TECU |
| WWV | 2.50–25.00 MHz | 0.005–0.026 TECU |

29,312 records/day, 98.6% GOOD quality. Also a Tier 2 product.

Speaker notes: "How do we know we're measuring real ionospheric physics? For each station, we compare dTEC at different frequencies on the same path. They agree to within 0.017 TECU RMS. This is 29,000 consistency checks per day, nearly all passing."

### Slide 14: From Spectrogram to TEC — CHU 7.85 MHz (1.5 min)
**"One ionospheric path, four views"**

[FIGURE: fig16 GRAPE spectrogram CHU 7.85 (top) + fig13 physics cascade panels B–D (below)]

Show for CHU 7.85 MHz (exclusive channel — no discrimination ambiguity):

- Top: GRAPE spectrogram + power graph (the carrier Doppler trace is *visible* in the spectrogram)
- Panel B: Doppler (phase rate, Tier 2) — the same feature, extracted quantitatively
- Panel C: dTEC/dt (derived from Doppler, Tier 2)
- Panel D: Integrated Doppler vs smoothed D\_clock — shape correlation r = 0.60

**The visual story:** The spectrogram shows the carrier frequency offset varying with time — that's ionospheric Doppler, directly visible. The power graph shows signal strength varying with propagation conditions. Below, our pipeline extracts the Doppler quantitatively, converts it to dTEC/dt, and validates against D\_clock. The bottom panel is the money shot: integrated Doppler tracks the shape of D\_clock at 82× finer sensitivity.

**GRAPE compatibility:** This spectrogram is the standard GRAPE data product uploaded to PSWS — the same data that feeds our science pipeline.

Speaker notes: "This is CHU at 7.85 MHz — an exclusive channel with no discrimination ambiguity. At top, the GRAPE spectrogram — the standard data product we upload to PSWS. You can see the carrier frequency offset changing through the day — that's ionospheric Doppler, directly visible in the spectrogram. The power graph above it shows signal strength. Below, our pipeline extracts the Doppler quantitatively, converts to dTEC/dt, and in the bottom panel validates against D_clock. Integrated Doppler tracks the shape of D_clock at 82 times smaller amplitude, r = 0.60. So the spectrogram, the Doppler, the dTEC, and the D_clock are all measuring the same ionosphere through different physical processes."

### Slide 15: What's Next — Honest Assessment (1 min)
**"What doesn't work yet, and what's coming"**

**Current limits:**

- Group-delay TEC: below noise floor (SNR 0.13)
- VTEC maps from HF alone: geometrically correct but sTEC noise-dominated
- Scintillation: infrastructure ready, awaiting geomagnetic event

**Just deployed:**

- ✅ GNSS-anchored dTEC (ZED-F9P, ~1 TECU accuracy)

**Future:**

- Tier 4: PPS injection into HF IQ stream (under development)
- Per-path slant correction for GNSS anchoring

**What would a network of stations enable?**

- Spatial TEC gradients: multiple stations → horizontal structure of ionospheric disturbances
- TID wavefront tracking: correlated Doppler across sites gives propagation direction and velocity
- Geolocation of ionospheric scatterers via multilateration of propagation delays
- Continental-scale oblique ionosonde network using existing HF time signal infrastructure — no transmitter needed

**What would 2–4 GPSDO-locked RX888s at one site enable?**

- Phased-array angle-of-arrival estimation on HF time signals (antenna spacing ~ λ/2 at 10 MHz ≈ 15 m)
- Separate multipath arrivals by direction, not just by delay — resolve 1F vs 2F geometrically
- Per-mode Doppler and dTEC: track ionospheric dynamics on individual ray paths
- Scintillation spatial coherence: measure decorrelation length of Fresnel-scale irregularities
- All using the same GPSDO clock → coherent cross-correlation between antennas with zero relative timing error

Speaker notes: "What doesn't work yet: group-delay TEC is buried in noise. VTEC maps from HF alone aren't credible. Scintillation monitoring is built but the ionosphere has been quiet. What just shipped: GNSS-anchored dTEC, using a local GPS receiver to provide absolute scale. What's coming: PPS injection directly into the HF IQ stream — that's Tier 4, which would give us microsecond timing on every sample and potentially rescue group-delay TEC. But I want to leave you with two bigger ideas. First: a network of these stations. Each one gives 17 ionospheric paths. Ten stations across the continent gives 170 paths — that's a passive oblique ionosonde network using transmitters that are already on the air. Correlated Doppler across sites gives you TID wavefront direction and velocity. Second: multiple GPSDO-locked RX888s at a single site. Because they share the same 10 MHz reference, the antennas are phase-coherent — you get a phased array for free. Antenna spacing of 15 meters at 10 MHz gives you angle-of-arrival discrimination. That means you can separate multipath arrivals by direction, not just by delay, and track Doppler and dTEC on individual ray paths. The infrastructure is the same — the GPSDO is doing the heavy lifting."

### Slide 16: Bottom Line Summary & Call to Action (30 sec)

**The answer to the central question:**

With an RX888 (~$180) and a GPSDO (~$162), you can:

- Measure ionospheric Doppler at 100% coverage, 24/7
- Extract dTEC/dt at ~6 mTECU/min sensitivity on 17 paths
- Discriminate three co-channel stations via physics
- Self-recover UTC to −55 µs from the time signals

**Daily output:** 24K timing records, 1.15M phase records, 20K dTEC records, 29K consistency checks.

**Open source:** github.com/HamSCI/hf-timestd (MIT license)

Speaker notes: "So: with about 340 dollars of hardware — an RX888 and a Leo Bodnar GPSDO — open-source software, and the time standard stations that are already on the air, you can build a 17-path ionospheric sounder that runs 24/7. The data products are scientifically meaningful, self-consistent, and validated against GPS ground truth. The code is on GitHub under MIT license. I'd love to see a network of these stations."

---

## Figure Generation Plan

### Figure 1: Metrological Ladder
Bar chart or waterfall showing timing accuracy tiers. Source: chronyc snapshot + fusion HDF5.

### Figure 2: Tick Detection Example
Correlation waveform showing tick detection with front-edge marking. Source: raw IQ + TickEdgeDetector.

### Figure 3: D_clock Time Series (24h)
Per-channel D_clock over 24 hours showing diurnal variation. Source: tick_timing HDF5.

### Figure 4: Fusion D_clock Distribution
Histogram of fusion D_clock residuals vs GPS. Source: fusion HDF5.

### Figure 5: dTEC Rate Time Series
Multi-station dTEC rate overlay showing ionospheric dynamics. Source: dtec HDF5.

### Figure 6: Differential dTEC Validation
Scatter plot of dTEC at freq1 vs freq2 for same station. Source: dtec_timeseries HDF5.

### Figure 7: Doppler Diurnal Signature
24-hour Doppler time series for CHU 7.85 MHz. Source: tick_timing HDF5.

### Figure 8: All-Arrivals Mode Timeline
Time-of-flight vs time showing multiple propagation modes. Source: all_arrivals HDF5.

### Figure 9: Shared-Channel Discrimination
D_clock scatter by station on SHARED 10 MHz, color-coded by station (WWV/WWVH/BPM), showing distinct systematic offsets. Inset: test signal tone power comparison between WWV (min 8) and WWVH (min 44). Source: tick\_timing + test_signal HDF5.

---

## Timing Budget (15 minutes) — revised 2026-03-06

| Act | Slide | Topic | Time | Cumulative |
|-----|-------|-------|------|------------|
| 1 | 1 | Title + central question | 0:30 | 0:30 |
| 1 | 2 | Phenomena ladder (hardware tiers) | 2:00 | 2:30 |
| 2 | 3 | TickEdgeDetector (measurement engine) | 1:30 | 4:00 |
| 2 | 4 | UTC recovery (dual Kalman, metrological ladder) | 1:00 | 5:00 |
| 2 | 5 | Kalman fusion plot (visual evidence) | 0:30 | 5:30 |
| 2 | 6 | Allan deviation (timing stability) | 0:30 | 6:00 |
| 2 | 7 | Shared-channel discrimination | 2:00 | 8:00 |
| 3 | 8 | 17 paths (fingerprint + heatmap) | 1:00 | 9:00 |
| 3 | 9–12 | Carrier-phase dTEC (4 slides, progressive) | 2:00 | 11:00 |
| 3 | 13 | Differential dTEC (self-consistency) | 1:00 | 12:00 |
| 3 | 14 | From spectrogram to TEC (CHU 7.85 MHz) | 1:30 | 13:30 |
|   | 15 | What's next (honest assessment) | 1:00 | 14:30 |
|   | 16 | Bottom line summary + call to action | 0:30 | 15:00 |

**Note:** Slides 5–6 are visual evidence supporting the Slide 4 metrology claims — let the plots speak, minimal narration. Slides 9–12 develop the dTEC argument progressively across four slides. Slides 15–16 are closers. The three acts structure the narrative as question → method → payoff.

---

## Glossary — Key Terms for Q&A

**D\_clock** — The propagation delay residual: the difference between the observed arrival time of a time signal tick and the expected arrival time based on a geometric propagation model. Measured in milliseconds. Positive D\_clock means the signal arrived later than predicted (longer ionospheric path). Requires absolute UTC (Tier 3+).

**dTEC** — The rate of change of Total Electron Content along the signal path, derived from carrier-phase Doppler shift: dTEC/dt = −f\_D × c × f / 40.3. Measured in TECU per second or TECU per minute. A *rate* measurement that requires only frequency stability (Tier 2), not absolute time.

**Scintillation** — Rapid fluctuations in signal amplitude (S4 index) and/or phase (σ\_φ index) caused by small-scale ionospheric irregularities. At HF frequencies the Fresnel zone is ~100 km, so classical scintillation is rare except during geomagnetic storms (equatorial spread-F, polar irregularities). Our tick-to-tick σ\_φ ≈ 1.0 rad during quiet conditions is multipath + noise, not scintillation.

**sTEC** — Slant Total Electron Content: the integrated electron density along the oblique signal path from transmitter to receiver, in TEC Units (1 TECU = 10¹⁶ electrons/m²). Related to VTEC by a geometric mapping function that depends on elevation angle and assumed ionospheric shell height.

**IONEX** — IONosphere EXchange format: a standard file format for distributing ionospheric TEC maps. Our system writes IONEX files from the VTEC mapper every ~3 minutes and also reads external GPS-derived IONEX maps for comparison.

**Mode ID** — Propagation mode identification: determining whether a received signal arrived via 1-hop F-layer (1F), 2-hop F-layer (2F), E-layer, or other paths. Modes are distinguished by their propagation delay (D\_clock), with 1F arriving first and higher-order modes arriving later. Requires absolute time (Tier 3+).

**Absolute delay** — The one-way propagation time from transmitter to receiver, measured in milliseconds. Equivalent to D\_clock when the propagation model offset is zero. Requires knowing both when the signal was transmitted (from the time standard schedule) and when it was received (from UTC recovery or PPS).

**Kalman filter** — A recursive state estimator that combines noisy measurements with a dynamic model to produce optimal estimates of system state. Our dual Kalman fusion runs two independent filters: L1 (geometric propagation model only) and L2 (with ionospheric group-delay correction). Each filter tracks the system clock offset and drift, fed by D\_clock residuals from 17 broadcast paths per minute.

---

## Q&A Preparation — Anticipated Questions and Answers

### Measurement validity

**"How do you know the Doppler is real ionospheric motion and not oscillator drift?"**
The GPSDO provides 1 ppb frequency stability. The observed Doppler on CHU 7.85 MHz is ±0.34 Hz, corresponding to ~43 ppb — 43× larger than possible GPSDO drift. Cross-frequency correlation within CHU (r = 0.43 across 3.33/7.85/14.67 MHz) confirms a shared ionospheric origin. Cross-station correlation is zero, confirming independent paths.

**"What's the actual accuracy of your dTEC measurements?"**
Internal consistency: differential dTEC RMS 0.017 TECU across frequencies (29,312 checks/day, 98.6% GOOD). Absolute calibration comes from GNSS anchoring — the local ZED-F9P gives overhead VTEC at ±1 TECU, which sets the DC level. The carrier-phase dTEC/dt sensitivity is ~6 mTECU/min.

**"Your D\_clock has ±1–7 ms uncertainty — isn't that too noisy for ionospheric science?"**
Yes — that's exactly why we emphasize carrier-phase dTEC (SNR 17–330×) over group-delay TEC (SNR 0.13). D\_clock is useful for mode identification and for validating that Doppler tracks the same ionosphere (r = 0.65 shape correlation), but it's not the primary science observable. The rate-domain products (Doppler, dTEC/dt) are far more precise.

### Comparison to existing systems

**"How does this compare to a standard GRAPE receiver?"**
We produce GRAPE-compatible spectrograms and upload to PSWS. The GPSDO adds quantitative Doppler extraction and dTEC derivation on top of what a standard GRAPE shows qualitatively in the spectrogram. The spectrogram is the starting point; our pipeline extracts the physics quantitatively.

**"Why not just use a GNSS receiver for TEC?"**
GNSS measures overhead vertical TEC at one point. HF time signals give oblique paths at fixed geometries to known transmitters — complementary measurements. The 17 paths sample different parts of the ionosphere at different reflection heights (2.5–25 MHz). GNSS provides the absolute scale; HF provides the spatial and temporal structure.

**"How does this compare to SuperDARN / ionosondes?"**
Those are active systems requiring transmit licenses and significant hardware. This is entirely passive, single-antenna, ~$340 total cost (Tier 2), 24/7 autonomous. The trade-off is that we only observe along fixed paths to known transmitters rather than scanning arbitrary directions.

### Practical / replication

**"Can I do this with my existing SDR?"**
You need wideband simultaneous coverage (0.5–30 MHz) for multi-frequency operation. The RX888 is unique at this price point (~$180) with 16-bit ADC and 64 MHz bandwidth. A narrowband SDR could monitor one frequency but loses the cross-frequency consistency checks, differential dTEC, and the 17-path geometry.

**"Do I need the $440 differential GPSDO?"**
No. The $162 Leo Bodnar base model gives you Tier 2 — Doppler, dTEC/dt, scintillation indices. The differential version with PPS output is Tier 4 for sub-microsecond timing precision, which enables sub-ms multipath resolution and group-delay TEC. Most of the science products we demonstrate are Tier 2.

**"How much bandwidth / storage does this take?"**
The RX888 streams ~250 MB/s raw IQ over USB3. KA9Q-radio does the channelization in real-time on the host PC. The decimated 10 Hz products are ~50 MB/day per channel (~450 MB/day for 9 channels). The raw 24 kHz per-minute archive is ~2 GB/day per channel if retained (we currently retain only today's raw data).

### Scientific depth

**"You show r = 0.65 shape correlation between integrated Doppler and D\_clock — why not higher?"**
D\_clock has ~6.5 ms noise from propagation model errors; integrated Doppler accumulates its own drift from measurement noise. The scale ratio (45–82×) shows that Doppler has far finer sensitivity to ionospheric changes than D\_clock. The correlation is statistically significant given the noise levels, and the fact that it's positive at all — with the correct sign and physically meaningful scale ratio — is the validation.

**"Can you detect TIDs with this?"**
The infrastructure exists — dTEC time series at 1-second resolution on 17 paths. Medium-scale TIDs (period 15–60 min, velocity ~100–300 m/s) should produce coherent oscillations in dTEC across paths. We haven't had a validated TID event in the current data window (February conditions, quiet geomagnetic activity). This is a near-term goal, particularly as we approach equinox.

**"Can you resolve individual multipath modes on the ionogram?"**
Not yet with the current matched-filter correlator. The GPSDO gives us µs-level absolute sample timing, so the clock isn't the limit — the limit is the ambiguity function of the matched filter. A 300 ms CHU tone template with 100 Hz bandpass produces a correlation mainlobe ~10 ms wide. The 1F→2F hop separation for our geometry (CHU at ~1650 km) is only ~6 ms — below the mainlobe width. Two arrivals within 10 ms merge into a single broadened peak. Three paths forward: (a) carrier-domain analysis on unique channels (CHU, WWV 20/25 MHz) where we can use wider bandpass without cross-station contamination, giving finer time resolution; (b) PhaRLAP ray-tracing for precise multi-hop delay predictions, enabling constrained model fitting even within the mainlobe; (c) phased-array angle-of-arrival with multiple GPSDO-locked receivers to separate modes geometrically rather than temporally. The 3F and 4F hops (>12 ms separation) may already be resolvable — that's a near-term investigation.

**"What about ionospheric absorption / D-region effects?"**
The power graph in the spectrogram shows signal strength variations that include D-region absorption (riometer-like). We don't currently separate absorption from multipath fading or other propagation effects, but the multi-frequency power data is there. A solar flare SID (sudden ionospheric disturbance) would show simultaneous power drops across all channels — detectable but not yet demonstrated.

### Network / future

**"What would a network of these stations show?"**
With 2–3 stations in different grid squares: spatial TEC gradients, TID wavefront tracking (propagation direction and velocity from correlated Doppler across sites), and multilateration of ionospheric scatterers from differential propagation delays. Each station adds 17 independent paths.

**"Is this compatible with PSWS / HamSCI data infrastructure?"**
Yes. We produce standard GRAPE spectrograms (same format, same frequency resolution). The decimated 10 Hz IQ data is the GRAPE standard. Full PSWS upload integration is in progress.

### dTEC methodology

**"How do you calculate dTEC/dt?"**
From the carrier phase of individual per-second tick detections. The TickEdgeDetector extracts ~55 carrier phase measurements per minute per station-frequency (one per tick). We unwrap the phase time series for continuity, compute the finite-difference phase rate (Doppler), then apply the ionospheric dispersion relation:

    d(sTEC)/dt = −f_D × c × f / 40.3

where f_D is the Doppler shift in Hz, c is the speed of light, f is the carrier frequency in Hz, and 40.3 m³/s² is the ionospheric constant. The physics: ionospheric phase advance is φ_iono(t) = −(2π/c) × (40.3/f) × sTEC(t), so the time derivative gives dTEC/dt directly from the measured Doppler. We then integrate dTEC/dt via trapezoidal rule to get relative TEC(t). Quality gates include: cycle-slip detection (phase acceleration > 5 Hz/s), unwrap quality scoring (fraction of inter-sample |Δφ| > π/2), and gap rejection (dt > 120 s). The code is in `carrier_tec.py:CarrierTECEstimator.compute_dtec_from_phase()`.

Key point for the audience: this is a **Tier 2 product** — it requires only GPSDO frequency stability to ensure consecutive samples are correctly spaced. It does not require absolute time. The Doppler is measured from the *slope* of carrier phase across ticks within a minute, not from the absolute phase value.

**"What does anchoring with local GNSS VTEC add? What's the quantitative difference with and without it?"**
Without anchoring, carrier-phase dTEC is a *relative* measurement — you get dTEC/dt (the rate) and the *shape* of TEC(t) via integration, but the DC level is arbitrary. The integrated TEC drifts freely because there's no absolute reference. The per-minute summary (`dtec_mean_tecu`) is meaningless without an anchor; only `dtec_rate_tecu_per_s` is reliable. Quality is capped at MARGINAL regardless of SNR when unanchored (code enforces this: `if not is_anchored and qflag == 'GOOD': qflag = 'MARGINAL'`).

With GNSS anchoring (our ZED-F9P provides overhead VTEC at ~1 TECU accuracy, ~1 sample/second), the integrated dTEC is pinned to an absolute scale. The anchor is applied at the midpoint of each minute: we read the nearest GNSS VTEC within ±120 seconds and use it as the DC level for all station-channels.

Quantitative difference:
- **Without anchor:** dTEC/dt sensitivity ~6 mTECU/min (unchanged), but integrated TEC has unbounded cumulative drift (~0.05–0.5 TECU/hour depending on noise). Useful for short-term dynamics (TIDs, flares) but not for absolute TEC.
- **With GNSS anchor:** Integrated TEC absolute accuracy ~1–3 TECU (dominated by the ±1 TECU GNSS accuracy and the ~10–30% mapping error from using zenith VTEC for oblique paths without per-path slant correction).
- **With group-delay anchor (fallback):** Essentially useless — group-delay TEC has SNR 0.13, so the anchor itself is noise-dominated. Confidence gate (≥0.5) rejects most group-delay anchors in practice.

For stations without a GNSS receiver: the dTEC *rate* products are fully valid — Doppler, dTEC/dt, differential dTEC (cross-frequency consistency) all work at Tier 2 without any anchor. The integrated absolute TEC is the only product that degrades. The rate domain is where most of the science value lives anyway.

### Propagation modeling

**"What external data sources are you using?"**
Three tiers, with automatic fallback:

1. **WAM-IPE (primary)** — NOAA's Whole Atmosphere Model–Ionosphere Plasmasphere Electrodynamics. Fetched from public S3 bucket (`s3://noaa-nws-wam-ipe-pds/`, no credentials needed) or NOMADS fallback. 5-minute cadence, 1° geographic grid. Provides hmF2, NmF2, TEC. Updated every 5 minutes, cached for 1 hour.

2. **GIRO ionosonde network (supplementary)** — Global Ionospheric Radio Observatory, via DIDBase (`lgdc.uml.edu/common/DIDBFast498`). Real-time foF2 and hmF2 from nearby ionosondes. Used to correct WAM-IPE systematic biases. Cached for 15 minutes.

3. **Climatological fallback** — Built-in parametric model using Chapman layer profiles with diurnal, seasonal, latitudinal, and equatorial anomaly terms. No network dependency. This is what runs when WAM-IPE and GIRO are both unavailable.

Additionally: **IRI-2020** (International Reference Ionosphere) is attempted as a middle tier between WAM-IPE and the parametric fallback, providing foF2 and hmF2 from the empirical climatological model when real-time data is unavailable.

The propagation model (`propagation_model.py:HFPropagationModel`) uses these ionospheric parameters to compute HF group delay by numerically integrating the group refractive index n_g = 1/√(1 − f_p²/f²) through the electron density profile, evaluating 1F/2F/3F/1E modes with MUF checks. Uncertainty is source-dependent: ±0.5 ms (WAM-IPE+GIRO), ±1.0 ms (WAM-IPE alone), ±1.5 ms (IRI), ±3.0 ms (parametric), ±5.0 ms (no model).

**"What external data sources could you use?"**
Several additional sources exist but are not yet integrated:

- **IRTAM (IRI Real-Time Assimilative Mapping)** — GIRO's real-time assimilation product that blends IRI with live ionosonde data globally. Would replace our own WAM-IPE + GIRO blending with a community-standard assimilated product. Available via DIDBase.
- **SWPC US-TEC** — NOAA Space Weather Prediction Center real-time TEC maps from dual-frequency GPS. Would provide a cross-validation source for our HF-derived TEC and a better spatial anchor than single-point GNSS VTEC.
- **Madrigal/CEDAR** — MIT Haystack Observatory's database of ionospheric measurements including ISR (incoherent scatter radar) profiles. Rich Ne(h) data but not real-time.
- **GOES X-ray flux** — Already fetched for SID detection but not yet used as a D-region absorption model input. C/M/X-class flare magnitudes could drive a real-time D-region opacity correction.
- **SuperDARN** — Convection patterns and irregularity maps from the HF radar network. Would help identify scintillation-prone conditions. Data available via BAS/VT.
- **OMNI/ACE solar wind** — Upstream solar wind parameters for predicting geomagnetic storm onset. Would enable predictive mode switching (e.g., widen search windows during storm onset).
- **IGS IONEX maps** — Already partially integrated (we write and read IONEX files). Global GPS-derived TEC maps at 2-hour cadence with 2.5°×5° resolution. Would provide better spatial anchoring than single-point GNSS VTEC.

**"What would ray-tracing (e.g., PhaRLAP) add?"**
Our current propagation model uses a 1D numerical integration through a Chapman-layer electron density profile — essentially a vertical slice model with geometric hop geometry. This has three significant limitations that ray-tracing would address:

1. **Multi-hop delay precision.** Our model computes N-hop delay as N × (geometric + iono per hop), treating each hop identically. In reality, the reflection height and slant TEC differ for each hop because the ionosphere varies along the path. For CHU at ~1650 km, the 1F→2F delay separation is only ~6 ms — our model uncertainty (±0.5–5 ms depending on data source) is comparable to the mode separation. PhaRLAP would give per-hop delays accurate to ~0.1 ms by tracing the actual ray through a 3D ionosphere, making mode identification far more reliable.

2. **Off-great-circle propagation.** HF rays don't follow great circles — tilted ionospheric layers (from TIDs, the equatorial anomaly, or auroral gradients) bend rays laterally. Our model assumes great-circle geometry. PhaRLAP traces through the 3D refractive index field and naturally captures lateral deviation, which affects both the delay and the effective reflection point location.

3. **Constrained multipath fitting.** The matched-filter correlator's 10 ms mainlobe merges 1F and 2F arrivals (6 ms separation for CHU). With PhaRLAP providing precise predicted arrival times for each mode, we could fit a constrained multi-mode model to the correlation function — essentially deconvolving the merged arrivals using the predicted delays as priors. This would let us resolve multipath modes that are currently below our temporal resolution limit.

4. **Angle-of-arrival prediction.** If we add a phased array (multiple GPSDO-locked RX888s), PhaRLAP would predict the expected elevation and azimuth of each mode arrival, enabling direct comparison with measured angles of arrival.

Integration path: PhaRLAP is a MATLAB/compiled library. We would call it via the Python `pharlap` wrapper or pre-compute lookup tables of (station, frequency, time) → (mode, delay, elevation) and interpolate at runtime. The IonoDataService already provides the ionospheric parameters that PhaRLAP needs as input.

### Shared-channel discrimination

**"How do you separate WWV from WWVH — they're on the same frequency?"**
We don't separate them — we measure both in parallel. The TickEdgeDetector runs station-specific matched-filter templates simultaneously: WWV at 1000 Hz (5 ms tick) and WWVH at 1200 Hz (5 ms tick). Different tone frequencies mean different templates — they don't interfere. Each produces its own independent D_clock, Doppler, SNR, and confidence every minute. The NIST tone schedule provides 14 ground-truth calibration minutes per hour (WWV-only: minutes 1, 16, 17, 19; WWVH-only: minutes 2, 43–51) which refine the per-station delay models. Additional broadcast features (440 Hz in minutes 1/2, test signals in minutes 8/44) provide further calibration opportunities, but the primary separation is physical — different tone frequencies, different templates.

**"How do you separate BPM from WWV — both use 1000 Hz?"**
Three physical features make them distinguishable even at the same tone frequency: (1) Tick duration — WWV = 5 ms tick within an 800 ms tone; BPM = 10 ms tick within a 300 ms marker. Separate matched-filter templates (0.8s vs 0.3s). (2) Propagation delay — WWV arrives at ~4–8 ms after the minute boundary (1,119 km path); BPM arrives at ~40–55 ms (11,564 km, 3F/4F multi-hop). The >30 ms temporal gap keeps them well-resolved, and each template searches within its station's expected arrival window. (3) BPM UT1 minutes (25–29, 55–59) — BPM transmits 100 ms pulses, 10× longer than WWV's 5 ms ticks. These are unambiguous BPM markers and calibrate BPM's delay model.

**"What about harmonics and intermodulation products contaminating the tick detection?"**
This is a real issue. The 500 Hz × 2 = 1000 Hz harmonic contaminates WWV tick detection; 600 Hz × 2 = 1200 Hz contaminates WWVH. The 440 Hz × 3 = 1320 Hz harmonic lands near the WWVH 1200 Hz band. Mitigation: IIR notch filters at 440, 500, and 600 Hz (Q=20) are applied before tick correlation, removing the fundamentals that generate the harmonics. Additionally, the NIST broadcast specification (SP 432) provides a 10 ms silence zone before each tick, which suppresses the intermod pedestal. On shared channels during intermod-prone minutes, tick detection confidence is reduced (clean=0) but ticks are still detected because the notch filters remove the contamination source.

**"What about the WWV/BPM timing overlap — don't the ticks collide?"**
Within each second, both WWV and BPM produce a 1000 Hz tick. Their arrivals differ by ~35–50 ms (path difference). The matched-filter mainlobe is ~10 ms wide, and the 35+ ms separation keeps the correlation peaks resolved. During anomalous propagation (e.g., long-delayed WWV multipath approaching BPM's normal arrival time), the arrivals could converge — this is flagged when timing falls outside the learned delay model's ±1 ms window (measurement phase) or ±5 ms window (refinement phase).

**"How do you know the discrimination is working correctly and not producing artifacts?"**
The strongest validation is the cross-station Doppler correlation. If the discrimination were mislabeling multipath components of a single station as different stations, the Doppler time series would be correlated (same ionospheric path → same Doppler). Instead, we measure r ≈ 0 between all station pairs on all four shared frequencies (max |r| = 0.18). Independent ionospheric paths produce uncorrelated Doppler — which is exactly what we observe. Additionally, the D_clock ordering (WWV < WWVH < BPM) is consistent across all four shared frequencies and matches the geographic path distances (1,119 / 6,599 / 11,564 km). An artifact wouldn't maintain physically consistent ordering across independent frequencies.

**"What fraction of minutes produce valid measurements for all three stations?"**
Production data shows ~860–877 records per station per frequency per day (of ~877 possible minutes in a 14-hour window). All three stations maintain essentially identical measurement cadence — we're not biased toward the strongest signal. The parallel template approach means each station is measured independently every minute; a "failure" for one station (e.g., BPM below noise floor at night) doesn't affect the other two. The 14 ground-truth minutes per hour from NIST's exclusive tone schedule calibrate the delay models, but the measurements themselves don't depend on voting or decision logic — the matched-filter templates inherently select for each station's physical signal characteristics.

### The "gotcha" questions

**"You're claiming sub-millisecond UTC from HF — isn't that just because you already have GPS?"**
Fair question. Our GPS validates the claim but isn't required for operation. The UTC recovery uses only the HF signals themselves — tick detection, propagation model, dual Kalman fusion. The GPS+PPS provides ground truth for measuring how well it works. A Tier 2 station (no PPS) can bootstrap to effective Tier 3 using only the HF time signals. The GPS lets us *prove* it works, not *make* it work.

**"Your scintillation infrastructure has never detected a real event — why present it?"**
We present it as "within reach," not "demonstrated." The honest framing matters. The dual-source infrastructure (S4 from test signal multi-tone, σ\_φ from per-tick carrier phase) is wired and producing data. Solar cycle 25 is near maximum — geomagnetic storms will come. When they do, the infrastructure is ready. We'd rather have the measurement chain validated and waiting than scrambling to build it during an event.

**"How much of this was built by AI?"**
The codebase is developed in collaboration with AI coding assistants (primarily Cascade/Claude). The human provides domain knowledge, measurement strategy, and validation against physical reality. The AI accelerates implementation of signal processing pipelines, data management, and analysis code. All scientific claims are validated against production data and physical consistency checks — the AI doesn't get to decide what's true, the data does.
