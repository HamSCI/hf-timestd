# Anchor-Pair Torn Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop hf-timestd from re-basing a channel's sample-to-UTC mapping on a pair of radiod numbers it read from two different status packets, and catch, refuse or undo any such re-base that still slips through.

**Architecture:** One small module reads radiod's `(gps_time, rtp_timesnap)` pair atomically, and every site that reads the pair uses it. A second module holds the pure decision on whether a re-observed pair that disagrees with the mapping by more than 0.75 s may be adopted. On a GPSDO station with T6 authoritative, the GPSDO decides. On any other station, the step must persist across two checks. The stream recorder keeps the mapping it replaced, so it can restore that mapping exactly if the step reverses.

**Tech Stack:** Python 3.11, pytest (`uv run pytest`), ka9q-python ≥ 3.27 (`ChannelInfo.get_anchor()`), hamsci-dsp `CounterMap`.

**Spec:** none written. The authority is the 2026-11-13 AC0G-ND finding, summarised in *Findings* below.

## Findings (the authority for this plan)

1. At 11-12 12:00:06Z, 23:00:05Z and 11-13 04:10:05Z, `StreamRecorderV2._revalidate_radiod_pair_inner` logged "revalidation found radiod's advertised pair +0.97 / +0.998 / +0.858 s away from the adopted mapping … adopting". Exactly one check later, it logged the same size with the opposite sign and adopted again. radiod had not restarted.
2. Through all three events, the ka9q-python ANCHOR PAIR AUDIT kept its running maximum unchanged in psk-recorder (470 ms), wspr-recorder (470 ms) and meteor-scatter (402 ms), and also inside timestd-core-recorder itself (390 ms). So radiod sent no pair that was a second off, and hf-timestd's own listener received none.
3. `stream_recorder_v2.py:1076-1077` reads `ci.gps_time` and then `ci.rtp_timesnap` as two separate attributes. ka9q-python's `ChannelInfo.update_anchor()` writes the atomic `_anchor_pair` first, then the two legacy fields one at a time, and its docstring warns that reading the legacy fields as a pair can tear. `get_anchor()` is the safe read.
4. Each adoption folded the offset judge's correction into the ring anchor (`_update_ring_anchor`). After the round trip, 0.19–0.43 s stayed behind. Metrology then hard-rejected every WWV/WWVH detection (21σ), and L2 starved. A recorder restart cleared it.
5. Open point: one torn read should be off by about one status interval (~0.45 s), but the steps measured 0.86–1.0 s. Task 4 measures this in the field.
6. On a station where T6 is AUTHORITATIVE, the GPSDO measures UTC for a radiod input-sample index (`T3ShadowComparator._t6_index`), and each channel's `CounterMap` maps its RTP onto that index to about 0.5 µs. Together they give a GPSDO-based UTC for any channel's RTP that never touches `gps_time`.

## Global Constraints

