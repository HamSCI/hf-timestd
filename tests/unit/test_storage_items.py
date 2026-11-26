"""Storage classes and verbs (contract v0.9 17.7)."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SIG = os.environ.get("SIGMOND_LIB_FOR_TESTS") or str(
    Path(__file__).resolve().parents[3] / "sigmond" / "lib")
if os.path.isdir(_SIG):
    sys.path.insert(0, _SIG)
from hf_timestd import archiver, storage_items as si  # noqa: E402

needs_kit = pytest.mark.skipif(si.load_kit() is None, reason="sigmond client kit absent")

NOW = time.time()


def _day(offset):
    return (datetime.fromtimestamp(NOW, tz=timezone.utc) - timedelta(days=offset)).strftime("%Y%m%d")


def _mk(root, rel, size=100, age_days=0.0):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = NOW - age_days * 86400
    os.utime(p, (t, t))
    return p


@pytest.fixture
def tree(tmp_path):
    r = tmp_path
    _mk(r, "state/a.json")
    _mk(r, "authority_history.db")
    _mk(r, "authority_history.db-wal")
    _mk(r, "status/s.json")
    _mk(r, "upload/u.tar")
    for n in ("phase2", "products", "timing", "t3_shadow", "wwvb", "mystery"):
        _mk(r, f"{n}/f.dat", age_days=1)
    _mk(r, "archive-status.json")
    _mk(r, f"raw_buffer/WWV_10000/{_day(5)}/a.bin.zst", 300, 5)
    _mk(r, f"raw_buffer/WWV_10000/{_day(5)}/a.json", 10, 4.9)
    return r


def test_classes_from_tree(tree):
    got = {}
    for e in tree.iterdir():
        got[e.name] = si.class_of(e.name)
    assert got["state"] == got["status"] == got["authority_history.db"] == got["authority_history.db-wal"] == "pinned"
    assert got["raw_buffer"] == "raw"
    assert got["upload"] == "foreign"
    for n in ("phase2", "products", "timing", "t3_shadow", "wwvb", "archive-status.json", "mystery"):
        assert got[n] == "derived"


def test_sinks_shape(tmp_path):
    s = si.sinks(tmp_path, 1000)
    files = [x for x in s if x["kind"] == "file"]
    assert len(files) == 3 and all(x["classes"] for x in files)
    assert all(x["kind"] == "service" and x["expires"] for x in s if x not in files)
    pinned, raw, rest = files
    assert pinned["classes"] == ["pinned"] and "final_policy" not in pinned
    assert raw["target"] == str(tmp_path / "raw_buffer")
    assert raw["classes"] == ["raw", "sent"]
    assert raw["final_policy"] == "give-up-oldest:raw" and raw["floor_bytes"] == 2000
    assert rest["target"] == str(tmp_path) and set(rest["classes"]) == {"derived", "pinned"}


def test_raw_items_are_day_dirs(tree):
    raw = [i for i in si.list_items(tree) if i.cls == "raw"]
    assert len(raw) == 1
    assert raw[0].path.endswith(_day(5)) and raw[0].bytes == 310
    assert abs(raw[0].mtime - (NOW - 4.9 * 86400)) < 2


@pytest.mark.parametrize("off", [0, 1])
def test_delete_never_today_or_yesterday(tmp_path, off):
    d = _mk(tmp_path, f"raw_buffer/C/{_day(off)}/a.bin.zst").parent
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is False and d.exists()


def test_delete_old_day(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    d = _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst").parent
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is True and not d.exists()


def test_delete_respects_archive_gate(tmp_path, monkeypatch):
    d = _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst").parent
    monkeypatch.setattr(archiver, "raw_day_may_leave", lambda *a, **k: False)
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is False and d.exists()


@needs_kit
def test_pinned_never_deleted_or_chosen(tree):
    kit = si.load_kit()
    items = si.list_items(tree)
    chosen = kit.choose_trim(items, 0, 0, final=True)
    assert all(i.cls != "pinned" for i in chosen)
    for i in items:
        if i.cls == "pinned":
            assert si.delete_item(i, NOW) is False
            assert os.path.exists(i.path)


@needs_kit
def test_cli_holdings_and_unsupported(tree, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tree}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg),
               PYTHONPATH=_SIG)
    out = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "holdings", "--json"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    j = json.loads(out.stdout)
    assert j["bytes_by_class"]["raw"] == 310


def test_sqlite_files_listed_pinned_never_evicted(tmp_path):
    for n in ("timestd.db", "timestd.db-wal", "timestd.db-shm", "x.db-journal", "y.sqlite3"):
        _mk(tmp_path, f"phase2/{n}", age_days=1)
    _mk(tmp_path, "phase2/keep.csv", age_days=1)
    _mk(tmp_path, "upload/z.db")
    got = {Path(i.path).name: i.cls for i in si.list_items(tmp_path)}
    assert got == {"timestd.db": "pinned", "timestd.db-wal": "pinned",
                   "timestd.db-shm": "pinned", "x.db-journal": "pinned",
                   "y.sqlite3": "pinned", "keep.csv": "derived"}
    for i in si.list_items(tmp_path):
        if i.cls == "pinned":
            assert si.delete_item(i, NOW) is False
    gone = si.Item(str(tmp_path / "phase2/timestd.db"), 1, "derived", 0.0)
    assert si.delete_item(gone, NOW) is False
    assert (tmp_path / "phase2/timestd.db").exists()


def test_list_items_shows_timestd_db_as_pinned(tmp_path):
    (tmp_path / "phase2").mkdir()
    _mk(tmp_path, "phase2/timestd.db", 10, age_days=1)
    items = si.list_items(tmp_path)
    assert any(i.path.endswith("phase2/timestd.db") and i.cls == "pinned" for i in items)


def test_non_date_dir_not_a_raw_day(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    d = _mk(tmp_path, "raw_buffer/C/junk/a.bin.zst").parent
    assert [i for i in si.list_items(tmp_path) if i.cls == "raw"] == []
    item = si.Item(str(d), 1, "raw", 0.0)
    assert si.delete_item(item, NOW) is False and d.exists()


def test_list_items_without_kit(tree, monkeypatch):
    monkeypatch.setattr(si, "load_kit", lambda: None)
    assert si.list_items(tree)
    assert len([s for s in si.sinks_for(tree) if s["kind"] == "file"]) == 3


def test_unrelated_command_does_not_walk_tree(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("tree walked")
    monkeypatch.setattr(si, "list_items", boom)
    monkeypatch.setattr(sys, "argv", ["hf-timestd", "version"])
    from hf_timestd import cli
    cli.main(["version"])  # would raise AssertionError if the tree were walked


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "sigmond" or name.startswith("sigmond."):
            raise ImportError("blocked for test")
        return None


@pytest.mark.parametrize("verb", [["holdings", "--json"], ["trim", "--fit-bytes", "0"]])
def test_unsupported_without_sigmond(verb, monkeypatch, capsys):
    for k in [k for k in sys.modules if k == "sigmond" or k.startswith("sigmond.")]:
        monkeypatch.delitem(sys.modules, k)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if "sigmond" not in p])
    monkeypatch.setattr(sys, "meta_path", [_Block()] + sys.meta_path)
    monkeypatch.setattr(si, "SIGMOND_LIB", "/nonexistent/lib")
    monkeypatch.setenv("TIMESTD_CONFIG", "/nonexistent/c.toml")
    monkeypatch.setattr(sys, "argv", ["hf-timestd", "storage"] + verb)
    from hf_timestd import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["storage"] + verb)
    assert e.value.code == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"unsupported": True}


def _archive_all(monkeypatch, days, tmp=None):
    """Make the archiver's status file report these days copied (channel C)."""
    from pathlib import Path as _P
    import tempfile
    d = _P(tempfile.mkdtemp())
    status = d / "archive-status.json"
    ds = sorted(days)
    _write_status(status, oldest=ds[0], newest=ds[-1])
    monkeypatch.setattr(archiver, "STATUS_PATH", status)
    return status


