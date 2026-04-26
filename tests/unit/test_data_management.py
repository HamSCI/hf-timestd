"""
Unit tests for hf_timestd.data_management

CLI-facing helpers for data lifecycle operations: size accounting, summary
printing, and gated deletion of RTP / analytics / upload / status trees.

Tests cover:
- get_data_size: empty/missing path, nested files, file count, traversal
  failure path
- format_size: B/KB/MB/GB/TB/PB ladder, boundary at 1024
- print_data_summary: invokes path resolver helpers and prints all sections
- clean_*: dry-run prints but doesn't touch disk; confirm=True deletes;
  confirm prompt accepts/rejects; missing directory short-circuit; on-disk
  cleanup recreates the directory
- clean_all: aggregates over all four directories, dry-run is non-destructive,
  delete prompt requires 'DELETE ALL'
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hf_timestd.data_management import DataManager


# =============================================================================
# Helpers
# =============================================================================


def make_resolver(*, data, analytics, upload, status,
                  web_ui=None, credentials=None) -> MagicMock:
    """Build a path-resolver mock with the four canonical directory accessors."""
    pr = MagicMock()
    pr.get_data_dir.return_value = data
    pr.get_analytics_dir.return_value = analytics
    pr.get_upload_state_dir.return_value = upload
    pr.get_status_dir.return_value = status
    pr.get_web_ui_data_dir.return_value = web_ui or (data.parent / 'web_ui')
    pr.get_credentials_dir.return_value = credentials or (data.parent / 'credentials')
    return pr


@pytest.fixture
def trees(tmp_path):
    """Build a fresh data/analytics/upload/status layout under tmp_path."""
    paths = {
        'data': tmp_path / 'data',
        'analytics': tmp_path / 'analytics',
        'upload': tmp_path / 'upload',
        'status': tmp_path / 'status',
    }
    for p in paths.values():
        p.mkdir()
    return paths


@pytest.fixture
def manager(trees):
    return DataManager(make_resolver(**trees))


# =============================================================================
# get_data_size
# =============================================================================


class TestGetDataSize:
    def test_empty_directory(self, manager, trees):
        size, count = manager.get_data_size(trees['data'])
        assert size == 0
        assert count == 0

    def test_missing_path(self, manager, tmp_path):
        size, count = manager.get_data_size(tmp_path / 'absent')
        assert (size, count) == (0, 0)

    def test_files_counted_and_sized(self, manager, trees):
        a = trees['data'] / 'a.bin'
        a.write_bytes(b'1234567890')
        sub = trees['data'] / 'sub'
        sub.mkdir()
        b = sub / 'b.bin'
        b.write_bytes(b'12345')
        size, count = manager.get_data_size(trees['data'])
        assert size == 15
        assert count == 2

    def test_directories_not_counted_as_files(self, manager, trees):
        # A directory tree with only sub-directories has zero files
        (trees['data'] / 'sub' / 'sub2').mkdir(parents=True)
        size, count = manager.get_data_size(trees['data'])
        assert size == 0
        assert count == 0

    def test_traversal_error_logged(self, manager, trees, caplog, monkeypatch):
        # Force rglob to raise → method swallows exception, logs error,
        # returns whatever was accumulated so far.
        bogus = trees['data']

        def boom(self, *args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, 'rglob', boom)
        size, count = manager.get_data_size(bogus)
        assert (size, count) == (0, 0)
        assert any('Error calculating size' in r.message for r in caplog.records)


# =============================================================================
# format_size
# =============================================================================


class TestFormatSize:
    @pytest.mark.parametrize("size,expected_unit", [
        (0, 'B'),
        (512, 'B'),
        (1024, 'KB'),
        (1024 ** 2, 'MB'),
        (1024 ** 3, 'GB'),
        (1024 ** 4, 'TB'),
        (1024 ** 5, 'PB'),
    ])
    def test_unit_ladder(self, manager, size, expected_unit):
        s = manager.format_size(size)
        assert s.endswith(expected_unit)

    def test_below_1024_is_bytes(self, manager):
        assert manager.format_size(999) == '999.00 B'

    def test_one_kib_renders_as_one(self, manager):
        assert manager.format_size(1024) == '1.00 KB'

    def test_two_decimal_places(self, manager):
        # Anything that's not exactly an integer multiple still shows .XX
        s = manager.format_size(1500)
        assert '.' in s


# =============================================================================
# print_data_summary
# =============================================================================


class TestPrintSummary:
    def test_prints_each_section(self, manager, trees, capsys):
        # Drop content into each tree so sizes are non-zero
        for name in ('data', 'analytics', 'upload', 'status'):
            (trees[name] / 'f.bin').write_bytes(b'x' * 100)

        manager.print_data_summary()
        out = capsys.readouterr().out
        # Each major section header appears
        assert 'RTP Recordings' in out
        assert 'Analytics' in out
        assert 'Upload State' in out
        assert 'Runtime Status' in out
        assert 'Total Deletable' in out
        assert 'Site Management' in out

    def test_calls_resolver_for_paths(self, manager, trees):
        manager.print_data_summary()
        manager.path_resolver.get_data_dir.assert_called()
        manager.path_resolver.get_analytics_dir.assert_called()
        manager.path_resolver.get_upload_state_dir.assert_called()
        manager.path_resolver.get_status_dir.assert_called()
        manager.path_resolver.get_web_ui_data_dir.assert_called()
        manager.path_resolver.get_credentials_dir.assert_called()


# =============================================================================
# Per-tree clean_* methods
# =============================================================================


class TestCleanData:
    def test_missing_directory_short_circuits(self, tmp_path, capsys):
        pr = make_resolver(
            data=tmp_path / 'absent',
            analytics=tmp_path / 'a',
            upload=tmp_path / 'u',
            status=tmp_path / 's',
        )
        DataManager(pr).clean_data(dry_run=False, confirm=True)
        out = capsys.readouterr().out
        assert 'does not exist' in out

    def test_dry_run_does_not_touch_disk(self, manager, trees, capsys):
        target = trees['data'] / 'keep.bin'
        target.write_bytes(b'hello')
        manager.clean_data(dry_run=True, confirm=True)
        # File survives dry-run
        assert target.exists()
        assert '[DRY RUN]' in capsys.readouterr().out

    def test_confirm_true_deletes_then_recreates(self, manager, trees, capsys):
        (trees['data'] / 'a.bin').write_bytes(b'x' * 1000)
        (trees['data'] / 'sub').mkdir()
        (trees['data'] / 'sub' / 'b.bin').write_bytes(b'y' * 500)

        manager.clean_data(dry_run=False, confirm=True)

        # Directory still exists but is empty
        assert trees['data'].exists()
        assert list(trees['data'].iterdir()) == []
        assert 'Deleted' in capsys.readouterr().out

    def test_confirm_prompt_accepts_DELETE(self, manager, trees, monkeypatch):
        (trees['data'] / 'a.bin').write_bytes(b'x')
        monkeypatch.setattr('builtins.input', lambda _: 'DELETE')
        manager.clean_data(dry_run=False, confirm=False)
        assert list(trees['data'].iterdir()) == []

    def test_confirm_prompt_rejects_non_match(self, manager, trees, monkeypatch, capsys):
        (trees['data'] / 'a.bin').write_bytes(b'x')
        monkeypatch.setattr('builtins.input', lambda _: 'no')
        manager.clean_data(dry_run=False, confirm=False)
        # File survived rejection
        assert (trees['data'] / 'a.bin').exists()
        assert 'Cancelled' in capsys.readouterr().out


class TestCleanAnalytics:
    def test_dry_run_does_not_touch_disk(self, manager, trees, capsys):
        target = trees['analytics'] / 'keep.bin'
        target.write_bytes(b'hello')
        manager.clean_analytics(dry_run=True, confirm=True)
        assert target.exists()
        assert '[DRY RUN]' in capsys.readouterr().out

    def test_real_delete_clears_tree(self, manager, trees):
        (trees['analytics'] / 'a').write_bytes(b'x')
        manager.clean_analytics(dry_run=False, confirm=True)
        assert list(trees['analytics'].iterdir()) == []

    def test_missing_directory_handled(self, tmp_path, capsys):
        pr = make_resolver(
            data=tmp_path / 'd',
            analytics=tmp_path / 'absent',
            upload=tmp_path / 'u',
            status=tmp_path / 's',
        )
        DataManager(pr).clean_analytics(dry_run=False, confirm=True)
        assert 'does not exist' in capsys.readouterr().out


class TestCleanUploads:
    def test_dry_run_does_not_touch_disk(self, manager, trees, capsys):
        (trees['upload'] / 'q').write_bytes(b'x')
        manager.clean_uploads(dry_run=True, confirm=True)
        assert (trees['upload'] / 'q').exists()
        assert '[DRY RUN]' in capsys.readouterr().out

    def test_real_delete_clears_tree(self, manager, trees):
        (trees['upload'] / 'q').write_bytes(b'x')
        manager.clean_uploads(dry_run=False, confirm=True)
        assert list(trees['upload'].iterdir()) == []

    def test_missing_directory_handled(self, tmp_path, capsys):
        pr = make_resolver(
            data=tmp_path / 'd',
            analytics=tmp_path / 'a',
            upload=tmp_path / 'absent',
            status=tmp_path / 's',
        )
        DataManager(pr).clean_uploads(dry_run=False, confirm=True)
        assert 'does not exist' in capsys.readouterr().out


# =============================================================================
# clean_all
# =============================================================================


class TestCleanAll:
    def test_dry_run_lists_all_paths_and_does_not_delete(self, manager, trees, capsys):
        for name in ('data', 'analytics', 'upload', 'status'):
            (trees[name] / 'f').write_bytes(b'x')

        manager.clean_all(dry_run=True, confirm=True)
        out = capsys.readouterr().out

        assert '[DRY RUN]' in out
        # Every directory still has its file
        for name in ('data', 'analytics', 'upload', 'status'):
            assert (trees[name] / 'f').exists()

    def test_confirm_prompt_requires_DELETE_ALL(self, manager, trees, monkeypatch, capsys):
        for name in ('data', 'analytics', 'upload', 'status'):
            (trees[name] / 'f').write_bytes(b'x')
        monkeypatch.setattr('builtins.input', lambda _: 'DELETE')  # not enough
        manager.clean_all(dry_run=False, confirm=False)
        # All files survived
        for name in ('data', 'analytics', 'upload', 'status'):
            assert (trees[name] / 'f').exists()
        assert 'Cancelled' in capsys.readouterr().out

    def test_confirm_true_deletes_all_trees_and_recreates_them(
            self, manager, trees, capsys):
        for name in ('data', 'analytics', 'upload', 'status'):
            (trees[name] / 'f.bin').write_bytes(b'a' * 50)
            (trees[name] / 'sub').mkdir()
            (trees[name] / 'sub' / 'g.bin').write_bytes(b'b' * 25)

        manager.clean_all(dry_run=False, confirm=True)

        # Each directory still exists, but is empty
        for name in ('data', 'analytics', 'upload', 'status'):
            assert trees[name].exists()
            assert list(trees[name].iterdir()) == []

    def test_clean_all_handles_missing_directory(self, tmp_path):
        # A directory that doesn't exist should not crash clean_all
        pr = make_resolver(
            data=tmp_path / 'd',
            analytics=tmp_path / 'a',
            upload=tmp_path / 'u',
            status=tmp_path / 'absent',  # never created
        )
        for d in (pr.get_data_dir.return_value,
                  pr.get_analytics_dir.return_value,
                  pr.get_upload_state_dir.return_value):
            d.mkdir()
        # clean_all walks all four; the absent one should be skipped silently
        DataManager(pr).clean_all(dry_run=False, confirm=True)