- Timing faults are exposed, never silently corrected. Every refusal, adoption and restore is logged at WARNING or above, with a fixed prefix, and the same event is never applied silently.
- Restoring a mapping hf-timestd itself adopted in error undoes our own mistake. It does not correct radiod, and it counts as exposure as long as it is logged at ERROR.
- `REVALIDATE_ADOPT_THRESHOLD_S` stays at 0.75 s.
- Recording must never be disturbed: revalidation never raises (keep the existing `try/except` in `revalidate_radiod_pair`).
- No change to ka9q-python in this plan. A separate note to Rob proposes deprecating the legacy fields there.
- Tests run with `uv run pytest -q --override-ini addopts="-ra" <path>`. Put pytest temp under `~/.cache` (`--basetemp ~/.cache/pytest-tmp/anchor`).
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01BUMH4psju42aywNgiJv98Z`
- Work on `main`. Do not push; Michael pushes, and Rob is told about every repo change.

## Review Focus

1. **A ChannelInfo test fake with no `_anchor_pair`.** Many existing tests build ChannelInfo with `MagicMock` or `__new__`. `read_anchor_pair` must fall back to the legacy fields and must not crash on a `MagicMock` attribute. Pinned in Task 1 (`test_read_anchor_pair_mock_without_get_anchor`).
2. **A genuine radiod restart on a T6 station while T6 is also re-locking.** T6 isn't authoritative then, so the persistence path must still adopt within two checks (~120 s). Pinned in Task 2 (`test_no_t6_genuine_step_adopted_on_second_tick`).
3. **The T6 map and the channel map from different radiod runs** (right after a restart, the maps refresh at different times). The prediction must return None, not a value from a stale run. Pinned in Task 2 (`test_t6_prediction_none_across_runs`).
4. **RTP wrap (178 956.97 s at 24 kHz) during a pending step.** The comparison of a pending step with a fresh one must be wrap-safe. Pinned in Task 2 (`test_persistence_wrap_safe`).
5. **A restore after the writer has already rolled its chunk or file.** `restore_timing_mapping` must flush like `add_timing_snapshot` does and must not lose samples. Pinned in Task 3 (`test_restore_flushes_like_adopt`).

---

## File Structure

- Create `src/hf_timestd/core/anchor_pair.py`: `read_anchor_pair(ci)`, the one atomic read used everywhere.
- Create `src/hf_timestd/core/pair_step_arbiter.py`: pure step decisions, `T6Reference`, `t6_predicted_utc_s`, `PairStepArbiter`. No I/O.
- Modify `src/hf_timestd/core/stream_recorder_v2.py`: torn-read sites; revalidation uses the arbiter; keeps the previous mapping; restore; probe.
- Modify `src/hf_timestd/core/core_recorder_v2.py`: torn-read sites; provides `T6Reference` to each stream recorder.
- Modify `src/hf_timestd/core/ring_buffer.py:210`: torn-read site.
- Modify `src/hf_timestd/core/binary_archive_writer.py`: `current_mapping()` and `restore_timing_mapping()`.
- Tests: `tests/test_anchor_pair.py`, `tests/test_pair_step_arbiter.py`, `tests/test_revalidation_guard.py`.

---

### Task 1: Atomic pair read at every site

**Files:**
- Create: `src/hf_timestd/core/anchor_pair.py`
- Modify: `src/hf_timestd/core/stream_recorder_v2.py:466-467, 630-631, 1076-1077`
- Modify: `src/hf_timestd/core/core_recorder_v2.py:2078-2079, 2642-2645, 2663-2665, 2996-2997, 3315-3316, 3348-3349, 4048-4049`
- Modify: `src/hf_timestd/core/ring_buffer.py:210`
- Test: `tests/test_anchor_pair.py`

**Interfaces:**
- Produces: `read_anchor_pair(ci) -> Optional[tuple[int, int]]` returns `(gps_time_ns, rtp_timesnap)`, or None when `ci` is None or either half is None. It prefers `ci.get_anchor()` when that attribute is a real callable on a ka9q `ChannelInfo`, and otherwise reads the legacy fields. It never raises.

- [ ] **Step 1: Write the failing tests**

```python
from types import SimpleNamespace
from unittest.mock import MagicMock
from ka9q.discovery import ChannelInfo
from hf_timestd.core.anchor_pair import read_anchor_pair

def _torn_ci():
    ci = ChannelInfo.__new__(ChannelInfo)
    ci._anchor_pair = (2_000_000_000, 48_000)   # consistent pair
    ci.gps_time, ci.rtp_timesnap = 3_000_000_000, 48_000   # torn legacy fields
    return ci

def test_read_anchor_pair_prefers_atomic_pair():
    assert read_anchor_pair(_torn_ci()) == (2_000_000_000, 48_000)

def test_read_anchor_pair_legacy_fallback():
    assert read_anchor_pair(SimpleNamespace(gps_time=5, rtp_timesnap=7)) == (5, 7)

def test_read_anchor_pair_none_cases():
    assert read_anchor_pair(None) is None
    assert read_anchor_pair(SimpleNamespace(gps_time=None, rtp_timesnap=7)) is None

def test_read_anchor_pair_mock_without_get_anchor():
    m = MagicMock(spec=["gps_time", "rtp_timesnap"]); m.gps_time, m.rtp_timesnap = 1, 2
    assert read_anchor_pair(m) == (1, 2)

def test_no_paired_legacy_reads_left_in_src():
    # Every (gps_time, rtp_timesnap) pair read goes through read_anchor_pair.
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "hf_timestd"
    pat = re.compile(r"getattr\([^)]*'rtp_timesnap'|\b(ci|_ci|src_ci|dst_ci|channel_info)\.rtp_timesnap\b")
    hits = [f"{p}:{i}" for p in src.rglob("*.py") if p.name != "anchor_pair.py"
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if pat.search(line) and not line.lstrip().startswith("#")]
    assert hits == []
