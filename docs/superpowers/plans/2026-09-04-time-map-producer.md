# TimeMap producer Implementation Plan (hf-timestd)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hf-timestd fills a `TimeMap` per archive chunk from what it already knows — the T6 native anchor, the authority manager's snapshot (including the host-clock verdict), radiod's pair, the channel's facts — writes it into the per-chunk `timing` block as the schema v2 `state` record, publishes the station's `chain` record, and mirrors the host-clock verdict into the authority history store. Archive labels untouched (Phase 1: publish the map).

**Architecture:** A pure `TimeMapProducer` (`core/time_map_producer.py`) turns a `TimeMapInputs` bundle into a `TimeMap` using the hamsci-dsp builders; `BinaryArchiveWriter` gains a provider hook and a `counter_epoch_id` tracker, and its per-chunk `timing` block becomes the v2 state record with the legacy keys mirrored at top level for one release; `StreamRecorderV2` wires the provider from the recorder's anchor, authority state and an `AuthorityReader`; a `timing_chain_publisher` writes `/run/hf-timestd/timing_chain.json` from `[timing.provenance]` when the fusion service builds the authority runner; `AuthorityManager._write_snapshot` adds the four `host_clock_*` columns.

**Tech Stack:** Python 3.11 (station venv), hamsci-dsp ≥ 0.5.0 with `hamsci_dsp.timing_map` (Plan A, editable sibling), pytest via `.venv/bin/python -m pytest tests/<file> -p no:cacheprovider --override-ini="addopts=" -q` from the repo root (a test uses a relative `scripts/` path).

**Spec:** `docs/design/TIMING_PROVENANCE_MODEL.md` (amended 2026-09-04) §3.0, §3.1, §3.1.1, §3.2, §3.4, §6 deliverable 2; `docs/design/MEASUREMENT_MODEL.md` §3 (counter re-basing), §6.4 (lock credibility), §8; `docs/design/HOST_CLOCK_INTEGRITY.md`.

## Global Constraints

- Depends on hamsci-dsp Plan A (`hamsci_dsp.timing_map`: `TimeMap`, `sysclock_map`, `native_anchor_map`, `null_map`, `Chain`, `BudgetTerm`, `PAYLOAD_CHAIN_ID`, `SYSCLOCK_CHAIN_ID`; `AuthoritySnapshot.host_clock`). Land Plan A first.
- Archive writer's sample labels do not change (spec §6 Phase 1: "archive writer untouched"). Only metadata changes.
- The `timing` block keeps its legacy keys (`radiod_gps_time_ns`, `radiod_rtp_timesnap`, `offset_ns`, `offset_sigma_ns`, `judge_tier`, `judge_age_s`, `segment_id`, `rate_ppm`) at top level for one release so `hamsci_physics.grape.decimation_pipeline.timing_from_sidecar` keeps working until Plan C lands; the same values also appear under `engineering`. Retire the mirror one release after Plan C ships (record in §4.5-style deprecation note).
- Lock credibility for Phase 1 = the recorder's T6 anchor authority state is `AUTHORITATIVE` with no violations (`_t6_authority_status()`); the learned reference gate and stale-lock guard feed the same state. No new guard is built here (MEASUREMENT_MODEL §6.4 names them; HOST_CLOCK_INTEGRITY records that the LB-1421 path may be shut).
- `counter_epoch_id` changes when a newly adopted radiod pair disagrees with the mapping in force by more than 0.5 s (a re-base renumbers by seconds; MEASUREMENT_MODEL §3) and on the first adoption. Format `pair-<gps_time_ns>`.
- Never raise from the provenance path into the recording path: every new call is wrapped, failures log and fall back to `null_map` with the exception text as the reason (spec §7).
- Host clock and A-level come from `/run/hf-timestd/authority.json` via `hamsci_dsp.timing.AuthorityReader` (freshness 60 s). A stale or absent snapshot yields `a_level_provenance="assumed"`, `a_level` from config, `host_clock=None`.
- Develop on `main`; deploy is fast-forward plus restart together (lazy imports).

---

## File map

| file | responsibility |
|---|---|
| `src/hf_timestd/core/time_map_producer.py` (create) | `TimeMapInputs`, `TimeMapProducer.build()` |
| `src/hf_timestd/core/timing_chain_publisher.py` (create) | `[timing.provenance]` → `Chain`; write `/run/hf-timestd/timing_chain.json` |
| `src/hf_timestd/core/binary_archive_writer.py` (modify) | `set_time_map_provider`, `_counter_epoch_id`, `_chunk_timing_block` |
| `src/hf_timestd/core/stream_recorder_v2.py` (modify) | wire the provider |
| `src/hf_timestd/core/core_recorder_v2.py` (modify) | expose `time_map_context()` for a channel |
| `src/hf_timestd/core/authority_manager.py` (modify `_write_snapshot`) | `host_clock_*` columns |
| `src/hf_timestd/core/multi_broadcast_fusion.py:~4509` (modify) | publish the chain beside the authority runner |
| `src/hf_timestd/cli.py` (modify) | validate `[timing.provenance]` |
| `config/timestd-config.toml.template` (modify) | `[timing.provenance]` section |
| `tests/test_time_map_producer.py`, `tests/test_timing_chain_publisher.py`, `tests/test_binary_archive_writer_timing_v2.py` (create); `tests/test_authority_manager_host_clock.py`, `tests/test_cli_validate_host_clock.py` (modify) | |
| `docs/METROLOGY.md` §4.5 authority.json section, `docs/TIMING-PIPELINE-WIRING.md` (modify) | the v2 `timing` block and `timing_chain.json` |

---

### Task 1: Host-clock columns in the authority history snapshot

**Files:**
- Modify: `src/hf_timestd/core/authority_manager.py` (`_write_snapshot`, after `"disagreement_flags": list(state.disagreement_flags),`)
- Test: `tests/test_authority_manager_host_clock.py` (append)

**Interfaces:**
- Produces snapshot keys `host_clock_verdict` (str), `host_clock_since_utc` (str|None), `host_clock_t2_ms` (float|None, signed value of the T2 pair witness), `host_clock_lb1421_s` (float|None). Matches hamsci-dsp Plan A Task 5 columns.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_authority_manager_host_clock.py
class _RecordingStore:
    def __init__(self):
        self.rows = []
    def insert(self, snapshot):
        self.rows.append(dict(snapshot))


def test_snapshot_store_receives_flat_host_clock_columns(tmp_path):
    store = _RecordingStore()
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t2 = FakeProbe("T2", _sysclock("T2", -11679.507, witness_only=True))
    t5 = FakeProbe("T5", ProbeResult("T5", available=False, reason="no valid fix",
                                     detail={"host_minus_gps_s": -12.1}))
    m = _manager(tmp_path, [t6, t2, t5], snapshot_store=store)
    m.tick()
    row = store.rows[-1]
    assert row["host_clock_verdict"] == "fault"
    assert row["host_clock_since_utc"] == "2026-09-04T15:06:50.000000Z"
    assert row["host_clock_t2_ms"] == pytest.approx(11679.507, abs=0.001)
    assert row["host_clock_lb1421_s"] == -12.1


