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
    _put(tmp_path / "raw_buffer", "1700000000.bin.zst", 1300)
    for n in ("1700000600.WWV.0.scratch", "1700000600.bin.zst.tmp", "1700000600.bin.zst.drain", "1700000600.json"):
        _put(tmp_path / "raw_buffer", n, 1)
    r = mod.check_data_flow(tmp_path / "state.json")
    assert r["flowing"] is False
    assert r["latest_file"] == "1700000000.bin.zst"


def _flow(tmp_path, monkeypatch, age, caplog):
    import logging
    mod = _load()
    monkeypatch.setattr(mod, "RAW_BUFFER_DIR", tmp_path / "raw_buffer")
    _put(tmp_path / "raw_buffer", "1700000000.bin.zst", age)
    with caplog.at_level(logging.WARNING):
        r = mod.check_data_flow(tmp_path / "state.json")
    return r, [x.getMessage() for x in caplog.records]


def test_700s_old_chunk_is_healthy_without_warning(tmp_path, monkeypatch, caplog):
    r, msgs = _flow(tmp_path, monkeypatch, 700, caplog)
    assert r["flowing"] is True
    assert msgs == []


def test_900s_old_chunk_warns_but_still_flows(tmp_path, monkeypatch, caplog):
    r, msgs = _flow(tmp_path, monkeypatch, 900, caplog)
    assert r["flowing"] is True
    assert any("SLOW" in m for m in msgs)
    assert not any("STOPPED" in m for m in msgs)


def test_1300s_old_chunk_is_stopped(tmp_path, monkeypatch, caplog):
    r, msgs = _flow(tmp_path, monkeypatch, 1300, caplog)
    assert r["flowing"] is False
    assert any("DATA FLOW STOPPED" in m for m in msgs)


def test_freshness_alert_limit_is_two_chunks():
    import re
    sh = Path(__file__).resolve().parents[2] / "scripts" / "check-freshness-alert.sh"
    m = re.search(r"^MAX_RAW_STALE_SECONDS=(\d+)", sh.read_text(), re.M)
    assert m and int(m.group(1)) >= 1200
