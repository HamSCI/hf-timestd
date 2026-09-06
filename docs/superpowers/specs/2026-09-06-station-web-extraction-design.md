# station-web — Phase 5 of the hf-timestd split

**Status:** APPROVED DESIGN, implementation plan next
**Date:** 2026-09-06
**Authors:** Michael (mjh) with Claude
**Parent:** `docs/design/HF_TIMESTD_SPLIT_DESIGN.md` (2026-08-10), §5.5 and Phase 5 of
`docs/superpowers/plans/2026-08-10-hf-timestd-split.md`
**Depends on:** Phases 0–3 (landed: hamsci-dsp fattened, hamsci-physics extracted)
**Followed by:** Phase 4 (gnss-vtec), then Phase 6 (catalog, image)

## 1. Purpose and remit

hf-timestd still carries a web UI in its `web-api/` directory: about 11,000 lines of
FastAPI, 20 routers, 15 hand-written HTML pages, served by `timestd-web-api.service`
from the timing core's own checkout and venv. The 2026-08-10 split design named a
fifth repository, station-web, to take it. This document specifies that extraction.

Michael set the remit on 2026-09-06 in one sentence: **station-web provides data
reports and documentation, and nothing else.** Its sources are hamsci-physics,
hf-timestd (primarily metrology), and gnss-vtec where a station runs a
multi-channel GNSS receiver. It reads data products under `/var/lib/timestd`. It
never imports `hf_timestd`, never runs `journalctl`, `chronyc`, or a shell, never
reads recorder quota or upload queues, and never reaches the network. Sigmond
already owns that ground through `smd status` and the heartbeat.

That remit also serves the exposure review of 2026-09-05. A client that reads a
directory of products and nothing else can run on any host that holds a copy of
the products. That property makes the "publish outward" arrangement possible
later without rework here.

## 2. Decisions taken (mjh, 2026-09-06)

| # | Decision | Choice |
|---|---|---|
| 1 | Repository home | Stage under `mijahauan/station-web`; transfer to `HamSCI/` later (hamsci-physics precedent) |
| 2 | Catalog shape | Contract-conformant meta-client, contract 0.8, `kind = "client"` |
| 3 | Router scope | Keep the product-backed routers; cut logs, docs evidence, quota/queue health (§5) |
| 4 | Documentation roots | hf-timestd, hamsci-physics, hamsci-dsp, sigmond `docs/scientist`; config-driven, read-only |
| 5 | Network posture | Configurable bind, default `0.0.0.0:8000`; CORS closed to same origin; no shell, no journal |
| 6 | Runtime identity | Own venv, own config at `/etc/station-web/`, data root defaults to `/var/lib/timestd`, dedicated user with group read |
| 7 | Cutover | One smd transaction per station; soak B4 first, ND follows |
| 8 | gmag-webui | Stays a separate component; not absorbed in Phase 5 |
| 9 | Timing-validation page | Cut; backlog a rebuild from L2/L3 products |
| 10 | Chrony and propagation | Keep product-backed endpoints; rebuild chrony snapshot and comparison from `DIAG_chrony_stats`; cut the two live-model propagation endpoints |
| 11 | Space weather | Read hamsci-physics's cache file; no fetching, no poller thread |
| 12 | Front-end assets | Vendor Plotly; pin the markdown renderer |
| 13 | Homeless modules | `models/broadcast.py` moves into station-web; `core/stability_analysis.py` moves into hamsci-dsp |
| 14 | Tests | A FastAPI TestClient suite covers every kept route against a fixture database |

## 3. Architecture

```
                 /var/lib/timestd  (frozen data root, owned by hf-timestd)
                 ├── phase2/<channel>/…      L1, L2 products      ← hf-timestd writes
                 ├── phase2/fusion/…         L3 fusion, DIAG_chrony_stats ← hf-timestd writes
                 ├── phase2/science/…        L3 tec/dtec/tid, L3C ← hamsci-physics writes
                 ├── iono_cache/space_weather.json               ← hamsci-physics writes
                 ├── products/<ch>/…         GRAPE decimated + spectrograms ← hamsci-physics writes
                 └── (L3_gnss_vtec)                               ← gnss-vtec writes (Phase 4)
                            │  read only, through hamsci_dsp.io
                            ▼
                      station-web  (FastAPI, uvicorn, one process)
                      ├── routers/   thin HTTP over services/
                      ├── services/  product readers + derivations
                      ├── static/    15 pages, vendored Plotly, CSS, JS
                      ├── docs roots (four repos, read-only)
                      └── control socket  /run/station-web/control.sock  (§13 of the contract)
                            │
                            ▼
                      browser on the LAN, port 8000
```

