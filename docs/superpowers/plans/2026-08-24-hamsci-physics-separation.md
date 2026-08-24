# hamsci-physics separation — execution plan (2026-08-24)

**Status:** ACTIVE — supersedes the Phase-3 naming and narrows the scope of
`2026-08-10-hf-timestd-split.md`; that plan's §5 checkboxes remain the
canonical work ledger for Phases 1–2 and its frozen contracts stand
unchanged.  Work lives on the `split-phase1` branches of hf-timestd and
hamsci-dsp (unpushed; ultra review #1 targets the branch pair).

## 0. Decisions taken (mjh, 2026-08-24)

| decision | choice | note |
|---|---|---|
| Scope | Phases 0–3 now; Phases 4 (gnss-vtec) and 5 (station-web) deferred | gate stamps in the spec §2 and plan §Gate |
| Science repo name | **hamsci-physics** (was `wwv-h-iono`) | operator override of the spec §3 signal-type naming; ⚠ flag for rob's §11 pass — the old rationale ("`timestd-physics` perpetuates the union") is answered by the aggregate framing: one science client, WWV/H-iono orchestration first, room for more analyses without a repo per signal |
| E1 (WWVB) | waived — `wwvb_*` stays core under every disposition | evaluation continues on #23/#25's own track |
| E2 (fusion-sans-VTEC) | closed on spec §12 findings | fallback = graceful degradation; gnss-vtec install-optional |
| Convention adoption (content-time labels, rob-approved) | queued **behind** this separation | flip default + floor-fed `label_plane_offset_ns` + HPPS one-liner + docs #12/#38 |

## 1. Phase 1 — fatten hamsci-dsp (IN FLIGHT, ~40%)

Done on the branches (hamsci-dsp 55c543b, 009efca, 23de610; hf-timestd
30a6d67, 0256b88):

- `hamsci_dsp.stations` — frozen StationCatalog DI seam; `wwv_constants`
  re-exports (coords, tuples, STATION_LOCATIONS, frequency lists).
- Leaves: `ionosphere.ionex` (**with a live-bug fix**: the parser's
  longitude fill position reset per continuation line, so IONEX VTEC read
  ~0 beyond the first 16 longitude columns of every latitude row — a
  science-facing disclosure for rob), `ionosphere.space_weather`
  (cache-dir + HTTP-session DI; no `requests` dep in the library).
- L0 engines: `propagation.{tec_estimator,carrier_tec,vtec,engine}`,
  `ionosphere.tomography`, `raytrace` (replaces the stub; catalog-DI
  station table, optional receiver coords + `_require_receiver()`).
- 8 engine test files ported (141 pass, 1 importorskip pending L3).
- hf-timestd shims: ionex_parser + space_weather committed; the six L0
  shims are WRITTEN but uncommitted pending the full-suite gate —
  star-import shims drop underscore names (raytrace fixed by explicit
  re-export; two worker tests still open on module identity).

Remaining, in dependency order (each: move → shim → port tests → suite
gates on both sides; gate = failure set byte-identical to the 28-failure
baseline):

1. **L1** `ionospheric_model` → `ionosphere/model.py` — MERGE, keeping the
   existing `IonoState`/`ionosphere_state` API; rewrite its IONEXParser /
   hop_geometry imports; space-weather via the moved service.
2. **L2** `iono_data_service` → `ionosphere/data_service.py` (audit its
   cache paths for DI), `tec_validator`, `transmission_time_solver`
   (catalog DI for STATIONS).
3. **L3** `propagation_model` → `propagation/model.py` (catalog DI; the
   deferral test un-skips), then **Phase 2's cut lands here**: remove
   fusion's unconditional `TECEstimator` import (`multi_broadcast_fusion`
   :685), keep the guarded `HFPropagationModel` seam importing hamsci-dsp.
4. **L4** `propagation_mode_solver`, `arrival_pattern_matrix` (catalog DI;
   wwv_constants schedule keys stay hf-timestd-local).
