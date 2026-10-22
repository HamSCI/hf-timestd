# Fusion's Kalman filters: a design review

**Date:** 2026-10-22.  **Code reviewed:** hf-timestd at `676045b`.
**Reviewer:** Claude, for Michael Hauan (AC0G).  **Scope:** read-only.  I
changed no code and touched no station.  A parallel agent gathers the station
readings; this review rests on the code, the design documents and simulations
that drive fusion's own code with synthetic data.

---

## Verdict

The per-broadcast Kalman filter does not filter.  Its measurement noise sits
ten to a hundred times below the scatter of the data it receives, so its gain
stays near one and its output tracks each raw measurement to within 0.002 ms.
Its reported uncertainty comes from a hand-set formula in signal-to-noise
ratio, not from the data.  It ignores the time between observations.  The
replay of row 17 makes all of this run about 200 times as often, but the
replay does not cause the bad uncertainty.  The filter reports the same
uncertainty with or without it.

A second estimator matters more than the Kalman filter, and row 17 does not
name it.  The "hardware calibration" in `_update_calibration` pulls each
broadcast's mean D_clock toward zero with a time constant of a minute or
two.  So fusion's published D_clock cannot hold any offset that persists.  In
simulation a 10 ms host-clock step reached the fused output at no more than
1.3 ms, then vanished within 13 minutes.  A host clock walking 0.5 ms per
minute for two hours, 60 ms in all, never moved the fused output past 0.8 ms.
No ERROR line fired in either case.  This breaks the rule in
`feedback_timing_expose_not_correct`: the code does not expose a timing fault;
it erases it.  It also reproduces, by a different route, the standing vote for
the host clock that `reference_tick_ensemble_is_noise` found in the tick
ensemble.

The fix for row 17 should go in, because it cleans up the logs, the long-term
statistics and the filter's motion state.  But a simulation of that fix moved
the published D_clock by less than 0.01 ms.  Quality demands more: a ruling
on the calibration, an honest noise model, and in the end retirement of this
filter from the timing path in favour of the station timing estimator that
already exists in `src/hf_timestd/estimator/`.

### The five defects that matter most

| rank | defect | where | consequence |
|---|---|---|---|
| 1 | The calibration EMA zeroes each broadcast's mean D_clock, so a persistent clock or frame error disappears from the fused output | `multi_broadcast_fusion.py:2428`, `:2440-2465` | Fusion cannot expose a step or a walk.  The T3 authority rung and the judge's T3 bench both read a number near zero by construction |
| 2 | Measurement noise R comes from a formula in SNR with constants 10-100× too small; the reported σ equals that floor whatever the data do | `broadcast_kalman_filter.py:233-253`, `:456-471` | σ of 0.03-0.25 ms against a real error of 1-3 ms; NIS (defined below) 2.5-25 instead of 1; the inverse-variance weights rank broadcasts by SNR, not by measured quality |
| 3 | The lookback replay feeds every row to every stateful estimator on every cycle (row 17) | `:1312`, `:2393`, `:2590` | Kalman n_updates 206× the number of rows; long-term statistics 400× too many samples, all stamped "now"; false "SIGNIFICANT GPSDO DRIFT" warnings, 26 in a 4-hour run with no drift |
| 4 | The filter keeps no clock: every update assumes dt = 1 minute, whatever the real gap | `broadcast_kalman_filter.py:170-173`, `:369`, `:401` | Under replay the filter believes 30 minutes pass every 8.2 s; a 10-minute leap-second hold predicts through 2,200 "minutes" and reports σ = 8.5 s |
| 5 | The weighted mean treats 30 minutes of rows from each broadcast, and several frequencies of one site, as independent | `:3466`, `:2264-2287`, `:3528` | The formal uncertainty shrinks by √(rows) for no reason; WWV's frequencies outvote WWVH although they share one path error |

The rest of this document gives the evidence, then the redesign.

---

## Terms used below

- **Kalman filter.**  A recursive estimator.  It holds a *state* (here two
  numbers: a delay and its rate of change) and a *covariance* `P` that says how
  uncertain the state stands.  Each cycle it *predicts* the state forward by
  the elapsed time, then *updates* it with a measurement.
- **Process noise `Q`.**  How much the true state may wander between two
  observations.  It should scale with the elapsed time and come from a
  measured stability of the thing tracked.
- **Measurement noise `R`.**  The variance of one measurement about the truth.
  It should come from the measured scatter of the data.
- **Innovation `ν`.**  The measurement minus the prediction.  Its predicted
  variance reads `S = H P Hᵀ + R`.
- **Gain `K`.**  How far the update moves the state toward the measurement.
  With `R` tiny against `P`, `K` approaches 1 and the filter copies the
  measurement.
- **NIS** (normalised innovation squared).  `ν²/S`.  For a filter whose `Q`
  and `R` match reality, NIS averages 1.  A mean well above 1 means the filter
  claims more confidence than the data support.
- **NEES** (normalised estimation error squared).  The same test against known
  truth, available in simulation.
- **WLS** (weighted least squares).  Here, a weighted mean with weights
  `w = 1/σ²`.  It gives the best linear estimate only when the inputs carry
  independent errors with those σ.
- **Allan and Hadamard deviation.**  Measures of how much a time series
  wanders as a function of averaging time τ.  The Hadamard form ignores a
  steady drift, which suits a filter that already models a rate.
- **EMA** (exponential moving average).  `x ← x + α (target − x)`.  With
  α = 0.1 per update and two updates per 8.2 s cycle, the memory lasts about
  40 seconds.

---

## 1 · What runs, and who reads it

