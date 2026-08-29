# T6 Folded Self-Acquisition — Design

**Date:** 2026-08-29
**Status:** Approved design, pre-implementation
**Approvers:** Michael (mjh)
**Extends:** `T6_ANCHOR_INVERSION_DESIGN.md` §3 (the two-stage estimator).
Nothing in that document is superseded; this changes which stage may gate.
**Prerequisites:** hamsci-dsp `37c0219` + hf-timestd `52c6cb6` — the
receiver-operating-point columns, without which the before/after in §8
cannot be told apart from a change in band conditions.

## 1. Problem

T6's authoritative path is gated on a stage that fails first.

`core_recorder_v2.py:4679` seeds the fine stage only when the matched-filter
calibrator reports `locked`, and `:4693` skips the authority call entirely
unless a live coarse offset exists. The MF calibrator therefore holds a veto
over the whole tier. It is also the stage least able to keep it: it integrates
0.5 s per edge and peak-picks against `threshold = 0.5 * _peak_running`, and
once noise batch-maxima approach half the real edge peak it locks itself out
completely.

Measured 2026-08-28 by driving the shipped `BpskPpsCalibratorMF` with
`tests/test_bpsk_pps_calibrator_mf._make_bpsk_signal` at calibrated C/N0:

| C/N0 dB-Hz | 75 | 70 | 66 | 63 | 61 | 59 | 58.5 | 58.0 |
|---|---|---|---|---|---|---|---|---|
| per-edge sigma | 0.68 µs | 1.23 | 2.04 | 2.85 | 3.60 | 4.6-6.0 | 5.0-6.7 | **bimodal** |

Sigma follows `1/sqrt(SNR)` (16 dB gives 6.7x observed against 6.3x
predicted) down to roughly 59 dB-Hz. Below that the behaviour is not graceful
degradation but a **stochastic cliff**: at 58.0 dB-Hz, seed 11 yields no lock
at all, seed 23 yields 13 edges with 17 rejected, seed 37 yields 32 edges.
Below ~57, nothing. Costas stayed locked throughout (`dphase_ema` 0.0016
against the 0.050 gate), so the cliff is the adaptive threshold, not carrier
recovery.

B4 sits on the wrong side of it at night. As radiod's RX888 AGC walked the
front-end gain from +11.9 dB to -4.2 dB across the evening of 2026-08-28, the
T6 channel's C/N0 fell 56.99 -> 48.50 dB-Hz (0.52 dB per dB of gain, linear
across the range), and `authority_history.db` showed
`t6_available = 0, "anchor authority ACQUIRING"` throughout. Hourly averages
over 2026-08-24..28 put `t6_sigma_ms` at 0.21-7.82 ms for 00-06Z against
0.003-0.07 ms for 14-23Z.

Meanwhile `BpskEdgeFineStage` — which folds 30 s coherently and is far more
sensitive — cannot run at all without a seed:

```python
coarse_fold = self.coarse_offset_fold_domain(registration)
if coarse_fold is None:
    return None
```

The robust stage is held hostage by the fragile one.

⚠ The AGC is **not** the fault and must not be "fixed". radiod holds the A/D
at ~-20 dBFS at every gain setting; it lowers gain because the band really is
that loud, and any fixed gain safe against night clipping would be that same
low gain all day. The AGC is better than the alternative. The cliff is ours.

## 2. Goal

Make T6 hold through the hours it currently fails. Judged on the **worst**
hours, not the best: a design reaching a lower sigma on a quiet afternoon but
dropping lock at 03:00Z loses to one that holds through the night at a worse
sigma.

## 3. Target architecture

`BpskEdgeFineStage` acquires its own edge position. The MF calibrator keeps
running and keeps seeding when it locks, but stops being a gate.

```
fine stage folds K s ─┬─ coarse fresh?  → ±6 ms around coarse        (seeded)
                      ├─ own offset?    → ±6 ms around own last      (tracking)
                      └─ neither        → boxcar MF over the second  (bootstrap)
                                          → zero-crossing fit (shared by all three)
MF calibrator ──────── seeds when locked; witness for fine_coarse; NEVER gates
authority ──────────── unchanged; runs whenever an estimate exists
```

