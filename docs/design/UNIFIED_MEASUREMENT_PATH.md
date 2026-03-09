# Unified Measurement Path

**Date:** March 9, 2026
**Status:** DESIGN — no code changes yet
**Author:** Michael James Hauan (AC0G) / Cascade

---

## Problem Statement

The current `MetrologyEngine.process_minute()` forks into two fundamentally
different code paths depending on whether GPS+PPS is present:

- **RTP mode** (`is_rtp_authority=True`): calls `_measure_tone_at_known_time()`
  per-second with a fixed ±50–100 ms search window.  Does not use the physics
  model's adaptive uncertainty.
- **Fusion mode** (`is_rtp_authority=False`): calls
  `tone_detector.process_samples()` with a model-informed adaptive search window.

This means:

1. RTP mode does not exercise the same algorithm that Fusion mode depends on.
   We cannot validate Fusion-mode metrology using GPS+PPS ground truth because
   the code paths are different.
2. RTP mode uses a fixed search window that is wider than necessary when the
   physics model is confident (wastes sensitivity) and may be too narrow under
   disturbed conditions.
3. The tick edge detector (robust median of ~57 front-edge detections) runs in
   both modes, but the per-second matched filter correlator uses different code.

### Corrected terminology

- **GPSDO**: Always present in both modes.  Disciplines the ADC clock.
  Provides the "steel ruler" — every sample is exactly 1/fs apart.
  Sample-to-sample and second-to-second timing is exact in both modes.
- **GPS+PPS**: Present in RTP mode only.  Tells radiod the UTC of a specific
  sample (RTP_TIMESNAP).  Places the steel ruler on the UTC timeline.
- **Fusion mode**: GPSDO present, GPS+PPS absent.  The steel ruler exists
  with full precision, but its UTC placement must be reconstructed from the
  time signals themselves.  **This is the primary objective of hf-timestd.**
- **RTP mode**: GPSDO + GPS+PPS.  The ruler is placed by GPS+PPS.
  The HF signal analysis runs identically but its UTC estimate is compared
  against GPS+PPS ground truth rather than being taken as authoritative.

---

## Design Principle

**One algorithm, two uses.**

```
                    ┌─────────────────────────────┐
                    │  Raw IQ + GPSDO steel ruler   │
                    └──────────────┬────────────────┘
                                   │
                                   ▼
                    ┌─────────────────────────────┐
                    │  Unified Measurement Path     │
                    │  • Tick edge ensemble          │
                    │  • Per-second matched filter   │
                    │  • Adaptive search window      │
                    │  • Physics likelihood weight   │
                    │  • Multipath catalog            │
                    └──────────┬──────────┬─────────┘
                               │          │
                    ┌──────────▼──┐  ┌────▼──────────┐
                    │  UTC estimate │  │  Arrival      │
                    │  (D_clock)    │  │  catalog      │
                    │  → timing     │  │  → physics    │
                    └──────┬───────┘  └───────────────┘
                           │
              ┌────────────┼────────────┐
              │ Fusion mode │ RTP mode   │
              │ D_clock is  │ D_clock is │
              │ authoritative│ compared   │
              │ → chrony SHM│ to GPS+PPS │
              │             │ → residual │
              └─────────────┴────────────┘
```

The fork between modes occurs **after** the measurement, not before.  All signal
processing — detection, adaptive windowing, false-positive gating, multipath
resolution — is identical.  RTP mode adds a verification step; Fusion mode uses
the result directly.

---

## The Steel Ruler in Fusion Mode

Even without GPS+PPS, the GPSDO-locked ADC provides:

- **Exact interval timing**: If tick N is at sample S₁ and tick N+1 is at
  sample S₂, the time between them is exactly (S₂ − S₁) / fs.  No drift,
  no jitter.
- **Minute-scale coherence**: All 57 ticks in a minute are on the same ruler.
  The tick edge ensemble's robust median exploits this — averaging 57
  independent measurements of the same quantity.