Fusion (`timestd-fusion`, `multi_broadcast_fusion.py`) runs `fuse()` twice
every ~8.2 s, once per feed (L1 and L2).  Each pass reads every L1 row from
the last 30 minutes (`fuse(lookback_minutes=30)`, read at `:1803-1862`).

| estimator | state | lives | shared across feeds? |
|---|---|---|---|
| `BroadcastKalmanFilter`, one per (feed, broadcast) | delay and its rate | `broadcast_kalman_filter.py` | no; two banks |
| WLS over every row in the lookback | none (per cycle) | `fuse()` `:3461-3470` | — |
| "hardware calibration", an EMA per broadcast | `hardware_offset_ms` | `_update_calibration` `:2333-2538` | **yes** |
| long-term drift regression | sums n, Σt, Σy, Σt², Σty | `_update_long_term_stats` `:2540-2653` | **yes** |
| holdover uncertainty growth | last locked value, time | `fuse()` `:3721-3784` | yes |
| D_clock jump hold | last emitted value | `fuse()` `:3985-4004` | yes |
| Allan deviation of the fused output | 24 h of samples | `AllanDeviationTracker` `:469` | yes |

The L1 and L2 Kalman banks stand apart, and the code says why: "the dual-feed
cross-check is meaningless if they share filter state" (`:686-689`).  The
calibration, the long-term statistics and the holdover anchor do not stand
apart.  Both feeds read and write them.  In simulation the two feeds' fused
outputs agreed to 0.01 ms throughout, and the code itself records that they
ran byte-identical for weeks (`:5166-5170`).  The dual feed gives no
independent check today.

**Who reads the result.**

- `fusion_status.json` → `FusionStatusProbe` (`fusion_status_probe.py`) →
  the T3 rung of the authority manager.  When T3 holds authority, its
  `rtp_to_utc_offset_ns` comes from `d_clock_fused_ms`
  (`MEASUREMENT_MODEL.md` §0.2).  The authority manager also compares T3 with
  T4 (2 ms) and T2 (5 ms) (`authority_manager.py:139-147`).
- The Offset Judge's `FusionBench` (`offset_judge.py:561-631`) reads the same
  two numbers as the T3 bench: offset `d_clock_fused_ms`, σ `uncertainty_ms`.
- chrony's FUSE refclock, on the legacy path, when the anchor closure stays
  off (`:5179-5188`).  The shipped config marks FUSE `noselect`
  (`config/chrony-timestd-refclocks.conf:90`), as `MEASUREMENT_MODEL.md`
  §7.1.1 requires, so chrony logs it but does not steer on it.
- Grades, consistency flags and the status the operator sees.

So the Kalman σ reaches the weights, the weights reach `d_clock_fused_ms`, and
that number reaches authority, the judge and chrony's logs.  That chain gives
this review its stakes.

---

## 2 · The state model and the process noise

**What the filter estimates.**  The docstring and `METROLOGY.md` §12.3 say the
state holds time of flight, `x = [ToF_ms, dToF/dt]`, and justify the filter by
"the ionosphere cannot teleport".  The code feeds it D_clock instead
(`_apply_broadcast_kalmans`, `:1306-1313`; the comment at `:1302-1304` admits
it).  D_clock, as fusion builds it, holds the clock or frame error plus the
propagation model's error for that broadcast plus detection noise.  The
ionosphere's continuity argument covers only the middle term.  The clock term
needs no process noise at all under a GPSDO, and the detection noise has no
continuity.  So the physical argument for the model does not reach the
quantity the filter tracks.

**The process model.**  Constant velocity: `F = [[1, dt], [0, 1]]`.  The
constructor fixes dt at 1 minute (`broadcast_kalman_filter.py:170-173`), and
`update()` uses that fixed `F` every time (`:369`, `:401`).  Nothing passes the
row's timestamp in.  `predict()` accepts a dt but defaults to 1, and the one
caller (the leap-second hold, `:1310`) passes nothing.

**`Q` and how it got chosen.**  Hand-set on 2026-01-07 (`fd56417`), in three
frequency bands and three station multipliers (`:212-253`):

| band | `q_tof` (ms² per update) | WWVH ×1.5 | BPM ×2 |
|---|---|---|---|
| < 5 MHz | 1.5 | 2.25 | 3.0 |
| 5-10 MHz | 0.8 | 1.2 | 1.6 |
| ≥ 10 MHz | 0.3 | 0.45 | 0.6 |

No Allan or Hadamard measurement stands behind these numbers, and no document
derives them.  The covariance predict adds `Q` without multiplying by dt
(`:401`), although the units comment says ms²/min.  The adaptive version
multiplies `Q` by up to three factors (`:505-525`): SNR (up to ×4), innovation
size (unbounded, `|ν|/2` above 2 ms), and a "recent mode change" factor of 3
that, as section 5 shows, stays on almost all the time.

**Verdict on the model.**  A constant-velocity model can suit a slowly moving
propagation delay.  But here `Q` has no measured basis, no dt, and three
multipliers that react to the very noise the filter should average.  The rate
state carries no information: in simulation with a true rate of zero, it
wandered with a standard deviation of 0.23 ms/min.

---

## 3 · The measurement model

**`R` per broadcast.**  `_get_measurement_noise` (`:442-471`) returns
`(base / √snr_linear)²`, with `base` = 0.3 ms for WWV, 0.8 ms for WWVH and
1.5 ms for BPM.  So:

