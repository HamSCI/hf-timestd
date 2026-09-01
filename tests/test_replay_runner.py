# tests/test_replay_runner.py
import sqlite3

import pytest

from hf_timestd.core.admission_cascade import AdmissionState, ChannelState
from hf_timestd.core.station_arrival_gate import StationWindow
from hf_timestd.replay.report import summarise
from hf_timestd.replay.runner import replay


class FakeSource:
    """Fixed geometry, so the test exercises the runner not the ionosphere."""
    def windows_for(self, minute_utc, frequency_mhz):
        return {"WWV": StationWindow(station="WWV", min_ms=3.0, max_ms=5.0, scatter_max_ms=20.0),
                "WWVH": StationWindow(station="WWVH", min_ms=22.0, max_ms=24.0, scatter_max_ms=60.0)}

    def eligible_for(self, minute_utc, frequency_mhz):
        return {"WWV", "WWVH"}


class FakeSourceRefusedGeometry:
    """Geometry source that refuses to provide windows."""
    def windows_for(self, minute_utc, frequency_mhz):
        return {}

    def eligible_for(self, minute_utc, frequency_mhz):
        return {"WWV", "WWVH"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = []
    for i in range(4):
        minute = 1788196740 + 60 * i
        # WWV real and strong; WWVH labelled by the deployed model but weak
        rows.append(("SHARED_10000", "", minute, "WWV", 10.0, minute + 1,
                     0, 59004.0, 0.0, 25.0, 1.0, 4.24))
        rows.append(("SHARED_10000", "", minute, "WWVH", 10.0, minute + 1,
                     1, 59023.0, 0.0, 3.0, 0.1, 22.85))
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_replay_admits_wwv_and_refuses_weak_wwvh(db):
    verdicts = list(replay(db, FakeSource(), floor_snr_db=10.0,
                           tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert len(verdicts) == 4
    for mv in verdicts:
        assert mv.verdict.stations["WWV"].state is AdmissionState.ADMITTED
        assert mv.verdict.stations["WWVH"].state is AdmissionState.BELOW_FLOOR
        assert mv.verdict.channel_state is ChannelState.CHANNEL_PARTIAL


def test_summary_counts_the_deployed_over_report(db):
    """The deployed model labelled WWVH every minute; the cascade did not."""
    report = summarise(replay(db, FakeSource(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert report.state_counts[AdmissionState.ADMITTED] == 4
    assert report.state_counts[AdmissionState.BELOW_FLOOR] == 4
    assert report.deployed_over_reports == 4
    assert report.deployed_under_reports == 0


def test_summary_buckets_by_utc_hour(db):
    report = summarise(replay(db, FakeSource(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert 17 in report.by_hour
    assert sum(report.by_hour[17].values()) == 8  # 2 stations x 4 minutes


@pytest.fixture
def db_with_skipped_snr(tmp_path):
    """Database with arrivals that have null SNR (to be skipped)."""
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = []
    minute = 1788196740
    # Two good arrivals, one with null SNR (should be skipped)
    rows.append(("SHARED_10000", "", minute, "WWV", 10.0, minute + 1,
                 0, 59004.0, 0.0, 25.0, 1.0, 4.24))
    rows.append(("SHARED_10000", "", minute, "WWVH", 10.0, minute + 1,
                 1, 59023.0, 0.0, 3.0, 0.1, 22.85))
    rows.append(("SHARED_10000", "", minute, "CHU", 10.0, minute + 1,
                 2, 59050.0, 0.0, None, 0.05, 50.0))  # null SNR
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_geometry_refused_yields_verdict_not_skip(db):
    """When windows_for returns {}, a MinuteVerdict IS yielded with geometry_refused=True."""
    verdicts = list(replay(db, FakeSourceRefusedGeometry(), floor_snr_db=10.0,
                           tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert len(verdicts) == 4  # All 4 minutes yield verdicts
    for mv in verdicts:
        assert mv.geometry_refused is True
        assert mv.verdict is None


def test_geometry_refused_counted_in_summary(db):
    """Geometry-refused minutes are counted in ReplayReport.geometry_refused."""
    report = summarise(replay(db, FakeSourceRefusedGeometry(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert report.geometry_refused == 4
    assert report.state_counts.total() == 0  # No states counted (verdict was None)


def test_skipped_null_snr_threaded_to_report(db_with_skipped_snr):
    """skipped_null_snr from MinuteGroup reaches ReplayReport.skipped_null_snr_total."""
    report = summarise(replay(db_with_skipped_snr, FakeSource(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    assert report.skipped_null_snr_total == 1


def test_render_output_shows_geometry_refused_and_skipped(db):
    """render() output includes geometry_refused and skipped_null_snr lines."""
    report = summarise(replay(db, FakeSourceRefusedGeometry(), floor_snr_db=10.0,
                              tolerance_ms=1.0, lookback=5, reacquire_after=3))
    output = report.render()
    assert "geometry refused: 4" in output
    assert "arrivals with null SNR (skipped):" in output
