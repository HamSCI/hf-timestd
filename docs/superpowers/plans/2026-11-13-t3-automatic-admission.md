# T3 Automatic Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each station decides for itself, minute by minute and from evidence, whether T3 may label its data, and stops when T3 no longer beats T2.  Later, fleet calibration and transmitter offsets from sigmondb extend that decision to stations without a reference.

**Architecture:** The authority manager already builds one shadow row a minute.  A pure policy (`T3AdmissionPolicy`) reads that row, keeps a rolling window, and returns a decision with a route and a reason.  The manager publishes the decision as a `t3_admission` block in `authority.json`.  Every other process (recorder, judge, metrology) stops reading a once-per-process flag and instead asks a small gate (`T3AdmissionGate`) that reads that block.  A clock-stability estimator, fed by T3's own phase in the sample plane, sets the window, the hysteresis and the judge's clock term.  Phase B adds a client for the published sigmondb tables behind a protocol that a fake satisfies, so the code can be built and tested before the server exists.

**Tech Stack:** Python ≥ 3.10, numpy, pytest via `uv run`, `requests` (already a dependency) for the ClickHouse HTTP interface, hamsci-dsp `CounterMap`.

**Spec:** `docs/superpowers/specs/2026-11-12-t3-automatic-admission-design.md` (the authority).  It revises ruling B of `docs/superpowers/specs/2026-10-30-t3-restoration-design.md` (§8 step 7, §9).  Read both.

## Global Constraints

- Timing-authority invariant (hf-timestd `CLAUDE.md`): no new `time.time()`, `datetime.now()` or `chronyc tracking` in the timing path.  Every clock the new code needs arrives as an injected `now_fn` or `mono_fn`.  The admission window uses the shadow row's `utc_minute` stamp, which `AuthorityManager.now_fn` already supplies as a record stamp; it names minutes and never enters timing arithmetic.
- Expose, never correct.  T3 never steers chrony (MEASUREMENT_MODEL §7.1.1).  Nothing here adjusts the host clock or any offset; the station reports which tier it uses and why, and consumers decide.
- T3 never names the wrong second and never jumps by more than 0.5 s.  A jump withdraws it at once, whatever the sigmas or the evidence say (10-30 §9 criterion 4).
- The shadow record keeps running whether or not T3 holds admission.
- Admission compares by median and by 95th percentile of the absolute error, T3 against T2 (10-30 §9 criterion 6).  Sigma honesty means the rms of residual over sigma lies between 0.5 and 2 (§9 criterion 3).
- The setting: `[timing.t3] admission = "auto" | "off" | "on"`, default `"auto"`.  Old `admitted = true` reads as `"on"`; `admitted = false` reads as `"auto"`.
- Every new component that reads a file or the network never raises into the timing path; failure closes T3 (auto mode) and logs once per failure streak.
- Phase B code depends on servers that do not exist yet.  It reaches them only through the `SigmondbPublic` protocol (Task B1), and its tests use a fake.
- Tests run with `uv run pytest -q --override-ini addopts="-ra" --basetemp="$HOME/.cache/pytest-tmp/t3-admission" <path>` from the repo root.  Each new test is seen to fail before its code exists.
- Work on `main`.  Commit after each task.  Do not push.
- Every commit message ends with exactly these two lines:

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z
  ```

- Before any deploy to AC0G-B4, announce on the claude-bus (see `~/hamsci/CLAUDE.md`).  This change moves every station from shadow-only to automatic admission when it lands, B4 included.

### Builder choices this plan fixes (none comes from the spec)

The spec leaves the window statistics to the builder.  These defaults live as module constants in `t3_admission_policy.py` and `clock_stability.py`, each with a comment naming it a builder choice:

| Constant | Default | Why |
|---|---|---|
| `TAU0_S` | 60 | one shadow row a minute |
| `STABILITY_HISTORY_MIN` | 1440 | one day of phase points |
| `STABILITY_MIN_POINTS` | 120 | below this, stability reads "unmeasured" |
| `WINDOW_MIN_S` | 1800 | a p95 needs at least 20 samples to mean anything |
| `WINDOW_MAX_S` | 7200 | a longer window lags dawn and dusk by more than an hour |
| `HOLD_MIN_S` | 300 | |
| hold rule | `hold_s = max(HOLD_MIN_S, window_s // 4)` | see Decisions, note on hysteresis |
| `FILL_MIN_PAIRS` | 20 | and at least half the window's minutes |
| `JUMP_MS` | 500.0 | spec: 0.5 s |
| reference-loss hold | one `window_s` | spec silent |
| `CALIBRATION_MAX_AGE_S` (B) | 259200 (72 h) | spec: "over the last few days" |
| `CALIBRATION_MIN_MINUTES` (B) | 60 | |
| `FETCH_INTERVAL_S` (B) | 3600, jitter 0–300 s | spec: hourly to start |
| `OFFSET_MAX_CONFIRM_AGE_S` (B) | 604800 (7 days) | spec: offsets change over days |
| `OFFSET_AGE_SIGMA_MS_PER_H` (B) | 0.005 | copy age adds uncertainty |

## Review Focus

1. A metrology or recorder process starts while `authority.json` is missing, stale (the fusion service has died) or malformed.  The gate must answer "not admitted" in auto mode and never raise.  Test: `test_gate_closes_on_missing_stale_or_malformed_file` (Task A7).
2. The host clock steps back, so two shadow rows carry the same `utc_minute`.  The policy must not count that minute twice or change state twice.  Test: `test_a_repeated_minute_adds_no_sample` (Task A5).
3. radiod restarts mid-window.  T3's phase jumps by an arbitrary amount at the run boundary.  That step is not a T3 jump and must not withdraw admission or poison the stability estimate.  Tests: `test_a_run_change_is_not_a_jump` (A5), `test_run_boundaries_split_the_series` (A2).
4. A deploy restarts the fusion service.  The policy must warm-start from the shadow record, so a routine restart does not withhold T3 for half an hour; rows written before this change lack the new fields and must be skipped, not crash the seed.  Test: `test_seed_skips_old_rows_and_restores_the_window` (A6).
5. Chrony has no WAN source, or a probe returns NaN.  A minute without both errors must not count as a T3 win, and a station with no T2 at all must say so.  Tests: `test_nan_and_missing_offsets_yield_no_pair` (A3), `test_no_t2_says_so` (A5).

---

## Phase A — station-local, buildable now

### Task A1: The `admission` setting

**Files:**
- Modify: `src/hf_timestd/core/t3_admission.py`
- Modify: `config/timestd-config.toml.template:336-353` (replace the ruling-B comment and `admitted = false`)
- Modify: `src/hf_timestd/cli.py` (validate: a warn-level issue for the old key)
- Test: `tests/unit/test_t3_admission.py` (rewrite `test_the_switch_fails_closed`, `test_the_template_ships_the_switch_off`)

**Interfaces:**
- Produces:
  - `class AdmissionMode(str, Enum): AUTO = "auto"; OFF = "off"; ON = "on"`
  - `admission_mode_from_config(config: Optional[dict]) -> AdmissionMode`
  - `t3_admission_mode(config: Optional[dict] = None, *, path: Optional[Path] = None) -> AdmissionMode` (logs the mode once per process, replacing `t3_admitted`)
  - `ADMISSION_KEY = "admission"`; `ADMITTED_KEY = "admitted"` stays for the mapping.
- `admitted_from_config` and `t3_admitted` go away; Task A7 replaces every caller.

- [ ] **Step 1: Write the failing tests**

```python
def test_admission_mode_reads_the_new_key_and_maps_the_old():
    f = admission_mode_from_config
    assert f(None) is AdmissionMode.AUTO
    assert f({}) is AdmissionMode.AUTO
    assert f({"timing": {"t3": {"admission": "off"}}}) is AdmissionMode.OFF
    assert f({"timing": {"t3": {"admission": "on"}}}) is AdmissionMode.ON
    assert f({"timing": {"t3": {"admitted": True}}}) is AdmissionMode.ON
    assert f({"timing": {"t3": {"admitted": False}}}) is AdmissionMode.AUTO
    # The new key wins when both appear.
    assert f({"timing": {"t3": {"admission": "off", "admitted": True}}}) is AdmissionMode.OFF
    # Malformed values fall back to the default and log a WARNING.
    assert f({"timing": {"t3": {"admission": "onn"}}}) is AdmissionMode.AUTO
    assert f({"timing": {"t3": {"admitted": "true"}}}) is AdmissionMode.AUTO
    assert f({"timing": {"t3": "yes"}}) is AdmissionMode.AUTO

def test_the_template_ships_admission_auto():
    cfg = toml.load("config/timestd-config.toml.template")
    assert cfg["timing"]["t3"] == {"admission": "auto"}

def test_validate_warns_on_the_old_admitted_key():
    issues = retired_key_issues({"timing": {"t3": {"admitted": False}}})
    assert any("admitted" in i["message"] and "admission" in i["message"] for i in issues)
```

- [ ] **Step 2: Run them; expect ImportError on `AdmissionMode`.**
- [ ] **Step 3: Implement** in `t3_admission.py`; rewrite its module docstring to describe the three modes and drop the "flip both together" pairing note.  Add `(("timing.t3", "admitted"), 'replaced by [timing.t3] admission; true reads as "on", false as "auto"')` to `RETIRED_KEYS` in `cli.py`.  Rewrite the template comment block in plain prose: what each mode does, that `"on"` serves testing only, and that `authority.json`'s `t3_admission` block names the route and the reason each minute.
- [ ] **Step 4: Run; expect PASS.**  Other tests that import `t3_admitted` now fail; leave them for Task A7, which owns their rewrite, but note the list.
- [ ] **Step 5: Commit** `t3: [timing.t3] admission = auto|off|on, old admitted key mapped`.

### Task A2: Clock stability from T3's own phase

**Files:**
- Create: `src/hf_timestd/core/clock_stability.py`
- Test: `tests/unit/test_clock_stability.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class PhasePoint: utc_minute: int; phase_ms: float; run_id: float`
  - `phase_point_from_summary(summary: Optional[dict], utc_minute: int) -> Optional[PhasePoint]` — `phase_ms = (utc_ref − input_epoch_gps_s − input_index(rtp_ref) / input_samprate) × 1e3` through the summary's `CounterMap`; `run_id = counter_map.input_epoch_gps_s`.  Only differences within one run carry meaning; the constant offset (GPS−UTC, radiod's pair) cancels.  None when the summary lacks `utc_ref`, `rtp_ref` or a counter map, or is not `ACQUIRED`.
  - `@dataclass(frozen=True) class ClockStability: tau_cross_s: Optional[int]; window_s: int; hold_s: int; drift_ns_per_s: Optional[float]; n_points: int`
  - `class ClockStabilityEstimator: add(p: PhasePoint) -> None; stability() -> ClockStability; tdev_by_tau() -> Dict[int, float]` (τ in seconds → TDEV in ms)
