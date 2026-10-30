"""Task 15: the authority manager writes one T3 shadow row per UTC minute.

Only the manager holds T2 and T4 side by side, so only it can answer pass
criterion 6 (does T3 beat T2, with T4 as truth?).  The row carries T3's
registration state, the judge's ``t3_shadow`` block and T3 shadow residual,
the T6/T4/T2 offsets from ONE tick, the host-clock verdict, and a running
count of station-level T3 transitions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hf_timestd.core import authority_manager as am
from hf_timestd.core.authority_manager import AuthorityManager, ProbeResult
from hf_timestd.core.t3_shadow_record import T3ShadowRecord

M0 = datetime(2026, 10, 30, 12, 0, 5, tzinfo=timezone.utc)
M0_EPOCH = int(datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp())


@dataclass
class FakeProbe:
    t_level: str
    result: ProbeResult = field(default=None)  # type: ignore[assignment]

    def poll(self) -> ProbeResult:
        return self.result


class _Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = datetime.fromtimestamp(self.t.timestamp() + seconds, tz=timezone.utc)


class _Summary:
    def __init__(self, summary):
        self.summary = summary

    def read_summary(self):
        return self.summary


SUMMARY = {
    "state": "ACQUIRED", "sigma_ms": 0.8, "contributing": ["WWV_10_MHz"],
    "waiting": ["WWV_5_MHz"], "n_minutes": 12, "verified": True,
    "whole_second_unresolved": False, "rtp_domain": "WWV_10_MHz",
    "counter_epoch_id": "ep-1",
}

JUDGE = {
    "t3_shadow": {"t3_minus_t6_ms": 0.31, "t6_chain_delay_ms": 0.041,
                  "t6_anchor_input_index": 123, "t6_anchor_utc_ns": 456,
                  "t3_rtp_domain": "WWV_10_MHz", "t3_state": "ACQUIRED",
                  "t3_sigma_ms": 0.8},
    "shadow_residuals": {"T3": {"shadow_residual_ns": 310000.0,
                                "sigma_ns": 800000.0, "vs_tier": "T6"}},
}


@pytest.fixture
def reg_state(monkeypatch):
    """The published registration block; tests set ``box['state']``."""
    box = {"state": "ACQUIRED"}

    def block():
        return {"source": "label", "state": box["state"], "sigma_ms": 0.8,
                "counter_epoch_id": "ep-1", "raw_pair_residual_ms": None,
                "contributing": ["WWV_10_MHz"], "stations": [], "age_s": 3.0}
    monkeypatch.setattr(am, "registration_block", block)
    return box


def _probes(t6=0.0005, t4=0.02, t2=6.4):
    return [
        FakeProbe("T6", ProbeResult("T6", True, offset_ms=t6, sigma_ms=0.001, frame="rtp")),
        FakeProbe("T4", ProbeResult("T4", True, offset_ms=t4, sigma_ms=2.0)),
        FakeProbe("T2", ProbeResult("T2", True, offset_ms=t2, sigma_ms=20.0)),
    ]


def _manager(tmp_path, probes=None, clock=None, judge=JUDGE, record=True,
             summary=SUMMARY, **kw):
    judge_path = tmp_path / "offset_judge.json"
    if judge is not None:
        judge_path.write_text(json.dumps(judge))
    rec = T3ShadowRecord("minutes", tmp_path / "t3_shadow") if record else None
    return AuthorityManager(
        probes=probes if probes is not None else _probes(),
        output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1",
        upgrade_hysteresis=1,
        now_fn=clock or _Clock(M0),
        t3_shadow_record=rec,
        judge_status_path=judge_path,
        registration_store=_Summary(summary),
        **kw,
    )


def _rows(tmp_path, kind="minute"):
    out = []
    for p in sorted((tmp_path / "t3_shadow").glob("minutes-*.jsonl")):
        for line in p.read_text().splitlines():
            row = json.loads(line)
            if kind is None or row.get("kind") == kind:
                out.append(row)
    return out


def test_one_row_per_utc_minute(tmp_path, reg_state):
    clock = _Clock(M0)
    m = _manager(tmp_path, clock=clock)
    for _ in range(3):
        m.tick()
        clock.advance(15)
    clock.advance(30)   # into minute M+1
    m.tick()
    rows = _rows(tmp_path)
    assert [r["utc_minute"] for r in rows] == [M0_EPOCH, M0_EPOCH + 60]
    assert (tmp_path / "t3_shadow" / "minutes-20261030.jsonl").exists()


def test_row_holds_t2_and_t4_side_by_side(tmp_path, reg_state):
    m = _manager(tmp_path, probes=_probes(t6=0.0005, t4=0.02, t2=6.4))
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["t4_offset_ms"] == 0.02
    assert row["t2_offset_ms"] == 6.4
    assert row["t6_offset_ms"] == 0.0005
    # T4 and T2 come from ChronyTrackingProbe (sysclock frame), T6 from the
    # anchor-vs-PPS residual (rtp frame): the row names each one's frame.
    assert row["offset_frames"] == {"t6": "rtp", "t4": "sysclock", "t2": "sysclock"}


def test_row_carries_registration_judge_and_verdict(tmp_path, reg_state):
    m = _manager(tmp_path)
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["kind"] == "minute"
    assert row["t3"] == {
        "state": "ACQUIRED", "sigma_ms": 0.8, "contributing": ["WWV_10_MHz"],
        "waiting": ["WWV_5_MHz"], "n_minutes": 12, "verified": True,
        "whole_second_unresolved": False, "rtp_domain": "WWV_10_MHz",
        "counter_epoch_id": "ep-1",
    }
    assert row["t3_shadow"] == JUDGE["t3_shadow"]
    assert row["t3_vs_adopted"] == JUDGE["shadow_residuals"]["T3"]
    assert row["host_clock_verdict"] is not None
    assert row["t3_transitions_total"] == 0


def test_station_state_change_logs_once_and_counts(tmp_path, reg_state, caplog):
    clock = _Clock(M0)
    m = _manager(tmp_path, clock=clock)
    reg_state["state"] = "CANDIDATE"
    m.tick()
    clock.advance(60)
    reg_state["state"] = "WITNESS"
    with caplog.at_level(logging.INFO):
        m.tick()
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "T3 STATE station" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].startswith("T3 STATE station: CANDIDATE -> WITNESS")
    rows = _rows(tmp_path)
    assert rows[-1]["t3_transitions_total"] == 1
    transitions = _rows(tmp_path, kind="transition")
    assert [(t["from"], t["to"]) for t in transitions] == [(None, "CANDIDATE"),
                                                           ("CANDIDATE", "WITNESS")]
    assert all(t["scope"] == "station" for t in transitions)


def test_a_change_between_minute_rows_still_counts(tmp_path, reg_state):
    """The counter watches every tick, not only the minute's first."""
    clock = _Clock(M0)
    m = _manager(tmp_path, clock=clock)
    reg_state["state"] = "ACQUIRED"
    m.tick()                       # minute row M, state ACQUIRED
    clock.advance(20)
    reg_state["state"] = "CONFLICT"
    m.tick()                       # same minute: no row, but a transition
    clock.advance(20)
    reg_state["state"] = "ACQUIRED"
    m.tick()                       # same minute: back again
    clock.advance(30)
    m.tick()                       # minute row M+1
    rows = _rows(tmp_path)
    assert [r["t3_transitions_total"] for r in rows] == [0, 2]


