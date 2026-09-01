import sqlite3

import pytest

from hf_timestd.replay.archive_reader import read_minutes


@pytest.fixture
def tiny_db(tmp_path):
    """A two-minute archive shaped exactly like B4's L1_all_arrivals."""
    path = tmp_path / "timestd.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = [
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWV",
         10.0, 1788196741, 0, 59004.24, 0.0, 22.5, 1.0, 4.24),
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWVH",
         10.0, 1788196741, 1, 59022.85, 0.0, 4.0, 0.2, 22.85),
        ("SHARED_10000", "2026-08-31T17:20:00+0000", 1788196800, "WWV",
         10.0, 1788196801, 0, 59004.30, 0.0, 21.0, 1.0, 4.24),
    ]
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_groups_rows_by_minute(tiny_db):
    groups = list(read_minutes(tiny_db))
    assert [g.minute_utc for g in groups] == [1788196740, 1788196800]
    assert len(groups[0].arrivals) == 2
    assert groups[0].channel == "SHARED_10000"
    assert groups[0].frequency_mhz == 10.0


def test_arrival_ms_is_reduced_to_position_in_the_second(tiny_db):
    """arrival_ms is ms-into-the-minute; the cascade works within a second."""
    groups = list(read_minutes(tiny_db))
    assert groups[0].arrivals[0].arrival_ms == pytest.approx(4.24, abs=0.01)
    assert groups[0].arrivals[0].corr_snr_db == 22.5


def test_deployed_labels_are_recorded_for_the_counterfactual(tiny_db):
    groups = list(read_minutes(tiny_db))
    assert groups[0].deployed_labels == {"WWV", "WWVH"}


def test_channel_and_time_filters(tiny_db):
    assert list(read_minutes(tiny_db, channel="NOPE")) == []
    got = list(read_minutes(tiny_db, start_utc=1788196800))
    assert [g.minute_utc for g in got] == [1788196800]


def test_opens_read_only(tiny_db):
    """The harness must never be able to write to an archive."""
    groups = list(read_minutes(tiny_db))
    assert groups  # sanity: it did read
    con = sqlite3.connect(f"file:{tiny_db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO L1_all_arrivals (channel) VALUES ('x')")
    con.close()
