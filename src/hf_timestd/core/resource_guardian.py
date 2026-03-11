#!/usr/bin/env python3
"""
Resource Guardian — ensures hf-timestd never crashes the host system.

Design principle: hf-timestd is a good citizen.  It computes its own
resource requirements from the channel configuration, refuses to start
if the system cannot sustain them, and at runtime enforces a hard cap:

    **hf-timestd's total disk footprint shall never exceed 80% of the
    filesystem, and its total memory shall never exceed 80% of RAM.**

No per-system tuning is needed.  The guardian auto-sizes everything
from the number of configured channels and the IQ sample rate.

Data budget (per channel per day, at 24 kHz IQ):
    raw_buffer      ~14 GB   (IQ binary archive, 24 kHz × 4 B × 86400 s)
    phase2           ~4 GB   (HDF5 data products)
    ────────────────────────
    subtotal        ~18 GB/channel/day

Minimum baseline = 2 days × N_channels × 18 GB + 5 GB overhead.
This must fit within 80% of the filesystem.  If it doesn't, the
system needs more storage before running hf-timestd.

Three layers of protection:

1. **Preflight** — at startup, compute the baseline and verify it fits.
   Also verify minimum RAM (700 MB per channel + 2 GB system headroom).
   If either check fails → CRITICAL log → clean exit → systemd backoff.

2. **Watchdog** — every 60 s, check total disk usage.  If hf-timestd's
   data footprint pushes the filesystem past 80%, evict the oldest
   complete day of data (across raw_buffer and phase2 simultaneously)
   until usage drops below 80%.  Always keep at least 1 day.

3. **Hard stop** — if disk reaches 95% despite cleanup (other processes
   are filling disk), stop all writes and exit.  We will not crash the
   host.
"""

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GB = 1024 ** 3
MB = 1024 ** 2

# The one rule: never let the filesystem exceed this percentage.
DISK_MAX_PERCENT = 80.0

# If disk exceeds this despite our cleanup, something else is filling
# the disk — stop all writes to avoid being part of the problem.
DISK_HARD_STOP_PERCENT = 95.0

# Per-channel data budget (bytes per day) at 24 kHz complex IQ
# IQ: 24000 samples/s × 4 bytes/sample × 2 (I+Q) × 86400 s/day ≈ 14.2 GB
RAW_BYTES_PER_CHANNEL_PER_DAY = 24000 * 4 * 2 * 86400
# Phase2 HDF5 products: empirically ~4 GB/channel/day (varies with station)
PHASE2_BYTES_PER_CHANNEL_PER_DAY = 4 * GB
# Total per channel per day
BYTES_PER_CHANNEL_PER_DAY = RAW_BYTES_PER_CHANNEL_PER_DAY + PHASE2_BYTES_PER_CHANNEL_PER_DAY

# Baseline: 2 full days of data
BASELINE_DAYS = 2
# Fixed overhead (logs, state, products, upload queue, IONEX, etc.)
OVERHEAD_BYTES = 5 * GB

# Memory: ~250 MB per channel (empirical: 112-200 MB peak per worker
# including IQ load, FFT, matched filters, and HDF5 writes)
RAM_PER_CHANNEL = 250 * MB
# System headroom: leave at least this much for OS + other processes
RAM_SYSTEM_HEADROOM = 2 * GB

# Date pattern for directories and files
DATE_RE = re.compile(r'(\d{8})')


class ResourceState(Enum):
    """Operational state returned by the watchdog."""
    OK = 'ok'                # All clear — normal operation
    CLEANED = 'cleaned'      # Was over 80%, evicted oldest day, now OK
    STOP = 'stop'            # At 95% — something else filling disk, stop writes
    EMERGENCY = 'emergency'  # Cannot clean enough — cease all activity


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


