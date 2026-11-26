# v3.96 Plan E: the timing core stands alone — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GNSS VTEC leaves hf-timestd for hamsci-physics, and the two data-fetch timers that feed hf-timestd's own propagation model (IONEX and the IRI indices) leave hamsci-physics for hf-timestd, so hf-timestd installs and runs with no hamsci-physics on the host.

**Architecture:** The code moves whole, with its tests, and keeps every output where it lands today: the same SQLite table, the same directories, the same service user.  A new, general `[[systemd.moves]]` declaration in the gaining component's `deploy.toml` tells sigmond's `smd align` that a unit changed owners.  On each station align runs the gaining side's adoption command first (config and credentials), then stops and disables the old unit, then enables and starts the new one.  It never runs both copies, and it leaves a station alone, with a drift fault, when topology disables the new owner.

**Tech Stack:** Python 3.11, pytest, systemd units, bash; sigmond's stdlib-only core (`bin/smd`, `lib/sigmond/`).

**Spec:** `/home/mjh/hamsci/tasks/hf-timestd-audit/ASSESSMENT.md`, Michael's decision 10 ("Move VTEC to hamsci-physics and fetch timers into hf-timestd"), with the detail in `part-C-fusion-watchdogs.md` (C5, C24, open question 6) and `part-D-grape-physics.md` (D16, D17, D18, D19, open question 5).  Ledger items folded in: `nd-vtec-zero-division`, `vtec-local-usb-source`.

---

## What fits v3.96, and what waits

Everything in decision 10 fits v3.96.  The move changes no science and no timing value; it changes where code lives and which unit runs it.  The one new mechanism, `[[systemd.moves]]`, stays small and general, and the next move between components can reuse it.

**v3.96 (this plan), 12 tasks:**
- hamsci-physics gains the GNSS package, the VTEC service, its unit and an adoption command (Tasks 1-3).
- hamsci-physics gives up the IONEX and IRI fetch units, and drops two health checks that never ran (Task 4).
- hf-timestd gains the IONEX and IRI fetch units and its own Earthdata credential helper (Task 5).
- hf-timestd drops the VTEC code, its dependency on hamsci-physics, and fusion's read of the VTEC table (Tasks 6-8).
- sigmond learns `[[systemd.moves]]`, applies it during align, reports a half-done move as drift, and points `sigmond-site-timing` at the new VTEC config (Tasks 9-11).
- Task 12 measures B4 before and after, and checks the two ledger items on their own stations.

**Later (follow-ups, with the reason):**
- **USB u-blox sensing** (the rest of `vtec-local-usb-source`).  The serial source already exists (below).  What remains, sensing a USB u-blox at bring-up, belongs with `sensed-bringup`, which the v3.95 ledger moved after the deb-apt pilot.
- **hf-timestd's `hs-uploader` dependency.**  Nothing in hf-timestd's `src/` or `scripts/` imports `hs_uploader` since GRAPE left in August (checked 11-25).  Dropping it also shrinks the timing core, but it touches a different sibling and its own lock; one line in a later release.
- **The quota manager's `vtec` category** (`src/hf_timestd/quota_manager.py:63, 88, 187-203`).  It scans `data/gnss_vtec/*.h5`, which nothing has written since the SQLite cutover.  Decision 7 (sigmond's ledger owns retention) retires that code wholesale; touching it here would collide.
- **`pipeline-watchdog.sh` restarts `hamsci-physics-fusion`** (`scripts/pipeline-watchdog.sh:383-388`).  It only acts when that unit is enabled, so hf-timestd still runs alone.  Decision 6 (one watchdog, in sigmond) retires this restarter.
- **`hamsci-physics-fusion.service` has no CPU-affinity entry** in sigmond's `AFFINITY_UNITS` (`lib/sigmond/cpu.py:50-121`, checked 11-25), so it may run on radiod's cores.  A one-line fix, but a CPU-placement change on every station; it deserves its own announcement.
- **The VTEC-era scripts** `scripts/zedf9p_tec_client.py`, `scripts/test_nav_sat.py` and `scripts/gps_timeserver_client.py` in hf-timestd import nothing that moves.  They go in the Phase 3 dead-code sweep.

## What the code says today (verified 2026-11-25)

The audit dates from 11-23.  Three things changed since, or the ledger never caught up:

1. **AC0G-ND's ZeroDivisionError has a fix on hf-timestd main.**  Commit `039bd554` (11-22) makes `ecef_to_geodetic` raise `ValueError` at the Earth's centre (`src/hf_timestd/core/broadcast_ephemeris.py:48-66`), makes `RTCM3Adapter` ignore an implausible RTCM 1005/1006 position (`src/hf_timestd/core/rtcm3_adapter.py:103-106, 213-223`), and lets the configured station position stand in (`config_station_ecef`, `:109-122`).  Tests: `tests/test_rtcm3_station_position.py`.  The ledger item still reads `problem`.  This plan carries the fix and its tests across unchanged; Task 12 runs the item's check on ND.
2. **VTEC already reads a local USB receiver.**  Commits `17c63fbc` and `8b9459b0` (11-14) added `SerialSource` (`src/hf_timestd/core/gnss_source.py:93-185`), the `--device` flag and `[gnss_vtec] device`, and `SupplementaryGroups=dialout` on the unit.  N8UR has run it since 11-14 16:02Z.  The brief's "reads only a TCP stream" no longer holds.  The serial source moves with the rest; sensing moves to the follow-ups.
3. **B4's `hamsci-physics-iri-update.service` failed** in the audit's own snapshot (`b4/snapshot-20261123T1215Z.txt:8`: `NRestarts 3`, `failed`).  Nobody recorded why.  The move would carry the fault to `timestd-iri-update.service`.  Task 12 reads its journal first.

Other audit claims held: the only `hamsci_physics` import in hf-timestd sits at `scripts/live_vtec.py:60`; `pyproject.toml:32` and `:108` declare the dependency; the IRI health check names the pre-split timer (`hamsci-physics/scripts/iri-indices-healthcheck.sh:17`); neither health-check timer appears in hamsci-physics's `deploy.toml` units.

## Who reads each output, before and after

| Output | Written by (today → v3.96) | Read by | Changes for the reader |
|---|---|---|---|
| `L3_gnss_vtec` rows, channel `GNSS`, in `/var/lib/timestd/phase2/timestd.db` | `timestd-vtec.service` → `hamsci-physics-vtec.service` | station-web (`services/gnss_vtec.py`, `routers/tec.py`); hamsci-physics-fusion's dTEC anchor (`physics_fusion_service.py:1269-1339`); hf-timestd fusion's cross-check (`multi_broadcast_fusion.py:3420-3504, 4291-4378`) | none for station-web and physics-fusion: same table, columns, channel and `processing_version`.  hf-timestd fusion stops reading it (Task 8). |
| `data/gnss_vtec.csv` under `/var/lib/timestd` | same move, when `save_csv = true` | only the manual `scripts/health-check-vtec.sh:35` | that script goes (Task 7).  See open question 3. |
| `data/dcb/`, `data/brdc/` caches under `/var/lib/timestd` | same move | the VTEC service itself | none: same user, same working directory. |
| `/var/lib/timestd/ionex/*.INX.gz` | `hamsci-physics-ionex-download` → `timestd-ionex-download` | hf-timestd's `IonoDataService` (IRI fallback, `core/iono_data_service.py:20-21` → `hamsci_dsp.ionosphere.data_service`), run inside metrology and l2-calibration; `hf-timestd data sources` (`cli.py:1360-1379`) | none: same directory, same user. |
| `apf107.dat`, `ig_rz.dat` in `/opt/pharlap_4.7.4/dat/iri2020` | `hamsci-physics-iri-update` → `timestd-iri-update` | PHaRLAP ray tracing through hamsci-dsp's propagation model, used by hf-timestd; `hf-timestd data sources` (`cli.py:1381-1395`) | none: same directory; still runs as root. |
| `/etc/hf-timestd/earthdata-netrc` | operator | today the VTEC unit (`NETRC=`); after v3.96 the IONEX unit | adoption copies it to `/etc/hamsci-physics/earthdata-netrc` for VTEC, if that file is absent. |
| `/etc/hamsci-physics/earthdata-netrc` | operator | today the IONEX unit; after v3.96 the VTEC unit | adoption copies it to `/etc/hf-timestd/earthdata-netrc` for IONEX, if absent. |

No reader outside these three repos and station-web reads any of them (grep of every checkout under `/home/mjh/hamsci/repos` and `ops`, 11-25).

## How a station upgrades

`smd align --apply --release v3.96` re-executes itself under the new smd once sigmond's checkout moves (`bin/smd:10260-10297`), so the new move logic runs in the same pass.  Then, on each station:

1. `apply_plan` moves the checkouts.  hf-timestd's install runs (`pyproject.toml` changed); `uv sync --frozen` drops `hamsci_physics` from hf-timestd's venv.  hamsci-physics's `[build]` runs `pip install -e .` and gains `pyserial`, `pyubx2`, `pyrtcm`.  The running `timestd-vtec` process keeps going from memory; its unit file link now dangles.
2. **New step, before bring-up:** for each `[[systemd.moves]]` entry whose old units are enabled or active here:
   1. Run the gaining component's `migrate` command.  For VTEC, `hamsci-physics vtec adopt` writes `/etc/hamsci-physics/gnss-vtec.toml` from hf-timestd's `[gnss_vtec]` (only where it reads `enabled = true`) and copies the Earthdata credential.  A failure here stops the move and leaves the old unit running.
   2. Stop and disable the old units; clear any dangling link; `daemon-reload`.
   3. Enable the new unit, and start it when an old one was active.
3. Bring-up continues: `sigmond-site-timing` now reads and writes `/etc/hamsci-physics/gnss-vtec.toml`; declared timers get enabled; the restart stage restarts moved components' active services.

The VTEC gap lasts the seconds between step 2.2 and 2.3.  No serial port ever has two readers.  IONEX and IRI lose nothing: their files persist, and each fetch reaches back (IONEX up to 7 days, IRI keeps the last good file).  A station that skips v3.96 and aligns to a later release gets the same moves, because the entries stay in `deploy.toml` until a release drops them on purpose.

## Open questions for Michael

**Answered 2026-11-25: all as recommended.  Question 7: Plan D owns hamsci-physics-fusion's default position (review question 30), so this plan adds no thirteenth task.**

