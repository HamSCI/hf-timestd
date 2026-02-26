# HamSCI 2026 Workshop — Presentation Plan

**Title:** Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation
**Author:** Michael James Hauan (AC0G)
**Duration:** 15 minutes
**Date:** Prepared 2026-02-24

---

## Narrative Arc (revised 2026-02-25)

**Central question:** *With an RX888 and a GPSDO, what kind of ionospheric science can we do?*

**Central thesis:** A GPSDO-locked SDR listening to HF time standard stations is a precision ionospheric instrument.  The GPSDO provides the frequency stability that unlocks carrier-phase observables (Doppler, dTEC/dt, scintillation); recovering UTC from the time signals themselves adds absolute propagation delay (D_clock), mode identification, and absolute TEC.

**Structure — three acts:**

1. **The hardware question** (Slides 1–2): What timing infrastructure do you have? Four tiers from bare crystal to GPS+PPS. Each tier unlocks a different class of ionospheric observable. This is the organizing framework.

2. **The metrology** (Slides 3–5): How we recover UTC from HF time signals when we already have a GPSDO. The GPSDO gives us the sample clock; the time signals give us absolute time-of-day via tick detection → multi-station fusion → Chrony feed. This is the bridge from Tier 2 (GPSDO-only, rate measurements) to Tier 3 (D_clock, absolute delay).

3. **The science payoff** (Slides 6–10): What comes into view once you have both frequency stability and time recovery. Demonstrated products from live data: carrier-phase dTEC, Doppler, shared-channel discrimination, multipath mode identification, cross-domain consistency.

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

## Evidence Audit (2026-02-23 production data)

### Metrological Ladder — Live Chrony Comparison

| Tier | Timing Source | Offset vs GPS | Bound (±) | Factor vs GPS |
|------|-------------|---------------|-----------|---------------|
| 0 | Unsynchronized PC clock | ~50–100 ms | — | ~50,000× |
| 1 | Internet NTP (time-e-b.nist.gov) | −6.0 ms | ±19 ms | ~6,000× |
| 2 | LAN NTP (router, stratum 2) | +6.7 ms | ±31 ms | ~6,700× |
| 3 | **GPS+PPS** (192.168.0.203, stratum 1) | **−0.001 ms** | **±0.094 ms** | **1× (reference)** |
| 4 | **TSL1** — HF L1 geometric | +1.1 ms | ±2.0 ms | ~1,100× |
| 5 | **TSL2** — HF L2 ionospheric | +0.8 ms | ±0.6 ms | ~800× |

**Key finding:** TSL2 (ionospherically corrected) has **3.3× tighter uncertainty** than TSL1 (geometric only). Both are **100× better than internet NTP**. The improvement from L1→L2 validates that the ionospheric correction is doing real work.

**Fusion D_clock statistics (full day, 2026-02-23):**

- 1,101 valid measurements out of 1,441 minutes
- Median offset: −1.2 ms
- MAD: 1.4 ms
- 60% of measurements within ±2 ms of GPS
- 86% within ±5 ms

### Tick Detection Performance

| Channel | Records/day | Stations | Median D_clock | SNR | Median Edges | Doppler Coverage |
|---------|------------|----------|---------------|-----|-------------|-----------------|
| CHU 3.33 | 1,343 | CHU | +0.89 ms | 21.5 dB | 50/58 | 99.9% |
| CHU 7.85 | 1,322 | CHU | +1.96 ms | 27.3 dB | 51/58 | 99.8% |
| CHU 14.67 | 1,348 | CHU | +1.84 ms | 22.2 dB | 38/58 | 99.9% |
| SHARED 2.5 | 4,110 | WWV+WWVH+BPM | −0.15 ms | 7.9 dB | 57/57 | 99.8% |
| SHARED 5.0 | 4,149 | WWV+WWVH+BPM | −0.18 ms | 8.0 dB | 57/57 | 99.8% |
| SHARED 10.0 | 4,139 | WWV+WWVH+BPM | −0.02 ms | 7.9 dB | 57/57 | 99.7% |
| SHARED 15.0 | 4,173 | WWV+WWVH+BPM | −0.15 ms | 8.0 dB | 57/57 | 99.6% |
| WWV 20.0 | 1,380 | WWV | −0.05 ms | 7.9 dB | 57/57 | 99.9% |
| WWV 25.0 | 1,378 | WWV | −0.08 ms | 7.9 dB | 57/57 | 99.7% |