| broadcast | SNR | `√R` (the filter's per-measurement σ) |
|---|---|---|
| WWV | 20 dB | 0.030 ms |
| WWV | 10 dB | 0.095 ms |
| WWVH | 20 dB | 0.080 ms |
| WWVH | 10 dB | 0.25 ms |

The memory record puts real per-tick scatter at 14-21 ms
(`reference_tick_ensemble_is_noise`) and T3 marker scatter on B4 at about
3 ms (`reference_fuse_walk_mechanism`); same-site broadcasts agree to about
1 ms and different sites to about 4 ms (`reference_same_site_correlation`).
Against any of those, `R` sits one to three orders of magnitude too low.

**Consequence: the filter copies its input.**  With `Q`/`R` near 30 or more,
the steady-state gain stays near 1.  I fed the shipped class one row a minute
with 1 ms white noise at 15 dB: the output correlated with the raw input at
1.0000, and the two differed by 0.002 ms RMS.  The filter rejects no glitch
and smooths nothing, whatever `METROLOGY.md` §12.3 claims for it.

**Consequence: σ comes from SNR alone.**  At steady state with `K ≈ 1`, the
posterior variance settles at about `R`.  So the reported σ, which sets the
fusion weight (M-H12), carries no information beyond SNR and station name.
The weight ratio between two broadcasts then reads
`(snr_a/snr_b)·(base_b/base_a)²`: a WWV row at 20 dB would outweigh a WWVH
row at 20 dB by 7×, and a WWV row at 10 dB by 10×, whatever their actual
scatter.  The weights floor σ at 0.05 ms (`:2163`), which cuts those two
ratios to about 2.6× and 3.6× and hides how small the strongest channels'
σ claims run.

**Is the observation the right one?**  `MEASUREMENT_MODEL.md` §1 names one
measurand, the UTC instant of sample `n`, and §7.1 makes D_clock a derived
quantity.  Fusion's D_clock comes from L1's `timing_error_ms` (`metrology_engine.py:2290` writes it into `raw_toa_ms`; fusion reads it at `:2001-2025`),
measured against labels that come from radiod's pair or from the anchor,
depending on the regime.  So the observation carries whichever frame the
labels came from.  That suits a witness.  It does not suit an estimator of
`t₀`, because the calibration (section 6) then removes the frame's error.  A
T3 estimator of `t₀` should observe the registration itself: a label-plane
phase observation with its own σ, as the estimator spec requires
(`docs/superpowers/specs/2026-09-08-station-timing-estimator-design.md` §3).

---

## 4 · Update discipline and time

**The replay (row 17).**  `_read_latest_measurements` returns every row in
the 30-minute lookback.  `_apply_broadcast_kalmans` calls `kalman.update` on
each (`:1294-1316`).  Every ~8.2 s cycle therefore re-feeds about 30 rows per
broadcast per feed.  Measured with the shipped code over a simulated 4 hours:
240 distinct rows produced `n_updates = 49,483` per filter, 206× the rows.
The prior review's probe (90 updates from 30 rows over 3 cycles) agrees.

**A correction to row 17's account.**  Row 17 says the replay makes each
filter "report too small a sigma".  The simulation says otherwise.  Each
replayed update adds a full `Q` before it subtracts, so the steady-state σ
stays at the `R` floor with or without replay: 0.095 ms in both cases for
WWV at 10 dB, and likewise in all eight cases I ran (table in §9).  The σ runs
too small, but from defect 2, not from the replay.  The replay does cause the
other harms: the update count, the extra "Mode transition" lines from the
newest-to-oldest wrap, the noise in the rate state, and the long-term
statistics in §7.

**Time handling.**

- Observation time never enters the filter.  `update()` takes no timestamp.
  `last_update_time` stamps wall-clock time at `:358` and `:435`, and the
  mode-change clock runs on `time.time()` (`:383`, `:480`).
- No predict step matches the real dt.  The rows arrive about a minute apart,
  so dt = 1 happens to fit when each row arrives once.  Under replay the filter
  steps one "minute" per row, so it believes 30 minutes pass in 8.2 s.
- Out-of-order rows: the reader returns rows grouped by channel in reader
  order.  Nothing checks monotonic time per broadcast.
- Duplicates: every row arrives as a duplicate on every cycle after its first.
- Restart: `load_state` restores `P` and the state (`:690-732`).  The first
  update after a restart predicts one minute, whatever the downtime.
- Leap-second hold: the hold calls `predict()` once per row per cycle
  (`:1308-1310`).  Over the 10-minute hold that comes to about 2,200 predicts.
  In simulation the delay state moved 21.6 ms on a rate of 0.01 ms/min and σ
  reached 8.5 s.  Fusion coasts its output on the last locked value during
  the hold, and `K ≈ 1` snaps the state back on the first update, so the harm
  stays small.  It still shows how little the filter knows about time.

---

## 5 · Consistency, covariance handling and mode transitions

**Would NIS pass?**  No.  On one row a minute with 1 ms noise, mean NIS ran
2.5 (WWV, 10 dB) to 3.8 (WWV, 20 dB).  With 3 ms noise it ran 11 to 25.  A
consistent filter gives 1.  I computed NIS with the base `Q`; the adaptive
`Q` would lower it somewhat, which only shows the adaptive `Q` covering for
the wrong `R`.

**Mode transitions: what drives them.**  `detect_mode_transition` (`:527-558`)
flags MODE_CHANGE when `|ν| > 5·√S`.  Two defects shape `S`:

1. It uses `self.P`, which at that moment still holds the previous
   posterior.  `update()` defers the covariance predict until after the test
   (`:367-401`), so `S` omits a whole `Q`.
2. It uses `R` at a nominal 10 dB, 0.095 ms for WWV.

So for WWV the threshold sits near 0.6 ms.  Ordinary detection noise of 1 ms
crosses it on most updates.  Without any replay, 160 of 240 updates (two in
three) flagged MODE_CHANGE at 1 ms noise; at 3 ms, 206 of 240.  With replay
the same runs flagged 33,000 to 45,000.  So the replay multiplies the line
count by about 200, but the cause lies in `S`.

**What a mode transition does.**  It sets the wall-clock timestamp that
triples `Q` for the next five minutes (`:516-520`).  Since transitions fire on
most updates, `Q` stays tripled nearly always.  The other two consumers of that
timestamp, `get_search_window` (`:560`) and `is_converged` (`:607`), have no
callers anywhere in `src/`.  So the adaptive machinery does nothing useful
except write log lines, which the v3.73 row 18 work then had to rate-limit.

**Covariance resets, clamps and floors.**

- `P` never resets.  The Joseph-form update (`:413-431`) keeps it symmetric
  and positive; that work holds up and its tests have teeth.
- The weight floor of 0.05 ms (`:2163`) clamps a σ that already runs too
  small.  It hides the error rather than exposing it.
- Holdover caps the published uncertainty at 10 ms (`:3761`).  A cap on an
  uncertainty pushes it down, which `MEASUREMENT_MODEL.md` §4 and §9 rule 5
  forbid: a claim must never understate its own uncertainty.  The growth rate
  itself, `gpsdo_holdover_drift_rate = 0.001` ms/min (`:805`), applies a GPSDO
  rate to a host clock, as `reference_fuse_walk_mechanism` already notes.
- Single-station mode multiplies the uncertainty by 5 (`:3931`).  An honest
  direction, but an unmeasured factor.
- The final uncertainty adds, in quadrature, five constants: systematic 0.4,
  propagation 0.5 or 1.0, RTP jitter 0.1, tone 0.2-0.5, multipath 0.5 ms
  (`:3542-3606`, `:3912-3919`).  None carries a measurement.  Together they
  floor the result near 0.9 ms.  `MEASUREMENT_MODEL.md` §6 asks that an
  unevaluated term appear as `not_declared`, never as a plausible value.  The
  propagation term also falls to its 1.0 ms default for any mode string the
  table lacks, which includes `FALLBACK` and every `…+GNSS_VALIDATED` tag
  (`:3590`).

---

## 6 · The calibration: the defect that hides faults

`_update_calibration` learns a "hardware offset" per broadcast.  Its docstring
promises that it "does NOT zero out the mean D_clock" and calls the old
zeroing "the circular bug".  The code computes the target as
`hw_target = 0.0 - broadcast_mean` (`:2428`), the negative of the mean
filtered D_clock over the last 30 rows, and moves the offset toward it by an
EMA.  Fusion then adds that offset to every row (`_apply_calibration`,
`:2301-2331`).  So the calibrated D_clock of each broadcast converges on zero.
The circular bug never left.

**How fast.**  After the first 100 updates the code chooses α from the
size of `broadcast_mean` (`:2436-2470`).  The comment calls that size the
"calibrated residual", but `broadcast_mean` holds the mean of the
uncalibrated values.  For any broadcast whose propagation-model bias exceeds
2 ms, the code therefore runs α = 0.10 with steps up to 1 ms, or α = 0.15 with
steps up to 5 ms above 10 ms.  Both feeds call it every cycle, so the offset
follows the raw mean with a memory of under a minute.  Only a broadcast whose
raw mean stays under 2 ms gets the "glacial" rate, and a clock error larger
than 2 ms pushes every broadcast out of that band at once.

**The algebra behind it.**  Write each broadcast's D_clock as
`d_i = c + b_i + n_i`: a common clock or frame error `c`, a per-broadcast
bias `b_i`, and noise.  From `N` broadcasts one gets `N` equations in `N + 1`
unknowns.  Only the differences `b_i − b_j` can be identified.  Some outside
choice must fix the rest.  The calibration fixes it by setting `c = 0`
continuously, in effect declaring the frame correct.  That choice belongs to
the operator, and today nobody makes it; the EMA makes it silently every
40 seconds.

**What the simulation shows.**  I drove the shipped `fuse()` with four
broadcasts (WWV 5, 10, 15 MHz and WWVH 10 MHz), fixed model biases of +3, −2,
+5 and +8 ms, 1.5 ms white noise, one row per broadcast per minute, an 8.2 s
cycle and a 30-minute lookback.  Then I added a host-clock error to every row.

| injected host error | fused D_clock (L2) | grade, flag | ERROR lines |
|---|---|---|---|
| none, first 10 min | 6.3 ms falling to 0.1 ms (the biases, absorbed) | D → B, OK | 0 |
| +10 ms step at t = 120 min | 0.0 for 7 min (rows rejected as outliers), peak 1.3 ms at 128 min, back to 0.1 ms by 133 min | B → C → D → B, OK throughout | 0 |
| 0.5 ms/min walk from t = 120 min, 60 ms at 240 min | never above 0.8 ms; 0.2 ms at the end | B → C, OK throughout | 0 |

The only warnings in the step run: six "Rejecting outlier" lines, two
cross-frequency failures, and 36 "SIGNIFICANT GPSDO DRIFT" lines.  The
null run, with no host error at all, produced 26 of those drift warnings
too, so they carry no information (section 7).  In the walk, the offsets
grew until they would have hit `MAX_CALIBRATION_OFFSET_MS` = 80 ms
(`wwv_constants.py:432`); only then does an ERROR fire.

Two further mechanisms help hide a fault:

- The outlier filter runs on calibrated residuals with a median over all
  rows in the window (`:3116-3171`).  A fresh step affects only the newest
  rows, so it rejects them as outliers until the step fills enough of the
  window.  That explains the seven quiet minutes.
- Cross-station validation compares the calibrated station means
  (`:2813-2819`).  After calibration every broadcast's mean sits near zero, so
  the stations agree by construction.  The flag read OK through both faults.

**Consequences for the consumers.**  The T3 probe and the judge's T3 bench
both read `d_clock_fused_ms`.  Whatever the frame does, they read a number
near zero with σ near 1.6 ms.  The judge's T3 bench then testifies that
radiod's pair runs correctly; the authority manager's T3↔T4 agreement check
passes because T3 cannot disagree for long.  That repeats the failure of
`reference_tick_ensemble_is_noise`, a standing vote that the host clock is
right, from a second source.  `METROLOGY.md` §12.5 argued against a Kalman at
L3 because "a Kalman would mask this bias instead of estimating it".  The
calibration EMA does exactly that, with a shorter memory than any Kalman
would have.

**A caveat.**  These runs used synthetic rows.  The station agent's numbers
should confirm or refute them: the history of `hardware_offset_ms` in
`broadcast_calibration.json` against the raw mean D_clock, and on AC0G-B4 the
fused D_clock against the T6 registration (section 11).

---

## 7 · The long-term drift regression

`_update_long_term_stats` (`:2540-2653`) adds every row in the lookback, on
every cycle, for both feeds, with `t = time.time()` (`:2590`).  Over the
simulated 4 hours each broadcast accumulated about 97,500 samples from 241
rows, 400× too many, all stamped with the moment of the cycle rather than the
row's own time.  The regression then pools broadcasts with different
intercepts.  The inflated `n` shrinks the slope's standard error by roughly
√400 = 20.  The result: 26 "SIGNIFICANT GPSDO DRIFT DETECTED" warnings in a
4-hour run with no drift at all.  An alarm that fires on the null trains the
operator to ignore it.  The discontinuity check compares consecutive rows in
replay order, so the wrap from newest to oldest row can trip it, and the L1
and L2 passes share `long_term_last_values` (prior review, M-1).

---

## 8 · Weighting and correlated errors

**Rows, not broadcasts.**  The WLS mean at `:3466` runs over every row in the
lookback, about 30 per broadcast per feed, each carrying the filter's output
at the moment of its replay.  So the fused value works as a 30-minute weighted
moving average.  The formal uncertainty `√(1/Σw)` then treats 30 × N rows as
independent; with σ = 0.05 ms and 120 rows it reads 0.005 ms.  The scatter
term in `_wls_uncertainty` (`:2264-2287`) normally overrides it, because it
returns the weighted spread of the inputs rather than an error of the mean.
That rescue works by accident.  The older "statistical uncertainty" at
`:3528` divides the spread by √(rows), the same error again, and the holdover
branch still reads it through `measurement_uncertainty` (`:3758`), where it
can tighten the holdover bound.

**Shared information.**  WWV's frequencies leave one antenna field and share
one great-circle path, so their model errors correlate strongly (about 1 ms
agreement same-site against 4 ms cross-site).  The weights count four WWV
channels as four independent votes, and the σ formula gives each WWV row
several times a WWVH row's weight.  The fused value therefore follows WWV.
If the four WWV channels correlate at ρ = 0.8, they carry the information of
about 1.2 independent channels, not 4.  The calibration currently hides this
by zeroing every mean, but once the calibration changes (section 10) the
double counting will show.

**Other weights.**  `GLOBAL_DIFF` enters with a fixed weight of 10-100
(`:2171`), on a scale unrelated to `1/σ²`.  The trust factors (grade, mode,
station priority, a bootstrap ×3) multiply in unmeasured scalars.  The mode
ambiguity penalty asks the physics model per row, per cycle (`:2207-2232`).

---

## 9 · Fault exposure: summary

| fault | what fusion does | loud? |
|---|---|---|
| persistent clock or frame step | outlier filter drops it, calibration absorbs it within ~13 min | no ERROR; grade dips briefly |
| clock walk | calibration follows it to 80 ms | ERROR only at the 80 ms sanity limit |
| one broadcast's model bias changes (sunrise, mode change) | calibration absorbs it; science loses the signal | INFO "UN-CONVERGE" |
| detection glitch | filter passes it through (`K ≈ 1`); MAD filter may catch it | rate-limited WARNING |
| fused output jumps > 5 ms | holds one cycle, accepts on the second | ERROR, then WARNING (`:3985-4004`); sound |
| GPSDO drift | false alarms fire on the null | WARNING, meaningless |

The judge's own filter (`offset_judge.py:2107-2136`: a median, then an EMA
with α = 0.2, opening a new segment on any step beyond its band) treats a
step as a fracture rather than smoothing it.  That design suits a witness and
I found no defect in it, beyond its α counting polls rather than seconds.

