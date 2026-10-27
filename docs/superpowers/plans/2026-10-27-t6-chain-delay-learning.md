# T6 Chain-Delay Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The offset judge learns, stores and enforces each station's T6 chain delay, so a healthy T6 gets adopted on B4 and ND alike and any change in that delay raises an alarm.

**Architecture:** A robust `LabelPlaneTracker` measures the label − host offset. A new `ChainDelayStore` holds the station's record on disk. The judge uses the stored value as the expected plane term for T6 pairs at the single choke point `_cross_bench_delta_ns`, widens the band by the stored spread, re-checks a held T6 every tick, and latches T6 off on a sustained change. A CLI hands seed, accept and relearn requests to the running judge through a request file.

**Tech Stack:** Python 3.11, stdlib, pytest.  Tests run with `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-10-27-t6-chain-delay-learning-design.md`

## Global Constraints

- The learned value never corrects a timestamp, the chrony feed or the T6 anchor.  It enters only `_cross_bench_delta_ns` and the band arithmetic.
- `chain_delay_learn = false` keeps today's gate and adoption behaviour.  The tracker fixes in Task 1 apply either way.
- Config keys, in `[timing.offset_judge]`: `chain_delay_learn` (true), `chain_delay_learn_s` (7200), `chain_delay_outlier_ns` (5e6), `chain_delay_change_sustain_s` (600), `chain_delay_max_ns` (4e7), `chain_delay_path` (`/var/lib/timestd/state/t6_chain_delay.json`).
- Band: `cross_bench_k × sqrt(σ_c² + σ_l² + σ_stored² + spread²)`, with σ_c and σ_l capped by `cross_bench_sigma_ceiling_ms`.  The change alarm and the held-T6 check use this same band.
- Gross bound: refuse T6 when `|delta_raw| > chain_delay_max_ns + cross_bench_k × σ_l` (σ_l capped).
- Record history: at most 10 entries, newest last.
- One class per file; type hints; match the comment density of `offset_judge.py`.
- CRITICAL lines repeat at most hourly for a standing condition (the change alarm, the learning-phase conflict).
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01H4HYgCZ3dRtK4XWA1Xyoc3`

## Rulings made while planning (spec silent or in conflict)

- **CLI name.**  `hf-timestd calibrate` already exists (fusion calibration, required `--calib-file`).  The new commands become `hf-timestd chain-delay show|seed|accept|relearn`.  Task 7 updates the spec's text to match.
- **Outlier cut.**  A fixed 5 ms cut would discard a population that genuinely wanders ±6 ms (`test_the_centre_is_known_better_than_the_term_wanders`).  The cut becomes `max(chain_delay_outlier_ns, 4 × robust_spread)`, computed from the window before the cut.
- **No write races.**  Only the judge writes the record.  The CLI writes a request file beside it (`<record>.request.json`), which the judge consumes on its next tick.
- **Learner lost on restart.**  The learner lives in memory.  A restart mid-learn starts the learn again; the stored record survives.
- **Tests never touch `/var/lib`.**  `offset_judge.DEFAULT_CHAIN_DELAY_PATH` holds the production default, and Task 4 adds an autouse fixture to `tests/conftest.py` (and `tests/unit/conftest.py` if that exists) that points it at `tmp_path`.
- **Which tracker backs what.**  With `chain_delay_learn = false`, the existing single tracker (`_label_plane`) keeps today's role exactly: every label reading feeds it and it backs every plane term.  With it true, `_label_plane` takes only non-T6 label readings (T3), T6 feeds the learner and witness, and the T6 plane term comes from the record or the config value.  Existing plane-correction tests that feed T6 into `_label_plane` get `chain_delay_learn=False` in Task 5.
- **Health without a provider.**  A T6 reading without `detail["t6_health"]` never feeds the learner or the witness.

## Review Focus

1. A corrupt or unreadable record file: the judge logs one ERROR, moves the file aside to `<path>.corrupt-<utc>`, runs as if no record exists, and keeps ticking.  (Task 2 test.)
2. The state directory missing or read-only: saving fails, the judge logs once and keeps judging from the in-memory record.  (Task 2 test.)
3. A request with nonsense values (offset beyond the gross bound, negative sigma, unknown action): refused with a WARNING, the request file removed, the record untouched.  (Task 6 test.)
4. A station with no T6 at all (fusion-only DASI): no T6 readings, no learning, no record written, gate unchanged.  (Task 4 test.)
5. The anchor re-captured mid-window (a jump of hundreds of ms in one T6 reading): the gross bound drops it before any tracker sees it, and it never counts toward the held-T6 departure.  (Task 4 test.)

---

### Task 1: Robust tracker statistics and the extrapolation fix

**Files:**
- Modify: `src/hf_timestd/core/label_plane.py` (`LabelPlaneEstimate`, `LabelPlaneTracker.__init__`, `estimate`)
- Modify: `src/hf_timestd/core/offset_judge.py` (`observe_label_plane`, ~line 1764)
- Test: `tests/unit/test_label_plane_tracker.py`, `tests/test_offset_judge_plane_correction.py`

**Interfaces:**
- Produces: `LabelPlaneTracker(window_s, min_n, max_host_sigma_ns, correlation_time_s, host_bias_floor_ns, outlier_ns: float = 5_000_000.0)`; `LabelPlaneEstimate` gains `dropped: int = 0`.  `spread_ns` now means 1.4826 × MAD of the kept observations.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_label_plane_tracker.py`:

