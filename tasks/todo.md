# Metrology/physics remediation — P-M batch

Branch `metrology-physics-review-remediation`. One finding/cluster per
commit. Source: `docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md`.

## Done this session
- [x] **S2** (`0fac1d2`) — hop geometry consolidated onto
      `core/hop_geometry.py`; resolves M-M29, P-M12, P-M18, P-M19.
      (P-H8, P-H15 found already-done in `74024e3` / `ef24c62`.)
- [x] **P-M11** — `ionospheric_model` IRI cache: wall-clock TTL removed
      (a slot-keyed hit is always valid for the deterministic model),
      eviction made genuinely LRU, `_calculate_cache_ttl` /
      `_cache_ttl_seconds` deleted. P-M11's `_extract_scalar` half was
      already done in `c9117b3`. Tests: `test_ionospheric_iri_cache.py`.

## Remaining P-M (clean, one commit each)
- [x] **P-M13/P-M14/P-M15** `propagation_model` cluster — IRI tier uses
      real IRI TEC (surfaced on `LayerHeights.tec_tecu`); differential
      delay differences a shared mode (geometric cancels); `predict()`
      cache gains an `enable_cache` flag + documented monotonic-time
      assumption.
- [x] **P-M16** `iono_data_service` — temporal interpolation between the
      previous/current WAM-IPE grids; grid validation (ascending coords,
      finite physical fields); great-circle km GIRO distance.
- [ ] P-M17 `raytrace_engine` — R12 from solar feed; `spawn`; vectorise
- [ ] P-M20 `physics_fusion_service` — `_timed_write` thread leak
- [ ] P-M21 `physics_fusion_service` — full-table-scan reads → tail-read
- [ ] P-M22 `physics_fusion_service` — F2 virtual height hard-coded 300 km
- [ ] P-M23 `ionospheric_reanalysis` — foE formula; Es geometry; per-path MUF
- [ ] P-M24 `ionospheric_reanalysis` — `process_hour` not idempotent
- [ ] P-M25 `physics_service` — MOOT, module deleted by P-H28; verify only
- [ ] P-M26 `tid_detector` — TDOA solver geometry/conditioning/confidence

## Then
M-M (§3.3; M-M29 already done), Low (§3.4, §4.4), docs (§5), P-H29 (TID
L3 wire-in, deferred).

## Workflow
`uv run --frozen --extra dev pytest tests/` — `--frozen` keeps uv.lock
pinned. Known time-of-day flakes (deselect / not regressions):
`test_geometric_prediction`, `test_fusion_gnss_vtec_rtp_gate`.
