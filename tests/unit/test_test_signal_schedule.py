"""The scientific-modulation test signal: WWV minute 8, WWVH minute 48.

Corrected 2026-08-26 (mjh).  The codebase carried minute **44** for WWVH
consistently across eight sites -- the detector's own gate, the metrology
writer, the ground-truth minute list feeding a weight-15.0 vote, the
discriminator feature, and four docstrings.  WWVH transmits no test
signal in minute 44, so every WWVH observation was taken from a minute
where there was nothing to hear.

Two consequences followed from it:

* the metrology writer blanked `station` on non-detection, and the schema
  permits only WWV/WWVH -- so every one of those guaranteed
  non-detections failed validation and was discarded.  The L2 test_signal
  product held ZERO records on AC0G-B4;
* the ground-truth path treated minute 44 as a test-signal minute at
  weight 15.0, and never looked at minute 48.

Defined once in wwv_constants now, so a schedule fact has one home.
"""
from __future__ import annotations

import pytest

from hf_timestd.core.wwv_constants import (
    TEST_SIGNAL_MINUTE,
    TEST_SIGNAL_MINUTES,
    station_for_test_minute,
)


class TestSchedule:
    def test_wwv_transmits_in_minute_8(self):
        assert TEST_SIGNAL_MINUTE["WWV"] == 8
        assert station_for_test_minute(8) == "WWV"

    def test_wwvh_transmits_in_minute_48(self):
        assert TEST_SIGNAL_MINUTE["WWVH"] == 48
        assert station_for_test_minute(48) == "WWVH"

    def test_minute_44_is_not_a_test_signal_minute(self):
        """The regression this file exists for."""
        assert 44 not in TEST_SIGNAL_MINUTES
        assert station_for_test_minute(44) is None

    def test_the_set_is_exactly_those_two(self):
        assert TEST_SIGNAL_MINUTES == {8, 48}

    def test_an_ordinary_minute_names_no_station(self):
        for m in (0, 1, 2, 7, 9, 30, 47, 49, 59):
            assert station_for_test_minute(m) is None


class TestEveryConsumerUsesTheOneDefinition:
    """A schedule fact copied into eight places is how it drifted."""

    def test_the_detector_gate_agrees(self):
        import inspect
        from hf_timestd.core import wwv_test_signal as m
        src = inspect.getsource(m)
        assert "TEST_SIGNAL_MINUTES" in src
        assert "[8, 44]" not in src

    def test_the_metrology_writer_agrees(self):
        import inspect
        from hf_timestd.core import metrology_service as m
        src = inspect.getsource(m)
        assert "TEST_SIGNAL_MINUTES" in src
        assert "[8, 44]" not in src

    def test_the_ground_truth_path_agrees(self):
        import inspect
        from hf_timestd.core import timing_calibrator as m
        src = inspect.getsource(m)
        assert "TEST_SIGNAL_MINUTES" in src
        assert "[8, 44]" not in src

    def test_the_discriminator_feature_agrees(self):
        import inspect
        from hf_timestd.core import probabilistic_discriminator as m
        src = inspect.getsource(m)
        assert "{8, 44}" not in inspect.getsource(m)
        assert "TEST_SIGNAL_MINUTES" in src


class TestMissingIsRecordable:
    """A detector that can only record its successes cannot yield a
    detection rate, and a closed path is then indistinguishable from a
    healthy one.  `station` is a property of the schedule, not of whether
    anything was heard; absence belongs in `quality_flag`."""

    def test_station_comes_from_the_schedule_not_the_detection(self):
        import inspect
        from hf_timestd.core import metrology_service as m
        src = inspect.getsource(m)
        assert "'station': station if detection.detected else ''" not in src
        assert "'station': station," in src