- **Cross-channel coherence**: All 9 channels share the same GPSDO clock.
  Sample indices across channels are time-aligned.

What Fusion mode lacks is the **zero point**: which UTC second does sample 0
correspond to?  This is what the reconstruction solves.

The tick edge detector already works in this framework.  It finds tick
front-edges at exact sample indices.  The per-second matched filter correlator
should work the same way — using the GPSDO steel ruler for precise relative
timing, with the adaptive search window constraining where to look based on the
current UTC estimate and physics model.

---

## Adaptive Search Window

### Why It Matters

The matched filter correlator searches a window of ±W ms for the correlation
peak.  In a noise-only region with coherence length τ_c ≈ 55 ms (for the
bandpass-filtered measurement region), the number of independent noise samples
is N ≈ 2W / τ_c.  The expected peak-to-median ratio of N independent Rayleigh
samples scales as √(2·ln(N)):

| Window (±W ms) | N indep. | Expected noise peak | False positive risk |
|-----------------|----------|---------------------|---------------------|
| ±500            | ~18      | ~6.2 dB             | High — noise peaks cross 8 dB threshold |
| ±100            | ~3.6     | ~3.6 dB             | Moderate — current fixed window |
| ±50             | ~1.8     | ~2.4 dB             | Low |
| ±15             | ~0.5     | ~1.2 dB             | Near zero — real signal stands out clearly |
| ±5              | ~0.2     | ~0.5 dB             | Negligible |

**A narrower window directly improves weak-signal sensitivity.**  A real tone
at 4 dB correlation SNR is invisible in a ±100 ms window (buried under noise
peaks at 3.6 dB) but clearly detected in a ±15 ms window (noise peaks only
reach 1.2 dB).

More detected stations and frequencies → more independent timing observables →
better UTC reconstruction.  Improving weak-signal sensitivity on marginal
channels (CHU 3330 kHz, BPM 10000 kHz) by narrowing the search window adds
these channels to the fusion without changing any threshold constants.

---

### The Three Inputs to Window Width

The final search window for a given (station, frequency) pair is determined
by three independent inputs.  Understanding each — what governs it, what can
corrupt it, and what protects it — is essential.

#### Input 1: Physics Model Prior (`model_uncertainty_ms`)

Source: `HFPropagationModel.predict()` via `ArrivalPatternMatrix`.

Recomputed each minute from external ionospheric data.  **Not influenced by
detections.**  This is a prior, not a posterior.

| Model tier   | Source                     | Typical 3σ | What sets it                          |
|-------------|----------------------------|-----------|---------------------------------------|
| WAM-IPE     | NOAA real-time forecast     | ±8–15 ms  | Forecast age, spatial resolution      |
| IRI-2020    | Climatological model        | ±15–25 ms | Season, solar cycle, geomagnetic Kp   |
| Parametric  | Chapman layer + solar zenith| ±20–40 ms | Geometry and time of day only         |
| Vacuum×1.15 | Last resort                 | ±45 ms    | Fixed constant                        |

Per-station floors account for known model biases:

| Station | Floor (3σ) | Reason                                      |
|---------|-----------|----------------------------------------------|
| WWV     | ±15 ms    | Colorado paths, well-calibrated IRI           |
| WWVH    | ±15 ms    | Hawaii paths, well-calibrated IRI             |
| CHU     | ±100 ms   | Ottawa→Missouri: IRI systematic error ~70 ms  |
| BPM     | ±15 ms    | Default                                       |

**Cannot be corrupted by detections.**  Can only be wrong if the external data
source is corrupted (bad WAM-IPE file, stale IRI coefficients).  The tiered
fallback ensures graceful degradation — if WAM-IPE is unavailable, IRI is used;
if IRI fails, parametric; parametric cannot fail.

#### Input 2: Tracked Variance (`BroadcastWindowState`)

Source: `ArrivalPatternMatrix._broadcast_windows[station, freq]`.