Three rules govern the data flow:

1. **Products in, HTML and JSON out.** Every read goes through
   `hamsci_dsp.io.make_data_product_reader` or a plain read of a file another
   client wrote under the data root. No subprocess. No socket to another daemon
   except station-web's own control socket.
2. **Dependencies: hamsci-dsp and hamsci-physics as libraries.** hamsci-dsp for the
   readers, the registry, the station catalog, geometry, and (after this phase) the
   Allan-deviation module. hamsci-physics for `solar_zenith`, which seven call sites
   use. Nothing from `hf_timestd`.
3. **No writes under the data root.** station-web owns no directory there. Its only
   writable path is its systemd `RuntimeDirectory` for the control socket.

## 4. Repository layout

```
station-web/
├── pyproject.toml            console script: station-web = station_web.cli:main
├── deploy.toml               contract §5; link steps only
├── README.md
├── config/
│   └── station-web.toml.template
├── scripts/
│   ├── setup-station.sh      [contract.config].init  (idempotent, env-bag defaults)
│   └── config-review.sh      [contract.config].edit
├── systemd/
│   ├── station-web@.service  templated primary (contract §4)
│   └── station-web.service   single-instance alias → station-web@default
├── src/station_web/
│   ├── __init__.py
│   ├── version.py            GIT_INFO (hamsci-physics pattern)
│   ├── cli.py                version | inventory | validate | serve
│   ├── contract.py           build_inventory / build_validate / collect_issues
│   ├── config.py             /etc/station-web/station-web.toml + STATION_WEB_CONFIG
│   ├── control_socket.py     §13 unix socket: /healthz /readyz /status /metrics
│   ├── app.py                FastAPI factory (lifespan, sd_notify, watchdog)
│   ├── broadcast.py          ex hf_timestd/models/broadcast.py
│   ├── routers/              kept routers (§5)
│   ├── services/             kept services (§5)
│   └── static/               pages, css, js, vendor/plotly-2.27.0.min.js, vendor/marked.min.js (pinned release, hash in VENDOR.md)
├── docs/
│   └── (station-web's own docs)
└── tests/
    ├── test_client_contract.py
    ├── test_deploy_contract.py
    ├── conftest.py           fixture data root with one row of every read product
    └── test_routes.py        TestClient: every kept route → 200 against the fixture
```

## 5. What moves, what changes, what dies

### 5.1 Routers

| Router | Today | Disposition |
|---|---|---|
| `metrology` | `L3_fusion_timing` via FusionService | **Move** as is |
| `stability` | `L3_fusion_timing` + `core.stability_analysis` | **Move**; import ADEV from `hamsci_dsp` after §13 lands |
| `phase` | `L2_tick_phase`; raw read-only sqlite for the channel list | **Move**; channel list through the reader or the registry, not raw sqlite |
| `physics` | `L3_physics` | **Move** as is |
| `tec` | `L3_dtec`, `L3_tec` | **Move**; `hf_timestd.io` → `hamsci_dsp.io` |
| `tid` | `L3_tid` + `data_product_registry` | **Move**; registry from `hamsci_dsp` |
| `ionogram` | `L1_all_arrivals`; globs `phase2/<ch>/all_arrivals/*.h5` for channels | **Move**; channel discovery from the registry or directory listing stays acceptable (read-only) |
| `dashboard` | `L2_timing_measurements`, `L2_tick_timing`, `L1_broadcast_measurements`, `models.broadcast` | **Move**; broadcast registry from `station_web.broadcast` |
| `station`, `stations` | config + FusionService + `models.broadcast` | **Move**; same |
| `grape` | globs `products/<ch>/decimated`, `spectrograms/*.png`, `*_meta.json`, `upload/` | **Move**, minus the `upload/` status reads (management) |
| `correlations` | PropagationService + space weather + scipy | **Move**; space weather from the cache file |
| `space_weather` | live NOAA/GFZ fetch, poller thread at import, cache writes | **Rewrite** as a reader of `iono_cache/space_weather.json` written by hamsci-physics; no thread, no network, no writes |
| `propagation` | PropagationService (products) + two endpoints instantiating `HFPropagationModel(enable_realtime=True)` and `IonoDataService` | **Move the product endpoints; cut the two live-model endpoints** |
| `chrony` | `/history` from `DIAG_chrony_stats`; `/snapshot` and `/comparison` via `chronyc` | **Move `/history`; rebuild `/snapshot` and `/comparison`** as latest-row-per-source queries over `DIAG_chrony_stats` (fusion writes it once a minute) |
| `health` | HealthService: `pgrep`, `ps`, `df`, `uptime`; `quota_manager`; `upload/*.json` | **Replace** with product freshness: for each product station-web reads, the age of its newest row and a verdict against an expected cadence. No process or disk probes |
| `docs` | list / get / section over `<repo>/docs`; two evidence endpoints piping `journalctl` through a shell | **Move list / get / section over the four configured roots; cut the evidence endpoints** |
| `timing_validation` | in-process singleton of the running pipeline | **Cut** (decision 9); rebuild from products goes to the backlog |
| `logs` | `journalctl -u <unit>` | **Cut** |

