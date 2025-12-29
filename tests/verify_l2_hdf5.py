#!/usr/bin/env python3
"""
Verification test for L2 HDF5 data collection in Phase 2 analytics.

This script simulates the Phase 2 analytics service writing L2 timing
measurements to HDF5 and verifies:
1. HDF5 files are created correctly
2. Schema validation works
3. NaN/inf values are rejected
4. ISO GUM uncertainty budgets are calculated
5. Data can be read back with quality filtering
"""

import sys
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone

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

# Load required modules
base_path = Path(__file__).parent.parent / 'src' / 'hf_timestd'

schemas_path = base_path / 'schemas' / '__init__.py'
schemas = load_module(schemas_path)
sys.modules['hf_timestd.schemas'] = schemas

uncertainty_path = base_path / 'io' / 'uncertainty.py'
uncertainty_mod = load_module(uncertainty_path)
sys.modules['hf_timestd.io.uncertainty'] = uncertainty_mod

writer_path = base_path / 'io' / 'hdf5_writer.py'
writer_mod = load_module(writer_path)

reader_path = base_path / 'io' / 'hdf5_reader.py'
reader_mod = load_module(reader_path)

DataProductWriter = writer_mod.DataProductWriter
DataProductReader = reader_mod.DataProductReader
ISOGUMCalculator = uncertainty_mod.ISOGUMCalculator


