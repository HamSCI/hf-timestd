"""scripts/pipeline-watchdog.sh must measure liveness, not detections.

2026-09-04: hf-timestd 41d052a stopped promoting noise tick ensembles into
L1_metrology rows.  Those rows had landed every minute on every channel; the
800 ms marker correlator alone detects in 1-5 minutes per hour on AC0G-B4.
The watchdog read "no L1 row in 180 s" as a dead service and restarted every
sparse channel, then fusion (no L3 rows without L1 input) and L2-calibration
(a state file that fusion writes only once converged), every 5 minutes on
both stations.  A healthy metrology service writes L2_detection_attempts and
L1_all_arrivals rows every processed minute; fusion publishes
fusion_status.json every cycle; L2-calibration pings its systemd watchdog.
The script is exercised in --dry-run with PATH shims for systemctl, logger,
curl and sqlite3 (the devbox has no sqlite3 CLI; the shim uses Python's).
"""
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WATCHDOG = REPO / "scripts" / "pipeline-watchdog.sh"
CHANNEL = "SHARED_10000"

SQLITE_SHIM = r'''#!/usr/bin/env python3
import sqlite3, sys
args = [a for a in sys.argv[1:] if not a.startswith("-")]
db, sql = args[0], args[1]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
for row in con.execute(sql):
    print("|".join("" if v is None else str(v) for v in row))
'''

SYSTEMCTL_SHIM = r'''#!/bin/bash
# is-enabled/is-active answer from $ENABLED_UNITS; everything else is a no-op.
cmd=""; unit=""
for a in "$@"; do case "$a" in --*) ;; *) if [[ -z "$cmd" ]]; then cmd="$a"; else unit="$a"; fi;; esac; done
case "$cmd" in
  is-enabled|is-active) for u in $ENABLED_UNITS; do [[ "$u" == "$unit" ]] && exit 0; done; exit 1 ;;
  is-failed) exit 1 ;;
  *) exit 0 ;;
esac
'''


def _make_db(path: Path, now: int, ages: dict, fusion_l3_age):
    """Create the tables the watchdog queries, one row each at now-age."""
    con = sqlite3.connect(path)
    for table in ("L1_metrology_measurements", "L2_detection_attempts", "L1_all_arrivals"):
        # Same shape hamsci-dsp writes: ISO write time + minute boundary, and
        # the (channel, timestamp_utc) index the watchdog's lookup relies on.
        con.execute(f"CREATE TABLE {table} (channel TEXT, timestamp_utc TEXT, "
                    f"minute_boundary_utc INTEGER)")
        con.execute(f"CREATE INDEX idx_{table}_chan_ts ON {table} (channel, timestamp_utc)")
        age = ages.get(table)
        if age is not None:
            iso = datetime.fromtimestamp(now - age, tz=timezone.utc).isoformat()
            con.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (CHANNEL, iso, now - age))
    con.execute("CREATE TABLE L3_fusion_timing (minute_boundary INTEGER)")
    if fusion_l3_age is not None:
        con.execute("INSERT INTO L3_fusion_timing VALUES (?)", (now - fusion_l3_age,))
    con.commit()
    con.close()


def _touch(path: Path, age: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    t = time.time() - age
    os.utime(path, (t, t))


@pytest.fixture
def env(tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, body in (("sqlite3", SQLITE_SHIM), ("systemctl", SYSTEMCTL_SHIM),
                       ("logger", "#!/bin/bash\nexit 0\n"), ("curl", "#!/bin/bash\nexit 0\n")):
        p = shim / name
        p.write_text(body)
        p.chmod(0o755)
    data_root = tmp_path / "data"
    (data_root / "phase2" / CHANNEL).mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return {
        "shim": shim, "data_root": data_root, "run_dir": run_dir,
        "db": data_root / "phase2" / "timestd.db",
    }


def _run(env, enabled_units):
    e = dict(os.environ)
    e["PATH"] = f"{env['shim']}:{e['PATH']}"
    e["ENABLED_UNITS"] = " ".join(enabled_units)
    e["DATA_ROOT"] = str(env["data_root"])
    e["RUN_DIR"] = str(env["run_dir"])
    e["SQLITE_DB"] = str(env["db"])
    r = subprocess.run(["bash", str(WATCHDOG), "--dry-run"], env=e,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


MET = f"timestd-metrology@{CHANNEL}.service"
FUS = "timestd-fusion.service"
CAL = "timestd-l2-calibration.service"


def test_sparse_detections_with_fresh_attempts_is_alive(env):
    """The regression: attempts and arrivals fresh, L1 rows an hour old."""
    now = int(time.time())
    _make_db(env["db"], now, {"L1_metrology_measurements": 3600,
                              "L2_detection_attempts": 70, "L1_all_arrivals": 70}, None)
    out = _run(env, [MET])
    assert "Would restart" not in out, out


def test_no_processed_minute_anywhere_restarts_metrology(env):
    now = int(time.time())
    _make_db(env["db"], now, {"L1_metrology_measurements": 3600,
                              "L2_detection_attempts": 900, "L1_all_arrivals": 900}, None)
    out = _run(env, [MET])
    assert f"Would restart {MET}" in out, out


def test_fusion_alive_by_status_file_despite_stale_l3(env):
    now = int(time.time())
    _make_db(env["db"], now, {}, fusion_l3_age=3600)
    _touch(env["run_dir"] / "fusion_status.json", 10)
    out = _run(env, [FUS])
    assert "Would restart" not in out, out


def test_fusion_stale_status_file_restarts(env):
    now = int(time.time())
    _make_db(env["db"], now, {}, fusion_l3_age=30)
    _touch(env["run_dir"] / "fusion_status.json", 700)
    out = _run(env, [FUS])
    assert f"Would restart {FUS}" in out, out


def test_calibration_not_judged_by_fusions_state_file(env):
    now = int(time.time())
    _make_db(env["db"], now, {}, None)
    _touch(env["data_root"] / "state" / "broadcast_calibration.json", 3600)
    out = _run(env, [CAL])
    assert "Would restart" not in out, out


def test_future_dated_row_is_not_liveness(env):
    """The future-grace clause: a row stamped an hour ahead is a clock fault
    upstream, not proof the service processed a minute just now."""
    now = int(time.time())
    _make_db(env["db"], now, {"L1_metrology_measurements": 3600,
                              "L2_detection_attempts": -3600, "L1_all_arrivals": 900}, None)
    out = _run(env, [MET])
    assert f"Would restart {MET}" in out, out