- Method: modified Allan deviation per run segment, pooled across segments by sample count; `TDEV(τ) = τ · MDEV(τ) / √3`.  Evaluate τ = m·`TAU0_S` for m = 1, 2, 4, … while m ≤ n/4.  `tau_cross_s` = the τ that minimises TDEV (white phase noise falls as τ^-½, clock wander rises; the minimum marks the crossing).  When every TDEV < 1e-6 ms, `tau_cross_s` = the largest τ evaluated.  `window_s = clamp(tau_cross_s, WINDOW_MIN_S, WINDOW_MAX_S)`; `hold_s = max(HOLD_MIN_S, window_s // 4)`.  `drift_ns_per_s` = |least-squares slope of phase within the current run| + σ_y(`tau_cross_s`), both in ns/s.  Fewer than `STABILITY_MIN_POINTS` points: `tau_cross_s=None`, `drift_ns_per_s=None`, `window_s=WINDOW_MIN_S`, `hold_s=HOLD_MIN_S`.  Keep at most `STABILITY_HISTORY_MIN` points.

- [ ] **Step 1: Write the failing tests**

```python
def test_white_phase_noise_alone_never_turns_up():
    # seed 1, 1440 points, sigma 0.5 ms, one run
    assert est.stability().tau_cross_s >= 64 * 60

def test_white_phase_plus_random_walk_crosses_near_theory():
    # sigma_w 0.5 ms white PM + phase random walk 0.2 ms/step, seed 2, 1440 points.
    # TDEV^2 ~ sigma_w^2/m + s^2*m/6, minimum at m* = sqrt(6)*sigma_w/s ~ 6 min.
    s = est.stability()
    assert 120 <= s.tau_cross_s <= 960
    assert s.window_s == 1800 and s.hold_s == 450

def test_a_constant_rate_is_not_wander_but_counts_as_drift():
    # phase ramps 1 us per minute, no noise
    s = est.stability()
    assert s.tau_cross_s == max(est.tdev_by_tau())
    assert s.drift_ns_per_s == pytest.approx(1000 / 60, rel=1e-3)

def test_run_boundaries_split_the_series():
    # white PM seed 1, plus a +300 ms step where run_id changes at point 700
    assert est.stability().tau_cross_s >= 64 * 60

def test_too_few_points_reads_unmeasured():
    s = est_with_119_points.stability()
    assert (s.tau_cross_s, s.drift_ns_per_s, s.window_s, s.hold_s) == (None, None, 1800, 300)

def test_a_gpsdo_class_clock_reaches_the_window_cap():
    # white PM 0.3 ms only, seed 3: tau_cross >= 7200 s, so window caps
    s = est.stability()
    assert (s.window_s, s.hold_s) == (7200, 1800)

def test_phase_point_reads_the_summary_through_its_counter_map():
    # summary with utc_ref, rtp_ref and a CounterMap built by the helper in
    # tests/unit/test_t3_shadow_comparator.py; expect run_id == input_epoch_gps_s
    # and phase_ms matching the hand computation to 1e-6 ms
```

- [ ] **Step 2: Run; expect ModuleNotFoundError.**
- [ ] **Step 3: Implement `clock_stability.py`.**  numpy only; no clock reads.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `t3: clock stability from T3's sample-plane phase (TDEV crossing sets window and hold)`.

### Task A3: The minute row carries what admission needs, and the per-minute errors

**Files:**
- Modify: `src/hf_timestd/core/authority_manager.py:592-688` (`_t3_shadow_minute_row`, `T3_SHADOW_OFFSET_SIGNS`)
- Create: `src/hf_timestd/core/t3_reference_errors.py`
- Test: `tests/unit/test_t3_reference_errors.py`, `tests/unit/test_authority_t3_shadow_minute.py`, `tests/test_t3_sign_convention.py`

