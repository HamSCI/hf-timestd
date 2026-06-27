# BPSK PPS edge detection: methods, evidence, and the path to the diff detector

**Status:** Living engineering record.  **Method 2** (matched filter on
Costas-derotated real projection) feeds chrony as **HPPS** on SHM 2.
**Method 5** (per-sample magnitude derivative) is wired to publish as
**HFPS** on SHM 3 (gated by `diff_to_shm_unit`), but that feed is
**disabled in the shipped configuration** — `diff_to_shm_unit` is unset
and the live chrony config consumes only FUSE and HPPS, so HFPS is not
currently in use.  Method 5 showed ~22 ns short-term σ vs ~150 ns and
frequent multi-hundred-µs walks on Method 2, which is why the HFPS path
was built; it remains an opt-in/diagnostic feed pending long-window proof.

**Companion document:** `HF-PPS-CHRONY-TUNING.md` — parameter tuning
within Method 2, including the four-pass Costas threshold journey
of 2026-05-21 and the chrony refclock settings.  This document is
about *algorithm choice*; that one is about *parameter choice given
an algorithm*.

---

## 1. The problem we're solving

A GPS-disciplined oscillator (LB-1421) drives a Turn Island Systems
TS1 BPSK injector that flips a 45.375 MHz carrier's polarity once per
second at the 1 PPS edge.  Between edges the carrier is unmodulated
(no data symbols, per operator confirmation 2026-05-22).  An RX-888
SDR samples the band at 96 kHz IQ via radiod, and our calibrator
recovers the precise time-of-arrival of each polarity flip to feed
chrony as the HPPS SHM refclock (SHM unit 2).

The end goal is a chrony refclock with nanosecond-class short-term
jitter and zero long-term drift, capable of holding HPPS selected
through arbitrary RF and CPU transients.

**Inputs the algorithm receives:**
- Complex IQ samples at 96 kHz (band-limited through radiod's
  ±25 kHz channel filter, so a polarity flip has ~20 µs rise time
  ≈ 2 samples).
- Each batch's first sample carries an RTP timestamp; radiod's
  GPS_TIME / RTP_TIMESNAP mapping turns this into wall-clock UTC.
- Residual carrier offset after radiod's downmix: typically sub-Hz
  from RX-888 LO + GPSDO mismatch.

**Outputs the algorithm produces:**
- A `chain_delay`: the sub-sample offset of the polarity edge from
  the integer GPS second.
- A wall-clock timestamp at the edge, written to chrony's SHM unit 2.

---

## 2. Method 1 — legacy per-sample Δφ calibrator (pre-2026-05-06)

The original calibrator computed per-sample phase increments
`Δφ[n] = arg(s[n]·conj(s[n−1]))` and looked for sign reversals to
locate polarity flips.

**Why it was abandoned:**
- Single-sample noise dominated the Δφ signal.  At the 16 kHz sample
  rate of the time, the per-sample phase noise on TS1's BPSK
  comfortably exceeded the discrete π flip signature.
- No coherent processing gain — every detection decision was made on
  a single sample.

This is `bpsk_pps_calibrator.py` (still in tree as a legacy
fallback, default-off via `use_matched_filter = true`).

---

## 3. Method 2 — Costas + boxcar matched filter on Re(s_rot) (2026-05-06 → present)

The textbook approach for low-SNR BPSK timing.

```
  s_rot[n] = s[n] · exp(−jφ_costas[n])             ← Costas derotation
  I[n]     = Re(s_rot[n])                           ← project to real
  y[n]     = Σ_{k=1..N} I[n+k]   −   Σ_{k=−N..−1} I[n+k]
              (N = SR/2 = 48 000 — half-second boxcar)
  edge     = argmax |y[n]|, sub-sample interp on parabola through |y|
```

**Why we chose it:**
- The half-second boxcar is the optimal matched filter for a
  once-per-second polarity flip in additive Gaussian noise.  Coherent
  integration over N=48 000 samples gives ~47 dB of processing gain
  vs single-sample detection.
- `σ_t ≈ 1 / (2π · B · √SNR)` with the wider ±25 kHz channel filter
  gives sub-µs predicted timing precision.  This is the Cramér-Rao
  lower bound for time-of-arrival estimation with a matched filter
  (B the effective/RMS bandwidth, SNR the post-integration ratio);
  see Kay, *Fundamentals of Statistical Signal Processing: Estimation
  Theory* (1993), ch. 3, and Van Trees, *Detection, Estimation, and
  Modulation Theory, Part I*, for the matched-filter CRLB derivation.

