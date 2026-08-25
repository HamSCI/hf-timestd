"""_shutdown() must survive a recorder that never finished __init__.

Teardown runs in the failure path: something blew up during start-up and
a `finally` calls _shutdown to release whatever was acquired.  If it
touches an attribute a half-built object never got, it raises
AttributeError over the top of the original exception — and the operator
sees the teardown error instead of the reason the recorder failed to
start.

The test suite had been paying for this in advance: TestSharedMultiShutdown
builds its subject with __new__ and has to mirror every attribute
_shutdown reads, with comments recording each one added after it broke
("real __init__ sets this to None; the __new__ fast-path has to too").
Making teardown tolerant of a partial object is the fix for both.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def test_shutdown_of_a_barely_constructed_recorder_does_not_raise(tmp_path):
    """The minimum a caller could plausibly have: almost nothing."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.control = MagicMock()
    cr.recorders = {}
    cr.output_dir = tmp_path
    cr.start_time = 0.0
    cr.metrics = MagicMock()
    cr._write_status = MagicMock()

    cr._shutdown()          # must not raise


def test_shutdown_still_stops_what_is_present(tmp_path):
    """Tolerance must not become indifference: real handles still stop."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.control = MagicMock()
    cr.recorders = {}
    cr.output_dir = tmp_path
    cr.start_time = 0.0
    cr.metrics = MagicMock()
    cr._write_status = MagicMock()
    judge = MagicMock()
    multi = MagicMock()
    cr._offset_judge = judge
    cr._multi = multi

    cr._shutdown()

    judge.stop.assert_called_once()
    multi.stop.assert_called_once()