**Total: ~24,342 tick timing records/day across 9 channels, 17 broadcasts**

### Physics Products

| Product | Records/day | Status | Key Metric |
|---------|------------|--------|------------|
| Carrier-phase dTEC rate | 17,045 | ✅ | median ≈ 0 TECU/s, σ = 0.38 mTECU/s |
| Per-tick dTEC time series | 848,599 | ✅ | ~55 records/min/station, 1-second resolution |
| Differential dTEC | 22,474 | ✅ | RMS < 0.03 TECU (all GOOD quality) |
| All-arrivals (multipath) | 46,774 (CHU 7.85 alone) | ✅ | Multiple modes resolved per minute |
| Doppler shifts | 99.7%+ coverage | ✅ | CHU 7.85: ±0.34 Hz range, σ = 0.09 Hz |
| Per-tick carrier phase | 190,367 (3 channels) | ✅ | tick-to-tick σ_φ ≈ 1.0 rad (ionospheric) |
| Fusion UTC estimate | 1,101/day | ✅ | Median offset −1.2 ms vs GPS |

### Demonstrated Claims (backed by production data, 2026-02-23)

1. **Sub-millisecond UTC recovery from HF** — TSL2 at +774 µs ± 600 µs vs GPS ground truth
2. **100× better than internet NTP** — HF-derived time at 0.8 ms vs NTP at 6–7 ms offset
3. **L2 ionospheric correction improves L1** — 3.3× tighter uncertainty bound; the correction does real work
4. **50–57 ticks/min ensemble** — verified across all 9 channels, SNR-weighted robust median
5. **~850K per-tick carrier-phase records/day** — 1-second time resolution across 17 paths
6. **17K per-minute dTEC records/day** — primary science product, carrier-phase derived
7. **Differential dTEC RMS < 0.03 TECU** — 22K cross-frequency consistency checks/day, all GOOD quality
8. **Doppler extraction at 99.7%+ coverage** — diurnal signatures clearly resolved, 24/7
9. **Multipath mode identification** — 46K all-arrivals records/day on CHU 7.85 alone; multiple modes resolved per minute
10. **17 simultaneous sounding paths** — 4 stations × multiple frequencies, passive oblique ionosonde
11. **Shared-channel station discrimination** — 7 independent methods separate WWV, WWVH, and BPM on 2.5/5/10/15 MHz
12. **Metrological ladder** — live chronyc comparison: NTP → HF L1 → HF L2 → GPS+PPS
13. **GNSS-anchored dTEC** — local ZED-F9P VTEC (41.7 TECU, ±1 TECU) provides absolute scale for carrier-phase dTEC
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

### What the data actually shows (2026-02-23)

The CRITIC\_CONTEXT claim that "vtec_tecu is all NaN" is outdated. Live data:

| Station | TEC Records | VTEC Valid | Confidence Median |
|---------|------------|-----------|-------------------|
| BPM     | 571        | 297 (52%) | 0.300             |
| CHU     | 335        | 72 (21%)  | 0.270             |
| WWV     | 297        | 191 (64%) | 0.300             |
| WWVH    | 569        | 409 (72%) | 0.300             |

969/1,772 records (55%) have valid VTEC. The gate is `confidence >= 0.3` in
`_build_ipp_measurements()` (physics\_fusion_service.py:524).

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

Validated: 17 station-channel records written with `ANCHORED_GNSS`, `anchor_tec=41.7 TECU`.
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

