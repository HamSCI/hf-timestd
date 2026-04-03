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

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Compile-time fallback: 18 leap seconds as of 2017 (most recent insertion as of 2026)
_GPS_LEAP_SECONDS_FALLBACK = 18


def get_current_gps_leap_seconds() -> int:
    """Return the current GPS-UTC leap second offset (GPS is ahead of UTC).

    Reads /usr/share/zoneinfo/leap-seconds.list (IANA tzdata, updated by the OS
    package manager when a new leap second is announced).  Falls back to the
    compile-time constant (18) if the file is absent or unparseable.

    File format: non-comment lines are:
        <NTP_timestamp>  <cumulative_leap_seconds>  [# comment]
    The last such line gives the current total.
    """
    try:
        with open("/usr/share/zoneinfo/leap-seconds.list") as fh:
            last_ls = None
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        last_ls = int(parts[1])
                    except ValueError:
                        pass
            if last_ls is not None:
                return last_ls
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