def test_missing_judge_file_still_writes_the_row(tmp_path, reg_state):
    m = _manager(tmp_path, judge=None)
    state = m.tick()
    assert state.t_level_active == "T6"
    (row,) = _rows(tmp_path)
    assert row["t3_shadow"] is None
    assert row["t3_vs_adopted"] is None
    assert row["t4_offset_ms"] == 0.02


def test_bad_judge_json_still_writes_the_row(tmp_path, reg_state):
    m = _manager(tmp_path, judge=None)
    (tmp_path / "offset_judge.json").write_text("{not json")
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["t3_shadow"] is None and row["t3_vs_adopted"] is None


def test_judge_with_null_shadow_records_null(tmp_path, reg_state):
    m = _manager(tmp_path, judge={"t3_shadow": None, "shadow_residuals": {}})
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["t3_shadow"] is None and row["t3_vs_adopted"] is None


def test_unwritable_directory_leaves_the_tick_unaffected(tmp_path, reg_state):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    rec = T3ShadowRecord("minutes", blocker / "t3_shadow")
    m = AuthorityManager(
        probes=_probes(), output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1", upgrade_hysteresis=1,
        now_fn=_Clock(M0), t3_shadow_record=rec,
        judge_status_path=tmp_path / "missing.json",
        registration_store=_Summary(SUMMARY),
    )
    state = m.tick()
    assert state.t_level_active == "T6"
    assert json.loads((tmp_path / "authority.json").read_text())["t_level_active"] == "T6"


