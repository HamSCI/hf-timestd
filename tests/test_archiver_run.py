"""archiver.run / make_room / gate (design 2026-11-12, part 2)."""
import fcntl
import json
import os
from datetime import date
from pathlib import Path

import pytest

from hf_timestd import archiver as A
from tests.test_archiver import _noise, _write_chunk

UUID = "1111-2222"
TODAY = date(2026, 11, 13)


def _toml(tmp_path, mount, **kw):
    p = tmp_path / "archive.toml"
    lines = [f'uuid = "{UUID}"', f'mount = "{mount}"', 'archive_dtype = "f16"',
             f'high_water_pct = {kw.get("hw", 90)}']
    if "evict_first" in kw:
        lines.append("evict_first = [" + ", ".join(f'"{e}"' for e in kw["evict_first"]) + "]")
    p.write_text("\n".join(lines) + "\n")
    return p


class Usage:
    """A fake drive: percent full = 100 * bytes under root / capacity."""
    def __init__(self, root: Path, capacity: int):
        self.root, self.capacity = root, capacity

    def __call__(self, mount):
        used = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        return used, self.capacity, 100.0 * used / self.capacity


def _day(root: Path, rel: str, nbytes: int):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.bin.zst").write_bytes(b"\0" * nbytes)
    return d


def _env(tmp_path, monkeypatch, mounted=True, uuid_ok=True):
    mount = tmp_path / "mnt"
    mount.mkdir()
    monkeypatch.setattr(A, "_is_mount", lambda p: mounted)
    monkeypatch.setattr(A, "_uuid_matches", lambda p, u: uuid_ok)
    monkeypatch.setattr(A, "_station", lambda: "TESTHOST")
    return mount


def _run(tmp_path, cfg, **kw):
    return A.run(config_path=cfg, data_root=tmp_path / "ssd",
                 status_path=tmp_path / "status.json", lock_path=tmp_path / "lock",
                 today=kw.pop("today", TODAY), **kw)


def test_gate_no_toml(tmp_path):
    assert A.gate(None) == "no archive drive configured"
    st = _run(tmp_path, tmp_path / "missing.toml")
    assert st["state"] == "unconfigured"


def test_gate_not_a_mount(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch, mounted=False)
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "C" / "20261112", 1762905600, _noise())
    st = _run(tmp_path, _toml(tmp_path, mount))
    assert st["state"] == "absent" and st["present"] is False
    assert list(mount.iterdir()) == []


def test_gate_uuid_mismatch(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch, uuid_ok=False)
    st = _run(tmp_path, _toml(tmp_path, mount))
    assert st["state"] == "absent"
    assert "UUID" in st["last_error"]


def test_station_json_written_and_copies(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_usage", Usage(mount, 10**9))
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "WWV_10000" / "20261112", 1762905600, _noise())
    st = _run(tmp_path, _toml(tmp_path, mount))
    root = mount / "timestd" / "TESTHOST"
    sj = json.loads((root / "station.json").read_text())
    assert sj["station"] == "TESTHOST" and sj["channels"] == ["WWV_10000"]
    assert (root / "raw_buffer" / "WWV_10000" / "20261112" / "1762905600.json").exists()
    assert st["state"] == "ok" and st["copied"] == 1 and st["behind"] == 0


def test_make_room_oldest_day_across_channels(tmp_path):
    root = tmp_path / "mnt" / "timestd" / "H"
    for ch in ("A", "B"):
        for d in ("20261101", "20261102", "20261103"):
            _day(root / "raw_buffer", f"{ch}/{d}", 100)
    usage = Usage(tmp_path / "mnt", 600)        # 600/600 = 100 %
    res = A.make_room(root, tmp_path / "mnt", 70, [], TODAY, usage=usage)
    assert res["evicted"] == ["20261101"]       # both channels' oldest day, one step
    assert not (root / "raw_buffer" / "A" / "20261101").exists()
    assert not (root / "raw_buffer" / "B" / "20261101").exists()
    assert (root / "raw_buffer" / "A" / "20261102").exists()
    assert res["full"] is False


def test_evict_first_before_own(tmp_path):
    mnt = tmp_path / "mnt"
    wav = mnt / "wav-archive"
    _day(wav, "20251001", 100)
    _day(wav, "20251002", 100)
    (wav / "notes").mkdir(parents=True)
    root = mnt / "timestd" / "H"
    _day(root / "raw_buffer", "A/20261101", 100)
    usage = Usage(mnt, 300)
    res = A.make_room(root, mnt, 50, [wav], TODAY, usage=usage)
    assert res["evicted"] == [str(wav / "20251001"), str(wav / "20251002")]
    assert (root / "raw_buffer" / "A" / "20261101").exists()
    assert (wav / "notes").exists()


def test_evict_first_outside_mount_refused(tmp_path):
    mnt = tmp_path / "mnt"
    other = tmp_path / "elsewhere"
    _day(other, "20250101", 100)
    root = mnt / "timestd" / "H"
    _day(root / "raw_buffer", "A/20261101", 100)
    res = A.make_room(root, mnt, 1, [other], TODAY, usage=Usage(mnt, 100))
    assert (other / "20250101").exists()