**Simulation table for section 4 and 5** (shipped `BroadcastKalmanFilter`,
240 one-minute rows, white noise about a slow random walk, "replay" = every
8.2 s re-feed the last 30 rows):

| station | SNR | noise | mode | reported σ | actual RMS error | mean NIS | MODE_CHANGE | n_updates |
|---|---|---|---|---|---|---|---|---|
| WWV | 10 dB | 1 ms | once | 0.095 | 0.91 | 2.5 | 160 | 240 |
| WWV | 10 dB | 1 ms | replay | 0.095 | 0.91 | — | 33,121 | 49,483 |
| WWV | 10 dB | 3 ms | once | 0.095 | 2.72 | 15.3 | 206 | 240 |
| WWV | 20 dB | 1 ms | once | 0.030 | 0.91 | 3.8 | 177 | 240 |
| WWV | 20 dB | 3 ms | once | 0.030 | 2.73 | 25.0 | 214 | 240 |
| WWVH | 10 dB | 1 ms | once | 0.251 | 0.89 | 1.7 | 48 | 240 |
| WWVH | 20 dB | 3 ms | replay | 0.080 | 2.73 | — | 37,652 | 49,483 |

The reported σ understates the real error by 10× to 90×, with or without
replay.

---

## 10 · Tests: what they prove, and which cannot fail