def test_snapshot_columns_are_none_when_unwitnessed(tmp_path):
    store = _RecordingStore()
    m = _manager(tmp_path, [FakeProbe("T6", _rtp("T6", 0.0, 0.004))], snapshot_store=store)
    m.tick()
    row = store.rows[-1]
    assert row["host_clock_verdict"] == "unwitnessed"
    assert row["host_clock_t2_ms"] is None and row["host_clock_lb1421_s"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_authority_manager_host_clock.py -p no:cacheprovider --override-ini="addopts=" -q -k snapshot`
Expected: `KeyError: 'host_clock_verdict'`

- [ ] **Step 3: Implement**

In `_write_snapshot`, after the `"disagreement_flags"` line inside the `snapshot` dict literal, nothing; after the dict is built add:

```python
        hc = state.host_clock or {}
        witnesses = hc.get("witnesses") or {}
        snapshot["host_clock_verdict"] = hc.get("verdict")
        snapshot["host_clock_since_utc"] = hc.get("since_utc")
        snapshot["host_clock_t2_ms"] = (witnesses.get("T2") or {}).get("value")
        snapshot["host_clock_lb1421_s"] = (witnesses.get("lb1421") or {}).get("value")
```

- [ ] **Step 4: Run the manager tests**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_authority_manager_host_clock.py tests/test_authority_manager.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/authority_manager.py tests/test_authority_manager_host_clock.py
git commit -m "authority_manager: mirror the host-clock verdict into the history store as four flat columns

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 2: `TimeMapProducer` — one function from inputs to map

**Files:**
- Create: `src/hf_timestd/core/time_map_producer.py`
- Test: `tests/test_time_map_producer.py`

**Interfaces:**
- Consumes: `hamsci_dsp.timing_map.{native_anchor_map, sysclock_map, null_map}`, `hamsci_dsp.timing.AuthoritySnapshot` (fields `a_level`, `host_clock`, `sigma_ns`).
- Produces:
  - `TimeMapInputs(counter_space: str, counter_epoch_id: str, f_s_hz: int, measured_at_utc_ns: int, gps_time_ns: Optional[int] = None, rtp_timesnap: Optional[int] = None, anchor_rtp: Optional[int] = None, anchor_utc_ns: Optional[int] = None, anchor_sigma_ns: Optional[int] = None, lock_credible: bool = False, judge_tier: Optional[str] = None, engineering: Optional[dict] = None)`
  - `TimeMapProducer(snapshot_fn: Callable[[], Optional[AuthoritySnapshot]], a_level_config: str = "A0")`; `.build(inputs: TimeMapInputs) -> TimeMap` — never raises.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_time_map_producer.py
"""One function from what the recorder knows to the TimeMap it publishes.
TIMING_PROVENANCE_MODEL §3.1 / §3.1.1; MEASUREMENT_MODEL §6.4, §8."""
from __future__ import annotations

from datetime import datetime, timezone

from hamsci_dsp.timing import AuthoritySnapshot
from hf_timestd.core.time_map_producer import TimeMapInputs, TimeMapProducer

T0 = 1_788_537_251_999_997_458
HC_FAULT = {"verdict": "fault", "reason": "T2 disagrees by 11679.5 ms (> 1000 ms)",
            "witnesses": {"T2": {"kind": "pair_ms", "value": 11679.507, "bound": 60.0, "exceeded": True}},
            "since_utc": "2026-09-04T02:47:12.000000Z"}


def _snap(host_clock=None, a_level="A1"):
    return AuthoritySnapshot(
        utc_published=datetime(2026, 9, 4, 16, 10, tzinfo=timezone.utc), a_level=a_level,
        t_level_active="T6", t_level_available=["T6"], t_level_witnesses=["T2"],
        rtp_to_utc_offset_ns=0, sigma_ns=4093, stations_contributing=[],
        last_transition_utc=None, disagreement_flags=[], governor_radiod="AC0G-B4-status.local",
        host_clock=host_clock,
    )


def _inputs(**kw):
    base = dict(counter_space="AC0G-B4-status.local/T6_96000", counter_epoch_id="pair-1",
                f_s_hz=96_000, measured_at_utc_ns=T0,
                gps_time_ns=T0 - 12_000_000, rtp_timesnap=2_150_318_060,
                engineering={"judge_age_s": 0.3})
    base.update(kw)
    return TimeMapInputs(**base)


def test_credible_anchor_yields_native_anchor_map_with_observed_a_level():
    p = TimeMapProducer(snapshot_fn=lambda: _snap(host_clock={"verdict": "ok", "witnesses": {}}))
    m = p.build(_inputs(anchor_rtp=2_150_319_213, anchor_utc_ns=T0, anchor_sigma_ns=4093,
                        lock_credible=True, judge_tier="T6"))
    assert m.origin == "native_anchor" and m.u_epoch_ns == 4093
    assert (m.a_level, m.a_level_provenance) == ("A1", "observed")
    assert m.engineering["judge_tier"] == "T6"
    assert m.engineering["host_clock"]["verdict"] == "ok"
    assert m.utc_ns_at(2_150_319_213) == T0


def test_uncredible_anchor_does_not_fall_back_to_the_pair_silently():
    # A wrong edge registers nothing; the pair would be a different chain and
    # the reader must see the refusal, not a quiet downgrade.
    p = TimeMapProducer(snapshot_fn=lambda: _snap())
    m = p.build(_inputs(anchor_rtp=1, anchor_utc_ns=T0, anchor_sigma_ns=4000, lock_credible=False))
    assert m.origin is None and "lock_not_credible" in m.reason


def test_no_anchor_yields_sysclock_map_bounded_by_the_host_clock():
    p = TimeMapProducer(snapshot_fn=lambda: _snap(host_clock=HC_FAULT))
    m = p.build(_inputs())
    assert m.origin == "sysclock" and m.chain == "sysclock@1"
    assert m.u_epoch_ns == 11_679_507_000 and m.k == 1
    assert (m.n0, m.t0_utc_ns) == (2_150_318_060, T0 - 12_000_000)


def test_no_snapshot_means_assumed_a_level_and_no_host_clock():
    p = TimeMapProducer(snapshot_fn=lambda: None, a_level_config="A1")
    m = p.build(_inputs())
    assert (m.a_level, m.a_level_provenance) == ("A1", "assumed")
    assert m.u_epoch_ns == 8_030_000
    assert "host_clock" not in m.engineering


def test_nothing_at_all_is_a_null_map_with_a_reason():
    p = TimeMapProducer(snapshot_fn=lambda: None)
    m = p.build(_inputs(gps_time_ns=None, rtp_timesnap=None))
    assert m.origin is None and "no anchor" in m.reason and "no radiod pair" in m.reason


def test_a_raising_snapshot_fn_never_reaches_the_recorder():
    def boom():
        raise RuntimeError("authority.json unreadable")
    p = TimeMapProducer(snapshot_fn=boom, a_level_config="A0")
    m = p.build(_inputs())
    assert m.origin == "sysclock" and m.a_level_provenance == "assumed"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_time_map_producer.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `ModuleNotFoundError: No module named 'hf_timestd.core.time_map_producer'`

- [ ] **Step 3: Implement**

```python
# src/hf_timestd/core/time_map_producer.py
"""From what the recorder knows to the TimeMap it publishes.

TIMING_PROVENANCE_MODEL §3.1: the per-chunk `timing` block publishes the
registration in force.  §3.1.1: every rung fills the shape.  This module
decides which builder applies and never raises into the recording path.

Precedence.  A credible T6 anchor registers the payload-anchored chain.  An
anchor that is not credible registers NOTHING — it does not fall back to the
pair, because that would swap chains without saying so.  No anchor at all
and a radiod pair registers the sysclock chain.  Nothing is absence with a
reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from hamsci_dsp.timing import AuthoritySnapshot
from hamsci_dsp.timing_map import TimeMap, native_anchor_map, null_map, sysclock_map

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeMapInputs:
    counter_space: str
    counter_epoch_id: str
    f_s_hz: int
    measured_at_utc_ns: int
    gps_time_ns: Optional[int] = None
    rtp_timesnap: Optional[int] = None
    anchor_rtp: Optional[int] = None
    anchor_utc_ns: Optional[int] = None
    anchor_sigma_ns: Optional[int] = None
    lock_credible: bool = False
    judge_tier: Optional[str] = None
    engineering: Optional[dict] = field(default=None)


class TimeMapProducer:
    def __init__(self, snapshot_fn: Callable[[], Optional[AuthoritySnapshot]],
                 a_level_config: str = "A0") -> None:
        self._snapshot_fn = snapshot_fn
        self._a_level_config = str(a_level_config)

    def _snapshot(self) -> Optional[AuthoritySnapshot]:
        try:
            return self._snapshot_fn()
        except Exception as exc:  # noqa: BLE001 — provenance never disturbs recording
            logger.debug("time map: authority snapshot unavailable: %s", exc)
            return None

    def build(self, inputs: TimeMapInputs) -> TimeMap:
        snap = self._snapshot()
        if snap is not None:
            a_level, provenance, host_clock = snap.a_level, "observed", snap.host_clock
        else:
            a_level, provenance, host_clock = self._a_level_config, "assumed", None
        eng = dict(inputs.engineering or {})
        if inputs.judge_tier is not None:
            eng["judge_tier"] = inputs.judge_tier
        common = dict(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                      measured_at_utc_ns=inputs.measured_at_utc_ns,
                      a_level=a_level, a_level_provenance=provenance)
        try:
            if inputs.anchor_rtp is not None and inputs.anchor_utc_ns is not None:
                return native_anchor_map(
                    anchor_rtp=inputs.anchor_rtp, anchor_utc_ns=inputs.anchor_utc_ns,
                    sample_rate_hz=inputs.f_s_hz, sigma_ns=inputs.anchor_sigma_ns,
                    lock_credible=inputs.lock_credible, host_clock=host_clock,
                    engineering=eng, **common)
            if inputs.gps_time_ns is not None and inputs.rtp_timesnap is not None:
                return sysclock_map(
                    gps_time_ns=inputs.gps_time_ns, rtp_timesnap=inputs.rtp_timesnap,
                    f_s_hz=inputs.f_s_hz, host_clock=host_clock, engineering=eng, **common)
            return null_map(f_s_hz=inputs.f_s_hz, reason="no anchor and no radiod pair",
                            engineering=eng, **common)
        except Exception as exc:  # noqa: BLE001
            logger.warning("time map: builder failed (%s); publishing absence", exc)
            return null_map(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                            f_s_hz=inputs.f_s_hz, measured_at_utc_ns=inputs.measured_at_utc_ns,
                            reason=f"builder error: {exc}", a_level=a_level,
                            a_level_provenance=provenance, engineering=eng)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_time_map_producer.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/time_map_producer.py tests/test_time_map_producer.py
git commit -m "time_map_producer: a credible anchor registers the payload chain, no anchor registers the pair, a bad lock registers nothing

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 3: `counter_epoch_id` in the archive writer

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py` — `__init__` (add attributes beside `self._offset_judge = offset_judge`), the pair-adoption site (locate with `grep -n "_gps_time_ns_raw = " src/hf_timestd/core/binary_archive_writer.py`; every assignment of the adopted pair calls the new method), and a new method.
- Test: `tests/test_binary_archive_writer_timing_v2.py` (create)

**Interfaces:**
- Produces: `BinaryArchiveWriter._counter_epoch_id: Optional[str]`; `BinaryArchiveWriter._note_counter_epoch(gps_time_ns: int, rtp_timesnap: int) -> str` (returns the id in force after the note); `BinaryArchiveWriter.counter_epoch_id -> str` property (`"unregistered"` before any adoption).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_binary_archive_writer_timing_v2.py
"""The archive writer names the counter epoch and publishes the v2 state
record in its per-chunk timing block.  MEASUREMENT_MODEL §3: a radiod
restart renumbers the samples; no consumer extrapolates across it."""
from __future__ import annotations

from hf_timestd.core.binary_archive_writer import BinaryArchiveWriter


def _bare():
    w = BinaryArchiveWriter.__new__(BinaryArchiveWriter)
    w._counter_epoch_id = None
    w._counter_epoch_pair = None
    return w


def test_first_adoption_opens_an_epoch_named_by_the_pair():
    w = _bare()
    assert w.counter_epoch_id == "unregistered"
    assert w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000) == "pair-1788537000000000000"


def test_a_pair_that_agrees_with_the_mapping_keeps_the_epoch():
    w = _bare()
    w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000)
    # ten seconds later, 960,000 samples on, 3 ms of pair jitter: same counter
    assert w._note_counter_epoch(1_788_537_010_003_000_000, 965_000, 96_000) == "pair-1788537000000000000"


def test_a_pair_seconds_off_the_mapping_opens_a_new_epoch():
    w = _bare()
    w._note_counter_epoch(1_788_537_000_000_000_000, 5_000, 96_000)
    # radiod restarted: counter re-based near zero while UTC moved 60 s
    new = w._note_counter_epoch(1_788_537_060_000_000_000, 7, 96_000)
    assert new == "pair-1788537060000000000" and w.counter_epoch_id == new
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_binary_archive_writer_timing_v2.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `AttributeError: 'BinaryArchiveWriter' object has no attribute 'counter_epoch_id'`

- [ ] **Step 3: Implement**

In `__init__`, beside `self._offset_judge = offset_judge`:

```python
        # MEASUREMENT_MODEL §3 — the counter epoch.  radiod renumbers samples
        # on restart; a registration carried across that errs by seconds.
        # A new epoch opens when an adopted pair disagrees with the mapping
        # in force by more than COUNTER_EPOCH_STEP_S.
        self._counter_epoch_id: Optional[str] = None
        self._counter_epoch_pair: Optional[tuple] = None   # (gps_time_ns, rtp_timesnap, sample_rate)
```

Module-level constant near the top of the file: `COUNTER_EPOCH_STEP_S = 0.5`.

New methods (place after `set_offset_judge`):

```python
    @property
    def counter_epoch_id(self) -> str:
        return self._counter_epoch_id or "unregistered"

    def _note_counter_epoch(self, gps_time_ns: int, rtp_timesnap: int, sample_rate: int) -> str:
        """Open a new counter epoch when the adopted pair disagrees with the
        mapping in force by more than COUNTER_EPOCH_STEP_S; else keep it."""
        prev = self._counter_epoch_pair
        if prev is not None:
            p_gps, p_rtp, p_sr = prev
            delta = (int(rtp_timesnap) - int(p_rtp)) & 0xFFFFFFFF
            if delta > 0x7FFFFFFF:
                delta -= 0x1_0000_0000
            predicted_ns = int(p_gps) + 1_000_000_000 * delta // int(p_sr)
            if abs(int(gps_time_ns) - predicted_ns) <= COUNTER_EPOCH_STEP_S * 1e9:
                self._counter_epoch_pair = (int(gps_time_ns), int(rtp_timesnap), int(sample_rate))
                return self.counter_epoch_id
            logger.warning(
                f"{getattr(self.config, 'channel_name', '?')}: adopted pair sits "
                f"{(int(gps_time_ns) - predicted_ns) / 1e9:+.3f} s from the mapping in force — "
                f"counter re-based; opening a new counter epoch")
        self._counter_epoch_pair = (int(gps_time_ns), int(rtp_timesnap), int(sample_rate))
        self._counter_epoch_id = f"pair-{int(gps_time_ns)}"
        return self._counter_epoch_id
```

Then at every site that assigns the adopted pair (`self._gps_time_ns_raw = ...` together with `self._rtp_timesnap = ...`), add immediately after the assignment:

```python
        self._note_counter_epoch(gps_time_ns, rtp_timesnap, self.config.sample_rate)
```

using the local variable names in force at that site (read the surrounding lines; the pair is adopted from radiod status).

- [ ] **Step 4: Run the writer tests and the whole suite**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests -p no:cacheprovider --override-ini="addopts=" -q`
Expected: all pass (the `_bare()` fixture in the new test sets the two attributes because `__new__` skips `__init__`; any existing test that constructs the writer through `__init__` gets them from there).

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/binary_archive_writer.py tests/test_binary_archive_writer_timing_v2.py
git commit -m "binary_archive_writer: name the counter epoch; a pair seconds off the mapping opens a new one

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 4: The per-chunk `timing` block becomes the v2 state record

**Files:**
- Modify: `src/hf_timestd/core/binary_archive_writer.py` — `__init__` (provider attribute), new `set_time_map_provider`, new `_chunk_timing_block`, the chunk-start site that builds `judge_timing` (locate: `grep -n "judge_timing = None" src/hf_timestd/core/binary_archive_writer.py`).
- Test: `tests/test_binary_archive_writer_timing_v2.py` (append)

**Interfaces:**
- Consumes: `TimeMapInputs`, `TimeMap` (Plan A), the writer's `_gps_time_ns_raw`, `_rtp_timesnap`, `counter_epoch_id`, `config.sample_rate`, `config.channel_name`, the judge `verdict`.
- Produces: `set_time_map_provider(provider: Callable[[TimeMapInputs], TimeMap], counter_space: str) -> None`; `_chunk_timing_block(verdict, chunk_boundary_utc_ns: int) -> Optional[dict]` — the dict written as `metadata['timing']`: the v2 state record plus the legacy top-level keys.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_binary_archive_writer_timing_v2.py
from types import SimpleNamespace

from hamsci_dsp.timing_map import TimeMap
from hf_timestd.core.time_map_producer import TimeMapInputs

T0 = 1_788_537_251_999_997_458


class _Verdict(SimpleNamespace):
    pass


def _writer_with_provider(provider):
    w = _bare()
    w.config = SimpleNamespace(sample_rate=96_000, channel_name="T6_96000")
    w._gps_time_ns_raw = T0 - 12_000_000
    w._rtp_timesnap = 2_150_318_060
    w._note_counter_epoch(w._gps_time_ns_raw, w._rtp_timesnap, 96_000)
    w._time_map_provider = None
    w._time_map_counter_space = None
    w.set_time_map_provider(provider, counter_space="AC0G-B4-status.local/T6_96000")
    return w


def _verdict():
    return _Verdict(offset_ns=3_536_564.66, sigma_ns=982_495.95, tier="T6",
                    judge_age_s=0.3, segment_id=1, rate_ppm=None)


def test_timing_block_is_the_v2_state_record_with_legacy_keys_mirrored():
    seen = {}
    def provider(inputs: TimeMapInputs) -> TimeMap:
        seen["inputs"] = inputs
        from hamsci_dsp.timing_map import native_anchor_map
        return native_anchor_map(counter_space=inputs.counter_space,
                                 counter_epoch_id=inputs.counter_epoch_id,
                                 anchor_rtp=2_150_319_213, anchor_utc_ns=T0, sample_rate_hz=96_000,
                                 measured_at_utc_ns=T0, sigma_ns=4093, lock_credible=True,
                                 a_level="A1", a_level_provenance="observed",
                                 engineering=inputs.engineering)
    w = _writer_with_provider(provider)
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0 + 600 * 10**9)

    # v2 state record
    assert block["type"] == "state" and block["schema"] == "v2"
    assert block["origin"] == "native_anchor" and block["u_epoch_ns"] == 4093
    assert block["counter_epoch_id"].startswith("pair-")
    assert block["engineering"]["judge_tier"] == "T6"
    assert block["engineering"]["radiod_gps_time_ns"] == T0 - 12_000_000
    # legacy keys mirrored at top level for one release (Plan C retires the mirror)
    assert block["offset_ns"] == 3_536_564.66 and block["offset_sigma_ns"] == 982_495.95
    assert block["judge_tier"] == "T6" and block["radiod_rtp_timesnap"] == 2_150_318_060
    # the provider saw the writer's facts
    inp = seen["inputs"]
    assert inp.counter_space == "AC0G-B4-status.local/T6_96000"
    assert inp.gps_time_ns == T0 - 12_000_000 and inp.rtp_timesnap == 2_150_318_060
    assert inp.f_s_hz == 96_000 and inp.judge_tier == "T6"


def test_without_a_provider_the_legacy_block_is_unchanged():
    w = _bare()
    w.config = SimpleNamespace(sample_rate=96_000, channel_name="T6_96000")
    w._gps_time_ns_raw, w._rtp_timesnap = 1, 2
    w._time_map_provider = None
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0)
    assert set(block) == {"radiod_gps_time_ns", "radiod_rtp_timesnap", "offset_ns", "offset_sigma_ns",
                          "judge_tier", "judge_age_s", "segment_id", "rate_ppm"}


def test_no_verdict_and_no_provider_means_no_block():
    w = _bare()
    w.config = SimpleNamespace(sample_rate=96_000, channel_name="x")
    w._gps_time_ns_raw, w._rtp_timesnap = 1, 2
    w._time_map_provider = None
    assert w._chunk_timing_block(None, chunk_boundary_utc_ns=T0) is None


def test_a_provider_that_raises_yields_a_null_map_not_a_crash():
    def boom(inputs):
        raise RuntimeError("no authority")
    w = _writer_with_provider(boom)
    block = w._chunk_timing_block(_verdict(), chunk_boundary_utc_ns=T0)
    assert block["origin"] is None and "no authority" in block["reason"]
    assert block["offset_ns"] == 3_536_564.66      # legacy mirror still present
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_binary_archive_writer_timing_v2.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `AttributeError: ... has no attribute 'set_time_map_provider'`

- [ ] **Step 3: Implement**

`__init__` additions beside the counter-epoch attributes:

```python
        # TIMING_PROVENANCE_MODEL §3.1 — the per-chunk timing block publishes
        # the registration in force.  Late-bound by the recorder.
        self._time_map_provider = None
        self._time_map_counter_space: Optional[str] = None
```

New methods after `_note_counter_epoch`:

```python
    def set_time_map_provider(self, provider, counter_space: str) -> None:
        """Late-bind the TimeMap provider (a callable TimeMapInputs -> TimeMap)
        and this channel's counter-space name."""
        self._time_map_provider = provider
        self._time_map_counter_space = str(counter_space)

    def _legacy_timing_keys(self, verdict) -> dict:
        return {
            'radiod_gps_time_ns': self._gps_time_ns_raw,
            'radiod_rtp_timesnap': self._rtp_timesnap,
            'offset_ns': float(verdict.offset_ns),
            'offset_sigma_ns': float(verdict.sigma_ns),
            'judge_tier': verdict.tier,
            'judge_age_s': float(verdict.judge_age_s),
            'segment_id': int(verdict.segment_id),
            'rate_ppm': (float(verdict.rate_ppm)
                         if getattr(verdict, 'rate_ppm', None) is not None else None),
        }

    def _chunk_timing_block(self, verdict, chunk_boundary_utc_ns: int) -> Optional[dict]:
        """The `timing` block of a chunk's JSON sidecar.

        With a provider: the schema v2 `state` record (TIMING_PROVENANCE_MODEL
        §3.1) with the legacy Offset Judge keys mirrored at top level for one
        release, so hamsci-physics' timing_from_sidecar keeps reading until
        it moves to u_epoch_ns.  Without a provider: the legacy block alone.
        Never raises."""
        legacy = self._legacy_timing_keys(verdict) if verdict is not None else None
        provider = self._time_map_provider
        if provider is None:
            return legacy
        from hf_timestd.core.time_map_producer import TimeMapInputs
        from hamsci_dsp.timing_map import null_map
        eng = dict(legacy) if legacy is not None else {
            'radiod_gps_time_ns': self._gps_time_ns_raw,
            'radiod_rtp_timesnap': self._rtp_timesnap}
        inputs = TimeMapInputs(
            counter_space=self._time_map_counter_space or self.config.channel_name,
            counter_epoch_id=self.counter_epoch_id,
            f_s_hz=int(self.config.sample_rate),
            measured_at_utc_ns=int(chunk_boundary_utc_ns),
            gps_time_ns=self._gps_time_ns_raw, rtp_timesnap=self._rtp_timesnap,
            judge_tier=(verdict.tier if verdict is not None else None),
            engineering=eng,
        )
        try:
            tmap = provider(inputs)
        except Exception as exc:  # noqa: BLE001 — provenance never disturbs recording
            logger.warning(f"{self.config.channel_name}: time map provider failed: {exc}")
            tmap = null_map(counter_space=inputs.counter_space, counter_epoch_id=inputs.counter_epoch_id,
                            f_s_hz=inputs.f_s_hz, measured_at_utc_ns=inputs.measured_at_utc_ns,
                            reason=f"provider error: {exc}", engineering=eng)
        block = tmap.to_state_record(int(chunk_boundary_utc_ns))
        if legacy is not None:
            block.update(legacy)      # top-level mirror, one release
        return block
```

At the chunk-start site, replace the `judge_timing = None / if verdict is not None: judge_timing = {...}` construction with:

```python
        judge_timing = self._chunk_timing_block(
            verdict, chunk_boundary_utc_ns=int(round(float(chunk_boundary) * 1e9)))
```

(`chunk_boundary` is the epoch-seconds float already in scope there; keep the variable name `judge_timing` so `MinuteBuffer(judge_timing=...)` and the metadata write are untouched.)

- [ ] **Step 4: Run the writer tests and the whole suite**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests -p no:cacheprovider --override-ini="addopts=" -q`
Expected: all pass. `tests/test_chain_delay_ns_rename.py` and any test reading `metadata['timing']` keep passing because the legacy keys are still present.

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/binary_archive_writer.py tests/test_binary_archive_writer_timing_v2.py
git commit -m "binary_archive_writer: the chunk timing block becomes the v2 state record; legacy keys mirrored for one release

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 5: Wire the provider from the recorder

**Files:**
- Modify: `src/hf_timestd/core/core_recorder_v2.py` — new method `time_map_context(channel_name) -> dict` next to `_t6_lbe1421_status`; the site that constructs `StreamRecorderV2` (locate: `grep -n "StreamRecorderV2(" src/hf_timestd/core/core_recorder_v2.py`).
- Modify: `src/hf_timestd/core/stream_recorder_v2.py` — new method `wire_time_map(context_fn, snapshot_fn, a_level_config)`; call it from the same place `_wire_offset_judge`/`register_with` sets the judge.
- Test: `tests/test_core_recorder_time_map_context.py` (create)

**Interfaces:**
- Produces: `CoreRecorderV2.time_map_context(channel_name: str) -> dict` with keys `anchor_rtp`, `anchor_utc_ns`, `anchor_sigma_ns`, `lock_credible`, `counter_space` — anchor keys None unless `channel_name` is the T6 channel and `_t6_native_anchor` is set; `lock_credible = (state == "AUTHORITATIVE" and not violations)` from `_t6_authority_status()`; `anchor_sigma_ns` from the T6 authority's published sigma if available else None; `counter_space = f"{status_stream}/{channel_name}"`.
- `StreamRecorderV2.wire_time_map(context_fn: Callable[[str], dict], snapshot_fn: Callable[[], Optional[AuthoritySnapshot]], a_level_config: str) -> None` builds a `TimeMapProducer` and a provider closure that merges `context_fn(self.config.description)` into the writer's `TimeMapInputs`, then calls `self.archive_writer.set_time_map_provider(provider, counter_space)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_core_recorder_time_map_context.py
"""The recorder hands the archive writer what it knows about the channel's
registration: the T6 anchor, whether its lock is credible, the counter space."""
from unittest.mock import MagicMock

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.native_anchor import NativeAnchor

T0 = 1_788_537_251_999_997_458


def _bare(anchor=None, state="AUTHORITATIVE", violations=()):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_native_anchor = anchor
    cr._t6_channel_name = "T6_96000"
    cr._status_stream_name = "AC0G-B4-status.local"
    cr._t6_authority_status = MagicMock(return_value={
        "state": state, "violations": list(violations), "sigma_ns": 4093})
    return cr


def test_t6_channel_with_authoritative_anchor_is_credible():
    a = NativeAnchor(anchor_rtp=2_150_319_213, anchor_utc_ns=T0, sample_rate_hz=96_000,
                     chain_delay_ns=0, captured_at_utc_ns=T0, captured_via_tier="T5")
    ctx = _bare(anchor=a).time_map_context("T6_96000")
    assert ctx["anchor_rtp"] == 2_150_319_213 and ctx["anchor_utc_ns"] == T0
    assert ctx["anchor_sigma_ns"] == 4093 and ctx["lock_credible"] is True
    assert ctx["counter_space"] == "AC0G-B4-status.local/T6_96000"


def test_acquiring_state_is_not_credible():
    a = NativeAnchor(anchor_rtp=1, anchor_utc_ns=T0, sample_rate_hz=96_000,
                     chain_delay_ns=0, captured_at_utc_ns=T0, captured_via_tier="T5")
    ctx = _bare(anchor=a, state="ACQUIRING").time_map_context("T6_96000")
    assert ctx["lock_credible"] is False and ctx["anchor_rtp"] == 1


def test_other_channels_carry_no_anchor():
    ctx = _bare().time_map_context("SHARED_10000")
    assert ctx["anchor_rtp"] is None and ctx["lock_credible"] is False
    assert ctx["counter_space"] == "AC0G-B4-status.local/SHARED_10000"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_core_recorder_time_map_context.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `AttributeError: 'CoreRecorderV2' object has no attribute 'time_map_context'`

- [ ] **Step 3: Implement**

In `core_recorder_v2.py`, next to `_t5_lbe1421_status`:

```python
    def time_map_context(self, channel_name: str) -> dict:
        """What this recorder knows about a channel's registration, for the
        archive writer's TimeMap (TIMING_PROVENANCE_MODEL §3.1).

        The anchor belongs to the T6 channel only — registration transfer to
        another counter space is its own measurement (MEASUREMENT_MODEL §3)
        and Phase 1 does not perform it.  Lock credibility is the anchor
        authority's verdict: AUTHORITATIVE with no violations
        (MEASUREMENT_MODEL §6.4)."""
        stream = getattr(self, '_status_stream_name', None) or 'radiod'
        ctx = {
            'anchor_rtp': None, 'anchor_utc_ns': None, 'anchor_sigma_ns': None,
            'lock_credible': False,
            'counter_space': f"{stream}/{channel_name}",
        }
        anchor = getattr(self, '_t6_native_anchor', None)
        if anchor is None or channel_name != getattr(self, '_t6_channel_name', None):
            return ctx
        try:
            auth = self._t6_authority_status() or {}
        except Exception:  # noqa: BLE001
            auth = {}
        ctx['anchor_rtp'] = int(anchor.anchor_rtp)
        ctx['anchor_utc_ns'] = int(anchor.anchor_utc_ns)
        sigma = auth.get('sigma_ns')
        ctx['anchor_sigma_ns'] = int(sigma) if isinstance(sigma, (int, float)) else None
        ctx['lock_credible'] = (auth.get('state') == 'AUTHORITATIVE'
                                and not (auth.get('violations') or []))
        return ctx
```

Confirm the attribute names `_t6_channel_name` and `_status_stream_name` exist on the recorder (`grep -n "_t6_channel_name\b\|_status_stream_name\b" src/hf_timestd/core/core_recorder_v2.py`); if the recorder names them differently, use its names in the method and the test fixture, and make sure `_t6_authority_status()` returns `sigma_ns` (add it there from the authority's published sigma if it does not; the offset judge's T6 bench sigma is the fallback).

In `stream_recorder_v2.py`, add:

```python
    def wire_time_map(self, context_fn, snapshot_fn, a_level_config: str = "A0") -> None:
        """Give the archive writer a TimeMap provider (TIMING_PROVENANCE_MODEL §3.1).
        Best-effort: a wiring failure leaves the legacy timing block in place."""
        if self.archive_writer is None:
            return
        try:
            from hf_timestd.core.time_map_producer import TimeMapInputs, TimeMapProducer
            from dataclasses import replace
            producer = TimeMapProducer(snapshot_fn=snapshot_fn, a_level_config=a_level_config)
            channel = self.config.description

            def provider(inputs: TimeMapInputs):
                ctx = context_fn(channel) or {}
                return producer.build(replace(
                    inputs,
                    anchor_rtp=ctx.get('anchor_rtp'), anchor_utc_ns=ctx.get('anchor_utc_ns'),
                    anchor_sigma_ns=ctx.get('anchor_sigma_ns'),
                    lock_credible=bool(ctx.get('lock_credible', False))))

            ctx0 = context_fn(channel) or {}
            self.archive_writer.set_time_map_provider(
                provider, counter_space=ctx0.get('counter_space') or channel)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{self.config.description}: time map wiring failed "
                           f"(legacy timing block stays): {exc}")
```

At the recorder's `StreamRecorderV2(...)` construction site (or right after the judge wiring in `register_with`), add:

```python
        from hamsci_dsp.timing import AuthorityReader
        _reader = AuthorityReader()          # /run/hf-timestd/authority.json, 60 s freshness
        recorder.wire_time_map(
            context_fn=self.time_map_context,
            snapshot_fn=_reader.read,
            a_level_config=str(((self.config.get('timing', {}) or {})
                                 .get('authority_manager', {}) or {}).get('a_level', 'A0')),
        )
```

(`self.config` is the loaded TOML dict on the core recorder; confirm its name with `grep -n "self.config = " src/hf_timestd/core/core_recorder_v2.py`.)

- [ ] **Step 4: Run the suite**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests -p no:cacheprovider --override-ini="addopts=" -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/core_recorder_v2.py src/hf_timestd/core/stream_recorder_v2.py tests/test_core_recorder_time_map_context.py
git commit -m "recorder: wire the TimeMap provider -- the T6 anchor and its credibility for the T6 channel, the pair for every other

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 6: Publish the station's chain from `[timing.provenance]`

**Files:**
- Create: `src/hf_timestd/core/timing_chain_publisher.py`
- Modify: `src/hf_timestd/core/multi_broadcast_fusion.py` (beside the `build_authority_runner_from_config` call at ~L4509), `src/hf_timestd/cli.py` (validate), `config/timestd-config.toml.template`
- Test: `tests/test_timing_chain_publisher.py` (create), `tests/test_cli_validate_host_clock.py` (append)

**Interfaces:**
- Produces:
  - `default_payload_chain() -> Chain` (spec §3.2 payload-anchored example, `filter_group_delay` excluded by convention, `not_declared` where the spec says so); `default_sysclock_chain() -> Chain` (spec §3.2 sysclock@1).
  - `chains_from_config(cfg: dict) -> list[Chain]`: reads `[timing.provenance]` with optional `measurand_plane`, `calibration_plane`, `traceability_qualification`, and `[[timing.provenance.budget]]` term overrides (same keys as `BudgetTerm.to_dict()`); a term whose `term` matches a default replaces it, otherwise appends. Always returns both chains.
  - `publish_chains(chains: list[Chain], path: Path = Path("/run/hf-timestd/timing_chain.json")) -> None` — atomic write of `{"schema": "v2", "written_utc": ..., "chains": [chain.to_record(), ...]}`; never raises.
  - `provenance_issues(cfg) -> list[dict]` in `cli.py`: warn on a term that `BudgetTerm.from_dict` rejects, quoting the error.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_timing_chain_publisher.py
"""The station declares its plane and budget; the fusion service publishes
them where the packager can read them (TIMING_PROVENANCE_MODEL §3.2, §6)."""
import json

import pytest

from hf_timestd.core.timing_chain_publisher import (
    chains_from_config, default_payload_chain, default_sysclock_chain, publish_chains,
)


def test_default_payload_chain_matches_the_spec_shape():
    c = default_payload_chain()
    assert c.id == "payload-anchored@1"
    terms = {t.term: t for t in c.budget}
    assert terms["ts1_modulator_delay"].u_ns == 200
    assert terms["antenna_to_injector"].disposition == "not_declared"
    assert terms["filter_group_delay"].disposition == "excluded_by_convention"
    assert c.u_combined_ns == 5352


def test_default_sysclock_chain_has_no_single_combined_figure():
    c = default_sysclock_chain()
    assert c.id == "sysclock@1" and c.u_combined_ns is None
    assert {t.term for t in c.budget} >= {"pair_non_atomicity", "host_clock_discipline"}


def test_config_override_replaces_a_term_and_keeps_the_rest():
    cfg = {"timing": {"provenance": {
        "traceability_qualification": "feed measured 2026-09-10; front end not characterised",
        "budget": [{"term": "antenna_to_injector", "type": "B", "correction_ns": 41_000, "u_ns": 3_000,
                    "method": "12.3 m LMR-400 at 0.85 VF + preamp datasheet, 2026-09-10"}],
    }}}
    payload, sysclock = chains_from_config(cfg)
    terms = {t.term: t for t in payload.budget}
    assert terms["antenna_to_injector"].correction_ns == 41_000 and terms["antenna_to_injector"].disposition is None
    assert terms["ts1_modulator_delay"].u_ns == 200
    assert payload.traceability["qualification"].startswith("feed measured")
    assert sysclock.id == "sysclock@1"


def test_publish_writes_both_chains_atomically(tmp_path):
    path = tmp_path / "timing_chain.json"
    publish_chains(chains_from_config({}), path=path)
    doc = json.loads(path.read_text())
    assert doc["schema"] == "v2" and [c["id"] for c in doc["chains"]] == ["payload-anchored@1", "sysclock@1"]
    assert not list(tmp_path.glob("*.tmp"))


def test_publish_never_raises(tmp_path):
    publish_chains(chains_from_config({}), path=tmp_path / "no" / "such" / "dir" / "x.json")
```

```python
# append to tests/test_cli_validate_host_clock.py
from hf_timestd.cli import provenance_issues


def test_provenance_term_that_breaks_the_rule_is_warned():
    cfg = {'timing': {'provenance': {'budget': [
        {'term': 'edge_estimation', 'type': 'A', 'u_ns': 150, 'method': 'no measured_on'}]}}}
    issues = provenance_issues(cfg)
    assert len(issues) == 1 and 'measured_on' in issues[0]['message']


def test_clean_provenance_is_clean():
    assert provenance_issues({}) == []
    assert provenance_issues({'timing': {'provenance': {'budget': [
        {'term': 'gnss_antenna_feed', 'type': 'B', 'correction_ns': 62, 'u_ns': 5,
         'method': '15 m at 0.82 VF'}]}}}) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests/test_timing_chain_publisher.py tests/test_cli_validate_host_clock.py -p no:cacheprovider --override-ini="addopts=" -q`
Expected: `ModuleNotFoundError` / `ImportError: cannot import name 'provenance_issues'`

- [ ] **Step 3: Implement**

```python
# src/hf_timestd/core/timing_chain_publisher.py
"""The station's chain record: its plane and its budget.

TIMING_PROVENANCE_MODEL §3.2 gives the payload-anchored budget as it stands
and, since 2026-09-04, the sysclock chain.  Both ship as defaults so every
station publishes a chain; `[timing.provenance]` lets a station replace the
terms it has measured — the antenna-to-injector run, the GNSS feed — and
qualify its traceability claim in its own words.  MEASUREMENT_MODEL §9
invariant 4: a term may not leave the budget because it grew small.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from hamsci_dsp.timing_map import PAYLOAD_CHAIN_ID, SCHEMA, SYSCLOCK_CHAIN_ID, BudgetTerm, Chain

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path("/run/hf-timestd/timing_chain.json")


def default_payload_chain() -> Chain:
    return Chain(
        id=PAYLOAD_CHAIN_ID,
        measurand="UTC instant at which sample n was taken, at the antenna terminals",
        measurand_plane="antenna_terminals",
        calibration_plane="ts1_injection_point",
        traceability={"claim": "UTC(USNO) via GPS", "qualified": True,
                      "qualification": "antenna-to-injector path not declared; receiver front end not characterised"},
        budget=(
            BudgetTerm("ts1_modulator_delay", type="B", correction_ns=0, u_ns=200,
                       method="designer statement, P. Elliott WB6CXC, 2026-08-30; standard injector mode"),
            BudgetTerm("antenna_to_injector", type="B", disposition="not_declared",
                       spans=("antenna_terminals", "ts1_injection_point"),
                       method="feed, preamp and filter ahead of the injection point; station-specific"),
            BudgetTerm("injector_to_receiver", disposition="cancels",
                       spans=("ts1_injection_point", "rx888_adc"),
                       method="identical path for signal and injected reference; cancels by construction"),
            BudgetTerm("gnss_antenna_feed", type="B", disposition="not_declared",
                       method="cable length x velocity factor; a sign-known bias, not an uncertainty"),
            BudgetTerm("anchor_origin_dispersion", type="A", correction_ns=0, u_ns=1900,
                       measured_on={"build": "pre-folding", "date": "2026-08-24"}, disposition="historical",
                       method="63 anchors over 4.5 h ACROSS RE-LOCKS; the folded build of 2026-08-29 removed the re-locks"),
            BudgetTerm("edge_estimation", type="B", correction_ns=0, u_ns=5000,
                       method="conservative bound; becomes Type A computed from cn0_db_hz once the fine-stage sweep has run"),
            BudgetTerm("filter_group_delay", type="B", disposition="excluded_by_convention",
                       method="labeling_convention = content: the channel filter's group delay is pipeline latency outside the measurand"),
        ),
        k=2,
    )


def default_sysclock_chain() -> Chain:
    return Chain(
        id=SYSCLOCK_CHAIN_ID,
        measurand="UTC instant at which sample n was taken, at radiod's advertised epoch",
        measurand_plane="radiod_rtp_timesnap",
        calibration_plane="host_system_clock",
        traceability={"claim": "UTC via the host clock's chrony reference", "qualified": True,
                      "qualification": "the registration descends from the host system clock; its reference and discipline are stated per interval in engineering.host_clock"},
        budget=(
            BudgetTerm("pair_non_atomicity", type="A", correction_ns=0, u_ns=8_030_000,
                       measured_on={"build": "pre-anchor-inversion", "date": "2026-08-16"}, disposition="historical",
                       method="p99 of GPS_TIME minus RTP_TIMESNAP-implied epoch, 900 s, T6 channel; the running-minimum estimator of MEASUREMENT_MODEL §6.2 replaces this"),
            BudgetTerm("host_clock_discipline", type="A", disposition="per_interval",
                       method="the largest witnessed disagreement in the authority manager's host_clock block; enters u_epoch_ns directly, per interval, never as a constant"),
            BudgetTerm("channel_filter_group_delay", disposition="excluded_by_convention",
                       method="under labeling_convention = content the label denotes the antenna instant; the processing interval is pipeline latency, outside the measurand"),
        ),
        k=1,
    )


def chains_from_config(cfg: dict) -> List[Chain]:
    prov = ((cfg.get("timing", {}) or {}).get("provenance", {}) or {})
    payload = default_payload_chain()
    overrides = [BudgetTerm.from_dict(t) for t in (prov.get("budget") or [])]
    by_name = {t.term: t for t in payload.budget}
    for t in overrides:
        by_name[t.term] = t
    traceability = dict(payload.traceability)
    if prov.get("traceability_qualification"):
        traceability["qualification"] = str(prov["traceability_qualification"])
    payload = Chain(
        id=payload.id, measurand=payload.measurand,
        measurand_plane=str(prov.get("measurand_plane", payload.measurand_plane)),
        calibration_plane=str(prov.get("calibration_plane", payload.calibration_plane)),
        traceability=traceability, budget=tuple(by_name.values()), k=payload.k,
    )
    return [payload, default_sysclock_chain()]


def publish_chains(chains: List[Chain], path: Path = DEFAULT_PATH) -> None:
    """Atomic, world-readable, never raises."""
    doc = {"schema": SCHEMA,
           "written_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
           "chains": [c.to_record() for c in chains]}
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=str(path.parent), prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False, encoding="utf-8") as tmp:
            json.dump(doc, tmp, separators=(",", ":"))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("timing chain: failed to write %s: %s", path, exc)
```

In `multi_broadcast_fusion.py`, immediately after `authority_runner = build_authority_runner_from_config(config=_auth_config)`:

```python
        try:
            from hf_timestd.core.timing_chain_publisher import chains_from_config, publish_chains
            publish_chains(chains_from_config(_auth_config))
        except Exception as exc:  # noqa: BLE001
            logger.warning("timing chain not published: %s", exc)
```

(`_auth_config` is the full config dict there; confirm with the surrounding lines.)

In `cli.py`, next to `host_clock_issues`:

```python
def provenance_issues(cfg):
    """Warn-level issues for ``[timing.provenance]`` budget overrides: every
    term must obey BudgetTerm's rules (a value or a disposition; Type A
    carries measured_on).  The station's declaration is part of the record."""
    issues = []
    prov = ((cfg.get('timing', {}) or {}).get('provenance', None))
    if not isinstance(prov, dict):
        return issues
    from hamsci_dsp.timing_map import BudgetTerm
    for raw in (prov.get('budget') or []):
        try:
            BudgetTerm.from_dict(raw)
        except (ValueError, KeyError, TypeError) as exc:
            issues.append({'severity': 'warn', 'instance': 'default',
                           'message': f'[timing.provenance] budget term {raw.get("term", "?")!r}: {exc}'})
    return issues