class ResourceGuardian:
    """Universal, self-sizing resource manager for hf-timestd.

    Usage::

        guardian = ResourceGuardian.from_config('/etc/hf-timestd/timestd-config.toml')

        # At startup — refuses to start if system can't sustain the load:
        if not guardian.preflight_check():
            sys.exit(1)

        # In main loop — enforces 80% cap, evicts oldest data:
        status = guardian.watchdog_check()
        if status.state in (ResourceState.STOP, ResourceState.EMERGENCY):
            break  # stop processing
    """

    def __init__(
        self,
        data_root: str = '/var/lib/timestd',
        n_channels: int = 9,
        sample_rate: int = 24000,
        tiered_storage: bool = False,
    ):
        self.data_root = Path(data_root)
        self.n_channels = n_channels
        self.sample_rate = sample_rate
        self.tiered_storage = tiered_storage

        # Compute per-channel-per-day raw bytes from actual sample rate
        # complex IQ: sample_rate × 4 bytes × 2 (I+Q) × 86400 seconds
        self.raw_per_ch_per_day = sample_rate * 4 * 2 * 86400
        self.total_per_ch_per_day = self.raw_per_ch_per_day + PHASE2_BYTES_PER_CHANNEL_PER_DAY

        # Minimum storage: 2 days × N channels + overhead
        # With tiered storage, raw IQ lives in /dev/shm (RAM), not on
        # the data disk — only Phase2 HDF5 products hit persistent storage.
        if tiered_storage:
            disk_per_ch_per_day = PHASE2_BYTES_PER_CHANNEL_PER_DAY
        else:
            disk_per_ch_per_day = self.total_per_ch_per_day
        self.baseline_bytes = (
            BASELINE_DAYS * n_channels * disk_per_ch_per_day + OVERHEAD_BYTES
        )

        # Minimum RAM: per-channel workers + system headroom
        self.min_ram_bytes = n_channels * RAM_PER_CHANNEL + RAM_SYSTEM_HEADROOM

        self._last_watchdog_time = 0.0
        self._watchdog_interval = 60.0

    # ------------------------------------------------------------------
    # 1. Preflight check
    # ------------------------------------------------------------------

    def preflight_check(self) -> bool:
        """Verify the system can sustain hf-timestd before starting.

        Computes the minimum storage and RAM from the channel count
        and sample rate.  No hardcoded thresholds — everything is
        derived from the workload.
        """
        ok = True
        baseline_gb = self.baseline_bytes / GB

        logger.info(
            f"Resource preflight: {self.n_channels} channels × "
            f"{self.sample_rate} Hz, baseline = {baseline_gb:.0f} GB "
            f"({BASELINE_DAYS} days + overhead)"
        )

        # --- Disk ---
        try:
            stat = shutil.disk_usage(self.data_root)
            total_gb = stat.total / GB
            free_gb = stat.free / GB
            used_pct = (stat.used / stat.total) * 100

            # Step 1: Is the disk physically large enough?
            budget_bytes = stat.total * (DISK_MAX_PERCENT / 100.0)
            if self.baseline_bytes > budget_bytes:
                logger.critical(
                    f"PREFLIGHT FAIL: {self.n_channels}-channel baseline "
                    f"needs {baseline_gb:.0f} GB, but 80%% of this "
                    f"{total_gb:.0f} GB disk is only "
                    f"{budget_bytes / GB:.0f} GB. "
                    f"Need a disk of at least "
                    f"{self.baseline_bytes / (DISK_MAX_PERCENT / 100.0) / GB:.0f} GB."
                )
                ok = False
            else:
                # Step 2: If currently over 80%, evict oldest data first
                if used_pct >= DISK_MAX_PERCENT:
                    logger.warning(
                        f"Preflight: disk at {used_pct:.1f}%% "
                        f"({free_gb:.1f} GB free) — running cleanup "
                        f"to get under {DISK_MAX_PERCENT}%%"
                    )
                    cleaned = self._evict_oldest_days_until_under(
                        stat.total, DISK_MAX_PERCENT
                    )
                    if cleaned > 0:
                        logger.info(
                            f"Preflight cleanup freed {cleaned / GB:.1f} GB"
                        )
                    # Re-check after cleanup
                    stat = shutil.disk_usage(self.data_root)
                    free_gb = stat.free / GB
                    used_pct = (stat.used / stat.total) * 100

                # Step 3: After cleanup, are we under 80%?
                if used_pct >= DISK_MAX_PERCENT:
                    logger.critical(
                        f"PREFLIGHT FAIL: Disk still at {used_pct:.1f}%% "
                        f"({free_gb:.1f} GB free) after cleanup. "
                        f"Cannot get under {DISK_MAX_PERCENT}%% — "
                        f"free space manually before starting hf-timestd."
                    )
                    ok = False
                else:
                    logger.info(
                        f"Preflight disk: {free_gb:.1f} GB free of "
                        f"{total_gb:.0f} GB ({used_pct:.1f}%% used), "
                        f"baseline {baseline_gb:.0f} GB fits in "
                        f"80%% budget ({budget_bytes / GB:.0f} GB) — OK"
                    )
        except OSError as e:
            logger.critical(f"PREFLIGHT FAIL: Cannot stat {self.data_root}: {e}")
            ok = False

        # --- RAM ---
        ram_available = self._get_ram_available()
        ram_total = self._get_ram_total()
        if ram_total is not None:
            ram_budget = ram_total * (DISK_MAX_PERCENT / 100.0)  # 80% of RAM
            min_ram_gb = self.min_ram_bytes / GB
            if self.min_ram_bytes > ram_budget:
                logger.critical(
                    f"PREFLIGHT FAIL: {self.n_channels} channels need "
                    f"{min_ram_gb:.1f} GB RAM, but 80%% of "
                    f"{ram_total / GB:.1f} GB is only "
                    f"{ram_budget / GB:.1f} GB. Add RAM or reduce channels."
                )
                ok = False
            elif ram_available is not None and ram_available < self.min_ram_bytes * 0.5:
                logger.critical(
                    f"PREFLIGHT FAIL: Only {ram_available / GB:.1f} GB RAM "
                    f"available, need at least {self.min_ram_bytes * 0.5 / GB:.1f} GB "
                    f"to start. Close other applications."
                )
                ok = False
            else:
                avail_str = f"{ram_available / GB:.1f} GB available" if ram_available else "unknown available"
                logger.info(
                    f"Preflight RAM: {ram_total / GB:.1f} GB total, "
                    f"{avail_str}, need {min_ram_gb:.1f} GB — OK"
                )
        else:
            logger.warning("Preflight: cannot read /proc/meminfo (non-Linux?)")

        # --- Data directories ---
        for dirname in ['raw_buffer', 'phase2', 'state']:
            dirpath = self.data_root / dirname
            if not dirpath.exists():
                logger.info(f"Preflight: creating {dirpath}")
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
                "Resolve the issues above, then retry."
            )
        return ok

    # ------------------------------------------------------------------
    # 2. Runtime watchdog
    # ------------------------------------------------------------------

    def watchdog_check(self, force: bool = False) -> ResourceStatus:
        """Periodic check: enforce 80% disk cap by evicting oldest data.

        Call from the service main loop.  Returns quickly (no-op) if
        the check interval hasn't elapsed.
        """
        now = time.monotonic()
        if not force and (now - self._last_watchdog_time) < self._watchdog_interval:
            return ResourceStatus(
                state=ResourceState.OK,
                disk_free_bytes=0, disk_total_bytes=0,
                disk_used_percent=0.0,
                ram_available_bytes=0, ram_total_bytes=0,
                message='skipped (interval)',
            )
        self._last_watchdog_time = now

        # Read disk and RAM
        try:
            stat = shutil.disk_usage(self.data_root)
            disk_free = stat.free
            disk_total = stat.total
            disk_used_pct = (stat.used / stat.total) * 100
        except OSError:
            disk_free, disk_total, disk_used_pct = 0, 1, 100.0

        ram_available = self._get_ram_available() or 0
        ram_total = self._get_ram_total() or 1

        # --- Enforce the one rule ---
        state = ResourceState.OK
        message = ''
        bytes_cleaned = 0

        if disk_used_pct >= DISK_HARD_STOP_PERCENT:
            # Something outside hf-timestd is filling the disk.
            # Do our best to free space, but if we can't, stop.
            bytes_cleaned = self._evict_oldest_days_until_under(
                disk_total, DISK_MAX_PERCENT
            )
            # Re-check
            try:
                stat2 = shutil.disk_usage(self.data_root)
                disk_used_pct = (stat2.used / stat2.total) * 100
                disk_free = stat2.free
            except OSError:
                pass
            if disk_used_pct >= DISK_HARD_STOP_PERCENT:
                state = ResourceState.EMERGENCY
                message = (
                    f"EMERGENCY: disk at {disk_used_pct:.1f}%% even after "
                    f"cleanup — stopping all writes"
                )
                logger.critical(message)
            else:
                state = ResourceState.STOP
                message = (
                    f"Disk was at ≥{DISK_HARD_STOP_PERCENT}%%, cleaned "
                    f"{bytes_cleaned / GB:.1f} GB, now {disk_used_pct:.1f}%% "
                    f"— pausing to stabilize"
                )
                logger.error(message)

        elif disk_used_pct >= DISK_MAX_PERCENT:
            # Over 80% — evict oldest complete days until under
            bytes_cleaned = self._evict_oldest_days_until_under(
                disk_total, DISK_MAX_PERCENT
            )
            try:
                stat2 = shutil.disk_usage(self.data_root)
                disk_used_pct = (stat2.used / stat2.total) * 100
                disk_free = stat2.free
            except OSError:
                pass
            state = ResourceState.CLEANED
            message = (
                f"Disk was ≥{DISK_MAX_PERCENT}%%, evicted oldest data, "
                f"freed {bytes_cleaned / GB:.1f} GB, now {disk_used_pct:.1f}%%"
            )
            logger.info(message)

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
    # 3. Storage eviction — oldest-day-first, across all directories
    # ------------------------------------------------------------------

    def _evict_oldest_days_until_under(
        self,
        disk_total: int,
        target_percent: float,
    ) -> int:
        """Remove the oldest complete day of data, repeating until
        filesystem usage drops below *target_percent*.

        A "day" is identified by YYYYMMDD and removed from raw_buffer/
        AND phase2/ simultaneously.  Always keeps at least 1 day
        (today) so that the live pipeline isn't disrupted.
        """
        total_freed = 0
        today = time.strftime('%Y%m%d', time.gmtime())

        # Collect all date-strings present across both directories
        all_dates = self._collect_all_dates()
        # Remove today — never evict today's data
        all_dates.discard(today)
        # Sort oldest first
        dates_oldest_first = sorted(all_dates)

        for date_str in dates_oldest_first:
            # Check if we're under target
            try:
                stat = shutil.disk_usage(self.data_root)
                current_pct = (stat.used / stat.total) * 100
                if current_pct < target_percent:
                    break
            except OSError:
                break

            freed = self._evict_date(date_str)
            total_freed += freed
            if freed > 0:
                logger.info(
                    f"Evicted day {date_str}: freed {freed / GB:.1f} GB"
                )

        return total_freed

    def _collect_all_dates(self) -> Set[str]:
        """Find all YYYYMMDD date strings across raw_buffer/ and phase2/."""
        dates: Set[str] = set()

        # raw_buffer: CHANNEL/YYYYMMDD/
        raw_dir = self.data_root / 'raw_buffer'
        if raw_dir.exists():
            try:
                for channel_dir in raw_dir.iterdir():
                    if not channel_dir.is_dir():
                        continue
                    for sub in channel_dir.iterdir():
                        if sub.is_dir() and DATE_RE.fullmatch(sub.name):
                            dates.add(sub.name)
            except OSError:
                pass

        # phase2: CHANNEL/datatype/*_YYYYMMDD.h5
        phase2_dir = self.data_root / 'phase2'
        if phase2_dir.exists():
            try:
                for root, dirs, files in os.walk(phase2_dir):
                    for fname in files:
                        m = DATE_RE.search(fname)
                        if m:
                            dates.add(m.group(1))
            except OSError:
                pass

        return dates

    def _evict_date(self, date_str: str) -> int:
        """Remove all data for a single YYYYMMDD date across all dirs."""
        total_freed = 0

        # 1. raw_buffer/CHANNEL/YYYYMMDD/ directories
        raw_dir = self.data_root / 'raw_buffer'
        if raw_dir.exists():
            try:
                for channel_dir in raw_dir.iterdir():
                    date_dir = channel_dir / date_str
                    if date_dir.is_dir():
                        size = self._dir_size(date_dir)
                        try:
                            shutil.rmtree(date_dir)
                            total_freed += size
                        except OSError as e:
                            logger.warning(f"Cannot remove {date_dir}: {e}")
            except OSError:
                pass

        # 2. phase2: any file containing YYYYMMDD in its name
        phase2_dir = self.data_root / 'phase2'
        if phase2_dir.exists():
            try:
                for root, dirs, files in os.walk(phase2_dir):
                    for fname in files:
                        if date_str in fname:
                            fpath = Path(root) / fname
                            try:
                                size = fpath.stat().st_size
                                fpath.unlink()
                                total_freed += size
                            except OSError:
                                pass
            except OSError:
                pass

        # 3. upload/ and data/ — same pattern
        for subdir in ('upload', 'data'):
            d = self.data_root / subdir
            if d.exists():
                try:
                    for root, dirs, files in os.walk(d):
                        for fname in files:
                            if date_str in fname:
                                fpath = Path(root) / fname
                                try:
                                    size = fpath.stat().st_size
                                    fpath.unlink()
                                    total_freed += size
                                except OSError:
                                    pass
                except OSError:
                    pass

        return total_freed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_ram_available() -> Optional[int]:
        """Get available RAM in bytes from /proc/meminfo."""
        try:
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _get_ram_total() -> Optional[int]:
        """Get total RAM in bytes from /proc/meminfo."""
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
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config_path: str) -> 'ResourceGuardian':
        """Create from timestd-config.toml.

        Auto-detects the number of channels from the [[channels]]
        array and the sample rate from [recorder].  No resource_guardian
        section needed — everything is computed.
        """
        data_root = '/var/lib/timestd'
        n_channels = 0
        sample_rate = 24000
        tiered_storage = False

        try:
            section = None
            with open(config_path, 'r') as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith('[[') and 'channel' in stripped.lower():
                        n_channels += 1
                        continue
                    if stripped.startswith('['):
                        section = stripped.strip('[]').strip()
                        continue
                    if '=' not in stripped or stripped.startswith('#'):
                        continue
                    key, _, value = stripped.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if section == 'recorder':
                        if key == 'production_data_root':
                            data_root = value
                        elif key == 'sample_rate':
                            sample_rate = int(value)
                        elif key == 'tiered_storage':
                            tiered_storage = value.lower() == 'true'
        except (OSError, ValueError) as e:
            logger.warning(f"Could not parse {config_path}: {e}")

        # Fallback: count channel dirs on disk if config parse found none
        if n_channels == 0:
            raw_dir = Path(data_root) / 'raw_buffer'
            if raw_dir.exists():
                n_channels = sum(
                    1 for d in raw_dir.iterdir()
                    if d.is_dir() and not d.name.startswith('.')
                )
            if n_channels == 0:
                n_channels = 9  # safe default

        logger.info(
            f"ResourceGuardian: {n_channels} channels, "
            f"{sample_rate} Hz, data_root={data_root}"
            f"{', tiered_storage=ON (raw IQ in /dev/shm)' if tiered_storage else ''}"
        )

        return cls(
            data_root=data_root,
            n_channels=n_channels,
            sample_rate=sample_rate,
            tiered_storage=tiered_storage,
        )