Each (station, frequency) pair maintains a running exponentially-smoothed
variance of observed deviations from the model prediction:

```
variance ← 0.9 × variance + 0.1 × deviation²     (α = 0.1, ~10-min τ)
```

The tracked window only narrows when **confidence ≥ 0.8**, where confidence
is the product of two factors:

```
snr_factor = min(1.0, snr_db / 20.0)              → 1.0 requires SNR ≥ 20 dB
consistency_factor = min(1.0, obs_count / 10.0)    → 1.0 requires ≥ 10 obs
confidence = snr_factor × consistency_factor       → 0.8 requires both
```

When confidence ≥ 0.8:

```
proposed_3σ = 3 × √variance + 5 ms (floor margin)
window = clamp(proposed_3σ, min = 5 ms, max = initial_50 ms)
```

**What feeds it:** `record_detection()` is called from within
`validate_detection()` — i.e., only for detections that pass the current
search window bounds.  Detections outside the window are not recorded.

**What governs narrowing:** Requires ≥10 consecutive detections with
SNR ≥ 16 dB (to reach confidence 0.8).  A single observation cannot trigger
narrowing.  The exponential smoothing means each new observation only shifts
the variance by 10%.

**What governs widening:** Any validated detection with a large deviation
immediately inflates the variance.  If the window is narrow (σ = 2 ms) and a
real detection arrives at deviation = 20 ms (still within the physics model's
±100 ms CHU floor):
`variance ← 0.9 × 4 + 0.1 × 400 = 43.6 ms²` → proposed 3σ = 24.8 ms.
The window snaps wide in one step.

#### Input 3: UTC Estimate Uncertainty (Fusion mode only)

Source: `FusionTimingState.get_search_window_ms()`.

Currently a **coarse two-level switch**, not a continuous adaptation:

| Lock tier    | Window   | Entry criteria                                   |
|-------------|----------|--------------------------------------------------|
| NONE        | ±200 ms  | Initial state                                    |
| PROVISIONAL | ±100 ms  | ≥2 stations, ≥2 minutes, ≥4 measurements, σ<100  |
| REFINED     | ±100 ms  | 10+ min at PROVISIONAL, ≥30 measurements, σ<15   |

In RTP mode this input is zero (GPS+PPS provides UTC to ±50 µs).

---

### How They Combine

In `_add_arrival_to_matrix()`, the final 3σ window is:

```
tracked = BroadcastWindowState.current_uncertainty_ms
model   = HFPropagationModel uncertainty (if confidence > 0.3)

if model is confident:
    adaptive_3σ = min(tracked, max(model, 15 ms))
else:
    adaptive_3σ = tracked

adaptive_3σ = max(adaptive_3σ, per_station_floor)    # CHU: 100, WWV: 15
```

In words: take the **tighter** of tracked and model, but never below the
model's own uncertainty (when confident) or the per-station floor.

For the correlator search window in `process_minute()`:

```
model_unc_1σ = arrival.uncertainty_3sigma_ms / 3.0    # physics model
utc_unc_1σ   = 0.0 (RTP) or fusion_state uncertainty  # UTC estimate
total_1σ     = √(model_unc_1σ² + utc_unc_1σ²)
search_window = clamp(3 × total_1σ, min=5, max=200)
```

In RTP mode, `utc_unc = 0`, so the search window equals the physics model
window.  In Fusion mode, the two uncertainties add in quadrature.  As Fusion
lock tightens and `utc_unc → 0`, the two modes converge to identical windows.

---

### Failure Mode Analysis

#### FM1: Narrowing on a false positive

**Scenario:** A noise correlation peak at timing_error = +3 ms passes the
physics gate (within 5σ of expected).  `record_detection()` is called with
deviation = 3 ms.  The variance tracker records this as a legitimate
low-deviation observation, contributing to a narrow window.

**Protections (existing):**

