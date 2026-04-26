"""
Unit tests for hf_timestd.quota_manager

Day-level circular-buffer quota manager that scans the storage hierarchy and
deletes (or archives) the oldest day-units when usage exceeds a threshold.

Tests cover:
- Module-level YYYYMMDD regex
- DayEntry dataclass shape
- Category-priority ordering (products → phase2 → vtec → raw_iq)
- Protected dates: today, yesterday, and the min_days_to_keep window
- Per-tree scanners (raw_iq, phase2, products, vtec) — empty trees, valid
  dates picked up, malformed names ignored, protected dates skipped
- scan_day_entries: priority + date sort
- get_disk_usage: stubbed shutil.disk_usage
- _delete_entry / _force_delete_entry: dry-run vs real deletion, file vs dir
- _archive_is_mounted / _archive_entry — fall-back to delete on archive failure
- enforce_retention: trims derived data older than derived_max_days
- archive_raw: skip when no archive root, never moves today
- enforce_quota: short-circuits when under threshold, otherwise calls retention
  + archive + scan-and-delete
- get_storage_inventory: full date inventory across categories
- get_status: shape of the returned dict
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from hf_timestd.quota_manager import _DATE_RE, DayEntry, QuotaManager


# =============================================================================
# Module constants
# =============================================================================


class TestDateRegex:
    @pytest.mark.parametrize("s", [
        '20260426', '20260101', '20251231',
        '19991231',  # 19xx still valid
    ])
    def test_valid_dates_match(self, s):
        assert _DATE_RE.fullmatch(s) is not None

    @pytest.mark.parametrize("s", [
        '20261301',     # invalid month
        '20260132',     # day above 31
        '99990101',     # not 19xx or 20xx
        '2026010',      # too short
        '202604269',    # too long
    ])
    def test_invalid_dates_dont_match(self, s):
        assert _DATE_RE.fullmatch(s) is None

    def test_regex_does_not_validate_days_per_month(self):
        # Documented limitation: the regex allows day 01-31 for every month,
        # so Feb 30 is accepted as a "valid" YYYYMMDD even though it's a real-
        # calendar nonsense date. This is fine in practice because filenames
        # are written from a real datetime — but document it as expected.
        assert _DATE_RE.fullmatch('20260230') is not None

    def test_search_extracts_embedded_date(self):
        m = _DATE_RE.search('foo_20260426_bar')
        assert m is not None
        assert m.group(1) == '20260426'


# =============================================================================
# Dataclass invariant
# =============================================================================


class TestDayEntry:
    def test_construction(self):
        e = DayEntry(path=Path('/tmp/x'), date_str='20260426',
                     category='raw_iq', is_dir=True)
        assert e.path == Path('/tmp/x')
        assert e.date_str == '20260426'
        assert e.category == 'raw_iq'
        assert e.is_dir is True


# =============================================================================
# Category priority
# =============================================================================


class TestCategoryPriority:
    def test_priority_ordering_keeps_raw_iq_last(self):
        # raw_iq should have the largest priority value (deleted last)
        prio = QuotaManager.CATEGORY_PRIORITY
        assert prio['raw_iq'] > prio['vtec']
        assert prio['vtec'] > prio['phase2']
        assert prio['phase2'] > prio['products']

    def test_all_known_categories_listed(self):
        assert set(QuotaManager.CATEGORY_PRIORITY) == \
            {'products', 'phase2', 'vtec', 'raw_iq'}


# =============================================================================
# Construction & paths
# =============================================================================


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / 'timestd-data'


@pytest.fixture
def manager(data_root):
    return QuotaManager(data_root=data_root, threshold_percent=75.0,
                        min_days_to_keep=2)


class TestConstruction:
    def test_paths_derived_from_root(self, manager, data_root):
        assert manager.data_root == data_root
        assert manager.raw_buffer_dir == data_root / 'raw_buffer'
        assert manager.phase2_dir == data_root / 'phase2'
        assert manager.vtec_dir == data_root / 'data' / 'gnss_vtec'
        assert manager.products_dir == data_root / 'products'

    def test_default_archive_root_is_none(self, manager):
        assert manager.archive_root is None


# =============================================================================
# Protected dates
# =============================================================================


class TestProtectedDates:
    def test_protects_today_and_yesterday_at_minimum(self, data_root):
        # min_days_to_keep=0 still gives 2 protected dates (today + yesterday)
        m = QuotaManager(data_root=data_root, min_days_to_keep=0)
        protected = m._protected_dates()
        today = datetime.now(timezone.utc).date()
        assert today.strftime('%Y%m%d') in protected
        assert (today - timedelta(days=1)).strftime('%Y%m%d') in protected
        assert len(protected) == 2

    def test_min_days_to_keep_extends_window(self, data_root):
        m = QuotaManager(data_root=data_root, min_days_to_keep=7)
        protected = m._protected_dates()
        assert len(protected) == 7
        # Spans last 7 days
        today = datetime.now(timezone.utc).date()
        for d in range(7):
            assert (today - timedelta(days=d)).strftime('%Y%m%d') in protected


# =============================================================================
# Per-tree scanners
# =============================================================================


def _old_date(days_back: int = 30) -> str:
    """Return a YYYYMMDD string `days_back` days before today."""
    return (datetime.now(timezone.utc).date() - timedelta(days=days_back)).strftime('%Y%m%d')


def _populate_raw_iq(data_root, channel, date_str):
    d = data_root / 'raw_buffer' / channel / date_str
    d.mkdir(parents=True)
    (d / 'min_0.bin.zst').write_bytes(b'x' * 100)
    return d


def _populate_phase2(data_root, channel, product, date_str):
    d = data_root / 'phase2' / channel / product
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{channel}_{product}_{date_str}.h5"
    f.write_bytes(b'x' * 50)
    return f


def _populate_products(data_root, channel, date_str):
    d = data_root / 'products' / channel / 'decimated'
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{date_str}.bin"
    f.write_bytes(b'x' * 30)
    return f


def _populate_vtec(data_root, date_str):
    d = data_root / 'data' / 'gnss_vtec'
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"GNSS_gnss_vtec_{date_str}.h5"
    f.write_bytes(b'x' * 70)
    return f


class TestRawIQScanner:
    def test_empty_returns_no_entries(self, manager):
        assert manager._scan_raw_iq(set()) == []

    def test_picks_up_valid_day_directories(self, manager, data_root):
        old = _old_date(30)
        _populate_raw_iq(data_root, 'WWV_10000', old)
        entries = manager._scan_raw_iq(set())
        assert len(entries) == 1
        assert entries[0].date_str == old
        assert entries[0].category == 'raw_iq'
        assert entries[0].is_dir is True

    def test_protected_dates_skipped(self, manager, data_root):
        today = datetime.now(timezone.utc).date().strftime('%Y%m%d')
        _populate_raw_iq(data_root, 'WWV_10000', today)
        entries = manager._scan_raw_iq({today})
        assert entries == []

    def test_non_date_directories_ignored(self, manager, data_root):
        chan = data_root / 'raw_buffer' / 'WWV_10000'
        chan.mkdir(parents=True)
        (chan / 'metadata').mkdir()
        (chan / 'logs').mkdir()
        assert manager._scan_raw_iq(set()) == []


class TestPhase2Scanner:
    def test_empty_returns_no_entries(self, manager):
        assert manager._scan_phase2(set()) == []

    def test_picks_up_dated_h5_files(self, manager, data_root):
        old = _old_date(30)
        _populate_phase2(data_root, 'WWV_10000', 'timing_measurements', old)
        entries = manager._scan_phase2(set())
        assert len(entries) == 1
        assert entries[0].date_str == old
        assert entries[0].category == 'phase2'
        assert entries[0].is_dir is False

    def test_undated_files_ignored(self, manager, data_root):
        d = data_root / 'phase2' / 'misc'
        d.mkdir(parents=True)
        (d / 'no_date.h5').write_bytes(b'x')
        assert manager._scan_phase2(set()) == []

    def test_protected_dates_skipped(self, manager, data_root):
        today = datetime.now(timezone.utc).date().strftime('%Y%m%d')
        _populate_phase2(data_root, 'WWV_10000', 'timing_measurements', today)
        entries = manager._scan_phase2({today})
        assert entries == []


class TestProductsScanner:
    def test_empty_returns_no_entries(self, manager):
        assert manager._scan_products(set()) == []

    def test_picks_up_dated_files(self, manager, data_root):
        old = _old_date(30)
        _populate_products(data_root, 'WWV_10000', old)
        entries = manager._scan_products(set())
        assert len(entries) == 1
        assert entries[0].category == 'products'
        assert entries[0].date_str == old


class TestVTECScanner:
    def test_empty_returns_no_entries(self, manager):
        assert manager._scan_vtec(set()) == []

    def test_picks_up_dated_files(self, manager, data_root):
        old = _old_date(30)
        _populate_vtec(data_root, old)
        entries = manager._scan_vtec(set())
        assert len(entries) == 1
        assert entries[0].category == 'vtec'
        assert entries[0].date_str == old


# =============================================================================
# scan_day_entries — combined ordering
# =============================================================================


class TestScanDayEntries:
    def test_priority_then_date_sort(self, manager, data_root):
        old, older = _old_date(20), _old_date(40)
        _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        _populate_raw_iq(data_root, 'WWV_10000', older)
        _populate_products(data_root, 'WWV_10000', old)
        _populate_vtec(data_root, older)

        entries = manager.scan_day_entries()
        # Lower-priority categories come first (products=1, raw_iq=4)
        categories_order = [e.category for e in entries]
        # products first
        assert categories_order[0] == 'products'
        # raw_iq last
        assert categories_order[-1] == 'raw_iq'

    def test_within_category_oldest_first(self, manager, data_root):
        # Two phase2 files at different dates → oldest-first within category
        d1 = _old_date(10)
        d2 = _old_date(40)
        _populate_phase2(data_root, 'WWV_10000', 'tm', d1)
        _populate_phase2(data_root, 'WWV_10000', 'tm', d2)
        entries = [e for e in manager.scan_day_entries() if e.category == 'phase2']
        assert entries[0].date_str == d2  # older first
        assert entries[1].date_str == d1


# =============================================================================
# Disk usage
# =============================================================================


class TestGetDiskUsage:
    def test_returns_stubbed_values(self, manager):
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value._asdict = lambda: None
            mock_du.return_value.used = 50_000
            mock_du.return_value.total = 100_000
            mock_du.return_value.free = 50_000
            used, total, percent = manager.get_disk_usage()
        assert used == 50_000
        assert total == 100_000
        assert percent == pytest.approx(50.0)


# =============================================================================
# Deletion / archive
# =============================================================================


class TestDeleteEntry:
    def test_dry_run_does_not_remove_directory(self, manager, data_root):
        manager.dry_run = True
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        entry = DayEntry(path=d, date_str=old, category='raw_iq', is_dir=True)
        size = manager._delete_entry(entry)
        assert size > 0
        assert d.exists()  # not deleted

    def test_real_delete_removes_directory(self, manager, data_root):
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        entry = DayEntry(path=d, date_str=old, category='raw_iq', is_dir=True)
        size = manager._delete_entry(entry)
        assert size > 0
        assert not d.exists()

    def test_dry_run_does_not_remove_file(self, manager, data_root):
        manager.dry_run = True
        old = _old_date(30)
        f = _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        entry = DayEntry(path=f, date_str=old, category='phase2', is_dir=False)
        size = manager._delete_entry(entry)
        assert size == 50
        assert f.exists()

    def test_real_delete_removes_file(self, manager, data_root):
        old = _old_date(30)
        f = _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        entry = DayEntry(path=f, date_str=old, category='phase2', is_dir=False)
        manager._delete_entry(entry)
        assert not f.exists()

    def test_missing_path_returns_zero_and_logs(self, manager, caplog):
        entry = DayEntry(path=Path('/nonexistent/x'), date_str='20260101',
                         category='vtec', is_dir=False)
        size = manager._delete_entry(entry)
        assert size == 0
        assert any('Failed to remove' in r.message for r in caplog.records)


class TestArchiveIsMounted:
    def test_no_archive_root_returns_false(self, manager):
        assert manager._archive_is_mounted() is False

    def test_existing_writable_root_returns_true(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        m = QuotaManager(data_root=data_root, archive_root=archive)
        assert m._archive_is_mounted() is True

    def test_nonexistent_root_returns_false(self, data_root, tmp_path):
        m = QuotaManager(data_root=data_root, archive_root=tmp_path / 'absent')
        assert m._archive_is_mounted() is False


class TestArchiveEntry:
    def test_archive_moves_file(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        data_root.mkdir(parents=True)
        m = QuotaManager(data_root=data_root, archive_root=archive)
        old = _old_date(30)
        f = _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        entry = DayEntry(path=f, date_str=old, category='phase2', is_dir=False)
        size = m._archive_entry(entry)
        assert size == 50
        # Original gone, archived copy at the same relative path
        assert not f.exists()
        rel = f.relative_to(data_root)
        assert (archive / rel).exists()

    def test_archive_moves_directory(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        m = QuotaManager(data_root=data_root, archive_root=archive)
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        entry = DayEntry(path=d, date_str=old, category='raw_iq', is_dir=True)
        size = m._archive_entry(entry)
        assert size > 0
        assert not d.exists()
        rel = d.relative_to(data_root)
        assert (archive / rel).exists()

    def test_archive_dry_run_keeps_source(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        m = QuotaManager(data_root=data_root, archive_root=archive, dry_run=True)
        old = _old_date(30)
        f = _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        entry = DayEntry(path=f, date_str=old, category='phase2', is_dir=False)
        size = m._archive_entry(entry)
        assert size == 50
        # Dry run leaves the file alone
        assert f.exists()


class TestForceDeleteEntry:
    def test_deletes_file(self, manager, data_root):
        old = _old_date(30)
        f = _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        entry = DayEntry(path=f, date_str=old, category='phase2', is_dir=False)
        size = manager._force_delete_entry(entry)
        assert size == 50
        assert not f.exists()

    def test_deletes_directory(self, manager, data_root):
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        entry = DayEntry(path=d, date_str=old, category='raw_iq', is_dir=True)
        size = manager._force_delete_entry(entry)
        assert size > 0
        assert not d.exists()


# =============================================================================
# enforce_retention / archive_raw
# =============================================================================


class TestEnforceRetention:
    def test_no_action_when_nothing_old_enough(self, manager, data_root):
        # File two days old — derived_max_days defaults to 7
        recent = _old_date(2)
        _populate_phase2(data_root, 'WWV_10000', 'tm', recent)
        result = manager.enforce_retention()
        assert result['items_deleted'] == 0
        assert result['bytes_freed'] == 0

    def test_trims_derived_data_older_than_cutoff(self, data_root):
        m = QuotaManager(data_root=data_root, derived_max_days=7,
                         min_days_to_keep=2)
        old = _old_date(30)
        _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        _populate_products(data_root, 'WWV_10000', old)
        result = m.enforce_retention()
        assert result['items_deleted'] == 2
        assert result['bytes_freed'] > 0

    def test_does_not_touch_raw_iq(self, data_root):
        m = QuotaManager(data_root=data_root, derived_max_days=7,
                         min_days_to_keep=2)
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        m.enforce_retention()
        # Raw IQ is preserved by enforce_retention
        assert d.exists()


class TestArchiveRaw:
    def test_no_archive_root_returns_zero(self, manager):
        result = manager.archive_raw()
        assert result == {'dirs_archived': 0, 'bytes_freed': 0, 'dry_run': False}

    def test_archives_completed_days(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        m = QuotaManager(data_root=data_root, archive_root=archive)
        old = _old_date(30)
        d = _populate_raw_iq(data_root, 'WWV_10000', old)
        result = m.archive_raw()
        assert result['dirs_archived'] == 1
        assert result['bytes_freed'] > 0
        assert not d.exists()

    def test_never_moves_today(self, data_root, tmp_path):
        archive = tmp_path / 'archive'
        archive.mkdir()
        m = QuotaManager(data_root=data_root, archive_root=archive)
        today = datetime.now(timezone.utc).date().strftime('%Y%m%d')
        d = _populate_raw_iq(data_root, 'WWV_10000', today)
        result = m.archive_raw()
        assert result['dirs_archived'] == 0
        assert d.exists()


# =============================================================================
# enforce_quota
# =============================================================================


class TestEnforceQuota:
    def test_no_action_when_under_threshold(self, manager):
        # Stub usage to 50% with a 75% threshold
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 50_000_000
            mock_du.return_value.total = 100_000_000
            mock_du.return_value.free = 50_000_000
            result = manager.enforce_quota()
        assert result['initial_usage_percent'] == pytest.approx(50.0)
        assert result['files_deleted'] == 0

    def test_evicts_when_over_threshold(self, data_root):
        m = QuotaManager(data_root=data_root, threshold_percent=75.0,
                         min_days_to_keep=2)
        # Populate with old products + phase2 + raw
        old = _old_date(30)
        _populate_products(data_root, 'WWV_10000', old)
        _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        _populate_raw_iq(data_root, 'WWV_10000', old)

        # Stub usage to 90% so eviction kicks in
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 90_000_000
            mock_du.return_value.total = 100_000_000
            mock_du.return_value.free = 10_000_000
            result = m.enforce_quota()
        # At least the products entry got removed
        assert result['files_deleted'] >= 1


# =============================================================================
# get_storage_inventory / get_status
# =============================================================================


class TestGetStorageInventory:
    def test_empty_returns_zero_days(self, manager):
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 0
            mock_du.return_value.total = 100_000_000
            mock_du.return_value.free = 100_000_000
            inv = manager.get_storage_inventory()
        assert inv['total_days'] == 0
        assert inv['oldest_date'] is None
        assert inv['newest_date'] is None
        for cat in ('raw_iq', 'phase2', 'products', 'vtec'):
            assert inv['categories'][cat]['days'] == 0

    def test_aggregates_dates_across_categories(self, manager, data_root):
        old = _old_date(30)
        # Use a recent post-2025 date to satisfy the >= 20250101 filter
        _populate_phase2(data_root, 'WWV_10000', 'tm', old)
        _populate_raw_iq(data_root, 'WWV_10000', old)
        _populate_vtec(data_root, old)

        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 50
            mock_du.return_value.total = 1000
            mock_du.return_value.free = 950
            inv = manager.get_storage_inventory()
        assert inv['total_days'] == 1
        assert inv['categories']['raw_iq']['days'] == 1
        assert inv['categories']['phase2']['days'] == 1
        assert inv['categories']['vtec']['days'] == 1
        assert inv['categories']['products']['days'] == 0


class TestGetStatus:
    def test_status_shape(self, manager):
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 50
            mock_du.return_value.total = 100
            mock_du.return_value.free = 50
            s = manager.get_status()
        for key in ('data_root', 'disk_usage_percent', 'threshold_percent',
                    'over_threshold', 'min_days_to_keep', 'protected_dates',
                    'deletable_entries', 'deletable_by_category'):
            assert key in s

    def test_over_threshold_flag(self, manager):
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 90
            mock_du.return_value.total = 100
            mock_du.return_value.free = 10
            s = manager.get_status()
        assert s['over_threshold'] is True

    def test_status_serializable_to_json(self, manager):
        # The CLI prints this dict via json.dumps
        with patch('hf_timestd.quota_manager.shutil.disk_usage') as mock_du:
            mock_du.return_value.used = 10
            mock_du.return_value.total = 100
            mock_du.return_value.free = 90
            s = manager.get_status()
        # Round-trip without raising
        json.dumps(s)