```python
def test_one_wild_reading_does_not_wreck_the_spread():
    t = LabelPlaneTracker(min_n=8, window_s=1e9)
    for i in range(62):
        off = TRANSPORT_NS + (0.8 * MS if i % 2 else -0.8 * MS)
        t.observe(label_utc=1.0 + off / 1e9, host_utc=1.0,
                  host_sigma_ns=20_000.0, mono=float(i * 10))
    t.observe(label_utc=1.0 + (-298 * MS) / 1e9, host_utc=1.0,
              host_sigma_ns=20_000.0, mono=630.0)
    est = t.estimate()
    assert est.dropped == 1
    assert est.spread_ns < 2 * MS
    assert est.offset_ns == pytest.approx(TRANSPORT_NS, abs=1 * MS)
    assert est.sigma_ns < 5 * MS

def test_the_cut_scales_with_a_population_that_really_wanders():
    t = LabelPlaneTracker(min_n=4, window_s=1e9)
    for i in range(200):
        off = TRANSPORT_NS + (6 * MS if i % 2 else -6 * MS)
        t.observe(label_utc=1.0 + off / 1e9, host_utc=1.0,
                  host_sigma_ns=20_000.0, mono=float(i))
    assert t.estimate().dropped == 0
```

In `tests/test_offset_judge_plane_correction.py`:

```python
def test_observe_brings_both_readings_to_one_instant(tmp_path):
    # chain_delay_learn=False: this exercises the legacy single tracker,
    # which Task 5 keeps only for that mode.  The key is ignored until Task 4.
    j = make_judge(tmp_path, label_plane_min_n=1, chain_delay_learn=False)
    T = 1_700_000_000.0
    for i in range(10):
        # label read 0.3 s before the host; both say plane offset −16.5 ms
        label = reading("T6", T + i - 0.3 - 0.0165, 100.0 + i - 0.3, plane="label")
        host = reading("T4", T + i, 100.0 + i, sigma_ns=20_000.0)
        j.observe_label_plane(label, host)
    est = j._label_plane.estimate()
    assert est.offset_ns == pytest.approx(-16_500_000.0, abs=1_000.0)
```

- [ ] **Step 2: Run, expect FAIL**

Run: `uv run pytest tests/unit/test_label_plane_tracker.py tests/test_offset_judge_plane_correction.py -q`
Expected: the three new tests fail (`dropped` missing; offset reads about −316.5 ms).

