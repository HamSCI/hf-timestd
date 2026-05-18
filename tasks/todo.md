# S3 — per-broadcast Kalman coast during a leap-second hold

## Goal
During a CHU-FSK-detected TAI-UTC change (`_fsk_leap_second_hold`), every
measurement is stepped by ~1 s. Increment 2 routed that into the fusion-level
holdover coast, but `_apply_broadcast_kalmans` still ran a full `update()` on
the stepped measurement — corrupting each per-broadcast Kalman's state.

S3: coast the per-broadcast Kalmans (`predict()`, not `update()`) for the
duration of the hold.

Scope: `src/hf_timestd/core/multi_broadcast_fusion.py` — `_apply_broadcast_kalmans` only.

## Fix
`_apply_broadcast_kalmans` reads `self._fsk_leap_second_hold` once; per
measurement, when the hold is active it calls `kalman.predict()` (advance the
motion model, grow covariance) instead of `kalman.update(d_clock, snr)`.
`predict()` already guards the uninitialised case (returns 0.0, 100 ms σ → the
broadcast gets ~zero fusion weight). The hold is brief (~1 cycle), so a single
`predict(dt=1.0)` per cycle is the correct coast.

## Tasks — all done
- [x] git branch `s3-leap-second-coast` off `metrology-physics-review-remediation`
- [x] `_apply_broadcast_kalmans`: predict() vs update() branch on the hold;
      docstring updated
- [x] Tests: `tests/test_fusion_leap_second_coast.py` (3)
- [x] Full suite run

## Review

**Files changed**
- `src/hf_timestd/core/multi_broadcast_fusion.py` — +26 −12 (one block).
- `tests/test_fusion_leap_second_coast.py` — new, 3 tests.

**Behaviour change**
During a leap-second hold the per-broadcast Kalmans coast on their model
instead of ingesting the 1-second-stepped measurement, so their state is no
longer corrupted and they resume cleanly once the hold clears. Combined with
Increment 2 (fusion-level output coast) and S2, a leap second is now handled
coherently at both layers.

**Verification**
- `python -m py_compile`: OK.
- New suite `test_fusion_leap_second_coast.py`: 3/3 — a converged Kalman is
  not pulled by a +1000 ms stepped measurement during the hold; the filter
  resumes normal tracking after the hold clears; an uninitialised filter
  coasts to (0.0, 100 ms σ).
- Full repo suite: 1614 passed, 9 subtests passed (1611 + 3 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.

**Note** — of the agreed sub-decisions, S1 (redefine LOCKED as "≥2 broadcasts
whose per-broadcast Kalmans are converged, sustained ≥3 cycles") was not
implemented: Increment 2 deliberately kept the existing WLS-branch convergence
criterion (`kalman_converged`). That refinement overlaps finding M-M13 and is
not part of the dual-Kalman rework as scoped.
