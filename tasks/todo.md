# Dual-Kalman rework — Increment 2 (M-H13: remove the L3 Kalman)

## Goal
Eliminate the cascaded L3 Kalman in `multi_broadcast_fusion.py`. The
per-broadcast Kalman banks (Increment 1) already smooth each broadcast's
D_clock; WLS optimally combines them. A second Kalman on top violates the
white-innovation assumption (optimistic covariance). `fuse()` now outputs the
WLS weighted mean directly.

Scope: `src/hf_timestd/core/multi_broadcast_fusion.py` only — verified no
external readers of L3-Kalman internals; web-api/tests touch only the
`FusedResult.kalman_state` *string* field, which is kept.

## Decisions
- Kept attribute names `kalman_converged` / `kalman_n_updates` and the
  `FusedResult.kalman_state` string field — repurposed, not renamed
  (minimal blast radius). Comments/docstrings made honest.
- Holdover & leap-second hold coast the fused output on a new
  `last_locked_d_clock` anchor (S2). Per-broadcast predict-only coast (S3)
  remains Increment 3.
- `kalman_converged` now has a single definition: the WLS branch criterion
  (≥2 stations, wls_uncertainty < 3 ms).

## Tasks — all done
- [x] git worktree `dual-kalman-increment-2` off `metrology-physics-review-remediation`
- [x] Delete `_kalman_update` (210 lines)
- [x] `fuse()`: `fused_d_clock = fused_d_clock_raw`; drop use_l2_kalman /
      k_state_active / kalman_uncertainty / correction_alpha / dead
      weighted-scatter measurement_uncertainty
- [x] `self.kalman_n_updates += 1` once per `fuse()` cycle
- [x] `last_locked_d_clock` attr; set in WLS branch
- [x] Holdover branch coasts on `last_locked_d_clock`; `_fsk_leap_second_hold`
      routed into holdover/coast; logs use `fused_d_clock`
- [x] Monotonicity check relocated after the holdover/WLS branch (guards the
      final emitted value)
- [x] `__init__` cleanup (L1/L2 state, P, convergence threshold, drift-window
      attrs, correction_alpha, _updates_since_restart)
- [x] `save_state` / `load_state`: `_kalman_state` blocks removed; legacy keys
      ignored on load
- [x] Shutdown block: `kalman_state[0]` → `last_fused_d_clock`
- [x] Dead `uncertainty_threshold` line removed
- [x] Contradictory architecture comments fixed
- [x] Tests added: `tests/test_fusion_l3_kalman_removal.py`
- [x] Full suite run

## Review

**Files changed**
- `src/hf_timestd/core/multi_broadcast_fusion.py` — net −310 lines
  (141 insertions, 451 deletions).
- `tests/test_fusion_l3_kalman_removal.py` — new, 7 tests.

**Behavioural change**
- `fuse()` output (`d_clock_fused_ms`) is now the WLS weighted mean of the
  per-broadcast-Kalman-filtered, calibrated D_clocks — one fewer smoothing
  stage. Slightly noisier cycle-to-cycle, but the previous covariance was
  optimistic; the discontinuity filter + per-broadcast Kalmans absorb it.
- Holdover (incl. leap-second hold) coasts the output on the last LOCKED
  value and grows uncertainty — it no longer emits a noisy single-broadcast
  mean or a leap-second-stepped value.
- On restart there is no persisted L3 filter state; status shows ACQUIRING
  until the WLS branch re-converges (~1 cycle with 2 stations). Legacy
  `_kalman_state` keys in old calibration JSON are ignored.

**Verification**
- `python -m py_compile` + AST parse: OK.
- New suite `test_fusion_l3_kalman_removal.py`: 7/7 pass — L3 method/attrs
  gone, save/load drops legacy blocks, LOCKED cycle outputs a bracketed WLS
  mean, holdover coasts on the anchor and ignores a 99 ms single-station
  spike, `kalman_n_updates` counts cycles.
- Full repo suite: 1593 passed, 9 subtests passed. One pre-existing
  unrelated failure (`test_l2_clickhouse_wire … test_returns_noop_writer…`)
  — confirmed it fails identically on the base branch (ClickHouse env
  config, untouched by this work).

**Not done here (tracked elsewhere)**
- S3: per-broadcast Kalman predict-only coast during leap-second hold —
  Increment 3.
- Increment 3 (M-H12): `_calculate_weights` uses per-broadcast
  `kalman_uncertainty_ms`; WLS uncertainty = max(√(1/Σw), weighted_scatter).
- `uv.lock` reverted — `uv run` re-resolved `ka9q-python`; out of scope for
  this increment, left for a deliberate dependency bump.
