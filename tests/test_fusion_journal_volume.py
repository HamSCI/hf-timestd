"""timestd-fusion journal volume (v3.73 row 18).

On AC0G-B4 timestd-fusion wrote 124,000 lines an hour.  The top lines
repeated one condition on every cycle: "Rejecting outlier" for the same
WWVH channel, the Kalman filter's "Mode transition detected", the
cross-frequency failure and the HW calibration UN-CONVERGE line.  Each
now logs when it appears, then as a periodic count (ConditionLog).
"""

import logging
import tempfile
import time
from pathlib import Path

import pytest

from hf_timestd.core.broadcast_kalman_filter import BroadcastKalmanFilter
from hf_timestd.core.condition_log import ConditionLog
from hf_timestd.core.multi_broadcast_fusion import (
    BroadcastMeasurement,
    MultiBroadcastFusion,
)

FUSION_LOGGER = "hf_timestd.core.multi_broadcast_fusion"
KALMAN_LOGGER = "hf_timestd.core.broadcast_kalman_filter"


class FakeClock:
    def __init__(self) -> None:
        self.t = 5000.0

    def __call__(self) -> float:
        return self.t


def _m(station, freq_mhz, d_clock_ms, mode="1F"):
    return BroadcastMeasurement(
        timestamp=time.time(),
        station=station,
        frequency_mhz=freq_mhz,
        d_clock_ms=d_clock_ms,
        propagation_delay_ms=10.0,
        propagation_mode=mode,
        confidence=0.9,
        snr_db=25.0,
        quality_grade="A",
        channel_name=f"{station}_{freq_mhz}",
        raw_arrival_time_ms=100.0,
        uncertainty_ms=1.0,
        kalman_uncertainty_ms=1.0,
    )


@pytest.fixture
def fusion_and_clock():
    clock = FakeClock()
    with tempfile.TemporaryDirectory() as td:
        fusion = MultiBroadcastFusion(data_root=Path(td))
        fusion._conditions = ConditionLog(
            logging.getLogger(FUSION_LOGGER), clock=clock
        )
        fusion._kalman_conditions = ConditionLog(
            logging.getLogger(KALMAN_LOGGER), clock=clock
        )
        yield fusion, clock


def _records(caplog, needle, logger=FUSION_LOGGER):
    return [r for r in caplog.records
            if r.name == logger and needle in r.getMessage()]


OUTLIER_SET = [
    _m("WWV", 10.0, 2.0),
    _m("WWV", 15.0, 2.1),
    _m("WWVH", 10.0, 1.9),
    _m("WWVH", 15.0, 50.0),
]


def test_repeated_outlier_logs_once_then_a_count(fusion_and_clock, caplog):
    fusion, clock = fusion_and_clock
    fusion._read_latest_measurements = lambda *a, **k: list(OUTLIER_SET)
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    for _ in range(5):
        clock.t += 8.0
        result = fusion.fuse(skip_write=True)
        assert result.outliers_rejected >= 1  # the filter still rejects
    first = _records(caplog, "Rejecting outlier")
    assert len(first) == 1
    assert first[0].levelno == logging.WARNING
    assert "WWVH_15.0MHz" in first[0].getMessage()

    # 75 cycles of 8 s after the first reach the 600 s summary interval.
    for _ in range(71):
        clock.t += 8.0
        fusion.fuse(skip_write=True)
    assert len([r for r in _records(caplog, "Rejecting outlier")
                if r.getMessage().startswith("Rejecting outlier")]) == 1
    summary = _records(caplog, "still present")
    assert len(summary) == 1
    assert summary[0].levelno == logging.WARNING
    assert "WWVH_15.0MHz" in summary[0].getMessage()
    assert "75 more" in summary[0].getMessage()


def test_outlier_clear_logs_at_warning(fusion_and_clock, caplog):
    fusion, clock = fusion_and_clock
    fusion._read_latest_measurements = lambda *a, **k: list(OUTLIER_SET)
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    fusion.fuse(skip_write=True)
    fusion._read_latest_measurements = lambda *a, **k: list(OUTLIER_SET[:3])
    clock.t += 601.0
    fusion.fuse(skip_write=True)  # a clean cycle sweeps the old condition
    cleared = _records(caplog, "cleared")
    assert len(cleared) == 1
    assert cleared[0].levelno == logging.WARNING
    assert "WWVH_15.0MHz" in cleared[0].getMessage()


def test_cross_frequency_failure_logs_once(fusion_and_clock, caplog):
    fusion, clock = fusion_and_clock
    spread = [_m("WWVH", 10.0, 1.0), _m("WWVH", 15.0, 20.0)]
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    for _ in range(5):
        clock.t += 8.0
        ok, _reason, _dev = fusion._validate_cross_frequency_d_clock(spread)
        assert not ok  # the verdict itself never changes
    failed = [r for r in _records(caplog, "Cross-frequency validation FAILED")
              if r.levelno >= logging.WARNING]
    assert len(failed) == 1


def test_kalman_repeated_mode_transition_logs_once():
    clock = FakeClock()
    kf = BroadcastKalmanFilter("WWVH_10", "WWVH", 10.0)
    kf._conditions = ConditionLog(
        logging.getLogger(KALMAN_LOGGER), clock=clock
    )
    kf.update(0.0, 20.0)
    verdicts = []
    detect = kf.detect_mode_transition
    kf.detect_mode_transition = lambda inn: verdicts.append(detect(inn)) or verdicts[-1]
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    lg = logging.getLogger(KALMAN_LOGGER)
    lg.addHandler(handler)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        for i in range(40):
            clock.t += 8.0
            kf.update(80.0 if i % 2 == 0 else -80.0, 20.0)
        lines = [r for r in records if "Mode transition" in r.getMessage()]
        # The filter keeps flagging transitions; the journal hears once.
        assert verdicts.count("MODE_CHANGE") > 1
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old)