def test_never_today_or_yesterday(tmp_path):
    root = tmp_path / "mnt" / "timestd" / "H"
    _day(root / "raw_buffer", "A/20261112", 100)
    _day(root / "raw_buffer", "A/20261113", 100)
    res = A.make_room(root, tmp_path / "mnt", 10, [], TODAY, usage=Usage(tmp_path / "mnt", 200))
    assert res["evicted"] == [] and res["full"] is True
    assert (root / "raw_buffer" / "A" / "20261112").exists()


def test_full_with_only_protected_days(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    root = mount / "timestd" / "TESTHOST"
    _day(root / "raw_buffer", "A/20261113", 1000)
    monkeypatch.setattr(A, "_usage", Usage(mount, 1000))
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261112", 1762905600, _noise())
    st = _run(tmp_path, _toml(tmp_path, mount))
    assert st["state"] == "full"
    assert st["copied"] == 0 and st["behind"] == 1


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    root = mount / "timestd" / "TESTHOST"
    _day(root / "raw_buffer", "A/20261101", 1000)
    monkeypatch.setattr(A, "_usage", Usage(mount, 1000))
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261112", 1762905600, _noise())
    st = _run(tmp_path, _toml(tmp_path, mount), dry_run=True)
    assert (root / "raw_buffer" / "A" / "20261101").exists()
    assert not (root / "raw_buffer" / "A" / "20261112").exists()
    assert st["dry_run"] is True and st["would_evict"] == ["20261101"]


def test_flock_second_run_exits(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    lock = tmp_path / "lock"
    fh = open(lock, "w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        st = _run(tmp_path, _toml(tmp_path, mount))
        assert st["state"] == "busy"
    finally:
        fh.close()


def test_status_json_shape(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_usage", Usage(mount, 10**9))
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261110", 1762732800, _noise())
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261111", 1762819200, _noise())
    _run(tmp_path, _toml(tmp_path, mount))
    st = json.loads((tmp_path / "status.json").read_text())
    for k in ("run_utc", "present", "state", "channels", "copied", "bytes_copied", "behind",
              "kept_f32", "failures", "last_error", "bytes_per_day", "evicted"):
        assert k in st, k
    ch = st["channels"]["A"]
    assert (ch["oldest"], ch["newest"], ch["days"]) == ("20261110", "20261111", 2)
    assert ch["bytes"] > 0 and ch["dtype"] == "complex-f16"
    assert st["bytes_per_day"] > 0


def test_three_failures_counted(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_usage", Usage(mount, 10**9))
    _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261112", 1762905600, _noise())

    def bad(*a, **k):
        raise A.VerifyError("mismatch")
    monkeypatch.setattr(A, "copy_one", bad)
    cfg = _toml(tmp_path, mount)
    for _ in range(4):
        st = _run(tmp_path, cfg)
    assert st["failures"] == 1


def test_drive_error_ends_run(tmp_path, monkeypatch):
    mount = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_usage", Usage(mount, 10**9))
    for s in (1762905600, 1762905660):
        _write_chunk(tmp_path / "ssd" / "raw_buffer" / "A" / "20261112", s, _noise())
    calls = []

    def gone(*a, **k):
        calls.append(1)
        raise A.ArchiveIOError("OSError: No such device")
    monkeypatch.setattr(A, "copy_one", gone)
    st = _run(tmp_path, _toml(tmp_path, mount))
    assert st["state"] == "error" and "No such device" in st["last_error"]
    assert len(calls) == 1


def test_lock_dir_unwritable_is_error_not_busy(tmp_path):
    st = A.run(config_path=tmp_path / "none.toml", data_root=tmp_path,
               status_path=tmp_path / "s.json", lock_path=Path("/proc/nope/lock"))
    assert st["state"] == "error" and "lock" in st["last_error"]


# ---- final-review fixes

def test_rmtree_permission_skips_candidate(tmp_path, monkeypatch):
    root = tmp_path / "mnt" / "timestd" / "H"
    _day(root / "raw_buffer", "A/20261101", 100)
    _day(root / "raw_buffer", "A/20261102", 100)
    real = A.shutil.rmtree

    def rm(p, *a, **k):
        if p.name == "20261101":
            raise PermissionError(13, "Permission denied")
        return real(p, *a, **k)
    monkeypatch.setattr(A.shutil, "rmtree", rm)
    res = A.make_room(root, tmp_path / "mnt", 60, [], TODAY, usage=Usage(tmp_path / "mnt", 200))
    assert res["evicted"] == ["20261102"] and res["skipped"] == ["20261101"]


def test_root_refused_unless_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(A.os, "geteuid", lambda: 0)
    st = A.run(config_path=tmp_path / "none.toml", data_root=tmp_path,
               status_path=tmp_path / "s.json", lock_path=tmp_path / "lock")
    assert st["state"] == "error" and "root" in st["last_error"]


def test_invalid_and_orphans_counted(tmp_path, monkeypatch):
    import time as _t
    mount = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "_usage", Usage(mount, 10**9))
    d = tmp_path / "ssd" / "raw_buffer" / "A" / "20261112"
    b, j = _write_chunk(d, 1762905600, _noise())
    j.write_text("{bad")
    old = _t.time() - 600
    os.utime(j, (old, old))
    orphan = d / "1762905660.bin.zst"
    orphan.write_bytes(b"x")
    os.utime(orphan, (_t.time() - 2 * 86400,) * 2)
    st = _run(tmp_path, _toml(tmp_path, mount))
    assert st["invalid"] == 1 and st["orphans"] == 1 and st["copied"] == 0
