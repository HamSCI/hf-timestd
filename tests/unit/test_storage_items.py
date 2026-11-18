"""Storage classes and verbs (contract v0.9 17.7)."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/home/mjh/hamsci/repos/sigmond/lib")

from hf_timestd import archiver, storage_items as si  # noqa: E402

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
        _mk(r, f"{n}/f.dat")
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
    assert got["upload"] == "sent"
    for n in ("phase2", "products", "timing", "t3_shadow", "wwvb", "archive-status.json", "mystery"):
        assert got[n] == "derived"


def test_sinks_shape(tmp_path):
    s = si.sinks(tmp_path, 1000)
    assert len(s) == 3 and all(x["kind"] == "file" and x["classes"] for x in s)
    pinned, raw, rest = s
    assert pinned["classes"] == ["pinned"] and "final_policy" not in pinned
    assert raw["target"] == str(tmp_path / "raw_buffer")
    assert raw["final_policy"] == "give-up-oldest:raw" and raw["floor_bytes"] == 2000
    assert rest["target"] == str(tmp_path) and set(rest["classes"]) == {"derived", "sent"}


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


def test_delete_old_day(tmp_path):
    d = _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst").parent
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is True and not d.exists()


def test_delete_respects_archive_gate(tmp_path, monkeypatch):
    d = _mk(tmp_path, f"raw_buffer/C/{_day(4)}/a.bin.zst").parent
    monkeypatch.setattr(archiver, "raw_day_may_leave", lambda *a, **k: False)
    item = [i for i in si.list_items(tmp_path) if i.cls == "raw"][0]
    assert si.delete_item(item, NOW) is False and d.exists()


def test_pinned_never_deleted_or_chosen(tree):
    kit = si.load_kit()
    items = si.list_items(tree)
    chosen = kit.choose_trim(items, 0, 0, final=True)
    assert all(i.cls != "pinned" for i in chosen)
    for i in items:
        if i.cls == "pinned":
            assert si.delete_item(i, NOW) is False
            assert os.path.exists(i.path)


def test_cli_holdings_and_unsupported(tree, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[recorder]\nproduction_data_root = "{tree}"\n')
    env = dict(os.environ, TIMESTD_CONFIG=str(cfg),
               PYTHONPATH="/home/mjh/hamsci/repos/sigmond/lib")
    out = subprocess.run([sys.executable, "-m", "hf_timestd.cli", "storage", "holdings", "--json"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    j = json.loads(out.stdout)
    assert j["bytes_by_class"]["raw"] == 310
