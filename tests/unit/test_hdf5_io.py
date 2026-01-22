"""
Unit tests for HDF5 data product writer and reader
"""

import pytest
import h5py
import numpy as np
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import shutil

from hf_timestd.io import DataProductWriter, DataProductReader


@pytest.fixture
def temp_dir():
    """Create temporary directory for test files."""
    temp_path = Path(tempfile.mkdtemp())
    yield temp_path
    shutil.rmtree(temp_path)


@pytest.fixture
def sample_l2_measurement():
    """Create a sample L2 timing measurement."""
    return {
        'timestamp_utc': '2025-12-24T12:00:00Z',
        'minute_boundary_utc': 1735041600,
        'rtp_timestamp': 123456789,
        'station': 'WWV',
        'frequency_mhz': 10.0,
        'discrimination_method': 'TONE',
        'discrimination_confidence': 0.85,
        'tone_detected': True,
        'raw_arrival_time_ms': 5.38,
        'clock_offset_ms': -2.14,
        'uncertainty_ms': 1.2,
        'expanded_uncertainty_ms': 2.4,
        'coverage_factor': 2.0,
        'confidence_level': 0.95,
        'u_rtp_timestamp_ms': 0.05,
        'u_ionospheric_ms': 1.0,
        'u_multipath_ms': 0.5,
        'u_discrimination_ms': 0.3,
        'u_gpsdo_ms': 0.001,
        'u_propagation_model_ms': 0.3,
        'degrees_of_freedom': 1000,
        'quality_grade': 'B',
        'confidence': 0.85,
        'quality_flag': 'GOOD',
        'propagation_delay_ms': 5.38,
        'propagation_mode': '1E',
        'n_hops': 1,
        'snr_db': 15.3,
        'traceability_chain': 'GPSDO → UTC(GPS) → UTC(NIST)',
        'processing_version': '3.2.0',
        'processed_at': '2025-12-24T12:01:00Z',
        'calibration_date': '2025-12-01T00:00:00Z',
        'gpsdo_locked': True,
    }


class TestDataProductWriter:
    """Test HDF5 data product writer."""
    
    def test_create_writer(self, temp_dir):
        """Test creating a writer."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        assert writer.product_level == 'L2'
        assert writer.product_name == 'timing_measurements'
        assert writer.channel == 'WWV_10000'
        # Check version is valid semver format
        assert len(writer.schema['schema_version'].split('.')) == 3
    
    def test_write_valid_measurement(self, temp_dir, sample_l2_measurement):
        """Test writing a valid measurement."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Should not raise
        writer.write_measurement(sample_l2_measurement)
        writer.close()
        
        # Check file was created
        hdf5_path = temp_dir / 'WWV_10000_timing_measurements_20251224.h5'
        assert hdf5_path.exists()
        
        # Check file contents
        with h5py.File(hdf5_path, 'r') as f:
            assert 'timestamp_utc' in f
            assert f['timestamp_utc'].shape[0] == 1
            assert f['clock_offset_ms'][0] == -2.14
            assert f['station'][0].decode('utf-8') == 'WWV'
    
    def test_reject_nan_in_required_field(self, temp_dir, sample_l2_measurement):
        """Test that NaN is rejected in required fields that don't allow NaN."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Set uncertainty_ms to NaN (not allowed per schema - no allow_nan flag)
        sample_l2_measurement['uncertainty_ms'] = np.nan
        
        with pytest.raises(ValueError, match="NaN"):
            writer.write_measurement(sample_l2_measurement)
    
    def test_reject_inf_in_required_field(self, temp_dir, sample_l2_measurement):
        """Test that inf is rejected in required fields."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Set uncertainty_ms to inf (not allowed per schema)
        sample_l2_measurement['uncertainty_ms'] = np.inf
        
        with pytest.raises(ValueError, match="inf"):
            writer.write_measurement(sample_l2_measurement)
    
    def test_reject_invalid_enum(self, temp_dir, sample_l2_measurement):
        """Test that invalid enum values are rejected."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Set station to invalid value
        sample_l2_measurement['station'] = 'INVALID'
        
        with pytest.raises(ValueError, match="not in allowed values"):
            writer.write_measurement(sample_l2_measurement)
    
    def test_reject_out_of_range(self, temp_dir, sample_l2_measurement):
        """Test that out-of-range values are rejected."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Set confidence to out-of-range value
        sample_l2_measurement['confidence'] = 1.5  # Must be 0-1
        
        with pytest.raises(ValueError, match="outside valid range"):
            writer.write_measurement(sample_l2_measurement)
    
    def test_reject_missing_required_field(self, temp_dir, sample_l2_measurement):
        """Test that missing required fields are rejected."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Remove required field
        del sample_l2_measurement['clock_offset_ms']
        
        with pytest.raises(ValueError, match="Required field"):
            writer.write_measurement(sample_l2_measurement)
    
    def test_daily_file_rotation(self, temp_dir, sample_l2_measurement):
        """Test that files rotate daily."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Write measurement for Dec 24
        writer.write_measurement(sample_l2_measurement)
        
        # Write measurement for Dec 25
        sample_l2_measurement['timestamp_utc'] = '2025-12-25T12:00:00Z'
        writer.write_measurement(sample_l2_measurement)
        
        writer.close()
        
        # Check both files exist
        file_20251224 = temp_dir / 'WWV_10000_timing_measurements_20251224.h5'
        file_20251225 = temp_dir / 'WWV_10000_timing_measurements_20251225.h5'
        
        assert file_20251224.exists()
        assert file_20251225.exists()
    
    def test_file_metadata(self, temp_dir, sample_l2_measurement):
        """Test that file metadata is written correctly."""
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000',
            processing_version='3.2.0',
            station_metadata={'latitude': 38.918461, 'longitude': -92.127974}
        )
        
        writer.write_measurement(sample_l2_measurement)
        writer.close()
        
        # Check metadata
        hdf5_path = temp_dir / 'WWV_10000_timing_measurements_20251224.h5'
        with h5py.File(hdf5_path, 'r') as f:
            # Check version is valid semver format
            assert len(f.attrs['schema_version'].split('.')) == 3
            assert f.attrs['data_product'] == 'L2_timing_measurements'
            assert f.attrs['processing_level'] == 'L2'
            assert f.attrs['processing_version'] == '3.2.0'
            assert f.attrs['channel'] == 'WWV_10000'
            assert f.attrs['station_latitude'] == 38.918461
            assert f.attrs['station_longitude'] == -92.127974
    
    def test_context_manager(self, temp_dir, sample_l2_measurement):
        """Test using writer as context manager."""
        with DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        ) as writer:
            writer.write_measurement(sample_l2_measurement)
        
        # File should be closed and exist
        hdf5_path = temp_dir / 'WWV_10000_timing_measurements_20251224.h5'
        assert hdf5_path.exists()


