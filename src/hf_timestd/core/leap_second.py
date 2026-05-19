#!/usr/bin/env python3
"""
Leap Second Detection and Handling

Provides utilities for detecting and handling leap seconds in the hf-timestd
data pipeline. Leap seconds occur at 23:59:60 UTC on June 30 or December 31.

Key considerations for a time standard system:
1. During a positive leap second, minute 59 has 61 seconds (second 60 exists)
2. During a negative leap second, minute 59 has 59 seconds (second 59 skipped)
3. RTP timestamps continue monotonically - they don't "know" about leap seconds
4. Unix time (POSIX) doesn't include leap seconds - it repeats or skips

For hf-timestd:
- WWV/WWVH broadcast leap second warnings in the BCD time code
- The 29th second marker is doubled during a positive leap second
- We need to handle 61-second minutes in our sample buffers

Usage:
    from hf_timestd.core.leap_second import LeapSecondDetector
    
    detector = LeapSecondDetector()
    
    # Check if current time is near a potential leap second
    if detector.is_leap_second_window():
        logger.info("Leap second window - monitoring for 61-second minute")
    
    # Check if a specific minute had a leap second
    if detector.is_leap_second_minute(minute_boundary):
        # Allocate 61 seconds of samples instead of 60
        samples_expected = sample_rate * 61
"""

import bisect
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Compile-time fallback: 18 leap seconds as of 2017 (most recent insertion as of 2026)
_GPS_LEAP_SECONDS_FALLBACK = 18

# Conversion constants for the leap-second table.
_LEAP_SECONDS_FILE = "/usr/share/zoneinfo/leap-seconds.list"
# Seconds from the NTP epoch (1900-01-01) to the Unix epoch (1970-01-01).
# The first column of leap-seconds.list is an NTP timestamp.
_NTP_UNIX_OFFSET = 2_208_988_800
# Unix timestamp of the GPS epoch (1980-01-06 00:00:00 UTC).
_GPS_EPOCH_UNIX = 315_964_800
# TAI-UTC offset at the GPS epoch was 19 s, so GPS-UTC = DTAI - 19.
_TAI_GPS_OFFSET = 19
_NS_PER_S = 1_000_000_000

# Module cache: path -> (mtime, sorted_thresholds, parallel_offsets).
# Two parallel tuples are kept so bisect on a list of ints is hot-path cheap.
_LEAP_TABLE_CACHE: Dict[str, Tuple[float, Tuple[int, ...], Tuple[int, ...]]] = {}
_LEAP_TABLE_LOCK = threading.Lock()