**Interfaces:**
- Consumes: `phase_point_from_summary` (A2).
- Produces:
  - New row fields: `"t2_vs_adopted"` (the judge's `shadow_residuals["T2"]`, raw), `"t2_sigma_ms"` (T2 probe's `sigma_ms`), `"t3_phase_ms"`, `"t3_run_id"`.  `T3_SHADOW_OFFSET_SIGNS` gains `"t2_vs_adopted"` and `"t3_phase_ms"`.
  - `@dataclass(frozen=True) class ReferencePair: reference: str; t3_error_ms: float; t2_error_ms: Optional[float]`
  - `reference_errors(row: dict) -> Optional[ReferencePair]`
- The cases, best reference first.  Every error reads "estimate minus truth".  T3's error comes from its sample-plane reference where one exists; T2's error comes from the host plane, as the 11-01 verdict measured it (T2 against T4):

| `reference` | `t3_error_ms` | `t2_error_ms` |
|---|---|---|
| `"T6"` (T6 and T4) | `t3_shadow.t3_minus_t6_ms` | `t4_offset_ms − t2_offset_ms` |
| `"T6"` (T6, no T4) | `t3_shadow.t3_minus_t6_ms` | `t2_vs_adopted.shadow_residual_ns / 1e6` when its `vs_tier == "T6"` |
| `"T5"` | `t3_vs_adopted / 1e6`, `vs_tier == "T5"` | `t4_offset_ms − t2_offset_ms` if T4, else `t2_vs_adopted / 1e6`, `vs_tier == "T5"` |
| `"T4"` | `(t3_vs_adopted − t4_vs_adopted) / 1e6`, same `vs_tier`; or `t3_vs_adopted / 1e6` when `vs_tier == "T4"` | `t4_offset_ms − t2_offset_ms` |

  The second T6 row crosses the host↔sample link that carries B4's ~16 ms between T6 and T4.  Keep it, but name it in the docstring as unverified until a T6-only station's record confirms it.

- [ ] **Step 1: Write the failing tests**

```python
def test_t6_with_t4_takes_t3_from_the_sample_plane_and_t2_from_the_host():
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=0.02, t2_offset_ms=-0.49)
    assert reference_errors(row) == ReferencePair("T6", 0.26, pytest.approx(0.51))

def test_t4_only_station_uses_the_judge_difference():
    row = _row(t3_vs=(1_200_000.0, "T4"), t4_offset_ms=0.0, t2_offset_ms=-0.6)
    assert reference_errors(row) == ReferencePair("T4", 1.2, pytest.approx(0.6))

def test_no_reference_yields_none():
    assert reference_errors(_row(t2_offset_ms=-0.6)) is None

def test_nan_and_missing_offsets_yield_no_pair():
    row = _row(t3_minus_t6_ms=0.26, t4_offset_ms=None, t2_offset_ms=None)
    assert reference_errors(row).t2_error_ms is None
    assert reference_errors(_row(t3_minus_t6_ms=float("nan"))) is None

def test_the_row_carries_t2_vs_adopted_phase_and_run():   # in test_authority_t3_shadow_minute.py
    assert row["t2_vs_adopted"] == {"shadow_residual_ns": 510000.0, "sigma_ns": 2e7, "vs_tier": "T6"}
    assert row["t3_run_id"] == RUN_EPOCH and row["t3_phase_ms"] == pytest.approx(EXPECTED_PHASE_MS)
```

  Extend `tests/test_t3_sign_convention.py` with one case: a T2 source 0.5 ms late against T4 gives `t2_error_ms == +0.5`.

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**  `reference_errors` treats a non-finite or boolean value as missing.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `t3: minute row carries T2-vs-adopted and T3 phase; per-minute errors against the best reference`.

### Task A4: Sigma honesty by hour (criterion 3), computed at each reference station

**Files:**
- Create: `src/hf_timestd/core/t3_sigma_honesty.py`
- Test: `tests/unit/test_t3_sigma_honesty.py`

**Interfaces:**
- Consumes: `ReferencePair` (A3).
- Produces:
  - `@dataclass(frozen=True) class HourlyHonesty: hour_utc: int; n: int; rms_z: float; floor_fraction: float; honest: bool`
  - `class SigmaHonesty: add(utc_minute: int, pair: ReferencePair, t3_sigma_ms: Optional[float]) -> None; by_hour() -> Dict[int, HourlyHonesty]; seed(rows: Iterable[dict]) -> None`
- Rules: the stated sigma is T3's claim as the judge acts on it, `max(sigma_ms, ORIGIN_SIGMA_FLOOR_MS)`.  `z = t3_error_ms / claim`.  `honest` when `0.5 ≤ rms_z ≤ 2.0` (spec).  `floor_fraction` counts minutes whose claim equals the floor; Phase A reports it and gates nothing (see the report's note on criterion 3).  Keep `CALIBRATION_MAX_AGE_S` of minutes (72 h).

- [ ] **Step 1: Write the failing tests**

```python
def test_an_honest_day_hour():
    # hour 14: 60 minutes, errors alternating +1.0/-1.0 ms, sigma_ms 0.5 (claim 1.0)
    h = honesty.by_hour()[14]
    assert (h.n, h.rms_z, h.floor_fraction, h.honest) == (60, 1.0, 1.0, True)

def test_an_overconfident_night_hour():
    # hour 4: 60 minutes, error 3.0 ms, sigma_ms 1.5
    h = honesty.by_hour()[4]
    assert h.rms_z == pytest.approx(2.0) and h.honest is True
    # error 3.2 ms -> rms_z 2.1333, honest False

def test_minutes_older_than_72_hours_drop_out():
```

- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `t3: sigma honesty by UTC hour (criterion 3) at reference stations`.

### Task A5: The admission policy (route 1, modes, jumps, reasons)

**Files:**
- Create: `src/hf_timestd/core/t3_admission_policy.py`
- Test: `tests/unit/test_t3_admission_policy.py`

**Interfaces:**
- Consumes: `AdmissionMode` (A1), `PhasePoint`, `ClockStabilityEstimator`, `ClockStability` (A2), `reference_errors`, `ReferencePair` (A3), `SigmaHonesty` (A4).
- Produces:
  - `@dataclass(frozen=True) class MinuteEvidence: utc_minute: int; pair: Optional[ReferencePair]; phase: Optional[PhasePoint]; t3_sigma_ms: Optional[float]; t2_sigma_ms: Optional[float]; whole_second_unresolved: bool` with `@classmethod from_row(row: dict) -> MinuteEvidence`
  - `@dataclass(frozen=True) class AdmissionDecision: mode: AdmissionMode; admitted: bool; route: Optional[str]` (`"reference"`, `"sigma"`, `"forced"` or None) `; reason: str; reference: Optional[str]; n_pairs: int; t3_median_ms: Optional[float]; t3_p95_ms: Optional[float]; t2_median_ms: Optional[float]; t2_p95_ms: Optional[float]; stability: ClockStability; since_utc_minute: Optional[int]; transitions: int` with `to_block() -> dict` (JSON-safe; this is `authority.json`'s `t3_admission` block and the minute row's `admission` field) and `@classmethod initial(mode) -> AdmissionDecision`
  - `class T3AdmissionPolicy: __init__(self, mode: AdmissionMode); observe(ev: MinuteEvidence) -> AdmissionDecision; seed(rows: Iterable[dict]) -> int` (returns rows used) `; decision -> AdmissionDecision` (property) `; honesty -> SigmaHonesty` (property)
