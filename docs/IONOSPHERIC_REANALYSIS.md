# Ionospheric Reanalysis: Leveraging Physics to Interpret Noisy Data

**Purpose:** Document the governing rationale and implementation methodology of the ionospheric reanalysis service  
**Context:** This is Section 5 of [PHYSICS.md](PHYSICS.md), extracted here for standalone reference  
**Implementation:** `src/hf_timestd/core/ionospheric_reanalysis.py`  
**Execution:** Hourly via `systemd` timer at `:05` past each hour, `nice 19`, `IOSchedulingClass=idle`

---

## 1. The Problem: Mode Misidentification in Real-Time Processing

The real-time propagation mode solver assigns modes by matching measured arrival delays to geometrically computed candidates. This is a purely kinematic approach — it asks *"which mode geometry best explains this delay?"* without asking *"is this mode physically possible right now?"*

The consequence is predictable: at night, when the F2-layer is thin and the critical frequency drops below 5 MHz, the solver still happily labels noise-floor detections at 10 or 15 MHz as `4F2` or `3F2` modes. These are geometrically plausible (the delay matches) but physically impossible (the ionosphere cannot support F-layer propagation at those frequencies under current conditions).

This contamination propagates downstream:

1. **MUF inflation**: The Maximum Usable Frequency is estimated from the highest frequency showing F-layer propagation. Noise detections at 15 MHz labeled `3F2` inflate the MUF to ~17 MHz when the true MUF might be 8 MHz.

2. **TEC contamination**: The TEC estimator fits a 1/f² dispersion model across frequencies. Including noise-floor "measurements" that carry no ionospheric information corrupts the fit.

3. **Mode statistics**: Hourly and daily propagation statistics include phantom F-layer modes that never actually occurred.

The root cause is that the real-time solver operates on individual measurements in isolation, with no awareness of the ionospheric state. It cannot distinguish a genuine 15 MHz F-layer reflection from a noise spike that happens to fall at the right delay.

<!-- LIVE: reanalysis-summary -->

---

## 2. The Solution: Offline Physics-Based Reanalysis

The ionospheric reanalysis service addresses this by applying what we *know* about the ionosphere to constrain what we *observe*. It runs hourly as a low-priority offline job, re-examining the previous hour's L2 timing measurements through a physics-based lens.

The governing principle is straightforward: **a propagation mode can only exist if the ionosphere can support it.** The F2-layer reflects a signal only if the signal frequency is below the layer's Maximum Usable Frequency (MUF) for that geometry. The MUF depends on the critical frequency (foF2), which depends on solar illumination. All of these quantities are calculable from first principles.

---

## 3. The Physics: From Solar Zenith Angle to Mode Validity

The reanalysis applies a chain of physical reasoning, each step grounded in well-established ionospheric physics:

### Step 1: Solar Zenith Angle at the Path Midpoint

The ionosphere is a solar-driven phenomenon. The degree of ionization at any point depends primarily on the solar zenith angle (χ) — the angle between the sun and the local vertical. At the subsolar point χ = 0° and ionization is maximum; at the terminator χ = 90° and ionization drops rapidly.

For each transmitter-receiver path, we compute the geographic midpoint (where the signal reflects off the ionosphere) and calculate the solar elevation at that point for the hour being analyzed:

```
midpoint = great_circle_midpoint(tx_lat, tx_lon, rx_lat, rx_lon)
solar_elevation = solar_position(midpoint, timestamp)
χ = 90° - solar_elevation
```

**Implementation:** `src/hf_timestd/core/solar_zenith_calculator.py`

### Step 2: Critical Frequency Estimation via Chapman Layer Model

The F2-layer critical frequency (foF2) — the highest frequency that can be reflected at vertical incidence — follows the Chapman production function. Under solar illumination, photoionization produces free electrons; in darkness, recombination depletes them. The equilibrium electron density, and hence foF2, depends on cos(χ):

