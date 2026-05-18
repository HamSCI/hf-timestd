# M-H23 — L2 propagation-mode selection is near-circular

## Finding
`l2_calibration_service.py _calibrate_measurement` chose the propagation mode
by feeding each candidate's own delay back into `identify_mode`:

    candidate_arrival = raw_toa_ms + candidate.total_delay_ms
    identify_mode(measured_delay_ms=candidate_arrival)   # identifies `candidate`

`raw_toa_ms` is a small timing residual (D_clock), not an absolute measured
delay, so `candidate_arrival ≈ candidate.total_delay_ms` and every candidate
self-identified. The "winner" was whichever mode had the loosest
`delay_uncertainty_ms`. `propagation_delay_ms`, `n_hops`, `u_iono ∝ √n_hops`
were all chosen by tautology.

## Fix (pick the climatological primary)
`identify_mode` genuinely cannot be used here — L2 has only the residual, not
a measured delay. `calculate_modes` already returns candidates sorted by delay
and (Tier-1) MUF-feasibility-filtered; the propagation model itself defines its
primary mode as the shortest-delay feasible arrival. So:

- `_calibrate_measurement`: drop the circular loop; the dominant mode is
  `next((m for m in modes if m.viable), modes[0])`.
- `mode_confidence` (drives `u_prop_model` in the uncertainty budget) is now
  sourced from the propagation model: new `ModeCandidate.model_confidence`
  field, set by the Tier-1 path from `prediction.model_confidence`
  (iono-data quality); Tier-2 parametric fallback leaves it 0 (low-confidence).
- `identify_mode` is left intact — still correctly used by
  `back_calculate_emission_time` with a real measured delay.

Scope: `l2_calibration_service.py` (call site) + `propagation_mode_solver.py`
(one dataclass field + one Tier-1 set site). +26 -20.

## Tasks — done
- [x] Add `ModeCandidate.model_confidence`; Tier-1 sets it from the prediction
- [x] `_calibrate_measurement`: replace the circular loop with first-viable pick
- [x] Tests: `tests/test_l2_mode_selection.py` (2)
- [x] Full suite run

## Review
- Files: `l2_calibration_service.py` (+18 -19), `propagation_mode_solver.py`
  (+8 -1); new `tests/test_l2_mode_selection.py` (2 tests).
- New suite 2/2: mode selection is independent of `raw_toa_ms` (varying the
  D_clock residual leaves propagation_mode/n_hops/delay/confidence unchanged,
  while clock_offset still tracks it); the chosen mode equals `calculate_modes`'
  first viable candidate and its confidence is that candidate's
  `model_confidence`.
- Full repo suite: 1628 passed, 9 subtests passed (1626 + 2 new). One
  pre-existing unrelated `test_l2_clickhouse_wire` failure, deselected.
