# T6 Gap-Aware Tracking — Design

**Status:** APPROVED (Michael 2026-08-11) — the sole remaining blocker for
stage-1 acceptance and the split gate.
**Problem record:** `docs/T6-BLOCK-SLIP-ROOT-CAUSE-2026-08-10.md`.

## 1. Problem

Sample gaps (host-load USB loss — now largely mitigated — and the residual
recorder-internal ~10-min loss, plus any future hardware fault) make the T6
origin land an integer number of radiod blocks (±1 packet) away at each
re-lock: measured 1.78 s spread over 13 re-locks while the fine-stage
fraction repeated to the nanosecond. Two label pathologies reach the
calibrator: benign cancelling ±60-sample wobble (recorder repackaging) and
genuine sustained RTP jumps (real loss).

**Refinement from code reading (2026-08-11):** the coarse peak's RTP is
already taken from the declared per-sample array (`rtp_at_y[pi]`), so the
defect is NOT a simple buffer-index lookup. The suspect surfaces are
(a) the correlation/boxcar window mixing pre- and post-gap samples so the
peak lands on a sample whose declared RTP is not the true edge, (b) the
fine-fold's continuity indexing and median RTP registration across a gap,
and (c) the step-adoption/disambiguation machinery re-homing on
gap-shifted evidence. **Task 0 of the plan is to reproduce the slip in a
synthetic-gap unit test and localize the exact line — no fix before the
failing test exists.**

## 2. Design principles

1. **Declared RTP is truth for position; contiguity is never assumed.**
   A sustained declared-RTP jump is a real gap: the tracker's edge
   prediction (`last_edge + round(Δrtp/SR)·SR`, wrap-safe) must move WITH
   it, and correlation state that spans it must be invalidated, not
   trusted.
2. **Cancelling wobble is label noise.** Maintain a divergence ledger
   (cumulative declared-vs-contiguous offset per batch). Excursions that
   return to the running baseline within a small window (the ±60
   repackaging pattern) are smoothed: position lookups use the ledger's
   stable baseline, not the transient.
3. **Gaps must not kill liveness.** On a sustained gap: flush fold blocks
   spanning it, re-register immediately from post-gap declared RTP, and
   resume folding within one fold period — never the
   estimate_stale → DEGRADED → UNLOCKED death spiral for a survivable
   event.
4. **Re-locks must be origin-stable.** After any gap or re-acquisition,
   the derived origin (`utc_ns − rtp·1e9/SR`, wrap-aware) must equal the
   pre-gap origin to within one sample. This is the acceptance invariant
   and becomes a unit-test assertion, not only a field measurement.

## 3. Acceptance

- Unit: synthetic stream with (a) ±60 cancelling wobble, (b) injected
  N-block gaps, (c) both — edge tracking holds lock, no step adoption,
  origin identical before/after to <1 sample. The Task-0 failing test
  flips to green.
- Field: stage-1 criterion unchanged — origin spread < 10.4 µs across a
  night's re-locks within one channel lifetime on B4, through decode
  bursts (`scripts/t6_origin_spread.py`).

## 4. Non-goals

Delay-budget calibration (separate: path-differential vs TS-1 PPS OUT /
analytic filter delay); judge bench policy (rob); reducing the residual
loss itself (worthy, separate).