# ---------------------------------------------------------------------
# Fix round 1: each feed keeps its own conditions; every call site and
# the Kalman bank's sweep are covered.
# ---------------------------------------------------------------------

CLEAN_SET = OUTLIER_SET[:3]


def _first_lines(caplog, prefix):
    return [r for r in caplog.records
            if r.name == FUSION_LOGGER and r.getMessage().startswith(prefix)]


def test_each_feed_logs_its_own_outlier_and_clear(fusion_and_clock, caplog):
    """L1 and L2 run separate fuse() passes; L2 publishes D_clock.  An L1
    condition must not hide the onset or the clear of the same one on L2."""
    fusion, clock = fusion_and_clock
    fusion._read_latest_measurements = lambda *a, **k: list(OUTLIER_SET)
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    fusion.fuse(force_l1_only=True, skip_write=True)
    fusion.fuse(skip_write=True)
    firsts = _first_lines(caplog, "Rejecting outlier")
    assert len(firsts) == 2
    assert any("(L1)" in r.getMessage() for r in firsts)
    assert any("(L2)" in r.getMessage() for r in firsts)

    # L2 goes clean, L1 keeps its outlier.
    fusion._read_latest_measurements = lambda *a, **k: list(
        OUTLIER_SET if k.get("force_l1_only") else CLEAN_SET
    )
    for _ in range(80):
        clock.t += 8.0
        fusion.fuse(force_l1_only=True, skip_write=True)
        fusion.fuse(skip_write=True)
    cleared = [r.getMessage() for r in _records(caplog, "cleared")]
    assert any(m.startswith("L2 outlier WWVH_15.0MHz") for m in cleared)
    assert not any(m.startswith("L1 outlier") for m in cleared)


def test_outlier_summary_carries_the_worst_deviation(fusion_and_clock, caplog):
    fusion, clock = fusion_and_clock
    fusion._read_latest_measurements = lambda *a, **k: list(OUTLIER_SET)
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    for _ in range(80):
        clock.t += 8.0
        fusion.fuse(skip_write=True)
    summary = _records(caplog, "still present")
    assert len(summary) == 1
    assert "max |dev|" in summary[0].getMessage()


def test_cross_frequency_feeds_keep_separate_keys(fusion_and_clock, caplog):
    fusion, clock = fusion_and_clock
    spread = [_m("WWVH", 10.0, 1.0), _m("WWVH", 15.0, 20.0)]
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    fusion._validate_cross_frequency_d_clock(spread, feed="L1")
    fusion._validate_cross_frequency_d_clock(spread, feed="L2")
    failed = _first_lines(caplog, "Cross-frequency validation FAILED")
    assert len(failed) == 2
    assert {("(L1)" in r.getMessage(), "(L2)" in r.getMessage())
            for r in failed} == {(True, False), (False, True)}


def _seed_converged(fusion, station, freq):
    from hf_timestd.core.multi_broadcast_fusion import BroadcastCalibration
    key = fusion._get_broadcast_key(station, freq)
    fusion.calibration[key] = BroadcastCalibration(
        station=station, frequency_mhz=freq, offset_ms=0.0,
        uncertainty_ms=1.0, n_samples=50, last_updated=time.time(),
        hardware_offset_ms=0.0, hardware_converged=True,
    )
    fusion.calibration_update_count = 1000  # past bootstrap


@pytest.mark.parametrize("d_clock, prefix", [
    (15.0, "HW Calibration UN-CONVERGE FAST"),
    (5.0, "HW Calibration UN-CONVERGE WWV"),
])
def test_unconverge_logs_once_per_broadcast(fusion_and_clock, caplog,
                                            d_clock, prefix):
    fusion, clock = fusion_and_clock
    _seed_converged(fusion, "WWV", 10.0)
    caplog.set_level(logging.DEBUG, logger=FUSION_LOGGER)
    for _ in range(12):
        clock.t += 8.0
        fusion._update_calibration([_m("WWV", 10.0, d_clock)])
    lines = _first_lines(caplog, prefix)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO


def test_kalman_bank_line_names_feed_and_clears_when_off_air(
        fusion_and_clock, caplog):
    """A broadcast that goes off air stops calling update(); fuse() must
    still sweep its condition so the clear line and its count arrive."""
    fusion, clock = fusion_and_clock
    caplog.set_level(logging.DEBUG)
    kf = fusion._get_or_create_broadcast_kalman("WWVH_10", "WWVH", 10.0, "l2")
    kf.update(0.0, 20.0)
    for i in range(10):
        clock.t += 8.0
        kf.update(80.0 if i % 2 == 0 else -80.0, 20.0)
    lines = [r.getMessage() for r in caplog.records
             if r.name == KALMAN_LOGGER and "Mode transition" in r.getMessage()]
    assert lines == ["L2 WWVH_10: Mode transition detected"]

    # Off air: no more updates for this broadcast; fuse() keeps cycling.
    fusion._read_latest_measurements = lambda *a, **k: []
    clock.t += 601.0
    fusion.fuse(skip_write=True)
    cleared = [r for r in caplog.records
               if "mode transitions: cleared" in r.getMessage()]
    assert len(cleared) == 1
    assert cleared[0].getMessage().startswith("L2 WWVH_10 mode transitions")