def _write_status(path, run=None, oldest="19000101", newest="29991231", **over):
    t = run if run is not None else NOW
    st = {"run_utc": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "state": "ok", "present": True, "behind": 0, "failures": 0, "invalid": 0,
          "orphans": 0, "channels": {"C": {"oldest": oldest, "newest": newest, "days": 7}}}
    st.update(over)
    path.write_text(json.dumps(st))


def test_events_staging_and_state_dirs_pinned(tmp_path):
    for rel in ("events-staging/state.json", "events-staging/w/win.bin",
                "phase2/WWV_10/state/k.json", "products/x/state/y/z.json"):
        _mk(tmp_path, rel, age_days=3)
    _mk(tmp_path, "phase2/WWV_10/old.csv", age_days=3)
    items = {Path(i.path).name: i.cls for i in si.list_items(tmp_path)}
    assert items["events-staging"] == "pinned"
    assert items["k.json"] == "pinned" and items["z.json"] == "pinned"
    assert items["old.csv"] == "derived"


def test_fresh_derived_file_never_a_trim_item(tmp_path):
    _mk(tmp_path, "phase2/WWV_10/open.csv")
    _mk(tmp_path, "phase2/WWV_10/old.csv", age_days=1)
    cls = {Path(i.path).name: i for i in si.list_items(tmp_path)}
    assert cls["open.csv"].cls == "pinned" and cls["old.csv"].cls == "derived"
    forged = si.Item(cls["open.csv"].path, 1, "derived", NOW)
    assert si.delete_item(forged, NOW) is False
    assert (tmp_path / "phase2/WWV_10/open.csv").exists()


