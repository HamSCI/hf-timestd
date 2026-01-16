#!/usr/bin/env python3
"""
Tests for Leap Second Detection and Handling

Tests the leap_second.py module for:
1. Leap second candidate day detection
2. Leap second window detection
3. Known leap second lookup
4. Minute duration calculation (60 vs 61 seconds)
5. Sample count expectations
6. Anomaly detection for extra samples
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from hf_timestd.core.leap_second import (
    LeapSecondDetector,
    KNOWN_LEAP_SECONDS,
    LEAP_SECOND_MONTHS,
    get_detector,
    is_leap_second_window,
    get_expected_samples,
)


class TestLeapSecondDetector:
    """Tests for LeapSecondDetector class."""
    
    def test_init_default_sample_rate(self):
        """Test default sample rate initialization."""
        detector = LeapSecondDetector()
        assert detector.sample_rate == 20000
    
    def test_init_custom_sample_rate(self):
        """Test custom sample rate initialization."""
        detector = LeapSecondDetector(sample_rate=24000)
        assert detector.sample_rate == 24000


class TestLeapSecondCandidateDay:
    """Tests for is_leap_second_candidate_day method."""
    
    def test_june_30_is_candidate(self):
        """June 30 is a leap second candidate day."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert detector.is_leap_second_candidate_day(dt) is True
    
    def test_december_31_is_candidate(self):
        """December 31 is a leap second candidate day."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 12, 31, 12, 0, 0, tzinfo=timezone.utc)
        assert detector.is_leap_second_candidate_day(dt) is True
    
    def test_regular_day_not_candidate(self):
        """Regular days are not leap second candidates."""
        detector = LeapSecondDetector()
        # Test various non-candidate days
        test_dates = [
            datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 6, 29, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 7, 1, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 12, 30, 12, 0, 0, tzinfo=timezone.utc),
        ]
        for dt in test_dates:
            assert detector.is_leap_second_candidate_day(dt) is False, f"Failed for {dt}"
    
    def test_none_uses_current_time(self):
        """Passing None should use current UTC time."""
        detector = LeapSecondDetector()
        # This should not raise - just verify it runs
        result = detector.is_leap_second_candidate_day(None)
        assert isinstance(result, bool)


class TestLeapSecondWindow:
    """Tests for is_leap_second_window method."""
    
    def test_june_30_2359_is_window(self):
        """June 30 at 23:59 UTC is in the leap second window."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 6, 30, 23, 59, 30, tzinfo=timezone.utc)
        assert detector.is_leap_second_window(dt) is True
    
    def test_december_31_2359_is_window(self):
        """December 31 at 23:59 UTC is in the leap second window."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        assert detector.is_leap_second_window(dt) is True
    
    def test_june_30_2358_not_window(self):
        """June 30 at 23:58 UTC is NOT in the leap second window."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 6, 30, 23, 58, 59, tzinfo=timezone.utc)
        assert detector.is_leap_second_window(dt) is False
    
    def test_june_30_0000_not_window(self):
        """June 30 at 00:00 UTC is NOT in the leap second window."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 6, 30, 0, 0, 0, tzinfo=timezone.utc)
        assert detector.is_leap_second_window(dt) is False
    
    def test_regular_day_2359_not_window(self):
        """Regular day at 23:59 UTC is NOT in the leap second window."""
        detector = LeapSecondDetector()
        dt = datetime(2025, 7, 15, 23, 59, 30, tzinfo=timezone.utc)
        assert detector.is_leap_second_window(dt) is False


class TestKnownLeapSeconds:
    """Tests for known leap second lookup."""
    
    def test_known_leap_seconds_list(self):
        """Verify the known leap seconds list contains expected entries."""
        assert (2016, 12, 31) in KNOWN_LEAP_SECONDS
        assert (2015, 6, 30) in KNOWN_LEAP_SECONDS
        assert (2012, 6, 30) in KNOWN_LEAP_SECONDS
    
    def test_is_known_leap_second_true(self):
        """Test detection of known leap second dates."""
        detector = LeapSecondDetector()
        assert detector.is_known_leap_second(2016, 12, 31) is True
        assert detector.is_known_leap_second(2015, 6, 30) is True
    
    def test_is_known_leap_second_false(self):
        """Test non-leap-second dates return False."""
        detector = LeapSecondDetector()
        assert detector.is_known_leap_second(2017, 12, 31) is False
        assert detector.is_known_leap_second(2025, 6, 30) is False
    
    def test_leap_second_months_constant(self):
        """Verify LEAP_SECOND_MONTHS contains only June and December."""
        assert LEAP_SECOND_MONTHS == {6, 12}


class TestMinuteDuration:
    """Tests for minute duration calculation."""
    
    def test_regular_minute_is_60_seconds(self):
        """Regular minutes should be 60 seconds."""
        detector = LeapSecondDetector()
        # January 15, 2025 at 12:00 UTC
        minute_boundary = int(datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_minute_duration_seconds(minute_boundary) == 60
    
    def test_leap_second_minute_is_61_seconds(self):
        """Leap second minutes should be 61 seconds."""
        detector = LeapSecondDetector()
        # December 31, 2016 at 23:59 UTC (known leap second)
        minute_boundary = int(datetime(2016, 12, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_minute_duration_seconds(minute_boundary) == 61
    
    def test_candidate_day_without_leap_second_is_60(self):
        """Candidate day without actual leap second should be 60 seconds."""
        detector = LeapSecondDetector()
        # June 30, 2025 at 23:59 UTC (candidate but no leap second announced)
        minute_boundary = int(datetime(2025, 6, 30, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_minute_duration_seconds(minute_boundary) == 60


class TestExpectedSamples:
    """Tests for expected sample count calculation."""
    
    def test_regular_minute_samples(self):
        """Regular minute should expect sample_rate * 60 samples."""
        detector = LeapSecondDetector(sample_rate=20000)
        minute_boundary = int(datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_expected_samples(minute_boundary) == 20000 * 60
    
    def test_leap_second_minute_samples(self):
        """Leap second minute should expect sample_rate * 61 samples."""
        detector = LeapSecondDetector(sample_rate=20000)
        # December 31, 2016 at 23:59 UTC (known leap second)
        minute_boundary = int(datetime(2016, 12, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_expected_samples(minute_boundary) == 20000 * 61
    
    def test_custom_sample_rate(self):
        """Test with custom sample rate."""
        detector = LeapSecondDetector(sample_rate=24000)
        minute_boundary = int(datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_expected_samples(minute_boundary) == 24000 * 60


class TestLeapSecondAnomaly:
    """Tests for leap second anomaly detection."""
    
    def test_detect_leap_second_anomaly(self):
        """Detect leap second when extra samples match one second."""
        detector = LeapSecondDetector(sample_rate=20000)
        # December 31, 2016 at 23:59 UTC (known leap second)
        minute_boundary = int(datetime(2016, 12, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        
        expected_samples = 20000 * 60  # Normal expectation
        actual_samples = 20000 * 61    # Got one extra second
        
        is_leap, explanation = detector.check_for_leap_second_anomaly(
            minute_boundary, actual_samples, expected_samples
        )
        assert is_leap is True
        assert "Leap second detected" in explanation
    
    def test_no_anomaly_on_regular_minute(self):
        """No anomaly detected on regular minute with normal samples."""
        detector = LeapSecondDetector(sample_rate=20000)
        minute_boundary = int(datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        
        expected_samples = 20000 * 60
        actual_samples = 20000 * 60
        
        is_leap, explanation = detector.check_for_leap_second_anomaly(
            minute_boundary, actual_samples, expected_samples
        )
        assert is_leap is False
        assert explanation == ""
    
    def test_no_anomaly_for_data_loss(self):
        """Data loss (fewer samples) should not be detected as leap second."""
        detector = LeapSecondDetector(sample_rate=20000)
        minute_boundary = int(datetime(2016, 12, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        
        expected_samples = 20000 * 60
        actual_samples = 20000 * 55  # Lost 5 seconds of data
        
        is_leap, explanation = detector.check_for_leap_second_anomaly(
            minute_boundary, actual_samples, expected_samples
        )
        assert is_leap is False


class TestModuleLevelFunctions:
    """Tests for module-level convenience functions."""
    
    def test_get_detector_singleton(self):
        """get_detector should return same instance for same sample rate."""
        detector1 = get_detector(20000)
        detector2 = get_detector(20000)
        assert detector1 is detector2
    
    def test_get_detector_different_sample_rate(self):
        """get_detector should create new instance for different sample rate."""
        detector1 = get_detector(20000)
        detector2 = get_detector(24000)
        assert detector2.sample_rate == 24000
    
    def test_is_leap_second_window_function(self):
        """Test module-level is_leap_second_window function."""
        # This should not raise
        result = is_leap_second_window()
        assert isinstance(result, bool)
    
    def test_get_expected_samples_function(self):
        """Test module-level get_expected_samples function."""
        minute_boundary = int(datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())
        samples = get_expected_samples(minute_boundary, sample_rate=20000)
        assert samples == 20000 * 60


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""
    
    def test_midnight_boundary_after_leap_second(self):
        """Test the minute after a leap second (00:00 on Jan 1)."""
        detector = LeapSecondDetector()
        # January 1, 2017 at 00:00 UTC (minute after 2016 leap second)
        minute_boundary = int(datetime(2017, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_minute_duration_seconds(minute_boundary) == 60
    
    def test_minute_before_leap_second(self):
        """Test the minute before a leap second (23:58)."""
        detector = LeapSecondDetector()
        # December 31, 2016 at 23:58 UTC (minute before leap second)
        minute_boundary = int(datetime(2016, 12, 31, 23, 58, 0, tzinfo=timezone.utc).timestamp())
        assert detector.get_minute_duration_seconds(minute_boundary) == 60
    
    def test_all_known_leap_seconds(self):
        """Verify all known leap seconds are detected correctly."""
        detector = LeapSecondDetector()
        for year, month, day in KNOWN_LEAP_SECONDS:
            minute_boundary = int(datetime(year, month, day, 23, 59, 0, tzinfo=timezone.utc).timestamp())
            duration = detector.get_minute_duration_seconds(minute_boundary)
            assert duration == 61, f"Failed for {year}-{month:02d}-{day:02d}"
    
    def test_rtp_timestamp_continuity_concept(self):
        """
        Conceptual test: RTP timestamps should continue monotonically
        through leap seconds. This test documents the expected behavior.
        
        During a leap second:
        - UTC has second 60 (23:59:60)
        - RTP timestamp continues incrementing normally
        - We get 61 seconds worth of samples
        - The extra second is NOT a gap or discontinuity
        """
        detector = LeapSecondDetector(sample_rate=20000)
        
        # Simulate RTP behavior during leap second minute
        minute_boundary = int(datetime(2016, 12, 31, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        
        # RTP would deliver 61 seconds of samples
        expected_samples = detector.get_expected_samples(minute_boundary)
        assert expected_samples == 20000 * 61
        
        # The "extra" samples are legitimate, not a gap
        # This is important for buffer allocation


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
