"""The archive writer's disk check refuses a flush; it never deletes raw."""
import os
import shutil
import time
from collections import namedtuple

from hf_timestd.core.binary_archive_writer import BinaryArchiveConfig, BinaryArchiveWriter

U = namedtuple("U", "total used free")


def _writer(tmp_path):
    cfg = BinaryArchiveConfig(
        channel_name="testchan", frequency_hz=10e6, sample_rate=100,
        output_dir=tmp_path / "raw_buffer", use_tiered_storage=False,
        compression="none")
    w = BinaryArchiveWriter(cfg)
    w._flush_stop.set()
    w._flush_queue.put(None)
    w._flush_thread.join(timeout=5)
    return w


def test_disk_check_never_deletes_raw(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    old = w.archive_dir / "20200101" / "1577836800.bin.zst"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 10)
    (old.parent / "1577836800.json").write_text("{}")
    t = time.time() - 5 * 86400
    os.utime(old, (t, t))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(100, 95, 10**12))  # 95 % full, room left
    assert w._check_disk_space(w.archive_dir, 1000) is True
    assert old.exists() and (old.parent / "1577836800.json").exists()


def test_disk_check_refuses_when_free_space_short(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    old = w.archive_dir / "20200101" / "1577836800.bin.zst"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x" * 10)
    t = time.time() - 5 * 86400
    os.utime(old, (t, t))
    monkeypatch.setattr(shutil, "disk_usage", lambda p: U(10**12, 10**12 - 50 * 2**20, 50 * 2**20))
    assert w._check_disk_space(w.archive_dir, 1000) is False
    assert old.exists()  # refusing must not free space by deleting
