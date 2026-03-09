#!/usr/bin/env python3
"""
Resource Guardian — ensures hf-timestd never crashes the host system.

Design principle: hf-timestd must be a good citizen.  It may refuse to
start if resources are insufficient, and it will shed load gracefully
when resources run low, but it will NEVER fill the disk or exhaust
memory to the point of crashing itself or the host.

Three layers of protection:

1. **Preflight check** — called once at startup.  Verifies minimum disk
   space and memory before any processing begins.  If the check fails
   the service logs a CRITICAL message and exits cleanly (systemd will
   back off via RestartSec / StartLimitBurst).

2. **Runtime watchdog** — called periodically (default: every 60 s) from
   the service main loop.  Returns a ``ResourceStatus`` indicating
   whether the service should continue normally, shed load, or stop.

3. **Storage janitor** — called by the watchdog when disk usage exceeds
   the configured threshold.  Removes the oldest date-stamped HDF5
   files and raw IQ buffers across ALL managed directories, not just
   the caller's own files.  Respects a minimum retention period.

Managed directories (all under DATA_ROOT):
    raw_buffer/     IQ binary archive         ~35 GB/day   keep 2 days
    phase2/         HDF5 data products         ~2 GB/day   keep 30 days
    upload/         SFTP upload staging queue   variable    keep 7 days
    data/           GNSS VTEC CSVs             ~50 MB/day  keep 30 days
    products/       Derived products            small       keep 90 days

Minimum system requirements (checked at preflight):
    Disk:   50 GB free
    RAM:    4 GB available (for 9 channels)

Configuration lives in [resource_guardian] section of timestd-config.toml.
"""

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — minimum requirements
# ---------------------------------------------------------------------------
GB = 1024 ** 3
MB = 1024 ** 2

MIN_DISK_FREE_BYTES = 50 * GB       # Preflight: refuse to start below this
MIN_RAM_AVAILABLE_BYTES = 4 * GB     # Preflight: refuse to start below this

# Runtime thresholds (disk)
DISK_WARN_FREE_BYTES = 20 * GB       # Watchdog: start shedding load
DISK_CRITICAL_FREE_BYTES = 5 * GB    # Watchdog: stop processing, cleanup only
DISK_EMERGENCY_FREE_BYTES = 1 * GB   # Watchdog: emergency — stop everything

# Runtime thresholds (memory)
RAM_WARN_AVAILABLE_BYTES = 2 * GB    # Watchdog: log warning
RAM_CRITICAL_AVAILABLE_BYTES = 1 * GB  # Watchdog: stop accepting new work

# Date-stamped filename pattern: *_YYYYMMDD.h5 or YYYYMMDD directory
DATE_PATTERN = re.compile(r'(\d{8})')

# ---------------------------------------------------------------------------
# Storage retention policies (days)
# ---------------------------------------------------------------------------
DEFAULT_RETENTION = {
    'raw_buffer': 2,     # IQ archive — large, only need 2 days
    'phase2': 30,        # HDF5 data products
    'upload': 7,         # Upload staging
    'data': 30,          # GNSS VTEC
    'products': 90,      # Derived products
}

# Priority order for cleanup — most expendable first
CLEANUP_PRIORITY = ['raw_buffer', 'upload', 'phase2', 'data', 'products']


class ResourceState(Enum):
    """Operational state returned by the watchdog."""
    OK = 'ok'                # All clear — normal operation
    WARN = 'warn'            # Resources getting low — log, continue
    SHED_LOAD = 'shed_load'  # Reduce work: skip non-essential processing
    STOP = 'stop'            # Critical — stop processing, cleanup only
    EMERGENCY = 'emergency'  # Disk nearly full — cease all writes


@dataclass
class ResourceStatus:
    """Snapshot of system resource state."""
    state: ResourceState
    disk_free_bytes: int
    disk_total_bytes: int
    disk_used_percent: float
    ram_available_bytes: int
    ram_total_bytes: int
    message: str = ''
    bytes_cleaned: int = 0


@dataclass
class RetentionPolicy:
    """Per-directory retention configuration."""
    path: Path
    max_age_days: int
    # Patterns to match for cleanup: glob patterns for files/dirs
    # If empty, uses date-stamped directory cleanup (YYYYMMDD dirs)
    file_patterns: List[str] = field(default_factory=list)


