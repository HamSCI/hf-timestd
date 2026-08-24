# hf-timestd Split — Design Specification

**Status:** APPROVED DESIGN — implementation gated (see §2)
**Date:** 2026-08-10
**Authors:** Michael (mjh) with Claude; pending co-review by rob
**Supersedes:** extends `METROLOGY_PHYSICS_SPLIT.md` (Feb 2026) from an intra-repo
file-ownership map to a full repository split.

## 1. Problem and goal

hf-timestd predates sigmond and was shoe-horned into client-contract
compliance. It carries three concerns in one repository:

1. **The timing authority** — T-level classification, T6/T5/T3 machinery,
   `authority.json`, chrony SHM feeds, the offset judge.
2. **Ionospheric science** — dTEC/TID/TEC, ionospheric reanalysis, GRAPE.
3. **A web-ui** presenting both.

Goal: the core repository masters *the fundamental time issues sigmond and its
clients need*, and the science stands as a client alongside wspr-recorder /
psk-recorder — consuming the same `authority.json` contract every other
recorder consumes. The seam already exists: the METROLOGY.md RTP-reference
labelling invariant (`label_utc = rtp_time + rtp_to_utc_offset_ns`) is
implemented fleet-wide via `hamsci_dsp.timing.AuthorityReader`. Physics sits on
the wrong side of an existing boundary; this spec moves it across.

Naming is part of the architecture: `timestd-physics` perpetuates the very
union being broken, so the extracted components take new names (§4).

## 2. Sequencing gate

**No split implementation begins until T6 stage-1 acceptance is met**: origin
spread < 10.4 µs (one sample @ 96 kHz) across a night's re-locks within one
channel lifetime (see `T6_ORIGIN_ASSERTION_DESIGN.md`). As of 2026-08-10 the
post-fix spread is ~80 ms and block-quantised — not met. Do not refactor
around a timing core that still re-derives its origin at every re-lock.

**GATE MET — 2026-08-24.** Measured on AC0G-B4: origin spread **1.9 µs over
4.5 h within one channel lifetime, across re-locks and a process restart**
(63 ledger anchors; sub-second span 016628347–016630220 ns), a factor of five
inside the criterion.  Held through the same day's labeling-convention A/B
(two further re-locks, `fine_coarse` 0.003 ms at re-acquisition).  The
enablers, all landed on main and all inside the §5.1 stay-list: the honest
stream (ka9q-radio 55d9048d), the RTP counter-domain fix (`core/rtp_domain`,
f795cbe), the durable anchor ledger (`core/t6_anchor_ledger`, 6e37b42), and
the judge plane-correction mechanism (746c7e6).  Note for the phase work: the
content-time labeling convention (CONTENT_TIME_LABELING_CONVENTION.md,
approved by rob 2026-08-24) retires `filter_group_delay_ns` — config
templates and `_t6_fine_settings` touched by the split must carry that
change, not resurrect the key.

**E1/E2 gate disposition — mjh, 2026-08-24:** E2 (FUSION-sans-VTEC) is
recorded as answered by §12's interim findings ("gnss-vtec's absence costs
only the cross-check, not accuracy") — Phase-2 fallback: fusion degrades
gracefully, gnss-vtec stays install-optional.  E1 (WWVB disposition) is
deferred: `wwvb_*` is on the §5.1 stay-list either way, so it does not block
the cut.  Phases 4 (gnss-vtec) and 5 (station-web) are deferred by decision;
Phases 0–3 proceed.

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Propagation/iono model home | **hamsci-dsp** | Charter: signal-agnostic libraries "to study any signals from any source"; the graph already flagged `core/hop_geometry.py` as semantically equivalent to it |
| Repo shape | Core + science + web-ui + one new client | Each concern independently versioned and deployed |
| Science repo name | **wwv-h-iono** | Signal-type naming, in family with wspr, psk, hfdl, codar, hf-tec |
| Web-ui repo name | **station-web** | Org-neutral station dashboard |
| GNSS vTEC service | New client repo **gnss-vtec** | Neither timing-authority work nor a WWV/WWVH signal |
| Shared data layer (io/ + schemas) | Moves to **hamsci-dsp** | Single DB stays; all repos meet at the library, not at each other |
| Migration strategy | **Strangler, library first** | hf-timestd keeps working at every step; each phase soaks on B3/B4 |

