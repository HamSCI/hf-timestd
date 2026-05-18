# M-H15 — fusion uncertainty discards the holdover/WLS term

## Goal
`fuse()` computes a per-cycle `uncertainty` in the LOCKED/holdover branch, then
~140 lines later `uncertainty = measurement_uncertainty` overwrites it with the
static a-priori RSS budget. Consequences:
- Holdover uncertainty growth never reaches Chrony — a dropout looks as certain
  as a live lock.
- The Increment 3 WLS uncertainty was inert on the reported value.

Scope: `src/hf_timestd/core/multi_broadcast_fusion.py` only — one block.

## Fix
Replace the overwrite with an RSS combination:

    uncertainty = sqrt(branch_uncertainty²        # WLS (locked) or holdover term
                       + systematic² + propagation² + rtp_jitter²
                       + tone_detection² + multipath²)

The branch term supersedes the crude `statistical_uncertainty` inside
`measurement_uncertainty` (RSS-ing both would double-count). `measurement_uncertainty`
itself is untouched — still used as the holdover clamp reference and the
holdover-reason log. The dead `DISCRIMINATION_SUSPECT` block (confirmed dead:
`consistency_flag` is only ever 'CROSS_STATION_DISAGREE'/'OK') is left alone.

This also completes Increment 3: the WLS uncertainty now reaches the reported
`uncertainty_ms` on a LOCKED cycle.

## Tasks — all done
- [x] git branch `mh15-holdover-uncertainty` off `dual-kalman-increment-3`
- [x] Replace the `uncertainty = measurement_uncertainty` overwrite with the
      RSS combination; refloor; honest comment
- [x] Tests: `tests/test_fusion_holdover_uncertainty.py` (2)
- [x] Full suite run

## Review

**Files changed**
- `src/hf_timestd/core/multi_broadcast_fusion.py` — +21 −5 (one block).
- `tests/test_fusion_holdover_uncertainty.py` — new, 2 tests.

**Behaviour change**
- Holdover: reported `uncertainty_ms` now grows with dropout duration
  (verified: 1 min → 6.2 ms, 60 min → 6.2 ms, 600 min → 6.9 ms,
  6000 min → 14.6 ms; pre-fix all four were identical).
- LOCKED: reported `uncertainty_ms` is now RSS(WLS uncertainty, systematic,
  propagation, jitter, tone, multipath) — the Increment 3 WLS term is no
  longer discarded. The reported value is slightly different from before
  (the WLS term replaces the crude std/√n statistical term).
- `FusedResult.statistical_uncertainty_ms` is unchanged — still the crude
  weighted-std diagnostic ("Measurement scatter"), as its schema documents.

**Verification**
- `python -m py_compile`: OK.
- New suite `test_fusion_holdover_uncertainty.py`: 2/2 — holdover uncertainty
  monotonically grows with dropout duration; finite & positive.
- Full repo suite: 1611 passed, 9 subtests passed (1609 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
