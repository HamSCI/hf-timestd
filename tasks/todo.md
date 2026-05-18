# TSL3 displaced-reference fix

## Root cause (investigated 2026-05-18, post-Costas-gate deploy)

The BPSK calibrator hops ±100 ms phantom-grid cells. Live evidence: `chain_delay`
moved 565946.7 → 665946.7 → 385946.7 µs — identical sub-100 ms part, only the
100 ms digit changes. Within a cell it is stable to ~200 ns; the whole problem
is *which* cell it locks to.

Mechanism: RF turbulence (phantom/noise edges) resets `pps_consecutive` while the
Costas loop is locked → calibrator drops `locked` → core_recorder reset /
channel re-registration → blind re-acquisition with no phantom protection (both
gates inert during the bootstrap) → lands on a random ±100 ms cell → re-disambiguation
against a re-registered `channel_info` is not displacement-invariant (two
disambiguations gave effective delays 1.677 ms apart). TSL3 churns → chrony `x`.

## Plan

### Part 1 — stop the churn (calibrator: `bpsk_pps_calibrator_mf.py`)
- [x] Once `_acquired`, an edge with `|d| > edge_tolerance` is a phantom: it does
      NOT reset `pps_consecutive` and does NOT walk `_last_edge_rtp` — the lock is
      held straight through phantom bursts (coast).
- [x] Genuine chain-delay step = a persistent run of `STEP_CONFIRM_EDGES` (60)
      off-position edges agreeing on one new sample-of-second → re-home the lock
      (`_note_step_candidate`). Any accepted on-position edge clears the candidate.
- [x] Removed `cascade_tolerance_ms` / `cascade_tolerance_samples`.
- [x] Added `pps_phantom` counter + `_step_candidate_*`; surfaced in phase log
      and `l6_pps` status; `reset()` clears them.
- [x] core_recorder: dropped the `cascade_tolerance_ms` kwarg; added `pps_phantom`.
- [x] Tests: cascade-gate file replaced by `..._step_detection.py` (9 tests);
      costas-gate helper fixed. 31/31 BPSK tests pass.

### Part 2 — re-assessed after Part 1

Investigation finding: the displaced-reference bug was the **churn**, not the
cell. The disambiguation is cell-invariant (`frac(wall_time_sec)` does not
depend on which 100 ms cell the calibrator locked) — a *single* disambiguation
yields correct TSL3 regardless of cell. The bug was repeated reset → re-acquire
→ re-disambiguate, each against a different `channel_info` anchor. Part 1
eliminates the churn → one stable disambiguation. Part 1's step detector also
self-heals a wrong-cell acquisition (the true edge accumulates as a step).

So "Part 2 — deterministic cell selection" is **subsumed by Part 1**. The one
genuine residual is second-order and was NOT the observed failure: `channel_info`
snapshot aging can drift `rtp_to_wallclock`. Tracked separately; needs a careful
read of radiod timing semantics. NOT shipped blind.

## Review

Part 1 implemented on branch `tsl3-displaced-reference-fix` (off `main`).
Root cause: under RF turbulence, off-position phantom edges reset
`pps_consecutive`, the calibrator dropped `locked`, got reset, and re-acquired
blind onto a random ±100 ms phantom-grid cell. Fix: once acquired, a phantom
edge is inert (GPSDO pins the true edge to one cell — anything far off cannot be
it); the lock is held; only a 60-edge persistent run counts as a genuine step.
Verification: 31/31 BPSK tests pass. Real proof is deploy + observe TSL3 hold
one cell. Not deployed.