`hf-tec` (PRN-coded Hysell-network beacon TEC) is a different instrument — no
collision with wwv-h-iono; complementary members of the signal-type family.

## 4. Target architecture (5 repos)

```
                    hamsci-dsp  (shared, signal-agnostic)
                    ├── geometry, dsp, propagation math   (existing)
                    ├── timing.py authority reader        (existing)
                    ├── + propagation/iono engines        (station tables injected)
                    └── + data-product layer              (io writer/reader, schemas)
                         ▲            ▲            ▲            ▲
   hf-timestd (core)   wwv-h-iono   station-web   gnss-vtec
   timing authority    WWV/WWVH     FastAPI UI    u-blox vTEC
   T6/T5/T3, fusion,   science:     timing +      producer of
   authority.json,     dTEC/TID/    science       L3_gnss_vtec
   chrony SHM, judge,  TEC, iono-   dashboards
   metrology, L2-cal   reanalysis,  (reads DB)
                       GRAPE
```

### 4.1 Frozen contracts

- `authority.json` at `/run/hf-timestd/authority.json`, schema v1, written
  solely by AuthorityManager inside the fusion service (the §4.5.2
  METROLOGY.md coupling rule: authority.json + chrony SHM + mDNS decay in
  lockstep). Consumed via `hamsci_dsp.timing`; `DEFAULT_PATH` unchanged.
- The RTP-reference labelling invariant: every client — including wwv-h-iono —
  labels as `rtp_time + published_offset`, never the host clock.
- Shared SQLite data-product DB stays at `/var/lib/timestd/phase2/timestd.db`;
  `/var/lib/timestd` remains the shared data root. The grape hs-uploader
  watermark `source_id = "grape-datasets:/var/lib/timestd/upload"` is pinned —
  byte-identical forever, or shipped OBS* datasets re-ship.
- With four repos writing one SQLite DB, the WAL/busy_timeout policy becomes
  part of the contract: documented in hamsci-dsp, plus a concurrent-writer
  soak test.
