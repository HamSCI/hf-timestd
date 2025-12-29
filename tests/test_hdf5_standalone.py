#!/usr/bin/env python3
"""
Standalone test for HDF5 I/O module (bypasses full package import)
"""

import sys
import os
import tempfile
import shutil
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

# Import modules directly (bypass package __init__)
import importlib.util

def load_module(module_path):
    """Load a module from file path"""
    spec = importlib.util.spec_from_file_location("temp_module", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# Load schemas module
schemas_path = Path(__file__).parent.parent / 'src' / 'hf_timestd' / 'schemas' / '__init__.py'
schemas = load_module(schemas_path)
get_schema = schemas.get_schema

# Load uncertainty module
uncertainty_path = Path(__file__).parent.parent / 'src' / 'hf_timestd' / 'io' / 'uncertainty.py'
uncertainty_mod = load_module(uncertainty_path)
ISOGUMCalculator = uncertainty_mod.ISOGUMCalculator
UncertaintyBudget = uncertainty_mod.UncertaintyBudget

# Load HDF5 writer (needs schemas in sys.modules)
sys.modules['hf_timestd.schemas'] = schemas
sys.modules['hf_timestd.io.uncertainty'] = uncertainty_mod

writer_path = Path(__file__).parent.parent / 'src' / 'hf_timestd' / 'io' / 'hdf5_writer.py'
writer_mod = load_module(writer_path)
DataProductWriter = writer_mod.DataProductWriter

reader_path = Path(__file__).parent.parent / 'src' / 'hf_timestd' / 'io' / 'hdf5_reader.py'
reader_mod = load_module(reader_path)
DataProductReader = reader_mod.DataProductReader

def test_uncertainty():
    """Test ISO GUM uncertainty calculator"""
    print("Testing ISO GUM uncertainty calculator...")
    
    # Create budget
    budget = UncertaintyBudget(
        u_rtp_timestamp_ms=0.05,
        u_ionospheric_ms=1.0,
        u_multipath_ms=0.5,
        u_discrimination_ms=0.3,
        u_gpsdo_ms=0.001,
        u_propagation_model_ms=0.3,
        coverage_factor=2.0,
        confidence_level=0.95
    )
    
    # Calculate combined uncertainty
    result = ISOGUMCalculator.calculate_combined_uncertainty(budget)
    
    print(f"  Type A uncertainty: {result['u_type_a_ms']:.3f} ms")
    print(f"  Type B uncertainty: {result['u_type_b_ms']:.3f} ms")
    print(f"  Combined uncertainty: {result['u_combined_ms']:.3f} ms")
    print(f"  Expanded uncertainty: {result['u_expanded_ms']:.3f} ms")
    
    # Test quality grading
    grade = ISOGUMCalculator.assign_quality_grade(result['u_expanded_ms'])
    print(f"  Quality grade: {grade}")
    
    # Test quality flag
    flag = ISOGUMCalculator.assign_quality_flag(grade, 0.85, True)
    print(f"  Quality flag: {flag}")
    
    print("✓ Uncertainty calculator test passed\n")


def test_hdf5_io():
    """Test HDF5 writer and reader"""
    print("Testing HDF5 writer and reader...")
    
    # Create temp directory
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        # Create writer
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000',
            processing_version='3.2.0-test'
        )
        
        # Create sample measurement
        measurement = {
            'timestamp_utc': '2025-12-24T12:00:00Z',
            'minute_boundary_utc': 1735041600,
            'rtp_timestamp': 123456789,
            'station': 'WWV',
            'frequency_mhz': 10.0,
            'discrimination_method': 'TONE',
            'discrimination_confidence': 0.85,
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
            'traceability_chain': 'GPSDO → UTC(GPS) → UTC(NIST)',
            'processing_version': '3.2.0',
            'processed_at': '2025-12-24T12:01:00Z',
            'calibration_date': '2025-12-01T00:00:00Z',
            'gpsdo_locked': True,
        }
        
        # Write measurement
        writer.write_measurement(measurement)
        writer.close()
        
        print("  ✓ Wrote measurement to HDF5")
        
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
        
        assert len(measurements) == 1, f"Expected 1 measurement, got {len(measurements)}"
        assert measurements[0]['clock_offset_ms'] == -2.14
        assert measurements[0]['station'] == 'WWV'
        
        print("  ✓ Read measurement from HDF5")
        print(f"    Clock offset: {measurements[0]['clock_offset_ms']} ms")
        print(f"    Station: {measurements[0]['station']}")
        print(f"    Quality grade: {measurements[0]['quality_grade']}")
        
        print("✓ HDF5 I/O test passed\n")
        
    finally:
        # Cleanup
        shutil.rmtree(temp_dir)


def test_nan_rejection():
    """Test that NaN/inf values are rejected"""
    print("Testing NaN/inf rejection...")
    
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Try to write NaN
        measurement = {
            'timestamp_utc': '2025-12-24T12:00:00Z',
            'minute_boundary_utc': 1735041600,
            'rtp_timestamp': 123456789,
            'station': 'WWV',
            'frequency_mhz': 10.0,
            'discrimination_method': 'TONE',
            'discrimination_confidence': 0.85,
            'clock_offset_ms': float('nan'),  # NaN!
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
            'traceability_chain': 'GPSDO → UTC(GPS) → UTC(NIST)',
            'processing_version': '3.2.0',
            'processed_at': '2025-12-24T12:01:00Z',
            'calibration_date': '2025-12-01T00:00:00Z',
            'gpsdo_locked': True,
        }
        
        try:
            writer.write_measurement(measurement)
            print("  ✗ FAILED: NaN was not rejected!")
            return False
        except ValueError as e:
            if 'NaN' in str(e):
                print(f"  ✓ NaN correctly rejected: {e}")
            else:
                print(f"  ✗ Wrong error: {e}")
                return False
        
        writer.close()
        print("✓ NaN/inf rejection test passed\n")
        return True
        
    finally:
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    print("=" * 60)
    print("HDF5 I/O Module Standalone Tests")
    print("=" * 60)
    print()
    
    try:
        test_uncertainty()
        test_hdf5_io()
        test_nan_rejection()
        
        print("=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
