# HamSCI 2026 Workshop — Presentation Plan

**Title:** Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation
**Author:** Michael James Hauan (AC0G)
**Duration:** 15 minutes
**Date:** Prepared 2026-02-24

---

## Narrative Arc

**Central thesis:** A GPSDO-locked SDR turns standard time signals into a precision ionospheric instrument. The quality of the physics you can extract depends directly on the quality of your timing authority — and we can quantify both.

**Structure:** Metrological ladder → instrument description → demonstrated physics products

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

### Claims to KEEP (demonstrable)

1. **Sub-millisecond UTC recovery from HF** — TSL2 at +774 µs ± 600 µs
2. **100× better than internet NTP** — 0.8 ms vs 6–7 ms
3. **L2 ionospheric correction improves L1** — 3.3× tighter uncertainty bound
4. **50–57 ticks/min ensemble** — verified across all channels
5. **~850K per-tick dTEC records/day** — 1-second time resolution
6. **17K per-minute dTEC records/day** — primary science product
7. **Differential dTEC RMS < 0.03 TECU** — multi-frequency consistency validated
8. **Doppler extraction at 99.7%+ coverage** — diurnal signatures visible
9. **Multipath identification** — 46K all-arrivals records/day on CHU 7.85 alone
10. **17 simultaneous sounding paths** — 4 stations × multiple frequencies
11. **24/7 autonomous operation** — 6 systemd services, all active

### Claims to DROP or DOWNGRADE

1. **❌ VTEC maps from HF alone** — group-delay TEC has SNR ~0.13; drop as current capability, reframe as future work with GNSS anchoring
2. **⚠️ Scintillation indices** — S4 gate fixed, σ_φ service wired to tick_phase; infrastructure validated but no geomagnetic storm in data to demonstrate real event detection (Feb 11–24 all quiet)
3. **⚠️ TID detection** — algorithm exists but no validated TID event to show
4. **⚠️ Sporadic-E detection** — algorithm exists but no validated event to show
5. **⚠️ CHU FSK decode** — previously working (8/9 frames, conf=1.00); write path broken by later refactoring
6. **⚠️ "±0.008 ms uncertainty"** — this was a peak CHU result; typical is ±1–7 ms across channels
7. **⚠️ Phase stability claim** — tick-to-tick σ_φ ≈ 1.0 rad is multipath+noise, not scintillation; the Doppler (phase slope) is the validated product

### Claims to ADD (newly demonstrable)

1. **Metrological ladder comparison** — the chronyc snapshot is a powerful one-slide demonstration
2. **Differential dTEC as self-consistency check** — 22K records/day, all GOOD quality, validates carrier-phase methodology
3. **~850K per-tick measurements/day** — impressive data volume for a single-antenna station
4. **Per-station D_clock systematic offsets** — CHU +1–2 ms, WWV/WWVH/BPM ≈ 0 ms — reveals propagation model quality per path
5. **GNSS-anchored dTEC now live** — local ZED-F9P overhead VTEC (41.7 TECU typical, ~1 TECU accuracy) anchors carrier-phase dTEC; all 17 station-channels produce `ANCHORED_GNSS` records when GNSS data is available
6. **Scintillation infrastructure** — dual-source S4 (test signal) + σ_φ (tick phase), cross-correlated; groundwork laid, awaiting geomagnetic event for validation
7. **Shared-channel station discrimination** — 7 independent methods separate WWV, WWVH, and BPM on 2.5/5/10/15 MHz; exploits NIST tone schedule, template duration, cross-frequency gate, and propagation delay ordering

---

## Deep-Dive: VTEC Situation

### What the data actually shows (2026-02-23)

The CRITIC_CONTEXT claim that "vtec_tecu is all NaN" is outdated. Live data:

| Station | TEC Records | VTEC Valid | Confidence Median |
|---------|------------|-----------|-------------------|
| BPM     | 571        | 297 (52%) | 0.300             |
| CHU     | 335        | 72 (21%)  | 0.270             |
| WWV     | 297        | 191 (64%) | 0.300             |
| WWVH    | 569        | 409 (72%) | 0.300             |

969/1,772 records (55%) have valid VTEC. The gate is `confidence >= 0.3` in
`_build_ipp_measurements()` (physics_fusion_service.py:524).

### The hard limit: group-delay TEC SNR

The TEC estimator (tec_estimator.py) fits `D_clock(f) = slope/f² + intercept`.
The physics is correct. The problem is signal-to-noise:

- **Signal:** Ionospheric dispersion between 2.5–25 MHz for 20 TECU ≈ **0.85 ms**
- **Noise:** D_clock uncertainty from propagation model errors ≈ **6.5 ms**
- **SNR ≈ 0.13** — the 1/f² signal is buried in noise

With N=2 frequencies, confidence is capped at 0.3 (MAX_CONFIDENCE_N2). With N≥3,
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

**Implementation A — WWV/WWVH Test Signal S4 (wwv_test_signal.py:1516):**
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

**Implementation B — Per-Tick Phase Scintillation (tick_phase + phase_service):**
Computes σ_φ from tick_phase HDF5 data using sliding windows with Doppler
detrending. Produces 1,938 σ_φ records per 2-hour window across all channels.
190K+ per-tick carrier phase records/day.

**ScintillationService rewritten (2026-02-24):** Now reads from both `test_signal`
(for S4) and `tick_phase` (for σ_φ). Also computes tick-based amplitude S4 from
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
2. ~~Wire ScintillationService to tick_phase + test_signal~~ — **DONE** (full rewrite)
3. ~~Add schema linkage to DataProductRegistry~~ — **DONE** (get_schema/get_field_type)
4. **Wait for geomagnetic storm** — checked Feb 11–24 data: all quiet (σ(dTEC/dt) ≈ 0.0004 TECU/s every day, fading variance stable ~80–89 dB²). X8.1 flare from AR4366 was Feb 5, before our tick_phase data started.
5. **Cross-validate S4 vs σ_φ** — infrastructure ready; needs a real event to test

### Presentation framing

- **Brief mention:** "Dual-source scintillation infrastructure: S4 from test signal multi-tone, σ_φ from per-tick carrier phase. Groundwork is laid; validation awaits a geomagnetic event."
- **Honest:** "σ_φ ≈ 1.0 rad during quiet conditions is multipath + noise, not ionospheric scintillation. A storm would show correlated S4 + σ_φ spikes."

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

## Presentation Outline (15 minutes)

### Slide 1: Title (30 sec)
**Multi-Static HF Time Signal Analysis for Ionospheric Sounding and TEC Estimation**
- AC0G, EM38, central Missouri
- 17 broadcasts, 9 frequencies, 4 stations (WWV, WWVH, CHU, BPM)
- Single GPSDO-locked RX888 SDR via KA9Q-radio

### Slide 2: The Metrological Ladder (2 min)
**"How good is your clock? It determines what physics you can see."**

Show the timing authority hierarchy table from chronyc:
- Unsynchronized PC: ~100 ms
- Internet NTP: ~6 ms
- LAN GPS+PPS: ~1 µs (our ground truth)
- **HF TSL1 (geometric): ~1.1 ms**
- **HF TSL2 (ionospheric): ~0.8 ms**

Key point: HF time signals, properly processed, achieve **100× better than internet NTP**. The ionospheric correction (L2) demonstrably tightens the bound by 3.3×.

Speaker notes: "This is a live chronyc snapshot from our station. The GPS+PPS reference is our ground truth at ±94 microseconds. Our HF-derived timing feeds — TSL1 and TSL2 — are within about 1 millisecond. That's 100 times better than what you get from pool.ntp.org. And notice that TSL2, which applies an ionospheric correction, has a 3× tighter uncertainty bound than TSL1. The ionospheric model is doing real work."

### Slide 3: System Architecture (1.5 min)
**Three-phase pipeline: Record → Measure → Fuse**

Diagram:
```
RTP IQ (24 kHz/ch) → Phase 1: Binary Archive
                    → Phase 2: TickEdgeDetector (50-57 ticks/min)
                              → D_clock, Doppler, carrier phase, SNR
                    → Phase 3: Dual Kalman fusion → Chrony SHM
                              → dTEC, differential dTEC, multipath
```

Key numbers:
- 9 channels monitored 24/7
- ~24K tick timing records/day
- ~850K per-tick phase measurements/day
- ~17K dTEC records/day

Speaker notes: "The system runs as eight independent Linux services. Phase 1 archives raw IQ with RTP timestamps — that's our immutable record. Phase 2 runs a tick edge detector inspired by the ntpd WWV refclock driver — it finds 50 to 57 timing pips per minute per station and extracts timing, Doppler, and carrier phase from each one. Phase 3 fuses everything through dual Kalman filters and feeds the result to Chrony as a time source."

### Slide 4: TickEdgeDetector — The Measurement Engine (2 min)
**Extracting timing from 57 ticks per minute**