### 5.2 Modules the routers import from hf-timestd

| Module | Consumers in core | Disposition |
|---|---|---|
| `hf_timestd.io` | many | Already a shim; import `hamsci_dsp.io` directly (19 sites) |
| `hf_timestd.data_product_registry` | 2 | Already a shim; import `hamsci_dsp.data_product_registry` |
| `hf_timestd.io.sqlite_writer.DEFAULT_DB_PATH` | — | `hamsci_dsp.io.sqlite_writer` |
| `core.wwv_constants` (4 lat/lon) | many | `hamsci_dsp.stations.BUILTIN_CATALOG` carries the same coordinates |
| `models.broadcast` (515 lines) | none | **Moves into station-web** as `station_web.broadcast` |
| `core.stability_analysis` (310 lines, numpy only) | none | **Moves into hamsci-dsp** (see §13); station-web imports it from there |
| `core.timing_validation_service` | none | Dies with the cut router; stays in hf-timestd until someone deletes it there |
| `core.chrony_stats` | fusion, offset judge | Not imported; the product replaces it |
| `core.propagation_model`, `core.iono_data_service` | metrology, L2 calibration | Not imported; the two live endpoints die |
| `quota_manager` | recorder, T6 capture | Not imported; freshness health replaces it |

### 5.3 Pages

All 15 HTML pages move. `timing-validation.html` and `logs.html` are removed with
their routers, and `index.html`'s navigation loses those two links. The remaining
pages change only in their `<script src>` lines (§10).

## 6. Data access

**Reader.** `hamsci_dsp.io.make_data_product_reader(...)` with the `storage`
table from station-web's own config, which mirrors hf-timestd's `[storage]`
defaults for the frozen data root. A station with a non-default `sqlite_path`
sets it once in `/etc/station-web/station-web.toml`; `setup-station.sh` seeds it
from hf-timestd's config when that file exists.

**SQLite WAL.** hf-timestd writes in WAL mode. A reader opened `mode=ro` cannot
create the `-wal` and `-shm` sidecars, so a second uid needs write permission on
the containing directory and on those two files. Michael verified this
arrangement on 2026-09-06. The install therefore puts the `stationweb` user in
the `timestd` group and `validate` asserts that `phase2/` and the SQLite file's
directory are group-writable, reporting `fail` otherwise so a wrong permission
surfaces as a contract fault rather than an empty page.

**Chrony.** `DIAG_chrony_stats` (schema `diag_chrony_stats_v1`, `channel='fusion'`,
written by `ChronyStatsCollector` from the fusion loop) supplies history and, by a
latest-row-per-source query, the snapshot and the source comparison. When the
newest row ages past two minutes the endpoints say so in the payload instead of
presenting stale numbers as current.

**Space weather.** `iono_cache/space_weather.json`, written by hamsci-physics's
space-weather health check. station-web reads it, reports its age, and never
fetches.

**Documentation roots.** A list in config:

```toml
[docs]
roots = [
  { name = "hf-timestd",     path = "/opt/git/sigmond/hf-timestd/docs" },
  { name = "hamsci-physics", path = "/opt/git/sigmond/hamsci-physics/docs" },
  { name = "hamsci-dsp",     path = "/opt/git/sigmond/hamsci-dsp/docs" },
  { name = "sigmond",        path = "/opt/git/sigmond/sigmond/docs/scientist" },
]
```

