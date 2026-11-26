#!/usr/bin/env python3
"""Tests for ResourceGuardian — universal, self-sizing resource management."""

import logging
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd.core.resource_guardian import (
    ResourceGuardian,
    ResourceState,
    DISK_WARN_PERCENT,
    DISK_HARD_STOP_PERCENT,
    BASELINE_DAYS,
    PHASE2_BYTES_PER_CHANNEL_PER_DAY,
    RAM_PER_CHANNEL,
    RAM_SYSTEM_HEADROOM,
    OVERHEAD_BYTES,
    GB,
    MB,
)


@pytest.fixture
def tmp_data_root(tmp_path):
    """Create a temporary data_root with realistic directory structure."""
    for dirname in ['raw_buffer', 'phase2', 'upload', 'data', 'products', 'state']:
        (tmp_path / dirname).mkdir()
    return tmp_path


@pytest.fixture
def guardian(tmp_data_root):
    """Create a ResourceGuardian with temporary paths and small channel count."""
    return ResourceGuardian(
        data_root=str(tmp_data_root),
        n_channels=1,       # small so baseline fits any test disk
        sample_rate=24000,
    )


# ======================================================================
# Baseline computation
# ======================================================================

class TestBaselineComputation:
    """Verify the auto-sizing math."""

    def test_baseline_scales_with_channels(self):
        g1 = ResourceGuardian(n_channels=1, sample_rate=24000)
        g9 = ResourceGuardian(n_channels=9, sample_rate=24000)
        # 9-channel baseline should be ~9× the 1-channel baseline (minus shared overhead)
        assert g9.baseline_bytes > g1.baseline_bytes * 8

    def test_baseline_scales_with_sample_rate(self):
        g_lo = ResourceGuardian(n_channels=9, sample_rate=12000)
        g_hi = ResourceGuardian(n_channels=9, sample_rate=24000)
        assert g_hi.baseline_bytes > g_lo.baseline_bytes

    def test_baseline_includes_overhead(self):
        g = ResourceGuardian(n_channels=1, sample_rate=24000)
        assert g.baseline_bytes > OVERHEAD_BYTES

    def test_min_ram_scales_with_channels(self):
        g1 = ResourceGuardian(n_channels=1)
        g9 = ResourceGuardian(n_channels=9)
        assert g9.min_ram_bytes == 9 * RAM_PER_CHANNEL + RAM_SYSTEM_HEADROOM
        assert g1.min_ram_bytes == 1 * RAM_PER_CHANNEL + RAM_SYSTEM_HEADROOM


# ======================================================================
# Preflight
# ======================================================================

class TestPreflight:
    """Preflight check: refuse to start if system can't sustain the load."""

    def test_preflight_passes_with_sufficient_resources(self, guardian):
        """1-channel guardian with mocked healthy disk should pass."""
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            assert guardian.preflight_check() is True

    def test_preflight_fails_if_disk_too_small_for_baseline(self, tmp_data_root):
        """If 80% of disk < baseline, preflight must fail."""
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            n_channels=9999,   # absurd — baseline exceeds any disk
            sample_rate=24000,
        )
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            assert g.preflight_check() is False

    def test_preflight_fails_if_ram_too_small(self, tmp_data_root):
        """If 80% of RAM < min_ram, preflight must fail."""
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            n_channels=99999,  # min_ram will exceed any real system
            sample_rate=24000,
        )
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            assert g.preflight_check() is False

    def test_preflight_creates_missing_dirs(self, tmp_path):
        fresh = tmp_path / 'fresh'
        fresh.mkdir()
        g = ResourceGuardian(data_root=str(fresh), n_channels=1)
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            assert g.preflight_check() is True
        assert (fresh / 'state').exists()
        assert (fresh / 'raw_buffer').exists()
        assert (fresh / 'phase2').exists()


# ======================================================================
# Watchdog
# ======================================================================