```

and add `issues.extend(provenance_issues(cfg))` after `issues.extend(host_clock_issues(cfg))`.

Template, after the `[timing.authority_manager.host_clock]` block:

```toml
# === Timing provenance — the station declares its plane and budget ===
# docs/design/TIMING_PROVENANCE_MODEL.md §3.2.  Both chains ship as defaults;
# override only the terms THIS station has measured.  A term states a value
# or a disposition; a Type A term carries measured_on.  `hf-timestd validate`
# checks each override.  The fusion service publishes the result at
# /run/hf-timestd/timing_chain.json for the GRAPE packager.
[timing.provenance]
# measurand_plane = "antenna_terminals"
# calibration_plane = "ts1_injection_point"
# traceability_qualification = "antenna-to-injector path not declared; receiver front end not characterised"
# [[timing.provenance.budget]]
# term = "antenna_to_injector"
# type = "B"
# correction_ns = 41000
# u_ns = 3000
# method = "12.3 m LMR-400 at 0.85 VF + preamp datasheet, 2026-09-10"
```

- [ ] **Step 4: Run the suite and parse the template**

Run: `cd /home/mjh/hamsci/repos/hf-timestd && .venv/bin/python -m pytest tests -p no:cacheprovider --override-ini="addopts=" -q && .venv/bin/python -c "import tomllib; tomllib.load(open('config/timestd-config.toml.template','rb')); print('template ok')"`
Expected: all pass; `template ok`

- [ ] **Step 5: Commit**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add src/hf_timestd/core/timing_chain_publisher.py src/hf_timestd/core/multi_broadcast_fusion.py src/hf_timestd/cli.py config/timestd-config.toml.template tests/test_timing_chain_publisher.py tests/test_cli_validate_host_clock.py
git commit -m "timing_chain_publisher: the station's plane and budget, defaults plus measured overrides, published at /run/hf-timestd/timing_chain.json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
```