The docs router resolves `<root>/<doc>` with the requested name confined to the
root (no `..`, no absolute paths, `.md` only), lists what exists, and renders
sections. A root that does not exist on a host drops out of the list with a
`warn` in `validate`.

## 7. Contract conformance (CLIENT-CONTRACT v0.8)

**CLI.** `station-web version|inventory|validate --json`, plus `station-web serve`
as the unit's ExecStart. The first act of `main()` clears root log handlers and
routes logging to stderr so stdout carries only JSON (§3). `inventory` exits 0
even with no config and reports gaps in `issues`.

**Inventory shape.** One instance, `default`, as a §16.3.1 meta-client:

```json
"data_path": {
  "kind": "file",
  "path": "/var/lib/timestd",
  "details": {
    "upstream_client": "hf-timestd",
    "upstream_unit": "timestd-fusion.service",
    "also_reads": ["hamsci-physics", "gnss-vtec"]
  }
},
"serves": { "http": { "bind": "0.0.0.0", "port": 8000 } }
```

`upstream_client` stays singular because the contract makes it so; hf-timestd
owns the data root, so it takes the slot. `also_reads` and `serves` are extension
keys, legal under §16.3 (unknown keys pass through). The contract has no field
for a TCP listener because station-web will be the first conformant client that
binds one. This spec proposes a `serves` block for the next contract bump rather
than a private convention, and the plan files that as a sigmond issue.
`radiod_id`, `data_destination`, and `chain_delay_ns_applied` are omitted (§16.5).
`data_sinks` is empty: station-web produces nothing.

**Validate.** Shared `collect_issues()` feeds both verbs. Checks: config present;
data root exists (`fail`); SQLite file readable and its directory group-writable
(`fail`); each configured docs root exists (`warn`); `hamsci_dsp` and
`hamsci_physics` import (`fail`); vendored Plotly present (`fail`); the unit's
ExecStart reaches `main()` (§12.1); `[package].contract_version` in deploy.toml
matches `CONTRACT_VERSION` (§12.6); `config_path` absolute on both verbs (§12.3).

**Unit.** `station-web@.service`, `Type=notify`, `WatchdogSec=60`, with the two
EnvironmentFile lines the contract requires:

```ini
EnvironmentFile=-/etc/sigmond/coordination.env
EnvironmentFile=-/etc/station-web/env/%i.env
ExecStart=/opt/git/sigmond/station-web/venv/bin/station-web serve --instance %i
ExecReload=/bin/kill -HUP $MAINPID
User=stationweb
Group=timestd
RuntimeDirectory=station-web
ProtectSystem=strict
ReadOnlyPaths=/var/lib/timestd /opt/git/sigmond
ReadWritePaths=/var/lib/timestd/phase2      # holds timestd.db and its -wal/-shm sidecars; see §6
PrivateTmp=yes
NoNewPrivileges=yes
MemoryMax=600M
Nice=5
Restart=always
```

`station-web.service` exists for operators' fingers and delegates to
`station-web@default.service`. hf-timestd's `timestd-alert@` OnFailure hook does
not carry over; systemd `Restart=` suffices, as the plan said.

**Control socket (§13).** `/run/station-web/control.sock`, mode 0660, group
`sigmond`, stdlib `UnixStreamServer` in a thread, modelled on
wspr-recorder's `ipc_server.py` (hamsci-physics never implemented §13).
Endpoints `/healthz`, `/readyz`, `/status`, `/metrics` answer from cached state in
under 100 ms. `/status` carries the freshness table from §5.1.

**Logging (§10, §11).** stderr only, no file logs, so no `log_paths` key.
Level from `--log-level`, then `STATION_WEB_LOG_LEVEL`, then `CLIENT_LOG_LEVEL`,
then `[logging] level`, then `INFO`. SIGHUP re-reads the level and the docs roots.
`inventory` reports `log_level`.

## 8. Configuration

`/etc/station-web/station-web.toml`, override with `STATION_WEB_CONFIG`:

