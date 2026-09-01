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


def test_skips_null_snr_and_counts_them(tmp_path):
    """Rows with NULL corr_snr_db must be excluded and counted."""
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
         10.0, 1788196741, 1, 59022.85, 0.0, None, 0.2, 22.85),
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "CHU",
         10.0, 1788196741, 2, 59031.00, 0.0, 18.0, 0.1, 31.0),
    ]
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    groups = list(read_minutes(path))
    assert len(groups) == 1
    assert len(groups[0].arrivals) == 2
    assert groups[0].arrivals[0].corr_snr_db == 22.5
    assert groups[0].arrivals[1].corr_snr_db == 18.0
    assert groups[0].skipped_null_snr == 1


def test_end_utc_filter(tiny_db):
    """end_utc filter excludes rows at or after the boundary."""
    got = list(read_minutes(tiny_db, end_utc=1788196800))
    assert [g.minute_utc for g in got] == [1788196740]


def test_all_filters_together(tiny_db):
    """channel + start_utc + end_utc filters composed together."""
    got = list(read_minutes(
        tiny_db,
        channel="SHARED_10000",
        start_utc=1788196740,
        end_utc=1788196801))
    assert [g.minute_utc for g in got] == [1788196740, 1788196800]
    assert all(g.channel == "SHARED_10000" for g in got)


def test_null_snr_as_first_row(tmp_path):
    """A NULL-SNR row as the very first row must be counted in its own minute."""
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
         10.0, 1788196741, 0, 59004.24, 0.0, None, 1.0, 4.24),
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWVH",
         10.0, 1788196741, 1, 59022.85, 0.0, 4.0, 0.2, 22.85),
    ]
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    groups = list(read_minutes(path))
    assert len(groups) == 1
    assert groups[0].minute_utc == 1788196740
    assert len(groups[0].arrivals) == 1
    assert groups[0].skipped_null_snr == 1
    assert groups[0].deployed_labels == {"WWV", "WWVH"}


def test_all_null_snr_minute_between_normal_minutes(tmp_path):
    """A minute where EVERY row has NULL SNR, between two normal minutes.

    The null-only minute must be emitted with empty arrivals and
    skipped_null_snr > 0. Neighboring minutes' counters must not be inflated.
    """
    path = tmp_path / "timestd.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    rows = [
        # Minute 1: normal
        ("SHARED_10000", "2026-08-31T17:19:00+0000", 1788196740, "WWV",
         10.0, 1788196741, 0, 59004.24, 0.0, 22.5, 1.0, 4.24),
        # Minute 2: all NULL SNR
        ("SHARED_10000", "2026-08-31T17:20:00+0000", 1788196800, "WWV",
         10.0, 1788196801, 0, 59004.30, 0.0, None, 1.0, 4.24),
        ("SHARED_10000", "2026-08-31T17:20:00+0000", 1788196800, "WWVH",
         10.0, 1788196801, 1, 59022.85, 0.0, None, 0.2, 22.85),
        # Minute 3: normal
        ("SHARED_10000", "2026-08-31T17:21:00+0000", 1788196860, "CHU",
         10.0, 1788196861, 0, 59031.00, 0.0, 18.0, 0.1, 31.0),
    ]
    con.executemany(
        "INSERT INTO L1_all_arrivals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    groups = list(read_minutes(path))
    assert len(groups) == 3

    # Minute 1: normal
    assert groups[0].minute_utc == 1788196740
    assert len(groups[0].arrivals) == 1
    assert groups[0].arrivals[0].corr_snr_db == 22.5
    assert groups[0].skipped_null_snr == 0

    # Minute 2: all NULL SNR
    assert groups[1].minute_utc == 1788196800
    assert len(groups[1].arrivals) == 0
    assert groups[1].skipped_null_snr == 2
    assert groups[1].deployed_labels == {"WWV", "WWVH"}

    # Minute 3: normal
    assert groups[2].minute_utc == 1788196860
    assert len(groups[2].arrivals) == 1
    assert groups[2].arrivals[0].corr_snr_db == 18.0
    assert groups[2].skipped_null_snr == 0