1. **Confidence gate.**  The tracker requires ≥10 observations with SNR ≥ 16 dB
   to reach the 0.8 threshold before narrowing begins.  A single false positive
   cannot trigger narrowing.  Sustained false positives over 10+ minutes, all
   with SNR > 16 dB, would be required.

2. **Exponential smoothing.**  α = 0.1 means each observation shifts variance
   by only 10%.  A few false positives cannot dominate the running estimate.

3. **Physics gate (5σ cutoff).**  A false positive must land within 5σ of the
   physics model prediction.  In a ±100 ms window, the probability of a random
   noise peak landing within ±25 ms (5σ for σ = 5 ms model) is ~25%.  Not
   negligible, but:

4. **The false positive lands near the *model prediction*, not a wrong location.**
   The tracked variance tracks deviation from the model center.  A false
   positive at +3 ms from predicted says "propagation is stable" — it does not
   pull the window center to a wrong location (the center is always the model
   prediction, recomputed from physics each minute).

**Remaining vulnerability:**  On a dead channel (station below noise floor),
noise peaks with SNR < 16 dB yield `snr_factor < 0.8`, so confidence stays
below 0.8.  The window never narrows.  This is correct.  On a marginal channel
(SNR 10–15 dB), `snr_factor = 0.5–0.75`, confidence peaks at 0.75 — still
below threshold.  The window still does not narrow.  Only channels with
consistent real signal at ≥16 dB SNR can trigger narrowing.

**Assessment:**  Low risk.  The confidence gate effectively limits narrowing to
channels with genuine strong signal.

#### FM2: Failure to widen after conditions change

**Scenario:** The window has narrowed to ±8 ms based on stable nighttime 1F2
propagation.  A geomagnetic storm shifts the ionospheric delay by +25 ms over
a few minutes.  The real signal now falls outside the ±8 ms tracked window.
The detection fails.  `record_detection()` is not called (no validated
detection).  The window stays narrow.  The signal stays outside.  Positive
feedback loop:

```
Window narrow → signal outside → miss → no observation → window stays narrow
     ↑                                                          |
     └──────────────────────────────────────────────────────────┘
```

**Protections (existing):**

1. **Physics model recomputes each minute.**  If the ionosphere shifted, the
   model should reflect this with a wider `model_uncertainty_ms`.  The final
   window is `min(tracked, model)`.  If the model widens from ±15 ms to
   ±40 ms, the final window widens even if tracked is still narrow — **BUT
   only if the model detects the change.**

2. **Per-station floor.**  CHU: ±100 ms; WWV/WWVH: ±15 ms.  The window can
   never go below these regardless of tracked variance.

3. **FusionTimingState reset.**  If the fusion lock detects sustained
   measurement loss, it can reset to NONE (±200 ms wide search).

**Gap in existing protections:**

- The physics model may be **slow to react.**  WAM-IPE updates every 1–2 hours.
  IRI is climatological (does not react to storms at all).  A sudden
  ionospheric event (SID, X-class flare, geomagnetic storm onset) can shift
  delays by 10–30 ms within minutes — far faster than the model updates.

- The tracked variance has **no aging/staleness mechanism.**  Exponential
  smoothing has infinite memory.  If a channel goes quiet for 30 minutes then
  re-opens with different propagation, the tracked variance still reflects
  conditions from 30 minutes ago.

- There is **no "consecutive miss" detector** to force a reset.  A channel
  that stops producing detections is invisible to the tracker — it simply
  stops updating, frozen at its last state.

**This is the most dangerous failure mode.**  It causes silent loss of a
channel, with no diagnostic signal except the absence of detections (which
could also mean the station is off-air, the frequency is below MUF, etc.).

#### FM3: Circular narrowing (positive feedback between tracker and gate)

**Scenario:**  Tracked window narrows → only detections near center pass →
these have small deviations → variance stays low → window narrows further.

This is a real positive feedback loop, but it **converges to a stable point:**
the model-predicted delay ± the actual ionospheric jitter (floored at 5 ms).
If the real signal is within ±5 ms of the model, this is correct behavior.
If the signal drifts away, this becomes FM2.