### 3.1 Bootstrap is the boxcar MF, run once on the folded second

Not a new detector. The matched filter is the right thing for locating the
transition; folding is what gives it the SNR. The fine stage already owns the
accumulator, sign alternation and registration, so bootstrap reuses both
proven pieces: run the boxcar MF across the folded second, take its peak as
the search centre, and hand it to the existing zero-crossing fit exactly as a
coarse seed is handed over today.

Processing gain is `10*log10(K)`. **K stays at the shipped default of 30 s**
— 14.8 dB, placing the cliff near 43-44 dB-Hz, already below the worst C/N0
observed on B4 (48.5) — because leaving `fold_seconds` untouched keeps this
change to the acquisition path alone. K = 60 s (17.8 dB) remains available as
an existing knob if §8's measurement shows 30 s is not enough; it is not part
of this work.

### 3.2 The search is well posed

After sign-alternated folding the derotated second is +A before the edge and
-A after, so a **linear** array has exactly one interior sign change: the
edge. The circular wrap is not a sample-to-sample transition and cannot be
mistaken for one. The single awkward case is an edge within a few samples of
fold index 0, where the transition straddles the array boundary; the fold is
circular, so rolling by p/2 before the search and unrolling afterwards costs
nothing and removes the case.

### 3.3 Promotion requires confirmation

Self-seeding can cement a wrong crossing — the displaced-reference failure
this codebase already guards against in the MF with `STEP_CONFIRM_EDGES = 60`.
The fine stage gets the same discipline in its own terms:
`bootstrap_confirm_blocks` (default 3) consecutive full-search estimates must
agree within `bootstrap_confirm_tolerance_ms` (default 1.0) before the stage
will use its own offset as a seed. Until then every block does a full search.

Demotion back to bootstrap after **3 consecutive blocks** that either fail
their fit or are rejected for `edge_period`, so a wrong lock is always
escapable rather than defended. Three matches the confirmation count: the
stage should be no slower to abandon a position than it was to adopt one.

### 3.4 Stale coarse is fixed at the source

`reset()` does not clear `_coarse_offset_rtp`, which is why the outer
`coarse is not None` gate exists (Finding 3). Add `clear_coarse_offset()`,
called by the recorder whenever the MF is not locked. The stage then falls
back to tracking or bootstrap instead of searching a stale window, and the
outer gate is removed. `clear_coarse_offset()` clears **only** the
MF-supplied window, never the stage's own tracking offset — separate fields.

### 3.5 The cross-check survives, demoted from gate to witness

`T6AnchorAuthority.on_fine_estimate` already accepts
`coarse_offset_samples: Optional[float]` and already skips the `fine_coarse`
invariant when it is `None`. **No authority signature change is required.**
When the MF is locked, `fine_coarse` is enforced exactly as today; when it is
not, T6 proceeds on the folded estimate alone.

One addition, for honesty: record `fine_coarse_unverified: True` in
`last_check_metrics` when the check does not run, so an unrun check is
affirmatively marked rather than inferred from a missing key.

## 4. Threshold fix in the MF calibrator (secondary)

Once §3 lands, the treadmill is no longer a T6 outage — it is a lost
cross-check. Still worth fixing, because a witness that goes dark exactly when
it is most needed is a poor witness, but it is explicitly second-order.

**Sequencing: this is a separate commit, after §3 is deployed and verified on
B4 per §8.** Changing the detector and its threshold in one step would make a
regression in either indistinguishable from the other, on the tier that
decides whether the station may publish.

Replace `threshold = 0.5 * _peak_running` with `k * sigma_noise`.

