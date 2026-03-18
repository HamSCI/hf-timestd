#!/usr/bin/env python3
"""
HF Time Standard Quota Manager — Day-Level Circular Buffer

Monitors disk usage and removes the oldest *day-directories* (or day-files)
when usage exceeds a threshold.  Data is organised by date at every level:

  raw_buffer/<channel>/<YYYYMMDD>/  — per-minute .bin.zst files
  phase2/<channel>/<product>/<channel>_<product>_<YYYYMMDD>.h5
  phase2/science/<product>/<name>_<YYYYMMDD>.h5
  phase2/fusion/<name>_<YYYYMMDD>.h5
  data/gnss_vtec/GNSS_gnss_vtec_<YYYYMMDD>.h5
  products/<channel>/decimated/<YYYYMMDD>.bin  (+ spectrograms, etc.)

Deletion operates on whole days in priority order (raw_iq first, phase2 last).
Within a priority tier the globally oldest date is removed first.

Never deletes today or yesterday — GRAPE daily processing at 01:01 UTC needs
yesterday's raw data.  The min_days_to_keep parameter adds a further guard.
"""

import os
import re
import sys
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Regex for an 8-digit date that looks like YYYYMMDD (20xx or 19xx)
_DATE_RE = re.compile(r'((?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))')


@dataclass
class DayEntry:
    """One deletable unit — either a directory tree or a single file."""
    path: Path
    date_str: str          # YYYYMMDD
    category: str          # 'raw_iq', 'phase2', 'products', 'vtec'
    is_dir: bool           # True → shutil.rmtree; False → unlink