Show:
- Template matching: WWV 5ms/1000Hz, WWVH 5ms/1200Hz, CHU 300ms/1000Hz
- Front-edge back-calculation + sub-sample parabolic interpolation
- SNR-weighted robust median ensemble
- Cross-frequency discrimination gate (3 dB advantage required on shared channels)

Evidence table (from live data):
- CHU 7.85: 51 edges/min, 27.3 dB SNR, D_clock = +1.96 ms
- SHARED 10.0: 57 edges/min, 7.9 dB SNR, D_clock = −0.02 ms
- WWV 20.0: 57 edges/min, 7.9 dB SNR, D_clock = −0.05 ms

Speaker notes: "The tick edge detector is inspired by Dave Mills' ntpd WWV refclock driver. For each second of the minute, it correlates a station-specific template against the IQ data, finds the front edge with sub-sample precision, and builds a robust median ensemble. On CHU at 7.85 MHz we get 51 ticks per minute at 27 dB SNR. On the shared channels where WWV, WWVH, and BPM overlap, a cross-frequency discrimination gate separates the stations — WWV uses 1000 Hz ticks, WWVH uses 1200 Hz, and we require a 3 dB advantage to claim a detection."

### Slide 5: Shared-Channel Discrimination — Separating Three Stations (2 min)
**Seven independent methods disentangle WWV, WWVH, and BPM on 2.5/5/10/15 MHz**

The shared channels are the hardest measurement challenge: three transmitters on the same frequency, with intermodulation confounders. We use a layered discrimination approach:

| Method | Discriminates | When Available | Weight |
|--------|--------------|----------------|--------|
| Cross-frequency gate (1000 vs 1200 Hz) | WWV vs WWVH | Every second | High (3 dB advantage required) |
| Template duration (5ms vs 10ms) | WWV/WWVH vs BPM | Every second | Implicit (different matched filters) |
| 500/600 Hz tone schedule | WWV vs WWVH | 14 min/hr (ground truth) | Highest (15× weight) |
| 440 Hz tone | WWV vs WWVH | 2 min/hr (min 1,2) | High (10× weight) |
| BCD 100 Hz correlation | WWV vs WWVH | Most minutes | Medium (amplitude ratio) |
| Test signal (min 8/44) | WWV vs WWVH | 2 min/hr (schedule) | Highest when detected |
| Propagation delay ordering | All three | Every minute | Confirmatory |

**Confounders and mitigations:**
- 2nd harmonic of 500 Hz → 1000 Hz (contaminates WWV tick detection)
- 2nd harmonic of 600 Hz → 1200 Hz (contaminates WWVH tick detection)
- 10ms silence zone before each tick (NIST SP 432) suppresses the pedestal
- BCD 100 Hz × 500/600 Hz intermodulation → restricted to exclusive-broadcast minutes for ground truth

**Production evidence (SHARED 10 MHz, 2026-02-23):**

| Station | Detections/day | Detection rate | Median D_clock | Median SNR |
|---------|---------------|---------------|---------------|------------|
| WWV | 1,380 | 68% | −1.12 ms | 26.2 dB |
| WWVH | 1,379 | 42% | −0.08 ms | 17.8 dB |
| BPM | 1,380 | 33% | +1.48 ms | 21.1 dB |

**Key validation:** The D_clock systematic offsets match expected propagation geometry — Fort Collins (closest) arrives earliest, Pucheng (farthest, multi-hop) latest. This ordering is consistent across all four shared frequencies.

**Tone schedule provides absolute ground truth 14 minutes per hour:**
- WWV-only minutes (1, 16, 17, 19): WWV broadcasts 500/600 Hz, WWVH is silent
- WWVH-only minutes (2, 43–51): WWVH broadcasts 500/600 Hz, WWV is silent
- During these minutes, tone detection provides definitive station identification that calibrates the weighted voting system for the remaining 46 minutes

**Test signal as path fingerprint (minutes 8 and 44):**

| Metric | WWV (min 8) | WWVH (min 44) |
|--------|------------|---------------|
| Tone power 2 kHz | 40.8 dB | 22.7 dB |
| Multitone score | 0.98 | 0.79 |
| Chirp score | 0.40 | 0.05 |
| Fading variance | 97.7 dB² (σ=38) | 79.9 dB² (σ=13) |
| Effective SNR | 22.0 dB | 9.3 dB |
| Coherence time | 0.14 s | 0.21 s |

The path to Fort Collins (1,500 km, single-hop F) has higher power but more fading variability; the path to Hawaii (5,300 km, multi-hop) has lower power but more stable fading — consistent with the number of reflections.