---

### Task 7: Documentation and the deprecation note

**Files:**
- Modify: `docs/METROLOGY.md` (the "Published Authority State (schema v1)" subsection: one paragraph on `host_clock_*` history columns and `/run/hf-timestd/timing_chain.json`), `docs/TIMING-PIPELINE-WIRING.md` (the section describing the chunk sidecar's `timing` block: the v2 state record, the legacy mirror, and its retirement one release after hamsci-physics reads `u_epoch_ns`).

- [ ] **Step 1: Write the two passages.** In METROLOGY, after the `host_clock` JSON example added 2026-09-04: "The authority history store mirrors the verdict as `host_clock_verdict`, `host_clock_since_utc`, `host_clock_t2_ms`, `host_clock_lb1421_s`. The fusion service also publishes the station's chain records at `/run/hf-timestd/timing_chain.json` from `[timing.provenance]` (TIMING_PROVENANCE_MODEL §3.2)." In TIMING-PIPELINE-WIRING, under the chunk sidecar description: the `timing` block is now the schema v2 `state` record of TIMING_PROVENANCE_MODEL §3.1; the Offset Judge keys stay at top level for one release and also appear under `engineering`; consumers read `u_epoch_ns` and `stability_ns`, never `judge_tier`; retire the top-level mirror one release after hamsci-physics reads `u_epoch_ns`.

- [ ] **Step 2: Run the docs-referencing tests, if any** (`grep -rl "TIMING-PIPELINE-WIRING\|METROLOGY.md" tests | head`), then the whole suite once more.

- [ ] **Step 3: Commit and push**

```bash
cd /home/mjh/hamsci/repos/hf-timestd
git add docs/METROLOGY.md docs/TIMING-PIPELINE-WIRING.md
git commit -m "docs: the v2 timing block, the legacy mirror and its retirement, the chain file, the history columns

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014fxKGYhGpcPpYFsPbDj4KH"
git push origin main
```

---

## Self-review

**Spec coverage.** §3.0 "extend the existing per-chunk block, no new per-chunk file" → Task 4 (`metadata['timing']` becomes the state record); §3.1 fields → Tasks 2, 4, 5 (`counter_space`, `counter_epoch_id`, `n0`, `t0`, `f_s`, `origin`, `u`, `k`, `p`, `measured_at`, `a_level` + provenance, `lock_credible`, `host_clock`, `engineering` with the pair and tier); §3.1.1 sysclock registration → Task 2 (no anchor → `sysclock_map` bounded by the host clock); §3.2 chain publication → Task 6; §3.4 tier and pair under engineering → Task 4; §6 deliverable 2 → Tasks 2–5; MEASUREMENT_MODEL §3 counter re-basing → Task 3; §6.4 lock credibility → Tasks 2 and 5; HOST_CLOCK_INTEGRITY history columns → Task 1. Not in this plan: reading the record and the chain sidecar (Plan C).

**Placeholders.** Two locating steps use `grep -n` to find an exact line in a 1,639-line file rather than quoting a line number that will drift; each names the anchor text and gives the code to insert. No TBDs.

**Type consistency.** `TimeMapInputs` fields match between Task 2 (definition), Task 4 (construction in `_chunk_timing_block`) and Task 5 (`dataclasses.replace` with `anchor_rtp`, `anchor_utc_ns`, `anchor_sigma_ns`, `lock_credible`); `set_time_map_provider(provider, counter_space)` matches its call in `wire_time_map`; snapshot column names in Task 1 match hamsci-dsp Plan A Task 5.
