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


# ── Ruling C (2026-10-30), N-2: a quiet window stays quiet ───────────


def _warns(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_quiet_change_and_its_recovery_summarise_at_info(caplog):
    """A radiod restart: ACQUIRED -> BOOTSTRAP (quiet) -> CANDIDATE ->
    ACQUIRED.  The restart's own line already warned; an hour later the
    window must not warn again."""
    c = StateTransitionCounter("WWV_10000")
    c.observe("ACQUIRED", "start", 0)
    with caplog.at_level(logging.INFO):
        c.observe("BOOTSTRAP", "new run", 60, quiet=True)
        c.observe("CANDIDATE", "acquirer", 120)
        c.observe("ACQUIRED", "acquirer", 180)
        c.observe("ACQUIRED", "acquirer", 3700)        # past the hour
    assert _warns(caplog) == []
    assert c.count == 3
    assert any("3 transitions" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)


def test_a_fault_after_the_recovery_still_warns(caplog):
    """Control: once the scope is back where it was, a new change is a new
    event.  It warns, and the window's summary warns."""
    c = StateTransitionCounter("WWV_10000")
    c.observe("ACQUIRED", "start", 0)
    with caplog.at_level(logging.INFO):
        c.observe("BOOTSTRAP", "new run", 60, quiet=True)
        c.observe("ACQUIRED", "acquirer", 120)
        c.observe("CONFLICT", "siblings disagree", 600)
        c.observe("CONFLICT", "siblings disagree", 3700)
    warns = _warns(caplog)
    assert len(warns) == 2, warns
    assert "ACQUIRED -> CONFLICT" in warns[0]
    assert "3 transitions" in warns[1]


# ── Fix round 1, I-1: a recovery that never completes ends anyway ────


def test_a_recovery_that_never_returns_ends_after_recovery_s(caplog):
    """Review I-1: ACQUIRED -> BOOTSTRAP (quiet) -> CANDIDATE, and the scope
    never returns to ACQUIRED.  Days of BOOTSTRAP <-> CANDIDATE flapping and
    then a CONFLICT logged no WARNING at all.  The recovery now ends
    RECOVERY_S after the quiet change; later changes warn as usual."""
    c = StateTransitionCounter("station")
    c.observe("ACQUIRED", "start", 0)
    day = 86_400
    with caplog.at_level(logging.INFO):
        c.observe("BOOTSTRAP", "new run", 60, quiet=True)
        c.observe("CANDIDATE", "acquirer", 120)
        assert _warns(caplog) == []
        for d in range(1, 6):                        # daily flaps
            c.observe("BOOTSTRAP" if d % 2 else "CANDIDATE", "flap", d * day)
        c.observe("CONFLICT", "siblings disagree", 7 * day)
    warns = _warns(caplog)
    assert len(warns) == 6, warns                   # each flap and the CONFLICT
    assert "CONFLICT" in warns[-1]


def test_the_recovery_bound_is_one_hour_from_the_quiet_change(caplog):
    c = StateTransitionCounter("station")
    c.observe("ACQUIRED", "start", 0)
    with caplog.at_level(logging.INFO):
        c.observe("BOOTSTRAP", "new run", 60, quiet=True)
        c.observe("CANDIDATE", "acquirer", 60 + c.RECOVERY_S - 60)   # inside
        assert _warns(caplog) == []
        c.observe("BOOTSTRAP", "acquirer", 60 + c.RECOVERY_S)       # outside
    assert len(_warns(caplog)) == 1


# ── Fix round 1, I-2: latch on the run identity ──────────────────────


def test_a_new_run_identity_covers_every_change_within_the_bound(caplog):
    """Review I-2: six writers share one summary.  Once the station is back
    in ACQUIRED, a lagging writer's unflagged CANDIDATE belongs to the same
    restart.  A newly seen run identity makes every change within
    RECOVERY_S part of that one event, return to the old state or not."""
    c = StateTransitionCounter("station")
    c.observe("ACQUIRED", "start", 0, run_id=1000.0)
    with caplog.at_level(logging.INFO):
        c.observe("BOOTSTRAP", "x", 60, quiet=True, run_id=1000.0)  # raw pair moved
        c.observe("ACQUIRED", "x", 120, run_id=4600.0)              # new run seen
        c.observe("CANDIDATE", "laggard", 180, run_id=1000.0)       # old id: not new
        c.observe("ACQUIRED", "x", 240, run_id=4600.4)              # same run, jitter
        c.observe("CANDIDATE", "laggard", 300)
        c.observe("ACQUIRED", "x", 360, run_id=4600.0)
        c.observe("ACQUIRED", "x", 4000, run_id=4600.0)             # past the hour
    assert _warns(caplog) == []
    with caplog.at_level(logging.INFO):
        c.observe("CONFLICT", "later", 120 + c.RECOVERY_S + 600, run_id=4600.0)
    assert len(_warns(caplog)) == 1
