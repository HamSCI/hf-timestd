# hf-timestd — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** hf-timestd `7.0.0` / deploy `7.1.0` (2026-06-25).
**Prefix:** `HFT`.

> Pilot application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md).
> The sigmond↔component **interface** requirements are specified once in the
> [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> (v0.8) and referenced — not restated — here (§8.3). Provenance tags:
> `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this review.
> Status: ✅ implemented · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

A DASI2 station needs to know *what time it is* to a known, graded accuracy —
and to keep knowing it when GPS or the network is unavailable — because every
other RF client labels its data against a UTC↔RTP mapping. hf-timestd is the
station's **time-standard analyzer and timing authority**. It receives the HF
time broadcasts (WWV, WWVH, CHU, BPM) through `radiod`, measures tick arrival
against the GPSDO-disciplined RTP sample counter, and produces (a) a graded
UTC offset for the host and the suite, and (b) ionospheric science products —
chiefly carrier-phase differential TEC — as a by-product of the same
measurements.

Its defining design principle: the **RTP sample counter is the substrate**
("steel ruler") and UTC is an *annotation* on it, graded by a tier hierarchy
(T6 hardware-PPS … T3 HF-fusion … T0 host clock). This lets the station keep a
usable, honestly-graded clock across the full range from "GPS locked" to
"GPS-denied, HF-only." It is also the suite's contract §18 timing-authority
**producer**: it publishes the offset+tier other clients subscribe to.

## 2. Goals & objectives

- Recover UTC to **~50 µs** with GPS/PPS (RTP mode) and **sub-millisecond**
  from HF broadcasts alone (Fusion / GPS-denied mode), each with an honest
  uncertainty.
- Produce **carrier-phase dTEC at ~6 mTECU/min**, GNSS-VTEC-anchored when a
  local receiver is present.
- Discipline the host clock (chrony SHM) and publish a graded timing authority
  the rest of the suite can consume.
- Degrade gracefully and *legibly* (every output carries a tier + uncertainty;
  never assert better precision than measured).
- Run usefully **standalone** (radiod + this component) and as an integrated
  suite client.

## 3. Non-goals / out of scope

- **Being a receiver.** It consumes pre-tuned RTP from `radiod`; it does not
  tune hardware. (Owner: ka9q-radio.)
- **Disciplining other hosts' clocks** or cross-station fusion — it publishes
  locally; network-level fusion is PSWS/analysis scope.
- **Replacing GPS** — Fusion mode is a graceful-degradation tier, not a claim
  of GPS-equivalent accuracy.
- **Timing decisions inside subscribers** — it *publishes*; each subscriber
  decides how to apply the authority (contract §18 consumer obligations).

## 4. Stakeholders & actors

Station operator · `radiod` (RTP IQ source, required) · the GPSDO/PPS chain ·
optional TS-1 BPSK-PPS injector (T6) and LBE-1421 GPSDO (T5) · optional local
GNSS (ZED-F9P, VTEC anchor) · `chrony` (SHM consumer) · suite clients that
subscribe to the §18 authority (psk/wspr/hfdl/mag) · the PSWS GRAPE network
(Digital RF uploads) · sigmond (lifecycle, identity, status) · external data
sources (IONEX/Earthdata, IRI-2020, space-weather).

## 5. Assumptions & constraints

- `HFT-C-001` `[DOC]` ✅ `radiod` (ka9q-radio) SHALL be present and multicasting;
  there is no unicast fallback.
- `HFT-C-002` `[CODE]` ✅ The RTP sample counter SHALL be treated as the
  authoritative timeline; UTC is a graded annotation, never the substrate.
- `HFT-C-003` `[DOC]` ✅ The component SHALL be a **singleton per host** (one
  `station_id` per PSWS station); no per-instance config variant.
- `HFT-C-004` `[CODE]` ✅ Python ≥3.10, SQLite ≥3.37; `iri2020` and PHaRLAP
  require `gfortran`/`build-essential` at build time.
- `HFT-C-005` `[DOC]` ✅ Sibling libraries SHALL be editable installs
  (`ka9q-python`, `hamsci-dsp`) so a `git pull` propagates without reinstall.

## 6. Functional requirements

### 6.1 Acquisition (Phase 1 / core-recorder)
- `HFT-F-001` `[DOC]` ✅ SHALL ingest radiod RTP IQ for the configured channels
  and buffer it as zstd-compressed binary chunks (default 600 s) with JSON
  sidecars carrying sample-rate and RTP-timestamp metadata.
- `HFT-F-002` `[DOC]` ✅ SHALL maintain a real-time ring buffer feeding the
  metrology/fusion path in addition to the on-disk archive.
- `HFT-F-003` `[DOC]` 🟡 When the TS-1 BPSK-PPS (T6) path is enabled, SHALL
  extract the hardware PPS and expose it as a chrony SHM refclock.

### 6.2 Metrology (L1 → L2, per channel)
- `HFT-F-010` `[DOC]` ✅ SHALL detect time-standard tones/ticks per channel
  (templated `metrology@<broadcast>`), emitting L1 tick arrival times at µs
  precision with SNR, Doppler, and multipath awareness.
- `HFT-F-011` `[DOC]` ✅ SHALL apply per-path propagation correction (geometry +
  ionosphere; GNSS VTEC when available) to produce L2 D_clock per
  station-frequency with a full uncertainty budget.
- `HFT-F-012` `[CODE]` ✅ SHALL detect cross-channel SSRC aliasing (the
  l2-calibration single-sink) and refuse to fuse colliding streams.

### 6.3 Fusion & timing authority (L3)
- `HFT-F-020` `[DOC]` ✅ SHALL fuse multiple broadcasts (Kalman + inverse-variance
  WLS) into a converged D_clock and feed it to chrony SHM (`FUSE`).
- `HFT-F-021` `[DOC]` ✅ SHALL select an active tier (T6>T5>T4>T3>T2>T1>T0) by
  hysteresis with cross-check witnesses, and SHALL publish the result as the
  contract §18 authority (see §8.3, `HFT-I-002`).
- `HFT-F-022` `[DOC]` ✅ SHALL operate in **GPS-denied** mode (T3 HF-fusion
  consensus) as a production tier, not merely a diagnostic.
- `HFT-F-023` `[DOC]` ✅ SHALL expose a `calibrate` output (`offset_ms`,
  `uncertainty_ms`, `quality_grade` A–D, `convergence_state`, `usable`) for
  external consumers (e.g. wsprdaemon).

### 6.4 Physics / science (L3, optional)
- `HFT-F-030` `[DOC]` ✅ SHALL compute carrier-phase dTEC, GNSS-VTEC-anchored
  when a receiver is present, else flagged NO_ANCHOR / group-delay fallback.
- `HFT-F-031` `[DOC]` ✅ SHALL infer propagation mode (1F/2F/3F) + MUF per path,
  detect TIDs by cross-path correlation, and compute S4 / σφ scintillation.
- `HFT-F-032` `[DOC]` ✅ The physics pipeline SHALL be independently
  disable-able and its failure SHALL NOT corrupt the timing pipeline (§7
  `HFT-Q-002`).
- `HFT-F-033` `[CODE]` ✅ The `raytrace` (PHaRLAP) output SHALL be advisory
  overlay only and SHALL NEVER feed the timing path.

### 6.5 Service profiles & control
- `HFT-F-040` `[DOC]` ✅ SHALL expose four cumulative service profiles —
  `archive` ⊂ `rtp` ⊂ `fusion` ⊂ `full` — selectable via `[services].profile`,
  with per-service overrides.
- `HFT-F-041` `[DOC]` ✅ `core-recorder` SHALL be force-on in every profile.

### 6.6 Web API & observability
- `HFT-F-050` `[DOC]` ✅ SHALL serve a FastAPI dashboard (port 8000) exposing
  station/health/metrology/stability/propagation/tec/tid/grape/chrony routes.
- `HFT-F-051` `[CODE]` ✅ SHALL publish volatile runtime state to `/run/hf-timestd/`
  (`quality.json`, `authority.json`, fusion status) for fast local inspection.

### 6.7 GRAPE / PSWS upload
- `HFT-F-060` `[DOC]` ✅ SHALL run a daily pipeline (decimate 10 Hz → Digital RF +
  spectrogram → package → SFTP to `pswsnetwork.eng.ua.edu`) with retry on
  failure.

### 6.8 Self-description (contract surface)
- `HFT-F-070` `[DOC]` ✅ SHALL implement `inventory --json` / `validate --json`
  per contract v0.8 (see §8.3) with **pure-JSON stdout**.
- `HFT-F-071` `[CODE]` ✅ `validate` SHALL fail on SSRC collision
  `(freq,preset,sample_rate,encoding)` across channel groups and warn on missing
  callsign / unresolvable `ka9q.status` / zero channel groups.

## 7. Quality / non-functional requirements

- `HFT-Q-001` `[DOC]` ✅ Every timing output SHALL carry an explicit tier and a
  1σ uncertainty; the system SHALL NOT assert precision better than measured.
- `HFT-Q-002` `[DOC]` ✅ **Metrology/physics split:** timing SHALL always run;
  physics is optional and isolated so its crash cannot corrupt timing.
- `HFT-Q-003` `[DOC]` ✅ Long-running services SHALL have crash-loop protection +
  staleness watchdogs (`_timed_write`, pipeline/HFPS/HPPS watchdogs) that
  restart a unit not updating its store.
- `HFT-Q-004` `[CODE]` ✅ `authority.json` SHALL be written **atomically** and
  overwritten each cycle; consumers read it whole or not at all.
- `HFT-Q-005` `[CODE]` ✅ The SQLite store SHALL use WAL with a single writer per
  product for concurrent reads at ms commit cadence.
- `HFT-Q-006` `[DOC]` ✅ SHALL enforce a disk quota (default 80%) with
  day-granularity eviction (`prune` timer).
- `HFT-Q-007` `[CODE]` ✅ Optional dependencies (PHaRLAP, GNSS, Earthdata, PSWS
  creds) SHALL degrade gracefully to documented fallbacks, never hard-fail the
  core.
- `HFT-Q-008` `[DOC]` ✅ SHALL bootstrap to LOCKED within ~2 min (RTP) / 2–3 min
  (multi-station fusion) via NTP-seeded acquisition.

## 8. External interfaces

### 8.1 Inputs
- radiod RTP IQ via `ka9q-python` (9 channels: 2.5/5/10/15/20/25 MHz; 17
  broadcasts WWV/WWVH/CHU/BPM).
- `/etc/hf-timestd/timestd-config.toml` — operator MUST set: `[station]`
  callsign/id/instrument_id/grid (or lat+lon); `[ka9q].status`;
  `[recorder.channel_group.*].channels`; `[uploader.sftp]` host/ssh_key if
  uploading; `[services].profile`. Optional: `[timing.l6_pps]`, `[gnss_vtec]`,
  `[metrology]` physics/realtime toggles.
- External data: IONEX (Earthdata), IRI-2020, space-weather indices.
- Coordination/identity from `/etc/sigmond/coordination.env`.

### 8.2 Outputs
- **Authority:** `/run/hf-timestd/authority.json` (§18 producer; ~30 s cycle) +
  optional `authority_history.db`.
- **Science/measurement store:** SQLite at `/var/lib/timestd/phase2/timestd.db`
  (WAL) — products: L1 metrology, L2 timing, L3 fusion/dtec/tid/propagation/vtec.
  *(HDF5 retired at v7.0 — see `HFT-F-090`.)*
- **Clock discipline:** chrony SHM `FUSE` (L3 fusion) and `HPPS` (T6 PPS).
- **Web API:** `http://localhost:8000/api/*`.
- **PSWS:** Digital RF + spectrogram via SFTP to `pswsnetwork.eng.ua.edu`.
- **Raw archive:** zstd IQ chunks under `/var/lib/timestd/raw_buffer/`.

### 8.3 Contracts / APIs (reference, not restated)
- `HFT-I-001` `[DOC]` ✅ Conforms to **client contract v0.8**; `inventory --json`
  declares `provides_timing_calibration=true`, `data_path.kind=radiod-ka9q-python`,
  `data_sinks=[file]`, `control_socket=/run/hf-timestd/control.sock`, singleton
  `instance=default`. Full field semantics: contract §3/§16/§17.
- `HFT-I-002` `[DOC]` ✅ Is the contract **§18 timing-authority producer**;
  `authority.json` publishes `t_level_active`, `t_level_available`,
  `rtp_to_utc_offset_ns`, `sigma_ns`, `stations_contributing`,
  `disagreement_flags`, `governor_radiod`, `a_level`. Subscriber obligations are
  defined by the contract, not here.
- `HFT-I-003` `[DOC]` ✅ The sigmond↔PSWS upload seam (GRAPE SFTP, station_id +
  portal key) is governed by [PSWS-INTERFACE-BOUNDARY.md](https://github.com/HamSCI/sigmond/blob/main/docs/PSWS-INTERFACE-BOUNDARY.md).

## 9. Data requirements

SQLite product tables (auto-created per writer, channel-keyed): L1
`metrology_measurements`; L2 `timing_measurements`, `chu_fsk_decodes`; L3
`fusion_timing`, `tec`, `tid`, `propagation`, `gnss_vtec`. `authority_snapshot`
append-only history with per-tier (T3–T6) diagnostic columns. Retention:
operator-managed, default ~30 days; raw IQ under disk-quota eviction. Calibration
JSON schema `calibration_v1` (`offset_ms`, `uncertainty_ms`, `convergence_state`,
`quality_grade`, `usable`, uncertainty budget, ADEV).

## 10. Dependencies & development sequence

**Runtime deps:** `radiod` (required), `ka9q-python`, `hamsci-dsp` (editable
siblings), `iri2020`(+gfortran), `digital_rf`, `fastapi`/`uvicorn`,
`zstandard`, `numpy`/`scipy`/`pandas`, `paramiko`, `pydantic`. Optional: PHaRLAP
(`pylap`), GNSS (`pyserial`/`pyubx2`), Earthdata (`netCDF4`/`boto3`), `chrony`.

**Development sequence (intended, recovered as requirement):** Phase 1
ring-buffer IPC → Phase 2 temporal engine (tone detection / time-of-arrival) →
Phase 3 HDF5→SQLite migration (3a parity, 3b flip at v7.0) → **Phase 4 (shipped
v7.0): remove HDF5 code + h5py**. Tier build-out: T5 USB-GPSDO and T6 BPSK-PPS
("Phase 2B" hardware tiers) layer onto the T3/T4 software tiers. Roadmap:
amplitude calibration (for hf-tec) deferred; SQLite query optimization;
PHaRLAP raytrace overlay (advisory).

## 11. Acceptance criteria & verification

- Contract conformance → `hf-timestd validate --json` (exit 0, no `fail`) +
  surfaced via `smd status`.
- Timing accuracy/convergence → `status`/`calibrate` JSON (`quality_grade`,
  `usable`, `convergence_state`); ADEV at 60 s/1000 s.
- Pipeline liveness → the staleness watchdogs (a unit not updating its store is
  restarted) — these ARE the runtime acceptance check.
- Authority correctness → cross-tier witness disagreement flags absent under
  nominal GPS lock.
- Standalone operability → `scripts/install.sh` on a radiod-only host reaches a
  LOCKED `status` without sigmond present.

## 12. Risks & open questions

- `HFT-F-090` `[NEW]` 🟡 **Doc/code drift:** the config template still exposes
  legacy `[storage].write_hdf5/write_sqlite/read_sqlite` toggles although HDF5
  was retired and SQLite is the sole store at v7.0. The toggles SHALL be removed
  or documented as no-ops. *(Surfaced by this review — candidate #18 issue.)*
- `HFT-F-091` `[NEW]` ⬜ The **singleton-per-host** constraint (`HFT-C-003`) is
  documented but its *enforcement* (refuse a second instance / second
  station_id) is unverified. SHALL be enforced or explicitly validated.
- `HFT-Q-009` `[NEW]` ⬜ Multi-radiod `governor_radiod` disambiguation in the
  authority is present but its subscriber-side handling is untested across the
  suite (ties to sigmond `SIG-F-070`).
- Version skew: `pyproject` 7.0.0 vs `deploy.toml` 7.1.0 — intended
  future-proofing per the explore notes; confirm and align.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| HFT-I-002 (§18 authority producer) | PSWS: timing-tiering | authority cross-check | #6:50 |
| HFT-F-022 (GPS-denied fusion) | — | fusion `status` LOCKED w/o GPS | #6:25 (resilience) |
| HFT-F-030 (carrier-phase dTEC) | Clients: hf-timestd | dTEC vs GNSS VTEC | #6:19 (Doppler API) |
| HFT-F-090 (storage toggle drift) | *(new — file)* | template review | — |
| HFT-F-091 (singleton enforcement) | *(new — file)* | second-instance test | — |
| HFT-F-060 (GRAPE upload) | — | grape status / SFTP ack | #6:40 (WW0WWV→PSWS) |

*New rows (HFT-F-090/091, HFT-Q-009) are the review's surfaced gaps; promote to #18 Clients.*
