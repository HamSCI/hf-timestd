# T6 Gap-Aware Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:test-driven-development.
> Spec: `docs/design/T6_GAP_AWARE_TRACKING_DESIGN.md`. NO fix lands before
> Task 0's failing test reproduces the field defect.

**Files:** `core/bpsk_pps_calibrator_mf.py`, `core/bpsk_edge_fine_stage.py`,
(possibly) `core/core_recorder_v2.py` disambiguation wiring; tests beside
the existing T6 suites (reuse their synthetic BPSK generators).

## Task 0 — reproduce the slip (failing test first) — DONE 2026-08-11
Outcome: components EXONERATED (tests XPASSed → kept as regression
guards); defect localized to the label source (spec §1). Tasks below
REWRITTEN accordingly; original component-fix tasks 1–3 are OBSOLETE.

## Task 0b — reproduce the LABEL mechanism
- [ ] Test that feeds resequenced-style batches labelled with a
      last-received-header model (label races ahead during a simulated
      stall backlog) and shows the edge slip; flips green with Task 1+2.

## Task 1 — ka9q-python: expose delivered RTP
- [ ] PacketResequencer: return per-chunk first-sample timestamp (it is
      `next_expected_ts` at emission start; fills included).
- [ ] RadiodStream/MultiStream: maintain `quality.delivered_rtp_start`
      across delivery batching; document in REQUIREMENTS (KQP-Q new).
- [ ] Unit tests incl. gap, capped-gap, eviction, wrap.

## Task 2 — hf-timestd: use it
- [ ] `_t6_on_samples` (and the WWVB consumer's equivalent) label batches
      with `delivered_rtp_start` when present; one-time WARN fallback.
- [ ] Bump ka9q-python minimum in pyproject (evaluate against the pinned
      ka9q-radio per the contract discipline).

## OBSOLETE (kept for the record) — original Tasks 1–3
- [ ] Build on the existing synthetic BPSK-PPS generators in the T6 tests:
      stream batches with declared-RTP labels; inject (a) the cancelling
      ±60 wobble pattern (11-batch cycle from the LABEL AUDIT), (b) a
      sustained G-block gap mid-stream, (c) both.
- [ ] Assert: detected edge sequence stays on the true 1 s grid in
      declared RTP; origin before == origin after (<1 sample). EXPECT
      FAIL for (b)/(c) — mark xfail(strict=True) until the fix lands.
- [ ] The failing assertion localizes the defect line (correlation-window
      mixing vs fold registration vs step adoption). Record findings in
      the spec §1.

## Task 1 — divergence ledger (pure, unit-tested)
- [ ] New small class: per-batch cumulative declared-vs-contiguous offset;
      classify transient (returns to baseline ≤K batches) vs sustained
      (new baseline). Constants from measured data (wobble amplitude 60,
      cycle 11; gap sizes packet/block-quantised).

## Task 2 — coarse tracker gap handling
- [ ] Edge prediction + search window in declared-RTP space; on sustained
      shift, carry the window with the ledger's new baseline; invalidate
      correlation accumulation spanning the gap (the boxcar/MF window
      restarts clean after the gap boundary).
- [ ] Step-adoption machinery: a ledger-attributed gap must NOT count
      toward chain-delay step adoption (it is not an RF/DSP change).

## Task 3 — fine-stage gap resync
- [ ] Fold blocks spanning a sustained gap: discard (existing) BUT
      re-register immediately from post-gap declared RTP; estimates
      resume within one fold period; no estimate_stale from a single gap.

## Task 4 — full-suite + offline validation
- [ ] Whole T6 test suite green; Task-0 xfails flip to green (strict).
- [ ] Offline: re-run the estimator sweep on the captured real IQ + the
      180 s NPZ replay if feasible.

## Task 5 — deploy + stage-1 acceptance
- [ ] Deploy to B4 (new station, T6 armed), restart core-recorder.
- [ ] Overnight: `t6_origin_spread.py` — spread < 10.4 µs through decode
      bursts. PASS ⇒ stage-1 accepted ⇒ split gate opens.