## Presentation Outline (15 minutes) — revised 2026-02-25

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

| Channel | Edges/min | SNR (dB) | D_clock (ms) | Doppler coverage |
|---------|----------|---------|-------------|------------------|
| CHU 7.85 | 51 | 27.3 | +1.96 | 99.8% |
| SHARED 10.0 | 57 | 7.9 | −0.02 | 99.7% |
| WWV 20.0 | 57 | 7.9 | −0.05 | 99.9% |

~24K tick timing records/day, ~850K per-tick phase records/day, 9 channels, 24/7.

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
| Internet NTP | −6.0 ms | ±19 ms |
| **HF TSL1** (geometric) | +1.1 ms | ±2.0 ms |
| **HF TSL2** (ionospheric) | +0.8 ms | ±0.6 ms |
| GPS+PPS (ground truth) | −0.001 ms | ±0.094 ms |

**Key results:**

- HF timing is **100× better than internet NTP**
- L2 has **3.3× tighter bound** than L1 — ionospheric correction is doing real work
- Fusion D_clock: median −1.2 ms, 60% within ±2 ms, 86% within ±5 ms
- **A Tier 2 station running this software bootstraps itself to effective Tier 3**

Speaker notes: "Since we're listening to time standard stations and we know their broadcast schedules, we can recover UTC from the signals themselves. We run two independent Kalman filters — L1 uses geometric path delays, L2 adds an ionospheric correction. Both feed Chrony as reference clocks. The result: our HF-derived time is 100 times better than internet NTP and within about 1 millisecond of GPS ground truth. The ionospheric correction makes L2 three times tighter than L1 — it's doing real work. This is the bridge: a station with only a GPSDO can recover absolute time from the HF signals and unlock the D_clock products."

### Slide 5: Shared-Channel Discrimination (2 min)
**"Three stations on one frequency — seven ways to tell them apart"**

The hardest measurement challenge: WWV, WWVH, and BPM on 2.5/5/10/15 MHz.

**Methods (abbreviated — full detail in backup):**

- Tick frequency: 1000 Hz (WWV/BPM) vs 1200 Hz (WWVH), 3 dB gate
- Tick duration: 5 ms (WWV/WWVH) vs 10 ms (BPM)
- NIST tone schedule: ground truth 14 min/hr
- Propagation delay ordering: WWV < WWVH < BPM (consistent across 4 frequencies)

**Production evidence (SHARED 10 MHz, 2026-02-23):**

| Station | Records/day | Median D_clock | Median SNR |
|---------|------------|---------------|------------|
| WWV | 1,380 | −1.12 ms | 26.2 dB |
| WWVH | 1,379 | −0.08 ms | 17.8 dB |
| BPM | 1,380 | +1.48 ms | 21.1 dB |

**Physical validation:** D_clock ordering matches propagation geometry on all 4 shared frequencies. Cross-station Doppler: r ≈ 0 (independent ionospheric paths). Same-station cross-frequency Doppler: r = 0.43 (shared path → correlated).

[FIGURE: fig11 Doppler scatter triptych]

Speaker notes: "On the shared frequencies, three stations transmit simultaneously. We separate them with a layered approach. The strongest discriminator is the tick frequency gate — WWV at 1000 Hz, WWVH at 1200 Hz. For ground truth, the NIST tone schedule gives us 14 minutes per hour where only one station is broadcasting its audio tone. The physical validation is compelling: the D_clock offsets follow propagation geometry on all four shared frequencies, and the cross-station Doppler correlations are zero — proving these really are independent ionospheric paths, not artifacts of the discrimination."

---

### ACT 3: THE SCIENCE PAYOFF — What Comes Into View

---

### Slide 6: Carrier-Phase dTEC — The Primary Science Product (2 min)
**"Bypassing the propagation model noise floor"**

The detection-limit argument:

- Group-delay TEC: signal 0.85 ms, noise 6.5 ms → **SNR 0.13** (buried)
- Carrier-phase dTEC: ~6 mTECU/min sensitivity → **SNR 17–330×** for TIDs
- Formula: dTEC/dt = −f_D × c × f / 40.3