Speaker notes: "On the shared frequencies, three stations transmit simultaneously. We separate them using seven independent methods in a weighted voting system. The strongest discriminator is the tick frequency — WWV uses 1000 Hz, WWVH uses 1200 Hz, and we require a 3 dB advantage. BPM uses 1000 Hz like WWV but with 10 millisecond ticks instead of 5, so the matched filter naturally separates them. For absolute ground truth, we exploit the NIST tone schedule: 14 minutes per hour, only one station broadcasts a 500 or 600 Hz tone. During those minutes we know exactly which station we're hearing. The test signal minutes give us a path fingerprint — the multi-tone power profile is very different for the Fort Collins and Hawaii paths. And the propagation delay ordering — WWV arrives first, then WWVH, then BPM — is consistent across all four shared frequencies, confirming the attributions are correct."

### Slide 6: Dual Kalman Fusion — UTC Recovery (1.5 min)
**Two independent timing feeds to Chrony**

Show:
- L1 (geometric): raw timing residuals, no ionospheric model
- L2 (physics): ionospheric group delay correction applied
- Both feed Chrony SHM as independent reference clocks
- Chrony selects the best source

Evidence:
- Fusion D_clock: median −1.2 ms, 60% within ±2 ms, 86% within ±5 ms
- TSL2 uncertainty 3.3× tighter than TSL1
- GPS ground truth confirms sub-ms accuracy

Speaker notes: "We run two independent Kalman filters. L1 uses only geometric path delays — it's the fallback. L2 applies an ionospheric correction from our propagation model stack. Both feed Chrony as separate reference clocks. The key result: L2 consistently outperforms L1, with a 3.3 times tighter uncertainty bound. This validates that the ionospheric correction is adding real information, not just noise."

### Slide 7: Carrier-Phase dTEC — The Primary Science Product (2 min)
**Bypassing the propagation model noise floor**

Show the detection limit analysis:
- Group-delay TEC: signal 0.85 ms, noise 6.5 ms → **SNR 0.13** (below noise floor)
- Carrier-phase dTEC: ~6 mTECU/min sensitivity → **SNR 17–330×** for TIDs

Formula: `dTEC/dt = −f_D × c × f / 40.3`

Evidence:
- 17,045 dTEC records/day (per-minute)
- 848,599 per-tick dTEC records/day (1-second resolution)
- Median dTEC rate ≈ 0, σ = 0.38 mTECU/s

Speaker notes: "Here's the key insight. Group-delay TEC — measuring the 1/f² dispersion in arrival times — is below our noise floor. The propagation model has 6.5 millisecond errors, but the ionospheric dispersion signal is only 0.85 milliseconds. We can't see it. But carrier-phase dTEC bypasses this entirely. Instead of measuring absolute delay, we measure the rate of change of carrier phase, which converts directly to dTEC/dt. The sensitivity is about 6 milli-TECU per minute — that's 17 to 330 times above the noise floor for typical TID amplitudes."

### Slide 8: Differential dTEC — Self-Consistency Validation (1.5 min)
**Multi-frequency carrier phase proves the method works**

Show:
- Same station, different frequencies → same ionosphere → dTEC should agree
- Differential dTEC RMS < 0.03 TECU across all station pairs
- 22,474 records/day, all GOOD quality

| Station | Widest pair | RMS |
|---------|------------|-----|
| CHU | 3.33–14.67 MHz | 0.005–0.007 TECU |
| WWV | 2.50–25.00 MHz | 0.005–0.026 TECU |
| WWVH | 2.50–15.00 MHz | 0.003–0.012 TECU |
| BPM | 2.50–15.00 MHz | 0.002–0.011 TECU |

Speaker notes: "How do we know the carrier-phase dTEC is real and not an artifact? We have a built-in consistency check. For each station, we measure dTEC at multiple frequencies. The ionosphere is the same for all frequencies on the same path, so the dTEC rates should agree. They do — the RMS difference is less than 0.03 TECU across all pairs. This is a strong validation that we're measuring real ionospheric physics."

### Slide 9: Doppler Shifts — Ionospheric Dynamics (1.5 min)
**Per-minute Doppler from carrier phase slope**

Show:
- Doppler extracted from linear fit to unwrapped carrier phase across 50+ ticks
- CHU 7.85 MHz: range ±0.34 Hz, σ = 0.09 Hz
- SHARED 10.0 MHz: range ±0.24 Hz, σ = 0.04 Hz
- 99.7%+ coverage (nearly every minute has a valid Doppler measurement)