- [ ] **Step 3: Implement.**  In `estimate()`: median of all offsets; raw spread = 1.4826 × median(|o − median|); cut = max(`outlier_ns`, 4 × raw spread); keep offsets within the cut of the median; if fewer than `min_n` remain return `None`; recompute median and MAD spread on the kept set; `dropped` = removed count; the sigma formula stays.  In `observe_label_plane`, pass `label_utc=label.utc_at(host.mono)`.  Update the module docstring's spread description and the comment block above `test_the_centre_is_known_better…` (pstdev → MAD).

- [ ] **Step 4: Run the whole judge and tracker suite**

Run: `uv run pytest tests/unit/test_label_plane_tracker.py tests/test_offset_judge*.py tests/unit/test_*judge*.py -q`
Expected: all pass, `test_a_t6_step_is_not_absorbed_immediately` included.

- [ ] **Step 5: Commit** `judge: robust plane-tracker spread; observe at one instant`

---

### Task 2: `ChainDelayStore`

**Files:**
- Create: `src/hf_timestd/core/chain_delay_store.py`
- Test: `tests/unit/test_chain_delay_store.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class ChainDelayRecord:
    offset_ns: float; sigma_ns: float; spread_ns: float
    n: int; n_eff: float; span_s: float
    learned_from_utc: str; learned_to_utc: str
    lower_tier: str
    state: str                      # "seeded" | "confirmed"

class ChainDelayStore:
    SCHEMA = 1
    HISTORY_MAX = 10
    def __init__(self, path: os.PathLike, identity: Dict[str, float],
                 time_fn: Callable[[], float] = time.time): ...
    @property
    def path(self) -> Path: ...
    @property
    def request_path(self) -> Path: ...          # <path>.request.json
    def load(self) -> None                         # never raises
    @property
    def record(self) -> Optional[ChainDelayRecord]
    @property
    def candidate(self) -> Optional[ChainDelayRecord]
    def set_record(self, rec: ChainDelayRecord) -> bool       # old → history; saves
    def set_candidate(self, rec: Optional[ChainDelayRecord]) -> bool
    def promote_candidate(self) -> bool                       # candidate → record
    def discard(self) -> bool                                 # record+candidate → history
    def take_request(self) -> Optional[Dict]                  # read + delete request file
    @staticmethod
    def write_request(path: os.PathLike, request: Dict) -> None   # atomic, for the CLI
    def to_status(self) -> Dict                               # for offset_judge.json
```

`identity` holds `t6_frequency_hz`, `t6_sample_rate`, `low_edge_hz`, `high_edge_hz`.  The `bool` returns say whether the save reached disk.

- [ ] **Step 1: Write the failing tests:**
  - `test_round_trip`: `set_record` then a fresh store on the same path `load()`s the same record and `state`.
  - `test_identity_change_sets_the_record_aside`: a store with a different `t6_frequency_hz` loads `record is None`, the old record sits last in the file's `history`, and one WARNING appears (`caplog`).
  - `test_history_keeps_ten`: twelve `set_record` calls leave `len(history) == 10`.
  - `test_corrupt_file_is_moved_aside`: a file holding `"{not json"` loads `record is None`, a sibling named `t6_chain_delay.json.corrupt-*` exists, one ERROR logged.
  - `test_unwritable_dir_keeps_the_record_in_memory`: path under a `chmod 0o500` directory; `set_record` returns `False`, `record` still returns the record, one ERROR logged, a second failure logs nothing new.
  - `test_atomic_write_leaves_no_temp_file`.
  - `test_request_round_trip`: `write_request(store.request_path, {"action": "accept"})`, `take_request()` returns it and deletes the file; a second call returns `None`; an unreadable request file returns `None` and gets deleted.

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).

Run: `uv run pytest tests/unit/test_chain_delay_store.py -q`

