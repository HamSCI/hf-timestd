#!/usr/bin/env python3
"""
Resource Guardian — the emergency disk net for hf-timestd.

The sigmond storage ledger owns routine retention.  It trims each store on
its own schedule and keeps the disk well under 98 %.  The guardian no longer
keeps headroom, counts retained days, or moves data to an archive drive.  It
acts only when the disk fills despite the ledger.

What the guardian does:

1. **Preflight** — at startup, size the daily ingest budget from the channel
   count and sample rate, and refuse to start when the disk cannot hold the
   baseline (2 days + 1 day headroom + overhead), or when RAM falls short (250 MB per
   channel + 2 GB).  At or above the hard-stop percentage it starts the
   service paused rather than refusing.  Preflight never deletes.

2. **Watchdog** — every 60 s, read the disk.  Below the hard-stop percentage
   it does nothing, and it walks no data tree.  At or above it, it pauses
   writes and alerts at once.  Only after the condition holds for the grace
   window (600 s) does it delete: the smallest regenerable unit first, oldest
   first, down to a low-water mark (90 %).  Raw IQ goes last, one whole day
   at a time.  Today's data stays.  The deletion pass takes an exclusive lock
   (EVICT_LOCK) so the six metrology processes never evict at once.

3. **Hard stop** — if the disk stays at or above the hard-stop percentage
   after eviction, report EMERGENCY so the caller stops all writes.

The hard-stop percentage depends on who manages the station:

* ledger manages hf-timestd (``ledger_manages_us()``): 98 %, above the
  ledger's final stage.
* ledger does not manage it (a standalone install): 95 %.  The guardian is
  then the only raw cleaner a metrology profile has.

An evicted day gets deleted.  The guardian never moves data; the archive
drive is filled by timestd-archive.service, which copies.
"""

import fcntl
import logging
import os
import re
import shutil
from stat import S_ISREG
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Set

from ..ledger_gate import ledger_manages_us
from .binary_archive_writer import DEFAULT_ARCHIVE_DTYPE, validate_archive_dtype
from .iq_codec import DTYPE_F16, DTYPE_F32, bytes_per_sample

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GB = 1024 ** 3
MB = 1024 ** 2

# The headroom pass is gone: the sigmond ledger owns routine retention.
# HEADROOM_DAYS still sizes the preflight baseline.
HEADROOM_DAYS = 1

# Emergency nets (percentage-based).
DISK_WARN_PERCENT = 80.0       # debug note only
DISK_HARD_STOP_PERCENT = 95.0  # station the ledger does not manage
LEDGER_HARD_STOP_PERCENT = 98.0  # ledger manages us: above its final stage

# One evictor at a time across the metrology processes.  The lock lives in
# the service-owned runtime dir (RuntimeDirectory=hf-timestd), mode 0600,
# opened with O_NOFOLLOW and refused unless the service user owns it.
EVICT_LOCK = Path("/run/hf-timestd/guardian-evict.lock")
# A lock that stays contended this long is not a live evictor: evict anyway.
# 3 x the grace window (EVICT_GRACE_SEC below, 600 s).
EVICT_LOCK_MAX_WAIT_SEC = 3 * 600.0

# hf-timestd#31 — eviction must not destroy a whole day within 2 s of a
# transient spike over the hard stop.  A crossing PAUSES writes and alerts immediately, but
# nothing is deleted until it has PERSISTED for the grace window; then data is
# freed the smallest-regenerable-unit first, oldest-first, down to a low-water
# mark — never a whole day in one rmtree unless raw IQ is all that is left.
EVICT_GRACE_SEC = 600.0           # the hard stop must persist this long before any delete
EVICT_LOW_WATER_PERCENT = 90.0    # evict just enough to get back under this
EVICT_POLICY_AUTO = 'auto'        # DASI2 default (mjh 2026-08-22): auto with hysteresis
EVICT_POLICY_ALERT_ONLY = 'alert_only'  # never delete; pause + alert only

# Per-channel data budget (bytes per day) at 24 kHz complex IQ
# IQ: 24000 samples/s × 8 bytes/sample (complex64) × 86400 s/day ≈ 15.4 GiB; half as F16
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
    CLEANED = 'cleaned'      # (retained for callers; the watchdog no longer returns it)
    STOP = 'stop'            # At the hard stop — stop writes
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
    skipped: bool = False        # interval not elapsed: no new reading
    days_retained: int = 0       # always 0: the guardian walks no tree
    days_headroom: float = 0.0   # always 0: the ledger keeps the headroom