```toml
[station]        # seeded from the §14 env bag: STATION_CALL (fallback STATION_CALLSIGN), STATION_GRID, STATION_LAT, STATION_LON
callsign = ""
grid_square = ""
latitude = 0.0
longitude = 0.0

[data]
root = "/var/lib/timestd"

[storage]        # mirrors hf-timestd [storage]; seeded from its config when present
sqlite_path = ""

[web]
bind = "0.0.0.0"
port = 8000

[docs]
roots = [ … ]    # §6

[logging]
level = "INFO"
```

`setup-station.sh` follows hamsci-physics's 43-line pattern: idempotent, copies
the template, fills empty slots from the env bag, and additionally copies
`[storage] sqlite_path` from `/etc/hf-timestd/timestd-config.toml` when that file
exists. `config-review.sh` prints the config and runs `validate`.

The old `config.py` read `[web_ui]` and `[gnss_vtec]` and computed seven derived
directories that nothing used. None of that carries over.

## 9. Network posture

- Bind and port from config; default unchanged so LAN viewing keeps working
  through the cutover.
- CORS: no wildcard. Same-origin only, which for a page and its API on one port
  means the middleware can go entirely. The current `allow_origins=["*"]` with
  `allow_credentials=True` is a combination browsers reject anyway.
- No `subprocess` import anywhere in `station_web`. A test greps for it.
- No outbound network call anywhere in `station_web`. A test asserts that
  `requests`, `urllib.request`, `httpx`, and `aiohttp` never import.
- Path parameters that name files (docs, channels, dates) are validated against
  an allow-list pattern and confined to their root.
- Authentication and any public exposure wait for the portal decision from the
  2026-09-05 exposure review. This spec removes the surfaces that made exposure
  dangerous; it does not add a gate.

## 10. Front-end assets

Vendor `plotly-2.27.0.min.js` (the version the pages already name, about 3.5 MB)
and a pinned `marked` release under `static/vendor/`, with a `VENDOR.md`
recording version, source URL, and sha256. Every `<script src="https://cdn…">`
becomes `/static/vendor/…`. A test asserts no `http://` or `https://` appears in
any `<script src>` or `<link href>` in `static/`. Offline stations then render
charts, and a content-security policy becomes possible later.

## 11. sigmond integration

Catalog entry in `sigmond/etc/catalog.toml`:

```toml
[client.station-web]
kind            = "client"
description     = "Station data reports and documentation (metrology, ionospheric science, GNSS vTEC); reads products under /var/lib/timestd"
repo            = "https://github.com/mijahauan/station-web"   # staging: transfers to HamSCI/
uses            = []
requires        = ["hf-timestd", "hamsci-physics", "hamsci-dsp"]
contract        = "0.8"
install_script  = ""
start_priority  = 220
```

gnss-vtec is not in `requires`: the vTEC pages degrade to "no product" when it
does not run. `kind = "client"` because in this catalog `server` has meant
"not contract-conformant" and `test_catalog.py` enforces that coupling.

Profile: append `station-web` to `[profile.dasi2].clients` after
`hamsci-physics`. The comment there records what happened when hamsci-physics was
left off that list: units enabled against a venv bringup never built, 203/EXEC on
every boot of every imaged station. station-web must not repeat it.

No `etc/clients/station-web.deploy.toml` shim: conformant clients are discovered
through their own manifest.

`deploy.toml` uses only `kind = "link"` install steps (the CLI onto PATH, the two
units into `/etc/systemd/system`), because sigmond executes nothing else today.
Directory and config creation belong to `setup-station.sh`. `produces` names
`venv/bin/station-web`.

## 12. hf-timestd side

In one commit, after station-web installs cleanly on B4:

- Delete `web-api/` and `systemd/timestd-web-api.service`.
- `deploy.toml`: remove the unit's link step and its entry in `[systemd] units`.
- `pyproject.toml`: drop `fastapi`, `uvicorn[standard]`, `jinja2`,
  `python-multipart`, `aiofiles`. The last three are dead in both trees today.
- Delete `models/broadcast.py` and `core/stability_analysis.py` once their new
  homes exist and hf-timestd's own imports (none for either) are confirmed absent.
- Move `tests/unit/test_web_api_config_resolution.py` and the web-api half of
  `tests/unit/test_tid_l3_writer.py` to station-web's suite, rewritten without the
  sys.path insert.
- `core/timing_validation_service.py` stays; it has no consumer after the cut and
  becomes a candidate for a later residue pass, not this one.
- `service-profile`'s `web_api` toggle: remove, since the core no longer owns a
  web service.

