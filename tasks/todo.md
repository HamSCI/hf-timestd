# S2 — Consolidate HF hop geometry onto one spherical module

Branch: `metrology-physics-review-remediation` (HEAD `c8ea76b`).
Resolves review items **S2, M-M29, P-M12, P-M18, P-M19**.
(P-H8 `tec_geometry` elevation and P-H15 `propagation_model` MUF were
already made spherical in `74024e3` / `ef24c62`.)

## Problem
Hop geometry is reimplemented in ≥4 places with two conventions:
- spherical (correct): `arrival_pattern_matrix._spherical_hop_path`,
  `propagation_model._evaluate_mode`
- flat-Earth (wrong, several % long-path error):
  `propagation_mode_solver._hop_geometry` (M-M29),
  `propagation_engine._estimate_geometric` (P-M19),
  `ionospheric_model.update_calibration` flat-triangle inverse (P-M12),
  `raytrace_engine._geometric_fallback` straight-line (P-M18)

## Plan
- [x] 1. New `core/hop_geometry.py` — spherical law-of-cosines:
      `HopGeometry` dataclass, `hop_geometry()`, `height_from_path()`
      (inverse), `max_single_hop_distance_km()`, `n_hops_for_distance()`.
- [x] 2. `arrival_pattern_matrix._spherical_hop_path` → delegate to
      module (numerically identical — pure de-dup).
- [x] 3. `propagation_model._evaluate_mode` → module for
      slant/path/elevation + `max_single_hop_distance_km`
      (numerically identical).
- [x] 4. `propagation_mode_solver._hop_geometry` (M-M29) → spherical
      via module (behaviour change: flat→spherical).
- [x] 5. `propagation_engine._estimate_geometric` (P-M19) → spherical
      via module; thread `frequency_hz` through; replace the
      frequency-blind ×1.03 with a proper 40.3/f² group-delay term.
- [x] 6. `ionospheric_model.update_calibration` (P-M12) → `height_from_path`
      for both implied and predicted heights (one shared geometry).
- [x] 7. `raytrace_engine._geometric_fallback` (P-M18) → real spherical
      hop slant path + launch elevation + apogee.
- [x] 8. `tests/unit/test_hop_geometry.py`; update CHANGELOG; full suite.

## Review
S2 complete. One spherical hop-geometry module (`core/hop_geometry.py`);
all six call sites delegate to it. The two already-spherical sites
(`arrival_pattern_matrix`, `propagation_model`) are numerically
unchanged; the four flat-Earth sites (`propagation_mode_solver` M-M29,
`propagation_engine` P-M19, `ionospheric_model.update_calibration`
P-M12, `raytrace_engine._geometric_fallback` P-M18) now agree with
them. P-M19 also gained a proper 40.3/f² ionospheric term in place of
the frequency-blind ×1.03.

Verification: 51 new `test_hop_geometry` tests (forward, exact
round-trip inverse, flat-Earth limit, divergence, validation); full
suite green except the two known time-of-day flakes
(`test_geometric_prediction`, `test_fusion_gnss_vtec_rtp_gate`) —
`test_geometric_prediction` confirmed to fail identically on the
pre-change tree, so not a regression. New files black-clean.

`uv.lock` was left untouched — a `ka9q-python` spec drift surfaced by
`uv run` is unrelated to S2 and excluded from the commit.

## Next
Resume the clean P-M items: P-M11 (`ionospheric_model` IRI cache TTL —
P-M11's `_extract_scalar` half already done in `c9117b3`), then
P-M13–P-M17, P-M20–P-M26.