```

Also add one test in `tests/test_revalidation_guard.py`, `test_revalidation_ignores_torn_legacy_fields`. It builds a `StreamRecorderV2` the way `tests/test_stream_recorder_register_with.py` does, with a real `BinaryArchiveWriter` mock whose `evaluate_pair` returns `fresh_utc − implied`. It sets `channel_info=_torn_ci()` with a mapping adopted from `(2_000_000_000, 48_000)`, calls `revalidate_radiod_pair()`, and asserts `add_timing_snapshot` was not called.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest -q --override-ini addopts="-ra" --basetemp ~/.cache/pytest-tmp/anchor tests/test_anchor_pair.py tests/test_revalidation_guard.py`
Expected: FAIL. `anchor_pair` doesn't exist yet, and the source scan lists the 11 sites.

- [ ] **Step 3: Implement `read_anchor_pair` and replace every listed site with one call to it.** Where a site uses only one half, take that half from the tuple. Keep each site's existing None handling.

- [ ] **Step 4: Run the new tests plus the full suite**

Run: `uv run pytest -q --override-ini addopts="-ra" --basetemp ~/.cache/pytest-tmp/anchor tests/ ; echo exit=$?`
Expected: `exit=0`, and the pass count is at least the pre-change count plus the new tests.

- [ ] **Step 5: Commit** with the message `anchor pair: read (gps_time, rtp_timesnap) atomically at every site`. The body cites Findings 1–3 and gives the 11 sites.

---

### Task 2: Pair-step arbiter (GPSDO first, persistence otherwise)

**Files:**
- Create: `src/hf_timestd/core/pair_step_arbiter.py`
- Test: `tests/test_pair_step_arbiter.py`

**Interfaces:**
- Consumes: `NativeAnchor` (`core/native_anchor.py`), `CounterMap` (hamsci-dsp), `T3ShadowComparator._t6_index`. Promote it to a module-level `t6_anchor_input_index(anchor, t6_map) -> Optional[float]` in `t3_shadow_comparator.py`, and leave the static method calling it.
- Produces:
  - `@dataclass(frozen=True) class T6Reference: anchor: NativeAnchor; t6_map: CounterMap`
  - `t6_predicted_utc_s(rtp: int, chan_map: CounterMap, ref: T6Reference) -> Optional[float]` returns the UTC of channel sample `rtp` according to the GPSDO. It returns None when either map is missing, `_t6_index` is None, or the two maps name different radiod runs (`abs(chan_map.input_epoch_gps_s − ref.t6_map.input_epoch_gps_s) > COUNTER_EPOCH_SKEW_ALLOWANCE_S` from `counter_epoch_tracker`).
  - `class Verdict(enum.Enum): STEADY, ADOPT, HOLD, REFUSE_T6`
  - `class PairStepArbiter` with `decide(diff_s: float, fresh_utc_s: float, implied_utc_s: float, fresh_offset_s: float, t6_utc_s: Optional[float]) -> Verdict` and `reset() -> None`. `fresh_offset_s` is `gps_s − rtp/sample_rate`, which is constant within one counter run.
  - Constants: `T6_AGREE_S = 0.25`, `PERSIST_AGREE_S = 0.05`.

Decision rules (the exact algorithm):
1. `abs(diff_s) <= 0.75`: STEADY, and clear any pending step.
2. `t6_utc_s` is not None: ADOPT if `abs(fresh_utc_s − t6_utc_s) <= T6_AGREE_S` and `abs(implied_utc_s − t6_utc_s) > T6_AGREE_S`. Otherwise REFUSE_T6. Clear any pending step either way.
3. No T6, nothing pending: store `fresh_offset_s` as pending and return HOLD.
4. No T6, a step pending: ADOPT and clear it if the pending and fresh offsets agree within `PERSIST_AGREE_S`, compared modulo `2**32 / sample_rate` (wrap-safe). Otherwise replace the pending step with the fresh one and return HOLD.

`decide` takes `sample_rate` through the constructor: `PairStepArbiter(sample_rate: int)`.

- [ ] **Step 1: Write the failing tests**