@needs_kit
def test_tiny_fit_trim_spares_state_events_and_fresh(tmp_path, monkeypatch):
    cfgdir, tmp_path = tmp_path, tmp_path / "data"
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    keep = [_mk(tmp_path, "phase2/WWV_10/state/k.json", age_days=3),
            _mk(tmp_path, "events-staging/state.json", age_days=3),
            _mk(tmp_path, "events-staging/orders.json", age_days=3),
            _mk(tmp_path, "phase2/WWV_10/fresh.csv")]
    gone = _mk(tmp_path, "phase2/WWV_10/old.csv", age_days=3)
    day = _mk(tmp_path, f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5).parent
    cfg = cfgdir / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tmp_path}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg), PYTHONPATH=_SIG)
    out = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "trim",
                          "--fit-bytes", "1"], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert all(k.exists() for k in keep)
    assert not gone.exists()


def test_archived_raw_day_is_sent_and_others_stay_raw(tmp_path, monkeypatch):
    _archive_all(monkeypatch, {_day(5), _day(1), _day(0)})
    _mk(tmp_path, f"raw_buffer/C/{_day(5)}/a.bin.zst")
    _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst")
    _mk(tmp_path, f"raw_buffer/C/{_day(1)}/a.bin.zst")
    _mk(tmp_path, f"raw_buffer/C/{_day(0)}/a.bin.zst")
    got = {Path(i.path).name: i.cls for i in si.list_items(tmp_path, NOW)}
    assert got[_day(5)] == "sent"
    assert got[_day(4)] == "sent"  # inside the drive's range
    assert got[_day(1)] == got[_day(0)] == "raw"


@needs_kit
def test_sent_raw_day_goes_before_derived_and_gate_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    _archive_all(monkeypatch, {_day(5)})
    _mk(tmp_path, "phase2/WWV_10/old.csv", age_days=3)
    _mk(tmp_path, f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5)
    items = si.list_items(tmp_path, NOW)
    chosen = si.load_kit().choose_trim(items, 10**6, 0)
    assert chosen == []
    chosen = si.load_kit().choose_trim(items, 150, 0)
    assert [i.cls for i in chosen][0] == "sent"
    monkeypatch.setattr(archiver, "raw_day_may_leave", lambda *a, **k: False)
    assert si.delete_item(chosen[0], NOW) is False


def test_written_rate_is_file_level(tmp_path):
    # yesterday's day dir: files spread over the day; only the last 24 h counts
    ydir = f"raw_buffer/C/{_day(1)}"
    _mk(tmp_path, f"{ydir}/early.bin.zst", 1000, age_days=1.5)
    _mk(tmp_path, f"{ydir}/mid.bin.zst", 1000, age_days=1.2)
    _mk(tmp_path, f"{ydir}/late.bin.zst", 1000, age_days=0.5)
    _mk(tmp_path, f"raw_buffer/C/{_day(0)}/now.bin.zst", 1000, age_days=0.1)
    assert si.raw_written_24h(tmp_path, NOW) == 2000
    sink = si.sinks_for(tmp_path, NOW)[1]
    assert sink["floor_bytes"] == 4000
    items = si.list_items(tmp_path, NOW)
    assert si.written_per_day(tmp_path, items, NOW) == 2000