**Assessment:**  Not independently dangerous.  Subsumed by FM2.

---

### Required Safeguards (new, to be implemented)

The existing protections are sufficient for FM1 (false-positive narrowing) and
FM3 (circular narrowing).  FM2 (failure to widen on missed detections) requires
three new mechanisms:

#### Safeguard 1: Staleness Decay

If no validated detection arrives for a (station, frequency) pair within
N minutes, the tracked variance exponentially decays back toward the physics
model's uncertainty:

```
minutes_since_last_detection = now - last_detection_time
if minutes_since_last_detection > STALENESS_ONSET_MINUTES:      # e.g., 5
    decay_factor = exp(-0.1 × (minutes_since_last_detection - STALENESS_ONSET_MINUTES))
    tracked_3σ = model_3σ + (tracked_3σ - model_3σ) × decay_factor
```

After 5 minutes of silence, the tracked window starts widening.  After ~25
minutes of silence, the tracked window has returned to the model's width.
This breaks the "narrow and stuck" loop from FM2 without requiring any
detection events.

**Why 5 minutes:**  Ionospheric conditions change on ~10-minute timescales
(MSTIDs, spread-F onset).  A 5-minute staleness onset means we begin widening
before a typical ionospheric shift completes, giving the window time to open
before the signal migrates too far.

#### Safeguard 2: Consecutive Miss Counter

Track the number of consecutive minutes with no validated detection per
(station, frequency).  After K consecutive misses:

```
if consecutive_misses >= MISS_RESET_THRESHOLD:          # e.g., 5
    tracked_3σ = max(tracked_3σ, model_3σ)             # force to model width
    log WARNING: "channel stuck? N consecutive misses, reset to model window"
    consecutive_misses = 0                              # reset counter
```

This is a **hard backstop** in case Safeguard 1's gradual decay is too slow.
It forces the window to at least the physics model width after 5 minutes of
silence.

**Interaction with Safeguard 1:**  Safeguard 1 provides smooth, continuous
widening.  Safeguard 2 provides a hard floor that catches any case where
Safeguard 1 is insufficient (e.g., model itself is too narrow).

**Important subtlety:**  "No validated detection" means no detection passed
both the SNR threshold AND the physics gate.  A channel that is genuinely
off-air (no signal at all) will trigger this, but that is harmless — the wider
window is correct for when the channel re-opens.

#### Safeguard 3: Tracked Must Not Narrow Below Model

The current combination logic takes `min(tracked, model)` — letting the tracked
variance override the physics model downward.  This is architecturally
questionable: the physics model represents a physics-based floor for
ionospheric uncertainty at the current time.  The tracked variance is an
empirical estimate from a small number of recent observations.

**New rule:**  Tracked variance can only narrow the window below the model
width when confidence is **very high** (≥ 0.95) and the observation count is
**large** (≥ 30):

```
if tracked_3σ < model_3σ:
    if confidence >= 0.95 and observation_count >= 30:
        # Strong empirical evidence that conditions are calmer than model predicts
        final_3σ = max(tracked_3σ, BOOTSTRAP_MIN_UNCERTAINTY_MS)
    else:
        # Insufficient evidence to override physics — use model
        final_3σ = model_3σ
else:
    # Tracked is wider than model — use tracked (conditions are rougher)
    final_3σ = tracked_3σ

final_3σ = max(final_3σ, per_station_floor)
```

This means:
- The physics model is the **default floor** for the search window.
- Only sustained strong signal over 30+ minutes can push the window below the
  model's prediction.
- If the model says ±15 ms and observations say ±8 ms, we use ±15 ms unless
  we have very strong evidence.
- If observations say ±25 ms and the model says ±15 ms, we use ±25 ms
  (conditions are rougher than the model predicts — trust the data).

---

### Summary of Window Governance