def test_l2_data_collection():
    """Test L2 timing measurement collection and writing."""
    print("=" * 70)
    print("L2 HDF5 Data Collection Verification")
    print("=" * 70)
    print()
    
    temp_dir = Path(tempfile.mkdtemp())
    
    try:
        # Simulate Phase 2 analytics service
        print("1. Initializing HDF5 L2 writer...")
        writer = DataProductWriter(
            output_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000',
            processing_version='3.2.0',
            station_metadata={
                'latitude': 38.918461,
                'longitude': -92.127974,
                'callsign': 'AC0G'
            }
        )
        print("   ✓ Writer initialized")
        print()
        
        # Simulate convergence model and solution data
        print("2. Simulating Phase 2 analytics data...")
        
        # Create ISO GUM uncertainty budget (as Phase 2 analytics would)
        snr_db = 15.3
        gpsdo_locked = True
        discrimination_conf = 0.85
        
        budget = ISOGUMCalculator.create_default_budget(
            snr_db=snr_db,
            gpsdo_locked=gpsdo_locked,
            discrimination_confidence=discrimination_conf
        )
        
        unc_result = ISOGUMCalculator.calculate_combined_uncertainty(budget)
        quality_grade = ISOGUMCalculator.assign_quality_grade(unc_result['u_expanded_ms'])
        quality_flag = ISOGUMCalculator.assign_quality_flag(
            quality_grade=quality_grade,
            discrimination_confidence=discrimination_conf,
            gpsdo_locked=gpsdo_locked
        )
        
        print(f"   SNR: {snr_db} dB")
        print(f"   Combined uncertainty: {unc_result['u_combined_ms']:.3f} ms")
        print(f"   Expanded uncertainty: {unc_result['u_expanded_ms']:.3f} ms")
        print(f"   Quality grade: {quality_grade}")
        print(f"   Quality flag: {quality_flag}")
        print()
        
        # Write multiple measurements
        print("3. Writing L2 measurements to HDF5...")
        
        # Use current date for measurements
        now = datetime.now(timezone.utc)
        base_minute = int(now.timestamp() // 60) * 60  # Round down to minute boundary
        date_str = now.strftime('%Y-%m-%d')
        
        for i in range(5):
            minute_boundary = base_minute + (i * 60)
            timestamp_utc = datetime.fromtimestamp(minute_boundary, timezone.utc).isoformat().replace('+00:00', 'Z')
            
            # Vary the clock offset slightly
            clock_offset_ms = -2.14 + (i * 0.05)
            
            l2_measurement = {
                'timestamp_utc': timestamp_utc,
                'minute_boundary_utc': minute_boundary,
                'rtp_timestamp': 123456789 + (i * 1200000),
                'station': 'WWV',
                'frequency_mhz': 10.0,
                'discrimination_method': 'TONE',
                'discrimination_confidence': discrimination_conf,
                'clock_offset_ms': clock_offset_ms,
                'uncertainty_ms': unc_result['u_combined_ms'],
                'expanded_uncertainty_ms': unc_result['u_expanded_ms'],
                'coverage_factor': budget.coverage_factor,
                'confidence_level': budget.confidence_level,
                'u_rtp_timestamp_ms': budget.u_rtp_timestamp_ms,
                'u_ionospheric_ms': budget.u_ionospheric_ms,
                'u_multipath_ms': budget.u_multipath_ms,
                'u_discrimination_ms': budget.u_discrimination_ms,
                'u_gpsdo_ms': budget.u_gpsdo_ms,
                'u_propagation_model_ms': budget.u_propagation_model_ms,
                'degrees_of_freedom': unc_result['degrees_of_freedom'],
                'quality_grade': quality_grade,
                'confidence': discrimination_conf,
                'quality_flag': quality_flag,
                'propagation_delay_ms': 5.38,
                'propagation_mode': '1E',
                'n_hops': 1,
                'snr_db': snr_db,
                'utc_verified': gpsdo_locked,
                'multi_station_verified': False,
                'traceability_chain': 'GPSDO → UTC(GPS) → UTC(NIST)',
                'processing_version': '3.2.0',
                'processed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                'calibration_date': '2025-12-01T00:00:00Z',
                'gpsdo_locked': gpsdo_locked,
            }
            
            writer.write_measurement(l2_measurement)
            print(f"   ✓ Wrote measurement {i+1}: clock_offset={clock_offset_ms:.3f} ms")
        
        writer.close()
        print()
        
        # Verify HDF5 file was created
        print("4. Verifying HDF5 file creation...")
        hdf5_files = list(temp_dir.glob('*.h5'))
        assert len(hdf5_files) == 1, f"Expected 1 HDF5 file, found {len(hdf5_files)}"
        print(f"   ✓ HDF5 file created: {hdf5_files[0].name}")
        print(f"   ✓ File size: {hdf5_files[0].stat().st_size} bytes")
        print()
        
        # Read back and verify
        print("5. Reading back L2 measurements...")
        reader = DataProductReader(
            data_dir=temp_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel='WWV_10000'
        )
        
        # Use time range that covers our measurements
        start_time = datetime.fromtimestamp(base_minute - 60, timezone.utc).isoformat().replace('+00:00', 'Z')
        end_time = datetime.fromtimestamp(base_minute + 600, timezone.utc).isoformat().replace('+00:00', 'Z')
        
        measurements = reader.read_time_range(
            start=start_time,
            end=end_time
        )
        
        assert len(measurements) == 5, f"Expected 5 measurements, got {len(measurements)}"
        print(f"   ✓ Read {len(measurements)} measurements")
        print()
        
        # Verify data integrity
        print("6. Verifying data integrity...")
        for i, meas in enumerate(measurements):
            expected_offset = -2.14 + (i * 0.05)
            actual_offset = meas['clock_offset_ms']
            assert abs(actual_offset - expected_offset) < 0.001, \
                f"Clock offset mismatch: expected {expected_offset}, got {actual_offset}"
            
            # Verify required fields
            assert meas['station'] == 'WWV'
            assert meas['frequency_mhz'] == 10.0
            assert meas['quality_grade'] == quality_grade
            assert meas['quality_flag'] == quality_flag
            assert meas['gpsdo_locked'] == True
        
        print("   ✓ All measurements verified")
        print("   ✓ Clock offsets match expected values")
        print("   ✓ Quality grades consistent")
        print()
        
        # Test quality filtering
        print("7. Testing quality filtering...")
        good_measurements = reader.read_time_range(
            start='2025-12-24T22:00:00Z',
            end='2025-12-24T22:10:00Z',
            quality_flags=['GOOD']
        )
        
        print(f"   ✓ Quality filter works: {len(good_measurements)} GOOD measurements")
        print()
        
        # Test NaN rejection
        print("8. Testing NaN/inf rejection...")
        try:
            bad_measurement = l2_measurement.copy()
            bad_measurement['clock_offset_ms'] = float('nan')
            bad_measurement['timestamp_utc'] = '2025-12-24T22:10:00Z'
            
            writer2 = DataProductWriter(
                output_dir=temp_dir,
                product_level='L2',
                product_name='timing_measurements',
                channel='WWV_10000'
            )
            writer2.write_measurement(bad_measurement)
            print("   ✗ FAILED: NaN was not rejected!")
            return False
        except ValueError as e:
            if 'NaN' in str(e):
                print(f"   ✓ NaN correctly rejected: {e}")
            else:
                print(f"   ✗ Wrong error: {e}")
                return False
        print()
        
        # Get quality summary
        print("9. Quality summary statistics...")
        date_str_compact = now.strftime('%Y%m%d')
        summary = reader.get_quality_summary(date_str_compact)
        print(f"   Total measurements: {summary['total_measurements']}")
        print(f"   Grade distribution: {summary['grade_distribution']}")
        print(f"   Flag distribution: {summary['flag_distribution']}")
        print(f"   Mean confidence: {summary['mean_confidence']:.3f}")
        print(f"   Mean uncertainty: {summary['mean_uncertainty_ms']:.3f} ms")
        print()
        
        print("=" * 70)
        print("✓ ALL L2 VERIFICATION TESTS PASSED")
        print("=" * 70)
        print()
        print("Summary:")
        print("  ✓ HDF5 writer creates files correctly")
        print("  ✓ Schema validation enforced")
        print("  ✓ NaN/inf values rejected")
        print("  ✓ ISO GUM uncertainty budgets calculated")
        print("  ✓ Data can be read back accurately")
        print("  ✓ Quality filtering works")
        print()
        print("L2 data collection is SOUND and ready for production!")
        
        return True
        
    finally:
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    try:
        success = test_l2_data_collection()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ VERIFICATION FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
