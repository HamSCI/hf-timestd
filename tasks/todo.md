# Dual-Kalman rework — Increment 3 (M-H12: inverse-variance fusion weights)

## Goal
Make `_calculate_weights` the inverse-variance scheme it claims to be. The bug:
`base_weight = 1/σ²` used `uncertainty_ms`, which the per-broadcast-Kalman pass
drops to `None` → constant fallback → "inverse-variance" weighting did nothing.
Meanwhile SNR was triple-counted (snr_scale, an SNR-boosted confidence, and the
per-broadcast Kalman) and the genuine per-broadcast `kalman_uncertainty_ms` was
computed and never used.

Scope: `src/hf_timestd/core/multi_broadcast_fusion.py` only.

## Decisions
- `σ_i = kalman_uncertainty_ms` (declared on the `BroadcastMeasurement`
  dataclass). `w_i = trust_i / σ_i²`.
- `trust_i = grade × mode(×ambiguity) × station_priority` — a small
  non-statistical scalar. `snr_scale` and `confidence` dropped from the
  per-measurement weight: SNR already lives in σ_i (the per-broadcast Kalman
  is fed snr_db); confidence is SNR-contaminated.
- WLS uncertainty `= max(√(1/Σw), weighted_scatter)`, extracted into the pure
  static helper `_wls_uncertainty` so it is directly testable.
- Stay atomic (per user): the line-4163 `uncertainty = measurement_uncertainty`
  overwrite is M-H15, left untouched. So `wls_uncertainty` feeds the holdover
  base uncertainty and the lock gate now; the reported LOCKED uncertainty still
  routes through the RSS budget until M-H15 is done.

## Tasks — all done
- [x] git branch `dual-kalman-increment-3` off `dual-kalman-increment-2`
- [x] Declare `kalman_uncertainty_ms` on the `BroadcastMeasurement` dataclass
- [x] `_apply_broadcast_kalmans`: set it via the constructor
- [x] `_calculate_weights`: σ_i = kalman_uncertainty_ms; drop snr_scale +
      confidence; trust scalar = grade × mode × station_priority; σ fallback
      chain (kalman → uncertainty_ms → 1 ms) with a 0.05 ms floor
- [x] Add `_wls_uncertainty` static helper; LOCKED branch uses it
- [x] Tests: `tests/test_fusion_wls_weighting.py` (9); update one Increment 2
      test for the new convergence timing
- [x] Full suite run

## Review

**Files changed**
- `src/hf_timestd/core/multi_broadcast_fusion.py` — +91 −57.
- `tests/test_fusion_wls_weighting.py` — new, 9 tests.
- `tests/test_fusion_l3_kalman_removal.py` — 1 assertion relaxed (see below).

**Behavioural change — cold-start convergence timing**
The lock gate (`kalman_converged` ⇐ `wls_uncertainty < 3 ms`) now uses the
genuine WLS uncertainty. A cold start has fresh per-broadcast Kalmans (σ≈10 ms),
so cycle 1's WLS uncertainty is ≈4.2 ms → status is honestly `REACQUIRING`;
`LOCKED` follows on cycle 2 once those filters converge. The old code locked on
cycle 1 because it used the a-priori RSS budget, ignoring per-broadcast
convergence. Warm restarts are unaffected — per-broadcast Kalman state is
persisted (Increment 1), so they load converged and lock immediately. The
Increment 2 test `test_locked_cycle_outputs_weighted_mean` dropped its
`kalman_state == 'LOCKED'` assertion accordingly (its real subject — output is
the bracketed WLS mean — is unchanged); `TestConvergenceTiming` now covers the
REACQUIRING→LOCKED progression explicitly.

**Verification**
- `python -m py_compile`: OK.
- New suite `test_fusion_wls_weighting.py`: 9/9 — weight ∝ 1/σ², SNR no longer
  changes the weight, σ fallback chain, `_wls_uncertainty` formula (agreement→
  formal, disagreement→scatter, weighted, zero-weight→NaN), wiring, and
  cold-start REACQUIRING→LOCKED.
- Full repo suite: 1609 passed, 9 subtests passed (1593 + 16 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected —
  fails identically on the base branch.

**Not done here (tracked elsewhere)**
- M-H15: the `uncertainty = measurement_uncertainty` overwrite (line ~4163)
  still clobbers the LOCKED-cycle reported uncertainty. Until then the new
  WLS uncertainty drives the holdover base + lock gate but not the value
  reported to chrony on a LOCKED cycle.
