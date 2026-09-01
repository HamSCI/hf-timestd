import subprocess
import sqlite3
import sys


def _db(tmp_path):
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE L1_all_arrivals ("
        " channel TEXT, timestamp_utc TEXT, minute_boundary_utc INTEGER,"
        " station TEXT, frequency_mhz REAL, utc_second INTEGER,"
        " peak_rank INTEGER, arrival_ms REAL, timing_error_ms REAL,"
        " corr_snr_db REAL, peak_value REAL, model_expected_ms REAL)")
    con.execute(
        "INSERT INTO L1_all_arrivals VALUES"
        " ('SHARED_10000','',1788196740,'WWV',10.0,1788196741,0,"
        "  59004.0,0.0,25.0,1.0,4.24)")
    con.commit()
    con.close()
    return path


def test_cli_runs_and_reports(tmp_path):
    db = _db(tmp_path)
    out = subprocess.run(
        [sys.executable, "scripts/replay_admission.py", str(db),
         "--channel", "SHARED_10000"],
        capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    assert "minutes replayed" in out.stdout
    assert "state counts" in out.stdout


def test_cli_no_match_fails_loudly_instead_of_reporting_zeroes(tmp_path):
    db = _db(tmp_path)
    out = subprocess.run(
        [sys.executable, "scripts/replay_admission.py", str(db),
         "--channel", "NOPE_9999"],
        capture_output=True, text=True, timeout=300)
    assert out.returncode != 0
    combined = out.stdout + out.stderr
    assert "no minutes matched" in combined
    assert "NOPE_9999" in combined