class TestDataProductReader:
    """Test HDF5 data product reader."""
    
    def test_create_reader(self, temp_dir):
        """Test creating a reader."""
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        assert reader.product_level == 'L2'
        assert reader.product_name == 'timing_measurements'
        assert reader.channel == 'WWV_10000'
    
    def test_read_time_range(self, temp_dir, sample_l2_measurement):
        """Test reading measurements in a time range."""
        # Write test data
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Write 3 measurements
        for i in range(3):
            sample_l2_measurement['timestamp_utc'] = f'2025-12-24T12:{i:02d}:00Z'
            sample_l2_measurement['clock_offset_ms'] = -2.0 - i * 0.1
            writer.write_measurement(sample_l2_measurement)
        
        writer.close()
        
        # Read back
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        measurements = reader.read_time_range(
            start='2025-12-24T00:00:00Z',
            end='2025-12-24T23:59:59Z'
        )
        
        assert len(measurements) == 3
        assert measurements[0]['clock_offset_ms'] == -2.0
        assert measurements[1]['clock_offset_ms'] == -2.1
        assert measurements[2]['clock_offset_ms'] == -2.2
    
    def test_quality_filtering(self, temp_dir, sample_l2_measurement):
        """Test quality filtering."""
        # Write test data with different quality grades
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        grades = ['A', 'B', 'C', 'D']
        for i, grade in enumerate(grades):
            sample_l2_measurement['timestamp_utc'] = f'2025-12-24T12:{i:02d}:00Z'
            sample_l2_measurement['quality_grade'] = grade
            writer.write_measurement(sample_l2_measurement)
        
        writer.close()
        
        # Read with quality filter
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Filter for grade B or better
        measurements = reader.read_time_range(
            start='2025-12-24T00:00:00Z',
            end='2025-12-24T23:59:59Z',
            min_quality_grade='B'
        )
        
        assert len(measurements) == 2  # A and B only
        assert measurements[0]['quality_grade'] == 'A'
        assert measurements[1]['quality_grade'] == 'B'
    
    def test_get_quality_summary(self, temp_dir, sample_l2_measurement):
        """Test getting quality summary statistics."""
        # Write test data
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        grades = ['A', 'A', 'B', 'C', 'D']
        for i, grade in enumerate(grades):
            sample_l2_measurement['timestamp_utc'] = f'2025-12-24T12:{i:02d}:00Z'
            sample_l2_measurement['quality_grade'] = grade
            writer.write_measurement(sample_l2_measurement)
        
        writer.close()
        
        # Get summary
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        summary = reader.get_quality_summary('20251224')
        
        assert summary['total_measurements'] == 5
        assert summary['grade_distribution']['A'] == 2
        assert summary['grade_distribution']['B'] == 1
        assert summary['grade_distribution']['C'] == 1
        assert summary['grade_distribution']['D'] == 1
    
    def test_list_available_dates(self, temp_dir, sample_l2_measurement):
        """Test listing available dates."""
        # Write data for multiple dates
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        dates = ['2025-12-24', '2025-12-25', '2025-12-26']
        for date in dates:
            sample_l2_measurement['timestamp_utc'] = f'{date}T12:00:00Z'
            writer.write_measurement(sample_l2_measurement)
        
        writer.close()
        
        # List dates
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        available_dates = reader.list_available_dates()
        
        assert len(available_dates) == 3
        assert '20251224' in available_dates
        assert '20251225' in available_dates
        assert '20251226' in available_dates