```python
from hf_timestd.core.pair_step_arbiter import PairStepArbiter, Verdict

def test_small_disagreement_is_steady():
    assert PairStepArbiter(24000).decide(0.3, 100.3, 100.0, 5.0, None) is Verdict.STEADY

def test_t6_refuses_step_that_gpsdo_contradicts():
    # fresh pair 1 s off the GPSDO, mapping on it: the ND case
    assert PairStepArbiter(24000).decide(0.998, 101.0, 100.002, 5.0, 100.0) is Verdict.REFUSE_T6

def test_t6_adopts_step_that_gpsdo_confirms():
    assert PairStepArbiter(24000).decide(-0.97, 100.01, 100.98, 5.0, 100.0) is Verdict.ADOPT

def test_no_t6_single_step_held():
    assert PairStepArbiter(24000).decide(0.998, 101.0, 100.0, 6.0, None) is Verdict.HOLD

def test_no_t6_round_trip_never_adopted():
    a = PairStepArbiter(24000)
    assert a.decide(0.998, 101.0, 100.0, 6.0, None) is Verdict.HOLD
    assert a.decide(0.001, 160.0, 160.0, 5.0, None) is Verdict.STEADY

def test_no_t6_genuine_step_adopted_on_second_tick():
    a = PairStepArbiter(24000)
    assert a.decide(3.2, 103.2, 100.0, 8.2, None) is Verdict.HOLD
    assert a.decide(3.2, 163.2, 160.0, 8.21, None) is Verdict.ADOPT

def test_persistence_wrap_safe():
    wrap = 2**32 / 24000
    a = PairStepArbiter(24000)
    a.decide(3.2, 103.2, 100.0, 8.2, None)
    assert a.decide(3.2, 163.2, 160.0, 8.2 + wrap, None) is Verdict.ADOPT
```

Also add `test_t6_prediction_none_across_runs`: build two `CounterMap`s whose `input_epoch_gps_s` differ by 2 s, and assert `t6_predicted_utc_s(...)` is None. Add `test_t6_prediction_matches_anchor`: with maps from one run, the channel RTP that maps to T6's anchor index predicts `anchor.anchor_utc_ns / 1e9` to within 1 µs. Build the maps with the `CounterMap(...)` field list shown in hamsci-dsp `counter_map.py:48-58`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q --override-ini addopts="-ra" --basetemp ~/.cache/pytest-tmp/anchor tests/test_pair_step_arbiter.py`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the module and the `t6_anchor_input_index` promotion.**

- [ ] **Step 4: Run the tests to verify they pass**, plus `tests/` for any test of `t3_shadow_comparator`. Expected: PASS.

- [ ] **Step 5: Commit** with the message `pair-step arbiter: GPSDO decides when T6 is authoritative, else two-tick persistence`.

---

