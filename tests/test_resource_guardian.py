#!/usr/bin/env python3
"""Tests for ResourceGuardian — universal, self-sizing resource management."""

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
    HEADROOM_DAYS,
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

    def test_watchdog_cleans_when_headroom_low(self, tmp_data_root):
        """When free < daily_disk_budget, watchdog should evict oldest data."""
        # Use tiered storage so daily budget is ~4 GB (phase2 only),
        # keeping the test disk percentages well below 95%.
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            n_channels=1, sample_rate=24000, tiered_storage=True,
        )
        headroom_needed = g.daily_disk_budget * HEADROOM_DAYS  # ~4 GB
        # Use a small disk so that "free < headroom" doesn't also
        # trigger the 95% hard stop.  50 GB disk, 3 GB free = 94% used.
        disk_total = 50 * GB

        # Create an old date dir in raw_buffer
        old_date = time.strftime('%Y%m%d', time.gmtime(time.time() - 5 * 86400))
        ch_dir = tmp_data_root / 'raw_buffer' / 'TEST_CH'
        date_dir = ch_dir / old_date
        date_dir.mkdir(parents=True)
        (date_dir / 'data.bin').write_bytes(b'\x00' * 10000)

        # First calls: free < headroom but under 95% used.
        # After eviction: headroom restored.
        low_free = 3 * GB                         # 94% used, < 4 GB headroom
        high_free = headroom_needed * 2           # ~8 GB
        call_count = [0]
        def fake_disk_usage(path):
            call_count[0] += 1
            if call_count[0] <= 2:
                return type('', (), {
                    'total': disk_total,
                    'used': disk_total - low_free,
                    'free': low_free,
                })()
            else:
                return type('', (), {
                    'total': disk_total,
                    'used': disk_total - high_free,
                    'free': high_free,
                })()

        with patch('shutil.disk_usage', side_effect=fake_disk_usage):
            status = g.watchdog_check(force=True)

        assert status.state == ResourceState.CLEANED
        assert not date_dir.exists(), "Old date dir should have been evicted"

    def test_watchdog_reports_days_retained(self, tmp_data_root, guardian):
        """ResourceStatus should include days_retained and days_headroom."""
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 200 * GB, 'free': 300 * GB,
            })()
            status = guardian.watchdog_check(force=True)
            assert hasattr(status, 'days_retained')
            assert hasattr(status, 'days_headroom')
            assert status.days_headroom > 0

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

    def test_evict_preserves_today(self, tmp_data_root, guardian):
        """Today's data is never evicted."""
        today = time.strftime('%Y%m%d', time.gmtime())
        ch = tmp_data_root / 'raw_buffer' / 'CH' / today
        ch.mkdir(parents=True)
        (ch / 'data.bin').write_bytes(b'\x00' * 1000)

        # _evict_oldest_days_until_under should skip today
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB, 'used': 410 * GB, 'free': 90 * GB,
            })()
            guardian._evict_oldest_days_until_under(500 * GB, 80.0)

        assert ch.exists(), "Today's data must not be evicted"

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