class ResourceGuardian:
    """Universal, self-sizing resource manager for hf-timestd.

    Usage::

        guardian = ResourceGuardian.from_config('/etc/hf-timestd/timestd-config.toml')

        # At startup — refuses to start if system can't sustain the load:
        if not guardian.preflight_check():
            sys.exit(1)

        # In main loop — acts only at the hard stop:
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
        evict_policy: str = EVICT_POLICY_AUTO,
        evict_grace_sec: float = EVICT_GRACE_SEC,
        evict_low_water_percent: float = EVICT_LOW_WATER_PERCENT,
        clock=time.monotonic,
        archive_dtype: str = DEFAULT_ARCHIVE_DTYPE,
    ):
        self.data_root = Path(data_root)
        self.n_channels = n_channels
        self.sample_rate = sample_rate
        self.tiered_storage = tiered_storage

        # Compute per-channel-per-day raw bytes from actual sample rate and
        # the configured archive dtype: 8 bytes per complex sample as
        # complex64, 4 as F16.  This is the uncompressed size, an upper
        # bound for the .bin.zst files; eviction measures the bytes on disk.
        validate_archive_dtype(archive_dtype)
        self.archive_dtype = archive_dtype
        stored = DTYPE_F16 if archive_dtype == 'f16' else DTYPE_F32
        self.raw_per_ch_per_day = sample_rate * bytes_per_sample(stored) * 86400
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

        self.evict_policy = evict_policy
        self._ledger_managed = False      # last gate reading, for one log line per change
        self.evict_grace_sec = float(evict_grace_sec)
        self.evict_low_water_percent = float(evict_low_water_percent)
        self._clock = clock

        self._last_watchdog_time = float('-inf')   # first check runs at once
        self._evict_skipped = False
        self._lock_contended_since: Optional[float] = None
        self._watchdog_interval = 60.0
        # hf-timestd#31: monotonic time we FIRST saw the hard stop, or None when clear.
        self._over_since: Optional[float] = None

    # ------------------------------------------------------------------
    # 1. Preflight check
    # ------------------------------------------------------------------

    def preflight_check(self) -> bool:
        """Verify the system can sustain hf-timestd before starting.

        Budget-based: computes the exact daily ingest rate from the
        channel count and sample rate, then verifies the disk can hold
        at least BASELINE_DAYS + HEADROOM_DAYS of data.  If the disk
        is tight, it logs and lets the ledger (or the emergency path)
        deal with it.  At the hard stop it starts paused.  Never deletes.
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
            # Step 2: at the hard stop the service starts PAUSED, not refused:
            # a refusal strands the station, because no process then lives
            # long enough for the watchdog's grace window to elapse and the
            # emergency eviction to run.  The service holds its writes while
            # the watchdog says STOP.
            elif used_pct >= self.hard_stop_percent():
                logger.critical(
                    f"Preflight: disk at {used_pct:.1f}%% "
                    f"({free_gb:.1f} GB free) — at or above the hard stop "
                    f"({self.hard_stop_percent()}%%). Starting PAUSED: writes "
                    f"held until the watchdog frees space or the disk "
                    f"recovers."
                )
            else:
                # Step 3: report.  Preflight never deletes.
                headroom_days = (stat.free / self.daily_disk_budget
                                 if self.daily_disk_budget > 0 else 0)
                target_free = max(
                    headroom_bytes,
                    int(stat.total * (1.0 - DISK_WARN_PERCENT / 100.0)),
                )
                if stat.free < target_free:
                    logger.warning(
                        f"Preflight disk: {free_gb:.1f} GB free of "
                        f"{total_gb:.0f} GB ({used_pct:.1f}%% used), "
                        f"{headroom_days:.1f} days headroom — below the "
                        f"{DISK_WARN_PERCENT:.0f}%% target; starting anyway "
                        f"(the ledger trims routine data, the watchdog acts "
                        f"at the hard stop)"
                    )
                else:
                    logger.info(
                        f"Preflight disk: {free_gb:.1f} GB free of "
                        f"{total_gb:.0f} GB ({used_pct:.1f}%% used), "
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

    def hard_stop_percent(self) -> float:
        """The hard-stop percentage in force now.  Reads the ledger gate on
        every call (never cached) and logs one line per change."""
        managed = ledger_manages_us()
        if managed != self._ledger_managed:
            self._ledger_managed = managed
            if managed:
                logger.info("The sigmond storage ledger manages hf-timestd: "
                            "the emergency net moves to %.0f%%",
                            LEDGER_HARD_STOP_PERCENT)
            else:
                logger.info("The sigmond storage ledger does not manage "
                            "hf-timestd: the emergency net is %.0f%%",
                            DISK_HARD_STOP_PERCENT)
        return LEDGER_HARD_STOP_PERCENT if managed else DISK_HARD_STOP_PERCENT

    def watchdog_check(self, force: bool = False) -> ResourceStatus:
        """Periodic emergency check.

        Below the hard-stop percentage this reads the disk and returns OK.
        It walks no data tree and deletes nothing; the sigmond ledger owns
        routine retention.  At or above it, it pauses, then evicts after
        the grace window (see the module docstring).

        Call from the service main loop.  Returns quickly (no-op) if
        the check interval hasn't elapsed.
        """
        now = self._clock()
        if not force and (now - self._last_watchdog_time) < self._watchdog_interval:
            return ResourceStatus(
                state=ResourceState.OK,
                disk_free_bytes=0, disk_total_bytes=0,
                disk_used_percent=0.0,
                ram_available_bytes=0, ram_total_bytes=0,
                message='skipped (interval)', skipped=True,
            )
        self._last_watchdog_time = now

        hard_stop = self.hard_stop_percent()

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

        # --- Safety net: hard stop (hf-timestd#31: hysteresis) ---
        if disk_used_pct >= hard_stop:
            if self._over_since is None:
                self._over_since = now
            over_for = now - self._over_since
            grace_left = self.evict_grace_sec - over_for
            # ALWAYS pause + alert on a crossing; DELETE only after the
            # pressure has persisted past the grace window, and never in
            # alert_only mode.  A transient hog (the 2026-08-21 drill) clears
            # inside the window and costs no data.
            may_evict = (self.evict_policy == EVICT_POLICY_AUTO
                         and over_for >= self.evict_grace_sec)
            if not may_evict:
                if self.evict_policy == EVICT_POLICY_ALERT_ONLY:
                    reason = "alert_only policy — will not evict"
                else:
                    reason = f"grace {over_for:.0f}/{self.evict_grace_sec:.0f}s before evicting"
                state = ResourceState.STOP
                message = (
                    f"Disk at {disk_used_pct:.1f}%% (>= {hard_stop}%%) "
                    f"— PAUSING writes ({reason}); no data deleted")
                logger.critical(message)
                self._write_guardian_event(disk_used_pct, state, message, hard_stop,
                                           action="paused", bytes_cleaned=0,
                                           grace_left=max(0.0, grace_left))
            else:
                bytes_cleaned = self._evict_to_low_water(self.evict_low_water_percent)
                skipped_locked = self._evict_skipped
                action = "evicted"
                try:
                    stat2 = shutil.disk_usage(self.data_root)
                    disk_used_pct = (stat2.used / stat2.total) * 100
                    disk_free = stat2.free
                except OSError:
                    pass
                if skipped_locked and disk_used_pct >= hard_stop:
                    # Another process holds the eviction lock and works on it.
                    state = ResourceState.STOP
                    message = (
                        f"Disk at {disk_used_pct:.1f}%% (>= {hard_stop}%%) — "
                        f"another process is evicting; still paused")
                    logger.warning(message)
                    action = "evict-skipped-locked"
                elif disk_used_pct >= hard_stop:
                    state = ResourceState.EMERGENCY
                    message = (
                        f"EMERGENCY: disk at {disk_used_pct:.1f}%% even after "
                        f"evicting {bytes_cleaned / GB:.1f} GB — stopping all writes")
                    logger.critical(message)
                else:
                    state = ResourceState.STOP
                    self._over_since = None
                    message = (
                        f"Disk was ≥{hard_stop}%%, evicted "
                        f"{bytes_cleaned / GB:.1f} GB (regenerable first, oldest "
                        f"first), now {disk_used_pct:.1f}%% — pausing to stabilize")
                    logger.error(message)
                self._write_guardian_event(disk_used_pct, state, message, hard_stop,
                                           action=action, bytes_cleaned=bytes_cleaned,
                                           grace_left=0.0)

        # Back under the hard stop — clear the hysteresis timer.
        else:
            self._over_since = None
            self._lock_contended_since = None

        # Below the hard stop nothing else happens here: the sigmond ledger
        # owns routine retention, and the guardian walks no data tree.
        if state == ResourceState.OK and disk_used_pct >= DISK_WARN_PERCENT:
            logger.debug("Disk at %.1f%%; the ledger trims routine data",
                         disk_used_pct)

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

    def _evict_to_low_water(self, low_water_percent: float) -> int:
        """Free the smallest REGENERABLE unit first, oldest first, one at a
        time, re-checking disk after each, until usage is below
        ``low_water_percent`` (hf-timestd#31).  Raw IQ is the last resort and
        is removed a whole day at a time only when nothing regenerable is
        left; everything else (phase2 HDF5, products, vtec, upload, data) is
        removed one file at a time.  Never touches today.

        Takes EVICT_LOCK without blocking.  A process that cannot take it
        skips this pass, so six metrology processes never evict at once."""
        self._evict_skipped = False
        fd = self._take_evict_lock()
        if fd is False:
            self._evict_skipped = True
            return 0
        try:
            return self._evict_to_low_water_locked(low_water_percent)
        finally:
            if fd is not None:
                try:
                    os.close(fd)          # closing drops the flock
                except OSError:
                    pass

    def _take_evict_lock(self):
        """fd when taken; False when another process holds it (skip this
        pass); None to evict WITHOUT the lock.  None covers three cases, each
        logged at WARNING so an emergency never depends on the lock:
        the file cannot be opened, the file is not a regular file owned by
        this user (a planted or symlinked lock), or the lock has stayed
        contended longer than EVICT_LOCK_MAX_WAIT_SEC (a dead or hostile
        holder)."""
        try:
            EVICT_LOCK.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(EVICT_LOCK,
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600)
        except OSError as exc:
            logger.warning("Guardian eviction lock %s unavailable (%s); "
                           "evicting without it", EVICT_LOCK, exc)
            return None
        try:
            st = os.fstat(fd)
            if not S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                logger.warning("Guardian eviction lock %s is not a regular "
                               "file owned by uid %d; refusing it and "
                               "evicting without it", EVICT_LOCK, os.geteuid())
                os.close(fd)
                return None
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            now = self._clock()
            if self._lock_contended_since is None:
                self._lock_contended_since = now
            held = now - self._lock_contended_since
            if held > EVICT_LOCK_MAX_WAIT_SEC:
                logger.warning("Guardian eviction lock %s contended for %.0f s "
                               "(> %.0f s); evicting anyway",
                               EVICT_LOCK, held, EVICT_LOCK_MAX_WAIT_SEC)
                return None
            logger.warning("Guardian eviction skipped: another process holds "
                           "%s (contended %.0f s)", EVICT_LOCK, held)
            return False
        self._lock_contended_since = None
        return fd

    def _evict_to_low_water_locked(self, low_water_percent: float) -> int:
        total_freed = 0
        try:
            total = shutil.disk_usage(self.data_root).total
        except OSError:
            return 0
        target_used = total * (low_water_percent / 100.0)
        for kind, unit in self._eviction_candidates():
            try:
                if shutil.disk_usage(self.data_root).used <= target_used:
                    break
            except OSError:
                break
            freed = self._evict_unit(kind, unit)
            total_freed += freed
            if freed > 0:
                logger.info("Guardian eviction: freed %.2f GB (%s %s)",
                            freed / GB, kind, getattr(unit, 'name', unit))
        return total_freed

    def _eviction_candidates(self):
        """(kind, path) pairs in eviction PRIORITY order — regenerable before
        raw, oldest before newest within each — excluding today.  kind is
        'file' (delete the file) or 'raw_day' (rmtree the day dir)."""
        today = time.strftime('%Y%m%d', time.gmtime())

        def _date_key(path: Path):
            m = DATE_RE.search(path.name)
            return (m.group(1) if m else '99999999', path.name)

        # 1..N regenerable file trees, oldest-first, today excluded
        for sub in ('phase2', 'products', 'vtec', 'upload', 'data'):
            d = self.data_root / sub
            if not d.exists():
                continue
            files = []
            try:
                for root, _dirs, names in os.walk(d):
                    for name in names:
                        m = DATE_RE.search(name)
                        if m and m.group(1) == today:
                            continue
                        files.append(Path(root) / name)
            except OSError:
                continue
            for f in sorted(files, key=_date_key):
                yield ('file', f)

        # Last resort: raw IQ, oldest complete day at a time
        raw = self.data_root / 'raw_buffer'
        if raw.exists():
            dates = sorted(d for d in self._collect_all_dates() if d != today)
            seen = set()
            for date_str in dates:
                if date_str in seen:
                    continue
                seen.add(date_str)
                yield ('raw_day', date_str)

    def _usage_percent(self) -> float:
        try:
            u = shutil.disk_usage(self.data_root)
            return 100.0 * u.used / u.total if u.total else 100.0
        except OSError:
            return 100.0

    def _raw_day_may_leave(self, date_str: str) -> bool:
        from .. import archiver
        return archiver.raw_day_may_leave(self.data_root, date_str, self._usage_percent())

    def _evict_unit(self, kind: str, unit) -> int:
        if kind == 'file':
            try:
                size = unit.stat().st_size
                unit.unlink()
                return size
            except OSError:
                return 0
        if kind == 'raw_day':
            freed = 0
            raw = self.data_root / 'raw_buffer'
            if not self._raw_day_may_leave(unit):
                return 0
            try:
                for channel_dir in raw.iterdir():
                    date_dir = channel_dir / unit
                    if date_dir.is_dir():
                        size = self._dir_size(date_dir)
                        try:
                            shutil.rmtree(date_dir)
                            freed += size
                        except OSError:
                            pass
            except OSError:
                pass
            logger.info("Eviction: deleted raw day %s (last-resort)", unit)
            return freed
        return 0

    def _write_guardian_event(self, disk_used_pct, state, message, hard_stop, *,
                              action, bytes_cleaned, grace_left) -> None:
        """Write a structured event the fleet heartbeat can read — so a
        guardian pause/eviction is VISIBLE on the board, not just in the
        journal (hf-timestd#31, ties to sigmond#49)."""
        import json
        state_dir = self.data_root / 'state'
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'disk_used_percent': round(disk_used_pct, 1),
                'over_threshold': disk_used_pct >= hard_stop,
                'hard_stop_percent': hard_stop,
                'state': state.value if hasattr(state, 'value') else str(state),
                'policy': self.evict_policy,
                'action': action,
                'grace_sec': self.evict_grace_sec,
                'grace_remaining_sec': round(grace_left, 0),
                'bytes_cleaned': int(bytes_cleaned),
                'message': message,
            }
            tmp = state_dir / 'guardian-event.json.tmp'
            tmp.write_text(json.dumps(payload))
            tmp.replace(state_dir / 'guardian-event.json')
        except OSError as exc:
            logger.warning("Could not write guardian event: %s", exc)

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
        """Delete all data for a single YYYYMMDD date across all dirs.

        Deletes, never moves.  A raw day waits while the archive drive does
        not yet hold it, short of an emergency (archive-before-evict).
        """
        total_freed = 0

        # 1. raw_buffer/CHANNEL/YYYYMMDD/ directories
        raw_dir = self.data_root / 'raw_buffer'
        if raw_dir.exists() and self._raw_day_may_leave(date_str):
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

        # 2. phase2, upload/ and data/: any file containing YYYYMMDD
        for subdir in ('phase2', 'upload', 'data'):
            d = self.data_root / subdir
            if not d.exists():
                continue
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

        logger.info(f"Eviction: deleted day {date_str}")
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

    @staticmethod
    def _read_archive_dtype(config_path: str) -> str:
        """Read [recorder] archive_dtype with a real TOML parser.

        The guardian only advises on disk use, so a missing, unparseable or
        unsupported value logs one warning and sizes the archive as
        complex64; it never stops metrology.
        """
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            import tomli as tomllib
        try:
            with open(config_path, 'rb') as f:
                value = tomllib.load(f).get('recorder', {}).get(
                    'archive_dtype', DEFAULT_ARCHIVE_DTYPE)
        except OSError:
            return 'complex64'
        except tomllib.TOMLDecodeError as e:
            logger.warning(
                f"ResourceGuardian: cannot read archive_dtype from "
                f"{config_path} ({e}); sizing the archive as complex64")
            return 'complex64'
        try:
            return validate_archive_dtype(value)
        except ValueError as e:
            logger.warning(
                f"ResourceGuardian: {e}; sizing the archive as complex64")
            return 'complex64'

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
        evict_policy = EVICT_POLICY_AUTO
        evict_grace_sec = EVICT_GRACE_SEC
        evict_low_water_percent = EVICT_LOW_WATER_PERCENT

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
                        elif key == 'evict_policy':
                            evict_policy = value
                        elif key == 'evict_grace_sec':
                            evict_grace_sec = float(value)
                        elif key == 'evict_low_water_percent':
                            evict_low_water_percent = float(value)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not parse {config_path}: {e}")

        archive_dtype = cls._read_archive_dtype(config_path)

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
            evict_policy=evict_policy,
            evict_grace_sec=evict_grace_sec,
            evict_low_water_percent=evict_low_water_percent,
            archive_dtype=archive_dtype,
        )
