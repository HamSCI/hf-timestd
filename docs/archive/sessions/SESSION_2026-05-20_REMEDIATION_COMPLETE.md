# Session 2026-05-20 — Metrology/Physics Remediation Complete

**Date:** 2026-05-20  
**Branch:** `main`  
**Outcome:** All findings from `docs/CODE_REVIEW_2026-05-17_METROLOGY_PHYSICS.md` resolved (including deferred P-H29).  
**Suite:** `1993 passed, 1 deselected` (standing time-of-day flake `test_geometric_prediction`).

---

## What this session closed

The 2026-05-17 code review identified ~70 findings across the
metrology and physics pipelines.  This session brought the remediation
arc to completion: the 20-commit `metrology-physics-review-remediation`
branch was merged into `main`, and the remaining documentation, Low,
and deferred P-H29 work landed directly on `main`.

**Status as of `6ab5d5b`:**

| Category | Status |
|---|---|
| Critical & High (P-H1..P-H33, M-H1..M-H21) | ✅ all resolved |
| Physics-Medium (P-M1..P-M26) | ✅ all resolved |
| Metrology-Medium (M-M1..M-M35) | ✅ all resolved |
| Structural (S1..S4) | ✅ S2/S3/S4 resolved; S1 (contract refresh) folded into the closed D-H7 work |
| Documentation §5 (D-C1, D-H1..D-H8) | ✅ all resolved |
| Low §3.4 + §4.4 (~30 findings across 25 modules) | ✅ all resolved |
| **P-H29 (deferred at start of session)** | ✅ **closed today** |

**All 8 PHYSICS_CONTRACT Known Deviations** (D1–D8) are now resolved;
contract bumped 1.2.0 → 1.3.0.

---

## Today's commits on `main` (chronological)

The first row is yesterday's `--no-ff` merge of the remediation branch;
all subsequent commits landed today (2026-05-20).