Tests that cannot fail, or prove less than their names claim:

- `tests/test_broadcast_kalman_filter.py::test_multiple_updates` asserts
  σ < 10 ms after updates.  The initial `P = 100` gives σ = 10, and any update
  lowers it.  No filter defect could fail this.
- `::test_fading_handling` asserts the predicted σ > 0, and computes
  `tof_before` without using it.  It cannot fail.
- `tests/test_hierarchical_estimation.py::test_subsequent_measurements_smooth`
  asserts the second output lies strictly between 35.0 and 35.5.  With
  `P = 100` against `R ≈ 0.003` the gain reads 0.99997, so the test "proves
  smoothing" by a margin of 15 ns.  `::test_outlier_rejection` asserts a
  20 ms innovation flags a change; with the threshold near 0.6 ms that holds
  for any filter state.
- `tests/test_fusion_wls_weighting.py::test_cold_start_acquires_then_locks`
  feeds the same four rows on every cycle and asserts LOCK within five
  cycles.  It encodes the replay as the expected input; the lock it proves
  comes from re-feeding identical data.
- Every noise level in the Kalman tests sits at 0.01-0.1 ms, ten to a
  hundred times below the data the filter sees in service.  The tests
  therefore exercise the one regime where the hand-set `R` happens to fit.