- Reason strings (module constants; tests pin them):
  - `"admitted by reference ({ref})"`
  - `"admitted by reference ({ref}), held: no reference for {m} min"`
  - `"withheld: T2 tracks {ref} better (median {t3:.2f} vs {t2:.2f} ms, p95 {t3p:.2f} vs {t2p:.2f} ms)"`
  - `"withheld: window filling ({n} of {need} minutes)"`
  - `"withheld: no T2 to compare against"`
  - `"withheld: no reference, and no fleet calibration yet"` (Phase B replaces this path with route 2)
  - `"withdrawn: T3 named the wrong second ({err:.0f} ms against {ref})"`
  - `"withdrawn: T3 jumped {step:.0f} ms"`
  - `"withheld: T3 has not named the second"` (summary `whole_second_unresolved`)
  - `"off ([timing.t3] admission = \"off\")"`
  - `"forced on ([timing.t3] admission = \"on\"), for testing only"`
  - While a change waits out its hold, the reason of the standing state gains `"; change pending {s} of {hold} s"`.
- Rules:
  - The window holds the minutes with `utc_minute > now − window_s` (window from the current `ClockStability`).  A minute counts as a pair only when both errors exist.
  - Fill: `need = max(FILL_MIN_PAIRS, window_s // 120)`.
  - Condition: `median|e3| < median|e2|` and `p95|e3| < p95|e2|` (numpy `percentile`, default linear interpolation).
  - Hysteresis: a change in the condition becomes a change in `admitted` once it has held for `hold_s` (measured in `utc_minute`).  The first verdict after start waits its hold too.
  - Jump: `|e3| ≥ JUMP_MS`, or a phase step `≥ JUMP_MS` between consecutive minutes of one `run_id`, withdraws at once, clears the window, and restarts the fill.  A phase step across a `run_id` change counts as no jump.
  - Reference lost: while admitted by reference and no pair arrives, hold admission until `utc_minute − last_pair_minute ≥ window_s`, then withhold.
  - Mode OFF: never admitted.  Mode ON: admitted, except that a jump withdraws for `hold_s` (the safety rule outranks the test switch).
  - A `utc_minute` equal to or older than the last one observed returns the standing decision unchanged and adds nothing.
  - Each change of `admitted` logs once at WARNING with the reason and increments `transitions`.

- [ ] **Step 1: Write the failing tests** (minutes 60 s apart from 0; no stability history, so window 1800 s, hold 300 s, need 20)

```python
def test_day_admits_after_fill_and_hold():
    # e3 = 0.3, e2 = 0.6 every minute
    assert first_index(decisions, admitted=True) == 24
    assert decisions[24].reason == "admitted by reference (T6)"
    assert decisions[24].route == "reference"

def test_night_withdraws_after_the_p95_fails_and_the_hold_passes():
    # then from index 60 e3 = 1.2; p95 fails at 61 (two bad minutes of 30)
    assert first_index(decisions[60:], admitted=False, offset=60) == 66
    assert decisions[66].reason.startswith("withheld: T2 tracks T6 better")

def test_wrong_second_withdraws_at_once_and_refills():
    # admitted by 24; at 40 e3 = 600.0
    assert decisions[40].admitted is False
    assert decisions[40].reason == "withdrawn: T3 named the wrong second (600 ms against T6)"
    assert first_index(decisions[41:], admitted=True, offset=41) == 65

def test_a_phase_step_within_a_run_is_a_jump():
    # phase steps by 700 ms at index 40, same run_id
    assert decisions[40].reason == "withdrawn: T3 jumped 700 ms"

def test_a_run_change_is_not_a_jump():
    # phase steps by 700 ms at index 40 with a new run_id
    assert decisions[40].admitted is True

def test_reference_loss_holds_one_window():
    # admitted by 30 (pairs 0..30), no pairs from 31
    assert decisions[59].admitted is True
    assert decisions[59].reason == "admitted by reference (T6), held: no reference for 29 min"
    assert decisions[60].admitted is False
    assert decisions[60].reason == "withheld: no reference, and no fleet calibration yet"

def test_no_t2_says_so():
    # 30 minutes with e3 and no e2
    assert decisions[-1].reason == "withheld: no T2 to compare against"

def test_off_never_admits_and_on_admits_until_a_jump():
    assert all(not d.admitted for d in off_decisions)
    assert on_decisions[0].admitted and on_decisions[0].route == "forced"
    # jump at 10: withdrawn 10..14, forced again at 15
    assert [d.admitted for d in on_decisions[10:16]] == [False] * 5 + [True]

def test_a_repeated_minute_adds_no_sample():
    d1 = policy.observe(ev(minute=600)); d2 = policy.observe(ev(minute=600))
    assert d2 == d1 and policy.decision.n_pairs == d1.n_pairs

def test_transitions_count_once_each(caplog):
    # day -> night -> day
    assert decisions[-1].transitions == 3
    assert sum("T3 admission" in r.message for r in caplog.records if r.levelname == "WARNING") == 3

def test_to_block_is_strict_json():
    json.dumps(decision.to_block(), allow_nan=False)
```

- [ ] **Step 2: Run; expect ModuleNotFoundError.**
- [ ] **Step 3: Implement `t3_admission_policy.py`.**  Pure: no file I/O, no clock reads; the only time it knows comes from `MinuteEvidence.utc_minute`.  It feeds every `phase` to its `ClockStabilityEstimator` and every pair to its `SigmaHonesty`.  Recompute `ClockStability` at most every 10 minutes of `utc_minute` (builder choice).
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `t3: admission policy, route 1 (T3 beats T2 against the reference, window and hold from the clock)`.

### Task A6: The authority manager decides and publishes

**Files:**
- Modify: `src/hf_timestd/core/authority_manager.py` (`__init__`, `tick`, `_close_unadmitted_t3`, `_write_state`, `_write_t3_shadow_minute`, `_t3_shadow_minute_row`, `registration_block`)
- Modify: `src/hf_timestd/core/authority_runner.py:416-507`
- Modify: `src/hf_timestd/core/t3_shadow_record.py` (add `read_recent`)
- Test: `tests/unit/test_t3_admission.py` (authority tests), `tests/unit/test_authority_t3_admission_block.py` (new), `tests/unit/test_t3_shadow_record.py`

**Interfaces:**
- Consumes: `T3AdmissionPolicy`, `AdmissionDecision`, `MinuteEvidence` (A5); `t3_admission_mode` (A1).
- Produces:
  - `AuthorityManager.__init__(..., t3_policy: Optional[T3AdmissionPolicy] = None, t3_admitted: Optional[bool] = None)`.  `t3_admitted=True` builds a policy in mode ON and `False` one in mode OFF (kept so the existing tests stay short); neither given builds mode AUTO.
  - `AuthorityManager.t3_decision -> AdmissionDecision` (property).
  - `authority.json` gains `"t3_admission": decision.to_block()` on every tick.  `t3_unavailable_reason` reads the decision's reason while T3 stays unadmitted.  `registration_block(..., admitted: bool, admission_reason: Optional[str] = None)` writes `"admission": admission_reason` when it withholds.
  - The minute row gains `"admission": decision.to_block()`.
  - `T3ShadowRecord.read_recent(since_utc_minute: int) -> List[dict]` reads the stream's day files that can hold minutes at or after `since_utc_minute`, skipping unparseable lines.