| SHA | Scope | What |
|---|---|---|
| `2387c3b` | Merge | 20-commit no-ff merge of `metrology-physics-review-remediation` (S2/S3/S4 + all P-H + all P-M + all M-M) |
| `f10a292` | Docs D-H1/2/3 | Version banners → 7.0.0 across 6 docs; BPM weight 30%→0%; ARCHITECTURE dead link + stray-header repair |
| `aa75ef7` | Docs D-C1, D-H8 | `SCIENTIFIC_CAPABILITIES.md` SUPERSEDED banner; new `docs/OVERVIEW.md` entry point |
| `9efa984` | Docs D-H4/5/6/7 | TECHNICAL_REFERENCE HDF5 → SWMR; METROLOGY §4.5 ✅/⚠️/❌ status table; PHYSICS §3.1 split; contracts 1.1.0 → 1.2.0 |
| `1dcf138` | Docs | Stamp Known-Deviations refresh date |
| `00f94ac` | §3.4 Low | `tick_edge_detector` — half-template no-op; `is_clean_minute` docstring; 999 ms → `inf` |
| `4a18525` | §3.4 Low | `tick_matched_filter` — drop dead `_envelope_buffer`; honest `phase_rad` docstring |
| `433f701` | §3.4 Low | `tick_pll_decoder` — refresh A/B-test header; guard `np.mean([])` |
| `bbeec0b` | §3.4 Low | `buffer_timing` + `metrology_engine` — document `no_timing` sentinel; fix stale `metadata_fallback` gate |
| `f070e2c` | §3.4 Low | `metrology_engine` — drop dead `bpm_calibration` / `_save_calibration`; document `_last_*` single-owner discipline |
| `d87e708` | §3.4 Low | `multi_broadcast_fusion` — reconcile header GRADE/MODE weight tables with the actual code |
| `2ff35e2` | §3.4 Low | `broadcast_kalman_filter` — remove dead `check_gpsdo_continuity`; tolerance-compare anchor frequencies |
| `4c4cb0c` | §3.4 Low | `chrony_shm` — reconcile `poll` interval to canonical `poll 4` |
| `2174c92` | §3.4 Low | `metrology_service` — refresh stale "no clock offset" docstring; summarise `status.json` |
| `bcf2266` | §3.4 Low | `l2_calibration_service` — replace per-poll `*.h5` glob with `os.scandir` |
| `3ba9454` | §3.4 Low | `arrival_pattern_matrix` — 2-tuple example; parametric heights; float-key invariant |
| `8e9354f` | §4.4 Low | `tec_estimator` — drop unused `field` import; document SNR-weighting formula |
| `cef88ee` | §4.4 Low | `carrier_tec` — drop unused imports; `frequency_mhz` guard; document `carrier_phase_rad==0.0` heuristic limit |
| `d3f88aa` | §4.4 Low | `tec_geometry` — validate lat/lon at boundary; cap obliquity factor at sibling-match 10 |
| `b81d76b` | §4.4 Low | `vtec_mapper` — drop hard-coded receiver-coord defaults; consolidate polynomial-term enumeration |
| `7c21e06` | §4.4 Low | `iono_tomography` — require N≥3 paths; namespace `path_residuals` keys; document `effective_hmF2_km` as input-echo |
| `632fc3c` | §4.4 Low | `propagation_engine` — document `_estimate_heuristic` factors |
| `0d10a8b` | §4.4 Low | `ionospheric_model` — normalise `_location_key` to UTC; remove dead `calculate_hf_reflection_point` |
| `76b3e10` | §4.4 Low | `propagation_model` — `vacuum_fallback` uses hop-geometry + iono; drop redundant `ImportError` catch |
| `a8c5691` | §4.4 Low | `physics_propagation` — document never-functional pylap Tier-1 branch in deprecated header |
| `379a5b5` | §4.4 Low | `iono_data_service` — remove dead `_save_grid_cache` |
| `fe3cd1d` | §4.4 Low | `raytrace_engine` — fix foF2/NmF2 docstring contradiction; document `sys.path` mutation |
| `db03ba1` | §4.4 Low | `physics_fusion_service` — `os.scandir` freshness check; tighten `Dict[tuple, …]` annotations |
| `61d94dc` | §4.4 Low | `ionospheric_reanalysis` — derive `FOF2_NOON_MHZ` from `R12_MODERATE` |
| `eeaa1bf` | §4.4 Low | `tid_detector` — lift `math`/`itertools` imports; document event-list lifecycle + future bound |
| **`6ab5d5b`** | **P-H29** | **TID L3 wiring: new `l3_tid_v1.json` schema, `('L3', 'tid')` registry entry, `PhysicsFusionService.tid_writer` + `_run_tid_detection_cycle`, rewritten `web-api/services/tid_service.py`** |

29 commits added to `main` today on top of the merge commit (`2387c3b`).

---

## P-H29 details (today's headline work)

The TIDDetector statistical engine (P-H30..P-H33 + P-M26) was
statistically sound after the remediation branch but its outputs went
nowhere — `_active_events` and `_completed_events` were never
populated, no writer existed, and `web-api/services/tid_service.py`
read a bespoke per-date directory tree (`phase2/science/tid/<YYYY-MM-DD>/`)
that *nothing* in the pipeline wrote.  Commit `6ab5d5b` closes the
loop:

* **Schema** — `src/hf_timestd/schemas/l3_tid_v1.json` defines the L3
  TID-event record: one row per `TIDEvent` returned by `detect_tid()`.
  15 fields including `period_minutes`, `amplitude_ms`, `velocity_m_s`
  (`allow_nan`), `direction_deg` (`allow_nan`), `correlation_coefficient`,
  `significance_p`, `confidence`, leading/lagging path names, lag.
  MSTID / LSTID classification + quality-flag block included.

* **Registry** — `('L3', 'tid') → 'l3_tid_v1.json'` /
  `'fusion:tid'` added to `data_product_registry.py`.  Product
  resolves to `phase2/fusion/tid/`, next to `fusion_timing`,
  `d_clock`, `tec`, `gnss_vtec`.

* **Writer + run-loop** — `PhysicsFusionService.__init__` constructs
  a `TIDDetector` instance (2-hour buffer, 60-s sample interval) and
  a `tid_writer` via `make_data_product_writer` at the
  registry-resolved path.  New `_run_tid_detection_cycle(...)` runs
  as step 9 in `process_minute`: feeds one `PathResidual` per
  `(station, frequency_mhz)` for the current minute (median across
  modes), calls `detect_tid()`, and writes any returned event.
  Wrapped in best-effort try/except so a TID-pipeline failure can
  never crash the timing-critical fusion cycle.

