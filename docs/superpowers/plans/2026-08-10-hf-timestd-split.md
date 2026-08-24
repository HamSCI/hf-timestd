# hf-timestd Split — Phased Implementation Plan

> **For agentic workers:** This is a PHASE-level plan. Each phase is a
> multi-week body of work that gets its own task-level plan (via
> superpowers:writing-plans) when it starts. Do not begin any phase before
> its predecessor's acceptance criteria pass AND the §Gate below is open.

**Goal:** Split hf-timestd into a pure timing-authority core plus four
siblings — wwv-h-iono (WWV/WWVH science), station-web (UI), gnss-vtec
(GNSS vTEC producer) — all standing on a fattened hamsci-dsp (engines +
shared data layer), per `docs/design/HF_TIMESTD_SPLIT_DESIGN.md`.

**Spec:** `docs/design/HF_TIMESTD_SPLIT_DESIGN.md` (this commit).

## Gate

- [x] T6 stage-1 acceptance met: origin spread < 10.4 µs across a night's
      re-locks within one channel lifetime (`scripts/t6_origin_spread.py`
      against the live segment). **MET 2026-08-24: 1.9 µs over 4.5 h incl.
      re-locks and a restart (63 ledger anchors, AC0G-B4)** — see the spec's
      §2 gate stamp for the enabling fixes.
- [ ] rob co-review of the spec's §11 open items (hamsci-dsp charter, names,
      quota contract, org repo creation).  **Partially open 2026-08-24:**
      rob approved the content-time labeling convention (a §5.1-adjacent
      change the split must carry); §11 items proper — charter, names,
      wwv-h-iono org repo creation — still need his eyes.  Repo creation is
      not needed until Phase 3, so Phases 0–2 proceed.