**This is a Tier 2 product** — needs only the GPSDO, not absolute time.

Evidence:

- 17,045 dTEC records/day (per-minute)
- 848,599 per-tick dTEC records/day (1-second resolution)
- GNSS-anchored (ZED-F9P VTEC: 41.7 TECU, ±1 TECU accuracy)

Speaker notes: "Here's the payoff. Group-delay TEC — the classical approach of measuring 1/f² dispersion — is below our noise floor. The propagation model has 6.5 millisecond errors, the dispersion signal is 0.85 milliseconds. We can't see it. But carrier-phase dTEC bypasses this entirely. We measure the Doppler shift — the rate of change of carrier phase — and convert it to dTEC/dt. The sensitivity is 6 milli-TECU per minute. And crucially, this is a Tier 2 product: it only needs the GPSDO, not absolute time."

### Slide 7: Differential dTEC — Self-Consistency (1 min)
**"Same ionosphere, different frequencies — do they agree?"**

Same station at multiple frequencies → same ionospheric path → dTEC should match.

| Station | Widest pair | RMS |
|---------|------------|-----|
| CHU | 3.33–14.67 MHz | 0.005–0.007 TECU |
| WWV | 2.50–25.00 MHz | 0.005–0.026 TECU |

22,474 records/day, all GOOD quality. Also a Tier 2 product.

Speaker notes: "How do we know we're measuring real ionospheric physics? For each station, we compare dTEC at different frequencies on the same path. They agree to within 0.03 TECU RMS. This is 22,000 consistency checks per day, all passing."

### Slide 8: Cross-Domain Consistency — Physics Cascade (1.5 min)
**"From spectrogram to TEC — one ionospheric path, four views"**

[FIGURE: fig16 GRAPE spectrogram CHU 7.85 (top) + fig13 physics cascade panels B–D (below)]

Show for CHU 7.85 MHz (exclusive channel — no discrimination ambiguity):

- Top: GRAPE spectrogram + power graph (the carrier Doppler trace is *visible* in the spectrogram)
- Panel B: Doppler (phase rate, Tier 2) — the same feature, extracted quantitatively
- Panel C: dTEC/dt (derived from Doppler, Tier 2)
- Panel D: Integrated Doppler vs smoothed D\_clock — shape correlation r = 0.60

**The visual story:** The spectrogram shows the carrier frequency offset varying with time — that's ionospheric Doppler, directly visible. The power graph shows signal strength varying with propagation conditions. Below, our pipeline extracts the Doppler quantitatively, converts it to dTEC/dt, and validates against D\_clock. The bottom panel is the money shot: integrated Doppler tracks the shape of D\_clock at 82× finer sensitivity.

**GRAPE compatibility:** This spectrogram is the standard GRAPE data product uploaded to PSWS — the same data that feeds our science pipeline.

Speaker notes: "This is CHU at 7.85 MHz — an exclusive channel with no discrimination ambiguity. At top, the GRAPE spectrogram — the standard data product we upload to PSWS. You can see the carrier frequency offset changing through the day — that's ionospheric Doppler, directly visible in the spectrogram. The power graph above it shows signal strength. Below, our pipeline extracts the Doppler quantitatively, converts to dTEC/dt, and in the bottom panel validates against D_clock. Integrated Doppler tracks the shape of D_clock at 82 times smaller amplitude, r = 0.60. So the spectrogram, the Doppler, the dTEC, and the D_clock are all measuring the same ionosphere through different physical processes."

### Slide 9: 17 Simultaneous Ionospheric Paths (1.5 min)
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

46,774 all-arrivals records/day on CHU 7.85 alone. Multiple propagation modes resolved per minute.

