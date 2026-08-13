"""The trailing summary of `grape daily`.

Regression cover for a NameError that referenced a `status` dict no scope
had held since the hs_uploader switch. It fired only on the SUCCESS path,
after every stage had completed and state was saved, so the pipeline did
all its work and then died reporting on it: 30 consecutive days of
systemd-visible failure, and -- because the catch-up sweep runs after this
point -- a recovery mechanism that never executed once.
"""
import pytest

from hf_timestd.cli import _grape_daily_summary


def test_no_lines_when_upload_was_not_attempted():
    assert _grape_daily_summary(False, False, {"upload_status": "skipped"}) == []


def test_pending_when_upload_attempted_but_not_ok():
    (line,) = _grape_daily_summary(True, False, {"upload_status": "failed"})
    assert "pending" in line.lower()


def test_external_daemon_is_named_as_such():
    (line,) = _grape_daily_summary(True, True, {"upload_status": "external"})
    assert "hs-uploader.service" in line


def test_drained_reports_the_engine_and_status():
    (line,) = _grape_daily_summary(True, True, {"upload_status": "completed"})
    assert "hs_uploader" in line and "completed" in line


@pytest.mark.parametrize("st", [{}, None, {"other": 1}])
def test_missing_upload_status_does_not_raise(st):
    """The original bug was an unguarded lookup on the success path. Any
    state shape must produce a line rather than an exception -- reporting
    is not worth losing a completed pipeline over."""
    out = _grape_daily_summary(True, True, st)
    assert len(out) == 1 and out[0].strip()


def test_success_path_never_raises_for_any_flag_combination():
    for attempted in (True, False):
        for ok in (True, False):
            for st in ({}, None, {"upload_status": "external"},
                       {"upload_status": "completed"}, {"upload_status": "failed"}):
                _grape_daily_summary(attempted, ok, st)