class QuotaManager:
    """Manages disk quota by removing the oldest day-units when over threshold."""

    # Deletion priority (lowest number deleted first when over quota).
    # Principle: derived data before primary data — we can re-derive but
    # cannot recover deleted raw recordings.
    CATEGORY_PRIORITY = {
        'products': 1,     # GRAPE decimated/spectrograms — regenerable from raw
        'phase2':   2,     # Timing/metrology HDF5 — regenerable from raw
        'vtec':     3,     # GNSS VTEC daily HDF5 — small, re-downloadable
        'raw_iq':   4,     # Primary 24 kHz recordings — NOT recoverable
    }

    def __init__(
        self,
        data_root: Path,
        threshold_percent: float = 75.0,
        min_days_to_keep: int = 7,
        dry_run: bool = False,
        archive_root: Optional[Path] = None,
    ):
        self.data_root = Path(data_root)
        self.threshold_percent = threshold_percent
        self.min_days_to_keep = min_days_to_keep
        self.dry_run = dry_run
        self.archive_root = Path(archive_root) if archive_root else None

        # Managed directories
        self.raw_buffer_dir = self.data_root / 'raw_buffer'
        self.phase2_dir = self.data_root / 'phase2'
        self.vtec_dir = self.data_root / 'data' / 'gnss_vtec'
        self.products_dir = self.data_root / 'products'

    # ------------------------------------------------------------------
    # Disk helpers
    # ------------------------------------------------------------------

    def get_disk_usage(self) -> Tuple[int, int, float]:
        """Return (used_bytes, total_bytes, percent_used)."""
        stat = shutil.disk_usage(self.data_root)
        percent_used = (stat.used / stat.total) * 100
        return stat.used, stat.total, percent_used

    # ------------------------------------------------------------------
    # Day-entry discovery
    # ------------------------------------------------------------------

    def _protected_dates(self) -> set:
        """Dates that must never be deleted (today, yesterday, + guard)."""
        today = datetime.now(timezone.utc).date()
        protected = set()
        # Always protect today and yesterday (GRAPE needs yesterday at 01:01)
        for delta in range(max(2, self.min_days_to_keep)):
            d = today - timedelta(days=delta)
            protected.add(d.strftime('%Y%m%d'))
        return protected

    def _scan_raw_iq(self, protected: set) -> List[DayEntry]:
        """raw_buffer/<channel>/<YYYYMMDD>/ — whole day-directories."""
        entries = []
        if not self.raw_buffer_dir.exists():
            return entries
        for channel_dir in self.raw_buffer_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            for day_dir in channel_dir.iterdir():
                if not day_dir.is_dir():
                    continue
                name = day_dir.name
                if not _DATE_RE.fullmatch(name):
                    continue
                if name in protected:
                    continue
                entries.append(DayEntry(
                    path=day_dir, date_str=name,
                    category='raw_iq', is_dir=True
                ))
        return entries

    def _scan_phase2(self, protected: set) -> List[DayEntry]:
        """
        phase2 HDF5 files with date embedded in filename.

        Structure varies:
          phase2/<channel>/<product>/<ch>_<prod>_YYYYMMDD.h5
          phase2/science/<product>/<name>_YYYYMMDD.h5
          phase2/fusion/<name>_YYYYMMDD.h5
        We scan for any .h5 whose name contains a YYYYMMDD date.
        """
        entries = []
        if not self.phase2_dir.exists():
            return entries
        for f in self.phase2_dir.rglob('*.h5'):
            if not f.is_file():
                continue
            m = _DATE_RE.search(f.stem)
            if not m:
                continue
            date_str = m.group(1)
            if date_str in protected:
                continue
            entries.append(DayEntry(
                path=f, date_str=date_str,
                category='phase2', is_dir=False
            ))
        return entries

    def _scan_products(self, protected: set) -> List[DayEntry]:
        """products/<channel>/{decimated,spectrograms}/ — files with date in name."""
        entries = []
        if not self.products_dir.exists():
            return entries
        for f in self.products_dir.rglob('*'):
            if not f.is_file():
                continue
            m = _DATE_RE.search(f.stem)
            if not m:
                continue
            date_str = m.group(1)
            if date_str in protected:
                continue
            entries.append(DayEntry(
                path=f, date_str=date_str,
                category='products', is_dir=False
            ))
        return entries

    def _scan_vtec(self, protected: set) -> List[DayEntry]:
        """data/gnss_vtec/GNSS_gnss_vtec_YYYYMMDD.h5"""
        entries = []
        if not self.vtec_dir.exists():
            return entries
        for f in self.vtec_dir.glob('*.h5'):
            if not f.is_file():
                continue
            m = _DATE_RE.search(f.stem)
            if not m:
                continue
            date_str = m.group(1)
            if date_str in protected:
                continue
            entries.append(DayEntry(
                path=f, date_str=date_str,
                category='vtec', is_dir=False
            ))
        return entries

    def scan_day_entries(self) -> List[DayEntry]:
        """
        Collect all deletable day-entries, sorted by priority then date.

        O(channels × dates) for raw_iq, O(files) for phase2/products/vtec
        but phase2 rglob is unavoidable since files aren't in date-dirs.
        """
        protected = self._protected_dates()
        entries = []
        entries.extend(self._scan_raw_iq(protected))
        entries.extend(self._scan_products(protected))
        entries.extend(self._scan_vtec(protected))
        entries.extend(self._scan_phase2(protected))

        # Sort: lowest priority number first, then oldest date first
        entries.sort(key=lambda e: (
            self.CATEGORY_PRIORITY.get(e.category, 99),
            e.date_str
        ))
        return entries

    # ------------------------------------------------------------------
    # Archive helpers
    # ------------------------------------------------------------------

    def _archive_is_mounted(self) -> bool:
        """Check if the archive root is writable."""
        if not self.archive_root:
            return False
        try:
            return self.archive_root.is_dir() and os.access(self.archive_root, os.W_OK)
        except OSError:
            return False

    def _archive_entry(self, entry: DayEntry) -> int:
        """Move a day-entry to the archive drive.  Returns bytes freed on primary."""
        try:
            rel = entry.path.relative_to(self.data_root)
            dst = self.archive_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            if entry.is_dir:
                size = sum(
                    f.stat().st_size for f in entry.path.rglob('*') if f.is_file()
                )
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would archive dir: {entry.path} -> {dst} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                else:
                    shutil.move(str(entry.path), str(dst))
                    logger.info(f"Archived dir: {entry.path} -> {dst} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
            else:
                size = entry.path.stat().st_size
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would archive: {entry.path} -> {dst} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                else:
                    shutil.move(str(entry.path), str(dst))
                    logger.info(f"Archived: {entry.path} -> {dst} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
            return size
        except Exception as e:
            logger.warning(f"Archive failed for {entry.path}: {e}, falling back to delete")
            return self._force_delete_entry(entry)

    def _force_delete_entry(self, entry: DayEntry) -> int:
        """Unconditionally delete a day-entry (fallback when archive fails)."""
        try:
            if entry.is_dir:
                size = sum(
                    f.stat().st_size for f in entry.path.rglob('*') if f.is_file()
                )
                shutil.rmtree(entry.path)
            else:
                size = entry.path.stat().st_size
                entry.path.unlink()
            logger.info(f"Deleted (fallback): {entry.path} ({size / 1024**2:.1f} MB)")
            return size
        except Exception as e:
            logger.error(f"Failed to remove {entry.path}: {e}")
            return 0

    # ------------------------------------------------------------------
    # Deletion / archival
    # ------------------------------------------------------------------

    def _delete_entry(self, entry: DayEntry) -> int:
        """
        Remove a day-entry from primary storage.

        If an archive drive is configured and mounted, the entry is moved
        there instead of being deleted.  Falls back to deletion if the
        archive move fails.
        """
        if self.archive_root and self._archive_is_mounted():
            return self._archive_entry(entry)

        try:
            if entry.is_dir:
                # Estimate size before removal
                size = sum(
                    f.stat().st_size for f in entry.path.rglob('*') if f.is_file()
                )
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would remove dir: {entry.path} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                else:
                    shutil.rmtree(entry.path)
                    logger.info(f"Removed dir: {entry.path} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                return size
            else:
                size = entry.path.stat().st_size
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would delete: {entry.path} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                else:
                    entry.path.unlink()
                    logger.info(f"Deleted: {entry.path} "
                               f"({size / 1024**2:.1f} MB, {entry.category})")
                return size
        except Exception as e:
            logger.error(f"Failed to remove {entry.path}: {e}")
            return 0

    # ------------------------------------------------------------------
    # Public API (unchanged from previous version)
    # ------------------------------------------------------------------

    def enforce_quota(self) -> dict:
        """
        Check disk usage and delete oldest day-entries if over threshold.

        Returns dict compatible with the previous file-level QuotaManager.
        """
        used, total, percent = self.get_disk_usage()

        result = {
            'initial_usage_percent': percent,
            'threshold_percent': self.threshold_percent,
            'files_deleted': 0,
            'bytes_freed': 0,
            'final_usage_percent': percent,
            'dry_run': self.dry_run
        }

        logger.info(f"Disk usage: {percent:.1f}% (threshold: {self.threshold_percent}%)")

        if percent <= self.threshold_percent:
            logger.info("Disk usage within threshold, no action needed")
            return result

        target_percent = self.threshold_percent - 5  # headroom
        target_bytes = int(total * (target_percent / 100))
        bytes_to_free = used - target_bytes

        logger.info(f"Need to free {bytes_to_free / 1024**3:.2f} GB "
                   f"to reach {target_percent:.1f}%")

        entries = self.scan_day_entries()

        if not entries:
            logger.warning("No day-entries eligible for deletion")
            return result

        logger.info(f"Found {len(entries)} deletable day-entries")

        bytes_freed = 0
        items_deleted = 0

        for entry in entries:
            if bytes_freed >= bytes_to_free:
                break
            freed = self._delete_entry(entry)
            bytes_freed += freed
            items_deleted += 1

        if not self.dry_run:
            _, _, final_percent = self.get_disk_usage()
            result['final_usage_percent'] = final_percent
        else:
            result['final_usage_percent'] = ((used - bytes_freed) / total) * 100

        result['files_deleted'] = items_deleted
        result['bytes_freed'] = bytes_freed

        logger.info(f"{'Would free' if self.dry_run else 'Freed'} "
                   f"{bytes_freed / 1024**3:.2f} GB "
                   f"by removing {items_deleted} day-entries")

        return result

    def get_storage_inventory(self) -> dict:
        """
        Full inventory of all dates in storage, by category.

        Unlike scan_day_entries() this includes protected dates and reports
        the complete set of available dates — useful for knowing exactly
        which days of raw IQ can be retrieved for event analysis.
        """
        used, total, percent = self.get_disk_usage()

        # --- raw_iq: channel/<YYYYMMDD>/ dirs (only if binary data exists) ---
        raw_iq_dates: set = set()
        if self.raw_buffer_dir.exists():
            for channel_dir in self.raw_buffer_dir.iterdir():
                if not channel_dir.is_dir():
                    continue
                for day_dir in channel_dir.iterdir():
                    if not day_dir.is_dir() or not _DATE_RE.fullmatch(day_dir.name):
                        continue
                    # Only count if at least one .bin* file exists (not just .json)
                    has_binary = any(day_dir.glob('*.bin*'))
                    if has_binary:
                        raw_iq_dates.add(day_dir.name)

        # --- phase2: files with YYYYMMDD in name ---
        phase2_dates: set = set()
        if self.phase2_dir.exists():
            for f in self.phase2_dir.rglob('*.h5'):
                m = _DATE_RE.search(f.stem)
                if m:
                    phase2_dates.add(m.group(1))

        # --- products: files with YYYYMMDD in name ---
        products_dates: set = set()
        if self.products_dir.exists():
            for f in self.products_dir.rglob('*'):
                if f.is_file():
                    m = _DATE_RE.search(f.stem)
                    if m:
                        products_dates.add(m.group(1))

        # --- vtec ---
        vtec_dates: set = set()
        if self.vtec_dir.exists():
            for f in self.vtec_dir.glob('*.h5'):
                m = _DATE_RE.search(f.stem)
                if m:
                    vtec_dates.add(m.group(1))

        # Union of all dates
        all_dates = raw_iq_dates | phase2_dates | products_dates | vtec_dates
        # Filter out bogus dates (e.g. 19700101)
        valid_dates = sorted(d for d in all_dates if d >= '20250101')

        def _category_summary(dates: set) -> dict:
            valid = sorted(d for d in dates if d >= '20250101')
            return {
                'days': len(valid),
                'oldest': valid[0] if valid else None,
                'newest': valid[-1] if valid else None,
                'dates': valid,
            }

        protected = sorted(self._protected_dates())

        return {
            'disk_usage_percent': round(percent, 1),
            'disk_used_gb': round(used / 1024**3, 1),
            'disk_total_gb': round(total / 1024**3, 1),
            'disk_free_gb': round((total - used) / 1024**3, 1),
            'threshold_percent': self.threshold_percent,
            'over_threshold': percent > self.threshold_percent,
            'total_days': len(valid_dates),
            'oldest_date': valid_dates[0] if valid_dates else None,
            'newest_date': valid_dates[-1] if valid_dates else None,
            'protected_dates': protected,
            'categories': {
                'raw_iq': _category_summary(raw_iq_dates),
                'phase2': _category_summary(phase2_dates),
                'products': _category_summary(products_dates),
                'vtec': _category_summary(vtec_dates),
            },
        }

    def get_status(self) -> dict:
        """Get current quota status without making changes."""
        used, total, percent = self.get_disk_usage()
        entries = self.scan_day_entries()

        by_category: Dict[str, dict] = {}
        for e in entries:
            if e.category not in by_category:
                by_category[e.category] = {'count': 0, 'dates': set()}
            by_category[e.category]['count'] += 1
            by_category[e.category]['dates'].add(e.date_str)

        # Summarise for JSON serialisation
        summary = {}
        for cat, info in by_category.items():
            summary[cat] = {
                'entries': info['count'],
                'unique_dates': len(info['dates']),
                'oldest_date': min(info['dates']) if info['dates'] else None,
            }

        return {
            'data_root': str(self.data_root),
            'disk_usage_percent': percent,
            'disk_used_gb': used / 1024**3,
            'disk_total_gb': total / 1024**3,
            'threshold_percent': self.threshold_percent,
            'over_threshold': percent > self.threshold_percent,
            'min_days_to_keep': self.min_days_to_keep,
            'protected_dates': sorted(self._protected_dates()),
            'deletable_entries': len(entries),
            'deletable_by_category': summary,
        }


def main():
    parser = argparse.ArgumentParser(
        description='HF Time Standard Quota Manager - Enforce disk space limits'
    )
    parser.add_argument(
        '--data-root',
        type=Path,
        default=Path.home() / 'timestd-data',
        help='Root directory for hf-timestd data'
    )
    parser.add_argument(
        '--threshold',
        type=float,
        default=75.0,
        help='Disk usage threshold percent (default: 75)'
    )
    parser.add_argument(
        '--min-days',
        type=int,
        default=7,
        help='Minimum days to keep files (default: 7)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Only show what would be deleted'
    )
    parser.add_argument(
        '--status',
        action='store_true',
        help='Just show current status, no deletions'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    manager = QuotaManager(
        data_root=args.data_root,
        threshold_percent=args.threshold,
        min_days_to_keep=args.min_days,
        dry_run=args.dry_run
    )
    
    if args.status:
        import json
        status = manager.get_status()
        print(json.dumps(status, indent=2))
    else:
        result = manager.enforce_quota()
        
        if result['files_deleted'] > 0 or args.verbose:
            print(f"\nQuota enforcement complete:")
            print(f"  Initial usage: {result['initial_usage_percent']:.1f}%")
            print(f"  Files deleted: {result['files_deleted']}")
            print(f"  Space freed: {result['bytes_freed'] / 1024 / 1024 / 1024:.2f} GB")
            print(f"  Final usage: {result['final_usage_percent']:.1f}%")
            if result['dry_run']:
                print("  (DRY RUN - no files actually deleted)")


if __name__ == '__main__':
    main()