class TestWatchdog:
    """Runtime watchdog: budget-based headroom management."""

    def test_watchdog_ok_when_headroom_sufficient(self, guardian):
        """Plenty of free space → headroom OK."""
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB,
                'used': 200 * GB,   # 40%
                'free': 300 * GB,
            })()
            status = guardian.watchdog_check(force=True)
            assert status.state == ResourceState.OK

    def test_watchdog_skips_when_interval_not_elapsed(self, guardian):
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            guardian.watchdog_check(force=True)
            s2 = guardian.watchdog_check(force=False)
            assert 'skipped' in s2.message

    def test_watchdog_pauses_on_first_crossing_then_emergency_after_grace(self, guardian):
        """hf-timestd#31: a ≥95% crossing PAUSES (STOP) first — nothing to
        evict here, so only after the grace window does it escalate to
        EMERGENCY.  It must NOT go straight to EMERGENCY on the first tick
        (that is what let a 2 s spike destroy a whole day)."""
        guardian.evict_grace_sec = 600
        guardian._clock = lambda: 1000.0
        du = type('', (), {'total': 500 * GB, 'used': 480 * GB, 'free': 20 * GB})()  # 96%
        with patch('shutil.disk_usage', return_value=du):
            assert guardian.watchdog_check(force=True).state == ResourceState.STOP
            guardian._clock = lambda: 2000.0   # +1000 s, past the grace window
            assert guardian.watchdog_check(force=True).state == ResourceState.EMERGENCY


# ======================================================================
# Eviction logic
# ======================================================================

class TestEviction:
    """Oldest-day-first eviction across raw_buffer and phase2."""

    def test_evict_date_removes_raw_and_phase2(self, tmp_data_root, guardian):
        """Evicting a date removes both raw_buffer dirs and phase2 files."""
        date_str = '20260101'

        # raw_buffer/CHANNEL/YYYYMMDD/
        raw_ch = tmp_data_root / 'raw_buffer' / 'SHARED_10000' / date_str
        raw_ch.mkdir(parents=True)
        (raw_ch / 'data.bin').write_bytes(b'\x00' * 5000)

        # phase2/CHANNEL/tick_phase/SHARED_10000_tick_phase_YYYYMMDD.h5
        phase2_dir = tmp_data_root / 'phase2' / 'SHARED_10000' / 'tick_phase'
        phase2_dir.mkdir(parents=True)
        h5_file = phase2_dir / f'SHARED_10000_tick_phase_{date_str}.h5'
        h5_file.write_bytes(b'\x00' * 3000)

        freed = guardian._evict_date(date_str)

        assert freed == 8000
        assert not raw_ch.exists()
        assert not h5_file.exists()

    def test_collect_all_dates(self, tmp_data_root, guardian):
        """Collect dates from both raw_buffer and phase2."""
        # raw_buffer date
        (tmp_data_root / 'raw_buffer' / 'CH' / '20260301').mkdir(parents=True)
        # phase2 date
        p = tmp_data_root / 'phase2' / 'CH' / 'tick'
        p.mkdir(parents=True)
        (p / 'CH_tick_20260302.h5').write_bytes(b'\x00')

        dates = guardian._collect_all_dates()
        assert '20260301' in dates
        assert '20260302' in dates


# ======================================================================
# Config factory
# ======================================================================