class ResourceGuardian:
    """Unified resource management for all hf-timestd services.

    Usage::

        guardian = ResourceGuardian(data_root='/var/lib/timestd')

        # At startup:
        if not guardian.preflight_check():
            sys.exit(1)

        # In main loop (every 60s):
        status = guardian.watchdog_check()
        if status.state == ResourceState.STOP:
            break
        if status.state == ResourceState.SHED_LOAD:
            skip_non_essential = True
    """

    def __init__(
        self,
        data_root: str = '/var/lib/timestd',
        log_dir: str = '/var/log/hf-timestd',
        retention_days: Optional[Dict[str, int]] = None,
        disk_quota_percent: float = 85.0,
        min_disk_free_gb: float = 50.0,
        min_ram_available_gb: float = 4.0,
    ):
        self.data_root = Path(data_root)
        self.log_dir = Path(log_dir)
        self.disk_quota_percent = disk_quota_percent
        self.min_disk_free_bytes = int(min_disk_free_gb * GB)
        self.min_ram_available_bytes = int(min_ram_available_gb * GB)

        # Merge user-supplied retention with defaults
        self.retention = dict(DEFAULT_RETENTION)
        if retention_days:
            self.retention.update(retention_days)

        # Build retention policies
        self.policies: List[RetentionPolicy] = []
        for dirname in CLEANUP_PRIORITY:
            dirpath = self.data_root / dirname
            if dirname == 'phase2':
                # phase2 has per-channel subdirectories with dated HDF5 files
                self.policies.append(RetentionPolicy(
                    path=dirpath,
                    max_age_days=self.retention[dirname],
                    file_patterns=['*_????????.h5'],
                ))
            elif dirname == 'raw_buffer':
                # raw_buffer has per-channel dirs with YYYYMMDD subdirs
                self.policies.append(RetentionPolicy(
                    path=dirpath,
                    max_age_days=self.retention[dirname],
                    file_patterns=[],  # directory-based cleanup
                ))
            else:
                self.policies.append(RetentionPolicy(
                    path=dirpath,
                    max_age_days=self.retention[dirname],
                    file_patterns=['*.h5', '*.csv', '*.json'],
                ))

        self._last_watchdog_time = 0.0
        self._watchdog_interval = 60.0  # seconds

    # ------------------------------------------------------------------
    # 1. Preflight check
    # ------------------------------------------------------------------

    def preflight_check(self) -> bool:
        """Verify minimum resources before starting.

        Returns True if resources are sufficient, False if the service
        should refuse to start.
        """
        ok = True

        # Check disk
        try:
            stat = shutil.disk_usage(self.data_root)
            free_gb = stat.free / GB
            used_pct = (stat.used / stat.total) * 100

            if stat.free < self.min_disk_free_bytes:
                logger.critical(
                    f"PREFLIGHT FAIL: Disk has {free_gb:.1f} GB free, "
                    f"minimum is {self.min_disk_free_bytes / GB:.0f} GB. "
                    f"Free space on {self.data_root} before starting."
                )
                ok = False
            else:
                logger.info(
                    f"Preflight disk: {free_gb:.1f} GB free "
                    f"({used_pct:.1f}% used) — OK"
                )
        except OSError as e:
            logger.critical(f"PREFLIGHT FAIL: Cannot check disk: {e}")
            ok = False

        # Check RAM
        ram_available = self._get_ram_available()
        if ram_available is not None:
            ram_gb = ram_available / GB
            if ram_available < self.min_ram_available_bytes:
                logger.critical(
                    f"PREFLIGHT FAIL: {ram_gb:.1f} GB RAM available, "
                    f"minimum is {self.min_ram_available_bytes / GB:.0f} GB. "
                    f"Close other applications or add RAM."
                )
                ok = False
            else:
                logger.info(f"Preflight RAM: {ram_gb:.1f} GB available — OK")
        else:
            logger.warning("Preflight: could not check RAM (non-Linux?)")

        # Check data directories exist and are writable
        for dirname in ['raw_buffer', 'phase2', 'state']:
            dirpath = self.data_root / dirname
            if not dirpath.exists():
                logger.warning(f"Preflight: {dirpath} does not exist, creating")
                try:
                    dirpath.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    logger.critical(f"PREFLIGHT FAIL: Cannot create {dirpath}: {e}")
                    ok = False
            elif not os.access(dirpath, os.W_OK):
                logger.critical(f"PREFLIGHT FAIL: {dirpath} is not writable")
                ok = False

        if ok:
            logger.info("Preflight check PASSED")
        else:
            logger.critical(
                "Preflight check FAILED — service will not start. "
                "See messages above for required actions."
            )

        return ok

    # ------------------------------------------------------------------
    # 2. Runtime watchdog
    # ------------------------------------------------------------------

    def watchdog_check(self, force: bool = False) -> ResourceStatus:
        """Periodic resource check.  Call from the main loop.

        Returns a ResourceStatus with the recommended operational state.
        If not enough time has passed since the last check, returns OK
        without re-checking (unless *force* is True).
        """
        now = time.monotonic()
        if not force and (now - self._last_watchdog_time) < self._watchdog_interval:
            return ResourceStatus(
                state=ResourceState.OK,
                disk_free_bytes=0,
                disk_total_bytes=0,
                disk_used_percent=0.0,
                ram_available_bytes=0,
                ram_total_bytes=0,
                message='skipped (interval)',
            )
        self._last_watchdog_time = now

        # Disk
        try:
            stat = shutil.disk_usage(self.data_root)
            disk_free = stat.free
            disk_total = stat.total
            disk_used_pct = (stat.used / stat.total) * 100
        except OSError:
            disk_free = 0
            disk_total = 1
            disk_used_pct = 100.0

        # RAM
        ram_available = self._get_ram_available() or 0
        ram_total = self._get_ram_total() or 1

        # Determine state
        state = ResourceState.OK
        message = ''
        bytes_cleaned = 0

        if disk_free < DISK_EMERGENCY_FREE_BYTES:
            state = ResourceState.EMERGENCY
            message = (
                f"EMERGENCY: {disk_free / MB:.0f} MB disk free — "
                f"all writes suspended"
            )
            logger.critical(message)
        elif disk_free < DISK_CRITICAL_FREE_BYTES:
            state = ResourceState.STOP
            message = (
                f"CRITICAL: {disk_free / GB:.1f} GB disk free — "
                f"stopping processing, running emergency cleanup"
            )
            logger.error(message)
            bytes_cleaned = self._emergency_cleanup(disk_free)
        elif disk_free < DISK_WARN_FREE_BYTES:
            state = ResourceState.SHED_LOAD
            message = (
                f"WARNING: {disk_free / GB:.1f} GB disk free — "
                f"shedding load, running cleanup"
            )
            logger.warning(message)
            bytes_cleaned = self._routine_cleanup()
        elif disk_used_pct >= self.disk_quota_percent:
            state = ResourceState.WARN
            message = (
                f"Disk at {disk_used_pct:.1f}% (quota {self.disk_quota_percent}%) "
                f"— running routine cleanup"
            )
            logger.info(message)
            bytes_cleaned = self._routine_cleanup()

        # RAM checks (advisory — can't directly free Python memory)
        if ram_available < RAM_CRITICAL_AVAILABLE_BYTES:
            if state.value < ResourceState.SHED_LOAD.value:
                state = ResourceState.SHED_LOAD
            message += (
                f" | RAM critical: {ram_available / GB:.1f} GB available"
            )
            logger.warning(f"RAM critical: {ram_available / MB:.0f} MB available")
        elif ram_available < RAM_WARN_AVAILABLE_BYTES:
            if state == ResourceState.OK:
                state = ResourceState.WARN
            logger.info(f"RAM low: {ram_available / GB:.1f} GB available")

        if bytes_cleaned > 0:
            message += f" | cleaned {bytes_cleaned / MB:.0f} MB"
            logger.info(f"Storage janitor freed {bytes_cleaned / MB:.0f} MB")

        return ResourceStatus(
            state=state,
            disk_free_bytes=disk_free,
            disk_total_bytes=disk_total,
            disk_used_percent=disk_used_pct,
            ram_available_bytes=ram_available,
            ram_total_bytes=ram_total,
            message=message,
            bytes_cleaned=bytes_cleaned,
        )

    # ------------------------------------------------------------------
    # 3. Storage janitor
    # ------------------------------------------------------------------

    def _routine_cleanup(self) -> int:
        """Remove expired files/dirs according to retention policies."""
        total_freed = 0
        for policy in self.policies:
            freed = self._enforce_retention(policy)
            total_freed += freed
        return total_freed

    def _emergency_cleanup(self, current_free: int) -> int:
        """Aggressive cleanup: halve retention periods, then remove oldest."""
        total_freed = 0
        target_free = DISK_WARN_FREE_BYTES  # Try to get back to WARN level

        for policy in self.policies:
            if current_free + total_freed >= target_free:
                break
            # First pass: enforce normal retention
            freed = self._enforce_retention(policy)
            total_freed += freed

        if current_free + total_freed < target_free:
            # Second pass: halve retention periods
            logger.warning("Emergency: halving retention periods")
            for policy in self.policies:
                if current_free + total_freed >= target_free:
                    break
                emergency_policy = RetentionPolicy(
                    path=policy.path,
                    max_age_days=max(1, policy.max_age_days // 2),
                    file_patterns=policy.file_patterns,
                )
                freed = self._enforce_retention(emergency_policy)
                total_freed += freed

        return total_freed

    def _enforce_retention(self, policy: RetentionPolicy) -> int:
        """Remove data older than the retention period for one policy."""
        if not policy.path.exists():
            return 0

        cutoff_ts = time.time() - (policy.max_age_days * 86400)
        cutoff_date = time.strftime('%Y%m%d', time.gmtime(cutoff_ts))
        total_freed = 0

        if not policy.file_patterns:
            # Directory-based cleanup: raw_buffer/CHANNEL/YYYYMMDD/
            total_freed += self._cleanup_dated_dirs(policy.path, cutoff_date)
        else:
            # File-based cleanup: phase2/CHANNEL/*_YYYYMMDD.h5
            total_freed += self._cleanup_dated_files(
                policy.path, cutoff_date, policy.file_patterns
            )

        return total_freed

    def _cleanup_dated_dirs(self, base_path: Path, cutoff_date: str) -> int:
        """Remove YYYYMMDD directories older than cutoff across all channels."""
        total_freed = 0
        try:
            for channel_dir in sorted(base_path.iterdir()):
                if not channel_dir.is_dir():
                    continue
                for date_dir in sorted(channel_dir.iterdir()):
                    if not date_dir.is_dir():
                        continue
                    m = DATE_PATTERN.fullmatch(date_dir.name)
                    if m and m.group(1) < cutoff_date:
                        size = self._dir_size(date_dir)
                        try:
                            shutil.rmtree(date_dir)
                            total_freed += size
                            logger.info(
                                f"Retention cleanup: removed {date_dir} "
                                f"({size / MB:.0f} MB)"
                            )
                        except OSError as e:
                            logger.warning(f"Cannot remove {date_dir}: {e}")
        except OSError as e:
            logger.warning(f"Cannot scan {base_path}: {e}")
        return total_freed

    def _cleanup_dated_files(
        self,
        base_path: Path,
        cutoff_date: str,
        patterns: List[str],
    ) -> int:
        """Remove date-stamped files older than cutoff."""
        total_freed = 0
        try:
            # Walk all subdirectories (phase2/CHANNEL/subdir/*.h5)
            for root, dirs, files in os.walk(base_path):
                root_path = Path(root)
                for fname in files:
                    matched = any(
                        root_path.joinpath(fname).match(pat)
                        for pat in patterns
                    )
                    if not matched:
                        continue
                    m = DATE_PATTERN.search(fname)
                    if m and m.group(1) < cutoff_date:
                        fpath = root_path / fname
                        try:
                            size = fpath.stat().st_size
                            fpath.unlink()
                            total_freed += size
                            logger.debug(f"Removed expired: {fpath}")
                        except OSError as e:
                            logger.debug(f"Cannot remove {fpath}: {e}")
        except OSError as e:
            logger.warning(f"Cannot scan {base_path}: {e}")
        return total_freed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_ram_available() -> Optional[int]:
        """Get available RAM in bytes from /proc/meminfo (Linux only)."""
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024  # kB -> bytes
        except (OSError, ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _get_ram_total() -> Optional[int]:
        """Get total RAM in bytes from /proc/meminfo (Linux only)."""
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _dir_size(path: Path) -> int:
        """Total size of all files in a directory tree."""
        total = 0
        try:
            for f in path.rglob('*'):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    # ------------------------------------------------------------------
    # Configuration factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_path: str) -> 'ResourceGuardian':
        """Create a ResourceGuardian from timestd-config.toml.

        Reads the [resource_guardian] section if present, otherwise
        uses safe defaults.
        """
        data_root = '/var/lib/timestd'
        log_dir = '/var/log/hf-timestd'
        disk_quota_percent = 85.0
        min_disk_free_gb = 50.0
        min_ram_available_gb = 4.0
        retention_days = None

        try:
            # Simple TOML parsing for the sections we need
            section = None
            with open(config_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('['):
                        section = line.strip('[]').strip()
                        continue
                    if '=' not in line or line.startswith('#'):
                        continue

                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")

                    if section == 'recorder' and key == 'production_data_root':
                        data_root = value
                    elif section == 'logging' and key == 'log_dir':
                        log_dir = value
                    elif section == 'resource_guardian':
                        if key == 'disk_quota_percent':
                            disk_quota_percent = float(value)
                        elif key == 'min_disk_free_gb':
                            min_disk_free_gb = float(value)
                        elif key == 'min_ram_available_gb':
                            min_ram_available_gb = float(value)
                        elif key.startswith('retention_'):
                            # e.g., retention_raw_buffer = 3
                            dirname = key[len('retention_'):]
                            if retention_days is None:
                                retention_days = {}
                            retention_days[dirname] = int(value)

        except (OSError, ValueError) as e:
            logger.warning(
                f"Could not read config {config_path}: {e} — "
                f"using safe defaults"
            )

        return cls(
            data_root=data_root,
            log_dir=log_dir,
            disk_quota_percent=disk_quota_percent,
            min_disk_free_gb=min_disk_free_gb,
            min_ram_available_gb=min_ram_available_gb,
            retention_days=retention_days,
        )