def test_a_raising_record_never_reaches_the_tick(tmp_path, reg_state):
    class Boom:
        def append(self, row):
            raise RuntimeError("disk on fire")
    m = AuthorityManager(
        probes=_probes(), output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1", upgrade_hysteresis=1,
        now_fn=_Clock(M0), t3_shadow_record=Boom(),
        judge_status_path=tmp_path / "missing.json",
        registration_store=_Summary(SUMMARY),
    )
    assert m.tick().t_level_active == "T6"


def test_missing_summary_and_probes_record_nulls(tmp_path, reg_state):
    m = _manager(tmp_path, probes=[], summary=None)
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["t6_offset_ms"] is None
    assert row["t4_offset_ms"] is None
    assert row["t2_offset_ms"] is None
    assert row["t3"]["state"] == "ACQUIRED"
    assert row["t3"]["n_minutes"] is None and row["t3"]["waiting"] is None


def test_non_finite_offset_records_null_not_a_dropped_row(tmp_path, reg_state):
    m = _manager(tmp_path, probes=_probes(t2=float("nan")))
    m.tick()
    (row,) = _rows(tmp_path)
    assert row["t2_offset_ms"] is None and row["t4_offset_ms"] == 0.02


def test_no_record_writes_nothing(tmp_path, reg_state):
    m = _manager(tmp_path, record=False)
    m.tick()
    assert not (tmp_path / "t3_shadow").exists()
    assert m.t3_shadow_record is None


def test_runner_builds_the_record_from_config(tmp_path):
    from hf_timestd.core.authority_runner import build_authority_runner_from_config
    kw = dict(fusion_status_path=tmp_path / "fusion_status.json",
              authority_output_path=tmp_path / "authority.json")
    off = build_authority_runner_from_config(
        config={"timing": {"t3_shadow": {"enabled": False}}}, **kw)
    assert off.manager.t3_shadow_record is None

    on = build_authority_runner_from_config(
        config={"timing": {"t3_shadow": {"directory": str(tmp_path / "sh"),
                                          "retention_days": 7}}}, **kw)
    rec = on.manager.t3_shadow_record
    assert rec is not None
    assert rec.stream == "minutes"
    assert rec.directory == tmp_path / "sh"
    assert rec.retention_days == 7

    default = build_authority_runner_from_config(config={}, **kw)
    assert default.manager.t3_shadow_record.directory == Path("/var/lib/timestd/t3_shadow")
    assert default.manager.t3_shadow_record.retention_days == 14
    assert default.manager.judge_status_path == Path("/run/hf-timestd/offset_judge.json")
    # Building writes nothing.
    assert not (tmp_path / "sh").exists()


def test_runner_reads_the_judge_publish_path(tmp_path):
    from hf_timestd.core.authority_runner import build_authority_runner_from_config
    r = build_authority_runner_from_config(
        config={"timing": {"offset_judge": {"publish_path": str(tmp_path / "j.json")}}},
        fusion_status_path=tmp_path / "fusion_status.json",
        authority_output_path=tmp_path / "authority.json")
    assert r.manager.judge_status_path == tmp_path / "j.json"


def test_template_carries_the_t3_shadow_section():
    import tomllib
    template = Path(__file__).resolve().parents[2] / "config" / "timestd-config.toml.template"
    text = template.read_text()
    cfg = tomllib.loads(text)
    assert cfg["timing"]["t3_shadow"] == {
        "enabled": True, "directory": "/var/lib/timestd/t3_shadow", "retention_days": 14}


def test_a_bad_t3_shadow_table_costs_only_the_rows(tmp_path):
    from hf_timestd.core.authority_runner import build_authority_runner_from_config
    r = build_authority_runner_from_config(
        config={"timing": {"t3_shadow": {"retention_days": "two weeks"}}},
        fusion_status_path=tmp_path / "fusion_status.json",
        authority_output_path=tmp_path / "authority.json")
    assert r.manager.t3_shadow_record is None
