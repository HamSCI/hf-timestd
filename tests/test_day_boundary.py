#!/usr/bin/env python3
"""
Tests for Day Boundary and Midnight Handling

Tests critical edge cases around:
1. Midnight UTC rollover (23:59 → 00:00)
2. HDF5 daily file rotation
3. Date string generation across day boundaries
4. Minute boundary calculations at midnight
"""

import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import os

from hf_timestd.io.hdf5_writer import DataProductWriter


class TestMidnightBoundary:
    """Tests for midnight/day boundary handling."""
    
    def test_minute_boundary_at_2359(self):
        """Test minute boundary calculation at 23:59 UTC."""
        # 23:59:00 UTC on Jan 15, 2025
        dt = datetime(2025, 1, 15, 23, 59, 0, tzinfo=timezone.utc)
        minute_boundary = int(dt.timestamp())
        
        # Verify the timestamp is correct
        recovered_dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        assert recovered_dt.hour == 23
        assert recovered_dt.minute == 59
        assert recovered_dt.day == 15
    
    def test_minute_boundary_at_0000(self):
        """Test minute boundary calculation at 00:00 UTC (next day)."""
        # 00:00:00 UTC on Jan 16, 2025
        dt = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        minute_boundary = int(dt.timestamp())
        
        # Verify the timestamp is correct
        recovered_dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        assert recovered_dt.hour == 0
        assert recovered_dt.minute == 0
        assert recovered_dt.day == 16
    
    def test_date_string_at_2359(self):
        """Test date string generation at 23:59 UTC."""
        dt = datetime(2025, 1, 15, 23, 59, 30, tzinfo=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        assert date_str == '20250115'
    
    def test_date_string_at_0000(self):
        """Test date string generation at 00:00 UTC (next day)."""
        dt = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        assert date_str == '20250116'
    
    def test_consecutive_minutes_across_midnight(self):
        """Test consecutive minute boundaries across midnight."""
        # 23:59 on Jan 15
        minute_2359 = int(datetime(2025, 1, 15, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        # 00:00 on Jan 16
        minute_0000 = int(datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        
        # They should be exactly 60 seconds apart
        assert minute_0000 - minute_2359 == 60
    
    def test_month_boundary_rollover(self):
        """Test day boundary at end of month."""
        # 23:59 on Jan 31
        dt_before = datetime(2025, 1, 31, 23, 59, 0, tzinfo=timezone.utc)
        # 00:00 on Feb 1
        dt_after = datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        assert dt_before.strftime('%Y%m%d') == '20250131'
        assert dt_after.strftime('%Y%m%d') == '20250201'
        
        # Consecutive minutes
        assert int(dt_after.timestamp()) - int(dt_before.timestamp()) == 60
    
    def test_year_boundary_rollover(self):
        """Test day boundary at end of year."""
        # 23:59 on Dec 31, 2025
        dt_before = datetime(2025, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        # 00:00 on Jan 1, 2026
        dt_after = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        assert dt_before.strftime('%Y%m%d') == '20251231'
        assert dt_after.strftime('%Y%m%d') == '20260101'
        
        # Consecutive minutes
        assert int(dt_after.timestamp()) - int(dt_before.timestamp()) == 60


class TestHDF5DailyRotation:
    """Tests for HDF5 daily file rotation."""
    
    def test_hdf5_path_generation_same_day(self):
        """Test HDF5 path generation for same day."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = DataProductWriter(
                output_dir=Path(tmpdir),
                product_level='L2',
                product_name='timing_measurements',
                channel='WWV_10000'
            )
            
            # Two timestamps on same day
            path1 = writer._get_hdf5_path('20250115')
            path2 = writer._get_hdf5_path('20250115')
            
            assert path1 == path2
            assert '20250115' in str(path1)
    
    def test_hdf5_path_generation_different_days(self):
        """Test HDF5 path generation for different days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = DataProductWriter(
                output_dir=Path(tmpdir),
                product_level='L2',
                product_name='timing_measurements',
                channel='WWV_10000'
            )
            
            path1 = writer._get_hdf5_path('20250115')
            path2 = writer._get_hdf5_path('20250116')
            
            assert path1 != path2
            assert '20250115' in str(path1)
            assert '20250116' in str(path2)
    
    def test_hdf5_filename_format(self):
        """Test HDF5 filename follows expected format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = DataProductWriter(
                output_dir=Path(tmpdir),
                product_level='L2',
                product_name='timing_measurements',
                channel='WWV_10000'
            )
            
            path = writer._get_hdf5_path('20250115')
            
            # Expected format: {channel}_{product_name}_{date}.h5
            assert path.name == 'WWV_10000_timing_measurements_20250115.h5'


class TestTimestampConversion:
    """Tests for timestamp conversion edge cases."""
    
    def test_iso_format_at_midnight(self):
        """Test ISO format generation at midnight."""
        dt = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        iso_str = dt.isoformat().replace('+00:00', 'Z')
        
        assert iso_str == '2025-01-16T00:00:00Z'
    
    def test_iso_format_at_2359(self):
        """Test ISO format generation at 23:59."""
        dt = datetime(2025, 1, 15, 23, 59, 59, tzinfo=timezone.utc)
        iso_str = dt.isoformat().replace('+00:00', 'Z')
        
        assert iso_str == '2025-01-15T23:59:59Z'
    
    def test_unix_timestamp_to_datetime_at_midnight(self):
        """Test Unix timestamp to datetime conversion at midnight."""
        # Create timestamp for midnight
        dt_original = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        unix_ts = int(dt_original.timestamp())
        
        # Convert back
        dt_recovered = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        
        assert dt_recovered == dt_original
        assert dt_recovered.hour == 0
        assert dt_recovered.minute == 0
        assert dt_recovered.day == 16
    
    def test_minute_boundary_from_arbitrary_timestamp(self):
        """Test calculating minute boundary from arbitrary timestamp."""
        # Some time in the middle of a minute
        dt = datetime(2025, 1, 15, 23, 59, 45, tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        
        # Calculate minute boundary (floor to minute)
        minute_boundary = ts - (ts % 60)
        
        # Verify it's at :00 seconds
        boundary_dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        assert boundary_dt.second == 0
        assert boundary_dt.minute == 59
        assert boundary_dt.hour == 23


class TestDayBoundaryInPipeline:
    """Tests for day boundary handling in data pipeline context."""
    
    def test_date_extraction_from_minute_boundary(self):
        """Test extracting date string from minute boundary timestamp."""
        # Minute at 23:59 on Jan 15
        minute_2359 = int(datetime(2025, 1, 15, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        dt = datetime.fromtimestamp(minute_2359, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        
        assert date_str == '20250115'
        
        # Minute at 00:00 on Jan 16
        minute_0000 = int(datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        dt = datetime.fromtimestamp(minute_0000, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        
        assert date_str == '20250116'
    
    def test_processing_across_midnight(self):
        """Simulate processing minutes across midnight boundary."""
        # Simulate 5 minutes of processing: 23:57, 23:58, 23:59, 00:00, 00:01
        base_time = datetime(2025, 1, 15, 23, 57, 0, tzinfo=timezone.utc)
        
        expected_dates = ['20250115', '20250115', '20250115', '20250116', '20250116']
        
        for i, expected_date in enumerate(expected_dates):
            minute_dt = base_time + timedelta(minutes=i)
            minute_boundary = int(minute_dt.timestamp())
            
            # Extract date for file routing
            dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
            date_str = dt.strftime('%Y%m%d')
            
            assert date_str == expected_date, f"Failed at minute {i}: expected {expected_date}, got {date_str}"
    
    def test_file_rotation_trigger(self):
        """Test that file rotation would be triggered at midnight."""
        # Simulate tracking current date
        current_date = '20250115'
        
        # Process minute at 23:59 - same file
        minute_2359 = int(datetime(2025, 1, 15, 23, 59, 0, tzinfo=timezone.utc).timestamp())
        dt = datetime.fromtimestamp(minute_2359, tz=timezone.utc)
        new_date = dt.strftime('%Y%m%d')
        needs_rotation = new_date != current_date
        assert needs_rotation is False
        
        # Process minute at 00:00 - new file needed
        minute_0000 = int(datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc).timestamp())
        dt = datetime.fromtimestamp(minute_0000, tz=timezone.utc)
        new_date = dt.strftime('%Y%m%d')
        needs_rotation = new_date != current_date
        assert needs_rotation is True


class TestTimezoneHandling:
    """Tests for timezone handling at day boundaries."""
    
    def test_utc_is_used_consistently(self):
        """Verify UTC is used for all day boundary calculations."""
        # Create a timestamp that's midnight UTC
        utc_midnight = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        
        # This should be Jan 16 in UTC
        assert utc_midnight.strftime('%Y%m%d') == '20250116'
        
        # Unix timestamp should round-trip correctly
        ts = int(utc_midnight.timestamp())
        recovered = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert recovered.strftime('%Y%m%d') == '20250116'
    
    def test_naive_datetime_warning(self):
        """Document that naive datetimes should not be used."""
        # This test documents expected behavior - naive datetimes
        # will use local timezone which can cause day boundary issues
        
        # Always use timezone-aware datetimes
        aware_dt = datetime(2025, 1, 16, 0, 0, 0, tzinfo=timezone.utc)
        assert aware_dt.tzinfo is not None
        
        # The codebase should always use timezone.utc
        # This is a documentation test, not a functional test


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