- Behaviour:
  - The manager builds the minute row on the first tick of each new minute whether or not a shadow record exists.  It feeds `MinuteEvidence.from_row(row)` to the policy before appending, so the row records the decision it produced.
  - `_close_unadmitted_t3` reads `self.t3_decision.admitted`.
  - The runner builds the policy from `t3_admission_mode(config)` and seeds it from `t3_shadow_record.read_recent(now_minute − STABILITY_HISTORY_MIN·60)`, where `now_minute` comes from the manager's `now_fn`.  A seed failure logs one WARNING and starts empty.

- [ ] **Step 1: Write the failing tests**

```python
def test_authority_json_carries_the_admission_block_every_tick(tmp_path):
    m = _authority(tmp_path, t3_available=True)
    m.tick()
    block = json.loads((tmp_path / "authority.json").read_text())["t3_admission"]
    assert block["mode"] == "auto" and block["admitted"] is False
    assert block["reason"].startswith("withheld:")

def test_an_auto_station_admits_t3_from_its_rows(tmp_path):
    # fake now_fn advances 60 s a tick; judge file gives t3_minus_t6_ms 0.3, probes give T4 0.0 and T2 -0.6
    for _ in range(25): state = m.tick()
    published = json.loads((tmp_path / "authority.json").read_text())
    assert published["t3_admission"]["admitted"] is True
    assert published["registration"]["source"] == "hf_acquired"
    assert "t3_unavailable_reason" not in published

def test_the_decision_reaches_the_minute_row(tmp_path):
    assert rows[-1]["admission"]["reason"] == "admitted by reference (T6)"

def test_admission_runs_without_a_shadow_record(tmp_path):
    m = _authority(tmp_path, t3_available=True, t3_shadow_record=None)
    # same 25 ticks -> admitted

def test_seed_skips_old_rows_and_restores_the_window(tmp_path):
    # write 30 rows (10 in the pre-change shape, without t3_phase_ms/t2_vs_adopted)
    n = policy.seed(record.read_recent(since))
    assert n == 20
    assert policy.decision.n_pairs == 20

def test_existing_unadmitted_tests_now_name_the_mode():
    # rewrite test_unadmitted_t3_publishes_no_authority: t3_admitted=False -> mode off
    assert published["t3_unavailable_reason"] == 'off ([timing.t3] admission = "off")'
```

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**  Keep `tick()`'s order; only the T3 shadow step changes (row first, policy second, append third).
- [ ] **Step 4: Run** the new file and `tests/unit/test_t3_admission.py tests/unit/test_authority_t3_shadow_minute.py tests/test_authority_manager.py tests/test_authority_runner.py`; expect PASS.
- [ ] **Step 5: Commit** `t3: authority manager decides admission each minute and publishes t3_admission`.

### Task A7: One gate per process, read from `authority.json`

**Files:**
- Create: `src/hf_timestd/core/t3_admission_gate.py`
- Modify: `src/hf_timestd/core/core_recorder_v2.py:740-790, 3108-3130`
- Modify: `src/hf_timestd/core/metrology_service.py:275-280, 1675-1690`
- Modify: `src/hf_timestd/core/t3_registration_anchor.py` (`admitted: bool` → `gate`)
- Modify: `src/hf_timestd/core/timing_chain_publisher.py:98-111`
- Modify: `src/hf_timestd/core/multi_broadcast_fusion.py:4766-4772`
- Test: `tests/unit/test_t3_admission_gate.py` (new); rewrite the recorder, metrology, anchor and chain tests in `tests/unit/test_t3_admission.py`

**Interfaces:**
- Consumes: `AdmissionMode` (A1); the `t3_admission` block (A6).
- Produces:
  - `class T3AdmissionGate: __init__(self, mode: AdmissionMode, authority_path: Path = Path("/run/hf-timestd/authority.json"), stale_s: float = 120.0, mono_fn: Callable[[], float] = time.monotonic)`
  - `refresh() -> None` — the only method that touches the file.  Never raises.
  - `admitted() -> bool`, `reason() -> str`, `clock() -> Optional[ClockStability]` — attribute reads, no I/O, safe on a hot path.
  - `@classmethod fixed(cls, admitted: bool) -> T3AdmissionGate` for tests and for callers with no authority file.
- Rules:
  - Mode OFF: `admitted()` is always False.
  - Fresh block: `admitted()` follows `block["admitted"]`, in modes AUTO and ON alike (so a jump withdrawal reaches every process).
  - No fresh block: AUTO answers False; ON answers True.
  - Freshness uses no wall clock: the block counts as fresh only after the gate has seen `utc_published` change, and only while the last change lies within `stale_s` of `mono_fn()`.  The manager writes every 30 s, so 120 s spans four ticks.
- Callers:
  - The recorder builds one gate, refreshes it on its 60 s revalidation tick, and hands it to the judge (Task A8) and to `T3RegistrationAnchor`.
  - Each metrology process builds one gate and refreshes it once per minute before deciding labels (`metrology_service.py:1678`).
  - `publish_chains(..., t3_admission_mode: AdmissionMode)` records the mode.  Its registration block claims a source only in mode ON, because the chain record goes out once at start; the per-minute truth lives in `authority.json`.

- [ ] **Step 1: Write the failing tests**

```python
def test_gate_closes_on_missing_stale_or_malformed_file(tmp_path):
    g = T3AdmissionGate(AdmissionMode.AUTO, tmp_path / "authority.json", mono_fn=clock)
    g.refresh(); assert g.admitted() is False                       # missing
    _write(tmp_path, admitted=True, published="t0"); g.refresh()
    assert g.admitted() is False                                    # first sight: not yet fresh
    _write(tmp_path, admitted=True, published="t1"); g.refresh()
    assert g.admitted() is True
    clock.advance(121); g.refresh(); assert g.admitted() is False   # stale
    (tmp_path / "authority.json").write_text("{not json"); g.refresh()
    assert g.admitted() is False                                    # malformed, no raise

def test_on_follows_a_fresh_withdrawal_and_holds_otherwise(tmp_path):
    g = T3AdmissionGate(AdmissionMode.ON, ...)
    g.refresh(); assert g.admitted() is True
    # two fresh writes with admitted False, reason "withdrawn: T3 jumped 700 ms"
    assert g.admitted() is False and g.reason() == "withdrawn: T3 jumped 700 ms"

def test_off_ignores_the_file(tmp_path):

def test_admitted_reads_no_file(tmp_path, monkeypatch):
    # after one refresh, make Path.read_text raise; admitted() still answers

def test_metrology_follows_the_gate_minute_by_minute(tmp_path):
    # gate flips False -> True between two minutes; first returns origin_source "label", second "acquired"

def test_the_recorder_anchor_follows_the_gate(tmp_path):
```

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement the gate, then swap each caller.**  Remove every `t3_admitted(` call; `grep -rn "t3_admitted(" src/` must return nothing.
- [ ] **Step 4: Run** `tests/unit/test_t3_admission_gate.py tests/unit/test_t3_admission.py tests/unit/test_t3_registration_anchor.py tests/unit/test_metrology_service_t3_station_state.py`; expect PASS.  Then run the whole suite once.
- [ ] **Step 5: Commit** `t3: every process follows the published admission decision through one gate`.