**What we learned the hard way:**

The Costas loop tracks the *carrier phase* of the BPSK signal so that
the polarity ends up in the real axis.  When Costas is locked the MF
output peak is a clean triangle wave around the edges; `Re(MF)`
captures the full signal energy.  But the Costas loop has its own
dynamics:

  * (3a) Loop instability — `dphase_ema` exceeds a threshold and the
    "loop in motion" gate fires, blocking edge acceptance for tens of
    seconds while the loop re-locks.  *Four threshold-tuning passes
    on 2026-05-21* never converged: every time we raised the
    threshold, the BPSK regime drifted past the new value.  See
    `HF-PPS-CHRONY-TUNING.md` §3.3.
  * (3b) Per-restart disambiguation drift — each restart, Costas
    re-acquires at a slightly different operating point φ.  The
    real projection's peak amplitude `cos(θ)·polarity` depends on θ,
    so the MF reports a slightly different `chain_delay` after every
    restart, off by hundreds of µs.  This forces a re-disambiguation
    against T4 (LAN GPS), which itself has moment-to-moment drift.
    Net result: 3 restarts within 5 minutes on 2026-05-21 produced 3
    different chain_delay values drifting 635 µs in total — and the
    tsl3-watchdog made it worse by restarting again whenever chrony
    flagged the discontinuity.
  * (3c) Chain-delay step adoption pathology — once acquired, the MF
    can absorb a wandered sample position into a "step adoption" if
    60 consecutive off-position edges all agree.  Observed
    2026-05-22 ~10:30: a +135 µs walk became a "step adopted -426 ms"
    event, sticking chrony at the wrong calibration for minutes.

