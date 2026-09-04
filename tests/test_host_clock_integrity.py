"""The host-clock integrity verdict — the thing nobody said out loud on
2026-09-04.

Three independent witnesses measured AC0G-B4's host clock 11.6 s slow that
day and each result went nowhere: the T2 pair check wrote ":advisory" into
authority.json, the LB-1421 probe computed a 12 s host-versus-GPS gap and
returned None, and gpsdo-monitor's PPS study printed 999.91 ms per second.
This module takes those three measurements and returns one verdict.  Pure
logic, no I/O, so the day can be replayed here.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.host_clock_integrity import (
    HostClockAlarm,
    assess,
)


def test_no_witnesses_is_unwitnessed():
    v = assess(pair_disagreements={}, gps_second_delta_s=None, rate_ppm=None)
    assert v.verdict == "unwitnessed"
    assert v.witnesses == ()


def test_pair_within_bound_is_ok():
    v = assess(pair_disagreements={"T2": (12.0, 60.0)},
               gps_second_delta_s=None, rate_ppm=None)
    assert v.verdict == "ok"
    (w,) = v.witnesses
    assert w.name == "T2" and w.exceeded is False
    assert w.value == 12.0 and w.bound == 60.0


def test_pair_past_bound_is_suspect():
    v = assess(pair_disagreements={"T2": (80.0, 60.0)},
               gps_second_delta_s=None, rate_ppm=None)
    assert v.verdict == "suspect"
    assert v.witnesses[0].exceeded is True


def test_pair_past_fault_ms_is_fault_b4_replay():
    # authority.json on AC0G-B4, 2026-09-04 15:06Z:
    # "T6<->T2:11679.507ms>60.000ms:advisory"
    v = assess(pair_disagreements={"T2": (11679.507, 60.0)},
               gps_second_delta_s=None, rate_ppm=None, fault_ms=1000.0)
    assert v.verdict == "fault"
    assert "T2" in v.reason and "11679" in v.reason


def test_gps_second_inside_window_is_ok():
    v = assess(pair_disagreements={}, gps_second_delta_s=0.3, rate_ppm=None)
    assert v.verdict == "ok"
    (w,) = v.witnesses
    assert w.name == "lb1421" and w.exceeded is False


def test_gps_second_outside_window_is_fault():
    # Lb1421T5Probe on B4 at 15:53Z: host 12.2 s slow, so
    # (host_now - fix_age) - pps_utc_sec came out near -12.
    v = assess(pair_disagreements={}, gps_second_delta_s=-12.1, rate_ppm=None)
    assert v.verdict == "fault"
    assert v.witnesses[0].exceeded is True


def test_rate_past_threshold_is_suspect():
    # gpsdo-monitor pps_study.period_ms_p50 = 999.91 -> -90 ppm
    v = assess(pair_disagreements={}, gps_second_delta_s=None, rate_ppm=-90.4,
               rate_suspect_ppm=50.0)
    assert v.verdict == "suspect"
    assert v.witnesses[0].name == "pps_rate"


def test_rate_within_threshold_is_ok():
    v = assess(pair_disagreements={}, gps_second_delta_s=None, rate_ppm=5.0)
    assert v.verdict == "ok"


def test_worst_verdict_wins():
    v = assess(pair_disagreements={"T4": (30.0, 60.0), "T2": (4179.0, 60.0)},
               gps_second_delta_s=None, rate_ppm=-90.0)
    assert v.verdict == "fault"
    names = {w.name for w in v.witnesses}
    assert names == {"T4", "T2", "pps_rate"}


def test_alarm_enters_once_repeats_hourly_and_clears():
    alarm = HostClockAlarm(repeat_sec=3600.0)
    fault = assess(pair_disagreements={"T2": (11679.0, 60.0)},
                   gps_second_delta_s=None, rate_ppm=None)
    ok = assess(pair_disagreements={"T2": (1.0, 60.0)},
                gps_second_delta_s=None, rate_ppm=None)
    assert alarm.update(fault, now=1000.0) == "enter"
    assert alarm.update(fault, now=1030.0) is None
    assert alarm.update(fault, now=1000.0 + 3600.0) == "repeat"
    assert alarm.update(fault, now=1000.0 + 3600.0 + 30.0) is None
    assert alarm.since == 1000.0
    assert alarm.update(ok, now=1000.0 + 7300.0) == "clear"
    assert alarm.since is None
    assert alarm.update(ok, now=1000.0 + 7330.0) is None


def test_alarm_treats_a_verdict_change_as_a_new_entry():
    alarm = HostClockAlarm(repeat_sec=3600.0)
    suspect = assess(pair_disagreements={"T2": (80.0, 60.0)},
                     gps_second_delta_s=None, rate_ppm=None)
    fault = assess(pair_disagreements={"T2": (2000.0, 60.0)},
                   gps_second_delta_s=None, rate_ppm=None)
    assert alarm.update(suspect, now=0.0) == "enter"
    assert alarm.update(fault, now=10.0) == "enter"
    assert alarm.since == 0.0, "since marks the first non-ok tick, not the escalation"


def test_unwitnessed_never_alarms():
    alarm = HostClockAlarm()
    none = assess(pair_disagreements={}, gps_second_delta_s=None, rate_ppm=None)
    assert alarm.update(none, now=0.0) is None