### Task 3: Revalidation uses the arbiter; restore on round trip

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py` (beside `evaluate_pair`, line ~746)
- Modify: `src/hf_timestd/core/stream_recorder_v2.py` (`publish_counter_map`, `_revalidate_radiod_pair_inner`, new provider setter)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (wire the provider wherever `set_label_anchor_provider` is wired)
- Test: `tests/test_revalidation_guard.py`

**Interfaces:**
- Consumes: Task 1 `read_anchor_pair`; Task 2 `PairStepArbiter`, `Verdict`, `T6Reference`, `t6_predicted_utc_s`.
- Produces:
  - `BinaryArchiveWriter.current_mapping() -> Optional[tuple[int, int]]` returns the adopted `(gps_time_ns, rtp_timesnap)` exactly as `add_timing_snapshot` received it.
  - `BinaryArchiveWriter.restore_timing_mapping(gps_time_ns: int, rtp_timesnap: int) -> bool` does what `add_timing_snapshot` does (flush the in-progress chunk, set the mapping, relay to the judge), and also tags the next chunk's metadata with `"anchor_restored": true`.
  - `StreamRecorderV2.set_t6_reference_provider(provider: Callable[[], Optional[T6Reference]]) -> None`. The core recorder's provider returns `T6Reference(self._t6_native_anchor, self._t6_counter_map)` only while `self._t6_authority_last_decision.state is T6AuthorityState.AUTHORITATIVE`, and None otherwise.
  - `StreamRecorderV2.publish_counter_map` also keeps `self._counter_map = cmap`.
  - `StreamRecorderV2.ROUND_TRIP_WINDOW_S = 600`

Behaviour of `_revalidate_radiod_pair_inner` once changed:
- Read the pair with `read_anchor_pair`.
- When `diff` exceeds the threshold, compute `t6_utc_s` with `t6_predicted_utc_s(rtp_snap, self._counter_map, provider())` if all three exist, and ask the arbiter.
- ADOPT: before adopting, save `self._prev_mapping = (writer.current_mapping(), self._ring_anchor_state, monotonic_now)`. Then adopt as today. Log WARNING `PAIR STEP ADOPTED (<t6|persistent>)`.
- HOLD: log WARNING `PAIR STEP HELD (awaiting second tick)`. No change.
- REFUSE_T6: log ERROR `PAIR STEP REFUSED: GPSDO says the counter did not move` with diff, `fresh−t6` and `implied−t6`. No change.
- Round trip: on any tick, if `self._prev_mapping` is younger than `ROUND_TRIP_WINDOW_S`, the fresh pair is more than 0.75 s from the current mapping, and it is within 0.75 s of the saved mapping (use `evaluate_pair` against the saved pair through a writer-free helper), then call `writer.restore_timing_mapping(*saved)`. Also restore the ring anchor from the saved `_ring_anchor_state` exactly, through `ring_buffer.update_anchor`, with no fresh judge offset. Log ERROR `ANCHOR ROUND TRIP: restored the mapping in force before <iso time>`, clear `_prev_mapping` and call `arbiter.reset()`. This check runs before the arbiter.

- [ ] **Step 1: Write the failing tests.** Use the same fixture style as Task 1's revalidation test.
  - `test_t6_refusal_leaves_mapping_untouched`: the provider returns a reference that predicts the current mapping, and the fresh pair is +0.998 s off. Assert `add_timing_snapshot` is not called and an ERROR record contains `PAIR STEP REFUSED`.
  - `test_no_t6_round_trip_nothing_adopted`: tick 1 sees +0.998 s, tick 2 sees a clean pair. Assert zero adoptions and no ring anchor update.
  - `test_round_trip_restores_exact_prior_mapping`: force an adoption by calling the ADOPT path directly, then present a pair consistent with the old mapping. Assert `restore_timing_mapping` was called with the saved pair, `ring_buffer.update_anchor` received the saved `gps_time_ns + saved_offset` exactly, and an ERROR record contains `ANCHOR ROUND TRIP`.
  - `test_restore_flushes_like_adopt` (in the writer's own tests, beside the existing `add_timing_snapshot` tests): after `restore_timing_mapping`, the in-progress chunk is flushed with no lost samples, `current_mapping()` returns the restored pair, and the next chunk carries `anchor_restored`.

- [ ] **Step 2: Run them to verify they fail.** Same command, for `tests/test_revalidation_guard.py` and the writer test file. Expected: FAIL.

- [ ] **Step 3: Implement** the writer methods, the provider setter and wiring, the saved mapping, the arbiter call and the restore.

- [ ] **Step 4: Run the full suite.** Expected: `exit=0`.

- [ ] **Step 5: Commit** with the message `revalidation: GPSDO-arbitrated or persistent pair steps only; restore on round trip`.

---

### Task 4: Field probe for the ~1 s magnitude

**Files:**
- Modify: `src/hf_timestd/core/stream_recorder_v2.py` (`_revalidate_radiod_pair_inner`)
- Test: `tests/test_revalidation_guard.py`

**Interfaces:**
- Produces: on each revalidation tick, after `read_anchor_pair`, the recorder also reads the two legacy fields. If they don't equal the atomic pair, it logs WARNING `TORN-READ PROBE: atomic=(g,r) legacy=(g,r) implied_diff=±x.xxx s` at most once per 300 s per channel, and increments `self.torn_read_probe_count`. The probe only observes and never feeds a decision.

- [ ] **Step 1: Write the failing test** `test_probe_logs_and_counts_torn_legacy_fields`. Using Task 1's `_torn_ci()`, assert one WARNING containing `TORN-READ PROBE`, `torn_read_probe_count == 1`, and no adoption. A second tick within 300 s logs nothing further but still counts (`== 2`).
- [ ] **Step 2: Run it to verify it fails.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the full suite.** Expected: `exit=0`.
- [ ] **Step 5: Commit** with the message `revalidation: torn-read probe (field measurement of the 11-12 step size)`.

---

## After the plan (not tasks)

- Michael pushes; Rob is told on the bus. The deploy order follows the hf-timestd deploy rule: B4 first, then ND, with W2NAF the best place to watch, since it re-based about 10 times a week.
- Success in the field: zero `revalidation found … adopting` lines without a matching radiod restart, and the probe counts how often the legacy fields tear. If a step of about 1 s still appears with the probe silent, Finding 5 has a second cause and the GPSDO refusal is what holds the line.
- Propose to Rob, for ka9q-python: deprecate the legacy `gps_time` / `rtp_timesnap` fields, or write them before `_anchor_pair`, with a DeprecationWarning on paired reads.