class TestConfigFactory:
    """from_config() auto-detects channels and sample rate."""

    def test_counts_channels_from_toml(self, tmp_path):
        cfg = tmp_path / 'test.toml'
        cfg.write_text('''
[recorder]
production_data_root = "/var/lib/timestd"

[[channels]]
name = "SHARED_5000"
frequency_hz = 5000000

[[channels]]
name = "SHARED_10000"
frequency_hz = 10000000

[[channels]]
name = "CHU_7850"
frequency_hz = 7850000
''')
        g = ResourceGuardian.from_config(str(cfg))
        assert g.n_channels == 3
        assert g.data_root == Path('/var/lib/timestd')

    def test_missing_config_uses_defaults(self):
        g = ResourceGuardian.from_config('/nonexistent/path.toml')
        # Should fallback to counting dirs or default 9
        assert g.n_channels >= 1
        assert g.sample_rate == 24000

    def test_archive_dtype_with_trailing_comment(self, tmp_path):
        """A TOML comment after the value must not leak into archive_dtype."""
        cfg = tmp_path / 'c.toml'
        cfg.write_text('[recorder]\nproduction_data_root = "%s"\n'
                       'archive_dtype = "f16"   # B4 trial\n'
                       '[[channels]]\nname = "a"\n' % tmp_path)
        g = ResourceGuardian.from_config(str(cfg))
        assert g.archive_dtype == 'f16'

    def test_invalid_archive_dtype_warns_and_sizes_as_complex64(
            self, tmp_path, caplog):
        """The guardian only advises: a bad archive_dtype must never stop it."""
        cfg = tmp_path / 'c.toml'
        cfg.write_text('[recorder]\nproduction_data_root = "%s"\n'
                       'archive_dtype = "bogus"\n'
                       '[[channels]]\nname = "a"\n' % tmp_path)
        with caplog.at_level('WARNING'):
            g = ResourceGuardian.from_config(str(cfg))
        assert g.archive_dtype == 'complex64'
        warnings = [r for r in caplog.records
                    if r.levelname == 'WARNING' and 'archive_dtype' in r.getMessage()]
        assert len(warnings) == 1


# ======================================================================
# Helpers
# ======================================================================

class TestHelpers:

    def test_get_ram_available_returns_positive(self):
        ram = ResourceGuardian._get_ram_available()
        if os.path.exists('/proc/meminfo'):
            assert ram is not None
            assert ram > 0

    def test_dir_size(self, tmp_path):
        f1 = tmp_path / 'a.bin'
        f2 = tmp_path / 'sub' / 'b.bin'
        f2.parent.mkdir()
        f1.write_bytes(b'\x00' * 100)
        f2.write_bytes(b'\x00' * 200)
        assert ResourceGuardian._dir_size(tmp_path) == 300


# ======================================================================
# hf-timestd#31 — the guardian must not destroy a whole day within 2 s of
# a transient ≥95% spike; hysteresis, pause-first, granularity, gate, event.
# ======================================================================

class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def _du(total, used):
    return type('', (), {'total': total, 'used': used, 'free': total - used})()


def _live_du(root, fill_bytes, total_bytes):
    """disk_usage fake whose `used` = fill + REAL bytes under root, so an
    eviction actually moves the needle (small total so tiny fixtures count)."""
    import pathlib
    def f(_path):
        present = 0
        for pth in pathlib.Path(root).rglob('*'):
            if pth.is_file():
                try: present += pth.stat().st_size
                except OSError: pass
        used = fill_bytes + present
        return type('', (), {'total': total_bytes, 'used': used, 'free': total_bytes - used})()
    return f


def _day(root, date, *, raw_bytes=0, phase2_bytes=0):
    import pathlib
    if raw_bytes:
        d = pathlib.Path(root) / 'raw_buffer' / 'CH1' / date
        d.mkdir(parents=True, exist_ok=True)
        (d / f'{date}T000000.bin.zst').write_bytes(b'\0' * raw_bytes)
    if phase2_bytes:
        d = pathlib.Path(root) / 'phase2' / 'CH1' / 'L1'
        d.mkdir(parents=True, exist_ok=True)
        (d / f'L1_{date}.h5').write_bytes(b'\0' * phase2_bytes)