@needs_kit
def test_cli_holdings_reports_file_level_rate(tmp_path):
    cfgdir, tmp_path = tmp_path, tmp_path / "data"
    ydir = f"raw_buffer/C/{_day(1)}"
    _mk(tmp_path, f"{ydir}/early.bin.zst", 1000, age_days=1.5)
    _mk(tmp_path, f"{ydir}/late.bin.zst", 1000, age_days=0.5)
    cfg = cfgdir / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tmp_path}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg), PYTHONPATH=_SIG)
    out = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "holdings", "--json"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["bytes_written_per_day"] == 1000


def _status_case(tmp_path, monkeypatch, **kw):
    _mk(tmp_path / "data", f"raw_buffer/C/{_day(5)}/a.bin.zst")
    sp = tmp_path / "st.json"
    if kw.pop("absent", False):
        pass
    else:
        _write_status(sp, **kw)
    cls = [i.cls for i in si.list_items(tmp_path / "data", NOW, status_path=sp)]
    return cls[0]


@pytest.mark.parametrize("kw,want", [
    ({}, "sent"),
    ({"absent": True}, "raw"),
    ({"run": NOW - 7200}, "raw"),
    ({"run": NOW + 600}, "raw"),
    ({"behind": 3}, "raw"),
    ({"failures": 1}, "raw"),
    ({"invalid": 1}, "raw"),
    ({"orphans": 2}, "raw"),
    ({"state": "full"}, "raw"),
    ({"present": False}, "raw"),
    ({"channels": {"OTHER": {"oldest": "19000101", "newest": "29991231"}}}, "raw"),
    ({"oldest": _day(3)}, "raw"),
    ({"newest": _day(7)}, "raw"),
])
def test_sent_decided_by_status_file(tmp_path, monkeypatch, kw, want):
    assert _status_case(tmp_path, monkeypatch, **kw) == want


def test_garbled_status_is_raw(tmp_path):
    _mk(tmp_path, f"raw_buffer/C/{_day(5)}/a.bin.zst")
    sp = tmp_path / "bad.json"
    sp.write_text("{nope")
    assert [i.cls for i in si.list_items(tmp_path, NOW, status_path=sp)
            if "raw_buffer" in i.path] == ["raw"]


def test_listing_never_touches_archive_drive(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("archive drive touched")
    for n in ("raw_day_archived", "gate", "_is_mount", "_uuid_matches", "_load_config"):
        monkeypatch.setattr(archiver, n, boom)
    monkeypatch.setattr(os.path, "ismount", boom)
    _archive_all(monkeypatch, {_day(5)})
    _mk(tmp_path, f"raw_buffer/C/{_day(5)}/a.bin.zst")
    assert [i.cls for i in si.list_items(tmp_path, NOW) if "raw_buffer" in i.path] == ["sent"]


def test_final_takes_archived_day_before_unarchived(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    d5 = _mk(tmp_path / "data", f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5).parent
    d3 = _mk(tmp_path / "data", f"raw_buffer/C/{_day(3)}/a.bin.zst", 100, 3).parent
    sp = tmp_path / "st.json"
    _write_status(sp, oldest=_day(5), newest=_day(5))
    monkeypatch.setattr(archiver, "STATUS_PATH", sp)
    ans = si.final_raw(tmp_path / "data", 150, NOW)
    assert not d5.exists() and d3.exists()
    assert ans["held_bytes"] == 100 and ans["refused"] is False
    si.final_raw(tmp_path / "data", 0, NOW)
    assert not d3.exists()


@needs_kit
def test_cli_final_uses_sent_first(tmp_path, monkeypatch):
    cfgdir, data = tmp_path, tmp_path / "data"
    d5 = _mk(data, f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5).parent
    d3 = _mk(data, f"raw_buffer/C/{_day(3)}/a.bin.zst", 100, 3).parent
    cfg = cfgdir / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{data}"\n')
    # the child process reads the real STATUS_PATH; point it via a sitecustomize-free route
    status = cfgdir / "st.json"
    _write_status(status, oldest=_day(5), newest=_day(5))
    code = ("import sys; from hf_timestd import archiver; archiver.STATUS_PATH=__import__('pathlib').Path(%r);"
            "archiver.ARCHIVE_TOML=__import__('pathlib').Path('/nonexistent');"
            "from hf_timestd import cli; cli.main(['storage','final','--fit-bytes','150'])" % str(status))
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg), PYTHONPATH=_SIG)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert not d5.exists() and d3.exists()


