"""A new radiod run logs one WARNING for the station, not one per channel.

Final review I-7 (2026-10-30).  One radiod restart made every channel log
a WARNING registration reset and a WARNING state transition: about a
dozen WARNINGs for one event.  Spec §5 asks for a fault to show loudly,
once.  Now the first channel to see the new run (by its counter map's run
identity) logs the event's one WARNING; every other channel, and every
consequence on each channel, logs at INFO.
"""

import logging

from hf_timestd.core.state_transition_counter import StateTransitionCounter
from tests.unit.synth_origins import EPOCH_GPS_S, counter_map_for
from tests.unit.test_metrology_service_counter_domains import (
    MIN,
    SR,
    T0,
    _acquire,
    _channel,
    _meta,
    _start,
    _ticks,
)
from tests.unit.synth_ticks import label_timing

CHANNELS = (("WWV_20000", 50), ("SHARED_10000", 51), ("WWV_15000", 51))
RESTART_S = 3600
# The restart lands past the transition counter's hour, so its own
# rate-limit window cannot hide the WARNINGs under test.
K = 70


class _Rows:
    def append(self, row):
        pass


def _station(tmp_path):
    out = []
    for name, k0 in CHANNELS:
        svc = _channel(tmp_path, name)
        svc._shadow_transitions = StateTransitionCounter(name)
        svc._shadow_record = _Rows()
        _acquire(svc, k0)
        out.append((svc, k0))
    return out


def _minute(svc, k0, k, *, restarted=False, new_map=False):
    meta = _meta(k0, k, walk=0.25)
    if restarted:
        meta["raw_gps_time_ns"] += RESTART_S * 10**9
    if new_map:
        meta["counter_map"] = counter_map_for(
            k0, SR, epoch_gps_s=EPOCH_GPS_S + RESTART_S)
    svc.apply_registration(label_timing(T0 + 60 * k, 0.25, SR), _ticks(k, seed=20 + k),
                           _start(k0, k), MIN + 60 * k, meta)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and r.name.startswith("hf_timestd")]


def test_one_restart_is_one_warning_across_the_station(tmp_path, caplog):
    station = _station(tmp_path)
    for svc, k0 in station:                  # run A, held planes
        _minute(svc, k0, 1)
    assert all(svc.acquirer.registration is not None for svc, _ in station)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        # radiod restarts: the raw pair moves at once, the map a minute later
        for svc, k0 in station:
            _minute(svc, k0, K, restarted=True)
        for svc, k0 in station:
            _minute(svc, k0, K + 1, restarted=True, new_map=True)
    warns = _warnings(caplog)
    assert len(warns) == 1, warns
    assert "new radiod run" in warns[0]
    assert "WWV_20000" in warns[0]           # the first channel to see it
    infos = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and "new radiod run" in r.getMessage()]
    assert len(infos) == len(CHANNELS) - 1, infos


def test_a_counter_map_step_on_one_run_still_warns(tmp_path, caplog):
    """Control: a whole-block step on one run means the held plane was
    wrong.  That stays a WARNING on the channel it hit."""
    (svc, k0), *_ = _station(tmp_path)
    _minute(svc, k0, 1)
    stepped = counter_map_for(k0 + 1, SR)    # same run, one block moved
    meta = _meta(k0, 2, cmap=stepped, walk=0.25)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        svc.apply_registration(label_timing(T0 + 120, 0.25, SR), _ticks(2),
                               _start(k0, 2), MIN + 120, meta)
    assert any("on one radiod run" in w for w in _warnings(caplog))


def test_a_second_restart_is_announced_again(tmp_path, caplog):
    """The announcement names the run; a later run is a new event."""
    station = _station(tmp_path)
    for svc, k0 in station:
        _minute(svc, k0, 1)
    with caplog.at_level(logging.INFO):
        for svc, k0 in station:
            _minute(svc, k0, K, restarted=True, new_map=True)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        for svc, k0 in station:
            meta = _meta(k0, 2 * K, walk=0.25)
            meta["raw_gps_time_ns"] += 2 * RESTART_S * 10**9
            meta["counter_map"] = counter_map_for(
                k0, SR, epoch_gps_s=EPOCH_GPS_S + 2 * RESTART_S)
            svc.apply_registration(label_timing(T0 + 120 * K, 0.25, SR),
                                   _ticks(2 * K), _start(k0, 2 * K),
                                   MIN + 120 * K, meta)
    warns = [w for w in _warnings(caplog) if "new radiod run" in w]
    assert len(warns) == 1, _warnings(caplog)
