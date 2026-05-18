# TSL3 BPSK-PPS Costas-drift — Layer A fix

## Context

The BPSK PPS calibrator (`src/hf_timestd/core/bpsk_pps_calibrator_mf.py`,
feeds chrony refclock TSL3) intermittently has ~10–15 s Costas
carrier-recovery phase excursions. During an excursion the matched filter
produces strong phantom peaks; a phantom inside `cascade_tolerance_ms`
walks `_last_edge_rtp` and TSL3 re-locks biased (live: −1.1 ms on bee1
2026-05-18). Scoped in `docs/TSL3_COSTAS_DRIFT_2026-05-18.md`.

**Layer A**: add a Costas lock-quality signal and gate edge acceptance on
it, so an excursion coasts on the last-good chain delay instead of
re-locking biased.

## Detector design (validated against the 2026-05-08 debug capture)

Per-batch, two cheap tests on the existing Costas phase state:

1. **Motion test** — EMA of the per-batch phase increment
   `|Δφ|` stays below `COSTAS_DPHASE_MAX`. Normal `|Δφ|` EMA ≪ 0.001 rad;
   during an excursion it sits ~0.012–0.015 rad.
2. **Band test** — `|φ − φ_ema|` within `COSTAS_PHASE_BAND`, where `φ_ema`
   is a slow EMA of φ **frozen while the motion test fails** (so it cannot
   chase φ into the excursion). Normal deviation ~1e-4 rad; excursion >5 rad.

`costas_locked = motion_ok AND band_ok`, with a short re-lock debounce
(`COSTAS_RELOCK_SEC`) so acceptance only resumes once φ is fully settled.

Simulation over the real capture (3287 batches, one excursion):
100% locked in both normal regions, 0% locked through the excursion;
unlock at t≈15.1 s (onset), re-lock at t≈29.8 s (after recovery). The
phantom that was actually accepted in the capture was at t=25.6 s — well
inside the gated-off window.

Constants (hardcoded, physically motivated, cited to the capture):
`TAU_PHASE_EMA=10 s`, `TAU_DPHASE_EMA=0.5 s`, `COSTAS_DPHASE_MAX=0.004 rad`,
`COSTAS_PHASE_BAND=0.5 rad`, `COSTAS_RELOCK_SEC=0.5 s`. EMA coefficients
derived per-batch from `dt`, like the existing `_alpha`.

## Plan

- [x] **Detector** — `_update_costas_lock` in `bpsk_pps_calibrator_mf.py`,
      called from `process_samples` after the Costas phase update. New
      state: `_phase_ema`, `_dphase_ema`, `_costas_locked`,
      `_costas_relock_counter`; coefficients derived alongside `_alpha`.
      Public `costas_locked` property added.
- [x] **Gate** — in `_detect_and_record_peaks`, when `_acquired and not
      _costas_locked`: returns before any classification — no
      `_last_edge_rtp` / `pps_*` / `_chain_delay_samples` / `_peak_running`
      mutation (coast). Phantoms still recorded to the debug capture as
      classification `4`. Inert during acquisition.
- [x] **reset()** — new state cleared.
- [x] **Observability** — locked→unlocked (warning) / unlocked→locked
      (info) transition logs in `_update_costas_lock`; `costas_locked` +
      `dphase_ema` added to the periodic phase log and `costas_locked` to
      the `l6_pps` status dict in `core_recorder_v2.py` (via `getattr`, so
      the legacy non-MF calibrator path is safe).
- [x] **Backstop** — DROPPED. `cascade_tolerance_ms` 3.0→1.0 would swap one
      arbitrary ms window for another. The true PPS edge is GPSDO-locked to
      a fixed sample-of-second (zero drift); the real guardrail is the
      Costas gate (phantoms only occur during excursions, now gated off
      before the accept path). Cascade window stays 3.0; whether a no-drift
      system should have a separate "drift" window at all is a Layer-B
      cleanup note.
- [x] **Tests** — new file `test_bpsk_pps_calibrator_mf_costas_gate.py`
      (10 tests): detector white-box (lock debounce, excursion unlock,
      band-test holds through a plateau, re-lock after recovery); gate
      (locked accepts, unlocked coasts, phantom can't walk the reference,
      result keeps flowing, inert during acquisition); one integration
      test through `process_samples` with a swept carrier. Cascade-gate
      helper updated (`_costas_locked=True` = full TRACKING). All 28 BPSK
      tests pass.
- [x] Update the scoping doc status (Layer A implemented).

## Review

Implemented Layer A of the TSL3 Costas-drift fix on branch
`tsl3-costas-drift-fix` (off `origin/main`, with the scoping doc
cherry-picked from `0495d22`).

**What changed**
- `bpsk_pps_calibrator_mf.py`: `_update_costas_lock` detector +
  `costas_locked` property + the gate in `_detect_and_record_peaks` +
  five `COSTAS_*` module constants + transition/phase-log observability.
- `core_recorder_v2.py`: `costas_locked` in the `l6_pps` status dict.
- `test_bpsk_pps_calibrator_mf_costas_gate.py`: new, 10 tests.
- `docs/TSL3_COSTAS_DRIFT_2026-05-18.md`: status → Layer A done.

**Verification**
- 28/28 BPSK calibrator tests pass.
- The *implemented* detector replayed over the real 2026-05-08 capture
  gates the entire excursion (locked t<15.1 s and t>29.8 s, 0/650
  batches locked through the excursion) — the phantom accepted at
  t=25.6 s in that capture falls inside the gated-off window.

**Deferred**
- Layer B (eliminate the excursions) — needs more captures.
- Whether a GPSDO-locked (no-drift) system should keep a separate
  `cascade_tolerance` "drift" window at all — Layer-B cleanup note.

**Deployment note** — not deployed. The fix is automatic (no new config
keys). `cascade_tolerance_ms` was intentionally left at 3.0.
