#!/usr/bin/env python3
"""
Test script for HDF5 reader utility
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.hdf5_reader import (
    read_l2_timing_measurements,
    read_l1a_channel_observables,
    get_l2_timing_path,
    get_l1a_observables_path
)


def test_l2_reader():
    """Test L2 timing measurements reader"""
    print("Testing L2 Timing Measurements Reader...")
    print()
    
    # Test with an actual file
    test_file = Path('/var/lib/timestd/phase2/CHU_3330/clock_offset/CHU_3330_timing_measurements_20251225.h5')
    
    if not test_file.exists():
        print(f"✗ Test file not found: {test_file}")
        return False
    
    try:
        result = read_l2_timing_measurements(test_file, max_records=5)
        
        print("✓ Successfully read HDF5 file")
        print(f"  Source: {result['source']}")
        print(f"  Status: {result['status']}")
        print(f"  Total records: {result['statistics']['total_records']}")
        print(f"  Returned records: {result['statistics']['count']}")
        print(f"  Grade distribution: {result['grade_distribution']}")
        print()
        print("  Statistics:")
        print(f"    Min: {result['statistics']['min']:.3f} ms" if result['statistics']['min'] else "    Min: None")
        print(f"    Max: {result['statistics']['max']:.3f} ms" if result['statistics']['max'] else "    Max: None")
        print(f"    Mean: {result['statistics']['mean']:.3f} ms" if result['statistics']['mean'] else "    Mean: None")
        print(f"    Std: {result['statistics']['std']:.3f} ms" if result['statistics']['std'] else "    Std: None")
        print()
        print("  Sample measurements (first 3):")
        for i, m in enumerate(result['measurements'][:3], 1):
            print(f"    {i}. {m['timestamp']}")
            print(f"       Clock offset: {m['clock_offset_ms']:.3f} ± {m['uncertainty_ms']:.3f} ms")
            print(f"       Quality: {m['quality_grade']} ({m['quality_flag']}), Confidence: {m['confidence']:.3f}")
            print(f"       Station: {m['station']}, Method: {m['discrimination_method']}")
        
        return True
    except Exception as e:
        print(f"✗ Error reading L2 file: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_l1a_reader():
    """Test L1A channel observables reader"""
    print("\n\nTesting L1A Channel Observables Reader...")
    print()
    
    # Test with an actual file
    test_file = Path('/var/lib/timestd/phase2/CHU_3330/carrier_power/CHU_3330_channel_observables_20251225.h5')
    
    if not test_file.exists():
        print(f"✗ Test file not found: {test_file}")
        return False
    
    try:
        result = read_l1a_channel_observables(test_file, max_records=5)
        
        print("✓ Successfully read HDF5 file")
        print(f"  Source: {result['source']}")
        print(f"  Status: {result['status']}")
        print(f"  Total records: {result['total_records']}")
        print(f"  Returned records: {result['count']}")
        print()
        print("  Sample records (first 3):")
        for i, r in enumerate(result['records'][:3], 1):
            print(f"    {i}. {r['timestamp']}")
            print(f"       Quality: {r['quality_flag']}, Completeness: {r['data_completeness']*100:.1f}%")
            if 'carrier_power_db' in r:
                print(f"       Carrier power: {r['carrier_power_db']:.2f} dB")
            if 'carrier_snr_db' in r:
                print(f"       SNR: {r['carrier_snr_db']:.2f} dB")
            if 'carrier_doppler_hz' in r:
                print(f"       Doppler: {r['carrier_doppler_hz']:.3f} Hz")
        
        return True
    except Exception as e:
        print(f"✗ Error reading L1A file: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("=" * 60)
    print("HDF5 Reader Utility Test")
    print("=" * 60)
    
    l2_success = test_l2_reader()
    l1a_success = test_l1a_reader()
    
    print("\n" + "=" * 60)
    print("Test Results:")
    print(f"  L2 Reader: {'✓ PASS' if l2_success else '✗ FAIL'}")
    print(f"  L1A Reader: {'✓ PASS' if l1a_success else '✗ FAIL'}")
    print("=" * 60)
    
    return 0 if (l2_success and l1a_success) else 1


if __name__ == '__main__':
    sys.exit(main())