| Mechanism | Narrows window | Widens window | Cannot corrupt because |
|-----------|---------------|---------------|----------------------|
| Physics model | Better model tier | Worse tier / disturbed | Recomputed from external data each minute |
| Tracked variance | Low deviation over ≥10 high-SNR obs | High deviation in validated detections | Confidence gate (SNR ≥ 16 dB, ≥10 obs) |
| Per-station floor | N/A | Hard floor (CHU: 100, WWV: 15 ms) | Hardcoded constant |
| Absolute floor | N/A | 5 ms minimum | Hardcoded constant |
| **Staleness decay (new)** | N/A | Widens toward model after 5 min silence | Cannot narrow — only widens |
| **Miss counter (new)** | N/A | Hard reset to model width after 5 misses | Cannot narrow — only widens |
| **Model floor rule (new)** | Only with conf ≥ 0.95, ≥30 obs | Default: model is the floor | Protects against small-sample narrowing |
| FusionTimingState | Lock achieved | Lock lost (reset) | Only in Fusion mode |

---

## Unified Measurement Flow

### Per-minute processing (both modes)

```
1. Demodulate IQ → AM envelope (audio_signal)
   [identical in both modes — GPSDO steel ruler]

2. Compute expected delays for all stations
   expected_delays = {station: _predict_geometric_delay(station, t)}
   [identical — physics model, not mode-dependent]

3. Compute adaptive search window per station
   For each station:
     model_uncertainty_ms = arrival_matrix.uncertainty_3sigma_ms / 3.0
     if fusion_mode:
       utc_uncertainty_ms = fusion_state.get_current_uncertainty_ms()
     else:  # RTP mode
       utc_uncertainty_ms = 0.0  # GPS+PPS is ~50 µs, negligible
     search_window_ms = 3.0 * sqrt(model_uncertainty_ms² + utc_uncertainty_ms²)
     search_window_ms = clamp(search_window_ms, min=5.0, max=200.0)

4. Per-second matched filter correlation
   For each second in buffer:
     For each station template:
       Correlate with adaptive search window from step 3
       Record: arrival_ms, snr_db, corr_snr_db, timing_error_ms

5. Tick edge ensemble (TickEdgeDetector)
   detect_edges(audio_signal, iq_samples)
   → robust median of ~57 front-edge detections
   → timing_error_ms, doppler_hz, carrier_phase

6. Physics likelihood weighting
   For each detection:
     likelihood = matrix.detection_likelihood(timing_error_ms, snr_db)
     [continuous 0→1, not binary gate]
     [detections outside 5σ get likelihood ~0.01, not zero]

7. Multipath catalog
   All detections above threshold recorded in L1/all_arrivals
   Earliest clean arrival selected for timing (metrology)
   All arrivals available for mode resolution (physics)

8. Produce L1 measurement
   Uses earliest-arrival timing from step 5 (edge ensemble, front-edge)
   or step 4 (per-second correlator, dominant detection)
   Confidence = f(edge_ensemble_confidence, physics_likelihood, snr)
```

### Mode-specific post-processing (after step 8)

**RTP mode — verification:**
```
ground_truth_utc = GPS_TIME + (rtp - RTP_TIMESNAP) / fs
estimated_utc = metrology reconstruction from steps 4-8
residual_ms = (estimated_utc - ground_truth_utc) * 1000

→ Log residual for validation dashboard
→ Flag if |residual| > validation_threshold_ms
→ This IS the test of whether metrology works
```

**Fusion mode — authoritative:**
```
D_clock = timing_error_ms from best measurement
→ Feed to Kalman filter
→ Update FusionTimingState
→ Drive chrony SHM
```

---

## Multipath: Timing vs Physics Interests

A 2F2 arrival (two ionospheric hops) arrives ~3–6 ms after the 1F2 arrival
(one hop).  For timing, this multipath corrupts the measurement.  For physics,
it is a direct observable of layer height and electron density.

### The tension