```
foF2 = foF2_noon × cos^0.25(χ)    daytime (solar elevation > 0)
foF2 = foF2_night_floor            deep night (elevation ≤ −18°)
foF2 = interpolated                twilight (−18° < elevation ≤ 0°)
```

Where:
- **foF2_noon ≈ 10.9 MHz**, derived from the 12-month smoothed sunspot number R12 via `foF2_noon ≈ 6 + 0.07·R12` (R12 = 70, a moderate climatological anchor)
- **foF2_night_floor = 3.0 MHz** (residual nighttime ionization)
- The 0.25 exponent is the textbook Chapman-α result: peak electron density Ne ∝ cos χ, and foF2 ∝ Ne^0.5 ∝ cos^0.25 χ (Davies, 1990)

These regimes are framed in solar **elevation** (= 90° − χ): deep night ≤ −18° (astronomical twilight ended), twilight −18° → 0° (linear interpolation, with a 0.6×daytime anchor at the horizon, elevation 0), and daytime > 0°.

This is a climatological estimate — it represents typical conditions, not the exact foF2 at this moment. But it is sufficient to reject clearly impossible modes (e.g., 15 MHz F-layer propagation when foF2 ≈ 4 MHz at night).

#### E-Layer Critical Frequency (foE) and Sporadic-E Branch

In parallel with foF2, the reanalysis estimates the E-layer critical frequency foE from the same solar geometry, using the ITU-R P.1239 / Muggleton (1975) empirical formula:

```
foE = 0.9 × [(180 + 1.44·R12) × cos χ]^0.25     (MHz), with a 0.5 MHz night floor
```

The E layer is strongly Chapman (foE tracks cos^0.25 χ closely) and nearly vanishes after dusk, hence the small residual-ionization floor below the horizon. foE bounds the E-layer/Es-mode MUF used when validating low-elevation `1E` candidates and reclassifying daytime above-MUF detections as possible sporadic-E.

<!-- LOGS: reanalysis | filter: "solar_physics" -->

### Step 3: Oblique MUF from Secant Law

A signal propagating obliquely through the ionosphere can be reflected at frequencies higher than foF2, because the effective path through the layer is longer. The relationship is the **secant law**:

```
MUF_oblique = foF2 × sec(θ_i)
```

Where θ_i is the angle of incidence at the ionospheric layer. For a signal making n hops over a great-circle distance d, reflecting at height h above the Earth (radius R):

```
half_angle = d / (2 × n × R)
sin(elevation) = cos(half_angle) - R × sin(half_angle)² / (R + h)
θ_i = 90° - elevation
```

More hops means steeper incidence (smaller θ_i), which means *lower* oblique MUF. A 1-hop path at 1000 km has a much higher MUF than a 4-hop path over the same distance.

### Step 4: Mode Validation

With the oblique MUF computed for each candidate mode geometry, the validation is a simple inequality:

```
if signal_frequency > oblique_MUF(mode):
    mode is PHYSICALLY IMPOSSIBLE → reject or reclassify
```

Combined with an SNR gate (measurements below 12 dB are likely noise), this eliminates the phantom modes that contaminate the real-time estimates.

When a mode is rejected, the service attempts reclassification — trying higher hop counts (which have steeper angles and thus higher MUF) or, for strong daytime signals above the MUF, flagging as possible sporadic-E.

<!-- LOGS: reanalysis | filter: "mode_validation" -->

---

## 4. TEC Re-Estimation from Cleaned Data

With physically impossible modes removed, the TEC estimation becomes more reliable. The reanalysis uses a refined approach:

**The D_clock Insight:** Each L2 timing measurement contains `raw_arrival_time_ms` (actually D_clock — the timing error after subtracting the propagation model delay). If the propagation model were perfect, D_clock would be identical across all frequencies. The residual frequency-dependent pattern in D_clock *is* the ionospheric dispersion signal:

```
D_clock(f) = D_clock_vacuum + K × TEC / f²
```

Where K = 1.344 ms·MHz²/TECU is the ionospheric dispersion constant.

**Median Aggregation:** Rather than using a single measurement per frequency, the reanalysis takes the *median* D_clock across all valid measurements at each frequency within the hour. This is robust to outliers from occasional mode mis-assignments that survive the physics filter.

**Frequency Deduplication:** The TEC estimator fits T_obs vs 1/f². Multiple measurements at the same frequency map to the same x-value and add noise without improving the fit. The median aggregation naturally produces one data point per distinct frequency.

**Physical Validation:** Results are checked against physical bounds (0-200 TECU) and negative slopes (which indicate mode mixing or measurement pathology) are flagged and forced to zero rather than producing nonsensical negative TEC values.

<!-- LOGS: reanalysis | filter: "tec_reanalysis" -->

---

## 5. Outputs: L3C Propagation Statistics and Reanalyzed TEC

The reanalysis produces two data products:

**L3C Propagation Statistics** (`l3c_propagation_stats_v1` schema):
- Per-station, per-frequency mode probabilities (validated against physics)
- Estimated MUF with confidence
- Mean SNR, observation count, data completeness
- Quality flag (GOOD/MARGINAL/BAD)

**Reanalyzed L3A TEC** (`l3_tec_v1` schema):
- TEC in TECU from cleaned multi-frequency D_clock fit
- Confidence (R² of the 1/f² fit)
- Number of distinct frequencies used
- Dominant propagation mode
- Quality flag

The propagation service API (`/api/propagation/conditions`) now serves three MUF values:
- `muf_realtime_mhz`: Naive estimate from real-time mode assignments
- `muf_reanalyzed_mhz`: Physics-validated estimate from reanalysis
- `muf_estimate_mhz`: Best available (prefers reanalyzed when available)

---

## 6. Why This Works: The Epistemological Argument

The reanalysis exemplifies a general principle in measurement science: **prior knowledge constrains interpretation of noisy data.**

The real-time mode solver treats each measurement as an isolated observation and asks only "what mode geometry fits this delay?" This is the maximum-likelihood approach with a uniform prior — every mode is equally likely. The result is that noise, which is uniformly distributed in delay space, gets assigned to whichever mode geometry happens to be closest.

The reanalysis introduces a *physics-informed prior*: modes that require frequencies above the oblique MUF have zero probability. This is not a statistical assumption — it is a hard physical constraint. The ionosphere *cannot* reflect a 15 MHz signal when foF2 is 4 MHz, regardless of what the delay measurement says.

The improvement is most dramatic at night, when:
- foF2 drops to 3-5 MHz (only low frequencies can use F-layer)
- SNR drops on higher frequencies (signals are absorbed or not reflected)
- Noise-floor detections become a larger fraction of measurements
- The real-time solver has the most opportunity to misclassify noise as F-layer modes

During the day, when foF2 is 8-12 MHz and most frequencies genuinely propagate via F-layer, the reanalysis largely confirms the real-time assignments. This is the expected behavior — the physics constraint is most valuable precisely when the data is most ambiguous.

<!-- LOGS: reanalysis | filter: "hourly_summary" -->

---

## 7. Validation: Evidence from This Installation

The following live data demonstrates the reanalysis in operation on this installation. The key observable is the difference between the naive real-time MUF and the physics-validated reanalyzed MUF.

**When the correction is large** (e.g., real-time says 17 MHz, reanalysis says 8 MHz), it means the real-time pipeline was counting noise-floor detections as F-layer modes. The reanalysis correctly identified these as physically impossible and excluded them.

**When the correction is small or zero**, the real-time mode assignments were already physically consistent — the ionosphere could support the observed modes. This typically occurs during daytime when foF2 is high.

<!-- LIVE: reanalysis-summary -->

<!-- LOGS: reanalysis | filter: "muf_estimate" -->
