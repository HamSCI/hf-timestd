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
- [x] **P-M17** `raytrace_engine` — `r12_idx = -1` (IRI sources the
      date-appropriate sunspot index from its own files); raytrace
      subprocess uses `spawn` not `fork` (`_raytrace_worker` lifted to
      module level); IRI Ne-profile range interpolation vectorised.
- [x] **P-M20/P-M21/P-M22** `physics_fusion_service` cluster — per-writer
      write lock (`_run_timed_write`) so a timed-out write neither races
      the next nor leaks threads (P-M20); F2 reflection height sourced
      from the ionospheric model + shared spherical elevation (P-M22).
      P-M21 (full-table-scan reads) resolved by the SQLite cutover —
      `SqliteDataProductReader.read_time_range` is an indexed range query.
- [x] **P-M23/P-M24** `ionospheric_reanalysis` cluster — ITU-R foE
      formula (was 0.3·foF2 / 0.5 MHz); Es relabel gated on hop
      geometry; per-station MUF (was one global MUF); `process_hour`
      idempotent via `_existing_l3c_keys`/`_existing_tec_keys`.
      Also: S2 follow-on — removed flat-Earth `hop_elevation_angle`,
      routed callers through `hop_geometry`.
- [x] **P-M25** — verified moot; `physics_service.py` was deleted by
      `75b8217` (P-H28). No code change.
- [x] **P-M26** `tid_detector` — TDOA solver drops degenerate baselines
      and checks lstsq rank; 2-path fallback uses real pierce-point
      great-circle geometry; event confidence is `1 − significance_p`
      (was the ad-hoc `best_correlation × 1.2`).

## Then
M-M (§3.3; M-M29 already done), Low (§3.4, §4.4), docs (§5), P-H29 (TID
L3 wire-in, deferred).

## Workflow
`uv run --frozen --extra dev pytest tests/` — `--frozen` keeps uv.lock
pinned. Known time-of-day flakes (deselect / not regressions):
`test_geometric_prediction`, `test_fusion_gnss_vtec_rtp_gate`.
