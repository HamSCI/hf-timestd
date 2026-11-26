"""The recorder drains finished chunks out of RAM once, at start (v3.96)."""
import json
import os
import shutil
import time

from hf_timestd.core import tiered_storage
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.tiered_storage import DrainResult, drain_hot_buffer


def _chunk(day, stem):
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{stem}.bin.zst").write_bytes(b"x")
    (day / f"{stem}.json").write_text(json.dumps({"stem": stem}))


def test_drain_moves_every_chunk_regardless_of_age(tmp_path):
    hot, cold = tmp_path / "shm" / "raw_buffer", tmp_path / "disk" / "raw_buffer"
    day = hot / "WWV_10000" / "20261125"
    now = int(time.time()) // 600 * 600  # a chunk that ended a moment ago
    for stem in (str(now), f"{now}.seg2"):
        _chunk(day, stem)
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (2, 0)
    for stem in (str(now), f"{now}.seg2"):
        assert (cold / "WWV_10000" / "20261125" / f"{stem}.bin.zst").exists()
        assert (cold / "WWV_10000" / "20261125" / f"{stem}.json").exists()
    assert list(day.iterdir()) == []


def test_drain_moves_a_sidecar_with_no_bin(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    day = hot / "chan" / "20261111"
    day.mkdir(parents=True)
    (day / "100.json").write_text("{}")
    assert drain_hot_buffer(hot, cold).moved == 1
    assert (cold / "chan" / "20261111" / "100.json").exists()


def test_drain_moves_the_sidecar_before_the_bin(tmp_path, monkeypatch):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    _chunk(hot / "chan" / "20261111", "100")
    order = []
    real_move = shutil.move
    monkeypatch.setattr(shutil, "move",
                        lambda s, d: (order.append(os.path.basename(s)), real_move(s, d))[1])
    drain_hot_buffer(hot, cold)
    assert order == ["100.json", "100.bin.zst"]
    assert (cold / "chan" / "20261111" / "100.bin.zst").exists()


def test_drain_never_replaces_a_file_on_disk(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    hot_day, cold_day = hot / "chan" / "20261111", cold / "chan" / "20261111"
    hot_day.mkdir(parents=True)
    cold_day.mkdir(parents=True)
    (hot_day / "100.bin.zst").write_bytes(b"ram")
    (cold_day / "100.bin.zst").write_bytes(b"disk")
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (0, 1)
    assert (cold_day / "100.bin.zst").read_bytes() == b"disk"
    assert (hot_day / "100.bin.zst").read_bytes() == b"ram"


def test_drain_refuses_a_race_on_the_final_move(tmp_path, monkeypatch):
    """A writer that creates the name after the check keeps its file."""
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    hot_day, cold_day = hot / "chan" / "20261111", cold / "chan" / "20261111"
    hot_day.mkdir(parents=True)
    (hot_day / "100.bin.zst").write_bytes(b"ram")
    real_move = shutil.move

    def racing(s, d):
        real_move(s, d)
        if str(d).endswith(".drain"):
            (cold_day / "100.bin.zst").write_bytes(b"disk")
    monkeypatch.setattr(shutil, "move", racing)
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (0, 1)
    assert (cold_day / "100.bin.zst").read_bytes() == b"disk"
    assert (hot_day / "100.bin.zst").read_bytes() == b"ram"


def test_drain_leaves_scratch_and_tmp(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    hot_day = hot / "chan" / "20261111"
    hot_day.mkdir(parents=True)
    (hot_day / "100.testchan.1.scratch").write_bytes(b"s")
    (hot_day / "100.bin.zst.tmp").write_bytes(b"t")
    assert drain_hot_buffer(hot, cold).moved == 0
    assert (hot_day / "100.testchan.1.scratch").exists()
    assert (hot_day / "100.bin.zst.tmp").exists()


def test_drain_of_a_missing_hot_root_is_a_no_op(tmp_path):
    assert drain_hot_buffer(tmp_path / "none", tmp_path / "cold") == DrainResult(0, 0)


def test_run_drains_before_it_opens_channels(monkeypatch, tmp_path):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.recorder_config = {"tiered_storage": True, "hot_buffer_root": str(tmp_path / "shm")}
    cr.output_dir = tmp_path / "disk"
    calls = []
    monkeypatch.setattr(tiered_storage, "drain_hot_buffer",
                        lambda h, c: calls.append(("drain", h, c)) or DrainResult(0, 0))
    monkeypatch.setattr(cr, "_initialize_channels", lambda: calls.append(("init",)) or False)
    cr.run()
    assert calls[0] == ("drain", tmp_path / "shm" / "raw_buffer", tmp_path / "disk" / "raw_buffer")
    assert calls[1] == ("init",)


def test_run_does_not_drain_when_tiering_is_off(monkeypatch, tmp_path):
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.recorder_config = {"tiered_storage": False}
    cr.output_dir = tmp_path
    calls = []
    monkeypatch.setattr(tiered_storage, "drain_hot_buffer",
                        lambda h, c: calls.append("drain") or DrainResult(0, 0))
    monkeypatch.setattr(cr, "_initialize_channels", lambda: False)
    cr.run()
    assert calls == []


def test_drain_keeps_the_sidecar_when_the_disk_holds_only_the_bin(tmp_path):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    hot_day, cold_day = hot / "chan" / "20261111", cold / "chan" / "20261111"
    _chunk(hot_day, "100")
    cold_day.mkdir(parents=True)
    (cold_day / "100.bin.zst").write_bytes(b"disk")
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (0, 1)
    assert (hot_day / "100.json").exists()
    assert (hot_day / "100.bin.zst").read_bytes() == b"x"
    assert not (cold_day / "100.json").exists()
    assert (cold_day / "100.bin.zst").read_bytes() == b"disk"


def test_failed_drain_logs_error_naming_root_and_count(monkeypatch, tmp_path, caplog):
    import logging
    hot = tmp_path / "shm"
    _chunk(hot / "raw_buffer" / "chan" / "20261111", "100")
    _chunk(hot / "raw_buffer" / "chan" / "20261111", "700")
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.recorder_config = {"tiered_storage": True, "hot_buffer_root": str(hot)}
    cr.output_dir = tmp_path / "disk"

    def boom(h, c):
        raise OSError("disk gone")
    monkeypatch.setattr(tiered_storage, "drain_hot_buffer", boom)
    with caplog.at_level(logging.ERROR):
        cr._drain_hot_buffer()
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errs
    msg = errs[0].getMessage()
    assert str(hot / "raw_buffer") in msg and "2 finished chunk stem" in msg


def test_drain_sweeps_stale_staging_files_it_created(tmp_path):
    """Minor B: a drain that ran out of disk mid-copy strands <name>.drain.
    The next start removes it, and only names the drain itself creates."""
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    cold_day = cold / "chan" / "20261111"
    cold_day.mkdir(parents=True)
    stale = [cold_day / "100.bin.zst.drain", cold_day / "100.json.drain",
             cold_day / "200.seg2.bin.zst.drain"]
    keep = [cold_day / "notes.drain", cold_day / "300.bin.zst", cold_day / "400.scratch"]
    for f in stale + keep:
        f.write_bytes(b"x")
    _chunk(hot / "chan" / "20261111", "500")
    r = drain_hot_buffer(hot, cold)
    assert (r.moved, r.refused) == (1, 0)
    assert not any(f.exists() for f in stale)
    assert all(f.exists() for f in keep)
    assert (cold_day / "500.bin.zst").exists()