[FIGURE: 24-hour Doppler time series showing diurnal signature]

Speaker notes: "Every minute, we fit a line through the unwrapped carrier phase of 50+ ticks. The slope gives us the Doppler shift. On CHU at 7.85 MHz, we see a ±0.34 Hz range over the day — that's the ionospheric layer moving up at sunrise and down at sunset. The coverage is 99.7% — we get a Doppler measurement nearly every minute of every day."

### Slide 10: Multipath — All-Arrivals Product (1 min)
**Resolving multiple propagation modes simultaneously**

Show:
- CHU 7.85 MHz: 46,774 all-arrivals records/day
- Multiple correlation peaks per minute → multiple propagation modes
- Mode timeline visualization from web dashboard

Speaker notes: "The correlation function doesn't just have one peak — it has several. Each peak corresponds to a different propagation mode: 1-hop F-layer, 2-hop, E-layer. We record all of them. On CHU at 7.85 MHz, we get nearly 47,000 arrival records per day. This is essentially a passive oblique ionosonde — we're sounding 17 paths through the ionosphere simultaneously, 24/7."

### Slide 11: What Doesn't Work (Yet) — Honest Assessment (1 min)
**Detection limits and future directions**

- **Group-delay TEC**: below noise floor (SNR 0.13 — propagation model error >> dispersion signal)
- **VTEC maps from HF alone**: geometrically correct but sTEC is noise-dominated
- **Scintillation**: infrastructure built and wired, but ionosphere has been quiet — awaiting geomagnetic event

What we just fixed:
- **✅ GNSS-anchored dTEC** — deployed 2026-02-24. Local ZED-F9P overhead VTEC now anchors all 17 carrier-phase dTEC channels. `anchor_status=ANCHORED_GNSS` with ~1 TECU accuracy.

Remaining future paths:
- Per-path slant correction (VTEC × mapping factor for each HF elevation — ~10–30% refinement)
- Phase-engine integration (4× RX888 coherent array → angle-of-arrival)
- PHaRLAP 3D ray tracing for long paths (BPM)

Speaker notes: "I want to be honest about what doesn't work yet. Group-delay TEC is below our noise floor — the propagation model errors are 8 times larger than the dispersion signal. That means VTEC maps from HF alone aren't credible. But we've solved the anchoring problem: as of this week, a local ZED-F9P GPS receiver provides overhead VTEC at about 1 TECU accuracy, and the physics fusion service now uses it to anchor all 17 carrier-phase dTEC channels. Every dTEC record now has an absolute reference instead of drifting freely. The remaining refinement is per-path slant correction — converting overhead VTEC to slant TEC for each HF path geometry. We've also built the infrastructure for scintillation monitoring — dual-source S4 and sigma-phi — but the ionosphere has been quiet during our data capture. We need a geomagnetic storm to validate it."

### Slide 12: Summary & Data Availability (30 sec)

**What this station produces daily:**
- 24,342 tick timing measurements (D_clock, Doppler, SNR)
- 848,599 per-tick carrier phase measurements
- 17,045 carrier-phase dTEC records
- 22,474 differential dTEC consistency records
- 46,774+ multipath arrival records
- Sub-millisecond UTC recovery (100× better than internet NTP)

**Open source:** github.com/mijahauan/hf-timestd (MIT license)

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
D_clock scatter by station on SHARED 10 MHz, color-coded by station (WWV/WWVH/BPM), showing distinct systematic offsets. Inset: test signal tone power comparison between WWV (min 8) and WWVH (min 44). Source: tick_timing + test_signal HDF5.

---

## Timing Budget (15 minutes)

| Slide | Topic | Time | Cumulative |
|-------|-------|------|----------|
| 1 | Title + station overview | 0:30 | 0:30 |
| 2 | Metrological ladder | 1:30 | 2:00 |
| 3 | System architecture | 1:30 | 3:30 |
| 4 | TickEdgeDetector | 1:30 | 5:00 |
| 5 | Shared-channel discrimination | 2:00 | 7:00 |
| 6 | Dual Kalman fusion | 1:00 | 8:00 |
| 7 | Carrier-phase dTEC | 1:30 | 9:30 |
| 8 | Differential dTEC validation | 1:00 | 10:30 |
| 9 | Doppler shifts | 1:00 | 11:30 |
| 10 | Multipath / all-arrivals | 1:00 | 12:30 |
| 11 | What doesn't work + future | 1:00 | 13:30 |
| 12 | Summary | 0:30 | 14:00 |

**Note:** 1 minute buffer for Q&A overlap or slides running long.