| Concern | Timing (metrology) | Physics |
|---------|-------------------|---------|
| Primary arrival | Use it for D_clock | One data point among several |
| Secondary arrivals | Contaminant — bias the correlation peak | Valuable — each mode measures a different path |
| Arrival spread | Widen uncertainty estimate | Measure delay spread = layer height |
| Unresolved multipath | Bias D_clock by 1–3 ms | Below resolution — flag, don't interpret |

### Resolution: shared detection, divergent interpretation

The unified measurement path detects **all** arrivals above threshold.  Then:

**For timing (metrology):**
- Use the **tick edge ensemble** as the primary timing source.  Front-edge
  detection naturally prefers the first arrival (1F), which is the closest
  to the geometric delay.  The robust median of ~57 edges suppresses
  multipath-induced outliers.
- When multipath is detected (CLEAN deconvolution, or per-second arrival
  time spread), **widen the timing uncertainty** but do not discard the
  measurement.
- The per-second matched filter provides supplementary SNR and station
  identification but its timing is treated as secondary (centroid-biased
  by multipath).

**For physics:**
- Catalog all resolved arrivals in `L1/all_arrivals` with:
  `arrival_ms, snr_db, carrier_phase_rad, detection_method, sec_in_minute`
- Run CLEAN deconvolution on dedicated channels (already implemented,
  gated on `is_dedicated_channel=True`)
- Mode assignment is probabilistic: P(mode | delay, frequency, iono_model)
- Differential delay between resolved modes → virtual layer height
- Amplitude ratio between modes → relative absorption

### Practical limit

The current CLEAN implementation found that 5 ms tick templates have ~5 ms
time resolution, while typical 1F2→2F2 multipath is 3–6 ms separation — at
the resolution limit.  For timing purposes, this means multipath is often
**unresolved** (biases the peak but cannot be cleanly separated).

The correct response to unresolved multipath for timing:
1. Trust the tick edge ensemble (front-edge is less sensitive)
2. Widen the uncertainty estimate
3. Record the arrival time spread as a diagnostic

For physics, unresolved multipath below the tick bandwidth is a fundamental
limitation of 5 ms ticks.  The minute marker (800 ms for WWV, 500 ms for CHU)
and carrier phase provide complementary information at different time scales.

---

## What Changes in Code

### `BroadcastWindowState` — add safeguards (prerequisite)

Before the adaptive window can safely replace the fixed ±100 ms window,
the three safeguards from the failure mode analysis must be implemented in
`arrival_pattern_matrix.py`:

**Staleness decay:**  Add `last_detection_time` field to
`BroadcastWindowState`.  In `get_current_uncertainty_ms()`, if
`now - last_detection_time > STALENESS_ONSET_MINUTES`, apply exponential
decay toward the model uncertainty.

**Miss counter:**  Add `consecutive_misses` field.  Increment in a new
`record_miss()` method called from `process_minute()` when no detection
is validated for a (station, frequency).  Reset to 0 in `record_detection()`.
When `consecutive_misses >= MISS_RESET_THRESHOLD`, force window to model width.

**Model floor rule:**  In `_add_arrival_to_matrix()`, change the combination
logic so that tracked variance can only narrow below the model width when
confidence ≥ 0.95 and observation_count ≥ 30.  Otherwise, the model
uncertainty is the floor.

New constants:

```python
STALENESS_ONSET_MINUTES = 5       # begin decay after 5 min silence
STALENESS_DECAY_RATE = 0.1        # per minute beyond onset
MISS_RESET_THRESHOLD = 5          # force reset after 5 consecutive misses
MODEL_OVERRIDE_CONFIDENCE = 0.95  # confidence needed to narrow below model
MODEL_OVERRIDE_MIN_OBS = 30       # observations needed to narrow below model
```

### `MetrologyEngine.process_minute()` — eliminate the fork

The `if self.is_rtp_authority:` branch should be removed.  Both modes run the
same detection pipeline.  The adaptive search window width comes from:

```python
model_unc = arrival_info.uncertainty_3sigma_ms / 3.0  # from physics model
utc_unc = 0.0 if self.is_rtp_authority else self.fusion_state.get_current_uncertainty_ms()
search_window_ms = 3.0 * math.sqrt(model_unc**2 + utc_unc**2)
search_window_ms = max(5.0, min(200.0, search_window_ms))
```

After the detection loop, call `record_miss()` for any (station, frequency)
that had no validated detection this minute.  This feeds Safeguard 2.

### `_measure_tone_at_known_time()` — accept adaptive window

Replace the fixed `SEARCH_WINDOW_MS = max(50.0, min(100.0, ...))` with a
parameter passed from the caller based on the physics model's per-station
uncertainty.

### Post-measurement fork

After the unified measurement path produces an L1 measurement:
- RTP mode: compare against GPS+PPS ground truth, log residual
- Fusion mode: feed to Kalman filter, update chrony SHM

This is a **small** fork — a few lines at the end of `process_minute()`, not a
structural branch in the middle.

### `ArrivalPatternMatrix` — gate → likelihood

Per `METROLOGY_PHYSICS_SPLIT.md`, the binary accept/reject gate becomes a
continuous likelihood weight.  The 5σ hard cutoff is retained as a practical
bound (detections at 5σ still get likelihood ~0.01, not zero, unless they are
truly degenerate).

---

## Validation Strategy

With the unified path, RTP mode directly validates Fusion-mode metrology:

| Metric | What it tells us |
|--------|-----------------|
| Mean residual vs GPS+PPS | Systematic bias in UTC reconstruction |
| Residual std deviation | Random error (ionospheric + detection noise) |
| Per-station residual | Station-specific propagation model quality |
| Residual vs model tier | WAM-IPE vs IRI vs parametric accuracy |
| Residual vs search window | Whether adaptive window is well-calibrated |
| Weak-channel detection rate | Whether narrower windows recover marginal channels |
| Multipath flag vs residual | Whether multipath detection correctly widens uncertainty |
| Staleness decay activations | Whether channels recover after outages |
| Miss counter resets | How often channels get "stuck" and need forced widening |

This is the ground truth test that the current split code paths prevent.

---

## Relationship to Existing Design Documents

- **`TIMING_AUTHORITY_ARCHITECTURE.md`**: Describes the two-mode architecture
  correctly at the system level.  The unified measurement path is the
  implementation that makes the "always run fusion for comparison" promise real.
- **`METROLOGY_PHYSICS_SPLIT.md`**: Identifies the gate→weight change and the
  need to separate timing from physics.  The unified path implements the
  "shared detection, divergent interpretation" principle.
- **`ARRIVAL_PATTERN_MATRIX_ARCHITECTURE.md`**: The matrix provides the adaptive
  search windows.  The unified path is where those windows are actually used.

---

## Implementation Order

1. **Window safeguards in `BroadcastWindowState`** — Staleness decay,
   miss counter, model floor rule.  These are prerequisites — the adaptive
   window is not safe to deploy without them.  Low risk (only widens
   windows, never narrows).  Can be deployed and validated independently
   before any other change.

2. **Pass adaptive window to `_measure_tone_at_known_time()`** — Replace
   fixed `SEARCH_WINDOW_MS` with per-station parameter from physics model.
   Low risk, immediate benefit in both modes.

3. **Eliminate the RTP/Fusion fork in `process_minute()`** — Both modes
   call the same detection loop.  RTP mode adds ground-truth comparison
   at the end.  Medium effort, enables validation.

4. **Gate → likelihood in `ArrivalPatternMatrix`** — Per
   `METROLOGY_PHYSICS_SPLIT.md`.  Detections get continuous weights,
   not binary accept/reject.

5. **Multipath-aware uncertainty** — When CLEAN or per-second spread
   indicates multipath, widen the timing uncertainty for the Kalman filter.
   Physics pipeline records all arrivals.