The algorithm IS correct for noisy, stable signals.  It is fragile
for the actual TS1 + RX-888 + LB-1421 chain, where the SNR is high
(unhelpful for the matched filter's design point) but the Costas
loop's natural noise regime drifts on the scale of minutes to hours
(triggering thresholds tuned to a quieter regime).

This is the production path today.  The retreat plan if anything
fails: it works most of the time, particularly during quiet hours
(e.g. 07:00–08:00 UTC on 2026-05-22 had 55 of 55 `*` samples with
±6 µs offsets).  But it has these failure modes baked in.

---

## 4. Method 3 — magnitude correlation on raw complex MF (failed: 2026-05-22 ~01:48)

The first attempt at removing Costas's instability.  Same boxcar MF
but on the complex signal directly, peak-pick on the magnitude:

```
  y_complex[n] = Σ_{k=1..N} s[n+k]   −   Σ_{k=−N..−1} s[n+k]
  edge         = argmax |y_complex[n]|
```

**The reasoning** (which was wrong):
- For a polarity flip with stable carrier phase φ, `y_complex` =
  `−2N·A·e^(jφ)` so `|y_complex|` = `2N|A|`, independent of φ.
- Therefore the algorithm is rotation-invariant — no Costas needed.

**What actually happened, 2 minutes after deploy:**
- 01:52–01:53: brief `*` at -3.6 µs offset (the correct answer,
  glimpsed).
- 01:54: chrony saw `?` at -253 ms.
- 01:55 onwards: locked onto a sidelobe 185 ms off the true edge,
  stayed there indefinitely.

**Why it failed:**

The half-second boxcar over a *rotating* signal does NOT sum to a
constant magnitude.  For residual carrier frequency Δf (the real
TS1 + RX-888 chain has sub-Hz Δf after radiod's downmix), the boxcar
is a discrete equivalent of `sin(π·Δf·T) / sin(π·Δf/SR)` evaluated
at T = N/SR.  This goes through nulls at multiples of `SR/N = 2 Hz`
and has spectral sidelobes between nulls.

At any non-zero Δf, the main lobe at the true edge is *suppressed*
and sidelobes elsewhere become competitive.  Once the algorithm
locks onto a sidelobe with consistent off-position peaks, the
step-detection machinery downstream cements it.

**Diagnosis:** the unit tests at the time only varied `carrier_phase`
(static rotation), not `carrier_freq_hz`.  Static rotation IS handled
by magnitude correlation; rotating signal is not.  Test fixture
omission masked the algorithm flaw.

Reverted within 10 minutes of deploy.

---

## 5. Method 4 — magnitude correlation with Costas derotation (failed: 2026-05-22 ~02:53)

The corrected version of Method 3.  Keep Costas to remove residual
carrier *frequency*, then magnitude-pick on the derotated MF:

```
  s_rot[n]     = s[n] · exp(−jφ_costas[n])
  y_complex[n] = Σ_{k=1..N} s_rot[n+k] − Σ_{k=−N..−1} s_rot[n+k]
  edge         = argmax |y_complex[n]|
```

**The reasoning** (also wrong, more subtly):
- Costas keeps the signal at near-DC, so the boxcar integrates
  coherently.
- `|MF(s_rot)|` is robust to any small residual phase error θ
  because `|e^(jθ)| = 1` — full signal amplitude regardless of θ.
- This eliminates both (3a) the Costas-lock gating fragility (no
  longer need it, since |MF| doesn't depend on θ) and (3b) the
  per-restart amplitude-vs-θ drift (no `cos(θ)` factor any more).

**What actually happened:**
- 02:53:28–02:54:29: brief `?` while warming up (expected post-restart).
- 02:55:37: `*` at 8.3 µs offset.
- 02:56:39: `?` at 24 µs.
- 02:57:04: `chain-delay step adopted: lock re-homed -16991 samples
  (-176989.6 µs) after 60 consistent off-position edges`.
- 02:57:47 onwards: stuck at -177 ms offset, chrony rejected as `x`.

**Why it failed:**

Within each half-second integration window, the Costas residual
allows phase to drift by `ω · 0.5 s`.  If `θ_post_center ≠
θ_pre_center` (carrier has rotated during the window), then:

```
  y_complex[n] ≈ −N·A·e^(jθ_post) − N·A·e^(jθ_pre)
  |y_complex[n]| = N·A · |2·cos((θ_post − θ_pre) / 2)|
                = 2N·A · |cos(ω · 0.25 s)|
```

At ω = 2π · 1 Hz (a 1 Hz residual, plausible for sub-Hz Costas
tracking error), `cos(π/2) = 0` — the main peak is **completely
nulled**.  At the same time, sidelobes elsewhere reach their non-zero
local maxima.  Once the algorithm picks a sidelobe and the step-
detection adopts it (~1 minute), the lock is on the wrong edge.

**What `Re(MF)` does differently** that protects Method 2 against
this: the real projection takes `cos(θ(t))·polarity` *within* the
boxcar window.  The cos modulates the polarity sign at each sample,
so the integration of `cos(ω·t)·polarity` does NOT lose phase
coherence the same way.  It loses amplitude (by a factor of
`sinc(ω·0.5/2)`) but the *position* of the peak stays correct.
Magnitude correlation loses *position* itself.

This is the deeper lesson: a "rotation-invariant" detector built on
a long integration window is *not actually rotation-invariant* if
the rotation occurs *within* the window.

Reverted ~10 minutes after deploy.  Total time spent on Methods 3
and 4 combined: about 1.5 hours, with the user observing both
failures live.

---

## 6. Method 5 — per-sample magnitude derivative (diff feed, opt-in / disabled by default; first prototyped 2026-05-22 ~10:28)

The retreat from boxcar matched filters entirely.  Method 5 is wired to
publish as **HFPS** on SHM 3 (gated by `diff_to_shm_unit`) in
`core_recorder_v2.py`, but that feed is **disabled by default** —
`diff_to_shm_unit` is unset in the shipped config and chrony consumes
only FUSE and HPPS, so HFPS is not currently in use.  When enabled it
would run alongside HPPS (Method 2, SHM 2),
with chrony selecting between the two feeds, using T5 / LB-1421 NMEA
for second-of-arrival disambiguation.

```
  d[n] = |s[n] − s[n−1]|
  threshold = K · running_median(d)         (K = 100 default)
  peak: d[n] > threshold AND local max
  edge = parabolic interp on (d[n−1], d[n], d[n+1])
```

**Why this is right for our problem:**

The boxcar MF was chosen for processing gain in low-SNR signals.
TS1's BPSK on bee1 is *high-SNR*: signal amplitude `A`, per-sample
noise `σ_n` ≪ `A`, no data modulation between PPS edges.  Direct
detection doesn't need any integration gain.

For high-SNR direct detection of a polarity flip:

| Quantity                          | Magnitude              | At SR=96 kHz, Δf=0.5 Hz |
|-----------------------------------|------------------------|--------------------------|
| `|d[n]|` between flips            | `A · 2π · |Δf| / SR`   | `A · 3.3e-5`             |
| `|d[n]|` at the polarity flip     | `2A`                   | `2A`                     |
| Spike-to-background ratio         | `SR / (π · |Δf|)`      | `~ 60 000`               |

That's ~96 dB of margin.  The flip is unmissable.  No Costas, no
integration, no carrier-sensitivity.

**Why it doesn't have the failure modes of Methods 2-4:**

  * No half-second integration → no phase rotation within a window
    → no carrier-frequency sensitivity.  *Validated by tests:* the
    diff detector locks at carrier offsets up to at least 10 Hz; the
    boxcar MF starts degrading around 0.5 Hz.
  * No Costas state → no lock-state gating, no threshold-tuning
    treadmill, no per-restart disambiguation drift.
  * Each edge is detected independently → no "step adoption"
    machinery that can cement a wrong-position lock.
  * Background is set by carrier-induced jitter, not noise →
    threshold can be very high (100×) without missing real flips.

**Sub-sample timing:** the BPSK polarity transition through radiod's
±25 kHz channel filter has rise time ≈ 1/(2B) = 20 µs ≈ 2 samples at
96 kHz.  The derivative pulse is therefore Gaussian-ish, ~2 samples
wide.  Parabolic interpolation on three samples around the peak
yields sub-sample precision limited mainly by the residual carrier
noise floor.

---

## 7. Evidence: first 11 minutes of Method 5 sidecar (2026-05-22 10:28–10:40)

This evidence was captured during the original sidecar phase, when
Method 5 ran alongside Method 2 (production) dumping per-PPS edge
timestamps to CSV — Method 2 fed chrony and Method 5 was
observation-only.  (Method 5 has since been wired as an opt-in HFPS
feed on SHM 3, disabled by default; see §6.)

**First-snapshot data:**

```
Total edges:        603 over 10.8 min  (55.6/min — close to 60 expected)
Consensus position: 47916.1672 samples = 499.127 ms from second start
σ (inner band):     22 ns
MAD:                14.8 ns
Inner band:         582 / 603 = 96.5%
Outliers:           21 / 603 = 3.5% — scattered across 10 different positions
Detection margin:   506× median (range 116× – 865×)
```

**Direct comparison during the same window**, from
`authority_history.db`:

```
10:30:01 (Method 2):  chain-delay step adopted -40924 samples (-426 ms)
10:30:08 - 10:31:08:  Method 2's t6_offset_ms = -426.29 ms (sustained walk)
10:32:08:             Method 2 recovered to -0.5 µs after restart
10:34:08:             Method 2 at +2 µs, briefly stable
10:35:08 - 10:40:00:  Method 2 walked to +135 µs and stayed, chrony marked `?`

10:28-10:40 (Method 5 sidecar):  chain_delay = 47916.167 ± 0.001 samples
                                  through the ENTIRE window
```

Method 5 reported the same edge position for 11 continuous minutes
while Method 2 wandered through -426 ms, recovered, then walked to
+135 µs.

**Ratio of stabilities:** Method 5's 22 ns σ is ~6000× tighter than
Method 2's typical 135 µs offset excursions in the same window.

---

## 8. Outliers in Method 5 and how to handle them

The 3.5% of edges classified as outliers (21 of 603) are spread
across 10 different sample positions — no consistent wrong position
that step-detection would lock onto.  Their `d_magnitude` is ~100×
smaller than consensus peaks (0.0019 vs 0.17), meaning these were
weak local-maxima that briefly cleared the adaptive threshold.

The current implementation uses `threshold = K · running_median(|d|)`,
with K=100.  When the running median dips briefly (e.g. during a
quiet stretch), the threshold drops with it and weak peaks slip
through.

**Two simple refinements** before promoting Method 5 to production:

1. **Absolute floor based on running maximum.**  Replace
   `threshold = K · median` with `threshold = max(K · median,
   0.5 · running_max)`.  Outlier peaks are by definition
   significantly smaller than the consensus, so a fraction of the
   running max rejects them while still accepting real peaks.

2. **Inter-edge-time consistency.**  PPS edges are 1.000 s apart to
   within the GPSDO's stability.  Tighten the existing 0.99 s
   minimum-gap reject to a window of 0.999–1.001 s around the
   expected next edge.  Any peak outside that window is rejected
   regardless of amplitude.

Either refinement alone should drop the outlier rate from 3.5% to
<<1%.  Together, the inner band σ would approach the per-PPS
parabolic-interp limit, which for a 2-sample-wide derivative at
≥40 dB SNR is sub-ns.

---

## 9. What this leaves open

  * **Long-window stability of Method 5** — the 11-minute first
    snapshot is encouraging but doesn't prove hour- or day-scale
    stability.  Sidecar continues collecting.
  * **Production migration plan (not yet enabled)** — rather than swap
    Method 5 in for Method 2, the HFPS path was built as an opt-in
    parallel feed: when `diff_to_shm_unit` is set, Method 5 publishes
    its edge timestamps as HFPS on SHM 3 in parallel with HPPS (Method 2,
    unit 2).  That gate is **off in the shipped config**, so HFPS is not
    currently enabled.  Still open: once
    HFPS is proven to strictly outperform HPPS over multi-hour /
    multi-day windows, retire Method 2 along with its Costas threshold
    tuning, chain-delay disambiguation logic, and watchdog backoff
    (all of which exist to manage Method 2's failure modes).
  * **Sample-rate decision** — Method 5's precision is limited by
    sample period (10.4 µs at 96 kHz, sub-sample interp pushes
    this to ns).  Going to 192 kHz IQ would double the precision
    floor and is cheap with the RX-888.
  * **Chain_delay persistence across restarts** — Method 5 has no
    "lock" to disambiguate, so per-restart drift goes away
    automatically.  Persistence of the running-median and
    running-max state would help warm-up time but isn't essential.

---

## 10. The lessons we paid for

In rough order of how much time we spent on each lesson before it
was clear:

1. **The threshold-tuning treadmill is a symptom of an algorithm
   mismatch, not a problem to be solved with more tuning.**  Four
   passes of raising Costas thresholds on 2026-05-21 never converged
   because the *next* BPSK regime drift always exceeded the *new*
   threshold.  Yesterday's evening should have been a step back to
   "is this the right algorithm?" three passes earlier.

2. **A long integration window is sensitive to whatever rotates
   within it.**  A "rotation-invariant" magnitude over a half-second
   matched filter is not rotation-invariant if the carrier rotates
   over that window.  The fix isn't to make the magnitude smarter;
   it's to use a shorter window.

3. **The watchdog can be the dominant cause of the instability it
   was deployed to fix.**  Each Method 2 restart triggers a fresh
   chain-delay disambiguation that lands at a different position.
   The watchdog firing creates the very discontinuities chrony
   then flags, which the watchdog interprets as more instability.

4. **Test fixtures must exercise the relevant degrees of freedom.**
   Method 3 passed unit tests covering `carrier_phase` (static
   rotation) but not `carrier_freq_hz` (rotation per sample).  The
   live deploy walked to a 185 ms sidelobe within seconds — a
   failure mode the tests, as written, couldn't have caught.

5. **A strong signal calls for direct detection, not matched
   filtering.**  The half-second boxcar MF was the right tool for a
   low-SNR signal at the original 16 kHz sample rate.  At 96 kHz
   with a clean TS1 + LB-1421 chain it is the wrong tool — direct
   detection of the polarity flip's derivative gives orders-of-
   magnitude better stability with a small fraction of the
   complexity.

---

## Change log

- 2026-05-22 — Method 5 prototype shipped (commit 314b931).  Sidecar
  enabled on bee1 ~10:28 UTC, first 11-minute data captured.  This
  document written from that snapshot.

- 2026-05-22 — Method 4 (magnitude with Costas derotation) deployed
  02:53 UTC, walked to -177 ms, reverted 02:59 UTC.

- 2026-05-22 — Method 3 (magnitude without Costas) deployed 01:48
  UTC, walked to +185 ms, reverted 02:01 UTC.

- 2026-05-21 — Method 2 four-pass Costas threshold tuning.  See
  `HF-PPS-CHRONY-TUNING.md` §3.3 + commits e576c99 → 4584158 →
  e597518 → ddfc5b7.

- 2026-05-21 — Watchdog (timestd-tsl3-watchdog) backed off from
  120 s / 300 s to 600 s / 1800 s to avoid restart-induced
  chain_delay drift (commit 014e79c).

- 2026-05-06 → 2026-05-21 — Method 2 in production with no
  catastrophic failure modes documented.  TSL3 σ ~150 ns in clean
  hours; occasional µs-scale offset excursions handled.

- pre-2026-05-06 — Method 1 (legacy per-sample Δφ) in production.
  Replaced by Method 2 for processing gain.
