"""timestd-radiod-monitor reads finished chunks from the on-disk raw_buffer (v3.96).

Since v3.96 /dev/shm/timestd/raw_buffer holds only the open chunk's .scratch
file, so a lookup there reports "no data" on a healthy station."""
import importlib.util
import os
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "monitor_radiod_health.py"


def _load():
    spec = importlib.util.spec_from_file_location("monitor_radiod_health_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _put(root, name, age_s):
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    d = root / "WWV_10000" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x")
    t = time.time() - age_s
    os.utime(f, (t, t))


def test_a_fresh_chunk_on_disk_means_data_flows(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "RAW_BUFFER_DIR", tmp_path / "raw_buffer")
    _put(tmp_path / "raw_buffer", "1700000000.bin.zst", 30)
    r = mod.check_data_flow(tmp_path / "state.json")
    assert r["flowing"] is True


def test_scratch_and_temp_files_do_not_count(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "RAW_BUFFER_DIR", tmp_path / "raw_buffer")
    _put(tmp_path / "raw_buffer", "1700000000.bin.zst", 400)
    for n in ("1700000600.WWV.0.scratch", "1700000600.bin.zst.tmp", "1700000600.bin.zst.drain"):
        _put(tmp_path / "raw_buffer", n, 1)
    r = mod.check_data_flow(tmp_path / "state.json")
    assert r["flowing"] is False
    assert r["latest_file"] == "1700000000.bin.zst"