### Task A8: The judge follows the same clock

**Files:**
- Modify: `src/hf_timestd/core/offset_judge.py` (`SHADOW_ONLY_BENCHES` comment and use at 1212-1236, 2047-2066, 3420, 3475, 3530-3547; `HfAcquiredBench.read` at ~1040-1110)
- Test: `tests/unit/test_judge_t3_follows_admission.py` (new); update `tests/unit/test_shadow_only_never_dissents.py`, `tests/unit/test_judge_hf_acquired_shadow_only.py`, and `test_admitted_shadow_only_t3_still_never_dissents` in `tests/unit/test_t3_admission.py`

**Interfaces:**
- Consumes: `T3AdmissionGate` (A7), `ClockStability` (A2).
- Produces:
  - `OffsetJudge.__init__(..., t3_gate: Optional[T3AdmissionGate] = None, t3_admitted: bool = False)`.  `t3_admitted` builds `T3AdmissionGate.fixed(t3_admitted)` when no gate arrives.
  - `OffsetJudge.SHADOW_ONLY_BENCHES` becomes a method, `_shadow_only(bench: str) -> bool`: `hf_acquired` counts as shadow-only exactly while the gate says not admitted.  It then votes in dissent and may be adopted when admitted, under the existing cross-bench gate.
  - `HfAcquiredBench(..., clock_provider: Callable[[], Optional[ClockStability]] = lambda: None)`.  When `drift_ns_per_s` exists: `sigma_ns = hypot(sigma_ns, drift_ns_per_s × registration_age_s)`, and the freshness limit becomes `clamp(1e6 / drift_ns_per_s, FRESHNESS_S, window_s)` seconds, the age at which the clock term reaches the 1 ms origin floor (builder choice).  Unmeasured stability keeps today's `FRESHNESS_S = 180` and adds no term.
  - The judge refreshes nothing itself; the recorder refreshes the gate (A7).

- [ ] **Step 1: Write the failing tests**

```python
def test_admitted_t3_votes_and_unadmitted_t3_does_not(tmp_path):
    gate = _Gate(admitted=False)
    judge = _judge(tmp_path, t3_gate=gate, benches=[t6, t4, hf_acquired])
    _tick(judge, 3); assert "T3" not in judge._dissent_voters()
    gate.set(True); _tick(judge, 3); assert "T3" in judge._dissent_voters()

def test_the_clock_term_widens_t3_sigma_with_age():
    # drift 100 ns/s, registration age 60 s, summary sigma 0.5 ms -> floor 1.0 ms
    assert reading.sigma_ns == pytest.approx(math.hypot(1e6, 6000.0))

def test_a_gpsdo_clock_holds_t3_past_180_s():
    # drift 0.001 ns/s, window 7200: a 600 s old registration still reads
    assert bench.read() is not None

def test_a_crystal_clock_drops_t3_sooner():
    # drift 10000 ns/s -> limit clamps to 180 s; a 200 s old registration reads None

def test_unmeasured_stability_keeps_todays_bench():
    # clock_provider returns None: sigma and the 180 s limit unchanged
```

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement.**  Rewrite the `SHADOW_ONLY_BENCHES` comment block: the 10-30 pairing ("flip both together") no longer applies, since one decision drives both.
- [ ] **Step 4: Run** the new and updated judge tests, then `tests/test_offset_judge*.py`; expect PASS.
- [ ] **Step 5: Commit** `judge: T3 bench follows the admission decision and carries the clock term`.

---

## Phase B — needs `sigmondb_public` (Rob), built and tested against a fake first

Each task names its dependency.  None of them may reach a real server in tests.  Tasks B1, B2, B4 and B5 can be built now; B3 and B6 wait as marked.

### Task B1: The `SigmondbPublic` protocol, a fake, and the HTTP client

**Depends on:** Rob's answer on hosts, port and credentials (Decisions, question 2) for the production defaults only.  Build and test now.

**Files:**
- Create: `src/hf_timestd/core/sigmondb_public.py`
- Create: `tests/fakes/fake_sigmondb.py`
- Test: `tests/unit/test_sigmondb_public.py`

**Interfaces:**
- Produces:
  - `class SigmondbPublic(Protocol): def fetch(self, table: str) -> List[dict]: ...` — raises `SigmondbFetchError` on any failure.
  - `class SigmondbFetchError(Exception)`
  - `PUBLIC_TABLES = ("transmitter_offsets", "trust", "receivers", "transmitters", "t3_calibration")`
  - `class ClickHouseHttpClient: __init__(self, base_urls: Sequence[str], database: str = "sigmondb_public", user: str = "default", password: str = "", timeout_s: float = 10.0, session: Optional[requests.Session] = None)`.  `fetch` sends `SELECT * FROM {database}.{table} FORMAT JSONEachRow` as an HTTP GET with `user`/`password` parameters, tries each base URL in order, and refuses a table name outside `PUBLIC_TABLES`.
  - `class FakeSigmondb` (tests): `tables: Dict[str, List[dict]]`, `fail: Set[str]`, `calls: List[str]`.
- Every row the client returns must carry `built_at_utc` (spec: "every row carries the time wd30 built it"); a row without it fails the whole table.

- [ ] **Step 1: Write the failing tests** — a stub `requests` session: the query string names `sigmondb_public.transmitter_offsets` and `FORMAT JSONEachRow`; a first URL that times out falls through to the second; an unknown table raises before any request; a row without `built_at_utc` raises `SigmondbFetchError`; a 500 raises `SigmondbFetchError`.
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `sigmondb: public read-only client behind a protocol, with a fake`.

### Task B2: The last good copy, its age, and the fetch schedule

**Depends on:** B1.  Build and test now.

**Files:**
- Create: `src/hf_timestd/core/published_table_cache.py`
- Modify: `src/hf_timestd/core/authority_runner.py` (start the fetch thread when `[timing.t3.sigmondb] base_urls` is non-empty)
- Modify: `config/timestd-config.toml.template` (add `[timing.t3.sigmondb]` with `base_urls = []`, `interval_s = 3600`, commented)
- Test: `tests/unit/test_published_table_cache.py`

**Interfaces:**
- Consumes: `SigmondbPublic`, `SigmondbFetchError`, `PUBLIC_TABLES` (B1).
- Produces:
  - `@dataclass(frozen=True) class CachedTable: name: str; rows: List[dict]; built_at_utc: float; fetched_at_utc: float`, with `age_s(now_s: float) -> float` measured from the oldest `built_at_utc` in the rows.
  - `class PublishedTableCache: __init__(self, client: SigmondbPublic, directory: Path = Path("/var/lib/timestd/sigmondb"), now_fn: Callable[[], float] = ...)`; `refresh(table: str) -> bool`; `refresh_all() -> Dict[str, bool]`; `get(table: str) -> Optional[CachedTable]`.
  - `class TableFetchRunner(threading.Thread)`: calls `refresh_all()` every `interval_s` plus a jitter of 0–300 s drawn once at start (builder choice), and stops on an event.
- Rules: a failed fetch keeps the last good copy and logs one WARNING per failure streak.  A good fetch replaces the copy whole and writes it atomically (tmp + `os.replace`), one JSON file per table, so a restart starts from the last good copy.  An empty table counts as good only when the server answered without error.  `now_fn` serves bookkeeping only (hours-scale age); inject it, and never call the wall clock directly.

