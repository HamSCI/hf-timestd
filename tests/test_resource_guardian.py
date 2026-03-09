#!/usr/bin/env python3
"""Tests for ResourceGuardian — resource management and cleanup logic."""

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd.core.resource_guardian import (
    ResourceGuardian,
    ResourceState,
    ResourceStatus,
    RetentionPolicy,
    DISK_WARN_FREE_BYTES,
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
    """Create a ResourceGuardian with temporary paths."""
    return ResourceGuardian(
        data_root=str(tmp_data_root),
        log_dir=str(tmp_data_root / 'logs'),
        min_disk_free_gb=0.001,  # 1 MB — low for testing
        min_ram_available_gb=0.001,
    )


class TestPreflight:
    """Preflight check tests."""

    def test_preflight_passes_with_sufficient_resources(self, guardian):
        assert guardian.preflight_check() is True

    def test_preflight_fails_on_insufficient_disk(self, tmp_data_root):
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            min_disk_free_gb=999999,  # 999 TB — impossible
            min_ram_available_gb=0.001,
        )
        assert g.preflight_check() is False

    def test_preflight_fails_on_insufficient_ram(self, tmp_data_root):
        g = ResourceGuardian(
            data_root=str(tmp_data_root),
            min_disk_free_gb=0.001,
            min_ram_available_gb=999999,  # impossible
        )
        assert g.preflight_check() is False

    def test_preflight_creates_missing_dirs(self, tmp_path):
        # Create data_root (disk_usage needs it) but not subdirs
        fresh = tmp_path / 'fresh'
        fresh.mkdir()
        g = ResourceGuardian(
            data_root=str(fresh),
            min_disk_free_gb=0.001,
            min_ram_available_gb=0.001,
        )
        assert g.preflight_check() is True
        assert (fresh / 'state').exists()


class TestWatchdog:
    """Runtime watchdog tests."""

    def test_watchdog_ok_with_sufficient_resources(self, guardian):
        # Mock disk usage to avoid triggering quota on the real filesystem
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB,
                'used': 200 * GB,
                'free': 300 * GB,
            })()
            status = guardian.watchdog_check(force=True)
            assert status.state == ResourceState.OK

    def test_watchdog_skips_when_interval_not_elapsed(self, guardian):
        # First check runs
        s1 = guardian.watchdog_check(force=True)
        # Second check within interval — should skip
        s2 = guardian.watchdog_check(force=False)
        assert 'skipped' in s2.message

    def test_watchdog_returns_emergency_on_no_disk(self, guardian):
        """Simulate zero disk free."""
        fake_usage = shutil.disk_usage.__class__
        with patch('shutil.disk_usage') as mock_du:
            mock_du.return_value = type('', (), {
                'total': 500 * GB,
                'used': 500 * GB - 500 * MB,
                'free': 500 * MB,  # < DISK_EMERGENCY_FREE_BYTES (1 GB)
            })()
            status = guardian.watchdog_check(force=True)
            assert status.state == ResourceState.EMERGENCY


class TestRetentionCleanup:
    """Storage janitor tests."""

    def test_cleanup_dated_dirs(self, tmp_data_root, guardian):
        """Verify that old YYYYMMDD directories are removed."""
        channel = tmp_data_root / 'raw_buffer' / 'SHARED_10000'
        channel.mkdir(parents=True)

        # Create old and new date dirs
        old_date = time.strftime('%Y%m%d', time.gmtime(time.time() - 10 * 86400))
        new_date = time.strftime('%Y%m%d', time.gmtime())

        old_dir = channel / old_date
        new_dir = channel / new_date
        old_dir.mkdir()
        new_dir.mkdir()

        # Put a file in old dir so it has size
        (old_dir / 'data.bin').write_bytes(b'\x00' * 1000)
        (new_dir / 'data.bin').write_bytes(b'\x00' * 1000)

        # Run cleanup with 2-day retention
        policy = RetentionPolicy(
            path=tmp_data_root / 'raw_buffer',
            max_age_days=2,
            file_patterns=[],
        )
        freed = guardian._enforce_retention(policy)

        assert freed > 0
        assert not old_dir.exists(), "Old directory should be removed"
        assert new_dir.exists(), "New directory should be preserved"

    def test_cleanup_dated_files(self, tmp_data_root, guardian):
        """Verify that old *_YYYYMMDD.h5 files are removed."""
        channel = tmp_data_root / 'phase2' / 'SHARED_10000' / 'tick_phase'
        channel.mkdir(parents=True)

        old_date = time.strftime('%Y%m%d', time.gmtime(time.time() - 60 * 86400))
        new_date = time.strftime('%Y%m%d', time.gmtime())

        old_file = channel / f'SHARED_10000_tick_phase_{old_date}.h5'
        new_file = channel / f'SHARED_10000_tick_phase_{new_date}.h5'
        old_file.write_bytes(b'\x00' * 2000)
        new_file.write_bytes(b'\x00' * 2000)

        policy = RetentionPolicy(
            path=tmp_data_root / 'phase2',
            max_age_days=30,
            file_patterns=['*_????????.h5'],
        )
        freed = guardian._enforce_retention(policy)

        assert freed > 0
        assert not old_file.exists(), "Old HDF5 file should be removed"
        assert new_file.exists(), "New HDF5 file should be preserved"

    def test_cleanup_preserves_files_within_retention(self, tmp_data_root, guardian):
        """Files within retention period are not deleted."""
        channel = tmp_data_root / 'phase2' / 'CHU_7850' / 'clock_offset'
        channel.mkdir(parents=True)

        today = time.strftime('%Y%m%d', time.gmtime())
        f = channel / f'CHU_7850_clock_offset_{today}.h5'
        f.write_bytes(b'\x00' * 1000)

        policy = RetentionPolicy(
            path=tmp_data_root / 'phase2',
            max_age_days=30,
            file_patterns=['*_????????.h5'],
        )
        freed = guardian._enforce_retention(policy)

        assert freed == 0
        assert f.exists()


class TestConfigFactory:
    """from_config() factory method tests."""

    def test_from_config_with_valid_toml(self, tmp_path):
        cfg = tmp_path / 'test.toml'
        cfg.write_text('''
[recorder]
production_data_root = "/var/lib/timestd"

[resource_guardian]
disk_quota_percent = 90.0
min_disk_free_gb = 100.0
retention_raw_buffer = 3
retention_phase2 = 60
''')
        g = ResourceGuardian.from_config(str(cfg))
        assert g.disk_quota_percent == 90.0
        assert g.min_disk_free_bytes == 100 * GB
        assert g.retention['raw_buffer'] == 3
        assert g.retention['phase2'] == 60
        # Unspecified retentions keep defaults
        assert g.retention['upload'] == 7

    def test_from_config_missing_file_uses_defaults(self):
        g = ResourceGuardian.from_config('/nonexistent/path.toml')
        assert g.disk_quota_percent == 85.0
        assert g.retention['raw_buffer'] == 2


class TestHelpers:
    """Helper method tests."""

    def test_get_ram_available_returns_positive(self):
        """On Linux, RAM should be available."""
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
