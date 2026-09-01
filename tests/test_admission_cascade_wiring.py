#!/usr/bin/env python3
"""The live cascade observes; it must not alter anything.

Wired beside the arrival gate so live verdicts can be compared against the
replay that validated them (spec step 3). Nothing consumes it: no product, no
calibration, no clock. These tests pin that, and pin the one place it could
still fabricate — a measurement with no SNR must be skipped, not floored at
zero. That exact defect (`float(snr if snr is not None else 0.0)`) had to be
fixed in the replay reader, where a fabricated 0.0 dB would have become a
BELOW_FLOOR verdict about the ionosphere derived from missing data.
"""

import copy
import logging

import pytest

from hf_timestd.core.admission_cascade import AdmissionState
from hf_timestd.core.metrology_engine import MetrologyEngine
from hf_timestd.core.station_arrival_gate import StationWindow


class _Engine:
    """Only what _log_admission_cascade reads."""
    channel_name = "SHARED_10000"
    HISTORY_TOLERANCE_MS = MetrologyEngine.HISTORY_TOLERANCE_MS
    ADMISSION_FLOOR_SIGMA = MetrologyEngine.ADMISSION_FLOOR_SIGMA
    _admission_history = None


def _run(measurements, caplog):
    eng = _Engine()
    windows = {
        "WWV": StationWindow(station="WWV", min_ms=3.7, max_ms=9.1,
                             scatter_max_ms=19.4),
        "WWVH": StationWindow(station="WWVH", min_ms=22.0, max_ms=28.1,
                              scatter_max_ms=36.6),
    }
    expected = {"WWV": 4.05, "WWVH": 23.01}
    with caplog.at_level(logging.INFO, logger="hf_timestd.core.metrology_engine"):
        MetrologyEngine._log_admission_cascade(
            eng, measurements, expected, windows, {"WWV", "WWVH"})
    return eng, caplog.text


def test_a_clean_arrival_is_reported_admitted(caplog):
    m = [{"station": "WWV", "timing_error_ms": 0.2, "corr_snr_db": 20.0}]
    _, text = _run(m, caplog)
    assert "ADMISSION" in text
    assert "'WWV': 'ADMITTED'" in text


def test_it_does_not_mutate_the_measurements(caplog):
    """The engine's own measurement list feeds the science products."""
    m = [{"station": "WWV", "timing_error_ms": 0.2, "corr_snr_db": 20.0}]
    before = copy.deepcopy(m)
    _run(m, caplog)
    assert m == before


def test_a_measurement_with_no_snr_is_skipped_not_floored(caplog):
    """Missing SNR must not become 0.0 dB and then a BELOW_FLOOR verdict."""
    m = [{"station": "WWV", "timing_error_ms": 0.2, "corr_snr_db": None,
          "snr_db": None}]
    _, text = _run(m, caplog)
    assert "ADMISSION" in text
    assert "CHANNEL_SILENT" in text          # nothing above floor: honest
    assert "'WWV': 'ADMITTED'" not in text


def test_history_persists_across_minutes(caplog):
    """Key 3 is meaningless if the tracker resets every minute."""
    m = [{"station": "WWV", "timing_error_ms": 0.2, "corr_snr_db": 20.0}]
    eng, _ = _run(m, caplog)
    assert eng._admission_history is not None
    first = eng._admission_history
    with caplog.at_level(logging.INFO):
        MetrologyEngine._log_admission_cascade(
            eng, m, {"WWV": 4.05},
            {"WWV": StationWindow(station="WWV", min_ms=3.7, max_ms=9.1,
                                  scatter_max_ms=19.4)}, {"WWV"})
    assert eng._admission_history is first


def test_no_windows_means_no_verdict(caplog):
    eng = _Engine()
    with caplog.at_level(logging.INFO):
        MetrologyEngine._log_admission_cascade(eng, [], {}, {}, set())
    assert "ADMISSION" not in caplog.text


def test_the_calibrated_tolerances_are_the_measured_ones():
    """Guards against a silent retune away from what the archive showed."""
    assert MetrologyEngine.HISTORY_TOLERANCE_MS == {
        "WWV": 5.0, "WWVH": 6.0, "BPM": 9.0}
    assert MetrologyEngine.ADMISSION_FLOOR_SIGMA == 3.5
