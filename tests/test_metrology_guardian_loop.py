"""Task 11 fix round 1: the metrology SERVICE loop must live through the
guardian's grace window, so the emergency eviction can run (an unmanaged
station at the hard stop used to lose all processes and free nothing)."""
from unittest.mock import patch

import pytest

from hf_timestd.core import metrology_service as ms
from hf_timestd.core import resource_guardian as rg
from hf_timestd.core.resource_guardian import ResourceGuardian, GB

HERE_DAY = '20260101'


class Clock:
    def __init__(self): self.t = 100000.0
    def __call__(self): return self.t


class FakeReader:
    def __init__(self): self.cursor_reads = 0
    def write_cursor(self):
        self.cursor_reads += 1
        return 0                       # nothing to process: the loop idles
    def close(self): pass


def _service(monkeypatch, guardian, clk, max_sleeps):
    svc = object.__new__(ms.MetrologyService)
    svc.running = True
    svc.channel_name = 'T'
    svc._resource_guardian = guardian
    svc._ring_reader = None
    reader = FakeReader()
    monkeypatch.setattr(svc, '_attach_ring', lambda: reader)
    sleeps = []

    def fake_sleep(dt):
        sleeps.append(dt)
        clk.t += dt
        if len(sleeps) >= max_sleeps:
            svc.running = False
    monkeypatch.setattr(ms.time, 'sleep', fake_sleep)
    monkeypatch.setattr(ms.time, 'monotonic', clk)
    return svc, reader, sleeps


def _disk(root, fill, total):
    import pathlib

    def f(_p):
        present = sum(q.stat().st_size for q in pathlib.Path(root).rglob('*') if q.is_file())
        used = fill + present
        return type('', (), {'total': total, 'used': used, 'free': total - used})()
    return f


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, 'EVICT_LOCK', tmp_path / 'run' / 'l.lock')
    monkeypatch.setattr(rg, 'ledger_manages_us', lambda: False)


def _data(tmp_path):
    d = tmp_path / 'raw_buffer' / 'CH1' / HERE_DAY
    d.mkdir(parents=True)
    (d / f'{HERE_DAY}T000000.bin.zst').write_bytes(b'\0' * 10000)
    p = tmp_path / 'phase2' / 'CH1' / 'L1'
    p.mkdir(parents=True)
    (p / f'L1_{HERE_DAY}.h5').write_bytes(b'\0' * 5000)


def test_stop_then_grace_then_eviction_then_resume(tmp_path, monkeypatch):
    clk = Clock()
    _data(tmp_path)
    g = ResourceGuardian(data_root=str(tmp_path), n_channels=1, clock=clk,
                         evict_grace_sec=600, evict_low_water_percent=90.0)
    svc, reader, sleeps = _service(monkeypatch, g, clk, max_sleeps=900)
    fake = _disk(tmp_path, fill=24000, total=40000)           # 97.5 % at the start
    with patch('shutil.disk_usage', side_effect=fake):
        svc._run_ringbuffer_mode()
    # The loop did not exit on the first STOP: it ran the whole 900 s.
    assert len(sleeps) == 900
    assert not (tmp_path / 'phase2' / 'CH1' / 'L1' / f'L1_{HERE_DAY}.h5').exists()   # evicted
    # ... and it resumed: after eviction the loop reads the ring again.
    assert svc._guardian_paused is False
    assert reader.cursor_reads > 0


def test_it_pauses_while_stopped_and_reads_no_ring(tmp_path, monkeypatch):
    clk = Clock()
    _data(tmp_path)
    g = ResourceGuardian(data_root=str(tmp_path), n_channels=1, clock=clk,
                         evict_grace_sec=600)
    svc, reader, sleeps = _service(monkeypatch, g, clk, max_sleeps=100)
    with patch('shutil.disk_usage', side_effect=_disk(tmp_path, 24000, 40000)):
        svc._run_ringbuffer_mode()
    assert svc._guardian_paused is True
    assert reader.cursor_reads == 0
    assert len(sleeps) == 100                                  # alive, not exited


def test_eviction_that_fails_to_free_space_exits_on_emergency(tmp_path, monkeypatch):
    clk = Clock()
    _data(tmp_path)
    g = ResourceGuardian(data_root=str(tmp_path), n_channels=1, clock=clk,
                         evict_grace_sec=600, evict_low_water_percent=90.0)
    svc, reader, sleeps = _service(monkeypatch, g, clk, max_sleeps=5000)
    # something else fills the disk: eviction frees nothing that counts
    du = type('', (), {'total': 500 * GB, 'used': 490 * GB, 'free': 10 * GB})()
    with patch('shutil.disk_usage', return_value=du):
        svc._run_ringbuffer_mode()
    assert svc.running is False
    assert 600 <= len(sleeps) < 5000          # exited after the grace, not at once, not never


def test_a_preflight_at_the_hard_stop_starts_a_paused_service(tmp_path, monkeypatch):
    clk = Clock()
    g = ResourceGuardian(data_root=str(tmp_path), n_channels=1, clock=clk)
    du = type('', (), {'total': 500 * GB, 'used': 480 * GB, 'free': 20 * GB})()
    svc, reader, sleeps = _service(monkeypatch, g, clk, max_sleeps=3)
    with patch('shutil.disk_usage', return_value=du):
        assert g.preflight_check() is True
        svc._run_ringbuffer_mode()
    assert svc._guardian_paused is True and reader.cursor_reads == 0


def test_resume_logs_the_unmeasured_minutes_at_warning(tmp_path, monkeypatch, caplog):
    import logging
    clk = Clock()
    _data(tmp_path)
    g = ResourceGuardian(data_root=str(tmp_path), n_channels=1, clock=clk,
                         evict_grace_sec=600, evict_low_water_percent=90.0)
    svc, reader, sleeps = _service(monkeypatch, g, clk, max_sleeps=900)
    monkeypatch.setattr(ms.time, 'time', lambda: 1_700_000_000.0 + clk.t - 100000.0)
    with patch('shutil.disk_usage', side_effect=_disk(tmp_path, 24000, 40000)), \
            caplog.at_level(logging.WARNING):
        svc._run_ringbuffer_mode()
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('UNMEASURED' in m and '[T]' in m and '2023-11-14' in m for m in msgs), msgs
