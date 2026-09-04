"""AuthorityManager publishes a host-clock verdict beside the tier decision.

Replays of 2026-09-04.  AC0G-B4: active T6 (rtp-frame, correct anchor), T2
witness 11.68 s away, the manager wrote ":advisory" and moved on while the
host clock ran 11.6 s slow.  AC0G-ND: active T3, T2 witness-only 4.18 s
away, demoted to no authority, host clock 4.2 s slow.  Both days the T2
number was right and nothing said so.  The tier logic stays exactly as it
was — the anchor was fine — and a separate ``host_clock`` block now carries
the verdict, with a CRITICAL log line on entry and once an hour after.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hf_timestd.core.authority_manager import (
    TRUST_SIGMA_MS,
    AuthorityManager,
    ProbeResult,
)


@dataclass
class FakeProbe:
    t_level: str
    _result: ProbeResult = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._result is None:
            self._result = ProbeResult(self.t_level, available=False, reason="default")

    def set(self, result: ProbeResult) -> None:
        self._result = result

    def poll(self) -> ProbeResult:
        return self._result


def _rtp(t: str, offset_ms: float, sigma_ms: float, detail=None) -> ProbeResult:
    return ProbeResult(t, True, offset_ms=offset_ms, sigma_ms=sigma_ms,
                       detail=dict(detail or {}), frame="rtp")


def _sysclock(t: str, offset_ms: float, *, witness_only: bool = False) -> ProbeResult:
    return ProbeResult(t, available=not witness_only, offset_ms=offset_ms,
                       sigma_ms=TRUST_SIGMA_MS[t], frame="sysclock",
                       witness_only=witness_only)


class _Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = datetime.fromtimestamp(self.t.timestamp() + seconds, tz=timezone.utc)


T0 = datetime(2026, 9, 4, 15, 6, 50, tzinfo=timezone.utc)


def _manager(tmp_path, probes, clock=None, **kw) -> AuthorityManager:
    return AuthorityManager(
        probes=probes,
        output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1",
        upgrade_hysteresis=1,
        now_fn=clock or _Clock(T0),
        **kw,
    )


def _payload(tmp_path) -> dict:
    return json.loads((tmp_path / "authority.json").read_text())


# --- the B4 day --------------------------------------------------------------

def test_b4_replay_publishes_a_fault_and_leaves_the_tier_logic_alone(tmp_path):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t2 = FakeProbe("T2", _sysclock("T2", -11679.507, witness_only=True))
    m = _manager(tmp_path, [t6, t2])
    state = m.tick()

    # The anchor logic is untouched: T6 stays active, T2 stays advisory.
    assert state.t_level_active == "T6"
    assert any(f.startswith("T6<->T2:") and f.endswith(":advisory")
               for f in state.disagreement_flags)

    # The clock verdict is new, and loud.
    assert state.host_clock["verdict"] == "fault"
    t2w = state.host_clock["witnesses"]["T2"]
    assert t2w["kind"] == "pair_ms"
    assert t2w["value"] == pytest.approx(11679.507, abs=0.001)
    assert t2w["exceeded"] is True
    assert "T2" in state.host_clock["reason"]

    p = _payload(tmp_path)
    assert p["host_clock"]["verdict"] == "fault"
    assert p["host_clock"]["since_utc"] == "2026-09-04T15:06:50.000000Z"


# --- the ND day --------------------------------------------------------------

def test_nd_replay_faults_even_though_the_tier_logic_demotes_to_none(tmp_path):
    t3 = FakeProbe("T3", _rtp("T3", 0.0, 3.1, detail={"stations_used": ["WWV", "CHU"]}))
    t2 = FakeProbe("T2", _sysclock("T2", -4179.35, witness_only=True))
    m = _manager(tmp_path, [t3, t2])
    state = m.tick()
    assert state.t_level_active is None                      # unchanged behaviour
    assert any(f.startswith("asymmetric-T3-T2:witness-only-T2:demoted-to:none")
               for f in state.disagreement_flags)
    assert state.host_clock["verdict"] == "fault"
    assert state.host_clock["witnesses"]["T2"]["value"] == pytest.approx(4179.35, abs=0.01)


# --- the other two witnesses -------------------------------------------------

def test_lb1421_host_gps_gap_faults_through_an_unavailable_t5(tmp_path):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t5 = FakeProbe("T5", ProbeResult("T5", available=False,
                                     reason="no valid fix: host_gps_inconsistent",
                                     detail={"host_minus_gps_s": -12.1}))
    m = _manager(tmp_path, [t6, t5])
    state = m.tick()
    assert state.host_clock["verdict"] == "fault"
    w = state.host_clock["witnesses"]["lb1421"]
    assert w["kind"] == "gps_second_s" and w["value"] == -12.1


def test_pps_rate_provider_makes_the_clock_suspect(tmp_path):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    m = _manager(tmp_path, [t6], host_clock_rate_provider=lambda: -90.4)
    state = m.tick()
    assert state.host_clock["verdict"] == "suspect"
    assert state.host_clock["witnesses"]["pps_rate"]["value"] == pytest.approx(-90.4)


def test_a_rate_provider_that_raises_is_ignored(tmp_path):
    def boom():
        raise RuntimeError("no gpsdo")
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    m = _manager(tmp_path, [t6], host_clock_rate_provider=boom)
    state = m.tick()
    assert state.host_clock["verdict"] == "unwitnessed"


def test_agreeing_witnesses_give_ok_and_no_alarm(tmp_path, caplog):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t2 = FakeProbe("T2", _sysclock("T2", 0.8))
    m = _manager(tmp_path, [t6, t2], host_clock_rate_provider=lambda: 3.0)
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.authority_manager"):
        state = m.tick()
    assert state.host_clock["verdict"] == "ok"
    assert state.host_clock["since_utc"] is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_witness_at_all_is_unwitnessed_and_silent(tmp_path, caplog):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    m = _manager(tmp_path, [t6])
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.authority_manager"):
        state = m.tick()
    assert state.host_clock["verdict"] == "unwitnessed"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_same_frame_witness_is_not_a_host_clock_witness(tmp_path):
    # T5-direct (rtp frame) disagreeing with T6 says something about an
    # anchor, not about the host clock.  It must not enter the verdict.
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t5 = FakeProbe("T5", _rtp("T5", 2000.0, 5.0))
    m = _manager(tmp_path, [t6, t5])
    state = m.tick()
    assert "T5" not in state.host_clock["witnesses"]
    assert state.host_clock["verdict"] == "unwitnessed"


# --- the alarm ---------------------------------------------------------------

def test_alarm_is_critical_on_entry_hourly_after_and_info_on_clear(tmp_path, caplog):
    clock = _Clock(T0)
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t2 = FakeProbe("T2", _sysclock("T2", -11679.5, witness_only=True))
    m = _manager(tmp_path, [t6, t2], clock=clock)
    log_name = "hf_timestd.core.authority_manager"

    def criticals():
        return [r for r in caplog.records
                if r.levelno == logging.CRITICAL and "HOST CLOCK" in r.getMessage()]

    with caplog.at_level(logging.INFO, logger=log_name):
        m.tick()
        assert len(criticals()) == 1
        assert "11679" in criticals()[0].getMessage()
        assert "T2" in criticals()[0].getMessage()

        clock.advance(30)
        m.tick()
        assert len(criticals()) == 1, "no repeat inside the hour"

        clock.advance(3600)
        m.tick()
        assert len(criticals()) == 2, "one repeat after the hour"

        t2.set(_sysclock("T2", 0.5))
        clock.advance(30)
        state = m.tick()
    assert state.host_clock["verdict"] == "ok"
    infos = [r for r in caplog.records
             if r.levelno == logging.INFO and "HOST CLOCK" in r.getMessage()]
    assert len(infos) == 1 and "cleared" in infos[0].getMessage().lower()
    assert len(criticals()) == 2


def test_thresholds_are_configurable(tmp_path):
    t6 = FakeProbe("T6", _rtp("T6", 0.0, 0.004))
    t2 = FakeProbe("T2", _sysclock("T2", -300.0, witness_only=True))
    strict = _manager(tmp_path, [t6, t2], host_clock_fault_ms=200.0)
    assert strict.tick().host_clock["verdict"] == "fault"
    lax = _manager(tmp_path, [t6, t2], host_clock_fault_ms=1000.0)
    assert lax.tick().host_clock["verdict"] == "suspect"
    rate = _manager(tmp_path, [t6], host_clock_rate_provider=lambda: 30.0,
                    host_clock_rate_suspect_ppm=20.0)
    assert rate.tick().host_clock["verdict"] == "suspect"