1. **The ND RTCM capture in the test fixtures holds ND's surveyed antenna position.**  `tests/fixtures/gnss/lg290p_ac0g-nd_20261004T2040Z_20epochs.rtcm3` carries RTCM 1005 frames with ND's ECEF position, and `tests/unit/test_rtcm3_adapter.py:93-95` writes ND's latitude and longitude to five decimals in a comment and two asserts.  Both sit in hf-timestd's public history already.  Moving the tests would copy them into a second public repo.  The capture's pseudoranges, with the broadcast ephemeris beside them, also imply the position to tens of metres.  **Recommendation:** in the copy, rewrite each 1005 frame to the centre of grid square EN16 and replace the comment and asserts with that centre (Task 1, Step 3).  Accept the residue in the pseudoranges.  Leave hf-timestd's history alone; a rewrite would cost more than it protects.  A synthetic capture would remove the residue, at a day's work.
2. **hf-timestd's installer adds the GNSS box as a preferred chrony server.**  `scripts/install.sh:948-957` appends `server <[gnss_vtec].host> iburst prefer` to chrony.conf.  Once `[gnss_vtec]` leaves hf-timestd's config, that block reads nothing.  `sigmond-site-timing --trust-ntp` already owns the station's LAN NTP servers.  **Recommendation:** delete the block (Task 7, Step 5).  Lines it already wrote stay in each chrony.conf; nothing removes them.  If you want the GNSS box offered as an NTP server, site-timing's NTP scan finds it on port 123 today.
3. **`gnss_vtec.csv` has no reader** but a manual health script (C24).  When `save_csv` reads false, `live_vtec.py:421-422` prints every epoch to stdout instead, about 86,400 journal lines a day.  **Recommendation:** the moved service never prints epochs to stdout (Task 2); the example config ships `save_csv = false`; adoption keeps each station's current value.  Retire the CSV writer in a later release if nobody claims it.
4. **Retention of `L3_gnss_vtec`.**  Nothing declares it today.  hamsci-physics becomes its writer, so its inventory should declare it.  **Recommendation:** declare 30 days, as decision 3 set for metrology's science tables.  Enforcement belongs to the decision 7 plan (sigmond's ledger).
5. **Where the Earthdata login helper lives.**  After the move both repos fetch from NASA CDDIS: hamsci-physics for VTEC's DCB files, hf-timestd for IONEX.  **Recommendation:** copy the 150-line `cddis_auth.py` into hf-timestd as `core/earthdata_auth.py`, as hamsci-physics copied `iq_codec.py` the other way.  The cleaner home, hamsci-dsp, would tie v3.96 to hamsci-dsp's untested PyPI-prep head, which the v3.94 roll deferred.
6. **Fusion's VTEC cross-check overlaps Plan D.**  Plan D (`2026-11-25-v396-metrology-and-fusion.md`) rewrites `multi_broadcast_fusion.py` for decision 5.  This plan's Task 8 deletes the VTEC cross-check from the same file, because fusion reading a hamsci-physics product would break "stands alone".  **Recommendation:** Task 8 lands first; it deletes code and adds none, so Plan D rebases over it by content, not line numbers.  The change alters only the witness value's weights on stations with GNSS (the cross-check raised a row's confidence by 10 % and tagged its mode); it never touched `D_clock` (`tests/test_fusion_gnss_vtec_rtp_gate.py` pins that today).
7. **hamsci-physics-fusion's Columbia, Missouri default** (audit fault 6, second half).  Plan D asks "whichever v3.96 plan owns hamsci-physics" to take it.  Task 2 here adds a station-position helper to hamsci-physics that follows Plan D's rule.  **Recommendation:** the GRAPE plan, if it touches `physics_fusion_service.py`, wires the helper in; otherwise this plan adds that as a thirteenth task.  Say which.

## Estimate

12 tasks.  No task runs anything on a station while it builds.  At rollout, `smd align` changes units on stations three ways: on each station that runs VTEC (B4, AC0G-ND, N8UR at least), one service moves; on each station that runs hamsci-physics, two timers move and two never-enabled health-check units vanish; sigmond's new affinity entries place three new units off radiod's cores.  The usual align restarts of hf-timestd, hamsci-physics and sigmond follow.

---

## Global Constraints

