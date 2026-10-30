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
        c.observe("WITNESS", "verified", 280)
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
