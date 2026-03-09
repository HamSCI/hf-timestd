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
    DISK_MAX_PERCENT,
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
        """1-channel guardian on a real disk should always pass."""
        assert guardian.preflight_check() is True

    def test_preflight_fails_if_disk_too_small_for_baseline(self, tmp_data_root):
        """If 80% of disk < baseline, preflight must fail."""
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            n_channels=9999,   # absurd — baseline exceeds any disk
            sample_rate=24000,
        )
        assert g.preflight_check() is False

    def test_preflight_fails_if_ram_too_small(self, tmp_data_root):
        """If 80% of RAM < min_ram, preflight must fail."""
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            n_channels=99999,  # min_ram will exceed any real system
            sample_rate=24000,
        )
        assert g.preflight_check() is False

    def test_preflight_creates_missing_dirs(self, tmp_path):
        fresh = tmp_path / 'fresh'
        fresh.mkdir()
        g = ResourceGuardian(data_root=str(fresh), n_channels=1)
        assert g.preflight_check() is True
        assert (fresh / 'state').exists()
        assert (fresh / 'raw_buffer').exists()
        assert (fresh / 'phase2').exists()


# ======================================================================
# Watchdog
# ======================================================================

class TestWatchdog:
    """Runtime watchdog: enforce 80% disk cap."""

    def test_watchdog_ok_when_under_80_percent(self, guardian):
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

    def test_watchdog_cleans_when_over_80_percent(self, tmp_data_root, guardian):
        """When disk > 80%, watchdog should evict oldest data."""
        # Create an old date dir in raw_buffer
        old_date = time.strftime('%Y%m%d', time.gmtime(time.time() - 5 * 86400))
        ch_dir = tmp_data_root / 'raw_buffer' / 'TEST_CH'
        date_dir = ch_dir / old_date
        date_dir.mkdir(parents=True)
        (date_dir / 'data.bin').write_bytes(b'\x00' * 10000)

        # Mock disk_usage: first two calls (watchdog + eviction loop pre-check)
        # return >80%, subsequent calls return <80% (simulating freed space)
        call_count = [0]
        def fake_disk_usage(path):
            call_count[0] += 1
            if call_count[0] <= 2:
                # Watchdog check + eviction loop's first "are we under?" check
                return type('', (), {
                    'total': 500 * GB, 'used': 410 * GB, 'free': 90 * GB,
                })()
            else:
                # After eviction: under 80%
                return type('', (), {
                    'total': 500 * GB, 'used': 350 * GB, 'free': 150 * GB,
                })()

        with patch('shutil.disk_usage', side_effect=fake_disk_usage):
            status = guardian.watchdog_check(force=True)

        assert status.state == ResourceState.CLEANED
        assert not date_dir.exists(), "Old date dir should have been evicted"

    def test_watchdog_emergency_when_over_95_percent(self, guardian):
        """When disk > 95% and can't clean, should return EMERGENCY."""
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB,
                'used': 480 * GB,   # 96%
                'free': 20 * GB,
            })()
            status = guardian.watchdog_check(force=True)
            assert status.state == ResourceState.EMERGENCY


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
