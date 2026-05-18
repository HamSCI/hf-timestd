# M-H19 / M-H20 — BroadcastKalmanFilter innovation & timing

## Findings
- **M-H19** — `update()` fed `detect_mode_transition` and `_adaptive_process_noise`
  the residual `measurement - state[0]` computed BEFORE the predict step. The
  true innovation (post-predict) differs by doppler·dt, so the filter's
  defences keyed off the wrong residual.
- **M-H20** — "time since the last mode transition" was tracked two ways: an
  update-counter (`time_since_mode_change`, incremented +1.0/update) feeding
  the adaptive Q and search window, and a wall-clock timestamp
  (`last_mode_transition_time`) feeding `is_converged()`. `load_state` restored
  neither, so a restarted filter believed its last transition was ~10000 s ago
  and could flip "converged" immediately.

## Fix
- **M-H19**: predict the state first, derive ONE innovation post-predict, feed
  it to mode detection, the adaptive Q, and the measurement update. The
  covariance predict is deferred until the adaptive Q is known (it depends only
  on the post-predict innovation).
- **M-H20**: drop the `time_since_mode_change` update-counter; one wall-clock
  base, `last_mode_transition_time`, with all "minutes since" derived via the
  new `_minutes_since_mode_change()` helper; persist `last_mode_transition_time`
  in save/load_state (legacy state files without the key keep the stable
  default).

Scope: `src/hf_timestd/core/broadcast_kalman_filter.py` only. +51 −33.

## Tasks — done
- [x] Reorder `update()`: state-predict → one innovation → defences → covariance
      predict → measurement update
- [x] Replace `time_since_mode_change` with `_minutes_since_mode_change()`;
      thread through `_adaptive_process_noise`, `get_search_window`, `is_converged`
- [x] Persist/restore `last_mode_transition_time` in save/load_state
- [x] Tests: `tests/test_broadcast_kalman_timing.py` (3)
- [x] Full suite run

## Review
- Files: `broadcast_kalman_filter.py` (+51 −33); new
  `tests/test_broadcast_kalman_timing.py` (3 tests).
- New suite 3/3: the defences see the post-predict innovation (3.0 ms, not the
  pre-predict 5.0 ms, for a doppler=2 case); `time_since_mode_change` attr is
  gone; a restarted filter restores a recent transition and therefore does NOT
  immediately self-declare converged.
- The known-flaky `test_restart_stability` (root cause M-H20) passed 8/8
  consecutive runs after the fix.
- Full repo suite: 1622 passed, 9 subtests passed (1619 + 3 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