- [ ] **Step 1: Write the failing tests** — `test_a_failed_fetch_keeps_the_last_good_copy`, `test_a_restart_reads_the_copy_from_disk`, `test_age_counts_from_the_oldest_built_at` (rows built at 1000 and 4600, now 8200: `age_s == 7200`), `test_a_torn_write_leaves_the_old_file` (make `os.replace` raise: old file intact), `test_one_warning_per_failure_streak`.
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `sigmondb: keep the last good copy of each published table with its age`.

### Task B3: Reference stations publish their sigma honesty

**Depends on:** a way for station rows to reach sigmondb on wd30.  The spec describes only wd30 → public servers.  Build the export file now; wiring waits until Michael and Rob name the path (the chirp design's ingestion is the likely one).

**Files:**
- Create: `src/hf_timestd/core/t3_calibration_export.py`
- Test: `tests/unit/test_t3_calibration_export.py`

**Interfaces:**
- Consumes: `SigmaHonesty.by_hour()` (A4), `AuthorityManager.t3_decision` (A6).
- Produces: `export_rows(station: str, honesty: Dict[int, HourlyHonesty], reference: str, built_at_utc: float) -> List[dict]`, one row per hour with keys `station, reference, hour_utc, n, rms_z, floor_fraction, honest, built_at_utc`; and `write_spool(rows, directory: Path) -> Path` (JSONL, atomic).  Only stations whose decision names a reference export.

- [ ] **Step 1: Write the failing tests** — 24 hours in, 24 rows out; a station without a reference exports nothing; NaN never reaches the file (`allow_nan=False`).
- [ ] **Step 2–4: Run, implement, run.**
- [ ] **Step 5: Commit** `t3: export hourly sigma honesty for sigmondb`.

### Task B4: Route 2, admission by sigma under fleet calibration

**Depends on:** B2 for production data; the `t3_calibration` table on the public servers.  Build and test now with `FakeSigmondb`.  The rule for a station with no T2 waits on Decisions, question 1; until then such a station stays withheld with `"withheld: no T2 to compare against"`.

**Files:**
- Create: `src/hf_timestd/core/fleet_calibration.py`
- Modify: `src/hf_timestd/core/t3_admission_policy.py`
- Modify: `src/hf_timestd/core/authority_runner.py` (hand the cache to the policy)
- Test: `tests/unit/test_fleet_calibration.py`, `tests/unit/test_t3_admission_policy.py`

**Interfaces:**
- Consumes: `PublishedTableCache.get("t3_calibration")` (B2), `T3AdmissionPolicy` (A5).
- Produces:
  - `@dataclass(frozen=True) class HourCalibration: hour_utc: int; n: int; rms_z: float; holds: bool; age_s: float`
  - `fleet_calibration_for_hour(table: Optional[CachedTable], hour_utc: int, now_s: float) -> Optional[HourCalibration]` — pooled `rms_z = sqrt(Σ n·rms_z² / Σ n)` over rows for that hour; None when `Σ n < CALIBRATION_MIN_MINUTES` or no rows; `holds` when `0.5 ≤ rms_z ≤ 2.0` and `age_s ≤ CALIBRATION_MAX_AGE_S`.
  - `T3AdmissionPolicy.__init__(self, mode, *, fleet: Optional[Callable[[int], Optional[HourCalibration]]] = None)` — `fleet(hour_utc)` returns the hour's calibration.
- Rules: route 2 runs when no pair has arrived for a full window (the reference-loss hold of A5 ends there) or when the station never had a reference.  The condition: `fleet(hour).holds` and `max(t3_sigma_ms, ORIGIN_SIGMA_FLOOR_MS) < t2_sigma_ms`.  Same hold and jump rules as route 1.  The hour comes from `utc_minute`.
- Reasons (spec words where the spec gives them):
  - `"admitted by sigma, fleet calibration holds for {hh:02d}Z"`
  - `"withheld: T2 sigma smaller"`
  - `"withheld: no fleet calibration for {hh:02d}Z"`
  - `"withheld: fleet calibration fails for {hh:02d}Z (rms {rms:.2f})"`
  - `"withheld: fleet calibration stale ({h} h old)"`

- [ ] **Step 1: Write the failing tests**

```python
def test_pooled_rms_weights_by_minutes():
    rows = [_cal(hour=14, n=300, rms_z=1.0), _cal(hour=14, n=100, rms_z=2.0)]
    c = fleet_calibration_for_hour(_table(rows, built=now - 7200), 14, now)
    assert c.rms_z == pytest.approx(math.sqrt((300 * 1 + 100 * 4) / 400)) and c.holds

def test_route2_admits_by_day_and_withholds_at_night():
    # T3 sigma 0.5 (claim 1.0), T2 sigma 20.0; hour 14 holds, hour 4 rms 3.2
    assert d14.reason == "admitted by sigma, fleet calibration holds for 14Z" and d14.route == "sigma"
    assert d04.reason == "withheld: fleet calibration fails for 04Z (rms 3.20)"

def test_t2_sigma_smaller_withholds():
    # T2 sigma 0.8 against a claim of 1.0
    assert d.reason == "withheld: T2 sigma smaller"

def test_missing_and_stale_calibration_withhold():
    assert d_missing.reason == "withheld: no fleet calibration for 04Z"
    assert d_stale.reason == "withheld: fleet calibration stale (80 h old)"

def test_route1_outranks_route2_while_a_reference_answers():
```

- [ ] **Step 2–4: Run, implement, run.**
- [ ] **Step 5: Commit** `t3: route 2, admission by sigma while the fleet calibration holds`.

### Task B5: Transmitter offsets, and one epoch estimate per arrival

**Depends on:** the `transmitter_offsets`, `trust` and `transmitters` tables on the public servers (B2), and the offset sign convention (Decisions, question 3).  Build and test now with the fake.

**Files:**
- Create: `src/hf_timestd/core/transmitter_offsets.py`
- Create: `src/hf_timestd/core/transmitter_epoch.py`
- Modify: `config/timestd-config.toml.template` (`[timing.t3] transmitter_sources = []`, commented: a kind joins only after B4 shows it improves T3, ruling 9 of 10-30)
- Test: `tests/unit/test_transmitter_offsets.py`, `tests/unit/test_transmitter_epoch.py`

**Interfaces:**
- Consumes: `CachedTable` (B2).
- Produces:
  - `@dataclass(frozen=True) class TransmitterOffset: tx_id: str; kind: str; offset_ms: float; sigma_ms: float; last_confirmed_utc: float; n_confirming: int; built_at_utc: float`
  - `usable_offset(tx_id: str, offsets: Optional[CachedTable], trust: Optional[CachedTable], now_s: float, enabled_kinds: Collection[str]) -> Optional[TransmitterOffset]` — None when the kind is not enabled, no trusted station confirms it, `n_confirming < 1`, or `now_s − last_confirmed_utc > OFFSET_MAX_CONFIRM_AGE_S`.  Otherwise the returned `sigma_ms` = `hypot(sigma_ms, OFFSET_AGE_SIGMA_MS_PER_H × copy_age_h)`.
  - `@dataclass(frozen=True) class TimingObservation: tx_id: str; input_index: float; run_id: float; emission_nominal_utc_s: float; sigma_ms: float` (the §7.2 spool's record, as this plan assumes it)
  - `@dataclass(frozen=True) class EpochEstimate: tx_id: str; input_index: float; run_id: float; utc_s: float; sigma_ms: float`
  - `transmitter_epoch_estimate(obs: TimingObservation, offset: Optional[TransmitterOffset], predicted_delay_ms: float, predicted_sigma_ms: float) -> Optional[EpochEstimate]` — the UTC at which input sample `input_index` arrived: `utc_s = emission_nominal_utc_s + offset_ms/1e3 + predicted_delay_ms/1e3`; `sigma_ms = hypot(obs.sigma_ms, predicted_sigma_ms, offset.sigma_ms)`.  None when `offset` is None.  Sign assumed here: a positive `offset_ms` means the transmitter emits late.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_confirmed_offset_carries_its_copy_age():
    # offset 2.5, sigma 0.1, confirmed 2 days ago by 2 trusted stations, copy built 30 h ago
    o = usable_offset("codar-NANT", offsets, trust, now, {"codar"})
    assert o.sigma_ms == pytest.approx(math.hypot(0.1, 0.15))

def test_stale_unconfirmed_untrusted_or_disabled_offsets_contribute_nothing():
    # confirmed 8 days ago; n_confirming 0; absent from trust; kind "hfdl" not enabled -> None each

def test_the_epoch_estimate_adds_offset_and_delay():
    e = transmitter_epoch_estimate(obs(emission=1000.0, sigma=0.2), offset(2.5, 0.1803), 3.0, 0.3)
    assert e.utc_s == pytest.approx(1000.0055, abs=1e-9)
    assert e.sigma_ms == pytest.approx(math.sqrt(0.04 + 0.09 + 0.1803 ** 2))

def test_no_offset_no_estimate():
```

- [ ] **Step 2–4: Run, implement, run.**
- [ ] **Step 5: Commit** `t3: transmitter offsets from the fleet and one epoch estimate per arrival`.

### Task B6: Transmitter estimates enter T3 fusion

**Depends on (blocked):** (a) the §7.2 observation spool from hfdl-recorder and codar-sounder (10-30 §7.2; not built); (b) the station answer with one witness per transmitter site (10-30 §7.3; today `registration_store.fuse_registrations` fuses per-channel planes, with the last writer standing in); (c) a propagation prediction for these paths from hamsci-dsp / PHaRLAP.  Do not start until all three exist.  Plan this task in detail then; the outline below fixes only its contract.

**Files (expected):**
- Modify: the station-answer module that (b) creates
- Test: alongside it

**Interfaces:**
- Consumes: `EpochEstimate` (B5), `usable_offset` (B5).
- Produces: each transmitter joins the station answer as one witness, exactly as WWV and WWVH do: its estimate, with its sigma, under the same median gate.  A transmitter whose offset is unusable contributes nothing.  The shadow minute row records, per transmitter, the estimate's residual from the station answer, so B4 can show whether each kind improves T3 (criteria 1, 3 and 6) before it joins `transmitter_sources` anywhere else.

- [ ] **Step 1: Write the failing tests** — station answer with WWV and WWVH alone, then with a CODAR estimate at a known offset: the CODAR witness enters with weight `1/sigma²`; an unusable offset leaves the answer unchanged byte for byte; a CODAR estimate 5 sigma from the median drops out.
- [ ] **Step 2–4: Run, implement, run.**
- [ ] **Step 5: Commit** `t3: CODAR and HFDL estimates join the station answer as one witness per transmitter`.

---

## Decisions needed before Phase B

1. **A station with no T2 either.**  A station with no internet time has nothing for T3 to beat.  Does route 2 then admit T3 on fleet calibration alone?
   - (a) Yes: admit when the fleet calibration holds for the hour, with no sigma comparison.
   - (b) Yes, with a bar: admit when the calibration holds and T3's claim stays within the T3 tier's own ceiling, `CROSS_BENCH_SIGMA_CEILING_MS_BY_TIER["T3"]` = 20 ms.
   - (c) No: T3 stays a shadow, and the station labels with its free-running host clock.
   - *Recommendation: (b).*  A free-running host clock has no stated sigma at all, so an honest T3 beats it by any measure.  The bar keeps a T3 whose sigma has blown up from labelling anyway.  The same rule should then cover a reference station without T2: route 1 has nothing to compare, so it admits on the reference alone when `p95|e3|` stays under the same 20 ms.

2. **Rob: the public servers.**  Which hosts and port serve the public read-only interface (8123 on wsprdaemon.org and wd1-wd3 didn't answer from the devbox), and can they carry `sigmondb_public`?
   - (a) ClickHouse's HTTP interface on 8123, on each of wd1–wd3, tried in order.
   - (b) HTTPS on 443 through a reverse proxy on an existing WsprDaemon host.
   - (c) ClickHouse's native protocol on 9000.
   - *Recommendation: (b), with (a) acceptable.*  Station networks pass 443 most reliably, and an HTTP interface needs only `requests`, which hf-timestd already carries; (c) would add a driver dependency.  The client takes a list of base URLs either way, so the answer changes the template default and nothing else.  Rob also has to agree the database's size and the push credentials from wd30.

3. **The sign and shape of a published transmitter offset.**  The chirp design writes the source side; this plan reads it.
   - (a) `offset_ms` = emission minus UTC (positive: the transmitter runs late), with `sigma_ms`, `last_confirmed_utc`, `n_confirming`, `built_at_utc`.
   - (b) `offset_ms` = UTC minus emission.
   - *Recommendation: (a)*, which matches "reading minus truth", the convention every shadow-row error already uses.  B5 assumes it; a different answer changes one sign and its test.

## Out of scope, named so nobody assumes it

- The authority rung.  `FusionStatusProbe` stays dark by the 10-22 ruling, so even an admitted T3 cannot become `authority.json`'s `t_level_active`.  Admission here reaches labels, the ring and native anchors, §18 (through the judge), the archive sidecar and the judge.  The registration-backed T3 probe belongs to 10-30 §8 step 7's station-timing-estimator work.  Route 2's premise, "the authority already picks between tiers by sigma", holds only once that probe exists.
- The wd30 job that builds `sigmondb_public`, and the calibration's ingestion into sigmondb.

## Self-review

- Spec coverage: the setting and its mapping (A1); route 1 by median and p95 (A3, A5); windows from the station's clock (A2, A5); the judge on the same clock (A8); the never-wrong-second and jump rules (A5, A7 carries withdrawals everywhere); per-minute reasons in `authority.json` (A5, A6); the shadow record keeps running (A6); criterion 3 at reference stations (A4) and its publication (B3); the fetch, last good copy and age (B1, B2); route 2 (B4); transmitter offsets, staleness and the per-kind gate (B5); fusion entry (B6, blocked).  Transition logging and counting (criterion 5): A5.
- Types: `AdmissionMode`, `ClockStability`, `ReferencePair`, `MinuteEvidence`, `AdmissionDecision`, `T3AdmissionGate`, `CachedTable`, `HourCalibration`, `TransmitterOffset`, `EpochEstimate` keep one spelling from definition to every later use.
- Every number either comes from the spec (0.5 s jump, 0.5–2 rms band, median and p95, hourly fetch) or appears in the builder-choice table.
