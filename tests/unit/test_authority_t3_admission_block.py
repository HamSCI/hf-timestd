"""Task A6: the authority manager decides T3 admission each minute and
publishes the decision.

Each minute the manager builds the shadow row, feeds it to the admission
policy (A5), and records the decision in the row.  ``authority.json`` carries
the standing decision as ``t3_admission`` on every tick.  ``tick()`` writes
``authority.json`` before it builds the minute row, so a decision made on
tick N reaches ``authority.json`` on tick N+1 (30 s in production).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from hf_timestd.core.authority_manager import (
    AuthorityManager, ProbeResult, registration_block)
from hf_timestd.core.clock_stability import STABILITY_HISTORY_MIN
from hf_timestd.core.t3_admission import AdmissionMode
from hf_timestd.core.t3_admission_policy import T3AdmissionPolicy
from hf_timestd.core.t3_shadow_record import T3ShadowRecord

M0 = datetime(2026, 11, 13, 12, 0, 5, tzinfo=timezone.utc)
WALL0 = 1_800_000_000.0
_ACQUIRED = {"state": "ACQUIRED", "utc_ref": WALL0, "sigma_ms": 1.0,
             "counter_epoch_id": "ep-1", "contributing": ["SHARED_10000"]}
# T3 0.3 ms from T6; T2 0.6 ms from T4 (t4 - t2 = 0.0 - (-0.6)).
JUDGE = {"t3_shadow": {"t3_minus_t6_ms": 0.3}}
ADMITTED = "admitted by reference (T6)"
OFF = 'off ([timing.t3] admission = "off")'


class _Clock:
    def __init__(self, start=M0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = datetime.fromtimestamp(self.t.timestamp() + seconds,
                                        tz=timezone.utc)


class _Probe:
    def __init__(self, result):
        self.t_level = result.t_level
        self.result = result

    def poll(self):
        return self.result


class _Summary:
    def __init__(self, summary):
        self.summary = summary
        self.stale_s = 300.0

    def read_summary(self):
        return self.summary

    def summary_age_s(self, s):
        return 1.0


def _authority(tmp_path, *, t3_available=True, record=True, clock=None,
               judge=JUDGE, **kw):
    probes = [
        _Probe(ProbeResult("T3", t3_available, offset_ms=0.1, sigma_ms=1.0,
                           frame="rtp",
                           reason=None if t3_available else "probe says no")),
        _Probe(ProbeResult("T4", True, offset_ms=0.0, sigma_ms=2.0)),
        _Probe(ProbeResult("T2", True, offset_ms=-0.6, sigma_ms=20.0)),
    ]
    judge_path = tmp_path / "offset_judge.json"
    if judge is not None:
        judge_path.write_text(json.dumps(judge))
    if record is True:
        record = T3ShadowRecord("minutes", tmp_path / "t3_shadow")
    elif record is False:
        record = None
    return AuthorityManager(
        probes=probes, output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1", upgrade_hysteresis=1,
        now_fn=clock or _Clock(), t3_shadow_record=record,
        judge_status_path=judge_path,
        registration_store=_Summary(_ACQUIRED), **kw)


def _published(tmp_path):
    return json.loads((tmp_path / "authority.json").read_text())


def _rows(tmp_path):
    out = []
    for p in sorted((tmp_path / "t3_shadow").glob("minutes-*.jsonl")):
        for line in p.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "minute":
                out.append(row)
    return out


def _run(m, clock, ticks):
    for _ in range(ticks):
        m.tick()
        clock.advance(60)


# The policy needs FILL_MIN_PAIRS (20) pairs and then holds the change for
# HOLD_MIN_S (300 s, five rows): row 25 admits.  Row 25 is built on tick 25,
# after that tick wrote authority.json, so authority.json shows it on tick 26.
ROWS_TO_ADMIT = 25
TICKS_TO_PUBLISH = ROWS_TO_ADMIT + 1


# ── the block ────────────────────────────────────────────────────────


def test_authority_json_carries_the_admission_block_every_tick(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock)
    for _ in range(3):
        m.tick()
        block = _published(tmp_path)["t3_admission"]
        assert block["mode"] == "auto" and block["admitted"] is False
        assert block["reason"].startswith("withheld:")
        clock.advance(30)


def test_the_constructor_switch_maps_to_modes(tmp_path):
    assert _authority(tmp_path).t3_decision.mode is AdmissionMode.AUTO
    on = _authority(tmp_path, t3_admitted=True)
    assert on.t3_decision.mode is AdmissionMode.ON
    assert on.t3_decision.admitted is True and on.t3_admitted is True
    off = _authority(tmp_path, t3_admitted=False)
    assert off.t3_decision.mode is AdmissionMode.OFF
    assert off.t3_admitted is False
    policy = T3AdmissionPolicy(AdmissionMode.OFF)
    assert _authority(tmp_path, t3_policy=policy).t3_decision is policy.decision


def test_an_auto_station_admits_t3_from_its_rows(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock)
    _run(m, clock, ROWS_TO_ADMIT)
    # Decided on the last tick, after that tick published.
    assert m.t3_decision.admitted is True
    assert _published(tmp_path)["t3_admission"]["admitted"] is False
    state = m.tick()
    published = _published(tmp_path)
    assert published["t3_admission"]["admitted"] is True
    assert published["t3_admission"]["reason"] == ADMITTED
    assert published["registration"]["source"] == "hf_acquired"
    assert "admission" not in published["registration"]
    assert "t3_unavailable_reason" not in published
    # T3 holds a rung again (T4 still outranks it here).
    assert "T3" in state.t_level_available


def test_the_decision_reaches_the_minute_row(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock)
    _run(m, clock, ROWS_TO_ADMIT)
    rows = _rows(tmp_path)
    assert len(rows) == ROWS_TO_ADMIT
    assert rows[-1]["admission"]["reason"] == ADMITTED
    assert rows[-1]["admission"]["admitted"] is True
    assert rows[-2]["admission"]["admitted"] is False
    assert rows[0]["admission"]["reason"] == (
        "withheld: window filling (1 of 20 minutes)")


def test_admission_runs_without_a_shadow_record(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock, record=False)
    _run(m, clock, TICKS_TO_PUBLISH)
    assert _published(tmp_path)["t3_admission"]["admitted"] is True
    assert not (tmp_path / "t3_shadow").exists()


def test_two_ticks_in_one_minute_feed_one_row(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock, record=False)
    for _ in range(4):
        m.tick()
        clock.advance(20)          # ticks at :05 :25 :45 then the next minute
    assert m.t3_decision.n_pairs == 2


def test_unadmitted_t3_names_the_decision_reason(tmp_path):
    m = _authority(tmp_path)
    m.tick()
    published = _published(tmp_path)
    assert published["t_level_active"] == "T4"
    assert published["t3_unavailable_reason"] == (
        published["t3_admission"]["reason"])
    assert published["registration"]["source"] == "label"
    assert published["registration"]["admission"] == (
        published["t3_admission"]["reason"])


def test_mode_off_never_admits(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock, t3_admitted=False)
    _run(m, clock, TICKS_TO_PUBLISH + 2)
    published = _published(tmp_path)
    assert published["t3_admission"]["admitted"] is False
    assert published["t3_unavailable_reason"] == OFF
    assert published["registration"]["admission"] == OFF
    assert _rows(tmp_path)[-1]["admission"]["reason"] == OFF


def test_a_failing_policy_keeps_the_standing_decision_and_never_raises(
        tmp_path, caplog):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock)
    before = m.t3_decision

    def boom(ev):
        raise RuntimeError("policy bug")
    m.t3_policy.observe = boom
    with caplog.at_level(logging.DEBUG):
        _run(m, clock, 3)
    assert m.t3_decision is before
    assert _published(tmp_path)["t3_admission"] == before.to_block()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "policy bug" in errors[0].getMessage()
    # The rows still land, carrying the standing decision: the record
    # keeps running.
    rows = _rows(tmp_path)
    assert len(rows) == 3
    assert all(r["admission"] == before.to_block() for r in rows)


def test_a_failing_row_feeds_no_minute(tmp_path):
    clock = _Clock()
    m = _authority(tmp_path, clock=clock, record=False)

    def boom(*a, **k):
        raise RuntimeError("row builder bug")
    m._t3_shadow_minute_row = boom
    _run(m, clock, 3)
    assert m.t3_decision.n_pairs == 0
    assert m.t3_decision.admitted is False


def test_filling_logs_the_unavailable_line_once_not_each_minute(
        tmp_path, caplog):
    """The decision's reason counts minutes ("window filling (3 of 20
    minutes)"); a changed count is not a changed reason for the WARNING."""
    clock = _Clock()
    m = _authority(tmp_path, clock=clock)
    with caplog.at_level(logging.WARNING):
        _run(m, clock, 15)
    lines = [r for r in caplog.records if "T3 UNAVAILABLE" in r.getMessage()]
    assert len(lines) == 1


