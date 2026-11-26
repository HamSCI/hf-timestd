"""`storage compact`: estimate, prepare, holders, swap for timestd.db."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_SIG = os.environ.get("SIGMOND_LIB_FOR_TESTS") or str(
    Path(__file__).resolve().parents[3] / "sigmond" / "lib")
if os.path.isdir(_SIG):
    sys.path.insert(0, _SIG)
from hf_timestd import storage_compact, storage_items, storage_retention  # noqa: E402

needs_kit = pytest.mark.skipif(storage_items.load_kit() is None,
                               reason="sigmond client kit absent")
pytestmark = needs_kit


def _big_db(root, wal=True):
    db = root / "phase2" / "timestd.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE L2_tick_phase (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE noalias (a, b)")  # rowid without an INTEGER PRIMARY KEY
    con.executemany("INSERT INTO L2_tick_phase (v) VALUES (?)", [("x" * 200,)] * 2000)
    con.executemany("INSERT INTO noalias VALUES (?, ?)", [(i, i) for i in range(300)])
    con.execute("DELETE FROM L2_tick_phase WHERE id % 2 = 0")
    con.execute("DELETE FROM noalias WHERE a % 3 = 0")
    con.commit()
    con.close()
    return db


def _insert(db, table, n):
    con = sqlite3.connect(db)
    if table == "noalias":
        con.executemany("INSERT INTO noalias VALUES (?, ?)", [(1, 1)] * n)
    else:
        con.executemany(f"INSERT INTO {table} (v) VALUES (?)", [("new",)] * n)
    con.commit()
    con.close()


def _empty_proc(root):
    p = root / "proc"
    p.mkdir(exist_ok=True)
    return p


def _fake_proc(root, pid, target, cgroup):
    p = root / "proc" / str(pid)
    (p / "fd").mkdir(parents=True)
    (p / "fd" / "7").symlink_to(target)
    (p / "cgroup").write_text(cgroup)
    return root / "proc"


def _files(db):
    return sorted(p.name for p in db.parent.iterdir())


def test_prepare_refuses_without_room(tmp_path, monkeypatch):
    db = _big_db(tmp_path)
    live = storage_compact._stats(db)[0]
    monkeypatch.setattr(storage_compact.shutil, "disk_usage",
                        lambda p: type("U", (), {"free": int(live * 1.2) - 1})())
    a = storage_compact.prepare(db, margin_bytes=0)
    assert a["refused"] is True and "free space" in a["reason"]
    assert not Path(f"{db}.compact").exists()


def test_prepare_makes_an_incremental_copy_and_leaves_the_live_file(tmp_path):
    db = _big_db(tmp_path)
    a = storage_compact.prepare(db, margin_bytes=0)
    assert a["refused"] is False and a["compact_bytes"] > 0
    assert set(a) >= {"live_bytes", "file_bytes", "compact_bytes", "free_bytes",
                      "seconds", "refused", "reason"}
    assert sqlite3.connect(f"{db}.compact").execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert sqlite3.connect(db).execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    side = json.loads(Path(f"{db}.compact.json").read_text())
    assert side["max_rowid"]["L2_tick_phase"] == 1999 and "prepared_utc" in side


def test_prepare_replaces_a_stale_copy(tmp_path):
    db = _big_db(tmp_path)
    Path(f"{db}.compact").write_bytes(b"junk")
    Path(f"{db}.compact.json").write_text("junk")
    storage_compact.prepare(db, margin_bytes=0)
    assert sqlite3.connect(f"{db}.compact").execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_rowids_survive_vacuum_into_for_a_table_without_an_alias(tmp_path):
    db = _big_db(tmp_path)
    live = sqlite3.connect(db).execute("SELECT min(rowid), max(rowid) FROM noalias").fetchone()
    storage_compact.prepare(db, margin_bytes=0)
    assert sqlite3.connect(f"{db}.compact").execute(
        "SELECT min(rowid), max(rowid) FROM noalias").fetchone() == live


def test_swap_refuses_while_a_holder_remains(tmp_path):
    db = _big_db(tmp_path)
    storage_compact.prepare(db, margin_bytes=0)
    proc = _fake_proc(tmp_path, 4242, f"{db}-wal", "0::/system.slice/hf-timestd-recorder.service\n")
    a = storage_compact.swap(db, proc_root=proc)
    assert a["refused"] is True and "4242" in a["reason"]
    assert sqlite3.connect(db).execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_swap_refuses_without_a_copy_or_with_an_old_one(tmp_path):
    db = _big_db(tmp_path)
    assert storage_compact.swap(db, proc_root=_empty_proc(tmp_path))["refused"] is True
    storage_compact.prepare(db, margin_bytes=0)
    old = Path(f"{db}.compact")
    os.utime(old, (1, 1))
    a = storage_compact.swap(db, proc_root=_empty_proc(tmp_path))
    assert a["refused"] is True and "old" in a["reason"]
    Path(f"{db}.compact.json").unlink()
    storage_compact.prepare(db, margin_bytes=0)
    Path(f"{db}.compact.json").unlink()
    assert storage_compact.swap(db, proc_root=_empty_proc(tmp_path))["refused"] is True


def test_swap_catches_up_rows_written_after_prepare(tmp_path):
    db = _big_db(tmp_path)
    storage_compact.prepare(db, margin_bytes=0)
    _insert(db, "L2_tick_phase", 5)
    _insert(db, "noalias", 3)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE late (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO late (v) VALUES (?)", [("a",), ("b",)])
    con.commit()
    con.close()
    a = storage_compact.swap(db, proc_root=_empty_proc(tmp_path))
    assert a["refused"] is False, a
    assert a["rows_caught_up"]["L2_tick_phase"] == 5
    assert a["rows_caught_up"]["noalias"] == 3 and a["rows_caught_up"]["late"] == 2
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM L2_tick_phase").fetchone()[0] == 1005
    assert con.execute("SELECT count(*) FROM late").fetchone()[0] == 2
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    assert not Path(f"{db}.compact").exists() and not Path(f"{db}.compact.json").exists()
    assert a["before_bytes"] > a["after_bytes"]


def test_swap_keeps_mode(tmp_path):
    db = _big_db(tmp_path)
    os.chmod(db, 0o640)
    storage_compact.prepare(db, margin_bytes=0)
    assert storage_compact.swap(db, proc_root=_empty_proc(tmp_path))["refused"] is False
    st = db.stat()
    assert (st.st_mode & 0o777) == 0o640 and st.st_uid == os.getuid()


def test_holders_maps_pids_to_units(tmp_path):
    db = _big_db(tmp_path)
    proc = _fake_proc(tmp_path, 11, str(db), "0::/system.slice/metrology@a.service\n")
    _fake_proc(tmp_path, 12, f"{db}-shm", "0::/user.slice/session-3.scope\n")
    _fake_proc(tmp_path, 13, str(tmp_path / "other.db"), "0::/system.slice/x.service\n")
    assert storage_compact.holders(db, proc_root=proc) == [(11, "metrology@a.service"), (12, None)]


def test_estimate_creates_no_file_and_matches_prepare(tmp_path, monkeypatch):
    for wal_sidecars in (False, True):
        root = tmp_path / str(wal_sidecars)
        root.mkdir()
        db = _big_db(root)
        if wal_sidecars:  # a writer is open: -wal and -shm exist
            keep = sqlite3.connect(db)
            keep.execute("INSERT INTO L2_tick_phase (v) VALUES ('k')")
            keep.commit()
        before = _files(db)
        e = storage_compact.estimate(db, margin_bytes=0)
        assert _files(db) == before
        if wal_sidecars:
            keep.close()
        assert set(e) >= {"live_bytes", "expected_bytes", "free_bytes", "would_refuse", "reason"}
        assert e["would_refuse"] is False
    live = e["live_bytes"]
    for free, refuse in ((int(live * 1.2) + 1, False), (int(live * 1.2) - 1, True)):
        monkeypatch.setattr(storage_compact.shutil, "disk_usage",
                            lambda p, f=free: type("U", (), {"free": f})())
        e = storage_compact.estimate(db, margin_bytes=0)
        p = storage_compact.prepare(db, margin_bytes=0)
        assert e["would_refuse"] is refuse and p["refused"] is refuse
        assert e["reason"] == p["reason"]
        Path(f"{db}.compact").unlink(missing_ok=True)
        Path(f"{db}.compact.json").unlink(missing_ok=True)


def test_every_verb_drops_to_the_owner_first(tmp_path, monkeypatch):
    db = _big_db(tmp_path)
    kit = storage_items.load_kit()
    calls = []
    monkeypatch.setattr(kit, "drop_to_owner", lambda path: calls.append(("drop", path)))
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **k: (calls.append(("connect", a[0])), real(*a, **k))[1])
    storage_compact.estimate(db, margin_bytes=0)
    assert calls[0] == ("drop", str(db))
    calls.clear()
    storage_compact.prepare(db, margin_bytes=0)
    assert calls[0] == ("drop", str(db))
    calls.clear()
    storage_compact.swap(db, proc_root=_empty_proc(tmp_path))
    assert calls[0] == ("drop", str(db))
    assert [c for c in calls if c[0] == "connect"]


def test_compact_verb_through_the_cli(tmp_path):
    _big_db(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tmp_path}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg), PYTHONPATH=_SIG)

    def run(*argv):
        r = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "compact",
                            *argv, "--target", "timestd", "--json"],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    assert run("--holders")["holders"] is not None
    assert "would_refuse" in run("--estimate")
    assert run("--prepare")["refused"] in (True, False)
    r = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "compact",
                        "--estimate", "--target", "timestd.L2_tick_phase", "--json"],
                       capture_output=True, text=True, env=env)
    assert json.loads(r.stdout)["refused"] is True


def test_compact_target_comes_from_its_own_map():
    assert storage_retention.COMPACT_TARGETS["timestd"] == "phase2/timestd.db"
    assert "timestd" not in storage_retention.TARGETS


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_estimate_opens_read_only_in_a_directory_it_cannot_write(tmp_path):
    db = _big_db(tmp_path)
    os.chmod(db, 0o444)
    os.chmod(db.parent, 0o555)
    try:
        e = storage_compact.estimate(db, margin_bytes=0)
    finally:
        os.chmod(db.parent, 0o755)
    assert e["live_bytes"] > 0 and "reason" in e