* **Web API** — `web-api/services/tid_service.py` rewritten end-to-end
  to read the new L3 product via `make_data_product_reader`.  Public
  API (`get_recent_events`, `get_events_in_range`, `get_event_details`,
  `get_statistics`) unchanged so `routers/tid.py` is untouched.
  NaN velocity/direction values pass through as `None` for JSON
  cleanliness.

* **Tests** — 10 regression tests in `tests/unit/test_tid_l3_writer.py`
  cover the registry, schema (with `allow_nan` on velocity/direction),
  writer/reader round-trip, NaN-tolerance write, empty-directory
  service behaviour, end-to-end write→`TIDService.read`, and
  `_run_tid_detection_cycle` happy path on empty + synthetic data.

---

## Branch preservation

The original `metrology-physics-review-remediation` branch is preserved
on origin as the per-finding audit trail for the 20-commit remediation
pass.  Per-commit messages there carry the full per-finding
explanations; the summary entries in `CHANGELOG.md` and this document
provide the cross-reference.

---

## Pre-handoff for the next session — HDF5 → SQLite cutover

The next session takes on the HDF5 → SQLite cutover (Phase 3a — flip
readers to SQLite — through Phase 4 — remove HDF5 + h5py).  Detailed
runbook lives in:

* [`docs/HDF5-TO-SQLITE-MIGRATION.md`](../HDF5-TO-SQLITE-MIGRATION.md) — the canonical plan.
* `tasks/todo.md` — updated this session to remove the now-complete
  M-M tracking and reflect the cutover-pending state.
* [`scripts/parity_check_all.sh`](../../scripts/parity_check_all.sh) — covers every product; exit 3 = PENDING.

**State at handoff** (per the project memory `project_hf_timestd_sqlite_cutover`):

* Phase 1 (reader foundation), Phase 2 (all producers dual-write), and
  Phase 3a-prep (all consumers backend-agnostic) are committed.
* On bee1, all 13 data products were dual-writing as of 2026-05-19; the
  last parity-window summary was `60 OK / 10 SKIP / 0 FAIL / 0 PENDING`
  on the first full 9-channel sweep.
* The cutover runbook starts with a parity-window check
  (`sudo journalctl -u timestd-sqlite-parity --since "2026-05-19 11:30" | grep -E 'Summary|FAIL|PENDING'`)
  and proceeds to the flip (`read_sqlite=true` in both
  `/etc/hf-timestd/timestd-config.toml` and the repo
  `config/timestd-config.toml`, restart consumers, watch chrony TSL2
  and the next parity run).
* Branch merge step in the original runbook is **already done** (today,
  via `2387c3b`).  Remediation work (P-H29 etc.) referenced as
  "post-merge" in the runbook is **also done** (this session).  The
  cutover session can proceed directly to parity check → flip.

After the flip is verified:

1. **Phase 3b** — `write_hdf5 = false` in the storage config; SQLite
   becomes the sole writer.  Validate suite + chrony still green.
2. **Phase 4** — remove HDF5 + h5py code paths and the pinned
   `h5py>=3.8.0,<3.16.0` dependency.  The fusion-service h5py memory
   leak (documented in `multi_broadcast_fusion.py`'s `_malloc_trim`
   block) is only fully fixed once fusion holds a *long-lived* SQLite
   connection (still per-cycle today).

**Reader semantic note** (carried over from the cutover memory):
`SqliteDataProductReader` returns `None` for `NULL`, whereas the HDF5
reader fills with `NaN`/`0`/`""` (the `f7ec934` DUT1 bug class).  The
distinction is intentional; downstream code already handles the
SQLite-style nullability where it matters.

---

## Suite state

```
$ uv run --frozen --extra dev pytest tests/ --ignore=tests/integration \
      --deselect tests/test_metrology_engine.py::test_geometric_prediction
1993 passed, 1 deselected, 12 warnings, 15 subtests passed in 141s
```

The deselected test is the standing time-of-day flake
[`project_hf_timestd_flaky_geometric_prediction`] — known F-layer-height
behaviour at certain hours, not a regression.