Tests with teeth: the Joseph-form and NaN tests
(`tests/unit/test_broadcast_kalman_filter_remediation.py`), the post-predict
innovation test (`tests/test_broadcast_kalman_timing.py`), and the WLS
formula tests.

Tests that do not exist and should:

1. A host step and a host walk injected into fuse() must show in
   `d_clock_fused_ms` (section 6 gives the harness).  Today such a test
   would fail, which makes it the first test to write.
2. n_updates equals the number of distinct rows after many cycles.
3. Mean NIS on realistic noise sits within its χ² band.
4. The long-term regression sees each row once, at its own time, and raises
   no drift alarm on the null.
5. Each of these should be watched failing under a mutation before anyone
   trusts it (`feedback_test_power_by_mutation`).

---

## 11 · Measurements to take

All read-only, light on the station; copy the files off and analyse on the
devbox (`feedback_no_heavy_diagnostics_on_station_vms`).

1. **Replay rate.**  `n_updates` and `last_update` in
   `/var/lib/timestd/state/broadcast_kalmans/l2/*_kalman_state.json`, read
   twice an hour apart.  The prediction: about 200 updates per broadcast per
   minute, about 1 if the fix has gone in.
2. **Calibration tracking.**  `hardware_offset_ms` per broadcast in
   `broadcast_calibration.json`, sampled hourly for a day, beside the hourly
   mean raw D_clock from L1.  The prediction: the offset follows the raw mean
   with a lag of minutes.
3. **The real noise, for `R` and `Q`.**  48 hours of one-minute L1
   `timing_error_ms` per broadcast from B4 and ND.  From it: σ from first
   differences divided by √2 (robust to slow drift); the Hadamard deviation
   from τ = 1 to 240 minutes; and the correlation between WWV frequencies and
   between WWV and WWVH.
4. **The honesty gate.**  On B4, where T6 holds a registration, compare
   `d_clock_fused_ms` with the host error the T6 registration implies, over
   24 hours.  `MEASUREMENT_MODEL.md` §9 rule 5 requires the published σ never
   to fall below that scatter.
5. **The null alarm.**  Count "SIGNIFICANT GPSDO DRIFT" in B4's fusion journal
   over seven days.  A disciplined GPSDO should produce none.

---

## 12 · Redesign

Four stages.  Each stands alone, and each needs your ruling before it changes
a published number.  Stage 0 changes almost none; stage 2 changes the most.

### Stage 0 — the minimal fix for row 17

**Change.**

1. Give `BroadcastKalmanFilter.update` the row's own timestamp.  It predicts
   by the real gap, `dt = (t_row − t_last)/60`, scales `Q` by dt, and
   refuses a row at or before `t_last` (a duplicate or out-of-order row),
   counting the refusal.
2. In `_apply_broadcast_kalmans`, sort rows by time per (feed, broadcast).
   Update only rows newer than that filter's watermark.  For rows already
   consumed, return the output cached when the filter consumed them (a dict
   keyed by feed, broadcast and row time, pruned to the lookback).  The WLS
   then sees the same rows as today.
3. Persist `t_last` with the state file.  After a restart, a gap longer than a
   few hours re-initialises the filter with a wide `P` and says so in one log
   line.
4. Feed `_update_calibration`'s history and `_update_long_term_stats` only
   rows newer than their own watermarks, and give the long-term regression
   the row's time, not `time.time()`.  Key `long_term_last_values` by feed.
5. In a leap-second hold, predict once per cycle by the real elapsed time.

**Tests**, each watched failing under the named mutation:

- n_updates equals distinct rows after 100 cycles over a sliding window
  (mutation: drop the watermark).
- Two rows ten minutes apart grow `P[0,0]` by `10·q_tof` before the update
  (mutation: dt fixed at 1).
- The long-term `n` equals the row count and `Σt` uses row times (mutation:
  restore `t = now`).