- Only science→timing data dependency: `L3_gnss_vtec` (produced by gnss-vtec,
  read by fusion's D_clock ionospheric correction). Soft — fusion degrades
  gracefully without it.

## 5. Module disposition

### 5.1 hf-timestd keeps (timing core)

core_recorder_v2; all T6/T5 machinery (`bpsk_*`, `t6_anchor_authority`,
`native_anchor`, `t5_*`, `lb1421_*`); `offset_judge`;
`authority_manager`/`authority_runner`; `chrony_shm` + `chrony_stats`;
`mdns_fusion_advertiser` (authority-manager-only consumer; zeroconf stays a
core dep); `metrology_service` + `metrology_engine`; `l2_calibration_service`;
`multi_broadcast_fusion` (hosting AuthorityRunner); `wwvb_*`; RTP
stream/recorders; `quota_manager`; `tools/` T6 harnesses; `archive/`
(untouched — dead code, do not migrate).

### 5.2 → hamsci-dsp (Phase 1)

Engines: `propagation_model` (HFPropagationModel), `ionospheric_model` (IRI
wrapper; replaces the 58-line stub), `iono_data_service` (GIRO/WAM-IPE),
`ionex_parser`, `space_weather`, `tec_estimator`, `carrier_tec` residue,
`vtec_mapper` math, `iono_tomography`, `solar_zenith_calculator`,
`raytrace_engine` (replaces the 55-line stub), **plus the full propagation
cluster that staying core modules import**: `propagation_engine`,
`propagation_mode_solver`, `arrival_pattern_matrix`,
`transmission_time_solver`, `tec_geometry`, `tec_validator`. Without these,
hf-timestd would end up importing wwv-h-iono. Merge into the existing
`hamsci_dsp.{propagation,ionosphere}` namespaces — no parallel namespaces.
`physics_propagation.py` is deprecated: archive, don't migrate.

Data layer: `io/` (writer/reader factories, uncertainty, calibration_file,
authority_snapshot_store) + `schemas/` registry + `data_product_registry`.
Exceptions: `io/digital_rf_writer.py` has zero importers — do NOT move (heavy
`digital_rf` dep); archive or move with grape. `io/calibration_file.py:234`
imports `hf_timestd.version` — the one true circular dep; becomes an injected
version string.

Station-table DI seam: pure station data (coordinates/frequencies/power)
becomes `hamsci_dsp.stations` — a frozen StationCatalog with the built-in HF
time-station table; engines take `catalog` as a constructor argument
defaulting to the built-in. hf-timestd's `wwv_constants.py` re-exports for
back-compat and keeps timing schedules/thresholds locally.

Also: generic chronyc parsing lands beside `timing.py`. The local shims
(`hop_geometry`, `tec_geometry`, `snr`) die — direct hamsci-dsp imports.

Shim discipline: `hf_timestd.core` keeps thin per-symbol re-export shims until
each symbol's last consumer migrates (Phases 2–5); shims are deleted
per-symbol, tracked in the implementation plan.

### 5.3 → wwv-h-iono (Phase 3)

`physics_fusion_service`, `ionospheric_reanalysis`, `tid_detector`
(WWV-specific orchestration; the generic TDOA core may go to hamsci-dsp),
`propagation_stats`, `grape/` (+ grape-daily/upload-retry units + the
hs_uploader grape-psws pipeline block, watermark keys unchanged).

Port fixes bundled with the move (both are HDF5-purge residue):
- `_check_upstream_freshness` scans `*.h5` and is therefore always-stale →
  becomes a DB read.
- `_discover_channels` requires `phase2/<ch>/clock_offset` directories (a
  filesystem side-effect of l2-calibration) → becomes a DB read.

GRAPE's three hidden hooks:
1. `grape-daily.service` ExecStart is `-m hf_timestd.cli grape daily` in
   hf-timestd's venv — wwv-h-iono ships its own CLI entrypoint + unit in the
   same cutover.
2. The `[[hs_uploader.pipeline]]` block moves verbatim, never present in both
   deploy.tomls at once; source_id/transport byte-identical; wwv-h-iono's
   config carries an identical `[station]` block for
   `{station_id}`/`{instrument_id}` resolution.
3. Verify grape decimation's read path into `/var/lib/timestd/raw_buffer`
   during Phase-3 detailed design.

Also moving: the units absent from deploy.toml today (`timestd-iri-update.*`,
`timestd-iri-healthcheck.*`, `timestd-spaceweather-healthcheck.*`) — added to
wwv-h-iono's deploy.toml so they stop being contract orphans.

⚠ `timestd-physics.service` ExecStartPre currently mkdirs the CORE
`phase2/fusion` dir. That responsibility moves to timestd-fusion (or
timestd-tmpfiles.conf) in the same change, or fresh core installs break.

### 5.4 → gnss-vtec (Phase 4)

`scripts/live_vtec.py`, `core/gnss_tec.py`, `ubx_parser`, `cddis`. Writes
`L3_gnss_vtec` via the shared data layer.

### 5.5 → station-web (Phase 5)

`web-api/` wholesale, plus `timing_validation_service` and
`stability_analysis` (their only production consumers are the web-ui).
Depends on hamsci-dsp only. Living-docs endpoints become config-driven docs
roots (today `routers/docs.py` reads `<repo>/docs`); config no longer read
from `<repo>/config/timestd-config.toml`. Chrony parsing via hamsci-dsp or the
`DIAG_chrony_stats` product.

### 5.6 Removed from fusion (Phase 2)

The in-loop TECEstimator (`multi_broadcast_fusion.py:685`, `:3530-3620`) —
the standing METROLOGY_PHYSICS_SPLIT.md action item. The
`HFPropagationModel.predict()` call for the GNSS-VTEC D_clock correction
(`:3452`) stays: that is timing's legitimate use of the shared engine, via
hamsci-dsp post-Phase-1.

## 6. Unit naming

| Repo | Units |
|---|---|
| hf-timestd | keeps `timestd-*`: core-recorder, metrology@/.target, l2-calibration, fusion, radiod-monitor, chrony-monitor, clock-monitor, hpps-watchdog, pipeline-watchdog (physics/vtec stanzas stripped from `pipeline-watchdog.sh` and `check-freshness-alert.sh`), prune, alert@, chronyd-timestd-shm.conf, tmpfiles/affinity confs. clock-monitor + alert@ get added to deploy.toml (currently orphans). |
| wwv-h-iono | prefix `wwviono-`: physics, reanalysis, ionex-download, iri-update, iri-healthcheck, spaceweather-healthcheck, watchdog. **grape-daily keeps its name** (operator familiarity; only the hs-uploader source_id is pinned). |
| gnss-vtec | `gnss-vtec.service` (ex timestd-vtec) + gnss-vtec-watchdog.timer |
| station-web | `station-web.service` (ex timestd-web-api); no watchdog — systemd Restart= suffices |

Cutover rule: units are replaced wholesale in one smd transaction per phase
(old units keep running from the old checkout until the single switch); never
edit units in place; never Alias across repos.

## 7. Config split

The template has no [physics]/[iono]/[tec] sections — science config today is
[services] toggles + [gnss_vtec] + code defaults, so this is cheaper than it
looks.

- **hf-timestd keeps:** `[station]` (canonical), `[ka9q]`, `[recorder.*]`,
  `[wwvb]`, `[logging]`, `[monitoring]`, `[metrology]` (including the
  `physics_products` producer gate — it is the L1-feed switch for the science
  side and stays with the producer), `[timing.*]`, `[services]` (shrunk),
  `[storage]`. **Add explicit `[timing.authority_manager]`** — currently an
  undeclared namespace resolved entirely from code defaults.
- **wwv-h-iono:** `/etc/wwv-h-iono/config.toml` — `[station]` mirror,
  `[uploader.*]` (GRAPE/PSWS), reanalysis/ionex settings, toggles.
- **gnss-vtec:** `[station]` mirror + `[gnss_vtec]` contents.
- **station-web:** `[web_ui]` + docs-roots list + DB path.
- Wizard chain under sigmond's one-init-per-deploy.toml constraint: catalog
  `start_priority` forces install order (timing = 50, before clients);
  hf-timestd's `setup-station.sh` stays canonical for station identity;
  satellite inits default `[station]` from the CONTRACT §14 env bag, falling
  back to reading hf-timestd's config.

## 8. Cross-cutting rules

- **quota_manager stays the single disk-budget owner** of `/var/lib/timestd`.
  Retention becomes a data_product_registry (hamsci-dsp) contract that other
  repos' products register into. hf-timestd deleting other repos' aged
  products is documented as a contract, not a bug. GRAPE protected-dates
  logic preserved.
- **service_profile.py:** core drops the `full` profile and the
  physics/vtec/ionex/iono_reanalysis entries; `[gnss_vtec].enabled` gating
  moves to gnss-vtec; satellites get trivial enabled/disabled profiles.
- **`[metrology].physics_products = false`** is a supported degraded mode:
  wwv-h-iono services must idle cleanly against a station that produces no
  physics-gated L1/L2 products (no crash-loop). Acceptance-tested in Phase 3.
- **Tests split:** engine-level science tests → hamsci-dsp (Phase 1);
  service-level science tests → wwv-h-iono (Phase 3);
  `test_fusion_gnss_vtec_rtp_gate` and mixed metrology tests stay core;
  hamsci-dsp gains an import-lint test proving it never imports hf_timestd.
- **CLI:** `grape` and `raytrace` subcommands leave core `cli.py`; daemon /
  discover / create-channels / calibrate / shm-init / config / profile /
  service / data / inventory / version / quality / validate stay. Every new
  repo ships `inventory --json` (sigmond contract) + a client adapter.
- **pyproject:** fastapi/uvicorn/jinja2/aiofiles/python-multipart leave core
  with station-web; iri2020 (git pin), netCDF4/boto3, digital_rf, xarray leave
  with science; matplotlib/pandas audited at Phase 5 (likely leave core
  entirely once grape + stability depart). The `[tool.uv.sources]` editable
  sibling-path convention is reproduced in all four consumer repos.
- `ensure-pylap.sh` (PHaRLAP build) follows the raytrace consumer out of core.

## 9. Migration phases and acceptance

Phase ordering is strangler, library-first. Phase 2 before Phase 3 is
load-bearing (fusion must stop importing TECEstimator before its service
wrapper leaves). Phase 4 is independent of Phase 3; Phase 5 is
order-independent after Phase 1 and can be pulled forward.

**Phase 0 — gate + spec (this document).** Gate: T6 stage-1 acceptance
< 10.4 µs.

**Phase 1 — grow hamsci-dsp, repoint hf-timestd.** Move the engine cluster
(§5.2) into `hamsci_dsp.{propagation,ionosphere}`; add `hamsci_dsp.stations`;
move io/ + schemas + data_product_registry with the version-injection fix;
break up `core/__init__.py`'s ~25 eager imports into per-symbol shims; declare
`[timing.authority_manager]`; move engine tests; update pyprojects.
*Acceptance:* B3 — hf-timestd suite green with hamsci-dsp editable AND
hamsci-dsp suite green standalone (import-lint proves no hf_timestd import);
B4 — 24 h soak: authority.json fresh via the default path, chrony SHM within
pre-split baseline, product cadences unchanged, no watchdog restarts.

**Phase 2 — cut fusion's in-loop science.** Remove TECEstimator; audit the
fused-product schema for TECEstimator-sourced columns — null-fill or
minor-version, never silently change semantics; keep `_read_gnss_vtec` and
the propagation predict via hamsci-dsp; AuthorityRunner stays.
*Acceptance:* B3 fusion tests incl. `test_fusion_gnss_vtec_rtp_gate`; B4 48 h
soak — fused offset spread vs Phase-1 baseline, no TEC-correction regression,
chrony refclock gate never trips.

**Phase 3 — extract wwv-h-iono.** The only phase with mid-sequence outage
risk: unit cutover + hs-uploader block move + HDF5-residue fixes land in ONE
smd transaction.
*Acceptance:* B3 both repos green; B4 48 h — science product cadence
unchanged; grape-daily uploads with the watermark advancing WITHOUT
re-scanning old OBS* datasets (check the uploader cursor table);
`physics_products=false` degraded mode idles cleanly; hf-timestd service
uptime continuous through cutover; prune deletes aged science per contract.

**Phase 4 — extract gnss-vtec.**
*Acceptance:* `L3_gnss_vtec` cadence unchanged; fusion metrics still show the
D_clock correction applied; disable test — gnss-vtec stopped ⇒ fusion
degrades gracefully.

**Phase 5 — extract station-web.**
*Acceptance:* all routers 200 against a live station DB; living-docs render
from multi-repo docs roots; hf-timestd venv builds without web deps;
dashboards show live L1–L3 + iono + vtec end-to-end.

**Phase 6 — deployment/catalog/image.** Four new catalog.toml entries
(start_priority: timing = 50 before clients); topology additions; per-repo
deploy.toml + inventory adapters; profiles dasi2/base/client;
install/stop/uninstall script diets; wizard chain; sigmond-site-timing
refclock path verified (stays hf-timestd); DASI image rebuild — every change
lands in the image, nothing hand-applied.
*Acceptance:* clean-image install of all five components converges green from
scratch; `smd start/stop` per client toggles only its own units; the upgrade
path preserves configs (if_absent renders) and the grape watermark.

## 10. Known stale premises corrected during design

- `L2_timing_measurements` is NOT an orphan: `l2_calibration_service.py`
  produces it (writer created ~:222, written ~:528). The genuinely broken
  HDF5 residue is the two physics-side assumptions in §5.3. The 2026-08-07
  B4 finding predates/misread this; `timestd-physics` was disabled on that
  basis and its re-enable path is the Phase-3 port.
- The web-ui's real substrate dependency is `hf_timestd.io` + the DB, not the
  authority path — which is why the data layer moves to hamsci-dsp.

## 11. Open items for co-review (rob)

- Confirm the hamsci-dsp charter expansion (engines + data layer + stations
  catalog) and its release/versioning discipline — it becomes the fleet's
  load-bearing library.
- Confirm unit prefix `wwviono-` and repo name spelling `wwv-h-iono`.
- Confirm quota/retention contract (§8) — hf-timestd pruning other repos'
  products by registry declaration.
- HamSCI org repo creation + push permissions for the four new repos.
- **Magnetometer timing provenance gap** (Michael, 2026-08-10): mag-recorder
  stamps readings from the host clock (sysclock frame — the instrument is not
  radiod-sampled, so the RTP invariant legitimately does not apply), but the
  data carries a timestamp with NO provenance annotation. Proposed fix, cheap:
  stamp a `to_timing_authority`-style sidecar block (tier + σ derived from
  chrony state) on mag products so they meet the same annotated-timing bar as
  the RTP-frame clients. Belongs to mag-recorder, not this split — but the
  contract shape (provenance block) should come from `hamsci_dsp.timing` so
  it stays uniform fleet-wide.

## 12. Pre-gate evaluations (added in review, Michael 2026-08-10)

Two open questions about the post-split core. Both are config/offline
experiments on B4 — no timing-core code changes — runnable during the T6
wait; their outcomes amend this spec before Phase 1 begins.

### E1 — WWVB receiver utility

WWVB is pooled into fusion as one more D_clock source: the core-recorder
decode loop → `wwvb_fusion.py` L1 row (same `timing_error_ms` convention as
the HF workers) → combiner consumes it verbatim
(`multi_broadcast_fusion.py:2043`) beside WWV/WWVH/CHU/BPM, with a
groundwave-only delay model (`wwvb_propagation.py`). It is the one pool
member immune to the shared ionospheric-model error — a potentially valuable
independent cross-check — but its own docstring grades an uncalibrated WWVB
source MARGINAL (propagation-model residual + antenna-geometry offset must be
absorbed by GPS-learned calibration).

Measure on B4 over ≥3 days: decode success rate + L1 row cadence; fused
offset vs GPS truth (offset-judge benches) with WWVB in and out of the pool;
day/night split (night skywave contamination is the known LF failure mode).

**Decision:** contributor (keep + add calibration) · witness-only (keep at
zero weight) · retire (delete the ~530-line WWVB block from core_recorder_v2
plus the four `wwvb_*` modules — core diet in the split's spirit).

### E2 — FUSION quality without GNSS-VTEC

Post-split, a station without a raw-capable GNSS receiver is the TYPICAL
fleet configuration, not a corner: gnss-vtec is an optional client, and
Phase 2 removes the in-loop TECEstimator fallback (below-noise-floor by its
own docstring). Such stations get no fusion-level iono correction at all;
what remains is l2-calibration's climatological propagation subtraction
(IRI/IONEX). Question: does T3 hold its 0.5–2 ms (A1) budget without the
GNSS-VTEC Δiono correction?

Measure on B4 (has GNSS + GPS truth): (a) harvest the applied Δiono
distribution from fusion journals — read-only; (b) A/B the correction on/off
against the judge benches; (c) trial climatological IONEX TEC substituted
where GNSS-VTEC would go, as a zero-install-cost Phase-2 fallback candidate.
Baseline: B4 overnight T3 σ = 3.26 ms with all corrections active.

**Decision:** Phase-2 fallback = nothing (absence is within budget) ·
climatological IONEX substitute; plus a documented expected T3 σ for
GNSS-less DASI stations.

### Interim findings (2026-08-10, read-only recon on B4 via web-api :8000)

**E1 — WWVB is not merely marginal, it is unexercised.** B4 does not
configure WWVB at all: absent from the 17 configured broadcasts
(`/api/stations/broadcasts`), absent from the 24 h broadcasts dashboard, and
`stations_used` in live fusion is `[WWV, WWVH]` only. The consumer code is
idle. Additionally the `L3_fusion_timing` schema carries per-station stats
for WWV/WWVH/CHU/BPM but has **no WWVB columns** — a schema gap to close if
WWVB is kept. Consequence: E1 must first *enable* WWVB on a station (config +
core-recorder restart — deferred while the T6 hands-off measurement window is
live) before any utility measurement exists. The retire option currently has
zero production evidence against it.

**E2 — both iono corrections are already fusion-authority-only.** Code
confirms (`multi_broadcast_fusion.py:3490` and the TECEstimator block):
in RTP-authority mode, GNSS-VTEC Δiono is *never applied* to D_clock
(cross-check + confidence boost only), and the TECEstimator fallback carries
the same `if not self.is_rtp_authority` gate. Every GPSDO/A1 DASI install
runs RTP authority, so for the typical fleet station:
- Phase 2's TECEstimator removal changes nothing in the fused output;
- gnss-vtec's absence costs only the cross-check, not accuracy.
The genuine E2 question narrows to **fusion-authority (non-GPSDO) stations**,
which B4 cannot A/B in production without an authority-mode switch. Remaining
E2 work: (a) confirm B4's `[timing] authority = rtp` from config when shell
access permits (near-certain: template default + design docs); (b) decide
whether fusion-mode characterization needs a testbed or waits for a real
fusion-authority deployment; (c) the cross-check's diagnostic value is an
argument for keeping gnss-vtec install-optional but recommended.

Baseline recorded (21 h, 9026 fusion cycles via `/api/metrology/fusion/history`):
fused D_clock mean +0.05 ms, sd 0.60 ms; published σ mean 3.5 ms; grades
C:8181 / D:650 / B:195. Note σ sits above the METROLOGY T3 (A1) 0.5–2 ms
budget — worth its own look, independent of this split.
