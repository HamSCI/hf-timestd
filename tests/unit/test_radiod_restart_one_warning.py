"""One radiod restart: exactly one WARNING for the station, then silence.

Michael's ruling C (2026-10-30), finding N-2 of the final re-review.  The
I-7 fix made a radiod restart log one WARNING at the moment it happens,
and every channel's state change at INFO ("quiet").  But a quiet change
still opened the transition counter's hourly window, and an hour later
each channel summarised it at WARNING ("3 transitions in the last 60
min"): about six more WARNINGs, plus the station's own line.

This test runs the whole event: three channels hold planes, radiod
restarts (the raw pair moves first, the map a minute later), each channel
goes back through BOOTSTRAP, CANDIDATE and ACQUIRED, and the authority
manager's station counter watches the published summary every minute.
Then the data advance past the hour.  Across every ``hf_timestd`` logger
the event must cost exactly one WARNING.
"""

import logging
from datetime import datetime, timezone

from hf_timestd.core.authority_manager import AuthorityManager
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.state_transition_counter import StateTransitionCounter
from tests.unit.synth_origins import EPOCH_GPS_S, counter_map_for
from tests.unit.synth_ticks import label_timing
from tests.unit.test_metrology_service_counter_domains import (
    MIN,
    SR,
    T0,
    _acquire,
    _channel,
    _meta,
    _noise,
    _start,
    _ticks,
)
from tests.unit.test_new_radiod_run_logs_once import CHANNELS, K, RESTART_S


class _Rows:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(row)


class _MinuteClock:
    def __init__(self):
        self.minute = MIN

    def __call__(self):
        return datetime.fromtimestamp(self.minute + 5, tz=timezone.utc)


def _station(tmp_path):
    out = []
    for name, k0 in CHANNELS:
        svc = _channel(tmp_path, name)
        svc._shadow_transitions = StateTransitionCounter(name)
        svc._shadow_record = _Rows()
        _acquire(svc, k0)
        out.append((svc, k0))
    return out


def _authority(tmp_path, clock):
    return AuthorityManager(
        probes=[], output_path=tmp_path / "authority.json",
        a_level_provider=lambda: "A1", upgrade_hysteresis=1, now_fn=clock,
        t3_shadow_record=_Rows(),
        judge_status_path=tmp_path / "no-judge.json",
        registration_store=RegistrationStore(tmp_path / "reg",
                                             tmp_path / "registration.json"))


def _minute(svc, k0, k, *, restarted=False, new_map=False, quiet_air=False):
    """Minute k on one channel.  ``quiet_air``: the band is noise this
    minute, so a reset plane cannot re-acquire at once (BOOTSTRAP)."""
    meta = _meta(k0, k, walk=0.25)
    if restarted:
        meta["raw_gps_time_ns"] += RESTART_S * 10**9
    if new_map:
        meta["counter_map"] = counter_map_for(
            k0, SR, epoch_gps_s=EPOCH_GPS_S + RESTART_S)
    audio = _noise(100 + k) if quiet_air else _ticks(k, seed=20 + k)
    svc.apply_registration(label_timing(T0 + 60 * k, 0.25, SR), audio,
                           _start(k0, k), MIN + 60 * k, meta)


def _run(station, manager, clock, k, **kw):
    for svc, k0 in station:
        _minute(svc, k0, k, **kw)
    clock.minute = MIN + 60 * k
    manager.tick()


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith("hf_timestd")]


def test_a_radiod_restart_costs_one_warning_now_and_none_an_hour_later(
        tmp_path, caplog):
    station = _station(tmp_path)
    clock = _MinuteClock()
    manager = _authority(tmp_path, clock)
    _run(station, manager, clock, 1)                   # run A, planes held
    assert all(svc.acquirer.state == svc.acquirer.STATE_ACQUIRED
               for svc, _ in station)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        # radiod restarts: the raw pair moves at once, the map a minute later
        _run(station, manager, clock, K, restarted=True, quiet_air=True)
        for k in range(K + 1, K + 6):
            _run(station, manager, clock, k, restarted=True, new_map=True)
        # every channel went back through BOOTSTRAP, CANDIDATE and ACQUIRED
        for svc, _ in station:
            seen = [r["to"] for r in svc._shadow_record.rows
                    if r.get("kind") == "transition"]
            assert {"BOOTSTRAP", "CANDIDATE", "ACQUIRED"} <= set(seen), seen
            assert svc.acquirer.state == svc.acquirer.STATE_ACQUIRED
        # the station's published state moved too
        station_rows = [r for r in manager.t3_shadow_record.rows
                        if r.get("kind") == "transition"
                        and r.get("scope") == "station"
                        and r.get("from") is not None]
        assert station_rows, "the station counter saw the event"
        # past the hour every window opened by the restart has closed
        _run(station, manager, clock, K + 62, restarted=True, new_map=True)
        _run(station, manager, clock, K + 63, restarted=True, new_map=True)
    warns = _warnings(caplog)
    assert len(warns) == 1, warns
    assert "new radiod run" in warns[0]