- A duplicate row increments the refusal counter and leaves the state alone.
- No "SIGNIFICANT GPSDO DRIFT" in a 4-hour null run (mutation: restore the
  replay into the regression).

**Validation.**  Build an offline harness that reads recorded L1 rows (48 h
from B4 and from ND) and runs `fuse()` against a simulated clock, as section 6
did with synthetic rows.  Run the old and new code side by side.  Expect the
fused D_clock to agree within about 0.2 ms; my synthetic run of a prototype
of items 1-2 agreed to 0.01 ms, with n_updates falling from 49,483 to 240.
Expect the Mode-transition count to fall about 200-fold and the drift
warnings to stop.

**Risks.**  Low.  The fix leaves the calibration's absorption untouched
(the same prototype still erased the 10 ms step), so nobody should read this
stage as a timing fix.  If the watermark fails to persist, a restart replays
30 minutes once, as today.

### Stage 1 — an honest noise model

**Change.**

1. Set `R` per broadcast from the measured scatter: the robust σ of first
   differences over a sliding two hours, never below a declared floor.  Keep
   SNR only if the data show a relation; fit it, do not assume it.
2. Set `Q` from the measured Hadamard deviation of each broadcast's series
   (measurement 3), with the same two-term form the estimator package uses
   (`src/hf_timestd/estimator/process_noise.py`, `RulerNoise.q_matrix`).
3. Gate each innovation against `3·√S`, with `S` built from the *predicted*
   `P`.  Refuse and count what fails.  Declare a mode change only after
   several consecutive refusals with one sign; then re-initialise and log it
   at WARNING.  Delete the dead `get_search_window` and `is_converged`, and
   the three `Q` multipliers.
4. Publish a rolling mean NIS per broadcast over 60 updates in
   `fusion_status.json`.  For 60 samples the 95 % band runs about 0.74 to
   1.30.  Log a WARNING while it sits outside.
5. Fuse one value per broadcast per cycle, the newest filtered state, not
   every row.  Account for shared paths: either average each site first and
   then combine sites, or use generalised least squares (WLS with a full
   covariance matrix, so correlated inputs share one vote).  Estimate the
   same-site correlation from measurement 3.
6. Replace the five quadrature constants with measured terms, or publish them
   as `not_declared`.  Drop the 10 ms holdover cap.

**Validation.**  On the recorded 48 h: mean NIS per broadcast inside its band;
in simulation with known truth, NEES inside its band; reported σ within a
factor of 1.5 of the actual error.  Mutation: restore the old `R` formula, and
the NIS test must fail.

**Risks.**  The weights shift toward WWVH and the weaker channels.  The
published uncertainty grows toward the truth, so grades fall and T3 may
qualify less often.  Every consumer of T3 sees that.  It reflects honest
numbers, but it looks like a regression on a dashboard.

### Stage 2 — a ruling on the calibration

This stage carries the structural decision.  Section 6 shows that no
calibration can learn per-broadcast biases from fusion's own output without
also choosing the clock offset.  Two honest choices exist:

**A. Calibrate against an independent registration only.**  Where T6 or T5
gives a label-plane registration, learn each `b_i` as `d_i − c_ref`, averaged
over hours, and freeze it with its provenance: method, date, sample count and
σ.  Never learn it from fusion's own output.  Report any later change larger
than 3σ as a loud event, in the DETECT, ALARM, RE-ANCHOR pattern.  Where no
reference exists, do not calibrate; carry the propagation model's own bias as
a Type B term with its stated uncertainty.

**B. Calibrate only differences within a site.**  Learn each WWV frequency's
bias relative to one reference frequency, so the site mean stays free.  A
common clock error then survives into the fused output, and cross-site
disagreement reappears as the science signal it represents.

I recommend A where a reference exists and B elsewhere.  The current gauge,
"the clock holds no error", should go in either case.

**Validation.**  Use the offline harness on recorded rows with synthetic
faults added: steps of 1, 5, 10 and 50 ms; walks of 0.1 and 0.5 ms/min.
Acceptance: a 5 ms step appears in `d_clock_fused_ms` at 80 % or more within
5 minutes and stays while the step stays; a walk tracks within 1 ms plus the
noise; the null run raises no alarm.  The current code fails each of these
(section 6), which proves the test has power.  Then run §9 rule 5 on B4
against T6 for a week.

**Risks.**  High, and visible.  Fused D_clock will show the 3-10 ms
per-broadcast model biases the calibration now hides.  Cross-station
validation will fail more often, honestly.  T3↔T4 agreement at 2 ms will
demote T3 more often.  On any station where T3 holds authority, the published
offset will shift by whatever the calibration had absorbed, which moves
consumers' labels.  Roll it out on AC0G-B4 first, as the test bench, after an
announcement on the claude-bus; read the result against T6 there before any
other station sees it.

### Stage 3 — take this filter out of the timing path

`MEASUREMENT_MODEL.md` §3 makes T3 one estimator of `t₀`, and §7.1.1 keeps T3
out of chrony's electorate.  The station timing estimator in
`src/hf_timestd/estimator/` already does what `BroadcastKalmanFilter` fails
to do.  It keeps time by sample index, never by the host clock.  It reads
`Q` from the ruler's Allan deviation.  It gates each innovation at 3σ, refuses
any observation from the host plane, and holds to the rule that a step never
becomes a rate.  It has 225 tests and an acceptance corpus, and no consumer.

So the proposal reads:

1. The T3 rung of authority reads the tick registration (the
   `registration_acquirer` path) through the station timing estimator, with
   its own `u(t₀)`, rather than `d_clock_fused_ms`.