class TestGuardianHysteresis:
    def _guardian(self, root, clock, **kw):
        g = ResourceGuardian(data_root=str(root), n_channels=1, sample_rate=24000,
                             clock=clock, **kw)
        return g

    def test_first_crossing_pauses_and_does_not_evict(self, tmp_data_root):
        """A ≥95% spike must PAUSE (STOP) and delete nothing until the
        condition persists past the grace window."""
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = self._guardian(tmp_data_root, clk, evict_grace_sec=600)
        with patch('shutil.disk_usage', return_value=_du(500 * GB, 480 * GB)):  # 96%
            s = g.watchdog_check(force=True)
        assert s.state == ResourceState.STOP
        assert s.bytes_cleaned == 0
        assert (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()
        assert (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()

    def test_transient_spike_that_clears_never_evicts(self, tmp_data_root):
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000)
        g = self._guardian(tmp_data_root, clk, evict_grace_sec=600)
        with patch('shutil.disk_usage', return_value=_du(500 * GB, 480 * GB)):
            g.watchdog_check(force=True)
        clk.advance(120)                                   # 2 min later, disk recovered
        with patch('shutil.disk_usage', return_value=_du(500 * GB, 300 * GB)):  # 60%
            s = g.watchdog_check(force=True)
        assert s.state == ResourceState.OK
        assert (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()

    def test_sustained_pressure_evicts_after_grace(self, tmp_data_root):
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = self._guardian(tmp_data_root, clk, evict_grace_sec=600,
                           evict_low_water_percent=90.0)
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)  # ~97.5% at start
        with patch('shutil.disk_usage', side_effect=fake):
            assert g.watchdog_check(force=True).state == ResourceState.STOP   # 1st crossing: pause
            clk.advance(601)
            s = g.watchdog_check(force=True)                                  # after grace: evict
        assert s.bytes_cleaned > 0
        assert s.state in (ResourceState.CLEANED, ResourceState.STOP)

    def test_regenerable_data_evicted_before_raw(self, tmp_data_root):
        """Same day: the phase2 HDF5 (regenerable) goes before the raw IQ."""
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = self._guardian(tmp_data_root, clk, evict_grace_sec=0,
                           evict_low_water_percent=90.0)
        # total 40000, fill 24000: start 97.5%; deleting the 5000 phase2 file
        # drops to 85% (< 90% low-water) so eviction stops before touching raw.
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
        with patch('shutil.disk_usage', side_effect=fake):
            g.watchdog_check(force=True)
        assert not (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()
        assert (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()   # raw survived

    def test_alert_only_never_deletes(self, tmp_data_root):
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000)
        g = self._guardian(tmp_data_root, clk, evict_policy='alert_only', evict_grace_sec=0)
        with patch('shutil.disk_usage', return_value=_du(500 * GB, 490 * GB)):  # 98%
            s = g.watchdog_check(force=True)
        assert s.state in (ResourceState.STOP, ResourceState.EMERGENCY)
        assert s.bytes_cleaned == 0
        assert (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()

    def test_crossing_writes_a_guardian_event_the_board_can_read(self, tmp_data_root):
        import json
        clk = _Clock()
        g = self._guardian(tmp_data_root, clk, evict_grace_sec=600)
        with patch('shutil.disk_usage', return_value=_du(500 * GB, 480 * GB)):
            g.watchdog_check(force=True)
        ev = tmp_data_root / 'state' / 'guardian-event.json'
        assert ev.exists()
        d = json.loads(ev.read_text())
        assert d['over_threshold'] is True
        assert d['disk_used_percent'] >= 95.0
        assert d['policy'] == 'auto'
        assert 'evicting' not in d['action']         # paused, not evicting yet

    def test_from_config_reads_the_new_knobs(self, tmp_path):
        cfg = tmp_path / 'c.toml'
        cfg.write_text('[recorder]\nproduction_data_root = "%s"\n'
                       'evict_policy = "alert_only"\nevict_grace_sec = 300\n'
                       'evict_low_water_percent = 88\n[[channels]]\nname="a"\n' % tmp_path)
        g = ResourceGuardian.from_config(str(cfg))
        assert g.evict_policy == 'alert_only'
        assert g.evict_grace_sec == 300
        assert g.evict_low_water_percent == 88.0


# ======================================================================
# v3.96 Task 11 — the guardian is an emergency net.  The sigmond ledger
# owns routine retention.
# ======================================================================

import fcntl

from hf_timestd.core import resource_guardian as rg


def _guardian(root, used_pct, ledger, clock=None, **kw):
    """A guardian whose disk reads *used_pct* and whose ledger gate reads
    *ledger*.  Patches stay active through the test via the returned
    object's ``watchdog_check`` (they are applied with monkeypatch-free
    context managers inside the wrapper)."""
    g = ResourceGuardian(data_root=str(root), n_channels=1, sample_rate=24000,
                         clock=clock or _Clock(), **kw)
    real = g.watchdog_check

    def check(force=False):
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=ledger), \
             patch('shutil.disk_usage',
                   return_value=_du(500 * GB, int(500 * GB * used_pct / 100))):
            return real(force=force)
    g.watchdog_check = check
    return g


@pytest.fixture(autouse=True)
def _evict_lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, 'EVICT_LOCK', tmp_path / 'run' / 'guardian-evict.lock')
    # An unmanaged station unless a test says otherwise; never read the
    # real /run/sigmond/storage-status.json.
    monkeypatch.setattr(rg, 'ledger_manages_us', lambda: False)


class TestEmergencyOnly:
    def test_constants(self):
        assert rg.DISK_HARD_STOP_PERCENT == 95.0
        assert rg.LEDGER_HARD_STOP_PERCENT == 98.0

    def test_ledger_managed_station_holds_until_98(self, tmp_data_root):
        g = _guardian(tmp_data_root, 96.0, ledger=True)
        assert g.watchdog_check(force=True).state == ResourceState.OK

    def test_ledger_managed_station_stops_at_98(self, tmp_data_root):
        g = _guardian(tmp_data_root, 98.5, ledger=True)
        assert g.watchdog_check(force=True).state == ResourceState.STOP

    def test_unmanaged_station_still_stops_at_95(self, tmp_data_root):
        g = _guardian(tmp_data_root, 96.0, ledger=False)
        assert g.watchdog_check(force=True).state == ResourceState.STOP

    def test_hard_stop_percent_follows_the_gate_on_each_check(self, tmp_data_root):
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1)
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=True):
            assert g.hard_stop_percent() == 98.0
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False):
            assert g.hard_stop_percent() == 95.0

    def test_unmanaged_emergency_still_evicts_after_grace(self, tmp_data_root):
        """The metrology profile's only raw cleaner on an unmanaged station."""
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1,
                             clock=clk, evict_grace_sec=600,
                             evict_low_water_percent=90.0)
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False), \
             patch('shutil.disk_usage', side_effect=fake):
            assert g.watchdog_check(force=True).state == ResourceState.STOP
            clk.advance(601)
            s = g.watchdog_check(force=True)
        assert s.bytes_cleaned > 0

    def test_managed_emergency_at_98_evicts_after_grace(self, tmp_data_root):
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1,
                             clock=clk, evict_grace_sec=600,
                             evict_low_water_percent=90.0)
        fake = _live_du(tmp_data_root, fill_bytes=24500, total_bytes=40000)  # 98.7 %
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=True), \
             patch('shutil.disk_usage', side_effect=fake):
            assert g.watchdog_check(force=True).state == ResourceState.STOP
            clk.advance(601)
            s = g.watchdog_check(force=True)
        assert s.bytes_cleaned > 0
        assert not (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()

    def test_guardian_event_reports_the_threshold_in_force(self, tmp_data_root):
        import json
        g = _guardian(tmp_data_root, 99.0, ledger=True)
        g.watchdog_check(force=True)
        d = json.loads((tmp_data_root / 'state' / 'guardian-event.json').read_text())
        assert d['hard_stop_percent'] == 98.0

    def test_one_log_line_per_gate_change(self, tmp_data_root, caplog):
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1)
        with caplog.at_level(logging.INFO, logger='hf_timestd.core.resource_guardian'):
            with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                       return_value=True):
                g.hard_stop_percent(); g.hard_stop_percent()
        assert sum('ledger' in r.getMessage().lower() for r in caplog.records) == 1

    def test_ok_branch_walks_no_tree(self, monkeypatch, tmp_data_root):
        called = []
        monkeypatch.setattr(ResourceGuardian, "_collect_all_dates",
                            lambda self: called.append(1) or set())
        s = _guardian(tmp_data_root, 50.0, ledger=False).watchdog_check(force=True)
        assert called == []
        assert s.days_retained == 0 and s.days_headroom == 0

    def test_80_percent_warning_walks_no_tree_and_evicts_nothing(
            self, monkeypatch, tmp_data_root):
        called = []
        monkeypatch.setattr(ResourceGuardian, "_collect_all_dates",
                            lambda self: called.append(1) or set())
        _day(tmp_data_root, '20260101', raw_bytes=10000)
        s = _guardian(tmp_data_root, 90.0, ledger=False).watchdog_check(force=True)
        assert called == []
        assert s.state == ResourceState.OK and s.bytes_cleaned == 0
        assert (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()

    def test_low_headroom_below_threshold_evicts_nothing(self, tmp_data_root):
        """Free space under one day's budget no longer triggers eviction."""
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = _guardian(tmp_data_root, 94.0, ledger=False)
        s = g.watchdog_check(force=True)
        assert s.state == ResourceState.OK and s.bytes_cleaned == 0
        assert (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()

    def test_there_is_no_headroom_eviction_any_more(self):
        assert not hasattr(ResourceGuardian, '_ensure_headroom')
        assert not hasattr(ResourceGuardian, '_evict_oldest_days_until_under')

    def test_there_are_no_archive_methods_or_parameter(self):
        for name in ('_move_to_archive', '_enforce_archive_limits',
                     '_evict_archive_date', '_archive_is_mounted'):
            assert not hasattr(ResourceGuardian, name)
        with pytest.raises(TypeError):
            ResourceGuardian(archive_root='/x')

    def test_an_evicted_day_is_deleted_never_moved(self, tmp_data_root, tmp_path):
        _day(tmp_data_root, '20260101', raw_bytes=100, phase2_bytes=50)
        moved = []
        with patch('shutil.move', side_effect=lambda *a, **k: moved.append(a)):
            g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1)
            freed = g._evict_date('20260101')
        assert freed == 150 and moved == []
        assert not (tmp_data_root / 'raw_buffer' / 'CH1' / '20260101').exists()

    def test_from_config_ignores_archive_keys(self, tmp_path):
        cfg = tmp_path / 'c.toml'
        cfg.write_text('[recorder]\nproduction_data_root = "%s"\n'
                       'archive_root = "/mnt/x"\narchive_retention_days = 9\n'
                       '[[channels]]\nname="a"\n' % tmp_path)
        g = ResourceGuardian.from_config(str(cfg))
        assert not hasattr(g, 'archive_root')

    def test_preflight_never_evicts(self, tmp_data_root):
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1)
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False), \
             patch('shutil.disk_usage', return_value=_du(500 * GB, 425 * GB)):
            assert g.preflight_check() is True      # 85 %: over 80, under 95
        assert (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()

    def test_preflight_at_the_hard_stop_starts_paused_not_refused(self, tmp_data_root, caplog):
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1)
        du = _du(500 * GB, 480 * GB)                # 96 %
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False), patch('shutil.disk_usage', return_value=du), \
                caplog.at_level(logging.CRITICAL):
            assert g.preflight_check() is True
        assert any('PAUSED' in r.getMessage() for r in caplog.records)