# ── registration_block ───────────────────────────────────────────────


def test_registration_block_writes_the_given_reason():
    b = registration_block(_Summary(_ACQUIRED), admitted=False,
                           admission_reason="withheld: no T2 to compare against")
    assert b["source"] == "label"
    assert b["admission"] == "withheld: no T2 to compare against"
    from hf_timestd.core.t3_admission import NOT_ADMITTED_REASON
    assert registration_block(_Summary(_ACQUIRED))["admission"] == (
        NOT_ADMITTED_REASON)
    assert "admission" not in registration_block(
        _Summary(_ACQUIRED), admitted=True, admission_reason="ignored")


# ── the warm start ───────────────────────────────────────────────────


def _seed_row(minute, *, new_shape=True):
    row = {"kind": "minute", "utc_minute": minute,
           "t3": {"sigma_ms": 1.0, "whole_second_unresolved": False},
           "t3_shadow": {"t3_minus_t6_ms": 0.3},
           "t4_offset_ms": 0.0, "t2_offset_ms": -0.6}
    if new_shape:
        row.update({"t2_vs_adopted": None, "t2_sigma_ms": 20.0,
                    "t3_phase_ms": 1.0, "t3_run_id": 7.0})
    return row


def test_seed_skips_old_rows_and_restores_the_window(tmp_path):
    record = T3ShadowRecord("minutes", tmp_path / "t3_shadow")
    m0 = int(M0.timestamp()) // 60 * 60
    for k in range(30):
        record.append(_seed_row(m0 + 60 * k, new_shape=k >= 10))
    with open(next((tmp_path / "t3_shadow").glob("minutes-*.jsonl")), "a") as fh:
        fh.write("{not json\n")
    now = m0 + 60 * 30
    policy = T3AdmissionPolicy(AdmissionMode.AUTO)
    n = policy.seed(record.read_recent(now - STABILITY_HISTORY_MIN * 60))
    assert n == 20
    assert policy.decision.n_pairs == 20


