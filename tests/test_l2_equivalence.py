#!/usr/bin/env python3
"""Compare L2 timing measurements between CSV and HDF5."""

import csv
from pathlib import Path
from datetime import datetime, timezone
from hf_timestd.io import DataProductReader
from hf_timestd.paths import TimeStdPaths

def test_l2_equivalence(channel='SHARED_10000', date_str=None):
    """Compare L2 timing measurements for a channel."""
    
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
    
    print(f"\n{'=' * 60}")
    print(f"L2 Timing Measurements Equivalence Test: {channel}")
    print("=" * 60)
    
    paths = TimeStdPaths('/var/lib/timestd')
    clock_offset_dir = paths.get_clock_offset_dir(channel)
    
    # Read CSV
    print(f"\nReading CSV data...")
    csv_file = list(clock_offset_dir.glob(f'*_clock_offset_{date_str}.csv'))
    csv_data = {}
    
    if csv_file:
        with open(csv_file[0]) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    minute = int(float(row['minute_boundary_utc']))
                    csv_data[minute] = {
                        'clock_offset_ms': float(row['clock_offset_ms']),
                        'uncertainty_ms': float(row.get('uncertainty_ms', 1.0)),
                        'station': row['station']
                    }
                except (ValueError, KeyError):
                    continue
        print(f"  Found {len(csv_data)} CSV records")
    else:
        print(f"  No CSV file found for {date_str}")
        return False
    
    # Read HDF5
    print(f"\nReading HDF5 data...")
    try:
        reader = DataProductReader(
            data_dir=clock_offset_dir,
            product_level='L2',
            product_name='timing_measurements',
            channel=channel
        )
        
        start = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00Z"
        end = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T23:59:59Z"
        
        hdf5_data = reader.read_time_range(start=start, end=end)
        print(f"  Found {len(hdf5_data)} HDF5 records")
    except Exception as e:
        print(f"  HDF5 read failed: {e}")
        return False
    
    # Compare
    print(f"\nComparing data...")
    matches = 0
    mismatches = 0
    clock_diffs = []
    
    for h5_meas in hdf5_data:
        minute = h5_meas.get('minute_boundary', int(h5_meas.get('unix_timestamp', 0)))
        
        if minute in csv_data:
            csv_val = csv_data[minute]['clock_offset_ms']
            h5_val = h5_meas['clock_offset_ms']
            diff = abs(csv_val - h5_val)
            clock_diffs.append(diff)
            
            # Tolerance: 0.001ms or 0.1% of value
            tolerance = max(0.001, abs(csv_val) * 0.001)
            
            if diff < tolerance:
                matches += 1
            else:
                mismatches += 1
                if mismatches <= 5:  # Show first 5 mismatches
                    print(f'  Mismatch at {minute}: CSV={csv_val:.3f}, HDF5={h5_val:.3f}, diff={diff:.6f}ms')
    
    # Statistics
    print(f"\n{'=' * 60}")
    print("RESULTS")
    print("=" * 60)
    
    if clock_diffs:
        import statistics
        print(f'Total comparisons: {len(clock_diffs)}')
        print(f'Matches: {matches} ({matches/len(clock_diffs)*100:.1f}%)')
        print(f'Mismatches: {mismatches}')
        print(f'\nDifference Statistics:')
        print(f'  Mean: {statistics.mean(clock_diffs):.6f} ms')
        print(f'  Max: {max(clock_diffs):.6f} ms')
        print(f'  Std dev: {statistics.stdev(clock_diffs) if len(clock_diffs) > 1 else 0:.6f} ms')
        
        pass_rate = matches / len(clock_diffs)
        print(f'\nAcceptance Criteria:')
        print(f'  Pass rate >= 99%: {"✓" if pass_rate >= 0.99 else "✗"} ({pass_rate*100:.1f}%)')
        print(f'  Max diff < 0.01ms: {"✓" if max(clock_diffs) < 0.01 else "✗"} ({max(clock_diffs):.6f}ms)')
        
        if pass_rate >= 0.99:
            print(f'\n✓ L2 EQUIVALENCE TEST PASSED ({channel})')
            return True
        else:
            print(f'\n✗ L2 EQUIVALENCE TEST FAILED ({channel})')
            return False
    else:
        print('No overlapping data found')
        return False

if __name__ == '__main__':
    import sys
    
    channels = ['SHARED_10000', 'WWV_20000', 'CHU_3330']
    results = []
    
    print("=" * 60)
    print("L2 TIMING MEASUREMENTS EQUIVALENCE TEST SUITE")
    print("=" * 60)
    
    for channel in channels:
        try:
            results.append(test_l2_equivalence(channel))
        except Exception as e:
            print(f"\nError testing {channel}: {e}")
            results.append(False)
    
    print(f"\n{'=' * 60}")
    print("OVERALL RESULTS")
    print("=" * 60)
    for i, channel in enumerate(channels):
        status = "✓ PASS" if results[i] else "✗ FAIL"
        print(f"{channel}: {status}")
    
    if all(results):
        print(f'\n✓ ALL L2 TESTS PASSED')
        sys.exit(0)
    else:
        print(f'\n✗ SOME L2 TESTS FAILED')
        sys.exit(1)
