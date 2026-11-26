# v3.96 Plan D: metrology and fusion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the timing authority its own small unit, make fusion a once-a-minute witness that knows where the station stands, and run one metrology process per station in place of six.

**Architecture:** Three stages, each one shipped to B4 and measured for 24 hours before the next one merges.  Stage 1 moves the AuthorityManager out of `timestd-fusion` into `timestd-authority-manager.service` and changes nothing else.  Stage 2 hands fusion the station's position (or refuses to start) and slows it to one cycle a minute.  Stage 3 replaces the six `timestd-metrology@<channel>` processes with one `timestd-metrology-station.service` that runs each channel in its own thread.

**Tech Stack:** Python 3.11+, pytest, systemd units, bash (hf-timestd scripts, sigmond `bin/` scripts), sigmond's stdlib-only core.

**Spec:** `/home/mjh/hamsci/tasks/hf-timestd-audit/ASSESSMENT.md` (Michael's decisions 4 and 5, fault 6), with the detail in `part-B-metrology.md` (B1, B5, B25, B26, B27) and `part-C-fusion-watchdogs.md` (C1, C2, C4, C9, C10, C25).  The timing rule comes from `docs/design/MEASUREMENT_MODEL.md` ("one ruler, one registration").

---

## What fits v3.96, and what waits

All three decisions fit v3.96 in a reduced form.  The reduced form keeps every timing behaviour as it stands and changes only where the work runs.

**v3.96 (this plan):**
- Stage 1: the AuthorityManager in its own unit (Tasks 1-4).  Small.  It goes first and alone.
- Stage 2: fusion's position and its once-a-minute cadence (Tasks 5-7).  Small.
- Stage 3: one metrology process per station (Tasks 8-11).  The largest change in this plan.  It ships last, and only after Stages 1 and 2 have each held for 24 hours on B4.  If the Stage 3 canary fails its gate, Stage 3 moves to v3.97 and v3.96 ships with Stages 1 and 2.
- Task 12: the B4 measurement, run before Stage 1 and after each stage.

**Later (v3.97 or after), with the reason:**
- One station-wide delay prediction shared by all channels (B7).  It changes `MetrologyEngine` and hamsci-dsp's `ArrivalPatternMatrix`, and each channel's window learning (B9) would then share state.  That needs its own design.
- One writer for the station registration summary, as one answer per transmitter site (B5).  That belongs to the T3 restoration design (`docs/superpowers/specs/2026-10-30-t3-restoration-design.md`), not to a process move.  In v3.96 the channel threads still write the summary as the six processes did, but a station-wide lock now makes each check-and-write atomic.
- Dropping fusion's L1 pass and second Kalman bank (C2, C3) and rebuilding the ADEV tracker (C6).  station-web shows `d_clock_l1_ms` and `l1_l2_difference_ms` (`station-web/src/station_web/services/fusion_service.py:123-125`), so that cut needs a station-web change in the same release.
- Fusion reading only new rows (C2).
- The metrology unit's `Requires=timestd-core-recorder.service` cascade (each recorder restart restarts metrology).  It stays as it is today.
- hamsci-physics-fusion's Columbia, Missouri default (fault 6, second half).  It lives in hamsci-physics; whichever v3.96 plan owns hamsci-physics should take it, using the same rule as Task 6.

**Estimate:** 12 tasks.  Seven of them change what runs on stations: Tasks 3, 4, 6, 7, 10, 11 change units or restart services, and Task 9 adds the new metrology host.  Tasks 1, 2, 5 and 8 add library code only.  Task 12 reads B4 and changes nothing.

## Open questions for Michael

**Answered 2026-11-25: "As recommended"** (Michael, to V396-PLAN-REVIEW.md questions 19-23, and 29-30).  Each recommendation below now binds.  Stage 1 deploys to the fleet outside any T3 study window.

1. **Should the authority run on `rtp`-profile stations?**  Today the `rtp` profile leaves `timestd-fusion` off (`src/hf_timestd/service_profile.py:58-63`), so an `rtp` station publishes no `authority.json` at all.  sigmond sets `full` (`sigmond/bin/sigmond-site-timing:1042-1043`), so few stations, perhaps none, run `rtp`.  **Recommendation:** put `authority_manager` in the `rtp`, `fusion` and `full` profiles, not `archive`.  Task 3 does this.
2. **When a station has no precise latitude and longitude, may fusion use its grid square's centre?**  sigmond writes latitude and longitude only from a GPS fix or an explicit `smd config identity`; otherwise it writes `grid_square` alone (`sigmond/lib/sigmond/site_profile.py:603-614`).  A 6-character grid centre lies within about 5 km of the antenna, a few microseconds of path at HF.  **Recommendation:** yes.  Precise position first, then the grid centre, and refuse only when neither exists.  This matches "GPSDO location, else typed grid".
3. **Should the metrology host refuse to start without a position too?**  Today a channel falls back to its grid, and with neither it runs at a meaningless `XX00xx`.  **Recommendation:** yes, the same rule and the same refusal as fusion.  Task 9 does this.
4. **May the T3 admission state reset once when the authority moves?**  The `T3AdmissionPolicy` keeps its window in the AuthorityManager's memory.  Every fusion restart resets it today; the move resets it once more, and afterwards fusion restarts stop resetting it.  On a station where T3 stands admitted in `auto` mode, T3 may drop out for one admission window after the deploy.  **Recommendation:** accept it, and deploy Stage 1 to the fleet outside any T3 study window (`t3-study-v2` runs now).
5. **Should fusion keep `Requires=timestd-l2-calibration.service`?**  Once the authority leaves, that coupling costs only the witness.  **Recommendation:** keep it in v3.96; revisit with the L1-pass cut.

## What the code says beside the audit

These either correct the audit or add to it.  Each one shapes a task below.

- **Fusion reads its config for the authority, but not for the position.**  `run_fusion_service` already falls back to `/etc/hf-timestd/timestd-config.toml` for the authority block, the anchor closure and `[storage]` (`multi_broadcast_fusion.py:4597, 4651-4659`).  Only `__main__` reads `[station] latitude/longitude` and `[fusion] timing_authority_level`, and only when `--config` arrives (`:5205-5233`).  So the audit's "no config" fault reaches the position and the timing level only.  The Kansas default sits at `:651-652`.
- **The position log line prints precise coordinates.**  `multi_broadcast_fusion.py:5217-5218` logs `receiver_lat:.6f` at INFO.  Task 6 removes it.
- **sigmond already names a unit that does not exist.**  The TUI tells operators to run `systemctl status timestd-authority-manager` (`sigmond/lib/sigmond/tui/format.py:469, 490`).  Task 3 adopts that name, so the hint becomes true.
- **The coupling rule survives the move.**  METROLOGY.md (line 498) makes the authority manager's loop the single gate for `authority.json`, chrony SHM and mDNS.  Fusion writes no chrony SHM since 2026-10-22, and the mDNS advertiser lives inside the AuthorityManager.  So a separate unit keeps the rule; only its housing changes.  The audit framed the move as retiring the rule.
- **A grid-only station may start no metrology at all** (from the code; not verified on a station).  The template unit passes `--latitude ${LATITUDE}` (`systemd/timestd-metrology@.service`).  sigmond writes `LATITUDE=` empty when the config holds no latitude (`sigmond/bin/sigmond-site-timing:513-516`).  systemd turns `${LATITUDE}` into one empty argument, and argparse rejects `float('')`.  Stage 3 reads the position from the config through Task 5's resolver, which removes this path.
- **One process already shares the fetchers.**  `IonoDataService` and `SpaceWeatherService` are per-process singletons with locks (hamsci-dsp `ionosphere/data_service.py:337-356`, `ionosphere/space_weather.py:93-102`).  Six channel threads therefore share one WAM-IPE fetcher with no new code.  But `MetrologyService.stop()` stops that shared fetcher (`metrology_service.py:2264-2269`), so one channel stopping would silence it for all.  Task 8 fixes that.  L2 calibration keeps its own fetcher, so the station goes from seven fetchers to two, not one.
- **SQLite writers tolerate threads.**  hamsci-dsp opens each connection with `check_same_thread=False` (`io/sqlite_writer.py:158-165`).  Each channel still builds its writers in its own thread.

## Global Constraints

- Every change reaches stations through sigmond v3.96 and `smd align`; never a hand edit on a station.
- Timing keeps one registration for all consumers: `authority.json` stays the only carrier clients read, and the registration files keep their paths, fields and semantics.
- Fusion runs as a witness: it writes `L3_fusion_timing`, `fusion_status.json` and log lines, and nothing that a consumer applies.
- Timing faults get exposed, never silently corrected.  A missing position refuses loudly; it never falls back to a made-up place.
- Precise latitude and longitude never appear in a log line, a repo file or a test fixture.  Logs name the 6-character grid only.  Tests use grid centres (`FN31pr`).
- sigmond's core stays stdlib-only.
- hf-timestd tests: `cd /home/mjh/hamsci/repos/hf-timestd && uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" <paths> -v`.
- sigmond tests: `cd /home/mjh/hamsci/repos/sigmond && .venv/bin/pytest -o addopts="" <paths> -v`; shell tests run with `bash tests/<name>.sh`.
- Every new test must fail when the code it checks is removed.  Each task's "mutation check" step proves that.
- Commits stage named files only, and end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z
  ```
- Before any deploy to B4: announce on the claude-bus, and record unit and CPU-affinity changes in `/etc/sigmond/CLAUDE-COORDINATION.md` on the B4 VM.  Canary order: B4 and sigma first.
- Exit status 78 (`EX_CONFIG`) means "refused for configuration".  Units that can refuse carry `RestartPreventExitStatus=78`, so systemd leaves them failed instead of looping.

## Review Focus

1. **Two authority writers during the move.**  Between the new authority unit starting and old fusion stopping, both write `authority.json`, the snapshot store and the T3 shadow JSONL.  Expected: the window lasts one restart.  Task 3's install step restarts fusion before it starts the authority unit, and the rollback runbook (end of Task 11) stops both new units by hand.
2. **A position placeholder.**  The config template ships `latitude = 0.0`, `longitude = 0.0` and `grid_square = "<YOUR_GRID>"`.  Expected: the resolver treats `(0.0, 0.0)` and any `<...>` value as absent.  Task 5 tests both.
3. **One channel stops, the others keep going.**  A channel thread that raises, or a channel's `stop()`, must not stop the shared WAM-IPE fetcher or another channel.  Task 8 tests the fetcher; Task 9 tests a dying channel thread.
4. **Six channels start at once.**  Six threads building engines together would race the iri2020 first-use build (ledger item `iri2020-build-race`) and stack six IRI initialisations.  Expected: the host builds channels one at a time under an init lock.  Task 9 tests the order.
5. **Old scripts start template instances again.**  An older `sigmond-site-timing` or a v3.95 restore may run `systemctl enable --now timestd-metrology@<ch>`.  Expected: after Task 10 no template unit file exists, so that command fails harmlessly; and the host still takes each channel's `.metrology.lock`, so no second writer can run.  Task 10 tests the migration script; Task 9 tests the lock.

---

## File Structure

hf-timestd (`/home/mjh/hamsci/repos/hf-timestd`):

| File | Change | Responsibility |
|---|---|---|
| `src/hf_timestd/core/authority_runner.py` | modify | records when the last tick succeeded; FusionStatusProbe freshness default 180 s |
| `src/hf_timestd/core/authority_service.py` | create | entry point of `timestd-authority-manager.service` |
| `src/hf_timestd/core/station_position.py` | create | resolves the station position from `[station]`, or refuses |
| `src/hf_timestd/core/multi_broadcast_fusion.py` | modify | drop the authority thread; require a position; once a minute |
| `src/hf_timestd/cli.py` | modify | the `calibrate` fusion path uses the resolver |
| `src/hf_timestd/core/metrology_service.py` | modify | hostable in a thread: no signals, no shared-fetcher stop, summary lock, lag in status |
| `src/hf_timestd/core/metrology_channels.py` | create | reads the channel list from `/etc/hf-timestd/metrology-channels/*.env` |
| `src/hf_timestd/core/metrology_station.py` | create | one process, one thread per channel, supervision, systemd notify |
| `src/hf_timestd/service_profile.py` | modify | new `authority_manager` service key |
| `systemd/timestd-authority-manager.service` | create | the authority unit |
| `systemd/timestd-fusion.service` | modify | `--config`, `--interval 60.0`, `WatchdogSec=180`, `RestartPreventExitStatus=78` |
| `systemd/timestd-metrology-station.service` | create | the one metrology unit |
| `systemd/timestd-metrology.target` | modify | `Wants=` the station unit |
| `systemd/timestd-metrology@.service` | delete | retired |
| `scripts/migrate-metrology-station.sh` | create | stops and disables template instances, removes the stale template link, enables the station unit |
| `scripts/install.sh` | modify | copies the new units, calls the migration, drops per-channel enabling |
| `scripts/pipeline-watchdog.sh` | modify | metrology liveness from each channel's `status.json` |
| `deploy.toml` | modify | link steps and `[systemd]` lists |
| `docs/METROLOGY.md`, `docs/DEBUGGING.md` | modify | the authority's housing; journal identifiers |
| `tests/station_fixture.py` | create | `TEST_GRID`, `TEST_LAT`, `TEST_LON` from a grid centre |
| `tests/unit/test_authority_service.py`, `tests/unit/test_station_position.py`, `tests/unit/test_metrology_station.py`, `tests/unit/test_metrology_channels.py`, `tests/unit/test_metrology_service_hosted.py`, `tests/unit/test_v396_units.py`, `tests/test_migrate_metrology_station.py` | create | tests named in the tasks |
| `tests/test_authority_runner.py`, `tests/test_pipeline_watchdog_liveness.py`, `tests/unit/test_service_profile.py`, the 10 files that construct `MultiBroadcastFusion` | modify | as the tasks say |

sigmond (`/home/mjh/hamsci/repos/sigmond`):

| File | Change |
|---|---|
| `bin/sigmond-site-timing` | authority restarts name the new unit (Task 4); step 3 enables the station unit (Task 11) |
| `bin/sigmond-firstrun-bringup` | counts channels from the station unit (Task 11) |
| `lib/sigmond/commands/timing.py` | metrology facts and the start-missing remedy (Task 11) |
| `lib/sigmond/tui/format.py` | the fusion hint names the authority unit (Task 4); the consumer pattern (Task 11) |
| `lib/sigmond/cpu.py` | the two new units in the `other` class (Tasks 4 and 11) |
| `tests/test-site-timing-t4.sh`, `tests/test-site-timing-t6.sh`, `tests/test-site-timing-metrology.sh`, `tests/test_timing.py`, `tests/test_cpu.py` | modify |

---

## Stage 1: the AuthorityManager in its own unit

### Task 1: AuthorityRunner says when its last tick succeeded

**Files:**
- Modify: `src/hf_timestd/core/authority_runner.py:59-110` (class `AuthorityRunner`)
- Test: `tests/test_authority_runner.py` (class `TestAuthorityRunner`)

**Interfaces:**
- Produces: `AuthorityRunner.last_tick_ok_monotonic: Optional[float]` (None until a tick succeeds); `AuthorityRunner.tick_fresh(max_age_s: float, now_monotonic: Optional[float] = None) -> bool`.  `AuthorityRunner.__init__` gains `monotonic_fn: Callable[[], float] = time.monotonic`.

- [ ] **Step 1: Write the failing tests**

```python
def test_tick_fresh_false_before_first_tick(self):
    r = AuthorityRunner(manager=_FakeManager(), interval_sec=30.0,
                        monotonic_fn=lambda: 1000.0)
    self.assertIsNone(r.last_tick_ok_monotonic)
    self.assertFalse(r.tick_fresh(90.0))

def test_tick_fresh_after_good_tick_then_stale(self):
    clock = [1000.0]
    r = AuthorityRunner(manager=_FakeManager(), interval_sec=30.0,
                        monotonic_fn=lambda: clock[0])
    r._safe_tick()
    self.assertEqual(r.last_tick_ok_monotonic, 1000.0)
    self.assertTrue(r.tick_fresh(90.0, now_monotonic=1089.0))
    self.assertFalse(r.tick_fresh(90.0, now_monotonic=1091.0))

def test_failed_tick_does_not_refresh(self):
    clock = [1000.0]
    r = AuthorityRunner(manager=_RaisingManager(), interval_sec=30.0,
                        monotonic_fn=lambda: clock[0])
    r._safe_tick()
    self.assertIsNone(r.last_tick_ok_monotonic)
```

`_FakeManager.tick()` returns None; `_RaisingManager.tick()` raises `RuntimeError`.  Define both at the top of the file beside `_CountingProbe`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/test_authority_runner.py -k "tick_fresh or failed_tick" -v`
Expected: FAIL (`unexpected keyword argument 'monotonic_fn'`).

- [ ] **Step 3: Implement**

In `_safe_tick`, set `self.last_tick_ok_monotonic = self._monotonic()` after `self.manager.tick()` returns, and only then.  `tick_fresh` returns False while `last_tick_ok_monotonic` stays None.

- [ ] **Step 4: Run the whole file**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/test_authority_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation check**

Delete the assignment in `_safe_tick`; rerun Step 2's command; expect `test_tick_fresh_after_good_tick_then_stale` to FAIL.  Restore.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/authority_runner.py tests/test_authority_runner.py
git commit -m "authority: the runner records when its last tick succeeded

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 2: the authority service entry point

**Files:**
- Create: `src/hf_timestd/core/authority_service.py`
- Test: `tests/unit/test_authority_service.py`

**Interfaces:**
- Consumes: `build_authority_runner_from_config(config: dict) -> AuthorityRunner` (`authority_runner.py:112`); `AuthorityRunner.tick_fresh` (Task 1); `chains_from_config`, `publish_chains` (`core/timing_chain_publisher.py`); `t3_admission_mode` (`core/t3_admission.py`); `load_fusion_config(path) -> dict` (`multi_broadcast_fusion.py:4600`; import it, do not copy it).
- Produces: `run_authority_service(config_path: Path, *, notify: Callable[[str], None], stop_event: threading.Event, pet_interval_s: float = 10.0, build_runner=build_authority_runner_from_config, publish=None) -> int` and `main(argv: Optional[List[str]] = None) -> int`.  `python -m hf_timestd.core.authority_service --config <path> --log-level INFO`.

Behaviour the tests pin:
- It builds the runner from the config, publishes the timing chains once (best effort: a failure logs a WARNING and the service continues, as fusion does today at `multi_broadcast_fusion.py:4765-4776`), starts the runner, then sends `READY=1`.
- Every `pet_interval_s` it sends `WATCHDOG=1` only while `runner.is_alive()` and `runner.tick_fresh(3 * runner.interval_sec)`.  A dead or stuck manager stops the pets, and systemd's `WatchdogSec` restarts the unit.
- When `stop_event` is set it calls `runner.stop(timeout=5.0)` and returns 0.
- `main` installs SIGTERM/SIGINT handlers that set the event, and wires `notify` to `systemd.daemon.notify` when importable, else to a no-op.

- [ ] **Step 1: Write the failing tests**

```python
def test_ready_then_pets_while_fresh(tmp_path):
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=True, interval_sec=30.0)
    def notify(msg):
        sent.append(msg)
        if sent.count("WATCHDOG=1") == 2:
            stop.set()
    rc = run_authority_service(tmp_path / "cfg.toml", notify=notify, stop_event=stop,
                               pet_interval_s=0.01, build_runner=lambda c: runner,
                               publish=lambda cfg: None)
    assert rc == 0
    assert sent[0] == "READY=1"
    assert sent.count("WATCHDOG=1") == 2
    assert runner.started and runner.stopped

def test_no_pet_when_tick_stale(tmp_path):
    sent, stop = [], threading.Event()
    runner = FakeRunner(alive=True, fresh=False, interval_sec=30.0)
    threading.Timer(0.1, stop.set).start()
    run_authority_service(tmp_path / "cfg.toml", notify=sent.append, stop_event=stop,
                          pet_interval_s=0.01, build_runner=lambda c: runner,
                          publish=lambda cfg: None)
    assert "WATCHDOG=1" not in sent
    assert runner.fresh_asked_with == 90.0

def test_chain_publish_failure_does_not_stop_service(tmp_path):
    stop = threading.Event(); stop.set()
    def boom(cfg): raise RuntimeError("no chain")
    rc = run_authority_service(tmp_path / "cfg.toml", notify=lambda m: None,
                               stop_event=stop, build_runner=lambda c: FakeRunner(),
                               publish=boom)
    assert rc == 0
```

`FakeRunner` records `start()`, `stop()`, answers `is_alive()` and `tick_fresh(max_age_s)` from its constructor flags, and stores the `max_age_s` it was asked.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_authority_service.py -v`
Expected: FAIL (`ModuleNotFoundError: hf_timestd.core.authority_service`).

- [ ] **Step 3: Implement `authority_service.py`**

`publish=None` means the real call: `publish_chains(chains_from_config(cfg), t3_admission_mode=t3_admission_mode(cfg))`.  The module docstring says why the unit exists: clients read `authority.json`, and it must not die with fusion or with l2-calibration (audit C1).

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command.  Expected: PASS.

- [ ] **Step 5: Mutation check**

Replace the freshness condition with `runner.is_alive()` alone; `test_no_pet_when_tick_stale` must FAIL.  Restore.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/authority_service.py tests/unit/test_authority_service.py
git commit -m "authority: a service entry point of its own, petting systemd only while ticks succeed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 3: the unit, and fusion without the authority

**Files:**
- Create: `systemd/timestd-authority-manager.service`
- Modify: `src/hf_timestd/core/multi_broadcast_fusion.py:4731-4785` (authority block) and `:5169-5175` (its stop)
- Modify: `src/hf_timestd/core/authority_runner.py:1-10` (module docstring)
- Modify: `src/hf_timestd/service_profile.py:28-84`
- Modify: `deploy.toml:72-96` (link steps), `:157-190` (`[systemd] units`)
- Modify: `scripts/install.sh:721` (`CORE_UNITS`), `:1136` (post-restart check list)
- Modify: `docs/METROLOGY.md` (the coupling-rule paragraph near line 498)
- Test: `tests/unit/test_v396_units.py`, `tests/unit/test_service_profile.py`

**Interfaces:**
- Consumes: `python -m hf_timestd.core.authority_service` (Task 2).
- Produces: unit `timestd-authority-manager.service`; service key `authority_manager` in `SERVICE_UNIT_MAP`; `run_fusion_service` without an authority thread.

The unit copies fusion's identity and run directory so file ownership and paths stay as they are today: `User=timestd`, `Group=timestd`, `RuntimeDirectory=hf-timestd`, `RuntimeDirectoryMode=0755`, `RuntimeDirectoryPreserve=yes`, `EnvironmentFile=-/etc/sigmond/coordination.env`, `Type=notify`, `WatchdogSec=120`, `TimeoutStartSec=120`, `Restart=always`, `RestartSec=5`, `MemoryMax=300M`, `MemoryHigh=200M`, `MemorySwapMax=0`, `SyslogIdentifier=timestd-authority-manager`, `OnFailure=timestd-alert@%N.service`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`, `After=timestd-core-recorder.service chrony.service`, `WantedBy=multi-user.target`.  It carries no `Requires=` and no `Wants=`: the authority must run, and say "T6 unavailable", while the recorder stands down.  `ExecStart=/opt/git/sigmond/hf-timestd/venv/bin/python -m hf_timestd.core.authority_service --config /etc/hf-timestd/timestd-config.toml --log-level INFO`.

In fusion, keep `_auth_config = load_fusion_config(config_path)` and the `_anchor_closure` resolution (used at `:5011`), and move both above the deleted block.  Delete the runner build, the chain publish and the runner stop.

The profile map gains `'authority_manager': 'timestd-authority-manager.service'` in `rtp`, `fusion` and `full` (open question 1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_v396_units.py
ROOT = Path(__file__).resolve().parents[2]

def _unit(name): return (ROOT / "systemd" / name).read_text()

def test_authority_unit_runs_the_authority_service():
    u = _unit("timestd-authority-manager.service")
    assert "-m hf_timestd.core.authority_service" in u
    assert "--config /etc/hf-timestd/timestd-config.toml" in u
    assert "Type=notify" in u and "WatchdogSec=" in u
    assert "Requires=" not in u
    assert "RuntimeDirectoryPreserve=yes" in u

def test_fusion_no_longer_builds_the_authority():
    import inspect
    from hf_timestd.core import multi_broadcast_fusion as m
    src = inspect.getsource(m.run_fusion_service)
    assert "build_authority_runner_from_config" not in src
    assert "publish_chains" not in src

def test_deploy_links_and_lists_the_authority_unit():
    d = (ROOT / "deploy.toml").read_text()
    assert 'dst  = "/etc/systemd/system/timestd-authority-manager.service"' in d
    import tomllib
    units = tomllib.loads(d)["systemd"]["units"]
    assert units.index("timestd-authority-manager.service") < units.index("timestd-fusion.service")
```

```python
# tests/unit/test_service_profile.py — add
@pytest.mark.parametrize("profile", ["rtp", "fusion", "full"])
def test_authority_manager_runs_in_timing_profiles(profile):
    assert "authority_manager" in PROFILES[profile]
    assert SERVICE_UNIT_MAP["authority_manager"] == "timestd-authority-manager.service"

def test_archive_profile_has_no_authority():
    assert "authority_manager" not in PROFILES["archive"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_v396_units.py tests/unit/test_service_profile.py -v`
Expected: FAIL (missing unit file; key absent; fusion source still names the builder).

- [ ] **Step 3: Implement** the unit, the fusion edit, the profile entry, the deploy and install lists.  Rewrite the `authority_runner.py` docstring and METROLOGY.md's paragraph to say the manager runs in `timestd-authority-manager.service`; the single-gate rule stands, since the manager still owns `authority.json` and mDNS, and fusion writes no SHM.

- [ ] **Step 4: Run the suites that touch fusion and the authority**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_v396_units.py tests/unit/test_service_profile.py tests/test_authority_runner.py tests/unit/test_multi_broadcast_fusion_remediation.py tests/test_fusion_journal_volume.py tests/test_anchor_sign_instrumentation.py -v`
Expected: PASS.  (If `test_anchor_sign_instrumentation.py` lives under `tests/unit/`, use that path.)

- [ ] **Step 5: Mutation check**

Put the deleted `build_authority_runner_from_config(...)` call back into `run_fusion_service`; `test_fusion_no_longer_builds_the_authority` must FAIL.  Remove `authority_manager` from `rtp`; the parametrized profile test must FAIL.  Restore both.

- [ ] **Step 6: Full suite**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/ -x -q`
Expected: PASS (the same skips as on `main`).

- [ ] **Step 7: Commit**

```bash
git add systemd/timestd-authority-manager.service src/hf_timestd/core/multi_broadcast_fusion.py \
  src/hf_timestd/core/authority_runner.py src/hf_timestd/service_profile.py deploy.toml \
  scripts/install.sh docs/METROLOGY.md tests/unit/test_v396_units.py tests/unit/test_service_profile.py
git commit -m "authority: timestd-authority-manager.service publishes authority.json; fusion no longer hosts it

Clients lost authority.json whenever fusion or l2-calibration stopped (audit C1, C10).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 4: sigmond restarts the authority, not fusion

**Files:**
- Modify: `sigmond/bin/sigmond-site-timing:389-390` (T4 peers declared) and `:1031-1032` (T6 armed)
- Modify: `sigmond/lib/sigmond/tui/format.py:946`
- Modify: `sigmond/lib/sigmond/cpu.py:74-79`
- Test: `sigmond/tests/test-site-timing-t4.sh:59`, `sigmond/tests/test-site-timing-t6.sh:58`, `sigmond/tests/test_cpu.py`

**Interfaces:**
- Consumes: unit `timestd-authority-manager.service` (Task 3).

The T4 branch becomes `systemctl try-restart timestd-authority-manager`; its message says "restarting timestd-authority-manager".  The T6 branch restarts `timestd-core-recorder timestd-authority-manager` (the recorder reads the T6 config; the authority builds its T6 probe from it; fusion reads neither).  `format.py:946` names `timestd-authority-manager.service`.  `cpu.py` adds `'timestd-authority-manager.service': 'other'`.

- [ ] **Step 1: Change the two shell tests' expectations first**

`test-site-timing-t4.sh:59` greps `try-restart timestd-authority-manager`; `test-site-timing-t6.sh:58` greps `try-restart timestd-core-recorder timestd-authority-manager`.  Add to `test_cpu.py`:

```python
def test_authority_manager_is_placed_with_the_other_timestd_units():
    from sigmond.cpu import AFFINITY_UNITS
    assert AFFINITY_UNITS["timestd-authority-manager.service"] == "other"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/mjh/hamsci/repos/sigmond && bash tests/test-site-timing-t4.sh; bash tests/test-site-timing-t6.sh; .venv/bin/pytest -o addopts="" tests/test_cpu.py -v`
Expected: the two greps report ✗; the pytest FAILS with KeyError.

- [ ] **Step 3: Implement** the four edits.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, plus `.venv/bin/pytest -o addopts="" tests/test_restart_leaves_deps.py tests/test_tui_annotation_quality.py -v`.
Expected: PASS.

- [ ] **Step 5: Mutation check**

Revert the T4 line alone; `test-site-timing-t4.sh` must report ✗.  Restore.

- [ ] **Step 6: Commit (in sigmond)**

```bash
git add bin/sigmond-site-timing lib/sigmond/tui/format.py lib/sigmond/cpu.py \
  tests/test-site-timing-t4.sh tests/test-site-timing-t6.sh tests/test_cpu.py
git commit -m "site-timing: a T4 or T6 config change restarts timestd-authority-manager, which now owns authority.json

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

**Gate G1.**  Build a v3.96 candidate with Stage 1 only.  Run Task 12's window W0 on B4 first, then `smd align` B4 and sigma, then Task 12's window W1.  Stage 2 merges only when W1 passes.

---

## Stage 2: fusion knows where it stands, and runs once a minute

### Task 5: the station position resolver

**Files:**
- Create: `src/hf_timestd/core/station_position.py`
- Create: `tests/station_fixture.py`
- Test: `tests/unit/test_station_position.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class StationPosition: lat: float; lon: float; source: str  # "precise" | "grid"; grid: str  # 6 characters`
  - `StationPosition.describe() -> str` — for logs; names the source and the grid, never a coordinate.  Exactly: `"grid FN31pr (precise position held, not logged)"` for `precise`, `"grid FN31pr centre"` for `grid`.
  - `class StationPositionError(ValueError)` — its message names the config keys to set and never a coordinate.
  - `resolve_station_position(config: dict) -> StationPosition`
  - `EX_CONFIG = 78`
- `tests/station_fixture.py`: `TEST_GRID = "FN31pr"`, `TEST_LAT, TEST_LON = grid_to_latlon(TEST_GRID)` (from `hamsci_dsp.geometry`).

Rules, in order:
1. `[station] latitude` and `longitude` both present, both numbers, inside ±90 and ±180, and not both 0.0 → `source="precise"`, `grid=latlon_to_grid(lat, lon, 6)`.
2. Else `[station] grid_square`, stripped, at least 4 characters, not a `<...>` placeholder, and accepted by `grid_to_latlon` → its centre, `source="grid"`, `grid` = the first 6 characters (or 4).
3. Else raise `StationPositionError`.

- [ ] **Step 1: Write the failing tests**

```python
from tests.station_fixture import TEST_GRID, TEST_LAT, TEST_LON

def test_precise_position_wins():
    p = resolve_station_position({"station": {"latitude": TEST_LAT, "longitude": TEST_LON,
                                              "grid_square": "AA00aa"}})
    assert (p.lat, p.lon, p.source, p.grid) == (TEST_LAT, TEST_LON, "precise", TEST_GRID)

def test_template_zeroes_fall_back_to_grid():
    p = resolve_station_position({"station": {"latitude": 0.0, "longitude": 0.0,
                                              "grid_square": TEST_GRID}})
    assert p.source == "grid" and (p.lat, p.lon) == (TEST_LAT, TEST_LON)

def test_grid_only():
    p = resolve_station_position({"station": {"grid_square": TEST_GRID}})
    assert p.source == "grid"

@pytest.mark.parametrize("station", [
    {}, {"latitude": 0.0, "longitude": 0.0, "grid_square": "<YOUR_GRID>"},
    {"latitude": "", "longitude": ""}, {"grid_square": "FN3"}, {"latitude": 95.0, "longitude": 10.0},
])
def test_refuses_without_a_position(station):
    with pytest.raises(StationPositionError) as e:
        resolve_station_position({"station": station})
    assert "grid_square" in str(e.value) and "latitude" in str(e.value)

def test_describe_never_carries_a_coordinate():
    p = resolve_station_position({"station": {"latitude": TEST_LAT, "longitude": TEST_LON}})
    text = p.describe()
    assert f"{p.lat:.2f}" not in text and f"{abs(p.lon):.2f}" not in text
    assert TEST_GRID in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_station_position.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `station_position.py`.**

- [ ] **Step 4: Run them to verify they pass.**  Same command; expected PASS.

- [ ] **Step 5: Mutation check**

Remove the "not both 0.0" clause; `test_template_zeroes_fall_back_to_grid` must FAIL.  Remove the placeholder check; the `<YOUR_GRID>` case must FAIL.  Restore.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/station_position.py tests/station_fixture.py tests/unit/test_station_position.py
git commit -m "position: one resolver for the station position: precise, else grid centre, else refuse

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 6: fusion takes the position, or refuses

**Files:**
- Modify: `src/hf_timestd/core/multi_broadcast_fusion.py:598-612, 651-652` (constructor), `:4614-4670` (`run_fusion_service`), `:5181-5254` (`__main__`)
- Modify: `src/hf_timestd/cli.py:2645-2677` (the `calibrate` fusion path)
- Modify: `systemd/timestd-fusion.service` (ExecStart, `[Service]`)
- Modify: the 10 test files that construct `MultiBroadcastFusion` (find them with `grep -rln "MultiBroadcastFusion(" tests`)
- Test: `tests/unit/test_fusion_position.py` (create), `tests/unit/test_v396_units.py`

**Interfaces:**
- Consumes: `resolve_station_position`, `StationPosition`, `StationPositionError`, `EX_CONFIG` (Task 5).
- Produces: `MultiBroadcastFusion.__init__` raises `ValueError` when `receiver_lat` or `receiver_lon` is None.  `run_fusion_service(..., position: StationPosition, ...)` replaces the `receiver_lat` and `receiver_lon` parameters.  `__main__` and `cli.py` resolve the position from the config and, on `StationPositionError`, log one CRITICAL line (the error text) and exit `EX_CONFIG`.  The start log reads `Fusion station position: <position.describe()>`.

`__main__` reads the config from `--config`, defaulting to `DEFAULT_CONFIG_PATH`, so a unit that forgets the flag still finds the station.  The unit passes `--config /etc/hf-timestd/timestd-config.toml` anyway and gains `RestartPreventExitStatus=78`.  Delete the INFO line at `:5217-5218`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_fusion_position.py
from tests.station_fixture import TEST_LAT, TEST_LON

def test_fusion_refuses_without_position(tmp_path):
    with pytest.raises(ValueError, match="position"):
        MultiBroadcastFusion(data_root=tmp_path)

def test_fusion_uses_the_given_position(tmp_path):
    f = MultiBroadcastFusion(data_root=tmp_path, receiver_lat=TEST_LAT, receiver_lon=TEST_LON)
    assert (f.receiver_lat, f.receiver_lon) == (TEST_LAT, TEST_LON)
    assert (f.global_solver.receiver_lat, f.global_solver.receiver_lon) == (TEST_LAT, TEST_LON)

def test_main_exits_ex_config_without_position(tmp_path):
    cfg = tmp_path / "c.toml"; cfg.write_text('[station]\ncallsign = "X1X"\n')
    r = subprocess.run([sys.executable, "-m", "hf_timestd.core.multi_broadcast_fusion",
                        "--config", str(cfg), "--data-root", str(tmp_path)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 78
    assert "grid_square" in r.stderr

def test_position_helper_never_logs_a_coordinate(tmp_path, caplog):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"[station]\nlatitude = {TEST_LAT}\nlongitude = {TEST_LON}\n")
    with caplog.at_level(logging.INFO):
        pos = resolve_fusion_position(cfg)
    assert pos.source == "precise"
    assert f"{TEST_LAT:.2f}" not in caplog.text and f"{abs(TEST_LON):.2f}" not in caplog.text
    assert TEST_GRID in caplog.text
```

`resolve_fusion_position(config_path: Path) -> StationPosition` lives in `multi_broadcast_fusion.py`.  It loads the TOML through `load_fusion_config`, calls the resolver, logs `Fusion station position: <describe()>` at INFO, and lets `StationPositionError` propagate.  `__main__` and `cli.py` both call it.

```python
# tests/unit/test_v396_units.py — add
def test_fusion_unit_passes_config_and_stops_on_refusal():
    u = _unit("timestd-fusion.service")
    assert "--config /etc/hf-timestd/timestd-config.toml" in u
    assert "RestartPreventExitStatus=78" in u
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_fusion_position.py tests/unit/test_v396_units.py -v`
Expected: FAIL (fusion builds at Kansas; exit status 0 or 1, not 78).

- [ ] **Step 3: Implement** the constructor check, `resolve_fusion_position`, the new `run_fusion_service` signature, `__main__`, `cli.py`, and the unit.

- [ ] **Step 4: Update the existing fusion tests**

In each file from `grep -rln "MultiBroadcastFusion(" tests`, pass `receiver_lat=TEST_LAT, receiver_lon=TEST_LON` (import from `tests.station_fixture`).  Change no assertion.

- [ ] **Step 5: Run fusion's tests and the full suite**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/ -x -q`
Expected: PASS.

- [ ] **Step 6: Mutation check**

Restore the `else 39.0` default in the constructor; `test_fusion_refuses_without_position` must FAIL.  Make `resolve_fusion_position` log `pos.lat`; the coordinate test must FAIL.  Undo both.

- [ ] **Step 7: Commit**

```bash
git add src/hf_timestd/core/multi_broadcast_fusion.py src/hf_timestd/cli.py systemd/timestd-fusion.service \
  tests/unit/test_fusion_position.py tests/unit/test_v396_units.py <the 10 fusion test files>
git commit -m "fusion: compute at the station's own position, or refuse to start (exit 78)

Every station's fusion ran its geometry, IRI and propagation models at 39.0 N, 98.0 W (audit C1, C4, C5).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 7: fusion runs once a minute, as a witness

**Files:**
- Modify: `src/hf_timestd/core/multi_broadcast_fusion.py:4853-5150` (main loop: the pet and the sleep at `:5143`)
- Modify: `src/hf_timestd/core/authority_runner.py:292-298` (FusionStatusProbe freshness default)
- Modify: `systemd/timestd-fusion.service` (`Description=`, `--interval`, `WatchdogSec=`)
- Test: `tests/unit/test_fusion_cadence.py` (create), `tests/unit/test_v396_units.py`, `tests/test_authority_runner.py`

**Interfaces:**
- Produces: `seconds_until_next_cycle(interval_sec: float, loop_start: float, now: float) -> float` in `multi_broadcast_fusion.py`, returning `max(0.0, interval_sec - (now - loop_start))`.  It schedules work only; no timing value comes from it.

The loop pets `WATCHDOG=1` at its top (as now) and again just before it sleeps, then sleeps `seconds_until_next_cycle(...)`.  The unit runs `--interval 60.0`, `WatchdogSec=180`, and `Description=HF-Timestd multi-broadcast fusion (witness, once a minute)`.  FusionStatusProbe's default `freshness_sec` becomes 180.0, so a 60-second status file never reads "stale" between cycles.  Both fusion passes stay (see "Later").

- [ ] **Step 1: Write the failing tests**

```python
def test_next_cycle_subtracts_work_time():
    assert seconds_until_next_cycle(60.0, loop_start=100.0, now=112.5) == 47.5

def test_next_cycle_never_negative():
    assert seconds_until_next_cycle(60.0, loop_start=100.0, now=175.0) == 0.0

# test_v396_units.py
def test_fusion_unit_runs_once_a_minute():
    u = _unit("timestd-fusion.service")
    assert "--interval 60.0" in u and "WatchdogSec=180" in u

# test_authority_runner.py
def test_fusion_probe_freshness_covers_a_minute_cycle(self):
    r = build_authority_runner_from_config({})
    probe = next(p for p in r.manager.probes if isinstance(p, FusionStatusProbe))
    self.assertEqual(probe.freshness_sec, 180.0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_fusion_cadence.py tests/unit/test_v396_units.py tests/test_authority_runner.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the helper, the loop change, the probe default and the unit.

- [ ] **Step 4: Run them to verify they pass, then the full suite**

Run: the Step 2 command, then `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/ -x -q`.
Expected: PASS.

- [ ] **Step 5: Mutation check**

Make the helper return `interval_sec`; `test_next_cycle_subtracts_work_time` must FAIL.  Restore.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/multi_broadcast_fusion.py src/hf_timestd/core/authority_runner.py \
  systemd/timestd-fusion.service tests/unit/test_fusion_cadence.py tests/unit/test_v396_units.py tests/test_authority_runner.py
git commit -m "fusion: one witness cycle a minute in place of one every 8 s (Michael, decision 5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

**Gate G2.**  Before Stage 2 reaches any station past B4 and sigma, run Task 12 step 6 (the fleet position check): a station with neither a precise position nor a grid would now stand with fusion failed.  Then `smd align` B4 and sigma, and run Task 12's window W2.  Stage 3 merges only when W2 passes.

---

## Stage 3: one metrology process per station

### Task 8: MetrologyService can live in a thread

**Files:**
- Modify: `src/hf_timestd/core/metrology_service.py:197-206` (constructor), `:508-522` (`run`), `:1755-1826` (`_publish_registration`, the summary check and write), `:2244-2270` (`stop`), `:2373-2405` (`_write_status`)
- Test: `tests/unit/test_metrology_service_hosted.py`

**Interfaces:**
- Produces:
  - `MetrologyService.__init__(..., summary_lock: Optional[threading.Lock] = None, owns_shared_services: bool = True)`.  With `owns_shared_services=False` the service neither starts nor stops `IonoDataService` and `SpaceWeatherService`; the host does.
  - `MetrologyService.run(install_signals: bool = True) -> None`.
  - `MetrologyService.stop() -> None` stops the shared fetchers only when `owns_shared_services` holds.
  - `_publish_registration` holds `summary_lock` (when given) around `fresh_plane_from_another_writer(...)` and `write_summary(...)` together, so one channel cannot write between another's check and write.
  - `status.json` gains `"last_minute_lag_s"` (wall time of the write minus the processed minute's end, rounded to 0.1 s; a diagnostic of processing delay, never a timing value) and `"thread_restarts"` (an int the host sets on the service as `self.thread_restarts`, default 0).

The per-channel behaviour of the standalone process stays as it is: defaults keep today's signals and today's fetcher ownership.

- [ ] **Step 1: Write the failing tests**

Build the service through the existing test helpers in `tests/unit/test_metrology_service_remediation.py` (reuse its constructor stubs; do not invent new ones).

```python
def test_hosted_stop_leaves_shared_fetcher_running(svc_factory):
    iono = FakeFetcher()
    svc = svc_factory(owns_shared_services=False, iono_service=iono)
    svc.stop()
    assert iono.stopped is False

def test_standalone_stop_still_stops_fetcher(svc_factory):
    iono = FakeFetcher()
    svc = svc_factory(owns_shared_services=True, iono_service=iono)
    svc.stop()
    assert iono.stopped is True

def test_run_in_thread_installs_no_signal_handler(svc_factory, monkeypatch):
    calls = []
    monkeypatch.setattr(signal, "signal", lambda *a: calls.append(a))
    svc = svc_factory(owns_shared_services=False)
    svc._run_ringbuffer_mode = lambda: None
    t = threading.Thread(target=svc.run, kwargs={"install_signals": False}); t.start(); t.join(5)
    assert calls == []

def test_summary_check_and_write_hold_the_lock(svc_factory):
    lock = RecordingLock()
    svc = svc_factory(summary_lock=lock)
    svc.reg_store = StoreThatAssertsLockHeld(lock)   # fails if write_summary or the check runs unlocked
    svc._publish_registration(fused=FAKE_FUSED, contributing=[svc.channel_name],
                              label_s0=None, residual_ms=None, epoch=1)
    assert lock.acquisitions == 1

def test_status_carries_lag_and_restarts(svc_factory, tmp_path):
    svc = svc_factory(); svc.thread_restarts = 2
    svc._write_status(minute=1_700_000_040, results=[])
    s = json.loads(svc.status_file.read_text())
    assert s["thread_restarts"] == 2 and isinstance(s["last_minute_lag_s"], float)
```

Name `svc_factory` as a fixture in this file.  It wraps the remediation file's stubbed constructor and passes `iono_service` through by patching `IonoDataService.get_instance`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_metrology_service_hosted.py -v`
Expected: FAIL (unknown keyword arguments).

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them, and every metrology suite**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_metrology_service_hosted.py tests/unit/test_metrology_service_registration.py tests/unit/test_metrology_service_remediation.py tests/unit/test_metrology_service_t3_station_state.py tests/unit/test_metrology_service_counter_domains.py tests/unit/test_metrology_shadow_rows.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation check**

Drop the `owns_shared_services` condition from `stop()`; `test_hosted_stop_leaves_shared_fetcher_running` must FAIL.  Move `fresh_plane_from_another_writer` outside the lock; the lock test must FAIL.  Restore both.

- [ ] **Step 6: Commit**

```bash
git add src/hf_timestd/core/metrology_service.py tests/unit/test_metrology_service_hosted.py
git commit -m "metrology: a channel service can run in a thread without owning signals or the shared fetchers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 9: the metrology station host

**Files:**
- Create: `src/hf_timestd/core/metrology_channels.py`
- Create: `src/hf_timestd/core/metrology_station.py`
- Test: `tests/unit/test_metrology_channels.py`, `tests/unit/test_metrology_station.py`

**Interfaces:**
- Consumes: `MetrologyService(config, channel_name, frequency_hz, output_dir, receiver_grid, station_config, summary_lock=, owns_shared_services=False)` and `run(install_signals=False)` (Task 8); `resolve_station_position`, `EX_CONFIG` (Task 5); `metrology_config_from_toml` (`metrology_service.py:87`); `ResourceGuardian.from_config(path)`, `.preflight_check()`, `.watchdog_check()` (`core/resource_guardian.py`); `IonoDataService.get_instance(home_lat=, home_lon=)`, `SpaceWeatherService.get_instance()`.
- Produces:
  - `@dataclass(frozen=True) class ChannelSpec: name: str; frequency_hz: float; callsign: str; grid_square: str; station_id: str; instrument_id: str`
  - `load_channel_specs(env_dir: Path) -> List[ChannelSpec]` — one per `*.env`, sorted by name; a file without `FREQUENCY_HZ` raises `ValueError` naming the file.  It reads `KEY=VALUE` lines and ignores `#` lines.
  - `acquire_channel_lock(output_dir: Path) -> IO` — the lock logic now in `__main__` at `metrology_service.py:2565-2600`, moved verbatim in behaviour (permission error vs duplicate writer); raises `ChannelLockError(str)` in place of `sys.exit`.  `__main__` of `metrology_service.py` calls it, so the standalone path keeps working.
  - `class MetrologyStation` with
    - `__init__(self, specs: List[ChannelSpec], config: dict, position: StationPosition, data_root: Path, *, service_factory=MetrologyService, notify: Callable[[str], None] = lambda m: None, restart_backoff_s: float = 15.0, supervise_interval_s: float = 5.0)`
    - `start(self) -> None` — takes every channel lock first (a held lock refuses the whole start with `ChannelLockError`), starts the shared fetchers once at `position`, then one thread per channel, named for the channel.  Each thread builds its service inside an init lock shared by all channels, so builds run one at a time; then it runs the service.  `READY=1` goes out after every channel's build finished or failed.
    - `run_forever(self, stop_event: threading.Event) -> int` — every `supervise_interval_s`: restarts a channel whose thread died, no sooner than `restart_backoff_s` after its last start, increments its `thread_restarts`, and logs one ERROR naming the channel and the exception; sends `WATCHDOG=1` while at least one channel thread lives; runs the guardian's `watchdog_check()` every 30 s and on STOP or EMERGENCY stops every channel and returns 0 (today's behaviour per process).  On `stop_event` it stops every channel, stops the shared fetchers, joins threads (5 s each) and returns 0.
  - `main(argv: Optional[List[str]] = None) -> int` with `--config`, `--channels-dir` (default `/etc/hf-timestd/metrology-channels`), `--data-root` (default `/var/lib/timestd`), `--log-level`.  It returns `EX_CONFIG` when the position cannot be resolved or the channel directory holds no `.env` file, and 1 when a channel lock refuses or the guardian preflight fails.  Its log format: `'%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s'`, so every line names its channel.
  - Each channel's output stays `<data_root>/phase2/<channel>/`, and `station_config` carries the resolved `lat`/`lon` with the env file's identity fields: the same values the per-channel processes received.

- [ ] **Step 1: Write the failing tests**

```python
# test_metrology_channels.py
def test_reads_each_env_file(tmp_path):
    (tmp_path / "WWV_20000.env").write_text("# c\nFREQUENCY_HZ=20000000\nCALLSIGN=X1X\nGRID_SQUARE=FN31pr\n"
                                            "STATION_ID=\nINSTRUMENT_ID=\n")
    (tmp_path / "SHARED_2500.env").write_text("FREQUENCY_HZ=2500000\nCALLSIGN=X1X\n")
    specs = load_channel_specs(tmp_path)
    assert [s.name for s in specs] == ["SHARED_2500", "WWV_20000"]
    assert specs[1].frequency_hz == 20_000_000.0 and specs[1].station_id == ""

def test_missing_frequency_names_the_file(tmp_path):
    (tmp_path / "BAD.env").write_text("CALLSIGN=X1X\n")
    with pytest.raises(ValueError, match="BAD.env"):
        load_channel_specs(tmp_path)

def test_held_lock_is_a_duplicate_writer(tmp_path):
    held = acquire_channel_lock(tmp_path)
    with pytest.raises(ChannelLockError, match="already owns"):
        acquire_channel_lock(tmp_path)
```

```python
# test_metrology_station.py — FakeService records construction order and can be told to raise
def test_builds_channels_one_at_a_time(tmp_path):
    log = []
    station = make_station(tmp_path, ["A", "B", "C"], service_factory=fake_factory(log, build_s=0.05))
    station.start()
    wait_until(lambda: len(log) == 6)
    assert log == ["build A start", "build A end", "build B start", "build B end",
                   "build C start", "build C end"]

def test_dead_channel_restarts_others_keep_running(tmp_path):
    station = make_station(tmp_path, ["A", "B"], service_factory=fake_factory(raise_once={"A"}),
                           restart_backoff_s=0.0, supervise_interval_s=0.01)
    stop = threading.Event(); threading.Timer(0.3, stop.set).start()
    assert station.run_forever_after_start(stop) == 0      # helper: start() then run_forever()
    assert station.restarts("A") == 1 and station.restarts("B") == 0
    assert station.service("B").ran_without_interruption

def test_stop_stops_channels_then_shared_fetchers(tmp_path):
    order = []
    station = make_station(tmp_path, ["A"], service_factory=fake_factory(order=order),
                           fetchers=FakeFetchers(order))
    stop = threading.Event(); stop.set()
    station.start(); station.run_forever(stop)
    assert order == ["A stop", "fetchers stop"]

def test_ready_after_all_builds(tmp_path):
    sent = []
    station = make_station(tmp_path, ["A", "B"], notify=sent.append)
    station.start()
    wait_until(lambda: "READY=1" in sent)
    assert station.built() == {"A", "B"}

def test_refuses_whole_start_when_a_channel_lock_is_held(tmp_path):
    held = acquire_channel_lock(tmp_path / "phase2" / "B")
    station = make_station(tmp_path, ["A", "B"])
    with pytest.raises(ChannelLockError):
        station.start()
    assert station.threads() == []

def test_main_refuses_without_position(tmp_path):
    cfg = tmp_path / "c.toml"; cfg.write_text("[station]\n")
    (tmp_path / "ch").mkdir(); (tmp_path / "ch" / "A.env").write_text("FREQUENCY_HZ=5000000\n")
    assert main(["--config", str(cfg), "--channels-dir", str(tmp_path / "ch"),
                 "--data-root", str(tmp_path)]) == 78
```

`make_station`, `fake_factory`, `FakeFetchers` and `wait_until` live at the top of the test file.  The station takes its fetchers through a `fetchers=` keyword (an object with `start()` and `stop()`) that defaults to the real singletons; add that keyword to the `__init__` signature above.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_metrology_channels.py tests/unit/test_metrology_station.py -v`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement** both modules, and switch `metrology_service.__main__` to `acquire_channel_lock`.

- [ ] **Step 4: Run them to verify they pass, then the full suite**

Run: the Step 2 command, then `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/ -x -q`.
Expected: PASS.

- [ ] **Step 5: Mutation checks**

Remove the init lock; `test_builds_channels_one_at_a_time` must FAIL.  Remove the restart branch; `test_dead_channel_restarts_others_keep_running` must FAIL.  Take the locks after starting threads; the lock test must FAIL.  Restore all three.

- [ ] **Step 6: A real-service smoke test on the devbox**

Run two real channels against no ring for 20 s, to prove that real construction, logging and shutdown work across threads:

```bash
mkdir -p /var/tmp/mjh-pt/v396d-smoke/ch
printf 'FREQUENCY_HZ=10000000\nCALLSIGN=X1X\nGRID_SQUARE=FN31pr\n' > /var/tmp/mjh-pt/v396d-smoke/ch/SHARED_10000.env
printf 'FREQUENCY_HZ=20000000\nCALLSIGN=X1X\nGRID_SQUARE=FN31pr\n' > /var/tmp/mjh-pt/v396d-smoke/ch/WWV_20000.env
printf '[station]\ncallsign = "X1X"\ngrid_square = "FN31pr"\n[metrology]\nrealtime_iono = false\n' > /var/tmp/mjh-pt/v396d-smoke/c.toml
timeout -s TERM 20 uv run python -m hf_timestd.core.metrology_station --config /var/tmp/mjh-pt/v396d-smoke/c.toml \
  --channels-dir /var/tmp/mjh-pt/v396d-smoke/ch --data-root /var/tmp/mjh-pt/v396d-smoke/data --log-level INFO; echo "exit=$?"
```

Expected: lines tagged `[SHARED_10000]` and `[WWV_20000]` that say "waiting for producer", a clean shutdown line for each channel, and `exit=0`.  If the resource guardian refuses on the devbox, point `--data-root` at a disk with room; do not weaken the guardian.

- [ ] **Step 7: Commit**

```bash
git add src/hf_timestd/core/metrology_channels.py src/hf_timestd/core/metrology_station.py \
  src/hf_timestd/core/metrology_service.py tests/unit/test_metrology_channels.py tests/unit/test_metrology_station.py
git commit -m "metrology: one station process runs every channel in its own thread (Michael, decision 4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 10: the unit, the migration, the watchdog

**Files:**
- Create: `systemd/timestd-metrology-station.service`
- Modify: `systemd/timestd-metrology.target`
- Delete: `systemd/timestd-metrology@.service`
- Create: `scripts/migrate-metrology-station.sh`
- Modify: `scripts/install.sh:727-739` (template copy), `:788-858` (keeps writing env files), `:875-882` (per-instance enable), `:1053-1066` (`--restart-all`), `:1136`
- Modify: `deploy.toml:78-86` (link steps), `:157-193` (`units`, `templated_units`)
- Modify: `scripts/pipeline-watchdog.sh:275-338` (`check_metrology`)
- Modify: `docs/DEBUGGING.md` (section 3.3 and the `journalctl -u 'timestd-metrology@*'` lines)
- Test: `tests/unit/test_v396_units.py`, `tests/test_migrate_metrology_station.py`, `tests/test_pipeline_watchdog_liveness.py`

**Interfaces:**
- Consumes: `python -m hf_timestd.core.metrology_station` (Task 9).
- Produces: unit `timestd-metrology-station.service`; `scripts/migrate-metrology-station.sh [--dry-run]`.

The unit carries over the template's settings that still apply: `After=` and `Requires=timestd-core-recorder.service`, `RequiresMountsFor=/dev/shm`, `PartOf=timestd-metrology.target`, `OnFailure=timestd-alert@%N.service`, `StartLimitIntervalSec=600`, `StartLimitBurst=10`, `User=timestd`, both `EnvironmentFile=` lines for coordination.env and `/etc/hf-timestd/environment`, `MALLOC_ARENA_MAX=2`, `Restart=always`, `RestartSec=15`, `OOMScoreAdjust=-500`, `Nice=-10`, the hardening block (`NoNewPrivileges`, `ProtectSystem=strict`, `RuntimeDirectory=hf-timestd`, `RuntimeDirectoryPreserve=yes`, `ReadWritePaths=`, `ProtectHome`).  New or changed: `Type=notify`, `TimeoutStartSec=600`, `WatchdogSec=120`, `RestartPreventExitStatus=78`, `CPUQuota=300%` (six times the old 50 %), `MemoryHigh=900M`, `MemoryMax=1500M` (first values; Task 12's W3 sets the final ones), `SyslogIdentifier=timestd-metrology`, `WantedBy=timestd-metrology.target`.  `ExecStart=/opt/git/sigmond/hf-timestd/venv/bin/python -m hf_timestd.core.metrology_station --config /etc/hf-timestd/timestd-config.toml --channels-dir /etc/hf-timestd/metrology-channels --data-root ${DATA_ROOT} --log-level ${TIMESTD_LOG_LEVEL}`.

The target gains `Wants=timestd-metrology-station.service`.

`migrate-metrology-station.sh` does, in this order, and says what it did in each step:
1. If `/etc/systemd/system/timestd-metrology-station.service` does not exist, it says so and exits 1 without touching anything.
2. `systemctl daemon-reload`; `systemctl enable timestd-metrology-station.service`.
3. For every loaded or enabled `timestd-metrology@*.service`: `systemctl disable --now <unit>`.
4. `systemctl start timestd-metrology-station.service`, and waits up to 600 s for `is-active`.  When the start fails it prints the last 20 journal lines of the unit and exits 1.  The template instances stay stopped then: the per-channel locks forbid running both, and the operator needs to see the failure, not a silent fallback.
5. Removes `/etc/systemd/system/timestd-metrology@.service` when it is a symlink or a regular file, then `daemon-reload`.

`install.sh` stops copying and enabling the template, keeps writing the env files (the host reads them), and calls the migration script after the profile step.  `deploy.toml` drops the template's link step and the `templated_units` list, and adds a link step and a `units` entry for the station unit.

`check_metrology` in the pipeline watchdog judges the station unit by each channel's `status.json` (audit B25):
- unit not enabled → return; enabled but inactive → the existing restart (unchanged policy);
- each channel named by `/etc/hf-timestd/metrology-channels/*.env`: age of `$DATA_ROOT/phase2/<ch>/status.json` by mtime; a missing file counts as stale only once the unit has been active for more than 600 s (`systemctl show -p ActiveEnterTimestamp --value`);
- every channel stale beyond `METROLOGY_STALE` → restart the unit, naming the channels and ages;
- some channels stale → one `log_warn` naming them, no restart.

If the v3.96 plan for decision 6 (who restarts the recorder) rewrites this script, fold this rule into that rewrite instead of landing it twice.

- [ ] **Step 1: Write the failing tests**

```python
# test_v396_units.py
def test_template_unit_is_gone():
    assert not (ROOT / "systemd" / "timestd-metrology@.service").exists()

def test_station_unit():
    u = _unit("timestd-metrology-station.service")
    assert "-m hf_timestd.core.metrology_station" in u
    assert "Type=notify" in u and "RestartPreventExitStatus=78" in u
    assert "Requires=timestd-core-recorder.service" in u
    assert "%i" not in u

def test_target_wants_station_unit():
    assert "Wants=timestd-metrology-station.service" in _unit("timestd-metrology.target")

def test_deploy_has_no_templated_metrology():
    import tomllib
    d = tomllib.loads((ROOT / "deploy.toml").read_text())
    assert "timestd-metrology@.service" not in d["systemd"].get("templated_units", [])
    assert "timestd-metrology-station.service" in d["systemd"]["units"]
```

```python
# tests/test_migrate_metrology_station.py — PATH shim for systemctl that logs each call
# to $T/systemctl.log and answers list-units/is-active from env vars, as
# tests/test_pipeline_watchdog_liveness.py does.  UNIT_DIR overrides /etc/systemd/system.
def test_refuses_when_station_unit_missing(env):
    r = run_migrate(env, station_unit_present=False)
    assert r.returncode == 1 and env.calls() == []

def test_disables_instances_before_starting_station(env):
    r = run_migrate(env, instances=["timestd-metrology@SHARED_2500.service",
                                    "timestd-metrology@WWV_20000.service"])
    calls = env.calls()
    assert r.returncode == 0
    i_dis = calls.index("disable --now timestd-metrology@WWV_20000.service")
    assert i_dis < calls.index("start timestd-metrology-station.service")
    assert not (env.unit_dir / "timestd-metrology@.service").exists()

def test_failed_start_does_not_bring_back_instances(env):
    r = run_migrate(env, instances=["timestd-metrology@SHARED_2500.service"], station_starts=False)
    assert r.returncode == 1
    assert not any(c.startswith("start timestd-metrology@") for c in env.calls())
```

The script reads `UNIT_DIR` (default `/etc/systemd/system`) and `MIGRATE_START_WAIT_S` (default 600) from the environment, so the test can point it at a temp dir and wait 1 s.

```python
# tests/test_pipeline_watchdog_liveness.py — add, reusing its env fixture with
# ENABLED_UNITS="timestd-metrology-station.service" and a channels dir with two .env files
def test_station_one_stale_channel_warns_without_restart(env): ...
    # status.json fresh for SHARED_10000, 400 s old for WWV_20000
    # assert "restart" not in the dry-run log for timestd-metrology-station, and "WWV_20000" in a WARN line
def test_station_all_channels_stale_restarts(env): ...
    # both status.json 400 s old; assert the dry-run log names a restart of timestd-metrology-station.service
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/unit/test_v396_units.py tests/test_migrate_metrology_station.py tests/test_pipeline_watchdog_liveness.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the unit, target, deletion, migration script, install.sh, deploy.toml, watchdog and DEBUGGING.md edits.  In the watchdog, drop the old three-table SQLite liveness for metrology; keep `sqlite_channel_age` only if another check still calls it.

- [ ] **Step 4: Run them to verify they pass, then the full suite**

Run: the Step 2 command, then `uv run pytest --basetemp=/var/tmp/mjh-pt/v396d -o addopts="" tests/ -x -q`.
Expected: PASS.  Also `bash -n scripts/migrate-metrology-station.sh scripts/install.sh scripts/pipeline-watchdog.sh` prints nothing.

- [ ] **Step 5: Mutation checks**

Move step 3 of the migration after step 4; `test_disables_instances_before_starting_station` must FAIL.  Make the watchdog restart on any stale channel; `test_station_one_stale_channel_warns_without_restart` must FAIL.  Restore both.

- [ ] **Step 6: Commit**

```bash
git add systemd/timestd-metrology-station.service systemd/timestd-metrology.target \
  scripts/migrate-metrology-station.sh scripts/install.sh scripts/pipeline-watchdog.sh deploy.toml docs/DEBUGGING.md \
  tests/unit/test_v396_units.py tests/test_migrate_metrology_station.py tests/test_pipeline_watchdog_liveness.py
git rm systemd/timestd-metrology@.service
git commit -m "metrology: timestd-metrology-station.service replaces the six timestd-metrology@ instances

The migration disables the instances, starts the station unit, and removes the template;
the watchdog reads each channel's status.json (audit B25).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

### Task 11: sigmond knows the one metrology unit

**Files:**
- Modify: `sigmond/bin/sigmond-site-timing:562-574` (enable loop after step 3)
- Modify: `sigmond/bin/sigmond-firstrun-bringup:257-269`
- Modify: `sigmond/lib/sigmond/commands/timing.py:361-368` (facts), `:476-487` (`_start_missing_metrology`)
- Modify: `sigmond/lib/sigmond/tui/format.py:831`
- Modify: `sigmond/lib/sigmond/cpu.py:74-79`
- Test: `sigmond/tests/test-site-timing-metrology.sh`, `sigmond/tests/test_timing.py`, `sigmond/tests/test_cpu.py`

**Interfaces:**
- Consumes: unit `timestd-metrology-station.service`; `/var/lib/timestd/phase2/<ch>/status.json` with `last_update` (Tasks 8, 10).
- Produces (sigmond `commands/timing.py`): `metrology_running_channels(env_dir: Path, phase2_dir: Path, unit_active: bool, now: float, stale_s: float = 180.0) -> int` — the number of channels with an env file whose `status.json` mtime lies within `stale_s`, or 0 when the unit stands inactive.  The facts become `f['metrology'] = {'expected': <env files>, 'running': metrology_running_channels(...)}`.

Behaviour:
- **site-timing**: when `/etc/systemd/system/timestd-metrology-station.service` exists, run `systemctl enable --now timestd-metrology-station.service` (or `restart` under `RESTART_METROLOGY=1`), and only after it reports active, `systemctl disable --now` any `timestd-metrology@*` instance.  When the unit file does not exist (hf-timestd not yet updated), keep today's per-instance loop unchanged.  This ordering means a sigmond that updates before hf-timestd never leaves the station without metrology.
- **firstrun-bringup**: report `metrology: station unit active, N channels configured` from the unit state and the env-file count; keep the "NO metrology channels" warning when the unit stands inactive and the role is not radiod-only.
- **`_start_missing_metrology`**: start `timestd-metrology-station.service` when inactive; its return string names the unit.
- **TUI**: `("timestd-metrology", "timestd-metrology-station.service")`.
- **cpu.py**: add `'timestd-metrology-station.service': 'other'`; keep the template entry one release, since a station rolled back to v3.95 needs it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_timing.py
def test_running_channels_counts_fresh_status_files(tmp_path):
    env = tmp_path / "mc"; env.mkdir(); p2 = tmp_path / "phase2"
    for ch, age in (("A", 30), ("B", 400)):
        (env / f"{ch}.env").write_text("FREQUENCY_HZ=1\n")
        (p2 / ch).mkdir(parents=True); s = p2 / ch / "status.json"; s.write_text("{}")
        os.utime(s, (1000 - age, 1000 - age))
    assert metrology_running_channels(env, p2, unit_active=True, now=1000.0) == 1
    assert metrology_running_channels(env, p2, unit_active=False, now=1000.0) == 0

def test_metrology_remedy_starts_the_station_unit():
    calls = []
    run = fake_run(calls, active={"timestd-metrology-station.service": "inactive"})
    out = _start_missing_metrology(run, dry_run=False)
    assert ["systemctl", "start", "timestd-metrology-station.service"] in calls
    assert "timestd-metrology-station" in out
```

`tests/test-site-timing-metrology.sh`: add two cases, run against a PATH shim for `systemctl` that logs calls.  (a) With the station unit file present: the log shows `enable --now timestd-metrology-station.service` before any `disable --now timestd-metrology@`.  (b) With it absent: the log shows the per-instance `enable --now timestd-metrology@...` lines and no station call.  Extract the enable block the way the script already extracts the generator, or move it into a shell function the test can source.

Add the `test_cpu.py` assertion for `timestd-metrology-station.service`, as in Task 4.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /home/mjh/hamsci/repos/sigmond && .venv/bin/pytest -o addopts="" tests/test_timing.py tests/test_cpu.py -v; bash tests/test-site-timing-metrology.sh`
Expected: FAIL / ✗.

- [ ] **Step 3: Implement** the five edits.  Keep `commands/timing.py` stdlib-only.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, plus `.venv/bin/pytest -o addopts="" tests/test_firstrun_retry.py tests/test_align_enables_units.py tests/test_drift_units.py -v`.
Expected: PASS.

- [ ] **Step 5: Mutation check**

In site-timing, move the instance `disable --now` before the station `enable --now`; case (a) must report ✗.  Restore.

- [ ] **Step 6: Commit (in sigmond)**

```bash
git add bin/sigmond-site-timing bin/sigmond-firstrun-bringup lib/sigmond/commands/timing.py \
  lib/sigmond/tui/format.py lib/sigmond/cpu.py tests/test_timing.py tests/test_cpu.py tests/test-site-timing-metrology.sh
git commit -m "timing: sigmond starts, counts and repairs the one metrology unit; never stops instances before it runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z"
```

**Rollback runbook (Stage 3, and Stage 1).**  `smd restore` to v3.95 disables the units v3.96 added but leaves running services running (`sigmond/bin/smd`, `_restore_retire_units`).  So after a restore, on the station (as root): `systemctl stop timestd-metrology-station.service timestd-authority-manager.service`, then `systemctl restart timestd-fusion.service`, then `systemctl start 'timestd-metrology@*'` for each env file, or re-run `sigmond-site-timing`.  Until the two new units stop, the old fusion and the authority unit both write `authority.json`, and the station host holds every channel's lock, so the restored template instances cannot start.  Write these lines into the v3.96 release notes.

**Gate G3.**  `smd align` B4 alone, then run Task 12's window W3.  Then sigma.  If W3 fails its criteria, revert Tasks 8-11 from the v3.96 branch and ship Stages 1-2.

---

## Task 12: measure on B4, before and after each stage

Read-only and light.  Every command runs from the devbox and reaches B4 through `fleet-ssh`, which resolves the reach from `ops/fleet.toml`.  Save each output under `/home/mjh/hamsci/tasks/hf-timestd-audit/b4/` as `v396-W<n>-<what>.txt`.  The 2026-11-23 snapshot in that folder serves as the older baseline for memory and CPU.

Windows: **W0** in the 24 hours before Stage 1 reaches B4; **W1** 24 hours after Stage 1; **W2** 24 hours after Stage 2; **W3** 24 hours after Stage 3.  Each window takes a start snapshot (step 1), an end snapshot 24 hours later (step 1 again), and steps 2-5 at the end.

- [ ] **Step 1: Unit counters (start and end of the window)**

```bash
~/hamsci/ops/bin/fleet-ssh b4 'date -u +%FT%TZ; systemctl show -p Id,NRestarts,MemoryCurrent,MemoryPeak,CPUUsageNSec,ActiveState,ActiveEnterTimestamp timestd-core-recorder.service timestd-authority-manager.service timestd-fusion.service timestd-l2-calibration.service timestd-metrology-station.service $(systemctl list-units --plain --no-legend "timestd-metrology@*" | cut -d" " -f1)' \
  > ~/hamsci/tasks/hf-timestd-audit/b4/v396-W0-units-start.txt
```

CPU over the window equals the difference in `CPUUsageNSec` divided by 1e9, per unit; for metrology before Stage 3, the sum over the six instances.  A unit that restarted inside the window resets its counters; `NRestarts` says so, and that window must be repeated.

- [ ] **Step 2: Journal volume over the window**

```bash
~/hamsci/ops/bin/fleet-ssh b4 'for u in timestd-fusion timestd-authority-manager "timestd-metrology*"; do printf "%s " "$u"; journalctl -q -u "$u" --since "24 hours ago" -o cat | wc -l; done; printf "WAM-IPE updates "; journalctl -q -u "timestd-metrology*" --since "24 hours ago" -o cat | grep -c "WAM-IPE grid updated"' \
  > ~/hamsci/tasks/hf-timestd-audit/b4/v396-W0-journal.txt
```

- [ ] **Step 3: Authority continuity and tier steadiness**

```bash
~/hamsci/ops/bin/fleet-ssh b4 'sqlite3 -readonly /var/lib/timestd/authority_history.db "SELECT t_level_active, count(*) FROM authority_snapshot WHERE utc_published >= strftime(\"%Y-%m-%dT%H:%M:%S\",\"now\",\"-1 day\") GROUP BY 1; SELECT count(*), max(gap) FROM (SELECT (julianday(utc_published) - julianday(lag(utc_published) OVER (ORDER BY utc_published))) * 86400 AS gap FROM authority_snapshot WHERE utc_published >= strftime(\"%Y-%m-%dT%H:%M:%S\",\"now\",\"-1 day\")) WHERE gap > 90;"; stat -c "%n %Y" /run/hf-timestd/authority.json /run/hf-timestd/registration.json; date +%s' \
  > ~/hamsci/tasks/hf-timestd-audit/b4/v396-W0-authority.txt
```

If the nested quoting fails through the two shells, stage the SQL in a file under the hamsci user's home on B4 and run `sqlite3 -readonly <db> < file`; do not change the query.

- [ ] **Step 4: Metrology output per channel (all windows) and lag (W3)**

```bash
~/hamsci/ops/bin/fleet-ssh b4 'for c in SHARED_2500 SHARED_5000 SHARED_10000 SHARED_15000 WWV_20000 WWV_25000; do printf "%s " $c; sqlite3 -readonly /var/lib/timestd/phase2/timestd.db "SELECT count(*) FROM L2_tick_timing WHERE channel=\"$c\" AND timestamp_utc >= strftime(\"%Y-%m-%dT%H:%M:%S\",\"now\",\"-1 day\");"; done; grep -h -E "\"channel\"|last_minute_lag_s|thread_restarts" /var/lib/timestd/phase2/*/status.json' \
  > ~/hamsci/tasks/hf-timestd-audit/b4/v396-W0-metrology.txt
```

The query uses the `(channel, timestamp_utc)` index, one channel at a time, so it stays light on the 29 GB file.

- [ ] **Step 5: Fusion's position line (W2 and W3)**

```bash
~/hamsci/ops/bin/fleet-ssh b4 'journalctl -q -u timestd-fusion --since "24 hours ago" -o cat | grep -m1 "Fusion station position"'
```

Expected: `Fusion station position: grid <6 chars> ...`, with no digits of a coordinate.

- [ ] **Step 6: Fleet position check before Stage 2 leaves B4 and sigma**

For each station in `ops/fleet.toml` that runs hf-timestd, one at a time:

```bash
~/hamsci/ops/bin/fleet-ssh <station> 'sudo -n /opt/git/sigmond/hf-timestd/venv/bin/python -c "import tomllib; s=tomllib.load(open(\"/etc/hf-timestd/timestd-config.toml\",\"rb\")).get(\"station\",{}); print(\"precise\" if (s.get(\"latitude\") or s.get(\"longitude\")) else \"no-precise\", \"grid\" if str(s.get(\"grid_square\",\"\")).strip(\"<>\").strip() and not str(s.get(\"grid_square\",\"\")).startswith(\"<\") else \"no-grid\")"'
```

It prints two words and no coordinate.  A station reading `no-precise no-grid` (N8UR carries a 0,0 location in the ledger) needs its identity set through `smd config` before Stage 2 reaches it.

- [ ] **Step 7: Pass criteria, per window**

| Window | Must hold |
|---|---|
| W1 | `authority.json` age under 60 s at both ends; no authority gap over 90 s except the one deploy restart; the `t_level_active` distribution matches W0's shape (no new tier appearing more than a handful of minutes); `timestd-authority-manager` `NRestarts=0` and `MemoryPeak` under 150 MB; fusion `MemoryCurrent` no higher than W0. |
| W2 | fusion CPU-seconds per 24 h at most a quarter of W0's; fusion journal lines at most a fifth of W0's; the position line names a grid; authority criteria of W1 still hold. |
| W3 | metrology `MemoryCurrent` at most half of W0's six-instance sum (the audit measured 1.45 GB); metrology CPU-seconds no higher than W0's sum; `WAM-IPE grid updated` about one sixth of W0's count; `L2_tick_timing` rows per channel within 5 % of W0's; every channel's `last_minute_lag_s` under 30 s and `thread_restarts` 0; `NRestarts=0`; authority criteria of W1 still hold. |

A window that fails sends its stage back; the gate text under each stage says how.  Set `MemoryHigh` and `MemoryMax` of the station unit to 1.5 and 2 times W3's `MemoryPeak` in a follow-up commit before v3.96 leaves the canaries.

- [ ] **Step 8: Record**

Append the three tables (W0 against W1, W2, W3) to `/home/mjh/hamsci/tasks/hf-timestd-audit/ASSESSMENT.md` under a new heading "v3.96 measurements", update the `hf-timestd-audit` item in `~/hamsci/ops/ledger/` (`state`, `updated`), and post a one-paragraph summary to the claude-bus.