def test_final_survives_a_delete_that_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    monkeypatch.setattr(archiver, "STATUS_PATH", tmp_path / "none.json")
    data = tmp_path / "data"
    d5 = _mk(data, f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5).parent
    d3 = _mk(data, f"raw_buffer/C/{_day(3)}/a.bin.zst", 100, 3).parent
    real = si.delete_item

    def flaky(item, now):
        if item.path == str(d5):
            raise PermissionError("root-owned")
        return real(item, now)
    monkeypatch.setattr(si, "delete_item", flaky)
    ans = si.final_raw(data, 0, NOW)
    assert d5.exists() and not d3.exists()
    assert str(d5) in ans["reason"] and "root-owned" in ans["reason"]
    assert ans["held_bytes"] == 100


@needs_kit
def test_final_other_policy_uses_kit_and_refusal_has_holdings(tmp_path, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    monkeypatch.setattr(archiver, "STATUS_PATH", tmp_path / "none.json")
    kit = si.load_kit()
    data = tmp_path / "data"
    old = _mk(data, "phase2/WWV_10/old.csv", 100, 3)
    raw = _mk(data, f"raw_buffer/C/{_day(5)}/a.bin.zst", 100, 5)
    ans = si.apply_final(kit, data, "give-up-oldest:derived", 0, 0, NOW)
    assert not old.exists() and raw.exists() and ans["refused"] is False
    ans = si.apply_final(kit, data, "bogus", 0, 0, NOW)
    assert ans["refused"] is True and ans["bytes_by_class"] == {"raw": 100}


# ------------------------------------------------- another client's entries

def test_another_clients_entry_is_never_listed(tree):
    """upload/ holds hamsci-physics's GRAPE spool, which hamsci-physics registers as
    its own sink.  hf-timestd listed it as `sent` and its trim deleted packaged
    days file by file (B4, 2026-11-22/23), which also re-shipped the emptied
    datasets.  Contract 17.7 design: a client never deletes another's files."""
    _mk(tree, "upload/20261120/AC0G/GRAPE@AC0G_1_1/OBS2026-11-20T00-00/ch0/rf.h5", 500, 3)
    _mk(tree, "upload/20261120/.upload_complete", 0, 3)
    listed = [i.path for i in si.list_items(tree)]
    assert not [p for p in listed if "/upload/" in p or p.endswith("/upload")]
    held = sum(i.bytes for i in si.list_items(tree))
    assert held == sum(i.bytes for i in si.list_items(tree) if "upload" not in i.path)


def test_final_raw_leaves_another_clients_entry(tree, monkeypatch):
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tree / "no-archive.toml")
    f = _mk(tree, "upload/20261120/OBS/ch0/rf.h5", 500, 3)
    si.final_raw(tree, 0, NOW)
    assert f.exists()


def test_every_deletion_reaches_the_journal(tmp_path, monkeypatch):
    """sigmond captures a trim's stdout and stderr and keeps neither, so the
    deletions of 2026-11-22/23 left no trace.  Each one now writes a journal line."""
    monkeypatch.setattr(archiver, "ARCHIVE_TOML", tmp_path / "no-archive.toml")
    lines = []
    monkeypatch.setattr(si, "_journal", lines.append)
    f = _mk(tmp_path, "products/C/old.png", 123, 5)
    d = _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst", 300, 4).parent
    for it in si.list_items(tmp_path):
        if it.path in (str(f), str(d)):
            assert si.delete_item(it, NOW)
    assert not f.exists() and not d.exists()
    assert len(lines) == 2
    assert any(str(f) in l and "123 B" in l and "derived" in l for l in lines)
    assert any(str(d) in l and "raw" in l for l in lines)


def test_a_held_back_item_writes_nothing(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(si, "_journal", lines.append)
    d = _mk(tmp_path, f"raw_buffer/C/{_day(0)}/a.bin.zst").parent
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is False and d.exists()
    assert lines == []