5. Data layer: `io/` + `schemas/` + `data_product_registry` →
   hamsci-dsp; `calibration_file` version becomes an injected string;
   `digital_rf_writer` does NOT move.
6. `core/__init__.py` eager-import breakup (30 imports → lazy/shim);
   declare `[timing.authority_manager]` in the config template.
7. Import-lint test (hamsci-dsp never imports hf_timestd); pyprojects +
   `[tool.uv.sources]` already wired (editable sibling); version bump.
8. Acceptance: both suites green (B3), 24 h B4 soak.

Tooling: `scratchpad/move_engine.py` + `port_test.py` (namespace map
inlined); suite runner for hamsci-dsp is hf-timestd's venv
(`hf-timestd/.venv/bin/python -m pytest tests`) — the library is an
editable sibling; its own `uv sync` is unsatisfiable (pylap extra).

## 2. Phase 2 — cut fusion's in-loop science (after L3)

Per the 08-10 plan: remove TECEstimator from fusion; audit the fused
product schema for TECEstimator-sourced columns; keep `_read_gnss_vtec`
DB read + `HFPropagationModel.predict` (now from hamsci-dsp).
Acceptance: fusion tests incl. `test_fusion_gnss_vtec_rtp_gate`; 48 h B4
soak.  **Ultra review #1 runs here**, on the branch pair, before Phase 3.

## 3. Phase 3 — extract hamsci-physics

- **Repo birth (mjh action):** create `HamSCI/hamsci-physics`, push perms;
  rob §11 sign-off covers charter/name/quota alongside.
- Moves: `physics_fusion_service`, `ionospheric_reanalysis`,
  `tid_detector` (WWV-specific orchestration), `propagation_stats`,
  `grape/`; the deploy-toml-orphan units (`timestd-iri-update.*`,
  `timestd-iri-healthcheck.*`, `timestd-spaceweather-healthcheck.*`).
- Unit renames: `timestd-physics` → `hamsci-physics-fusion`;
  `timestd-iono-reanalysis` → `hamsci-physics-reanalysis`;
  `timestd-ionex-download` → `hamsci-physics-ionex-download`; IRI/
  spaceweather healthchecks take the `hamsci-physics-` prefix.
  **`grape-daily` keeps its name** (operator familiarity; only the
  hs-uploader `source_id` is pinned byte-identical).
- Port fixes bundled with the move (HDF5-purge residue): freshness scan
  and `_discover_channels` become DB reads.
- GRAPE's three hooks: own CLI entrypoint + unit in the same cutover;
  `[[hs_uploader.pipeline]]` block moves verbatim, never in both
  deploy.tomls; verify the raw_buffer read path in detailed design.
- Contract: hamsci-physics ships `deploy.toml`, `inventory --json`,
  configurator, sigmond catalog entry (`start_priority` after timing=50);
  config `/etc/hamsci-physics/config.toml` with an identical `[station]`
  block; `[metrology].physics_products = false` stays a supported
  degraded mode (services idle cleanly).
- Cutover: units replaced wholesale in one smd transaction; never edit in
  place; never Alias across repos.  Acceptance: 48 h B4 soak; grape
  uploads verified on wd30/PSWS watermarks unchanged.  **Ultra #2** on
  the slimmed timing core after this lands.

## 4. Frozen contracts (unchanged from the 08-10 spec §4.1)

authority.json schema/path/writer; RTP-reference labelling invariant;
`/var/lib/timestd` data root + shared SQLite DB + WAL policy;
grape watermark `source_id`; quota_manager as sole disk-budget owner;
timing core module list (§5.1) untouched.

## 5. Open items being carried

- rob §11: charter, **hamsci-physics name ratification**, quota contract,
  org repo creation (needed at Phase 3, not before).
- IONEX parser bug disclosure (science products that consumed IONEX VTEC
  before 2026-08-24 saw ~0 off the first 16 longitude columns).
- Convention adoption work (queued behind separation; mechanism already
  shipped: `label_plane_offset_ns`, 746c7e6).
- Paul's ε bench test (advisory; carry ε = 10 µs ± 20 µs).
