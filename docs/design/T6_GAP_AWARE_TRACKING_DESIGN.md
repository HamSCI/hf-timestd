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

**FINAL LOCALIZATION (2026-08-11, Task 0 executed):** the calibrator and
fine stage are EXONERATED — synthetic-gap tests with honest labels
XPASSed on first run (coarse MF holds the edge; the fine stage discards
the gap-spanning fold block and re-registers on-grid; both now guarded
by regression tests in `tests/test_bpsk_calibrator_gap_tracking.py`).
The defect is the LABEL SOURCE:

    core_recorder_v2._t6_on_samples:
        self._t6_calibrator.process_samples(samples, quality.last_rtp_timestamp)

`quality.last_rtp_timestamp` is the RTP header of the most recently
RECEIVED packet (ka9q-python stream.py:572, stamped pre-resequencer),
while `samples` is the resequenced/zero-filled (and, under consumer
stalls, deque-eviction-shortened) output. In steady state the mismatch
is near-constant (absorbed into chain delay) with packet-boundary
jitter = the measured ±60 two-state wobble; during stall-and-catchup the
label races ahead of the delivered backlog and re-syncs = the measured
block-quantised slips and the cancelling ±N-block pairs in the overnight
CSV. One line explains every signature.

**Fix (minimal, at the seam):** the resequencer already tracks the true
timestamp of every emitted sample (`next_expected_ts`). Expose the
delivered chunk's first-sample RTP (`quality.delivered_rtp_start`,
maintained across batching in RadiodStream) and use it in
`_t6_on_samples` (fallback to the old field + a one-time warning when
absent, for version skew). Honest labels turn every loss into the
exact case the components are proven to handle.

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
