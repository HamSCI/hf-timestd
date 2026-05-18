# M-H16 / M-H17 — consolidate outlier rejection

## Findings
- **M-H16** — `fuse()` ran outlier rejection twice: a pre-fusion pass (MAD on
  CALIBRATED values, 3.5σ) and a later `_reject_outliers` call (MAD on RAW
  d_clock_ms, 3.0σ). Raw values carry 30-60 ms inter-broadcast offsets that
  swamp the MAD, so the raw pass let real outliers slip through.
- **M-H17** — `_reject_outliers` paired a weighted median with an unweighted
  MAD — statistically incoherent; low-weight outliers inflate the MAD and
  survive.

## Fix (single pass on calibrated residuals)
The pre-fusion pass is already the correct one — MAD on calibrated residuals,
internally consistent (unweighted median + unweighted MAD), with floor/cap and
a never-reject-all guard. So:
- Removed the redundant `_reject_outliers` call (the raw-value second pass).
- Deleted the now-unused `_reject_outliers` method — which resolves M-H17 (the
  weighted/unweighted incoherence) by removal. The method had no other caller
  and no test referenced it.
- The pre-fusion pass now produces `n_rejected` for `FusedResult.outliers_rejected`.
- Module docstring + block comment corrected (weighted→calibrated, 3σ→3.5σ).

Scope: `src/hf_timestd/core/multi_broadcast_fusion.py` only. Net −53 lines.

## Tasks — done
- [x] Delete `_reject_outliers` method + its call; thread `n_rejected` from
      the surviving pass
- [x] Fix the stale module docstring
- [x] Tests: `tests/test_fusion_outlier_rejection.py` (3)
- [x] Full suite run

## Review
- Files: `multi_broadcast_fusion.py` (+13 −66); new
  `tests/test_fusion_outlier_rejection.py` (3 tests).
- New suite 3/3: `_reject_outliers` is gone; a 50 ms outlier is rejected by
  the single pass (fused stays with the ~2 ms cluster); a consistent cluster
  rejects nothing.
- Full repo suite: 1619 passed, 9 subtests passed (1616 + 3 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