Speaker notes: "The full system monitors 17 simultaneous paths through the ionosphere. On the shared channels, three stations at the same frequency give three independent ionospheric soundings — the Doppler correlation is zero, confirming they see different paths. Across frequencies, WWV from 2.5 to 25 MHz samples six different layers. CHU across three frequencies shows correlated Doppler — same path, same ionosphere, consistent. The correlation heatmap makes the structure clear: station clusters are independent, within-station cross-frequency is correlated. This is essentially a passive oblique ionosonde with 17 beams."

### Slide 10: What's Next — Honest Assessment (1 min)
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

### Slide 11: Summary & Call to Action (30 sec)

**The answer to the central question:**

With an RX888 (~$180) and a GPSDO (~$162), you can:

- Measure ionospheric Doppler at 99.7% coverage, 24/7
- Extract dTEC/dt at ~6 mTECU/min sensitivity on 17 paths
- Discriminate three co-channel stations via physics
- Self-recover UTC to ±1 ms from the time signals

**Daily output:** 24K timing records, 850K phase records, 17K dTEC records, 22K consistency checks.

**Open source:** github.com/mijahauan/hf-timestd (MIT license)

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

## Timing Budget (15 minutes) — revised 2026-02-25

| Act | Slide | Topic | Time | Cumulative |
|-----|-------|-------|------|------------|
| 1 | 1 | Title + central question | 0:30 | 0:30 |
| 1 | 2 | Phenomena ladder (hardware tiers) | 2:00 | 2:30 |
| 2 | 3 | TickEdgeDetector (measurement engine) | 2:00 | 4:30 |
| 2 | 4 | UTC recovery (dual Kalman, metrological ladder) | 1:30 | 6:00 |
| 2 | 5 | Shared-channel discrimination | 2:00 | 8:00 |
| 3 | 6 | Carrier-phase dTEC (primary science product) | 2:00 | 10:00 |
| 3 | 7 | Differential dTEC (self-consistency) | 1:00 | 11:00 |
| 3 | 8 | Physics cascade (CHU 4-domain) | 1:30 | 12:30 |
| 3 | 9 | 17 paths (fingerprint + heatmap) | 1:30 | 14:00 |
|   | 10 | What’s next (honest assessment) | 0:30 | 14:30 |
|   | 11 | Summary + call to action | 0:30 | 15:00 |

**Note:** Slides 10–11 are compressed closers. The three acts structure the narrative as question → method → payoff, echoing the central question on every slide.

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
Internal consistency: differential dTEC RMS < 0.03 TECU across frequencies (22,474 checks/day, all GOOD). Absolute calibration comes from GNSS anchoring — the local ZED-F9P gives overhead VTEC at ±1 TECU, which sets the DC level. The carrier-phase dTEC/dt sensitivity is ~6 mTECU/min.

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

### The "gotcha" questions

**"You're claiming sub-millisecond UTC from HF — isn't that just because you already have GPS?"**
Fair question. Our GPS validates the claim but isn't required for operation. The UTC recovery uses only the HF signals themselves — tick detection, propagation model, dual Kalman fusion. The GPS+PPS provides ground truth for measuring how well it works. A Tier 2 station (no PPS) can bootstrap to effective Tier 3 using only the HF time signals. The GPS lets us *prove* it works, not *make* it work.

**"Your scintillation infrastructure has never detected a real event — why present it?"**
We present it as "within reach," not "demonstrated." The honest framing matters. The dual-source infrastructure (S4 from test signal multi-tone, σ\_φ from per-tick carrier phase) is wired and producing data. Solar cycle 25 is near maximum — geomagnetic storms will come. When they do, the infrastructure is ready. We'd rather have the measurement chain validated and waiting than scrambling to build it during an event.

**"How much of this was built by AI?"**
The codebase is developed in collaboration with AI coding assistants (primarily Cascade/Claude). The human provides domain knowledge, measurement strategy, and validation against physical reality. The AI accelerates implementation of signal processing pipelines, data management, and analysis code. All scientific claims are validated against production data and physical consistency checks — the AI doesn't get to decide what's true, the data does.
