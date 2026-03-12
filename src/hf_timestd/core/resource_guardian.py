#!/usr/bin/env python3
"""
Resource Guardian — budget-based disk management for hf-timestd.

Design principle: **new data is always priority over old.**

The 9 RTP streams arrive at a deterministic, predictable rate.  We
compute the exact daily disk ingest budget from the channel count and
sample rate, then manage eviction proactively to maintain headroom
for incoming data.  No per-system tuning is needed — the guardian
auto-sizes everything from the workload.

Data budget (per channel per day, at 24 kHz IQ):
    raw_buffer      ~14 GB   (IQ binary archive, 24 kHz × 4 B × 86400 s)
    phase2           ~4 GB   (HDF5 data products)
    ────────────────────────
    subtotal        ~18 GB/channel/day  (or ~4 GB with tiered storage)

    9 channels → ~36 GB/day on disk (tiered) or ~164 GB/day (non-tiered)

Three layers of protection:

1. **Preflight** — at startup, compute the daily budget and verify the
   disk can hold at least 2 days + 1 day headroom.  If the disk is
   tight, proactively evict oldest days.  Only blocks at 95%.
   Also verify minimum RAM (250 MB per channel + 2 GB headroom).

2. **Watchdog** — every 60 s, ensure free space ≥ 1 day of incoming
   data.  If headroom is low, evict oldest complete days (across
   raw_buffer/ and phase2/ simultaneously) until room is restored.
   Always keeps today's data.  New data is ALWAYS priority over old.

3. **Hard stop** — if disk reaches 95% despite cleanup (other processes
   are filling disk), stop all writes and exit.  We will not crash
   the host.

Archive drive (optional):

    If ``archive_root`` is set in [recorder], evicted days are moved
    to the archive drive instead of deleted.  The archive drive has
    its own 80% disk cap and optional ``archive_retention_days``
    policy.  If the archive is full or unmounted, eviction falls
    back to deletion.  Config example::

        [recorder]
        archive_root = "/mnt/timestd-archive"
        archive_retention_days = 90
"""

import datetime
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

# ── Budget-based disk management ──
# Primary mechanism: always maintain room for HEADROOM_DAYS of incoming
# data.  The 9 RTP streams arrive at a deterministic rate, so we can
# compute the exact daily ingest budget and manage eviction proactively.
# New data is ALWAYS priority over old — oldest days are evicted first.
HEADROOM_DAYS = 1   # always keep room for at least this many days

