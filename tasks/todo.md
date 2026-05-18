# P-H1/H2/H5/H6 — tec_estimator.py

## Findings & fixes
- **P-H1** (docstring honesty): added an "Operational status — NOT an
  operational product" section — group-delay TEC across the WWV/CHU/BPM
  frequency spans is at/below the timing noise floor; `tec_u` is research-grade,
  consumers must gate on `confidence`.
- **P-H2** (confidence): `confidence` was `r²` — near 1 even on a noise-driven
  slope. Now slope detectability `1 − σ_slope/slope` (clamped 0..1): `_fit_wls`
  returns `σ_slope` (polyfit covariance for N>2; analytic from the two points'
  variances for N=2). New `TECResult.tec_uncertainty_tecu` (1σ of `tec_u`).
  3 stale `R2=` log labels in `multi_broadcast_fusion.py` → `conf=`.
- **P-H5** (negative slope): already fixed (retained with confidence 0 per
  contract CR-2) — guarded by a regression test, no code change.
- **P-H6** (frequency validation): each `frequency_hz` is checked finite and in
  1–60 MHz before reaching `1/f²`; invalid measurements skipped; `None` if <2
  valid remain.

## Behavioural note
`confidence` is now honestly low (group-delay TEC is below the noise floor),
so `multi_broadcast_fusion`'s HF-TEC-correction gate (`confidence > 0.5/0.9`)
fires far less — correct (it was applying noise-derived corrections). Live
impact is limited: that block is already cross-check-only in RTP mode (M-H14).

## Review
- Files: `tec_estimator.py`, `multi_broadcast_fusion.py` (3 log labels);
  new `tests/test_tec_estimator_confidence.py` (6 tests).
- New suite 6/6: clean strong signal → high confidence; pure noise →
  confidence ~0 averaged over 40 realisations (r²-based would average ~1);
  `tec_uncertainty_tecu` populated; invalid frequency skipped / all-invalid →
  None; negative slope retained with confidence 0.
- Full repo suite: 1636 passed, 9 subtests passed (1630 + 6 new).