2. Fusion's D_clock becomes a witness only: published, judged, never
   adopted.
3. If per-broadcast filtering still serves science, it moves to
   hamsci-physics and observes time of flight (arrival minus transmission),
   with the stage 0 and stage 1 corrections.
4. The integration meets the estimator's stated obligation (ruling R20 in
   `estimator.py:24-32`): one tier string per independent witness, so two
   adapters reading one source cannot outvote a third.

**Validation.**  Shadow the estimator on B4 and ND for a week beside the
current T3; compare both with T6 on B4.  The acceptance corpus in
`tests/estimator/test_acceptance_corpus.py` already covers the recorded
failures.

**Risks.**  The estimator has run against live signal only once, on
2026-09-08, and that run found its rates wrong by three orders of magnitude
(`STATION-TIMING-ESTIMATOR.md`).  It needs a long shadow before it carries
authority.  The wiring takes real work in the authority manager and the
status file.

---

## 13 · Smaller findings

- `DISCRIMINATION_SUSPECT` never gets assigned, so the branch at `:3848-3897`
  never runs; inside it, `suspect_count` never increments.
- `_save_calibration` runs every 10 updates; the comment "Only save when
  Kalman has converged" at `:2532` does not describe the code.
- A warm restart with two or more calibrations sets `kalman_n_updates = 200`
  (`:1129`), which skips the bootstrap ramp and reads ACQUIRING at once.  The
  T3 probe accepts ACQUIRING.  The name "kalman_state" now describes a WLS
  threshold, not any Kalman filter.
- The Allan deviation tracker measures a 30-minute moving average sampled
  every 8.2 s; its values below τ = 30 minutes describe the averaging, not
  the clock.
- `METROLOGY.md` §12 describes a ToF state, glitch rejection, a
  `_weighted_least_squares_fusion` method that does not exist, and an
  "instantaneous" WLS that in fact spans 30 minutes.  It should change when
  the code changes, so that it describes the current best state.

---

## 14 · Rulings I need from you

1. Stage 0: may it go in as hygiene, given that it moves no published number
   by more than a few hundredths of a millisecond in simulation?
2. Stage 2: option A, option B, or both?  This decides whether fusion's
   D_clock may ever again show a persistent offset.
3. Stage 3: should T3 authority move to the registration path and the
   station timing estimator, leaving fusion's D_clock as a witness?
4. Until stage 2 lands: should the T3 probe and the judge's T3 bench stop
   treating `d_clock_fused_ms` as evidence about the clock, given that the
   calibration forces it toward zero?

---

## Appendix · How I ran the simulations

I ran everything from the repository's own virtual environment against the
code at `676045b`, outside the repository, and committed nothing.

**Filter alone.**  A loop built a truth series (a random walk with 0.05 ms
steps per minute), added white noise of 1 or 3 ms, and fed the shipped
`BroadcastKalmanFilter` either one row a minute ("once") or, every 8.2 s, all
rows from the last 30 minutes ("replay").  It recorded the reported σ, the
error against truth, NIS from the filter's own predicted `S`, the number of
mode-change timestamps set, and `n_updates`.

**Whole fuse().**  A `MultiBroadcastFusion` in a temporary directory, with
`physics_model` and `arrival_matrix` set to `None`, the global differential
solve, tick reader and GNSS VTEC reader stubbed out, and
`_read_latest_measurements` replaced by a function that returns the last 30
minutes of synthetic rows for a simulated "now".  Each cycle called
`fuse(force_l1_only=True)` then `fuse(force_l1_only=False)`, as the service
does.  A log handler counted lines by level and message.  The stage 0
prototype wrapped `_apply_broadcast_kalmans` so each row reached each filter
once and older rows reused their cached output.

The station data should replace these synthetic inputs before anyone acts on
the numbers; section 11 lists what to collect.

## 15. Michael's rulings, 2026-10-22

1. **No.** T3 authority and the judge's T3 bench stop treating `d_clock_fused_ms` as evidence about the clock.
2. **Feed each row once** (stage 0).
3. **T3 stands alone.** T3 exists to build a reconstruction of UTC from the time-standard
   receptions alone.  No calibration may give a higher tier (T6, T5) authority over T3 locally, and
   no calibration may zero T3 against the host clock.  Option A is rejected.  (The rest of this
   ruling, and ruling 4, are still to come.)

### Rulings on the five ruling-1 questions (Michael, 2026-10-22)

Source of the questions: `~mjh/hamsci/proofs/d-clock-fused-consumers-20261022.md`.

1. **Sign convention (Q5):** MEASUREMENT_MODEL.md's own, `label_utc = rtp_time + rtp_to_utc_offset_ns`.
   One synthetic test with a known host offset checks every consumer before anything else moves.
2. **T3 goes dark (Q2)**, loudly: unavailable, with its reason, until a replacement proves itself.
3. **T3-only stations fall back to T2 (Q4)**, with the demotion shown on the panel and in the
   heartbeat.  List the T3-only stations before the change lands.
4. **The tick registration may win the T3 slot (Q1)** after the sign test, with its real sigma (not
   the 1.0 ms floor), and after a shadow run on B4 against T6.
5. **Stop the legacy FUSE write (Q3)**, and close its two install gaps.

**Order of work**, each step B4 first and announced on the claude-bus: (1) the sign test; (2) label
the fused value as calibrated and publish the raw value beside it; (3) delete the T6 disambiguation
branch; (4) stop the FUSE write and close its gaps; (5) take FusionBench out of the judge's
adoption and make the T3 rung report unavailable with its reason; (6) shadow the tick registration
on B4; (7) let it win T3 once the shadow run proves it.