- [ ] **Step 3: Implement** with `json`, `tempfile.NamedTemporaryFile(dir=parent, delete=False)` plus `os.replace`, and the file layout in spec §2 (`schema`, record fields at top level, `state`, `candidate`, `identity`, `history`).

- [ ] **Step 4: Run, expect PASS.**

- [ ] **Step 5: Commit** `judge: ChainDelayStore holds the station's T6 chain delay`

---

### Task 3: T6 health reaches the judge

**Files:**
- Modify: `src/hf_timestd/core/bpsk_pps_calibrator_mf.py` (re-home site, ~line 1105; `__init__`)
- Modify: `src/hf_timestd/core/offset_judge.py` (`NativeAnchorBench.__init__`, `poll`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (bench construction ~line 785; new `_t6_health_state`)
- Test: `tests/unit/test_t6_health_detail.py`

**Interfaces:**
- Produces: `BpskPpsCalibratorMF.rehome_count: int` (starts 0) and `BpskPpsCalibratorMF.last_rehome_mono: Optional[float]`, both set where the "chain-delay step adopted" WARNING fires (`time.monotonic()`).
- Produces: `NativeAnchorBench(provider, mono_fn=time.monotonic, health_provider: Optional[Callable[[], Optional[Dict]]] = None)`.  When it returns a dict, `poll()` copies it into `reading.detail["t6_health"]`.  The keys: `authority_state: str` (the `T6AuthorityState` name), `state_for_s: float`, `rehome_age_s: Optional[float]`.  Never use the key `authoritative`: `_adoptable` reads it.
- Produces: `CoreRecorderV2._t6_health_state() -> Optional[Dict]`, which remembers the last authority state and the mono time it changed, and reads the calibrator's `last_rehome_mono`.

- [ ] **Step 1: Write the failing tests:**
  - `test_rehome_counter_moves`: drive `_note_step_candidate` past `STEP_CONFIRM_EDGES` (follow the existing re-home test in `tests/` for the input shape; `grep -rn STEP_CONFIRM_EDGES tests`), assert `rehome_count == 1` and `last_rehome_mono is not None`.
  - `test_bench_carries_health`: a `NativeAnchorBench` with a stub provider (copy the stub from an existing NativeAnchorBench test; `grep -rn NativeAnchorBench tests`) and `health_provider=lambda: {"authority_state": "AUTHORITATIVE", "state_for_s": 700.0, "rehome_age_s": None}`; `poll().detail["t6_health"]["authority_state"] == "AUTHORITATIVE"`; `"authoritative" not in detail`.
  - `test_bench_without_health_provider_has_no_health`.

- [ ] **Step 2: Run, expect FAIL.**

Run: `uv run pytest tests/unit/test_t6_health_detail.py -q`

- [ ] **Step 3: Implement**, and wire `health_provider=self._t6_health_state` in the recorder.

- [ ] **Step 4: Run** the new file plus `tests/test_offset_judge*.py` and every test file that names `bpsk_pps_calibrator_mf`.  Expect PASS.

- [ ] **Step 5: Commit** `t6: expose re-home count and authority health to the judge`

---

### Task 4: The judge gates T6 against the stored record

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (constructor config; `_cross_bench_delta_ns`; `_cross_gate_ok_locked`; `_snapshot_locked`; new private helpers)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (pass identity from `[timing.t6_pps]` into the judge config)
- Test: `tests/test_offset_judge_chain_delay.py` (new; use the `FakeClock` / `reading()` / `make_judge()` pattern of `tests/test_offset_judge_plane_correction.py`, with `chain_delay_path=tmp_path / "cd.json"`)

**Interfaces:**
- Consumes: `ChainDelayStore`, `ChainDelayRecord` (Task 2).
- Produces: module constant `DEFAULT_CHAIN_DELAY_PATH = "/var/lib/timestd/state/t6_chain_delay.json"`, read at construction time; the autouse test fixture described under the rulings.  Config keys from Global Constraints, plus `chain_delay_identity: Dict` (the recorder fills it).  `OffsetJudge.chain_delay_store: Optional[ChainDelayStore]` (`None` when `chain_delay_learn` is false).  `OffsetJudge._chain_band_ns(cand: BenchReading, ref: BenchReading) -> float`.  `OffsetJudge._t6_gross_ok(cand, ref, delta_raw_ns: float) -> bool`.  Snapshot key `chain_delay`: `{"state": "none"|"seeded"|"confirmed", "record": {...}|None, "candidate": {...}|None, "changed": {...}|None, "learning": {...}|None}`.

Rules this task implements:
- For a pair where one side carries `plane == "label"` and tier `T6` and the other `plane == "host"`: when a record exists, the plane term comes from `record.offset_ns`; otherwise from today's path (`effective_label_plane_offset_ns()`, which Task 5 restricts to the T3 tracker).  Non-T6 pairs keep today's behaviour.
- `_cross_gate_ok_locked`, for a T6-versus-host pair: first the gross bound (on the raw delta, no plane term); then, with a record, `|delta| ≤ _chain_band_ns`; without a record, today's bound.

- [ ] **Step 1: Write the failing tests:**

```python
T = 1_700_000_000.0
SEED = dict(offset_ns=-16_350_000.0, sigma_ns=1_100_000.0, spread_ns=1_100_000.0,
            n=783, n_eff=100.0, span_s=49_000.0, learned_from_utc="x",
            learned_to_utc="y", lower_tier="T4", state="seeded")

def seeded_judge(tmp_path, **cfg):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"), **cfg)
    j.chain_delay_store.set_record(ChainDelayRecord(**SEED))
    return j

def test_record_in_force_admits_t6_at_its_chain_delay(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T - 0.0170, 1.0, sigma_ns=900_000.0, plane="label")
    t4 = reading("T4", T, 1.0, sigma_ns=200_000.0)
    assert j._cross_gate_ok_locked(t6, t4, 1.0)

def test_record_in_force_refuses_t6_far_from_it(tmp_path):
    j = seeded_judge(tmp_path)
    t6 = reading("T6", T - 0.0320, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._cross_gate_ok_locked(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), 1.0)

def test_gross_bound_refuses_without_a_record(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    t6 = reading("T6", T - 0.298, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._t6_gross_ok(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), -298e6)

def test_bootstrap_adopts_t6_with_a_record(tmp_path):
    j = seeded_judge(tmp_path)
    readings = [reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label"),
                reading("T4", T, 1.0, sigma_ns=200_000.0)]
    assert j._select_bench_locked(readings).tier == "T6"

def test_record_survives_a_new_judge(tmp_path):
    seeded_judge(tmp_path)
    j2 = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    assert j2.chain_delay_store.record.offset_ns == SEED["offset_ns"]

def test_learn_off_is_todays_gate(tmp_path):
    j = make_judge(tmp_path, chain_delay_learn=False)
    assert j.chain_delay_store is None
    t6 = reading("T6", T - 0.0165, 1.0, sigma_ns=900_000.0, plane="label")
    assert not j._cross_gate_ok_locked(t6, reading("T4", T, 1.0, sigma_ns=200_000.0), 1.0)

def test_station_without_t6_writes_no_record(tmp_path):
    j = make_judge(tmp_path, chain_delay_path=str(tmp_path / "cd.json"))
    for _ in range(5):
        j._select_bench_locked([reading("T4", T, 1.0, sigma_ns=200_000.0),
                                reading("T3", T, 1.0, sigma_ns=2e6)])
    assert not (tmp_path / "cd.json").exists()

def test_snapshot_publishes_the_record(tmp_path):
    j = seeded_judge(tmp_path)
    snap = j._snapshot_locked(1.0)
    assert snap["chain_delay"]["state"] == "seeded"
    assert snap["chain_delay"]["record"]["offset_ns"] == SEED["offset_ns"]
```

- [ ] **Step 2: Run, expect FAIL.**

Run: `uv run pytest tests/test_offset_judge_chain_delay.py -q`

- [ ] **Step 3: Implement.**  Build the store in the constructor when `chain_delay_learn` is true, calling `load()`.  Add the plane-term selection to `_cross_bench_delta_ns`, plus the gross bound and band to `_cross_gate_ok_locked`.  Keep the existing CRITICAL log text, and add the stored offset and band to it when a record exists.  Add the `chain_delay` snapshot block.  In the recorder, build `chain_delay_identity` from `[timing.t6_pps]` (`frequency_hz`, `sample_rate`, `low_edge_hz`, `high_edge_hz`).

- [ ] **Step 4: Run** the new file plus every judge test (`tests/test_offset_judge*.py tests/unit/test_*judge*.py tests/unit/test_one_gate_four_surfaces.py`).  Expect PASS.

- [ ] **Step 5: Commit** `judge: gate T6 against the station's stored chain delay`

---

### Task 5: Learning, seeding, and the quieter conflict log

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (constructor; `_observe_label_plane_locked`; `label_plane_status`; `effective_label_plane_offset_ns`; `_cross_gate_ok_locked` logging; new helpers)
- Test: `tests/test_offset_judge_chain_delay.py`

**Interfaces:**
- Consumes: Task 2 store, Task 3 `detail["t6_health"]`, Task 4 snapshot block.
- Produces: `OffsetJudge._t6_learner: LabelPlaneTracker` (window `1.05 × chain_delay_learn_s`, `outlier_ns = chain_delay_outlier_ns`) and `OffsetJudge._t6_witness: LabelPlaneTracker` (600 s window, same cut).  `_label_plane` keeps its name and follows the "which tracker backs what" ruling.  Existing tests in `tests/test_offset_judge_plane_correction.py` that feed T6 readings to `observe_label_plane` gain `chain_delay_learn=False`.  `OffsetJudge._t6_healthy(r: BenchReading) -> bool`.

Rules:
- `_t6_healthy`: `t6_health.authority_state == "AUTHORITATIVE"`, `state_for_s ≥ 600`, `rehome_age_s` is `None` or `≥ 600`, and `detail["anchor_tier"] == "T6"`.
- Each tick, a T6 label reading that passes `_t6_healthy` and the gross bound feeds both `_t6_learner` and `_t6_witness`, paired with the tightest host-plane reading at `host.mono`.  T3 label readings feed only `_t3_plane`.
- When `_t6_learner.estimate()` reports `span_s ≥ chain_delay_learn_s` and `sigma_ns ≤ MAX_PLANE_SIGMA_NS`, and no record exists or the record is `seeded`: write it as `confirmed` (log one WARNING with offset, sigma, spread, n_eff).  When it replaces a seed whose band it falls outside, also log a WARNING naming both values.  Once confirmed, the learner stops writing (Task 6 handles candidates).
- With no record and T6 refused by the gate: the CRITICAL conflict line fires once, then hourly, and names the learning progress (`span_s` of `chain_delay_learn_s`).  With a record, today's 60 s cadence stays.
- Snapshot `chain_delay.learning`: `{"span_s", "n", "target_s"}` while learning.

- [ ] **Step 1: Write the failing tests** (a helper `feed(j, clock, minutes, offset_ns, health=HEALTHY)` advances the clock 10 s a tick and calls `j._observe_label_plane_locked([t6, t4])`):
  - `test_two_healthy_hours_write_a_confirmed_record` (offset −16.35 ms ± 0.8 ms alternating; `record.state == "confirmed"`, offset within 0.2 ms).
  - `test_unhealthy_ticks_do_not_feed` (each of: `DEGRADED`; `state_for_s=100`; `rehome_age_s=60`; `anchor_tier="T5"`; no `t6_health` → learner `n == 0`).
  - `test_learn_replaces_a_seed_without_accept` (seed −16.35, learn −16.0 → confirmed −16.0, no WARNING about the seed band).
  - `test_learn_far_from_the_seed_warns` (seed −16.35, learn −24.0 → confirmed −24.0, one WARNING naming both).
  - `test_t3_readings_stay_out_of_the_t6_learner`.
  - `test_learning_conflict_logs_once_then_hourly` (`caplog`, 2 h of refused T6 → exactly 2 CRITICAL conflict lines; with a record, a refused T6 still logs at 60 s).
  - `test_anchor_recapture_reading_never_reaches_a_tracker` (one −298 ms reading among healthy ones → `_t6_witness.estimate().dropped == 0` and `n` unchanged by it).

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full judge suite; expect PASS.
- [ ] **Step 5: Commit** `judge: learn the T6 chain delay from a healthy T6, seed then confirm`

---

### Task 6: Change alarm, held-T6 check, latch, candidate, requests

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (`tick`; `_select_bench_locked`; new helpers)
- Test: `tests/test_offset_judge_chain_delay.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5.
- Produces: `OffsetJudge._chain_changed: Optional[Dict]` (`{"stored_ns", "live_ns", "since_utc"}`), published as `chain_delay.changed`.  `OffsetJudge._apply_chain_request_locked(req: Dict) -> None`.

Rules:
- **Change alarm:** with a record, when `_t6_witness.estimate()` exists and `|live − stored| > band` (the band with σ_live in place of σ_c and no σ_l) holds for `chain_delay_change_sustain_s`, set `_chain_changed`.  Log CRITICAL `T6 CHAIN DELAY CHANGED: stored X ms, now Y ms ± Z ms` once, then hourly.  Start a fresh candidate learner (same rules as `_t6_learner`).  When it completes, `store.set_candidate(...)`.
- **Latch:** while `_chain_changed` holds, T6 readings are not adoptable.  When the live estimate returns inside the band and stays there for `chain_delay_change_sustain_s`, clear it, log one WARNING, and drop the candidate learner (the candidate already on disk stays).
- **Held-T6 check:** while the judge holds T6 and a record exists, every tick tests the held T6 against the gross bound and the band, using the tightest host-plane reading.  A departure lasting `sustain_window_s` drops T6: the judge re-selects with `_chain_consistent_locked` over the non-T6 readings.  A reading the gross bound refuses counts as absent for that tick, not as a departure.
- **Requests** (`store.take_request()` once per tick, before selection):
  - `{"action": "seed", "offset_ns", "sigma_ns", "spread_ns"}` → record with `state="seeded"`, `lower_tier="T4"`, `n=0`.  Refused when `|offset_ns| > chain_delay_max_ns`, or either sigma ≤ 0.
  - `{"action": "accept"}` → refused unless a candidate exists with `span_s ≥ chain_delay_learn_s` and `sigma_ns ≤ MAX_PLANE_SIGMA_NS`.  Otherwise `promote_candidate()`, clear `_chain_changed`.
  - `{"action": "relearn"}` → `discard()`, clear `_chain_changed`, reset the learners.
  - Anything else → WARNING, ignored.  Every refusal logs one WARNING naming the reason.

- [ ] **Step 1: Write the failing tests:**
  - `test_sustained_shift_alarms_and_drops_t6` (confirmed −16.35; feed −22.5 ms for 11 min → `_chain_changed` set, the selected tier is not T6, one CRITICAL).
  - `test_one_bad_minute_does_not_alarm` (6 ticks at −22.5 inside healthy hours → no alarm).
  - `test_return_to_band_clears_the_latch`.
  - `test_shift_writes_a_candidate_after_a_full_learn_and_keeps_the_record` (record unchanged; `store.candidate.offset_ns ≈ −22.5 ms`).
  - `test_accept_promotes_the_candidate`; `test_accept_refuses_a_short_candidate`.
  - `test_relearn_discards_both`.
  - `test_seed_request_with_absurd_values_is_refused` (offset −300 ms; sigma 0; action "frobnicate"), each leaving the record unchanged and the request file gone.
  - `test_held_t6_is_rechecked_and_dropped` (judge on T6, residual moves 15 ms off the record for 70 s → tier drops to T4).
  - `test_held_t6_survives_a_single_gross_outlier`.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full judge suite; expect PASS.
- [ ] **Step 5: Commit** `judge: alarm on a T6 chain-delay change, never absorb it`

---

### Task 7: Mark the label step, the CLI, config and docs

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (`OffsetVerdict` fields; `_verdict_locked`; tier-change log in `tick`)
- Modify: `src/hf_timestd/core/binary_archive_writer.py` (`_legacy_timing_keys`, ~line 535)
- Modify: `src/hf_timestd/cli.py` (new `chain-delay` subcommand)
- Modify: `config/timestd-config.toml.template` (`[timing.offset_judge]` keys, commented, with defaults)
- Modify: `docs/superpowers/specs/2026-10-27-t6-chain-delay-learning-design.md` (CLI name), `docs/OFFSET-JUDGE-SPEC-2026-08-05.md` (one short section pointing at the design)
- Test: `tests/unit/test_sidecar_label_plane.py` (extend), `tests/test_offset_judge_chain_delay.py`, `tests/test_cli_chain_delay.py` (new)

**Interfaces:**
- Produces: `OffsetVerdict.chain_delay_ns: Optional[float] = None`, `OffsetVerdict.chain_delay_state: Optional[str] = None`, both filled from the record in force.  Sidecar `timing` gains `chain_delay_ns` and `chain_delay_state`.
- Produces: `tier_transitions.jsonl` beside the record path: one JSON line per change of the adopted tier, `{"utc", "from", "to", "delta_ns", "chain_delay_ns"}`, where `delta_ns` takes the shadow residual of the new tier against the old.
- Produces: `hf-timestd chain-delay show [--json]` (reads the record file and `offset_judge.json`), `seed --offset-ms F --sigma-ms F --spread-ms F`, `accept`, `relearn`.  The last three call `ChainDelayStore.write_request` on `<chain_delay_path>.request.json`, with the path read from the config's `[timing.offset_judge]`.  Each prints what it requested and that the judge acts on its next tick.

- [ ] **Step 1: Write the failing tests:**
  - sidecar: a verdict with `chain_delay_ns=-16.35e6, chain_delay_state="confirmed"` puts both keys in the timing block; a verdict without them writes `None`.
  - transitions: T4 → T6 adoption appends exactly one line with `from == "T4"`, `to == "T6"`; a tick with no change appends nothing.
  - CLI: `main(["chain-delay", "seed", "--offset-ms", "-16.35", "--sigma-ms", "1.1", "--spread-ms", "1.1", "--config", cfg])` writes a request whose `offset_ns == -16_350_000.0`; `show --json` on a seeded file prints `"state": "seeded"`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, the config template and the two doc edits included.
- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest tests -q -p no:cacheprovider --override-ini="addopts="`
Expected: no new failures against `main` before Task 1 (record the baseline first).
- [ ] **Step 5: Commit** `judge: mark the T6 label step; hf-timestd chain-delay CLI`

---

## After the build (not tasks for the implementer)

Deploy follows the ruling: seed, then learn.
1. Announce on the bus and to the GRAPE users: B4's labels step about 16 ms from T4 to T6 at a stated time.
2. Deploy to B4 by git fast-forward and restarting the two timestd units (never `smd apply`).
3. `sudo hf-timestd chain-delay seed --offset-ms -16.35 --sigma-ms 1.1 --spread-ms 1.1` on B4.  ND: take the seed from its judge's shadow residual at deploy time.
4. Record the actual step time from `tier_transitions.jsonl` and post it.