Acceptance from the plan stands: the core venv builds without web dependencies.

## 13. hamsci-dsp side

`core/stability_analysis.py` (pure numpy: `compute_phase_adev`,
`compute_frequency_adev`, `identify_noise_type`, `compute_stability_at_tau`,
`compute_stability_metrics`) moves to `hamsci_dsp/stability.py` with its tests.
hamsci-dsp's charter covers signal-agnostic analysis and no dsp module covers
Allan deviation today. station-web and, if it ever needs it, hf-timestd import
from there. This lands first, as a hamsci-dsp minor release, so station-web can
depend on it from its first commit.

## 14. Cutover and soak

Per station, one smd transaction. Sequence on B4:

1. `git ff` hamsci-dsp (stability module) and reinstall its editable consumers as
   usual.
2. `smd install station-web`: clone, build venv, link CLI and units, enable
   `station-web@default`. It fails to bind port 8000 while the old unit runs;
   that is expected and harmless (`Restart=always`).
3. `systemctl disable --now timestd-web-api.service`.
4. `git ff` hf-timestd to the commit of §12; remove the dangling unit link;
   `daemon-reload`.
5. `systemctl restart station-web@default`; confirm `/health` and every page.
6. `smd config init station-web` if step 2 did not seed a config; `smd admin diag
   drop-in station-web` green.

Downtime of the monitoring page: seconds. No radiod restart, no recorder restart,
no timing service touched. Announce on the claude-bus before step 2 all the same.

Soak: 24 hours on B4 with the freshness table watched, then ND. The v3.38 image
line picks station-web up in Phase 6.

## 15. Testing and acceptance

- `tests/test_client_contract.py`: stdout-only JSON on every verb; inventory
  exits 0 with no config; contract version matches the manifest; meta-client
  `data_path`; radiod fields absent; `serves` present.
- `tests/test_deploy_contract.py`: manifest invariants, link-only steps,
  `produces` names the console script.
- `tests/test_routes.py`: a fixture data root holding one row of every product
  station-web reads (L1 all-arrivals, L1 broadcast, L2 timing, L2 tick phase, L2
  tick timing, L2 test signal, L3 fusion, L3 physics, L3 tec, L3 dtec, L3 tid, L3C
  propagation stats, DIAG chrony stats, space_weather.json, a GRAPE spectrogram and
  meta) and every kept route returning 200 with a well-formed body. Every cut route
  returns 404.
- Hygiene tests: no `subprocess`, no outbound HTTP client import, no CDN URL in
  `static/`, no `hf_timestd` import.
- Acceptance on B4 (from the plan, amended): all kept routers 200 against the live
  database; docs render from the four roots; the core venv builds without web
  dependencies; dashboards show live L1–L3 plus ionospheric products end to end;
  vTEC pages report "no product" honestly on a station without gnss-vtec.

## 16. Out of scope, and what follows

- **Phase 4, gnss-vtec:** next. Scope preview: `core/gnss_tec.py` (539 lines),
  `scripts/live_vtec.py`, `timestd-vtec.service`, the `[gnss_vtec]` config section,
  the Earthdata credential file. station-web's vTEC pages need nothing from it
  beyond the `L3_gnss_vtec` product.
- **Timing-validation rebuild from products:** backlog item in hf-timestd.
- **gmag-webui absorption:** deferred (decision 8).
- **Authentication and public exposure:** the portal decision from the exposure
  review, not this phase.
- **Contract amendment:** a `serves` block for HTTP-binding clients, filed against
  sigmond.
- **hamsci-physics `run` install steps that never executed:** noted, not touched.

## 17. Risks

| Risk | Mitigation |
|---|---|
| WAL sidecar permissions differ on ND or on an imaged station | `validate` fails loudly on the directory mode; the plan checks ND before its cutover |
| A kept route quietly depends on host state the audit missed | The hygiene tests (no subprocess, no HTTP client, no `hf_timestd`) catch the import; the route test catches the behaviour |
| Port 8000 already bound by another service on some host | `[web] port` in config; `validate` warns when the port is in use by a unit other than station-web |
| Vendored Plotly drifts from the pages' expectations | Same 2.27.0 the pages name today; `VENDOR.md` pins the hash |
| The dasi2 profile omission recurs | The catalog test that checks profile members exist gains a check that every `client`-kind entry with `start_priority >= 200` appears in some profile or is `hardware_gated` |