- Every change ships in sigmond v3.96 through `smd align`; never a hand edit on a station.
- Each change gets measured on B4 before and after with the audit's 24-hour counters (Task 12).
- Timing faults get exposed, never silently corrected; one registration for all consumers.  This plan changes no timing value.
- hamsci-physics never imports `hf_timestd` (`hamsci-physics/tests/test_import_lint.py`).  After Task 6, hf-timestd never imports `hamsci_physics` and declares no dependency on it.
- sigmond's core stays stdlib-only.
- Precise station positions never go into repos, public logs or test fixtures.  Tests use grid-square centres (`FN31pr`, or `EN16` for the ND capture).  Adoption and the VTEC service never log a latitude or longitude.
- Outputs keep their shape: table `L3_gnss_vtec`, channel `GNSS`, product level `L3`, product name `gnss_vtec`, `processing_version '1.0.0'`, the same measurement keys; default store `/var/lib/timestd/phase2/timestd.db`; `WorkingDirectory=/var/lib/timestd` so the relative paths `data/gnss_vtec.csv`, `data/dcb`, `data/brdc` keep their meaning; IONEX in `/var/lib/timestd/ionex`; IRI indices in `/opt/pharlap_4.7.4/dat/iri2020`.
- A move stops the old unit before it starts the new one.  Two copies never run.
- Tests: hf-timestd and hamsci-physics run `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/<name> -o addopts="" <paths>`; sigmond runs `.venv/bin/pytest <paths>`.  Each new test fails when the code it checks is removed: every task names its mutation check.
- Commits stage named files only, and each message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z
  ```
- Docs describe the current best state; each task updates the docs its change makes wrong.

## Review Focus

1. **A station runs `timestd-vtec` but topology disables hamsci-physics.**  The move must leave the old unit running and say so loudly, as a drift fault naming `smd enable hamsci-physics`.  Pinned in Task 9 (`test_disabled_owner_holds_the_move`) and Task 10 (`test_drift_faults_a_held_move`).
2. **The old unit's file vanished before the move ran** (hf-timestd's checkout moved first, so `/etc/systemd/system/timestd-vtec.service` dangles).  `systemctl disable` may fail on it; the move must still clear the wants-link and the dangling link.  Pinned in Task 9 (`test_dangling_old_unit_is_cleared`).
3. **A USB receiver (N8UR) never has two readers.**  The old unit stops before the new one starts.  Pinned in Task 9 (`test_stop_comes_before_start`).
4. **A re-run after a partial failure does nothing twice.**  With the old unit neither enabled nor active, the move reports "unchanged".  A failed `migrate` leaves the old unit running and blocks the align.  Pinned in Task 9 (`test_second_run_is_unchanged`, `test_failed_migrate_keeps_the_old_unit`).
5. **A station with `[gnss_vtec] enabled = false`, or the template's placeholder host, gets no VTEC process at all**, not an idle one.  Adoption writes no file; the new unit's `ConditionPathExists` skips it.  Pinned in Task 3 (`test_disabled_section_writes_nothing`, `test_unit_gates_on_the_config_file`).

---

## File Structure

**hamsci-physics** (`/home/mjh/hamsci/repos/hamsci-physics`)

| Path | Action | Responsibility |
|---|---|---|
| `src/hamsci_physics/gnss/__init__.py` | create | package marker; one-line docstring |
| `src/hamsci_physics/gnss/{ubx_parser,rtcm3_adapter,broadcast_ephemeris,brdc_fetcher,gnss_source,gnss_tec}.py` | create (copied) | receiver framing, RTCM3 adapter, ephemeris, sources, TEC physics; content from hf-timestd with imports renamed |
| `src/hamsci_physics/gnss/station.py` | create | `station_ecef(...)`: the antenna position for elevation geometry |
| `src/hamsci_physics/gnss/vtec_service.py` | create | the service loop (port of `live_vtec.py`) |
| `src/hamsci_physics/gnss/adopt.py` | create | one-time adoption of hf-timestd's `[gnss_vtec]` and Earthdata credential |
| `src/hamsci_physics/cli.py` | modify | `vtec adopt` subcommand |
| `src/hamsci_physics/contract.py` | modify | declare `timestd.L3_gnss_vtec` sink when VTEC runs |
| `systemd/hamsci-physics-vtec.service` | create | the VTEC unit |
| `config/gnss-vtec.toml.example` | create | the VTEC config, documented |
| `deploy.toml` | modify | add the VTEC unit and its `[[systemd.moves]]`; drop the fetch timers |
| `pyproject.toml` | modify | `pyserial`, `pyubx2`, `pyrtcm` |
| `systemd/hamsci-physics-{ionex-download,iri-update,iri-healthcheck,spaceweather-healthcheck}.{service,timer}` | delete | moved or cut |
| `scripts/{download_ionex_daily.sh,ionex_integration.py,update-iri-indices.sh,iri-indices-healthcheck.sh,spaceweather-healthcheck.sh}` | delete | moved or cut |
| `tests/test_gnss_*.py`, `tests/fixtures/gnss/*` | create | moved tests and fixtures |
| `tests/test_update_iri_indices.py`, `tests/test_package_imports.py` | delete / modify | moved to hf-timestd |
| `README.md`, `docs/INDEX.md`, `docs/NASA_EARTHDATA_SETUP.md` | modify | current state |

**hf-timestd** (`/home/mjh/hamsci/repos/hf-timestd`)

| Path | Action | Responsibility |
|---|---|---|
| `src/hf_timestd/core/earthdata_auth.py` | create | Earthdata netrc lookup and session (copy of hamsci-physics `cddis_auth.py`), plus `adopt_netrc` |
| `scripts/ionex_integration.py`, `scripts/download_ionex_daily.sh`, `scripts/update-iri-indices.sh` | create (moved) | the two fetchers |
| `systemd/timestd-{ionex-download,iri-update}.{service,timer}` | create | their units |
| `src/hf_timestd/cli.py` | modify | `data adopt-netrc`; `[gnss_vtec]` joins `RETIRED_SECTIONS` |
| `deploy.toml` | modify | add the four fetch units and two `[[systemd.moves]]`; drop `timestd-vtec` |
| `src/hf_timestd/core/{ubx_parser,rtcm3_adapter,broadcast_ephemeris,brdc_fetcher,gnss_source,gnss_tec}.py`, `scripts/live_vtec.py`, `scripts/timestd-vtec.service`, `systemd/timestd-vtec.service`, `scripts/health-check-vtec.sh` | delete | moved |
| `src/hf_timestd/service_profile.py` | modify | no `vtec` service |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | modify | no VTEC read |
| `pyproject.toml`, `uv.lock` | modify | no `hamsci-physics`, no `gnss` extra |
| `scripts/install.sh`, `scripts/setup-station.sh`, `scripts/{start,stop}-services.sh`, `scripts/verify_pipeline.sh`, `scripts/check-freshness-alert.sh` | modify | forget VTEC; install the fetch units |
| `config/timestd-config.toml.template` | modify | no `[gnss_vtec]` |
| `tests/test_timing_core_stands_alone.py` | create | the stand-alone guard |
| `CLAUDE.md`, `README.md`, `docs/INDEX.md`, `docs/GNSS_TIMESERVER_AND_VTEC.md`, `docs/external-data-sources.md` | modify | current state |

**sigmond** (`/home/mjh/hamsci/repos/sigmond`)

| Path | Action | Responsibility |
|---|---|---|
| `lib/sigmond/unit_moves.py` | create | parse, inspect and apply `[[systemd.moves]]`; drift items |
| `bin/smd` | modify | align's dry run and apply call the moves |
| `lib/sigmond/drift/build_report.py` | modify | add the moves collector |
| `bin/sigmond-site-timing` | modify | step 4 writes `/etc/hamsci-physics/gnss-vtec.toml` |
| `lib/sigmond/cpu.py` | modify | three new units in `AFFINITY_UNITS` |
| `docs/CLIENT-CONTRACT.md`, `docs/operator/glossary.md`, `docs/operator/day-2.md`, `CHANGELOG.md` | modify | the new key; the new unit name |

---

### Task 1: hamsci-physics gains the GNSS package

**Files:**
- Create: `hamsci-physics/src/hamsci_physics/gnss/__init__.py` and the six modules named in File Structure, each copied from `hf-timestd/src/hf_timestd/core/<same name>.py` at hf-timestd main `2716b839`
- Create: `hamsci-physics/tests/test_gnss_ubx_parser.py` (from hf-timestd `tests/unit/test_ubx_parser.py`), `tests/test_gnss_rtcm3_adapter.py` (from `tests/unit/test_rtcm3_adapter.py`), `tests/test_gnss_station_position_rtcm.py` (from `tests/test_rtcm3_station_position.py`), `tests/test_gnss_serial_source.py` (from `tests/unit/test_gnss_serial_source.py`, minus `test_live_vtec_has_device_flag_and_reopens_on_serial_error` and `test_unit_and_installer_grant_dialout`, which Tasks 2 and 3 replace)
- Create: `hamsci-physics/tests/fixtures/gnss/BRDC00WRD_S_20262770000_GPS_16-23h_excerpt.rnx` (copied) and `tests/fixtures/gnss/lg290p_20261004T2040Z_20epochs_en16.rtcm3` (rewritten, Step 3)
- Create: `hamsci-physics/tests/fixtures/gnss/regrid_1005.py` (the rewriting tool, kept so the fixture can be rebuilt)
- Modify: `hamsci-physics/pyproject.toml` dependencies
- Test: `hamsci-physics/tests/test_gnss_package.py`

**Interfaces:**
- Produces: `hamsci_physics.gnss.<module>` with every public name the hf-timestd module had (`UBXParser`, `RTCM3Adapter`, `config_station_ecef`, `looks_like_rtcm3`, `plausible_ecef`, `BroadcastEphemeris`, `ecef_to_geodetic`, `geodetic_to_ecef`, `elevation_azimuth`, `BrdcFetcher`, `DEFAULT_BRDC_URL`, `brdc_url`, `make_source`, `SerialSource`, `TcpSource`, `build_receiver_config`, `GNSSTECAnalyzer`, `_MODULE_VERSION`).

- [ ] **Step 1: Write the failing package test**

```python
# tests/test_gnss_package.py
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_gnss_modules_import_from_hamsci_physics():
    from hamsci_physics.gnss.rtcm3_adapter import RTCM3Adapter, plausible_ecef
    from hamsci_physics.gnss.gnss_source import make_source
    from hamsci_physics.gnss.gnss_tec import GNSSTECAnalyzer
    from hamsci_physics.gnss.broadcast_ephemeris import ecef_to_geodetic
    assert RTCM3Adapter.__module__ == "hamsci_physics.gnss.rtcm3_adapter"
    ok, _ = GNSSTECAnalyzer.self_test()
    assert ok

def test_pyproject_declares_the_receiver_libraries():
    deps = " ".join(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"])
    for name in ("pyserial", "pyubx2", "pyrtcm"):
        assert name in deps, name
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd /home/mjh/hamsci/repos/hamsci-physics && .venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_package.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hamsci_physics.gnss'`.

- [ ] **Step 3: Copy the modules and tests; rewrite the ND fixture**

Copy each module; change every `hf_timestd.core.` import to `hamsci_physics.gnss.`.  Change nothing else in the modules.  Add to `pyproject.toml` dependencies: `"pyserial>=3.5"`, `"pyubx2>=1.2.0"`, `"pyrtcm>=1.1.0"` (hf-timestd's lock holds 3.5, 1.2.60, 1.1.12).  Install them into the dev venv with `.venv/bin/pip install -e .`.

Copy the tests with the same import rename.  In `test_gnss_rtcm3_adapter.py`, point `FIX` at `tests/fixtures/gnss` and `RTCM` at the rewritten capture.  In `test_gnss_serial_source.py`, keep the `SerialSource` tests.

Build the rewritten capture with `tests/fixtures/gnss/regrid_1005.py`: `regrid(src: Path, dst: Path, lat: float, lon: float, h: float) -> int` returns the number of 1005/1006 frames rewritten.  The algorithm, which the signature does not fix: walk the RTCM3 frames (preamble `0xD3`, 10-bit length, 24-bit CRC24Q, as `RTCM3Adapter._frames` does); for a message whose first 12 payload bits read 1005 or 1006, overwrite DF025 (payload bits 34-71), DF026 (74-111) and DF027 (114-151), each a 38-bit two's-complement count of 0.1 mm, with `geodetic_to_ecef(lat, lon, h)`; recompute the CRC over header plus payload; copy every other frame byte for byte.  Run it once, from the hamsci-physics checkout, on hf-timestd's capture with `lat=46.5, lon=-97.0, h=300.0` (the centre of EN16).  Commit only its output.

In the copied `test_antenna_position_comes_from_1005`, replace the ND comment and both asserts with `pytest.approx(46.5, abs=1e-4)` and `pytest.approx(-97.0, abs=1e-4)`.  A test that pins a VTEC or elevation value the position shifts (the antenna moves tens of kilometres) gets its expected value re-derived from the rewritten capture; name each such change in the commit message.  Grep the new files for `46.90` and `-96.79`; expect no match.

- [ ] **Step 4: Run the package test and every moved test**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_package.py tests/test_gnss_ubx_parser.py tests/test_gnss_rtcm3_adapter.py tests/test_gnss_station_position_rtcm.py tests/test_gnss_serial_source.py tests/test_import_lint.py -v`
Expected: all PASS.  The import lint passes because no new module imports `hf_timestd`.

Mutation check: delete the `if math.hypot(x, y, z) < 1.0: raise ValueError` guard in `gnss/broadcast_ephemeris.py`; `test_zero_position_raises_valueerror_not_zerodivision` must fail.  Delete `pyrtcm` from `pyproject.toml`; `test_pyproject_declares_the_receiver_libraries` must fail.  Restore both.

- [ ] **Step 5: Commit**

```bash
git add src/hamsci_physics/gnss/__init__.py src/hamsci_physics/gnss/ubx_parser.py \
  src/hamsci_physics/gnss/rtcm3_adapter.py src/hamsci_physics/gnss/broadcast_ephemeris.py \
  src/hamsci_physics/gnss/brdc_fetcher.py src/hamsci_physics/gnss/gnss_source.py \
  src/hamsci_physics/gnss/gnss_tec.py pyproject.toml tests/test_gnss_package.py \
  tests/test_gnss_ubx_parser.py tests/test_gnss_rtcm3_adapter.py \
  tests/test_gnss_station_position_rtcm.py tests/test_gnss_serial_source.py \
  tests/fixtures/gnss/regrid_1005.py tests/fixtures/gnss/BRDC00WRD_S_20262770000_GPS_16-23h_excerpt.rnx \
  tests/fixtures/gnss/lg290p_20261004T2040Z_20epochs_en16.rtcm3
git commit -m "gnss: the receiver and TEC modules move here from hf-timestd (decision 10)

The ND capture's antenna position now reads EN16's centre; no station's coordinates enter this repo.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 2: the VTEC service in hamsci-physics

**Files:**
- Create: `hamsci-physics/src/hamsci_physics/gnss/station.py`
- Create: `hamsci-physics/src/hamsci_physics/gnss/vtec_service.py` (port of hf-timestd `scripts/live_vtec.py`)
- Test: `hamsci-physics/tests/test_gnss_vtec_service.py`

**Interfaces:**
- Consumes: Task 1's modules; `hamsci_physics.cddis.CDDISDownloader`; `hamsci_dsp.io.make_data_product_writer`; `hamsci_dsp.geometry.grid_to_latlon`.
- Produces:
  - `station_ecef(config: dict, environ: Mapping[str, str]) -> Optional[Tuple[float, float, float]]` in `gnss/station.py`.  Order: `config_station_ecef(config)` (config `[station] latitude/longitude/altitude_m`, refusing 0/0 and out-of-range); else `STATION_LAT`/`STATION_LON` from `environ` under the same rules; else the centre of `config["station"]["grid_square"]` or `environ["STATION_GRID"]` (at least 4 characters, not a `<...>` placeholder); else `None`.  It logs nothing.
  - `load_config(gnss_path: Path, main_path: Path) -> dict` in `vtec_service.py`: returns `{"gnss_vtec": ..., "station": ..., "storage": ..., "logging": ...}`.  `[gnss_vtec]` and `[station]` come from `gnss_path`; `[storage]` and `[logging]` from `main_path`; a `[station]` key in `gnss_path` wins over the same key in `main_path`; a missing file reads as empty.
  - `write_epoch_line(line: str, csv_file: Optional[TextIO]) -> None`: writes to the CSV when given, else does nothing.
  - `main(argv: Optional[list] = None) -> int`, runnable as `python -m hamsci_physics.gnss.vtec_service --gnss-config PATH --config PATH [--host H --port P --device D --download-only]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gnss_vtec_service.py
import io
from hamsci_dsp.geometry import grid_to_latlon
from hamsci_physics.gnss.broadcast_ephemeris import ecef_to_geodetic, geodetic_to_ecef
from hamsci_physics.gnss.station import station_ecef
from hamsci_physics.gnss import vtec_service as vs
import math, pytest

GRID = "FN31pr"
LAT, LON = grid_to_latlon(GRID)

def _deg(ecef):
    lat, lon, _ = ecef_to_geodetic(*ecef)
    return math.degrees(lat), math.degrees(lon)

def test_config_position_wins_over_environment():
    pos = station_ecef({"station": {"latitude": LAT, "longitude": LON}},
                       {"STATION_LAT": "10.0", "STATION_LON": "10.0"})
    assert _deg(pos) == pytest.approx((LAT, LON), abs=1e-6)

def test_environment_position_when_config_has_none():
    pos = station_ecef({}, {"STATION_LAT": str(LAT), "STATION_LON": str(LON)})
    assert _deg(pos) == pytest.approx((LAT, LON), abs=1e-6)

def test_template_zeroes_fall_to_the_grid_centre():
    pos = station_ecef({"station": {"latitude": 0.0, "longitude": 0.0}}, {"STATION_GRID": GRID})
    assert _deg(pos) == pytest.approx((LAT, LON), abs=1e-6)

@pytest.mark.parametrize("cfg,env", [({}, {}), ({"station": {"grid_square": "<YOUR_GRID>"}}, {}),
                                      ({}, {"STATION_LAT": "95", "STATION_LON": "0"})])
def test_no_usable_position_is_none(cfg, env):
    assert station_ecef(cfg, env) is None

def test_load_config_takes_each_table_from_its_file(tmp_path):
    g = tmp_path / "gnss-vtec.toml"; m = tmp_path / "config.toml"
    g.write_text('[gnss_vtec]\nenabled = true\nhost = "192.0.2.10"\n[station]\nlatitude = 1.5\n')
    m.write_text('[station]\nlatitude = 9.0\ngrid_square = "FN31"\n[storage]\nsqlite_path = "/x.db"\n')
    c = vs.load_config(g, m)
    assert c["gnss_vtec"]["host"] == "192.0.2.10"
    assert c["station"] == {"latitude": 1.5, "grid_square": "FN31"}
    assert c["storage"]["sqlite_path"] == "/x.db"

def test_missing_files_read_empty(tmp_path):
    assert vs.load_config(tmp_path / "a", tmp_path / "b")["gnss_vtec"] == {}

def test_no_csv_means_no_stdout(capsys):
    vs.write_epoch_line("1,2,3\n", None)
    assert capsys.readouterr().out == ""

def test_csv_gets_the_line():
    f = io.StringIO()
    vs.write_epoch_line("1,2,3\n", f)
    assert f.getvalue() == "1,2,3\n"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_vtec_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hamsci_physics.gnss.station'`.

- [ ] **Step 3: Write `station.py` and `vtec_service.py`**

`vtec_service.py` keeps `live_vtec.py`'s loop as it stands at hf-timestd `2716b839` (reconnect backoff, watchdog petting, protocol sniffing, the plausibility gate, 60 s batch flush, the measurement dict and its keys), with four changes:
1. Configuration comes from `load_config(args.gnss_config, args.config)`.  `make_source` reads `cfg["gnss_vtec"]`; the writer gets `storage_config=cfg["storage"]`.
2. The RTCM3 fallback position comes from `station_ecef(cfg, os.environ)` in place of `config_station_ecef(config)`.  When it returns `None`, log one WARNING: `"no station position in gnss-vtec.toml, config.toml or coordination.env: RTCM3 elevations wait for a surveyed 1005"`.  Never log the position.
3. The epoch line goes through `write_epoch_line`; the `print(line, end='')` branch (`live_vtec.py:421-422`) goes.
4. `CDDISDownloader` comes from `hamsci_physics.cddis`, as before; its credential comes from `NETRC` in the unit (Task 3).

The disabled branch (`enabled` false: notify `READY=1`, then idle while petting the watchdog) stays, for a hand-written file that says `enabled = false`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_vtec_service.py tests/test_import_lint.py -v`
Expected: PASS.

Mutation check: make `station_ecef` skip the environment branch; `test_environment_position_when_config_has_none` must fail.  Put the `print(line, end="")` back into `write_epoch_line`'s no-CSV path; `test_no_csv_means_no_stdout` must fail.  Restore both.

- [ ] **Step 5: Commit**

```bash
git add src/hamsci_physics/gnss/station.py src/hamsci_physics/gnss/vtec_service.py tests/test_gnss_vtec_service.py
git commit -m "gnss: the VTEC service, its position from config, coordination.env or grid; no epoch lines on stdout

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 3: the VTEC unit, adoption, and the move declaration

**Files:**
- Create: `hamsci-physics/systemd/hamsci-physics-vtec.service`
- Create: `hamsci-physics/config/gnss-vtec.toml.example`
- Create: `hamsci-physics/src/hamsci_physics/gnss/adopt.py`
- Modify: `hamsci-physics/src/hamsci_physics/cli.py` (subparsers at `:252-318`, dispatch in `main()` at `:328`)
- Modify: `hamsci-physics/src/hamsci_physics/contract.py` (`_data_sinks`, `:60-92`)
- Modify: `hamsci-physics/deploy.toml` (`[systemd]` at `:130-143`)
- Modify: `hamsci-physics/README.md`, `docs/INDEX.md`, `docs/NASA_EARTHDATA_SETUP.md`
- Test: `hamsci-physics/tests/test_gnss_adopt.py`, `tests/test_gnss_vtec_unit.py`; modify `tests/test_deploy_contract.py`, `tests/test_client_contract.py`

**Interfaces:**
- Consumes: nothing from earlier tasks at run time; the unit runs Task 2's module.
- Produces:
  - In `adopt.py`: `GNSS_VTEC_TOML = Path("/etc/hamsci-physics/gnss-vtec.toml")`; `extract_table(text: str, table: str) -> Optional[str]` (the raw lines from the `[table]` header up to, not including, the next line whose first non-blank character opens a `[` header; trailing blank lines dropped; `None` when absent); `adopt_vtec(src_config: Path, dst: Path, now: datetime) -> str` returning `"kept"` (dst exists), `"none"` (no section, `enabled` not `true`, or neither a real host nor a device), or `"written"`; `adopt_netrc(src: Path, dst: Path) -> str` returning `"kept"`, `"absent"` or `"copied"`.
  - CLI: `hamsci-physics vtec adopt [--from-config P] [--to P] [--netrc-from P] [--netrc-to P]`, defaults `/etc/hf-timestd/timestd-config.toml`, `GNSS_VTEC_TOML`, `/etc/hf-timestd/earthdata-netrc`, `/etc/hamsci-physics/earthdata-netrc`.  It prints exactly two lines, `gnss-vtec: <word> (<dst>)` and `earthdata netrc: <word> (<dst>)`, and exits 0; an `OSError` prints its message and exits 1.
  - deploy.toml: `[[systemd.moves]]` with `from = ["timestd-vtec.service"]`, `to = "hamsci-physics-vtec.service"`, `migrate = ["/opt/git/sigmond/hamsci-physics/venv/bin/hamsci-physics", "vtec", "adopt"]`.  Task 9 parses this shape.

`adopt_vtec` writes the extracted `[gnss_vtec]` text under a two-line comment header (`# Adopted from <src> by hamsci-physics vtec adopt, <UTC ISO>.` and `# Edit here; hf-timestd no longer reads [gnss_vtec].`).  When the source's `[station]` holds a non-zero `latitude` and `longitude`, it appends a `[station]` table with `latitude`, `longitude` and, when present, `altitude_m`, so RTCM3 receivers that send a zero 1005 keep their fallback.  It writes mode 0644.  `adopt_netrc` copies bytes, sets mode 0600, and gives the copy the source's owner and group.  Neither prints or logs a coordinate.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gnss_adopt.py
import os, stat
from datetime import datetime, timezone
from hamsci_physics.gnss.adopt import adopt_netrc, adopt_vtec, extract_table

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)
SRC = '''[storage]
sqlite_path = "/var/lib/timestd/phase2/timestd.db"

[gnss_vtec]
enabled = true                # wired by sigmond-site-timing
host = "192.0.2.10"
port = 9000
protocol = "auto"
save_csv = true

[next]
x = 1
'''

def test_extract_table_stops_at_the_next_header():
    t = extract_table(SRC, "gnss_vtec")
    assert t.startswith("[gnss_vtec]") and 'host = "192.0.2.10"' in t and "[next]" not in t

def test_enabled_section_is_written_with_comments_kept(tmp_path):
    src, dst = tmp_path / "t.toml", tmp_path / "gnss-vtec.toml"
    src.write_text(SRC)
    assert adopt_vtec(src, dst, NOW) == "written"
    text = dst.read_text()
    assert "# wired by sigmond-site-timing" in text and "save_csv = true" in text
    assert stat.S_IMODE(dst.stat().st_mode) == 0o644

def test_disabled_section_writes_nothing(tmp_path):
    src, dst = tmp_path / "t.toml", tmp_path / "gnss-vtec.toml"
    src.write_text(SRC.replace("enabled = true ", "enabled = false"))
    assert adopt_vtec(src, dst, NOW) == "none" and not dst.exists()

def test_placeholder_host_writes_nothing(tmp_path):
    src, dst = tmp_path / "t.toml", tmp_path / "gnss-vtec.toml"
    src.write_text(SRC.replace('"192.0.2.10"', '"<YOUR_GNSS_HOST>"'))
    assert adopt_vtec(src, dst, NOW) == "none"

def test_existing_file_is_kept(tmp_path):
    src, dst = tmp_path / "t.toml", tmp_path / "gnss-vtec.toml"
    src.write_text(SRC); dst.write_text("[gnss_vtec]\nenabled = true\n")
    assert adopt_vtec(src, dst, NOW) == "kept"
    assert dst.read_text() == "[gnss_vtec]\nenabled = true\n"

def test_station_position_carries_without_printing(tmp_path, capsys):
    src, dst = tmp_path / "t.toml", tmp_path / "gnss-vtec.toml"
    src.write_text("[station]\nlatitude = 41.6875\nlongitude = -72.7917\n" + SRC)  # FN31pr centre
    adopt_vtec(src, dst, NOW)
    assert "latitude = 41.6875" in dst.read_text()
    assert "41.68" not in capsys.readouterr().out

def test_netrc_copy_keeps_mode_and_owner(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("machine urs.earthdata.nasa.gov login u password p\n"); os.chmod(src, 0o600)
    assert adopt_netrc(src, dst) == "copied"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert (dst.stat().st_uid, dst.stat().st_gid) == (src.stat().st_uid, src.stat().st_gid)
    assert adopt_netrc(src, dst) == "kept"
    assert adopt_netrc(tmp_path / "none", tmp_path / "c") == "absent"
```

```python
# tests/test_gnss_vtec_unit.py
import tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
UNIT = (ROOT / "systemd" / "hamsci-physics-vtec.service").read_text()

def test_unit_gates_on_the_config_file():
    assert "ConditionPathExists=/etc/hamsci-physics/gnss-vtec.toml" in UNIT

def test_unit_runs_the_module_as_timestd_where_the_data_lives():
    for line in ("User=timestd", "SupplementaryGroups=dialout", "WorkingDirectory=/var/lib/timestd",
                 "Environment=NETRC=/etc/hamsci-physics/earthdata-netrc",
                 "EnvironmentFile=-/etc/sigmond/coordination.env", "Type=notify", "WatchdogSec=60",
                 "-m hamsci_physics.gnss.vtec_service"):
        assert line in UNIT, line

def test_deploy_declares_the_unit_and_its_move():
    d = tomllib.loads((ROOT / "deploy.toml").read_text())["systemd"]
    assert d["units"][0] == "hamsci-physics-fusion.service"      # main service stays first
    assert "hamsci-physics-vtec.service" in d["units"]
    assert d["moves"] == [{"from": ["timestd-vtec.service"], "to": "hamsci-physics-vtec.service",
                           "migrate": ["/opt/git/sigmond/hamsci-physics/venv/bin/hamsci-physics",
                                       "vtec", "adopt"]}]
```

In `tests/test_client_contract.py`, add `test_inventory_declares_gnss_vtec_only_where_it_runs`: with a temporary `gnss-vtec.toml` that reads `enabled = true`, `build_inventory` lists a sink `{"kind": "service", "target": "timestd.L3_gnss_vtec", "retention_days": 30, ...}`; with no file it lists none.  `contract.py` takes the file path as a module constant `GNSS_VTEC_TOML` that the test monkeypatches.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_adopt.py tests/test_gnss_vtec_unit.py tests/test_client_contract.py -v`
Expected: FAIL (`ModuleNotFoundError: hamsci_physics.gnss.adopt`; the unit file missing).

- [ ] **Step 3: Write the unit, the example, `adopt.py`, the CLI and the sink**

The unit copies `hf-timestd/systemd/timestd-vtec.service` (OnFailure, StartLimit, Type=notify, WatchdogSec=60, NotifyAccess=main, MemoryMax=400M, MemorySwapMax=0, MALLOC_ARENA_MAX=2, Restart=always, RestartSec=10, journal output) with these differences: `Description=HamSCI Physics — GNSS VTEC`, `ConditionPathExists=/etc/hamsci-physics/gnss-vtec.toml`, no `EnvironmentFile=/etc/hf-timestd/environment`, `NETRC=/etc/hamsci-physics/earthdata-netrc`, `SyslogIdentifier=hamsci-physics-vtec`, and `ExecStart=/opt/git/sigmond/hamsci-physics/venv/bin/python -u -m hamsci_physics.gnss.vtec_service --gnss-config /etc/hamsci-physics/gnss-vtec.toml --config /etc/hamsci-physics/config.toml`.  Keep `OnFailure=timestd-alert@%N.service`: hamsci-physics already requires hf-timestd (`deploy.toml` `[[deps.git]]`).

`config/gnss-vtec.toml.example` carries hf-timestd's `[gnss_vtec]` block and comments (`config/timestd-config.toml.template:665-687`) with `enabled = true` and `save_csv = false`, plus a commented `[station]` block that says the position falls back to coordination.env and then the grid centre.

In `deploy.toml`, add `"hamsci-physics-vtec.service"` after `"hamsci-physics-fusion.service"`, then the `[[systemd.moves]]` table, with a comment that names decision 10 and says sigmond runs `migrate` before it stops the old unit.

Docs: README and `docs/INDEX.md` gain a "GNSS VTEC" section (the unit, the file, how adoption and `sigmond-site-timing` write it, how to add a USB receiver by hand).  `NASA_EARTHDATA_SETUP.md` says VTEC's DCB download reads `/etc/hamsci-physics/earthdata-netrc` and IONEX now lives in hf-timestd.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/ -q`
Expected: all PASS.

Mutation check: drop the `enabled` test from `adopt_vtec`; `test_disabled_section_writes_nothing` must fail.  Remove the `ConditionPathExists` line; `test_unit_gates_on_the_config_file` must fail.  Restore both.

- [ ] **Step 5: Commit**

```bash
git add systemd/hamsci-physics-vtec.service config/gnss-vtec.toml.example src/hamsci_physics/gnss/adopt.py \
  src/hamsci_physics/cli.py src/hamsci_physics/contract.py deploy.toml README.md docs/INDEX.md \
  docs/NASA_EARTHDATA_SETUP.md tests/test_gnss_adopt.py tests/test_gnss_vtec_unit.py tests/test_client_contract.py
git commit -m "vtec: hamsci-physics-vtec.service, its adoption command and the move sigmond applies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 4: hamsci-physics gives up the fetch timers and two dead health checks

**Files:**
- Delete: `hamsci-physics/systemd/hamsci-physics-ionex-download.{service,timer}`, `hamsci-physics-iri-update.{service,timer}`, `hamsci-physics-iri-healthcheck.{service,timer}`, `hamsci-physics-spaceweather-healthcheck.{service,timer}`
- Delete: `hamsci-physics/scripts/download_ionex_daily.sh`, `ionex_integration.py`, `update-iri-indices.sh`, `iri-indices-healthcheck.sh`, `spaceweather-healthcheck.sh`
- Delete: `hamsci-physics/tests/test_update_iri_indices.py`; modify `tests/test_package_imports.py` (drop `test_ionex_script_reexports_the_canonical_parser`, `:52-67`)
- Modify: `hamsci-physics/deploy.toml` (`[systemd].units`: drop the two timers and the 11-13 comment)
- Test: `hamsci-physics/tests/test_gnss_vtec_unit.py` (add one test)

**Interfaces:**
- Consumes: none.  Task 5 adds the same fetchers to hf-timestd in the same release; the two tasks ship together.

- [ ] **Step 1: Write the failing test**

```python
def test_hamsci_physics_ships_no_timing_inputs():
    names = {p.name for p in (ROOT / "systemd").iterdir()}
    for gone in ("hamsci-physics-ionex-download.timer", "hamsci-physics-iri-update.timer",
                 "hamsci-physics-iri-healthcheck.timer", "hamsci-physics-spaceweather-healthcheck.timer"):
        assert gone not in names, gone
    units = tomllib.loads((ROOT / "deploy.toml").read_text())["systemd"]["units"]
    assert not any("ionex" in u or "iri-" in u for u in units)
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_gnss_vtec_unit.py::test_hamsci_physics_ships_no_timing_inputs -v`
Expected: FAIL on `hamsci-physics-ionex-download.timer`.

- [ ] **Step 3: Delete the files and edit `deploy.toml`**

The health checks go for good, per D18 and D19: `hf-timestd data sources` already reports IONEX age and `apf107.dat` freshness (`hf-timestd/src/hf_timestd/cli.py:1328-1440`), and neither timer ever ran.  Keep `cddis.py` and `cddis_auth.py`: VTEC's DCB download uses them.

- [ ] **Step 4: Run the suite**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/ -q`
Expected: all PASS.  Mutation check: restore one timer file; the new test must fail.

- [ ] **Step 5: Commit**

```bash
git add deploy.toml tests/test_gnss_vtec_unit.py tests/test_package_imports.py
git rm systemd/hamsci-physics-ionex-download.service systemd/hamsci-physics-ionex-download.timer \
  systemd/hamsci-physics-iri-update.service systemd/hamsci-physics-iri-update.timer \
  systemd/hamsci-physics-iri-healthcheck.service systemd/hamsci-physics-iri-healthcheck.timer \
  systemd/hamsci-physics-spaceweather-healthcheck.service systemd/hamsci-physics-spaceweather-healthcheck.timer \
  scripts/download_ionex_daily.sh scripts/ionex_integration.py scripts/update-iri-indices.sh \
  scripts/iri-indices-healthcheck.sh scripts/spaceweather-healthcheck.sh tests/test_update_iri_indices.py
git commit -m "IONEX and IRI fetch move to hf-timestd; the two health checks that never ran go (D16-D19)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 5: hf-timestd fetches its own IONEX and IRI indices

**Files:**
- Create: `hf-timestd/src/hf_timestd/core/earthdata_auth.py` (from `hamsci-physics/src/hamsci_physics/cddis_auth.py`, with `SYSTEM_NETRC = Path('/etc/hf-timestd/earthdata-netrc')` and a header naming the copy's source), plus `adopt_netrc(src: Path, dst: Path) -> str` with Task 3's semantics
- Create: `hf-timestd/scripts/ionex_integration.py` (from hamsci-physics; `from hf_timestd.core.earthdata_auth import get_cddis_session, check_earthdata_credentials`)
- Create: `hf-timestd/scripts/download_ionex_daily.sh` (from hamsci-physics; `VENV_PYTHON=/opt/git/sigmond/hf-timestd/venv/bin/python3`, `SCRIPT_DIR=/opt/git/sigmond/hf-timestd/scripts`, the credential check imports `hf_timestd.core.earthdata_auth`, the message names `/etc/hf-timestd/earthdata-netrc`, the header names `timestd-ionex-download.timer`)
- Create: `hf-timestd/scripts/update-iri-indices.sh` (from hamsci-physics; `UA="hf-timestd/update-iri-indices (https://github.com/HamSCI/hf-timestd)"`, header names `timestd-iri-update.timer`; logic unchanged)
- Create: `hf-timestd/systemd/timestd-ionex-download.{service,timer}`, `timestd-iri-update.{service,timer}` (from the hamsci-physics units; `Documentation=https://github.com/HamSCI/hf-timestd`; `NETRC=/etc/hf-timestd/earthdata-netrc`; `EnvironmentFile=-/etc/hf-timestd/environment`; ExecStart paths under `/opt/git/sigmond/hf-timestd/scripts/`; schedules unchanged: 03:00 and 04:30 UTC, 30 min jitter, `Persistent=true`)
- Modify: `hf-timestd/src/hf_timestd/cli.py` (a `data adopt-netrc` subcommand beside `data sources`, `:2096-2107`)
- Modify: `hf-timestd/deploy.toml` (`[systemd].units` at `:157-191`), `scripts/install.sh` (the unit loop at `:747-757`)
- Modify: `hf-timestd/docs/external-data-sources.md` (`:62-66`)
- Test: `hf-timestd/tests/test_earthdata_auth.py` (from hamsci-physics `tests/test_cddis_auth.py`, renamed imports, plus adopt tests), `tests/test_update_iri_indices.py` (from hamsci-physics, `SCRIPT` re-pointed), `tests/unit/test_ionex_parser_module.py` (gains the re-export test), `tests/test_fetch_units.py`

**Interfaces:**
- Produces: `hf_timestd.core.earthdata_auth` with `find_netrc_path`, `validate_netrc`, `check_earthdata_credentials`, `get_cddis_session`, `adopt_netrc`.  CLI `hf-timestd data adopt-netrc [--from P] [--to P]`, defaults `/etc/hamsci-physics/earthdata-netrc` and `/etc/hf-timestd/earthdata-netrc`; prints `earthdata netrc: <word> (<dst>)`; exit 0, or 1 on `OSError`.
- deploy.toml gains two moves, which Task 9 parses:
  ```toml
  [[systemd.moves]]
  from    = ["hamsci-physics-ionex-download.timer", "hamsci-physics-ionex-download.service"]
  to      = "timestd-ionex-download.timer"
  migrate = ["/opt/git/sigmond/hf-timestd/venv/bin/hf-timestd", "data", "adopt-netrc"]

  [[systemd.moves]]
  from = ["hamsci-physics-iri-update.timer", "hamsci-physics-iri-update.service"]
  to   = "timestd-iri-update.timer"
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fetch_units.py
import tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def _unit(name): return (ROOT / "systemd" / name).read_text()

def test_ionex_unit_reads_hf_timestds_credential_and_script():
    u = _unit("timestd-ionex-download.service")
    assert "Environment=NETRC=/etc/hf-timestd/earthdata-netrc" in u
    assert "ExecStart=/opt/git/sigmond/hf-timestd/scripts/download_ionex_daily.sh" in u
    assert "User=timestd" in u and "hamsci-physics" not in u

def test_iri_unit_runs_as_root_and_writes_only_pharlaps_directory():
    u = _unit("timestd-iri-update.service")
    assert "User=root" in u and "ReadWritePaths=-/opt/pharlap_4.7.4/dat/iri2020" in u
    assert "ExecStart=/opt/git/sigmond/hf-timestd/scripts/update-iri-indices.sh" in u

def test_timers_keep_their_schedules():
    assert "OnCalendar=*-*-* 03:00:00" in _unit("timestd-ionex-download.timer")
    assert "OnCalendar=*-*-* 04:30:00" in _unit("timestd-iri-update.timer")

def test_deploy_lists_the_timers_and_their_moves():
    sd = tomllib.loads((ROOT / "deploy.toml").read_text())["systemd"]
    assert {"timestd-ionex-download.timer", "timestd-iri-update.timer"} <= set(sd["units"])
    assert [m["to"] for m in sd["moves"]] == ["timestd-ionex-download.timer", "timestd-iri-update.timer"]

def test_installer_copies_the_fetch_units():
    s = (ROOT / "scripts" / "install.sh").read_text()
    for f in ("timestd-ionex-download.service", "timestd-ionex-download.timer",
              "timestd-iri-update.service", "timestd-iri-update.timer"):
        assert f in s, f
```

`tests/test_earthdata_auth.py` carries every test of hamsci-physics's `test_cddis_auth.py` against `hf_timestd.core.earthdata_auth`, plus `test_default_netrc_is_hf_timestds` (`SYSTEM_NETRC == Path('/etc/hf-timestd/earthdata-netrc')`) and the three `adopt_netrc` cases from Task 3.

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_fetch_units.py tests/test_earthdata_auth.py tests/test_update_iri_indices.py tests/unit/test_ionex_parser_module.py -v`
Expected: FAIL (missing unit files, missing module).

- [ ] **Step 3: Copy the files and wire them**

`deploy.toml`: add the two timers to `[systemd].units` after `timestd-prune.timer`, each with a one-line comment ("moved from hamsci-physics, v3.96, decision 10"), then the two `[[systemd.moves]]`.  The service profile stays as it is: the timers run on every profile, like `timestd-prune.timer`; two small downloads a day cost nothing.  `install.sh`: add the four unit files to the copy loop at `:747-757` and change the comment above it (`:742-746`) to say the IONEX and IRI units came back in v3.96.  `docs/external-data-sources.md`: the IONEX section names `timestd-ionex-download.timer` and drops the pointer to hamsci-physics's doc; add an IRI-indices section for `timestd-iri-update.timer`.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_fetch_units.py tests/test_earthdata_auth.py tests/test_update_iri_indices.py tests/unit/test_ionex_parser_module.py -v`
Expected: PASS.

Mutation check: point the IONEX unit's `NETRC` at `/etc/hamsci-physics/earthdata-netrc`; `test_ionex_unit_reads_hf_timestds_credential_and_script` must fail.  Remove `timestd-iri-update.timer` from `install.sh`; `test_installer_copies_the_fetch_units` must fail.  Restore both.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/earthdata_auth.py scripts/ionex_integration.py scripts/download_ionex_daily.sh \
  scripts/update-iri-indices.sh systemd/timestd-ionex-download.service systemd/timestd-ionex-download.timer \
  systemd/timestd-iri-update.service systemd/timestd-iri-update.timer src/hf_timestd/cli.py deploy.toml \
  scripts/install.sh docs/external-data-sources.md tests/test_fetch_units.py tests/test_earthdata_auth.py \
  tests/test_update_iri_indices.py tests/unit/test_ionex_parser_module.py
git commit -m "IONEX and IRI-indices fetch return to hf-timestd, which reads them (decision 10, D16-D17)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 6: hf-timestd drops the VTEC code and its dependency on hamsci-physics

**Files:**
- Delete: `hf-timestd/scripts/live_vtec.py`, `scripts/timestd-vtec.service` (the old developer copy), `systemd/timestd-vtec.service`
- Delete: `hf-timestd/src/hf_timestd/core/{ubx_parser,rtcm3_adapter,broadcast_ephemeris,brdc_fetcher,gnss_source,gnss_tec}.py`
- Delete: `hf-timestd/tests/unit/test_ubx_parser.py`, `tests/unit/test_rtcm3_adapter.py`, `tests/test_rtcm3_station_position.py`, `tests/unit/test_gnss_serial_source.py`, `tests/fixtures/gnss/` (moved in Task 1)
- Modify: `hf-timestd/pyproject.toml` (`:32` dependency, `:60-63` the `gnss` extra, `:108` the uv source), `uv.lock` (`uv lock`)
- Modify: `hf-timestd/deploy.toml` (the link step at `:103-106`; the unit at `:169`)
- Modify: `hf-timestd/src/hf_timestd/service_profile.py` (`:34`, `:68-84`, `:102`, `:112`, `:135`, `:149`, `:166-169`, `:214`)
- Modify: `hf-timestd/src/hf_timestd/cli.py` (`RETIRED_SECTIONS` at `:336`)
- Modify: `hf-timestd/tests/unit/test_service_profile.py` (the `vtec` tests at `:68-170, 239-245`)
- Test: `hf-timestd/tests/test_timing_core_stands_alone.py`

**Interfaces:**
- Produces: `ServiceProfile` without the `vtec_available` field; `SERVICE_UNIT_MAP` without `vtec`; `RETIRED_SECTIONS['gnss_vtec']`.  sigmond's `profile_left_off` reads `profile show --json` rows only, so it needs no change.

- [ ] **Step 1: Write the failing guard**

```python
# tests/test_timing_core_stands_alone.py
"""hf-timestd installs and runs with no hamsci-physics on the host (decision 10)."""
import re, tomllib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
IMPORT = re.compile(r"^\s*(?:from\s+hamsci_physics|import\s+hamsci_physics)", re.M)

def test_no_code_imports_hamsci_physics():
    hits = [str(p.relative_to(ROOT)) for base in ("src", "scripts")
            for p in (ROOT / base).rglob("*.py") if IMPORT.search(p.read_text())]
    assert hits == []

def test_pyproject_does_not_depend_on_hamsci_physics():
    d = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert not any(x.startswith("hamsci-physics") for x in d["project"]["dependencies"])
    assert "hamsci-physics" not in d["tool"]["uv"]["sources"]
    assert "gnss" not in d["project"]["optional-dependencies"]

def test_no_unit_runs_hamsci_physics_code():
    for u in (ROOT / "systemd").iterdir():
        assert "/opt/git/sigmond/hamsci-physics" not in u.read_text(), u.name

def test_vtec_left_with_its_unit():
    assert not (ROOT / "systemd" / "timestd-vtec.service").exists()
    assert "timestd-vtec" not in (ROOT / "deploy.toml").read_text()
    from hf_timestd.service_profile import SERVICE_UNIT_MAP
    assert "vtec" not in SERVICE_UNIT_MAP

def test_validate_names_the_moved_section():
    from hf_timestd.cli import retired_section_issues
    msg = retired_section_issues({"gnss_vtec": {"enabled": True}})[0]["message"]
    assert "hamsci-physics" in msg and "/etc/hamsci-physics/gnss-vtec.toml" in msg
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_timing_core_stands_alone.py -v`
Expected: FAIL on `scripts/live_vtec.py` and on the pyproject dependency.

- [ ] **Step 3: Delete, edit, relock**

Delete the files listed.  In `service_profile.py`, remove the `vtec` key, the `vtec_available` field and its gate; rewrite the `'full'` description to `'Fusion + monitoring — the complete timing stack'` and drop the comment that calls VTEC "a natural augmentation".  In `test_service_profile.py`, delete the vtec-gating tests and change `test_vtec_flag_picked_up_from_gnss_vtec_section` into `test_gnss_vtec_section_no_longer_affects_the_profile` (a config with `[gnss_vtec] enabled = true` gives the same `active_services()` as one without).  Add `'gnss_vtec': 'moved to hamsci-physics in v3.96 (decision 10): /etc/hamsci-physics/gnss-vtec.toml holds it now, and `smd align` adopted it there'` to `RETIRED_SECTIONS`.  In `pyproject.toml`, drop the three lines and the comment that names `live_vtec.py`.  Then run `uv lock` and confirm `tests/test_editable_siblings_locked.py` passes.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/ -q`
Expected: PASS, apart from failures present on `main` before this task (record the before list first; the memory note on the suite says it ran green at last count).

Mutation check: put `from hamsci_physics.cddis import CDDISDownloader` into any file under `scripts/`; `test_no_code_imports_hamsci_physics` must fail.  Restore.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock deploy.toml src/hf_timestd/service_profile.py src/hf_timestd/cli.py \
  tests/unit/test_service_profile.py tests/test_timing_core_stands_alone.py
git rm scripts/live_vtec.py scripts/timestd-vtec.service systemd/timestd-vtec.service \
  src/hf_timestd/core/ubx_parser.py src/hf_timestd/core/rtcm3_adapter.py \
  src/hf_timestd/core/broadcast_ephemeris.py src/hf_timestd/core/brdc_fetcher.py \
  src/hf_timestd/core/gnss_source.py src/hf_timestd/core/gnss_tec.py \
  tests/unit/test_ubx_parser.py tests/unit/test_rtcm3_adapter.py tests/test_rtcm3_station_position.py \
  tests/unit/test_gnss_serial_source.py -r tests/fixtures/gnss
git commit -m "VTEC leaves hf-timestd for hamsci-physics; hf-timestd no longer depends on hamsci-physics (decision 10)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 7: hf-timestd's installer, scripts and config forget VTEC

**Files:**
- Modify: `hf-timestd/scripts/install.sh`: the dialout block (`:215-219`), the `--extra gnss` in `uv sync` and its comment (`:558-574`), the unit loop entry (`:753`), the VTEC unit block (`:776-785`), the chrony GNSS server block (`:948-957`, see open question 2), the try-restart list (`:1076-1083`)
- Modify: `hf-timestd/scripts/setup-station.sh` (Section 4 at `:491-510`; `:613-615`; `:794-798`; renumber the later sections)
- Modify: `hf-timestd/scripts/start-services.sh:87`, `scripts/stop-services.sh:28`, `scripts/verify_pipeline.sh:105, 660-668`, `scripts/check-freshness-alert.sh:171-174`
- Delete: `hf-timestd/scripts/health-check-vtec.sh`
- Modify: `hf-timestd/config/timestd-config.toml.template` (`[gnss_vtec]` at `:665-687`; `# vtec = false` at `:485`)
- Modify: `hf-timestd/CLAUDE.md` (the overview sentence on science products; the `uv sync` line), `README.md` (`:22, 52-54, 253-254, 272`), `docs/INDEX.md:51`, `docs/GNSS_TIMESERVER_AND_VTEC.md` (keep the NTP and PPS half; the VTEC half becomes a short pointer to hamsci-physics)
- Test: `hf-timestd/tests/test_timing_core_stands_alone.py` (add two tests)

**Interfaces:**
- Consumes: Task 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_installer_and_scripts_forget_vtec():
    for rel in ("scripts/install.sh", "scripts/setup-station.sh", "scripts/start-services.sh",
                "scripts/stop-services.sh", "scripts/verify_pipeline.sh",
                "scripts/check-freshness-alert.sh"):
        text = (ROOT / rel).read_text()
        assert "timestd-vtec" not in text and "gnss_vtec" not in text, rel
    assert "--extra gnss" not in (ROOT / "scripts/install.sh").read_text()

def test_template_has_no_gnss_vtec_section():
    import tomllib
    t = (ROOT / "config/timestd-config.toml.template").read_text()
    assert "[gnss_vtec]" not in t and "vtec" not in t.split("[services]")[1].split("[timing.t6_pps]")[0]
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_timing_core_stands_alone.py -v`
Expected: the two new tests FAIL on `scripts/install.sh`.

- [ ] **Step 3: Edit**

`install.sh` keeps `--extra lz4 --extra iono`.  Its dialout block goes: the hamsci-physics unit grants the group itself (`SupplementaryGroups=dialout`).  The chrony block goes, pending open question 2; the `VTEC_ENABLED` variable it read goes with the VTEC unit block.  The template's `[storage]` comment stays.  `GNSS_TIMESERVER_AND_VTEC.md` keeps its title only if the NTP half still earns it; otherwise rename it `GNSS_TIMESERVER.md` and fix `docs/INDEX.md`.

- [ ] **Step 4: Run the suite and a shell check**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/ -q && bash -n scripts/install.sh scripts/setup-station.sh`
Expected: PASS, and `bash -n` silent.

Mutation check: put `timestd-vtec` back into `start-services.sh`; `test_installer_and_scripts_forget_vtec` must fail.  Restore.

- [ ] **Step 5: Commit**

```bash
git add scripts/install.sh scripts/setup-station.sh scripts/start-services.sh scripts/stop-services.sh \
  scripts/verify_pipeline.sh scripts/check-freshness-alert.sh config/timestd-config.toml.template \
  CLAUDE.md README.md docs/INDEX.md docs/GNSS_TIMESERVER_AND_VTEC.md tests/test_timing_core_stands_alone.py
git rm scripts/health-check-vtec.sh
git commit -m "install, setup and service scripts: VTEC belongs to hamsci-physics now

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 8: fusion stops reading the VTEC table

**Files:**
- Modify: `hf-timestd/src/hf_timestd/core/multi_broadcast_fusion.py`: the block headed `GNSS VTEC INTEGRATION (Real-Time Physics Correction)` (today `:3419-3504`); `_read_gnss_vtec` (today `:4291-4378`); the two `last_vtec_tecu` branches (today `:3043-3048` and `:3736-3742`), which read an attribute nothing ever sets; the closing sentence of the `TEC ESTIMATION — REMOVED` comment (today `:3511-3512`) that still calls GNSS VTEC "the only ionospheric delay correction applied here"
- Delete: `hf-timestd/tests/test_fusion_gnss_vtec_rtp_gate.py`
- Modify: `hf-timestd/tests/test_fusion_no_inloop_tec.py` (`:38-42`), `tests/test_t3_sign_convention.py:141`, `tests/test_fusion_feed_each_row_once.py:80`

Find each piece by its text, not its line: Plan D edits the same file (open question 6).

**Interfaces:**
- Consumes: none.  `physics_model` (`HFPropagationModel`) stays: fusion's mode scoring still uses it (`:2345-2370`).

- [ ] **Step 1: Write the failing test**

In `tests/test_fusion_no_inloop_tec.py`, replace `test_fusion_keeps_the_sanctioned_seams` with:

```python
def test_fusion_reads_no_science_product():
    """Decision 10: GNSS VTEC belongs to hamsci-physics; the timing core reads none of its output."""
    code = "\n".join(line for _, line in _code_lines())
    assert "gnss_vtec" not in code and "last_vtec_tecu" not in code
    assert "HFPropagationModel" in code      # the propagation seam stays
```

- [ ] **Step 2: Run it to see it fail**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_fusion_no_inloop_tec.py -v`
Expected: FAIL (`gnss_vtec` found).

- [ ] **Step 3: Delete the code and the two monkeypatch lines**

After deletion, `iono_factor` and `iono_scale_factor` stay at 1.0, which is what they always held.

- [ ] **Step 4: Run the fusion tests**

Run: `.venv/bin/pytest --basetemp=/var/tmp/mjh-pt/v396e -o addopts="" tests/test_fusion_no_inloop_tec.py tests/test_t3_sign_convention.py tests/test_fusion_feed_each_row_once.py tests/unit/ -q`
Expected: PASS.  Mutation check: restore `_read_gnss_vtec`; the new test must fail.

- [ ] **Step 5: Commit**

```bash
git add src/hf_timestd/core/multi_broadcast_fusion.py tests/test_fusion_no_inloop_tec.py \
  tests/test_t3_sign_convention.py tests/test_fusion_feed_each_row_once.py
git rm tests/test_fusion_gnss_vtec_rtp_gate.py
git commit -m "fusion: drop the GNSS VTEC cross-check; its result reached only a DEBUG line (C5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 9: sigmond learns `[[systemd.moves]]`

**Files:**
- Create: `sigmond/lib/sigmond/unit_moves.py`
- Test: `sigmond/tests/test_unit_moves.py`

**Interfaces:**
- Consumes: `sigmond.align_apply.Step(component, outcome, detail)` (`lib/sigmond/align_apply.py:451`).
- Produces:
  - `@dataclass(frozen=True) class UnitMove: component: str; from_units: tuple; to_unit: str; migrate: tuple = ()`
  - `parse_moves(component: str, deploy: dict) -> list` — reads `deploy["systemd"]["moves"]`; `from` may be a string or a list; raises `ValueError` naming the component when `from` or `to` is missing, `to` sits in `from`, or `to` is not in the same component's `[systemd].units`.
  - `declared_moves(base: Path, names) -> list` — from each `<base>/<name>/deploy.toml`; an unreadable file yields none.
  - `declared_moves_at(repo: Path, component: str, commit: str, run=subprocess.run) -> list` — the same, from `git -C repo show <commit>:deploy.toml`.
  - `unit_state(unit: str, *, unit_dir: Path, run) -> tuple` — `(enabled, active)`.  `enabled` is true when `systemctl is-enabled` prints a word starting `enabled`, or when any `<unit_dir>/*.wants/<unit>` link exists (a unit whose file vanished still has its wants-link).  `active` is `systemctl is-active` printing `active`, `activating` or `reloading`.
  - `apply_move(move: UnitMove, *, apply: bool, owner_enabled: bool, unit_dir: Path, backup_dir: Path, run, now: datetime) -> Step`

`apply_move` decides in this order:
1. No old unit enabled or active → `Step(label, "unchanged", "")`, where `label = f"{move.component}: {', '.join(move.from_units)} -> {move.to_unit}"`.
2. `owner_enabled` false → `Step(label, "ran", f"held: topology disables {move.component}; {from} keeps running")`.  Nothing changes.
3. `apply` false → `Step(label, "would move", ...)`.
4. Run `migrate` (when given).  A non-zero exit → `Step(label, "failed", "<argv> exit N; <from> left running")`.  Nothing stopped.
5. For each old unit: `systemctl stop` when active; `systemctl disable` when enabled; then, whatever `disable` returned, remove each `<unit_dir>/*.wants/<unit>` link and the `<unit_dir>/<unit>` entry when it is a symlink (dangling, or into `/opt/git/sigmond/`), or move a regular file to `<backup_dir>/<unit>.<UTC stamp>`.  Then `systemctl daemon-reload`.
6. `systemctl enable <to>`; when any old unit was active, `systemctl start <to>`.  Then read `systemctl show -p ActiveState,ConditionResult --value <to>`: `active` → `"moved"`; `inactive` with `ConditionResult=no` → `"moved"`, detail `"<to> waits for its condition"`; anything else → `"failed"`, detail naming both units, because the old unit's code may be gone and nothing runs now.

`run(argv, capture_output=True, text=True, timeout=...)` follows `subprocess.run`; tests inject a fake.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unit_moves.py
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sigmond import unit_moves as um

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)
MOVE = um.UnitMove("hamsci-physics", ("timestd-vtec.service",), "hamsci-physics-vtec.service",
                   ("/x/hamsci-physics", "vtec", "adopt"))

class FakeSystemd:
    """Answers systemctl from a dict of states and records every argv."""
    def __init__(self, states, migrate_rc=0, after=("active", "yes")):
        self.states, self.calls, self.migrate_rc, self.after = states, [], migrate_rc, after
    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out, rc = "", 0
        if argv[0] != "systemctl":
            rc = self.migrate_rc
        elif argv[1] == "is-enabled":
            out = self.states.get(argv[2], ("", ""))[0]
        elif argv[1] == "is-active":
            out = self.states.get(argv[2], ("", "inactive"))[1]
        elif argv[1] == "show":
            out = "\n".join(self.after)
        return subprocess.CompletedProcess(argv, rc, out + "\n", "")

def _apply(fake, tmp_path, **kw):
    d = dict(apply=True, owner_enabled=True, unit_dir=tmp_path, backup_dir=tmp_path / "bak",
             run=fake, now=NOW)
    d.update(kw)
    return um.apply_move(MOVE, **d)

def test_stop_comes_before_start(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")})
    step = _apply(fake, tmp_path)
    flat = [" ".join(c) for c in fake.calls]
    assert step.outcome == "moved"
    assert flat.index("systemctl stop timestd-vtec.service") < flat.index("systemctl start hamsci-physics-vtec.service")
    assert flat.index("/x/hamsci-physics vtec adopt") < flat.index("systemctl stop timestd-vtec.service")

def test_failed_migrate_keeps_the_old_unit(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")}, migrate_rc=1)
    step = _apply(fake, tmp_path)
    assert step.outcome == "failed"
    assert not any(c[:2] == ["systemctl", "stop"] for c in fake.calls)

def test_second_run_is_unchanged(tmp_path):
    fake = FakeSystemd({})
    assert _apply(fake, tmp_path).outcome == "unchanged"
    assert not any(c[0] != "systemctl" or c[1] not in ("is-enabled", "is-active") for c in fake.calls)

def test_disabled_owner_holds_the_move(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")})
    step = _apply(fake, tmp_path, owner_enabled=False)
    assert step.outcome == "ran" and step.detail.startswith("held:")
    assert not any(c[:2] in (["systemctl", "stop"], ["systemctl", "disable"]) for c in fake.calls)

def test_dangling_old_unit_is_cleared(tmp_path):
    wants = tmp_path / "multi-user.target.wants"; wants.mkdir()
    (wants / "timestd-vtec.service").symlink_to(tmp_path / "timestd-vtec.service")
    (tmp_path / "timestd-vtec.service").symlink_to("/opt/git/sigmond/hf-timestd/systemd/timestd-vtec.service")
    fake = FakeSystemd({"timestd-vtec.service": ("", "active")})   # is-enabled prints nothing
    assert _apply(fake, tmp_path).outcome == "moved"
    assert not (wants / "timestd-vtec.service").is_symlink()
    assert not (tmp_path / "timestd-vtec.service").is_symlink()

def test_condition_skip_counts_as_moved(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")}, after=("inactive", "no"))
    step = _apply(fake, tmp_path)
    assert step.outcome == "moved" and "waits for its condition" in step.detail

def test_new_unit_that_fails_fails_the_move(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")}, after=("failed", "yes"))
    assert _apply(fake, tmp_path).outcome == "failed"

def test_dry_run_changes_nothing(tmp_path):
    fake = FakeSystemd({"timestd-vtec.service": ("enabled", "active")})
    assert _apply(fake, tmp_path, apply=False).outcome == "would move"
    assert all(c[1] in ("is-enabled", "is-active") for c in fake.calls)

def test_parse_refuses_a_target_the_component_does_not_declare():
    deploy = {"systemd": {"units": ["a.service"], "moves": [{"from": "old.service", "to": "b.service"}]}}
    with pytest.raises(ValueError, match="x"):
        um.parse_moves("x", deploy)

def test_parse_accepts_a_string_from():
    deploy = {"systemd": {"units": ["b.timer"], "moves": [{"from": "old.timer", "to": "b.timer"}]}}
    assert um.parse_moves("x", deploy) == [um.UnitMove("x", ("old.timer",), "b.timer", ())]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd /home/mjh/hamsci/repos/sigmond && .venv/bin/pytest tests/test_unit_moves.py -v`
Expected: FAIL with `ImportError: cannot import name 'unit_moves'`.

- [ ] **Step 3: Write `unit_moves.py`**

Stdlib only.  The module docstring explains the order (migrate, stop, disable, clear, enable, start) and why: a serial receiver tolerates one reader, and a failed migrate must leave the old unit running.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_unit_moves.py -v`
Expected: PASS.  Mutation check: swap the stop and the start in `apply_move`; `test_stop_comes_before_start` must fail.  Drop the wants-link sweep; `test_dangling_old_unit_is_cleared` must fail.  Restore both.

- [ ] **Step 5: Commit**

```bash
git add lib/sigmond/unit_moves.py tests/test_unit_moves.py
git commit -m "unit_moves: a component may declare a unit it took over; sigmond moves it without running two copies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 10: align applies the moves, and drift reports a half-done one

**Files:**
- Modify: `sigmond/bin/smd`: a new `_align_unit_moves(apply: bool, *, base, plan=None, run=subprocess.run, say=print) -> list` near `_align_enable_declared_units` (`:9715`); called in `_align_apply` immediately before `bringup_steps = _align_bringup(...)` (`:10722`), its steps added to the run's steps; called with `apply=False` in `cmd_align` before `_align_enable_declared_units(False, ...)` (`:11293`)
- Modify: `sigmond/bin/smd` `_align_print_steps` (`:10576-10590`): count `would move` and `unchanged` as non-blocking lines; leave the summary words as they are
- Modify: `sigmond/lib/sigmond/unit_moves.py`: `drift_items(base: Path, enabled: Mapping[str, bool], *, unit_dir: Path, is_enabled, service_active) -> list`
- Modify: `sigmond/lib/sigmond/drift/build_report.py` (beside the unit collector at `:169-175`)
- Modify: `sigmond/docs/CLIENT-CONTRACT.md` §5 (`:443` onward), `CHANGELOG.md` (v3.96)
- Test: `sigmond/tests/test_unit_moves.py` (add), `tests/test_align_unit_moves.py`

**Interfaces:**
- Consumes: Task 9.
- Produces: `drift_items` returns, per declared move whose old unit is enabled or active: `Item("unit", "fault", <old unit>, f"{old} still runs; {component} took it over as {to}", "smd align --apply")` when topology enables the component, else `Item("unit", "fault", <old unit>, f"{old} moved to {to} in {component}, which topology disables here", f"smd enable {component}")`.

`_align_unit_moves` reads moves for every component topology enables (`drift_report.topology_enabled()`), plus every component that declares a move whose old unit is in effect, so a disabled owner still gets its "held" line.  In the dry run, a component the plan moves (status `move`) is read at its target commit through `declared_moves_at`; every other component from its working tree.  On apply, after the checkouts moved, it reads working trees.  A `failed` move counts as blocking (`_ALIGN_BLOCKING`, `:10571`), so restarts wait, as they do for any failed step.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_align_unit_moves.py (shape; reuse test_align.py's fixtures for a fake base and topology)
def test_dry_run_lists_a_move_declared_at_the_target_commit(fake_base, capsys): ...
    # hamsci-physics's working tree declares no move; at the plan's target commit it does.
    # Expect the line "  would move timestd-vtec.service -> hamsci-physics-vtec.service (hamsci-physics)".

def test_apply_runs_moves_before_bringup(monkeypatch, fake_base):
    order = []
    monkeypatch.setattr(smd, "_align_unit_moves", lambda *a, **k: order.append("moves") or [])
    monkeypatch.setattr(smd, "_align_bringup", lambda *a, **k: order.append("bringup") or [])
    # drive _align_apply through the existing test harness for a clean plan
    assert order.index("moves") < order.index("bringup")
```

In `tests/test_unit_moves.py`, add `test_drift_faults_a_held_move` (topology disables hamsci-physics, `timestd-vtec.service` active → one fault whose fix reads `smd enable hamsci-physics`) and `test_drift_quiet_after_the_move` (old unit neither enabled nor active → no item).

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_align_unit_moves.py tests/test_unit_moves.py -v`
Expected: FAIL (`_align_unit_moves` and `drift_items` missing).

- [ ] **Step 3: Wire it in; document the key**

CLIENT-CONTRACT §5 gains a `[[systemd.moves]]` subsection: who declares it (the new owner), the three keys, the order sigmond follows, the held case, and that an entry stays until a release removes it on purpose.  CHANGELOG v3.96 gets one paragraph naming the three moves.

- [ ] **Step 4: Run the sigmond suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.  Mutation check: call `_align_unit_moves` after `_align_bringup`; `test_apply_runs_moves_before_bringup` must fail.  Restore.

- [ ] **Step 5: Commit**

```bash
git add bin/smd lib/sigmond/unit_moves.py lib/sigmond/drift/build_report.py docs/CLIENT-CONTRACT.md \
  CHANGELOG.md tests/test_align_unit_moves.py tests/test_unit_moves.py
git commit -m "align moves declared units before bring-up; drift faults an old unit that still runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 11: site-timing, CPU placement and operator docs follow the new unit

**Files:**
- Modify: `sigmond/bin/sigmond-site-timing` step 4 (`:576-649`)
- Modify: `sigmond/lib/sigmond/cpu.py` (`AFFINITY_UNITS`, after `'timestd-vtec.service'` at `:80`)
- Modify: `sigmond/docs/operator/glossary.md:73`, `docs/operator/day-2.md:162, 201`
- Test: `sigmond/tests/test_site_timing_lan_trust.py` (the GNSS tests at `:272-306` and the `env` fixture at `:131-146`); `tests/test_snapshot.py` style affinity test in `tests/test_unit_moves.py`

**Interfaces:**
- Consumes: Task 3's file path and example.

Step 4's new rules: `GCFG=${GNSS_VTEC_TOML:-/etc/hamsci-physics/gnss-vtec.toml}`.  "Unwired" now means `GCFG` absent (in place of the `<YOUR_GNSS_HOST>` placeholder in hf-timestd's config).  With a trusted GNSS host and no `GCFG`, it writes `GCFG` (mode 0644) with `[gnss_vtec]`, `enabled = true  # wired by sigmond-site-timing from lan-trust.toml`, `host`, `port = 9000`, `protocol = "auto"`, `ephemeris_dir = "data/brdc"`, `save_csv = false`, and then runs `systemctl restart hamsci-physics-vtec` (restart starts a stopped unit; try-restart would not).  When `/opt/git/sigmond/hamsci-physics` is absent, it writes nothing and says `GNSS host trusted, but hamsci-physics is not installed - VTEC stays off`.  The "set before the trust record" note reads the host from `GCFG`.  Offers and scanning stay as they are.

- [ ] **Step 1: Change the tests first**

In the fixture, add `'GCFG': tmp_path / 'gnss-vtec.toml'` and `'PHYS': tmp_path / 'hamsci-physics'` (created), and have `run_step` substitute both paths into the step body.  Then:

```python
def test_trusted_gnss_host_is_wired(env):
    trust(env, '--trust-gnss', '192.168.8.50')
    said = run_step(env, STEP4, subnet='')
    text = env['GCFG'].read_text()
    assert 'host = "192.168.8.50"' in text and 'enabled = true' in text
    assert 'restart hamsci-physics-vtec' in (env['T'] / 'systemctl.log').read_text().splitlines()
    assert 'trusted GNSS stream wired: 192.168.8.50' in said

def test_gnss_stream_is_offered_not_wired(env):        # GCFG absent, a stream on the LAN
    ... ; assert not env['GCFG'].exists()

def test_existing_gnss_host_stays_and_is_noted_once(env):
    cfg = '[gnss_vtec]\nenabled = true\nhost = "192.168.8.60"\nport = 9000\n'
    env['GCFG'].write_text(cfg)
    ...; assert env['GCFG'].read_text() == cfg

def test_no_hamsci_physics_no_file(env):
    env['PHYS'].rmdir(); trust(env, '--trust-gnss', '192.168.8.50')
    said = run_step(env, STEP4, subnet='')
    assert not env['GCFG'].exists() and 'hamsci-physics is not installed' in said
```

Add to `tests/test_unit_moves.py`:

```python
def test_new_units_stay_off_radiods_cores():
    from sigmond.cpu import AFFINITY_UNITS
    for u in ("hamsci-physics-vtec.service", "timestd-ionex-download.service", "timestd-iri-update.service"):
        assert AFFINITY_UNITS.get(u) == "other", u
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/bin/pytest tests/test_site_timing_lan_trust.py tests/test_unit_moves.py -v`
Expected: the GNSS tests and the affinity test FAIL.

- [ ] **Step 3: Edit step 4, `cpu.py` and the docs**

`AFFINITY_UNITS` gains the three entries under the hf-timestd/grape group comment.  The glossary and day-2 rows name `hamsci-physics-vtec.service` and say it reads `inactive` (condition not met) wherever no `/etc/hamsci-physics/gnss-vtec.toml` exists, which is the normal state.

- [ ] **Step 4: Run the suite and a shell check**

Run: `.venv/bin/pytest -q && bash -n bin/sigmond-site-timing`
Expected: PASS; `bash -n` silent.  Mutation check: change `restart` back to `try-restart`; `test_trusted_gnss_host_is_wired` must fail.  Restore.

- [ ] **Step 5: Commit**

```bash
git add bin/sigmond-site-timing lib/sigmond/cpu.py docs/operator/glossary.md docs/operator/day-2.md \
  tests/test_site_timing_lan_trust.py tests/test_unit_moves.py
git commit -m "site-timing wires a trusted GNSS stream into hamsci-physics; new units stay off radiod's cores

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

---

### Task 12: measure B4 before and after; check the two ledger items

> **Added 2026-11-26 (Task 8 review):** removing the VTEC cross-check changes fusion's published sigma on the VTEC stations (ND, B4, SIGMA, N8UR): the old `+GNSS_VALIDATED` tag made the uncertainty lookup fall to a flat 1.0 ms; now each mode gets its table value, about −0.55 to +1.89 ms change (×5 single-station).  It reaches the Offset Judge's T3 bench and the AuthorityManager's T3 sigma, not chrony and not D_clock.  Add to this task's before/after counters on B4: fusion_status.json `uncertainty_ms` and `propagation_uncertainty_ms`, `quality_grade`, and authority.json's T3 `sigma_ns` and adopted tier.

**Files:** none in the repos.  The operator stages the reads in `~/hf-measure-v396e.sh` on the devbox and runs it; every remote command reads only.

**Before anything on B4:** read the claude-bus and `/etc/sigmond/CLAUDE-COORDINATION.md` on B4, and post on the bus that the v3.96 canary will move `timestd-vtec` and the two fetch timers and add three CPU-affinity entries.  Record the affinity entries in the coordination doc when the canary lands.  Open a ledger item (`v396-timing-core-alone`) the same hour, and set `nd-vtec-zero-division` and `vtec-local-usb-source` to point at it.

- [ ] **Step 1: Readiness, read-only, on the stations that run VTEC**

For each of `b4`, `ac0g-nd`, `n8ur`:

```bash
~/hamsci/ops/bin/fleet-ssh <station> 'systemctl is-enabled timestd-vtec.service; systemctl is-active timestd-vtec.service; smd topology show 2>/dev/null | grep -i -E "hamsci-physics|grape"; sudo -n test -f /etc/hf-timestd/earthdata-netrc && echo netrc-hf; sudo -n test -f /etc/hamsci-physics/earthdata-netrc && echo netrc-phys'
```

Expected: hamsci-physics enabled wherever `timestd-vtec` runs.  A station where it does not gets `smd enable hamsci-physics` through the normal path before its roll, or Michael decides it keeps VTEC off.  (Use `smd topology` with whatever read-only verb the station's smd offers; the line only needs hamsci-physics's enabled state.)

On B4 also read why the IRI update failed on 11-23: `fleet-ssh b4 'journalctl -u hamsci-physics-iri-update --since -10d -o cat | tail -30'`.

- [ ] **Step 2: Before: start the 24-hour counters on B4, at least 24 hours before the canary**

Take the snapshot at T0 and again at T0+24 h.  Each run:

```bash
~/hamsci/ops/bin/fleet-ssh b4 'date -u +%FT%TZ; for u in timestd-vtec.service hamsci-physics-vtec.service hamsci-physics-ionex-download.service hamsci-physics-iri-update.service timestd-ionex-download.service timestd-iri-update.service timestd-fusion.service hamsci-physics-fusion.service; do echo "$u $(systemctl show -p NRestarts,MemoryCurrent,MemoryPeak,CPUUsageNSec,ActiveState,Result --value $u | tr "\n" " ")"; done'
~/hamsci/ops/bin/fleet-ssh b4 'systemctl list-timers --all --no-pager | grep -E "ionex|iri"'
~/hamsci/ops/bin/fleet-ssh b4 'for u in timestd-vtec hamsci-physics-vtec timestd-fusion; do echo "$u $(journalctl -u $u --since -24h -o cat | wc -l)"; done'
~/hamsci/ops/bin/fleet-ssh b4 'ps -eo args | grep -c -E "[l]ive_vtec|[v]tec_service"'
~/hamsci/ops/bin/fleet-ssh b4 '/usr/local/bin/hf-timestd data sources --json'
```

VTEC rows per hour and the longest gap, over the last 24 hours, through the `(channel, timestamp_utc)` index:

```bash
~/hamsci/ops/bin/fleet-ssh b4 'sudo -n -u timestd python3 -c "
import sqlite3, datetime as d
c = sqlite3.connect(\"file:/var/lib/timestd/phase2/timestd.db?mode=ro\", uri=True)
t0 = (d.datetime.now(d.timezone.utc) - d.timedelta(hours=24)).strftime(\"%Y-%m-%dT%H:%M:%S\")
ts = [r[0] for r in c.execute(\"SELECT unix_timestamp FROM L3_gnss_vtec WHERE channel=? AND timestamp_utc>=? ORDER BY timestamp_utc\", (\"GNSS\", t0))]
print(\"rows\", len(ts), \"max_gap_s\", max((b - a for a, b in zip(ts, ts[1:])), default=None))
"'
```

Record each output under the ledger item.  (If `fleet-ssh`'s quoting across the two hops mangles a command, stage it as a script on B4 by the normal path; see `reference_b4_v3_access`.)

- [ ] **Step 3: The canary**

Michael applies the v3.96 candidate to B4 with the release ladder's canary script (`smd align --manifest <candidate> --apply`).  Read the align log for three lines: `moved` for `timestd-vtec.service -> hamsci-physics-vtec.service`, and for each fetch timer.  Then at once:

```bash
~/hamsci/ops/bin/fleet-ssh b4 'systemctl is-enabled timestd-vtec.service hamsci-physics-vtec.service timestd-ionex-download.timer timestd-iri-update.timer hamsci-physics-ionex-download.timer hamsci-physics-iri-update.timer; ps -eo args | grep -c -E "[l]ive_vtec|[v]tec_service"; sudo -n head -3 /etc/hamsci-physics/gnss-vtec.toml; smd drift 2>/dev/null | grep -i -E "vtec|ionex|iri"'
```

Expected: the old names read disabled or not found; the new ones enabled; exactly one VTEC process; the adopted file present; no drift line about the moved units.

- [ ] **Step 4: After: the same counters, 24 hours on**

Repeat Step 2's commands at canary+24 h.  Pass when all of these hold:
- one VTEC process, `hamsci-physics-vtec.service` `NRestarts=0`;
- VTEC rows in the 24 h within 5 % of the before count, and the longest gap no more than 120 s outside the canary's minute;
- `MemoryPeak` of `hamsci-physics-vtec` no more than 10 % above `timestd-vtec`'s before figure;
- VTEC journal lines no more than before (fewer, if B4 ran with `save_csv = false`);
- `timestd-ionex-download.service` and `timestd-iri-update.service` each ran once with `Result=success` (IONEX exits 0 with "skipping" when no credential exists, which B4's empty `/var/lib/timestd/ionex` suggests);
- `hf-timestd data sources --json` reports the same IONEX and `apf107` ages as before, or younger.

Then the SIGMA canary, per the ladder, before the bless.

- [ ] **Step 5: After the fleet roll: the two ledger checks**

`nd-vtec-zero-division`: a day after ND's roll, `fleet-ssh ac0g-nd 'systemctl show -p NRestarts,ActiveState --value hamsci-physics-vtec.service; journalctl -u hamsci-physics-vtec --since -24h -o cat | grep -c -E "ZeroDivision|Traceback"'` reads `0`, `active`, `0`.  Close the item when it does.

`vtec-local-usb-source`: on N8UR, `fleet-ssh n8ur 'journalctl -u hamsci-physics-vtec --since -1h -o cat | grep -m3 "VTEC:"'` shows VTEC lines from the USB F9T.  The item stays open for sensing (follow-up), with its `road` moved to `sensed-bringup`.

---

## Self-review

1. **Spec coverage.**  Decision 10's two halves: VTEC out (Tasks 1-3, 6-8), fetch timers in (Tasks 4-5).  "Which timers and units move each way": the table above and File Structure.  "Who reads each output": the readers table.  "How a station upgrades without losing a day or running both": the upgrade section, Tasks 9-10, Review Focus 1-4, Task 12's gap and process counts.  The two ledger items: Task 12 Step 5, and "What the code says today".  The audit's C5, C24, D16-D19 verdicts: Task 8 (C5 cut), Task 2 (C24 stdout), Task 4 (D18, D19 cut).
2. **Step scan.**  Every code step names its file and signature; the two algorithms the signatures leave open (`regrid` and `apply_move`'s order) carry their rules.  Task 10's align tests name the harness to reuse rather than repeat it.
3. **Type consistency.**  `adopt_netrc(src, dst) -> str` with the words `kept`, `absent`, `copied` in both repos; `UnitMove(component, from_units, to_unit, migrate)` in Tasks 9 and 10; the deploy key `moves` with `from`, `to`, `migrate` in Tasks 3, 5 and 9; `GNSS_VTEC_TOML` in Tasks 3 and 11.
4. **Review Focus.**  Each of the five lines has a named test in its owning task.
5. **Proportion.**  The plan carries test code and rules; it copies no module body.  The moved modules travel as copies, not rewrites.
