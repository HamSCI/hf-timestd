"""StateTransitionCounter: each change logged once, counted."""
import logging

from hf_timestd.core.state_transition_counter import StateTransitionCounter


def test_first_state_is_startup_not_a_transition(caplog):
    c = StateTransitionCounter("SHARED_10000")
    with caplog.at_level(logging.DEBUG):
        row = c.observe("BOOTSTRAP", "first look", 1_793_000_000)
    assert row["reason"] == "startup"
    assert row["to"] == "BOOTSTRAP"
    assert c.count == 0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_each_change_logs_once_and_counts(caplog):
    c = StateTransitionCounter("SHARED_10000")
    with caplog.at_level(logging.DEBUG):
        c.observe("BOOTSTRAP", "startup", 100)
        first = c.observe("CANDIDATE", "two minutes agree", 160)
        repeat = c.observe("CANDIDATE", "still", 220)
        # Fix round 1: an hour apart, so each change opens its own window.
        c.observe("WITNESS", "verified", 160 + 3600)
    warns = [r.getMessage() for r in caplog.records
             if r.levelno == logging.WARNING]
    assert len(warns) == 2
    assert "T3 STATE SHARED_10000: BOOTSTRAP -> CANDIDATE" in warns[0]
    assert "two minutes agree" in warns[0]
    assert repeat is None
    assert c.count == 2
    assert first == {"kind": "transition", "scope": "SHARED_10000",
                     "from": "BOOTSTRAP", "to": "CANDIDATE",
                     "reason": "two minutes agree", "utc_minute": 160,
                     "count": 1}


def _levels(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records
            if r.levelno >= logging.INFO and "T3 STATE" in r.getMessage()]


def test_a_flapping_channel_logs_one_warning_per_window(caplog):
    """Fix round 1: ten changes inside 60 data minutes give one WARNING at
    the first, INFO for the other nine, and one summary WARNING when the
    window closes.  Every change still counts and returns its row."""
    c = StateTransitionCounter("SHARED_10000")
    states = ["CANDIDATE", "ACQUIRED"]
    with caplog.at_level(logging.DEBUG):
        c.observe("BOOTSTRAP", "startup", 0)
        rows = [c.observe(states[i % 2], "flap", 60 * (i + 1)) for i in range(10)]
        c.observe("ACQUIRED", "still", 60 * 30)   # same state, inside the window
        c.observe("ACQUIRED", "still", 60 + 3600)  # first observe at the window's end
    logs = _levels(caplog)
    warns = [m for lv, m in logs if lv == logging.WARNING]
    infos = [m for lv, m in logs if lv == logging.INFO]
    assert len(warns) == 2 and len(infos) == 9
    assert "BOOTSTRAP -> CANDIDATE" in warns[0]
    assert warns[1] == ("T3 STATE SHARED_10000: 10 transitions in the last "
                        "60 min, now ACQUIRED")
    assert all(r is not None and r["kind"] == "transition" for r in rows)
    assert c.count == 10 and rows[-1]["count"] == 10


def test_changes_ninety_minutes_apart_are_two_warnings_no_summary(caplog):
    c = StateTransitionCounter("SHARED_10000")
    with caplog.at_level(logging.DEBUG):
        c.observe("BOOTSTRAP", "startup", 0)
        c.observe("CANDIDATE", "a", 60)
        c.observe("ACQUIRED", "b", 60 + 90 * 60)
        c.observe("ACQUIRED", "still", 60 + 200 * 60)
    logs = _levels(caplog)
    assert [lv for lv, _ in logs] == [logging.WARNING, logging.WARNING]
    assert not any("transitions in the last" in m for _, m in logs)
    assert c.count == 2