def _runner(tmp_path, admission="auto", **kw):
    from hf_timestd.core.authority_runner import (
        build_authority_runner_from_config)
    cfg = {"timing": {"t3": {"admission": admission},
                      "t3_shadow": {"directory": str(tmp_path / "t3_shadow")},
                      "offset_judge": {
                          "publish_path": str(tmp_path / "j.json")}}}
    return build_authority_runner_from_config(
        config=cfg, fusion_status_path=tmp_path / "fusion_status.json",
        authority_output_path=tmp_path / "authority.json", **kw)


@pytest.mark.parametrize("admission,mode", [
    ("auto", AdmissionMode.AUTO), ("off", AdmissionMode.OFF),
    ("on", AdmissionMode.ON)])
def test_the_runner_builds_the_policy_from_the_setting(tmp_path, admission,
                                                       mode):
    assert _runner(tmp_path, admission).manager.t3_decision.mode is mode


def test_the_runner_seeds_the_policy_from_the_record(tmp_path):
    # The runner's manager stamps minutes from the host clock (its default
    # now_fn), so the rows sit in the half hour before now.
    m0 = int(datetime.now(timezone.utc).timestamp()) // 60 * 60 - 60 * 30
    record = T3ShadowRecord("minutes", tmp_path / "t3_shadow")
    for k in range(25):
        record.append(_seed_row(m0 + 60 * k))
    runner = _runner(tmp_path)
    assert runner.manager.t3_decision.n_pairs == 25


def test_a_seed_failure_warns_once_and_starts_empty(tmp_path, monkeypatch,
                                                    caplog):
    def boom(self, since, until_utc_minute=None):
        raise OSError("disk gone")
    monkeypatch.setattr(T3ShadowRecord, "read_recent", boom)
    with caplog.at_level(logging.WARNING):
        runner = _runner(tmp_path)
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "seed" in r.getMessage()]
    assert len(warnings) == 1 and "disk gone" in warnings[0].getMessage()
    assert runner.manager.t3_decision.n_pairs == 0


# ── fix round 1 (review of A6) ───────────────────────────────────────


def test_the_announcement_repeats_when_only_the_reference_changes(
        tmp_path, caplog):
    """Masking the numbers must leave tier names alone: "T6" and "T4"
    differ."""
    m = _authority(tmp_path)
    t2_better = ("withheld: T2 tracks {ref} better (median 0.40 vs 0.30 ms, "
                 "p95 0.90 vs 0.80 ms)")
    with caplog.at_level(logging.WARNING):
        for ref in ("T6", "T6", "T4"):
            m._note_t3({"T3": ProbeResult(
                "T3", False, reason=t2_better.format(ref=ref))})
    lines = [r for r in caplog.records if "T3 UNAVAILABLE" in r.getMessage()]
    assert len(lines) == 2
    assert "tracks T4 better" in lines[-1].getMessage()


def test_a_row_that_keeps_failing_withdraws_within_one_window(
        tmp_path, caplog):
    """A persistent row bug must not freeze an admission: each failed
    minute feeds the policy empty evidence, so the reference-loss hold
    runs out within one window (failure closes T3 in auto mode)."""
    clock = _Clock()
    m = _authority(tmp_path, clock=clock, record=False)
    _run(m, clock, ROWS_TO_ADMIT)
    assert m.t3_decision.admitted is True
    window_min = m.t3_decision.stability.window_s // 60

    def boom(*a, **k):
        raise RuntimeError("row builder bug")
    m._t3_shadow_minute_row = boom
    with caplog.at_level(logging.DEBUG):
        _run(m, clock, window_min + 1)
    assert m.t3_decision.admitted is False
    assert m.t3_decision.reason.startswith("withheld:")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    # No record exists here: the ERROR names the admission evidence.
    assert "admission" in errors[0].getMessage()
    assert "minutes record is incomplete" not in errors[0].getMessage()


def test_the_runner_seeds_no_row_stamped_after_now(tmp_path):
    """A host clock that stepped back leaves rows stamped in the future.
    The seed must not take them: the policy would then ignore every live
    minute until the clock caught up."""
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    record = T3ShadowRecord("minutes", tmp_path / "t3_shadow")
    for k in range(1, 26):
        record.append(_seed_row(now + 60 * k))
    runner = _runner(tmp_path)
    assert runner.manager.t3_decision.n_pairs == 0