class TestEvictLock:
    def _over_grace(self, root):
        clk = _Clock()
        _day(root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = ResourceGuardian(data_root=str(root), n_channels=1, clock=clk,
                             evict_grace_sec=0, evict_low_water_percent=90.0)
        return g

    def test_only_one_process_evicts_at_a_time(self, tmp_data_root, caplog):
        g = self._over_grace(tmp_data_root)
        rg.EVICT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(rg.EVICT_LOCK, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # another process holds it
        try:
            fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
            with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                       return_value=False), \
                 patch('shutil.disk_usage', side_effect=fake), \
                 caplog.at_level(logging.DEBUG, logger='hf_timestd.core.resource_guardian'):
                s = g.watchdog_check(force=True)
        finally:
            os.close(fd)
        assert s.bytes_cleaned == 0
        assert (tmp_data_root / 'phase2' / 'CH1' / 'L1' / 'L1_20260101.h5').exists()
        assert any(r.levelno == logging.WARNING and 'evict' in r.getMessage().lower()
                   for r in caplog.records)
        assert s.state == ResourceState.STOP          # not EMERGENCY: someone else is evicting
        import json
        ev = json.loads((tmp_data_root / 'state' / 'guardian-event.json').read_text())
        assert ev['action'] == 'evict-skipped-locked'

    def test_the_lock_is_released_after_a_pass(self, tmp_data_root):
        g = self._over_grace(tmp_data_root)
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False), patch('shutil.disk_usage', side_effect=fake):
            assert g.watchdog_check(force=True).bytes_cleaned > 0
        fd = os.open(rg.EVICT_LOCK, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if still held
        finally:
            os.close(fd)

    def test_an_unopenable_lock_does_not_disarm_the_emergency(self, tmp_data_root, monkeypatch):
        g = self._over_grace(tmp_data_root)
        monkeypatch.setattr(rg, 'EVICT_LOCK', tmp_data_root / 'raw_buffer' / 'CH1' / 'no' / 'x' / 'f')
        (tmp_data_root / 'raw_buffer' / 'CH1' / 'no').write_text('a file, so mkdir fails')
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
        with patch('hf_timestd.core.resource_guardian.ledger_manages_us',
                   return_value=False), patch('shutil.disk_usage', side_effect=fake):
            assert g.watchdog_check(force=True).bytes_cleaned > 0

    # -- lock hardening -------------------------------------------------

    def _hot(self, root):
        g = self._over_grace(root)
        fake = _live_du(root, fill_bytes=24000, total_bytes=40000)
        return g, fake

    def test_the_lock_is_created_0600(self, tmp_data_root):
        g, fake = self._hot(tmp_data_root)
        with patch('shutil.disk_usage', side_effect=fake):
            g.watchdog_check(force=True)
        assert (os.stat(rg.EVICT_LOCK).st_mode & 0o777) == 0o600

    def test_a_symlinked_lock_is_refused_and_eviction_still_runs(
            self, tmp_data_root, caplog):
        g, fake = self._hot(tmp_data_root)
        rg.EVICT_LOCK.parent.mkdir(parents=True)
        target = tmp_data_root / 'elsewhere'
        target.write_text('x')
        os.symlink(target, rg.EVICT_LOCK)
        with patch('shutil.disk_usage', side_effect=fake), \
                caplog.at_level(logging.WARNING):
            assert g.watchdog_check(force=True).bytes_cleaned > 0
        assert any('unavailable' in r.getMessage() for r in caplog.records)
        assert target.read_text() == 'x'

    def test_a_lock_owned_by_someone_else_is_refused(self, tmp_data_root, monkeypatch, caplog):
        g, fake = self._hot(tmp_data_root)
        rg.EVICT_LOCK.parent.mkdir(parents=True)
        rg.EVICT_LOCK.write_text('')
        monkeypatch.setattr(os, 'geteuid', lambda: os.stat(rg.EVICT_LOCK).st_uid + 1)
        held = os.open(rg.EVICT_LOCK, os.O_RDWR)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a hostile holder
        try:
            with patch('shutil.disk_usage', side_effect=fake), \
                    caplog.at_level(logging.WARNING):
                assert g.watchdog_check(force=True).bytes_cleaned > 0
        finally:
            os.close(held)
        assert any('not a regular file owned' in r.getMessage() for r in caplog.records)

    def test_a_lock_contended_past_the_bound_is_evicted_anyway(self, tmp_data_root):
        clk = _Clock()
        _day(tmp_data_root, '20260101', raw_bytes=10000, phase2_bytes=5000)
        g = ResourceGuardian(data_root=str(tmp_data_root), n_channels=1, clock=clk,
                             evict_grace_sec=0, evict_low_water_percent=90.0)
        rg.EVICT_LOCK.parent.mkdir(parents=True)
        held = os.open(rg.EVICT_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fake = _live_du(tmp_data_root, fill_bytes=24000, total_bytes=40000)
        try:
            with patch('shutil.disk_usage', side_effect=fake):
                assert g.watchdog_check(force=True).bytes_cleaned == 0     # contended: skip
                clk.advance(rg.EVICT_LOCK_MAX_WAIT_SEC + 1)
                assert g.watchdog_check(force=True).bytes_cleaned > 0      # bound passed
        finally:
            os.close(held)