# Secondary safety nets (percentage-based, in case something else is
# consuming disk outside hf-timestd's management):
DISK_WARN_PERCENT = 80.0       # log a warning
DISK_HARD_STOP_PERCENT = 95.0  # stop all writes — something else filling disk

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
    OK = 'ok'                # Headroom is sufficient — normal operation
    CLEANED = 'cleaned'      # Evicted oldest day(s) to restore headroom
    STOP = 'stop'            # At 95% — something else filling disk, stop writes
    EMERGENCY = 'emergency'  # Cannot free enough — cease all activity


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
    days_retained: int = 0       # how many days of data are on disk
    days_headroom: float = 0.0   # how many more days can fit before eviction


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
        archive_root: Optional[str] = None,
        archive_retention_days: Optional[int] = None,
    ):
        self.data_root = Path(data_root)
        self.n_channels = n_channels
        self.sample_rate = sample_rate
        self.tiered_storage = tiered_storage
        self.archive_root = Path(archive_root) if archive_root else None
        self.archive_retention_days = archive_retention_days

        # Compute per-channel-per-day raw bytes from actual sample rate
        # complex IQ: sample_rate × 4 bytes × 2 (I+Q) × 86400 seconds
        self.raw_per_ch_per_day = sample_rate * 4 * 2 * 86400
        self.total_per_ch_per_day = self.raw_per_ch_per_day + PHASE2_BYTES_PER_CHANNEL_PER_DAY

        # Daily disk budget: exact bytes/day arriving on persistent storage.
        # With tiered storage, raw IQ lives in /dev/shm (RAM), not on
        # the data disk — only Phase2 HDF5 products hit persistent storage.
        if tiered_storage:
            self.disk_per_ch_per_day = PHASE2_BYTES_PER_CHANNEL_PER_DAY
        else:
            self.disk_per_ch_per_day = self.total_per_ch_per_day
        self.daily_disk_budget = n_channels * self.disk_per_ch_per_day

        # Minimum storage: BASELINE_DAYS of data + headroom + overhead
        self.baseline_bytes = (
            (BASELINE_DAYS + HEADROOM_DAYS) * self.daily_disk_budget
            + OVERHEAD_BYTES
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

        Budget-based: computes the exact daily ingest rate from the
        channel count and sample rate, then verifies the disk can hold
        at least BASELINE_DAYS + HEADROOM_DAYS of data.  If the disk
        is tight, evicts oldest days proactively — new data is always
        priority over old.  Only blocks startup at 95% (hard stop).
        """
        ok = True
        headroom_bytes = self.daily_disk_budget * HEADROOM_DAYS
        baseline_gb = self.baseline_bytes / GB
        budget_gb = self.daily_disk_budget / GB

        logger.info(
            f"Resource preflight: {self.n_channels} channels × "
            f"{self.sample_rate} Hz"
        )
        logger.info(
            f"  Daily ingest budget: {budget_gb:.1f} GB/day on disk"
            f" ({self.disk_per_ch_per_day / GB:.1f} GB/ch/day × "
            f"{self.n_channels} channels"
            f"{', tiered storage' if self.tiered_storage else ''})"
        )
        logger.info(
            f"  Baseline requirement: {baseline_gb:.0f} GB "
            f"({BASELINE_DAYS} days + {HEADROOM_DAYS} day headroom + overhead)"
        )

        # --- Disk ---
        try:
            stat = shutil.disk_usage(self.data_root)
            total_gb = stat.total / GB
            free_gb = stat.free / GB
            used_pct = (stat.used / stat.total) * 100

            # Step 1: Is the disk physically large enough for the workload?
            if self.baseline_bytes > stat.total:
                logger.critical(
                    f"PREFLIGHT FAIL: {self.n_channels}-channel workload "
                    f"needs {baseline_gb:.0f} GB baseline, but disk is only "
                    f"{total_gb:.0f} GB. Need a larger disk."
                )
                ok = False
            # Step 2: Hard stop — refuse at 95% regardless (external consumer)
            elif used_pct >= DISK_HARD_STOP_PERCENT:
                logger.critical(
                    f"PREFLIGHT FAIL: Disk at {used_pct:.1f}%% "
                    f"({free_gb:.1f} GB free) — above hard stop "
                    f"({DISK_HARD_STOP_PERCENT}%%). "
                    f"Free space manually before starting hf-timestd."
                )
                ok = False
            else:
                # Step 3: Bring disk under budget.
                # New data is ALWAYS priority over old.  At startup we
                # aggressively evict oldest days to get under 80% AND
                # ensure headroom for incoming data.  This puts the disk
                # in a clean state so the runtime watchdog only has to
                # maintain it.
                target_free = max(
                    headroom_bytes,
                    int(stat.total * (1.0 - DISK_WARN_PERCENT / 100.0)),
                )
                if stat.free < target_free:
                    deficit_gb = (target_free - stat.free) / GB
                    logger.warning(
                        f"Preflight: disk at {used_pct:.1f}%% "
                        f"({free_gb:.1f} GB free), need {target_free / GB:.1f} GB free "
                        f"(max of {HEADROOM_DAYS}-day headroom and "
                        f"{DISK_WARN_PERCENT}%% target) "
                        f"— evicting {deficit_gb:.1f} GB of oldest data"
                    )
                    cleaned = self._ensure_headroom(target_free)
                    if cleaned > 0:
                        logger.info(
                            f"Preflight eviction freed {cleaned / GB:.1f} GB"
                        )
                    # Re-check after cleanup
                    stat = shutil.disk_usage(self.data_root)
                    free_gb = stat.free / GB
                    used_pct = (stat.used / stat.total) * 100

                # Report final state
                n_days = len(self._collect_all_dates())
                headroom_days = stat.free / self.daily_disk_budget if self.daily_disk_budget > 0 else 0
                if stat.free < target_free:
                    logger.warning(
                        f"Preflight disk: {free_gb:.1f} GB free of "
                        f"{total_gb:.0f} GB ({used_pct:.1f}%% used), "
                        f"{n_days} days retained, "
                        f"{headroom_days:.1f} days headroom — "
                        f"could not reach target but allowing startup. "
                        f"Runtime watchdog will continue evicting."
                    )
                else:
                    logger.info(
                        f"Preflight disk: {free_gb:.1f} GB free of "
                        f"{total_gb:.0f} GB ({used_pct:.1f}%% used), "
                        f"{n_days} days retained, "
                        f"{headroom_days:.1f} days headroom — OK"
                    )
        except OSError as e:
            logger.critical(f"PREFLIGHT FAIL: Cannot stat {self.data_root}: {e}")
            ok = False

        # --- RAM ---
        ram_available = self._get_ram_available()
        ram_total = self._get_ram_total()
        if ram_total is not None:
            ram_budget = ram_total * 0.80  # use at most 80% of RAM
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

        # --- Archive drive ---
        if self.archive_root:
            if self._archive_is_mounted():
                for dirname in ['raw_buffer', 'phase2']:
                    adir = self.archive_root / dirname
                    if not adir.exists():
                        try:
                            adir.mkdir(parents=True, exist_ok=True)
                            logger.info(f"Preflight: created archive dir {adir}")
                        except OSError as e:
                            logger.warning(f"Cannot create archive dir {adir}: {e}")
                try:
                    astat = shutil.disk_usage(self.archive_root)
                    logger.info(
                        f"Preflight archive: {astat.free / GB:.1f} GB free of "
                        f"{astat.total / GB:.0f} GB at {self.archive_root}"
                        f"{f', retention={self.archive_retention_days}d' if self.archive_retention_days else ''}"
                    )
                except OSError as e:
                    logger.warning(f"Cannot stat archive drive: {e}")
            else:
                logger.warning(
                    f"Archive drive configured ({self.archive_root}) "
                    f"but not mounted — eviction will delete instead of archive"
                )

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
        """Periodic check: ensure headroom for incoming data.

        Budget-based: the 9 RTP streams arrive at a deterministic rate.
        We compute the exact daily disk budget and ensure at least
        HEADROOM_DAYS of free space is available.  If not, evict the
        oldest complete days until headroom is restored.

        The 95% hard-stop is a secondary safety net in case something
        outside hf-timestd is filling the disk.

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

        headroom_bytes = self.daily_disk_budget * HEADROOM_DAYS

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

        state = ResourceState.OK
        message = ''
        bytes_cleaned = 0
        n_days = 0
        headroom_days = 0.0

        # --- Safety net: hard stop at 95% ---
        if disk_used_pct >= DISK_HARD_STOP_PERCENT:
            # Something outside hf-timestd is filling the disk.
            # Do our best to free space, but if we can't, stop.
            bytes_cleaned = self._ensure_headroom(headroom_bytes)
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
                    f"evicting {bytes_cleaned / GB:.1f} GB — stopping all writes"
                )
                logger.critical(message)
            else:
                state = ResourceState.STOP
                message = (
                    f"Disk was ≥{DISK_HARD_STOP_PERCENT}%%, evicted "
                    f"{bytes_cleaned / GB:.1f} GB, now {disk_used_pct:.1f}%% "
                    f"— pausing to stabilize"
                )
                logger.error(message)

        # --- Primary mechanism: budget-based headroom ---
        elif disk_free < headroom_bytes:
            n_days_before = len(self._collect_all_dates())
            bytes_cleaned = self._ensure_headroom(headroom_bytes)
            try:
                stat2 = shutil.disk_usage(self.data_root)
                disk_used_pct = (stat2.used / stat2.total) * 100
                disk_free = stat2.free
            except OSError:
                pass
            n_days = len(self._collect_all_dates())
            headroom_days = disk_free / self.daily_disk_budget if self.daily_disk_budget > 0 else 0
            state = ResourceState.CLEANED
            message = (
                f"Headroom low: evicted {n_days_before - n_days} day(s), "
                f"freed {bytes_cleaned / GB:.1f} GB. "
                f"Now {disk_free / GB:.1f} GB free "
                f"({headroom_days:.1f} days headroom, "
                f"{n_days} days retained)"
            )
            logger.info(message)

        # --- Informational: warn at 80% even if headroom is OK ---
        elif disk_used_pct >= DISK_WARN_PERCENT:
            n_days = len(self._collect_all_dates())
            headroom_days = disk_free / self.daily_disk_budget if self.daily_disk_budget > 0 else 0
            message = (
                f"Disk at {disk_used_pct:.1f}%% but headroom OK "
                f"({headroom_days:.1f} days, {n_days} days retained)"
            )
            logger.debug(message)

        else:
            if self.daily_disk_budget > 0:
                headroom_days = disk_free / self.daily_disk_budget
                n_days = len(self._collect_all_dates())

        return ResourceStatus(
            state=state,
            disk_free_bytes=disk_free,
            disk_total_bytes=disk_total,
            disk_used_percent=disk_used_pct,
            ram_available_bytes=ram_available,
            ram_total_bytes=ram_total,
            message=message,
            bytes_cleaned=bytes_cleaned,
            days_retained=n_days,
            days_headroom=headroom_days,
        )

    # ------------------------------------------------------------------
    # 3. Storage eviction — oldest-day-first, across all directories
    # ------------------------------------------------------------------

    def _ensure_headroom(self, min_free_bytes: int) -> int:
        """Evict oldest complete days until free space >= *min_free_bytes*.

        Budget-based: knows exactly how much space the pipeline needs
        and evicts the minimum number of oldest days to make room.
        New data is ALWAYS priority over old.

        A "day" is identified by YYYYMMDD and removed from raw_buffer/
        AND phase2/ simultaneously.  Always keeps today so the live
        pipeline isn't disrupted.
        """
        total_freed = 0
        today = time.strftime('%Y%m%d', time.gmtime())

        all_dates = self._collect_all_dates()
        all_dates.discard(today)
        dates_oldest_first = sorted(all_dates)

        for date_str in dates_oldest_first:
            try:
                stat = shutil.disk_usage(self.data_root)
                if stat.free >= min_free_bytes:
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

    def _evict_oldest_days_until_under(
        self,
        disk_total: int,
        target_percent: float,
    ) -> int:
        """Legacy: evict until filesystem usage drops below *target_percent*.

        Kept as a fallback for the 95% hard-stop path.  Budget-based
        _ensure_headroom() is the primary eviction mechanism.
        """
        target_free = disk_total * (1.0 - target_percent / 100.0)
        return self._ensure_headroom(int(target_free))

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
        """Remove all data for a single YYYYMMDD date across all dirs.

        If an archive drive is configured and mounted, data is moved
        there instead of deleted.  The archive drive's own 80% cap
        and retention policy are enforced separately.
        """
        archive = None
        if self.archive_root and self._archive_is_mounted():
            archive = self.archive_root
            # Make room on archive drive before moving
            self._enforce_archive_limits()

        total_freed = 0

        # 1. raw_buffer/CHANNEL/YYYYMMDD/ directories
        raw_dir = self.data_root / 'raw_buffer'
        if raw_dir.exists():
            try:
                for channel_dir in raw_dir.iterdir():
                    date_dir = channel_dir / date_str
                    if date_dir.is_dir():
                        size = self._dir_size(date_dir)
                        if archive:
                            total_freed += self._move_to_archive(
                                date_dir, archive / 'raw_buffer' / channel_dir.name / date_str
                            )
                        else:
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
                                if archive:
                                    # Mirror the directory structure under archive/phase2/
                                    rel = fpath.relative_to(phase2_dir)
                                    total_freed += self._move_to_archive(
                                        fpath, archive / 'phase2' / rel
                                    )
                                else:
                                    fpath.unlink()
                                    total_freed += size
                            except OSError:
                                pass
            except OSError:
                pass

        # 3. upload/ and data/ — same pattern (always delete, not archived)
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

        action = 'archived' if archive else 'deleted'
        logger.info(f"Eviction: {action} day {date_str}")
        return total_freed

    # ------------------------------------------------------------------
    # 4. Archive drive management
    # ------------------------------------------------------------------

    def _archive_is_mounted(self) -> bool:
        """Check if the archive root exists and is a mount point or writable dir."""
        if not self.archive_root:
            return False
        try:
            return self.archive_root.is_dir() and os.access(self.archive_root, os.W_OK)
        except OSError:
            return False

    def _move_to_archive(self, src: Path, dst: Path) -> int:
        """Move a file or directory from primary to archive.  Returns bytes freed."""
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            size = self._dir_size(src) if src.is_dir() else src.stat().st_size
            shutil.move(str(src), str(dst))
            return size
        except OSError as e:
            logger.warning(f"Archive move failed {src} → {dst}: {e}, deleting instead")
            # Fallback: delete if move fails (e.g., cross-device + dir)
            try:
                if src.is_dir():
                    size = self._dir_size(src)
                    shutil.rmtree(src)
                else:
                    size = src.stat().st_size
                    src.unlink()
                return size
            except OSError:
                return 0

    def _enforce_archive_limits(self) -> int:
        """Enforce 80% disk cap and retention policy on the archive drive.

        Evicts the oldest archived days until the archive disk is under
        80% and all data is within the retention window.
        Returns bytes freed.
        """
        if not self.archive_root or not self._archive_is_mounted():
            return 0

        total_freed = 0
        today = time.strftime('%Y%m%d', time.gmtime())

        # Collect all dates in the archive
        archive_dates: Set[str] = set()
        for subdir in ('raw_buffer', 'phase2'):
            adir = self.archive_root / subdir
            if not adir.exists():
                continue
            try:
                for root, dirs, files in os.walk(adir):
                    for name in dirs + files:
                        m = DATE_RE.search(name)
                        if m:
                            archive_dates.add(m.group(1))
            except OSError:
                pass

        archive_dates.discard(today)
        dates_oldest_first = sorted(archive_dates)

        for date_str in dates_oldest_first:
            should_evict = False

            # Retention policy: delete archive data older than N days
            if self.archive_retention_days is not None:
                try:
                    date_obj = datetime.datetime.strptime(date_str, '%Y%m%d')
                    age_days = (datetime.datetime.utcnow() - date_obj).days
                    if age_days > self.archive_retention_days:
                        should_evict = True
                except ValueError:
                    continue

            # Disk cap: archive drive over 80%
            if not should_evict:
                try:
                    astat = shutil.disk_usage(self.archive_root)
                    if (astat.used / astat.total) * 100 >= DISK_WARN_PERCENT:
                        should_evict = True
                    else:
                        break  # Under 80% and within retention — done
                except OSError:
                    break

            if should_evict:
                freed = self._evict_archive_date(date_str)
                total_freed += freed
                if freed > 0:
                    logger.info(
                        f"Archive eviction: deleted {date_str}, "
                        f"freed {freed / GB:.1f} GB"
                    )

        return total_freed

    def _evict_archive_date(self, date_str: str) -> int:
        """Delete all data for a single date from the archive."""
        total_freed = 0

        # raw_buffer/CHANNEL/YYYYMMDD/
        raw_dir = self.archive_root / 'raw_buffer'
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
                            logger.warning(f"Cannot remove archive {date_dir}: {e}")
            except OSError:
                pass

        # phase2: files containing YYYYMMDD
        phase2_dir = self.archive_root / 'phase2'
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
        archive_root = None
        archive_retention_days = None

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
                        elif key == 'archive_root':
                            archive_root = value if value else None
                        elif key == 'archive_retention_days':
                            archive_retention_days = int(value)
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
            archive_root=archive_root,
            archive_retention_days=archive_retention_days,
        )
