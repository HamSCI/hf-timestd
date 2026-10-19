"""ConditionLog: a persisting condition logs once, then as a count.

v3.73 row 18.  timestd-fusion on AC0G-B4 wrote 124,000 lines an hour,
most of them the same few conditions repeated every cycle.  ConditionLog
logs a condition when it appears, a periodic count while it persists,
and one line when it clears.  The first and the last line keep the
level the caller gave, so a fault stays loud.
"""

import logging

import pytest

from hf_timestd.core.condition_log import ConditionLog


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def log(clock):
    return ConditionLog(
        logging.getLogger("test.condition_log"),
        summary_interval_s=600.0,
        clear_after_s=300.0,
        clock=clock,
    )


def _msgs(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records
            if r.name == "test.condition_log"]


def test_first_occurrence_logs_at_callers_level(log, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    assert log.note("WWVH_10MHz", "Rejecting outlier: WWVH_10MHz dev=40ms")
    assert _msgs(caplog) == [
        (logging.WARNING, "Rejecting outlier: WWVH_10MHz dev=40ms"),
    ]


def test_repeats_within_interval_log_nothing(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("WWVH_10MHz", "outlier 1")
    for i in range(50):
        clock.t += 8.0  # one fusion cycle; 400 s in all, under 600
        assert not log.note("WWVH_10MHz", f"outlier {i + 2}")
    assert len(_msgs(caplog)) == 1


def test_persisting_condition_logs_periodic_count(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("WWVH_10MHz", "outlier first")
    for i in range(80):  # 80 cycles x 8 s = 640 s, past one interval
        clock.t += 8.0
        log.note("WWVH_10MHz", f"outlier {i}")
    msgs = _msgs(caplog)
    assert len(msgs) == 2
    level, text = msgs[1]
    assert level == logging.WARNING
    # 75 repeats reach the 600 s mark (75 x 8 = 600).
    assert "WWVH_10MHz" in text
    assert "75 more" in text
    assert "outlier 74" in text  # the latest instance rides along
    assert log.active_keys() == ["WWVH_10MHz"]
    # The clear line carries the 5 repeats after the summary, and the total.
    clock.t += 301.0
    log.sweep()
    clear = _msgs(caplog)[-1][1]
    assert "81 occurrences" in clear
    assert "5 since the last report" in clear


def test_clear_logs_once_with_the_unsummarised_count(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("WWVH_10MHz", "outlier first")
    for _ in range(9):
        clock.t += 8.0
        log.note("WWVH_10MHz", "again")
    clock.t += 299.0
    log.sweep()
    assert len(_msgs(caplog)) == 1  # absent < clear_after_s: still active
    clock.t += 2.0
    log.sweep()
    msgs = _msgs(caplog)
    assert len(msgs) == 2
    level, text = msgs[1]
    assert level == logging.WARNING
    assert "WWVH_10MHz" in text and "cleared" in text
    assert "10 occurrences" in text
    assert "9 since the last report" in text
    assert log.active_keys() == []
    log.sweep()
    assert len(_msgs(caplog)) == 2  # the clear logs once


def test_condition_after_clear_logs_again(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("k", "first")
    clock.t += 400.0
    log.sweep()
    clock.t += 1.0
    assert log.note("k", "back again")
    assert [m for _, m in _msgs(caplog)][-1] == "back again"


def test_keys_track_independently(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("WWVH_10MHz", "a")
    log.note("WWVH_15MHz", "b")
    log.note("WWVH_10MHz", "a again")
    assert [m for _, m in _msgs(caplog)] == ["a", "b"]


def test_note_sweeps_other_keys(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("old", "old condition")
    clock.t += 301.0
    log.note("new", "new condition")
    texts = [m for _, m in _msgs(caplog)]
    assert texts[0] == "old condition"
    assert any("old" in t and "cleared" in t for t in texts[1:])
    assert texts[-1] == "new condition" or "new condition" in texts


def test_info_level_condition_stays_info(log, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("WWV_10", "WWV_10: Mode transition detected", level=logging.INFO)
    assert _msgs(caplog) == [(logging.INFO, "WWV_10: Mode transition detected")]


def test_clear_after_defaults_to_summary_interval(clock):
    c = ConditionLog(logging.getLogger("x"), summary_interval_s=120.0,
                     clock=clock)
    assert c.clear_after_s == 120.0


def test_summary_and_clear_carry_the_worst_value(log, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="test.condition_log")
    log.note("k", "first", value=3.0, value_name="|dev|", unit="ms")
    for v in (-12.5, 4.0, 7.0):
        clock.t += 200.0
        log.note("k", "again", value=v, value_name="|dev|", unit="ms")
    summary = _msgs(caplog)[1][1]
    assert "max |dev| 12.50 ms" in summary
    clock.t += 50.0
    log.note("k", "again", value=2.0, value_name="|dev|", unit="ms")
    clock.t += 301.0
    log.sweep()
    clear = _msgs(caplog)[-1][1]
    assert "max |dev| 2.00 ms" in clear  # reset after the summary