⚠ The obvious noise estimator does not work here, and the code says so: a
clean MF output is a triangle wave whose median sits on the **ramp**, not in
noise, which is why the peak-relative form was chosen. Use the **MAD of the
second difference of `y`** — a triangle's second difference is ~zero except at
the apex, so what survives is noise. `k` is fixed at the value giving a per-block false-alarm probability
of <= 1e-3 against the measured noise distribution; it is derived during
implementation, pinned by a test, and written down as a module constant with
its derivation — not an operator knob.

## 5. Packet drops inside a fold — measure first

One batch whose registration disagrees by more than `REGISTRATION_SPREAD_LIMIT`
(240 samples) discards the entire block. At K = 30-60 s that is 30-60 s lost,
and three consecutive missed blocks trips `estimate_stale` -> DEGRADED. Drops
therefore threaten availability directly.

The right eventual shape is to exclude the offending sub-chunk rather than the
block: the fold already carries per-bin `_cnt`, so untrusted samples can simply
not accumulate. **But this is not built blind.** `blocks_discarded` is already
counted and nothing reads it. Surface `t6_fold_blocks_discarded` and
`t6_fold_seconds_folded` into `authority_snapshot` alongside the columns added
in `37c0219`, measure how often it fires on B4, and build drop-tolerance only
if the measurement justifies it.

## 6. Error handling

| Condition | Behaviour |
|---|---|
| Bootstrap finds no crossing, or the fit fails | return `None`, stay in bootstrap |
| Confirmation blocks disagree | stay in bootstrap, do not promote |
| Tracking-mode fit fails N times, or `edge_period` violations run | demote to bootstrap |
| Block discarded (registration spread) | no estimate this block; count it |
| Estimates stop arriving | unchanged: the authority's `on_tick` liveness invariant degrades loudly rather than freezing the anchor |
| Any exception in the stage | unchanged: the recorder's existing try/except is the outer backstop |

Absence of an estimate must always remain visible as absence. Nothing in this
design may make T6 hold an anchor it can no longer justify.

## 7. Testing

Unit, in `tests/test_bpsk_edge_fine_stage_bootstrap.py`:

* bootstrap locates a known edge across a C/N0 range
* an edge at fold index 0 and at p-1 is found (the roll)
* no crossing present -> `None`, no exception
* promotion requires `bootstrap_confirm_blocks` agreeing estimates
* a deliberately displaced bootstrap result is **not** promoted
* demotion after repeated fit failure
* `clear_coarse_offset()` stops a stale window being used, and does not clear
  the stage's own tracking offset

Integration:

* a fine stage that is never given a coarse now produces estimates — today it
  returns `None` forever. This is the headline behaviour of the change.
* the authority reaches AUTHORITATIVE from folded estimates with no MF lock,
  and `fine_coarse_unverified` is recorded on those cycles

Regression — the test that proves the exercise:

* **the C/N0 sweep, landed as a repo test**: assert the folded path still
  locks at a C/N0 where the per-second MF does not, pinning the cliff position
  as a guarded number rather than a session finding.

## 8. Verification on B4

Stated in the terms of §2. Compare **00-06Z** before and after:

* T6 lock fraction (`t6_available`) and `t6_sigma_ms` from `authority_snapshot`
* with `rf_gain`, `if_power`, `t6_baseband_power`, `t6_n0` confirming the
  receiver conditions were comparable across the comparison — the reason the
  provenance work went first

Success: T6 holds lock through hours where it currently reports ACQUIRING, at
a comparable C/N0. A lower sigma in the daytime is not success.

Deploy is git ff + restart of the `timestd-*` units, never `install.sh`.
The restart re-anchors the recorders, so it is scheduled with the operator.

## 9. Out of scope

Sliding or overlapping folds; any change to `T6AnchorAuthority`'s state
machine; any change to TS-1 injection level (held in reserve — Paul warns of
spurs, and closing the gap by injection alone would need 12-14 dB); any
change to the RX888 AGC.

Separately noted, not part of this work: `TSL3` is the superseded refid for
what is now `HPPS`, and survives in 46 places in live source. A rename of
comments and docstrings belongs in its own commit so neither review is
muddied by the other.
