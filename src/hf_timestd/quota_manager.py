#!/usr/bin/env python3
"""
HF Time Standard Quota Manager

Monitors disk usage and removes oldest data files when usage exceeds threshold.
Designed to run periodically (e.g., every hour via cron or systemd timer).

Default behavior:
- Threshold: 75% disk usage
- Removes oldest NPZ files first, then spectrograms, then DRF data
- Logs all deletions for audit trail
"""

import os
import sys
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Optional
from dataclasses import dataclass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class FileInfo:
    """Information about a data file for quota management."""
    path: Path
    size_bytes: int
    mtime: float  # Modification time as timestamp
    category: str  # 'npz', 'spectrogram', 'drf', 'csv'


class QuotaManager:
    """Manages disk quota by removing oldest files when threshold exceeded."""
    
    # File removal priority (lowest priority removed first)
    CATEGORY_PRIORITY = {
        'raw_iq': 1,       # Remove raw IQ first (largest size, recoverable from stream)
        'vtec': 2,         # Remove VTEC data next (can redownload or recompute)
        'products': 3,     # Remove legacy products
        'phase2': 4,       # Remove Phase 2 data last (high computation cost)
    }
    
    def __init__(
        self,
        data_root: Path,
        threshold_percent: float = 75.0,
        min_days_to_keep: int = 7,
        dry_run: bool = False
    ):
        """
        Initialize quota manager.
        
        Args:
            data_root: Root directory for hf-timestd data
            threshold_percent: Disk usage threshold (0-100)
            min_days_to_keep: Never delete files newer than this
            dry_run: If True, only log what would be deleted
        """
        self.data_root = Path(data_root)
        self.threshold_percent = threshold_percent
        self.min_days_to_keep = min_days_to_keep
        self.dry_run = dry_run
        
        # Directories to manage
        self.raw_buffer_dir = self.data_root / 'raw_buffer'
        self.phase2_dir = self.data_root / 'phase2'
        self.vtec_dir = self.data_root / 'data' / 'gnss_vtec'
        self.products_dir = self.data_root / 'products'

    def get_disk_usage(self) -> Tuple[int, int, float]:
        """
        Get disk usage for the partition containing data_root.
        
        Returns:
            Tuple of (used_bytes, total_bytes, percent_used)
        """
        stat = shutil.disk_usage(self.data_root)
        percent_used = (stat.used / stat.total) * 100
        return stat.used, stat.total, percent_used
    
    def scan_files(self) -> List[FileInfo]:
        """
        Scan all managed data files.
        
        Returns:
            List of FileInfo objects sorted by priority then age (oldest first)
        """
        files = []
        # Allow cleaning files older than 1 hour if disk is full
        # This ensures we can actually free space if recently created files are filling up the disk
        min_age_seconds = min(self.min_days_to_keep * 86400, 3600)
        cutoff_time = datetime.now().timestamp() - min_age_seconds
        
        # Scan raw_buffer (Binary IQ files) - Highest priority to delete (largest size)
        if self.raw_buffer_dir.exists():
            # Search recursively for .bin and .bin.zst
            for ext in ['*.bin', '*.bin.zst']:
                for f in self.raw_buffer_dir.rglob(ext):
                    if not f.is_file(): continue
                    stat = f.stat()
                    if stat.st_mtime < cutoff_time:
                        files.append(FileInfo(
                            path=f,
                            size_bytes=stat.st_size,
                            mtime=stat.st_mtime,
                            category='raw_iq'
                        ))

        # Scan GNSS VTEC data
        if self.vtec_dir.exists():
            for f in self.vtec_dir.glob('*.h5'):
                if not f.is_file(): continue
                stat = f.stat()
                if stat.st_mtime < cutoff_time:
                    files.append(FileInfo(
                        path=f,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                        category='vtec'
                    ))

        # Scan Phase 2 (HDF5 timing/tones) - Keep longer
        if self.phase2_dir.exists():
            for f in self.phase2_dir.rglob('*.h5'):
                if not f.is_file(): continue
                stat = f.stat()
                if stat.st_mtime < cutoff_time:
                    files.append(FileInfo(
                        path=f,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                        category='phase2'
                    ))

        # Scan Products - Keep longest
        if self.products_dir.exists():
            for f in self.products_dir.rglob('*'):
                if not f.is_file(): continue
                stat = f.stat()
                if stat.st_mtime < cutoff_time:
                    files.append(FileInfo(
                        path=f,
                        size_bytes=stat.st_size,
                        mtime=stat.st_mtime,
                        category='products'
                    ))
        
        # Sort by priority (low first), then by age (oldest first)
        files.sort(key=lambda f: (
            self.CATEGORY_PRIORITY.get(f.category, 99),
            f.mtime
        ))
        
        return files
    
    def delete_file(self, file_info: FileInfo) -> bool:
        """
        Delete a file and log the action.
        
        Returns:
            True if deleted successfully
        """
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete: {file_info.path} "
                           f"({file_info.size_bytes / 1024 / 1024:.1f} MB, "
                           f"category={file_info.category})")
                return True
            else:
                file_info.path.unlink()
                logger.info(f"Deleted: {file_info.path} "
                           f"({file_info.size_bytes / 1024 / 1024:.1f} MB)")
                return True
        except Exception as e:
            logger.error(f"Failed to delete {file_info.path}: {e}")
            return False
    
    def enforce_quota(self) -> dict:
        """
        Check disk usage and delete oldest files if over threshold.
        
        Returns:
            Dictionary with summary of actions taken
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
        
        # Need to free space
        target_percent = self.threshold_percent - 5  # Free to 5% below threshold
        target_bytes = int(total * (target_percent / 100))
        bytes_to_free = used - target_bytes
        
        logger.info(f"Need to free {bytes_to_free / 1024 / 1024 / 1024:.2f} GB "
                   f"to reach {target_percent:.1f}%")
        
        # Get files sorted by deletion priority
        files = self.scan_files()
        
        if not files:
            logger.warning(f"No files older than {self.min_days_to_keep} days to delete")
            return result
        
        logger.info(f"Found {len(files)} files eligible for deletion")
        
        # Delete files until we're under target
        bytes_freed = 0
        files_deleted = 0
        
        for file_info in files:
            if bytes_freed >= bytes_to_free:
                break
            
            if self.delete_file(file_info):
                bytes_freed += file_info.size_bytes
                files_deleted += 1
        
        # Get final usage
        if not self.dry_run:
            _, _, final_percent = self.get_disk_usage()
            result['final_usage_percent'] = final_percent
        else:
            # Estimate for dry run
            result['final_usage_percent'] = ((used - bytes_freed) / total) * 100
        
        result['files_deleted'] = files_deleted
        result['bytes_freed'] = bytes_freed
        
        logger.info(f"{'Would free' if self.dry_run else 'Freed'} "
                   f"{bytes_freed / 1024 / 1024 / 1024:.2f} GB "
                   f"by deleting {files_deleted} files")
        
        return result
    
    def get_status(self) -> dict:
        """Get current quota status without making changes."""
        used, total, percent = self.get_disk_usage()
        files = self.scan_files()
        
        # Categorize files
        by_category = {}
        for f in files:
            if f.category not in by_category:
                by_category[f.category] = {'count': 0, 'size_bytes': 0}
            by_category[f.category]['count'] += 1
            by_category[f.category]['size_bytes'] += f.size_bytes
        
        return {
            'data_root': str(self.data_root),
            'disk_usage_percent': percent,
            'disk_used_gb': used / 1024 / 1024 / 1024,
            'disk_total_gb': total / 1024 / 1024 / 1024,
            'threshold_percent': self.threshold_percent,
            'over_threshold': percent > self.threshold_percent,
            'min_days_to_keep': self.min_days_to_keep,
            'deletable_files': len(files),
            'deletable_by_category': by_category
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