- [x] E1 — WWVB utility evaluation complete, disposition decided
      (spec §12: contributor / witness-only / retire).  **Waived (deferred)
      by mjh 2026-08-24**: `wwvb_*` is on the §5.1 stay-list under every
      disposition, so E1 cannot change the cut; evaluation continues on its
      own track (#23/#25).
- [x] E2 — FUSION-sans-VTEC characterization complete, Phase-2 fallback
      chosen (spec §12: nothing vs climatological IONEX).  **Closed by mjh
      2026-08-24** on §12's interim findings ("gnss-vtec's absence costs only
      the cross-check, not accuracy"): fallback = graceful degradation,
      gnss-vtec stays install-optional; Phases 4–5 deferred by decision.

## Global constraints

- Branch: `main`, no feature branches (fleet convention). Tags for releases.
- Contracts frozen throughout: authority.json path+schema v1; the labelling
  invariant; `/var/lib/timestd` data root; the grape watermark source_id
  byte-identical.
- Every phase ends with its B3 suite green and its B4 soak passed before the
  next begins. Strangler rule: old code keeps working until the single smd
  cutover transaction per phase.
- uv.lock churn trap: `git checkout -- uv.lock` before committing.
- Image-completeness rule: every deployment change lands in catalog/deploy
  manifests and ultimately the DASI image — nothing hand-applied on stations.

## Phase 1 — grow hamsci-dsp, repoint hf-timestd

- [ ] Move engine cluster into `hamsci_dsp.{propagation,ionosphere}`:
      propagation_model, ionospheric_model, iono_data_service, ionex_parser,
      space_weather, tec_estimator, carrier_tec residue, vtec_mapper,
      iono_tomography, solar_zenith_calculator, raytrace_engine, PLUS
      propagation_engine, propagation_mode_solver, arrival_pattern_matrix,
      transmission_time_solver, tec_geometry, tec_validator. Archive
      physics_propagation.py (deprecated), do not migrate.
- [ ] Add `hamsci_dsp.stations`: frozen StationCatalog + built-in HF
      time-station table; engines take `catalog=` defaulting to built-in;
      `wwv_constants.py` re-exports for back-compat, keeps schedules local.
- [ ] Move io/ + schemas/ + data_product_registry to hamsci-dsp.
      Fix `io/calibration_file.py:234` version import → injected string.
      Leave `io/digital_rf_writer.py` behind (zero importers; archive or
      move with grape later). Document WAL/busy_timeout as contract; add
      concurrent-writer soak test.
- [ ] Break up `core/__init__.py` eager imports (~25 modules) into
      per-symbol lazy shims; maintain a shim inventory with owner-phase for
      each symbol's deletion.
- [ ] Declare `[timing.authority_manager]` in the config template.
- [ ] Move engine-level tests to hamsci-dsp; add the import-lint test
      (hamsci-dsp must never import hf_timestd).
- [ ] Update both pyprojects + `[tool.uv.sources]`; release hamsci-dsp.
- [ ] **Acceptance:** B3 both suites green (hamsci-dsp standalone); B4 24 h
      soak — authority.json fresh, chrony SHM within baseline, product
      cadences unchanged, no watchdog restarts.

## Phase 2 — cut fusion's in-loop science

- [ ] Remove TECEstimator from multi_broadcast_fusion (:685, :3530-3620).
- [ ] Audit fused-product schema for TECEstimator-sourced columns —
      null-fill or minor-version; never silently change semantics.
- [ ] Keep `_read_gnss_vtec` DB read + HFPropagationModel.predict (:3452)
      via hamsci-dsp; AuthorityRunner hosting unchanged (:4798).
- [ ] **Acceptance:** B3 fusion tests incl. test_fusion_gnss_vtec_rtp_gate;
      B4 48 h soak — fused offset spread vs Phase-1 baseline, no
      TEC-correction regression, chrony refclock gate never trips.

## Phase 3 — extract wwv-h-iono  (⚠ only phase with outage risk)

- [ ] New repo: physics_fusion_service (freshness + channel discovery ported
      to DB reads — the two HDF5 residues), ionospheric_reanalysis,
      tid_detector, propagation_stats, grape/ + own CLI (`inventory --json`,
      grape subcommands).
- [ ] Units per spec §6 (`wwviono-*`; grape-daily keeps its name); include
      the orphan iri/spaceweather units in deploy.toml.
- [ ] Move `phase2/fusion` mkdir from the physics unit to
      timestd-fusion/tmpfiles.
- [ ] Move the `[[hs_uploader.pipeline]]` grape-psws block verbatim (never
      in both deploy.tomls); `[station]` mirrored in the new config.
- [ ] Verify grape decimation's `/var/lib/timestd/raw_buffer` read path.
- [ ] Own config + init wizard (env-bag defaults, hf-timestd fallback);
      strip physics/vtec stanzas from core watchdog scripts.
- [ ] Single smd cutover transaction: disable old units, install repo,
      enable new units.
- [ ] **Acceptance:** B3 both repos green; B4 48 h — science cadence
      unchanged; grape watermark advances WITHOUT re-scanning old OBS*;
      `physics_products=false` idles cleanly; core uptime continuous;
      prune deletes aged science per contract.

## Phase 4 — extract gnss-vtec  (independent of Phase 3)

- [ ] New small repo: live_vtec.py, gnss_tec.py, ubx_parser, cddis;
      `gnss-vtec.service` + watchdog; `[gnss_vtec]` config moves.
- [ ] Remove vtec from core service_profile/deploy.toml.
- [ ] **Acceptance:** L3_gnss_vtec cadence unchanged; fusion correction
      still applied; disable test — fusion degrades gracefully.

## Phase 5 — extract station-web  (order-independent after Phase 1)

- [ ] Move web-api/ wholesale; absorb timing_validation_service +
      stability_analysis; chrony parsing via hamsci-dsp or
      DIAG_chrony_stats.
- [ ] Config-driven docs roots for living-docs; own config file;
      `station-web.service`.
- [ ] Drop fastapi stack from core pyproject; audit matplotlib/pandas
      remnants.
- [ ] **Acceptance:** all routers 200 against a live DB; living-docs render
      from multi-repo roots; core venv builds without web deps; dashboards
      show live L1–L3 + iono + vtec end-to-end.

## Phase 6 — deployment, catalog, image

- [ ] 4 catalog.toml entries (start_priority timing=50 first); topology.py;
      profiles dasi2/base/client; per-repo deploy.toml + client adapters.
- [ ] install/stop/uninstall script diets; wizard chain;
      sigmond-site-timing refclock path verified; ensure-pylap.sh follows
      raytrace out of core.
- [ ] DASI image rebuild with all five components baked.
- [ ] **Acceptance:** clean-image install converges green from scratch;
      per-client smd start/stop isolation; upgrade path preserves configs
      + grape watermark.