def _parse_leap_seconds_file(path: str) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Parse ``leap-seconds.list`` into two parallel sorted tuples.

    Returns ``(gps_thresholds, gps_utc_offsets)`` where
    ``gps_thresholds[i]`` is the smallest GPS-seconds-since-GPS-epoch at
    which ``gps_utc_offsets[i]`` (= DTAI − 19) became effective.

    The file's first column is an NTP timestamp of the UTC moment the new
    DTAI takes effect (i.e. one second after the leap-second insertion).
    Converting to the GPS counter requires GPS = TAI − 19 and
    TAI = UTC + DTAI, so the GPS-epoch-relative threshold is
    ``utc_unix + dtai − 19 − GPS_EPOCH_UNIX``.

    Returns empty tuples if the file is missing or unparseable.
    """
    thresholds: list[int] = []
    offsets: list[int] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                try:
                    utc_ntp = int(parts[0])
                    dtai = int(parts[1])
                except ValueError:
                    continue
                utc_unix = utc_ntp - _NTP_UNIX_OFFSET
                gps_threshold = utc_unix + dtai - _TAI_GPS_OFFSET - _GPS_EPOCH_UNIX
                thresholds.append(gps_threshold)
                offsets.append(dtai - _TAI_GPS_OFFSET)
    except OSError:
        return (), ()

    # Sort by threshold (entries are written in chronological order, but be safe).
    order = sorted(range(len(thresholds)), key=thresholds.__getitem__)
    return (
        tuple(thresholds[i] for i in order),
        tuple(offsets[i] for i in order),
    )


def _get_leap_table(path: str) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Return the cached leap-second table for ``path``, re-parsing on mtime change."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        with _LEAP_TABLE_LOCK:
            _LEAP_TABLE_CACHE.pop(path, None)
        return (), ()

    with _LEAP_TABLE_LOCK:
        cached = _LEAP_TABLE_CACHE.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1], cached[2]

    # Parse outside the lock — file I/O shouldn't block other lookups.
    thresholds, offsets = _parse_leap_seconds_file(path)

    with _LEAP_TABLE_LOCK:
        _LEAP_TABLE_CACHE[path] = (mtime, thresholds, offsets)
    return thresholds, offsets


def gps_leap_seconds_at_gps_time(
    gps_time_ns: int,
    *,
    path: Optional[str] = None,
) -> int:
    """Return the GPS-UTC offset (s) effective at ``gps_time_ns``.

    Looks up the most recent entry in ``/usr/share/zoneinfo/leap-seconds.list``
    whose effective-from GPS time is ≤ ``gps_time_ns``. The file is
    mtime-cached, so steady-state lookups are an ``os.stat`` plus a
    bisect.

    Why per-call (and not captured once at import)?  ``hf-timestd`` is a
    multi-week daemon. If a leap second is inserted while the process
    runs, every UTC derived from GPS_TIME after the insertion would
    silently carry a 1 s error. Keying off the buffer's own GPS time
    means a leap-second boundary mid-stream picks up the correct offset
    on each side automatically — and a new ``leap-seconds.list`` shipped
    by the OS package manager is picked up without a process restart.

    Falls back to :data:`_GPS_LEAP_SECONDS_FALLBACK` (18) when the file
    is unavailable or the GPS time precedes every entry (pre-1980; not
    a real use case for this project).
    """
    if path is None:
        # Resolved lazily so tests that monkeypatch ``_LEAP_SECONDS_FILE``
        # take effect on every call site, including code paths that pass
        # no ``path=`` argument.
        path = _LEAP_SECONDS_FILE
    thresholds, offsets = _get_leap_table(path)
    if not thresholds:
        return _GPS_LEAP_SECONDS_FALLBACK

    gps_sec = gps_time_ns / _NS_PER_S
    idx = bisect.bisect_right(thresholds, gps_sec) - 1
    if idx < 0:
        # GPS time predates the table (pre-1972 UTC). Shouldn't happen
        # in this project; fall back rather than guess.
        return _GPS_LEAP_SECONDS_FALLBACK
    return offsets[idx]


def get_current_gps_leap_seconds() -> int:
    """Return the current GPS-UTC leap second offset (GPS is ahead of UTC).

    Reads /usr/share/zoneinfo/leap-seconds.list (IANA tzdata, updated by the OS
    package manager when a new leap second is announced) and converts from
    TAI-UTC to GPS-UTC.  Falls back to the compile-time constant (18) if the
    file is absent or unparseable.

    File format: non-comment lines are
        <NTP_timestamp>  <DTAI>  [# comment]
    where DTAI = TAI-UTC in seconds (currently 37).  GPS time is fixed
    relative to TAI by 19 s, so GPS-UTC = DTAI - 19 (currently 18).  Before
    this conversion was added, this helper returned the DTAI value directly,
    which produced a 19-second offset in every UTC derived from radiod's
    GPS_TIME and showed up as a ~19 s lag in RingBufferReader.head_utc
    after Phase 2 ring-buffer metrology went live.  Hosts without
    leap-seconds.list hit the 18 fallback and were accidentally correct.
    """
    try:
        with open("/usr/share/zoneinfo/leap-seconds.list") as fh:
            last_dtai = None
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        last_dtai = int(parts[1])
                    except ValueError:
                        pass
            if last_dtai is not None:
                return last_dtai - 19  # TAI-UTC → GPS-UTC
    except OSError:
        pass
    logger.debug(
        "leap-seconds.list unavailable; using fallback GPS_LEAP_SECONDS=%d",
        _GPS_LEAP_SECONDS_FALLBACK,
    )
    return _GPS_LEAP_SECONDS_FALLBACK


# Known leap seconds (positive) since 2000
# Format: (year, month, day) - leap second inserted at 23:59:60 UTC
KNOWN_LEAP_SECONDS = [
    (2005, 12, 31),
    (2008, 12, 31),
    (2012, 6, 30),
    (2015, 6, 30),
    (2016, 12, 31),
    # No leap seconds announced after 2016 as of 2025
    # The next possible dates are June 30 or December 31 of any year
]

# Leap second can only occur at end of June or December
LEAP_SECOND_MONTHS = {6, 12}


class LeapSecondDetector:
    """
    Detects and handles leap second events.
    
    Leap seconds are inserted at 23:59:60 UTC on the last day of June or December.
    This class provides utilities to:
    1. Check if we're in a potential leap second window
    2. Detect if a minute boundary corresponds to a leap second minute
    3. Calculate expected samples for a minute (60 or 61 seconds)
    """
    
    def __init__(self, sample_rate: int = 20000):
        """
        Initialize leap second detector.
        
        Args:
            sample_rate: Sample rate in Hz (default 20000)
        """
        self.sample_rate = sample_rate
        self._last_leap_check: Optional[float] = None
        self._in_leap_window = False
    
    def is_leap_second_candidate_day(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if the given date is a potential leap second day.
        
        Leap seconds can only occur on June 30 or December 31.
        
        Args:
            dt: Datetime to check (default: current UTC time)
            
        Returns:
            True if this is a potential leap second day
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        
        # June 30 or December 31
        if dt.month == 6 and dt.day == 30:
            return True
        if dt.month == 12 and dt.day == 31:
            return True
        return False
    
    def is_leap_second_window(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if we're in the leap second insertion window.
        
        The window is the last minute of June 30 or December 31 (23:59 UTC).
        
        Args:
            dt: Datetime to check (default: current UTC time)
            
        Returns:
            True if we're in the leap second window
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        
        if not self.is_leap_second_candidate_day(dt):
            return False
        
        # Leap second window is 23:59:00 - 23:59:60 UTC
        return dt.hour == 23 and dt.minute == 59
    
    def is_known_leap_second(self, year: int, month: int, day: int) -> bool:
        """
        Check if a specific date had a known leap second.
        
        Args:
            year: Year
            month: Month (1-12)
            day: Day of month
            
        Returns:
            True if this date had a leap second
        """
        return (year, month, day) in KNOWN_LEAP_SECONDS
    
    def get_minute_duration_seconds(self, minute_boundary: int) -> int:
        """
        Get the expected duration of a minute in seconds.
        
        Most minutes are 60 seconds. During a positive leap second,
        the last minute of June 30 or December 31 is 61 seconds.
        
        Args:
            minute_boundary: Unix timestamp of minute start
            
        Returns:
            60 or 61 seconds
        """
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        
        # Check if this is a leap second minute
        if self.is_leap_second_window(dt):
            # Check if this year had/has a leap second on this date
            if self.is_known_leap_second(dt.year, dt.month, dt.day):
                logger.info(f"Leap second minute detected: {dt.isoformat()}")
                return 61
        
        return 60
    
    def get_expected_samples(self, minute_boundary: int) -> int:
        """
        Get expected sample count for a minute.
        
        Args:
            minute_boundary: Unix timestamp of minute start
            
        Returns:
            Expected number of samples (sample_rate * 60 or 61)
        """
        duration = self.get_minute_duration_seconds(minute_boundary)
        return self.sample_rate * duration
    
    def check_for_leap_second_anomaly(
        self,
        minute_boundary: int,
        actual_samples: int,
        expected_samples: int
    ) -> Tuple[bool, str]:
        """
        Check if a sample count anomaly might be due to a leap second.
        
        This helps distinguish between:
        - Actual data loss (gap in recording)
        - Leap second (61 seconds of data in a 60-second buffer)
        
        Args:
            minute_boundary: Unix timestamp of minute start
            actual_samples: Actual samples received
            expected_samples: Expected samples (usually sample_rate * 60)
            
        Returns:
            (is_leap_second, explanation) tuple
        """
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        
        # Check if we got ~1 second extra samples
        extra_samples = actual_samples - expected_samples
        one_second = self.sample_rate
        
        if abs(extra_samples - one_second) < one_second * 0.01:  # Within 1%
            if self.is_leap_second_window(dt):
                return True, f"Leap second detected: {extra_samples} extra samples at {dt.isoformat()}"
        
        return False, ""


# Module-level detector instance for convenience
_detector: Optional[LeapSecondDetector] = None


def get_detector(sample_rate: int = 20000) -> LeapSecondDetector:
    """Get or create the module-level leap second detector."""
    global _detector
    if _detector is None or _detector.sample_rate != sample_rate:
        _detector = LeapSecondDetector(sample_rate)
    return _detector


def is_leap_second_window() -> bool:
    """Check if current time is in a leap second window."""
    return get_detector().is_leap_second_window()


def get_expected_samples(minute_boundary: int, sample_rate: int = 20000) -> int:
    """Get expected sample count for a minute, accounting for leap seconds."""
    return get_detector(sample_rate).get_expected_samples(minute_boundary)
