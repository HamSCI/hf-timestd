#!/usr/bin/env python3
"""Compare VTEC data between CSV and HDF5 formats."""

import csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from hf_timestd.io import DataProductReader

def test_vtec_equivalence():
    """Compare last 24 hours of VTEC data."""
    
    print("=" * 60)
    print("VTEC Data Equivalence Test")
    print("=" * 60)
    
    # Read CSV data
    csv_path = Path('/var/lib/timestd/gnss_vtec.csv')
    csv_data = []
    
    print(f"\nReading CSV data from {csv_path}...")
    if csv_path.exists():
        with open(csv_path) as f:
            reader = csv.reader(f)
            for line in reader:
                if line[0] != 'timestamp':
                    try:
                        csv_data.append({
                            'timestamp': float(line[0]),
                            'vtec': float(line[1]),
                            'nsats': int(line[2])
                        })
                    except (ValueError, IndexError):
                        continue
        print(f"  Found {len(csv_data)} CSV records")
    else:
        print(f"  CSV file not found!")
        return False
    
    # Read HDF5 data
    hdf5_dir = Path('/var/lib/timestd/data/gnss_vtec')
    print(f"\nReading HDF5 data from {hdf5_dir}...")
    
    try:
        reader = DataProductReader(
            data_dir=hdf5_dir,
            product_level='L3',
            product_name='gnss_vtec',
            channel='GNSS'
        )
        
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=24)).isoformat().replace('+00:00', 'Z')
        end = now.isoformat().replace('+00:00', 'Z')
        
        hdf5_data = reader.read_time_range(start=start, end=end)
        print(f"  Found {len(hdf5_data)} HDF5 records")
    except Exception as e:
        print(f"  HDF5 read failed: {e}")
        return False
    
    # Compare
    print(f"\nComparing data...")
    matches = 0
    mismatches = 0
    vtec_diffs = []
    unmatched_hdf5 = 0
    
    for h5_meas in hdf5_data:
        h5_ts = h5_meas['unix_timestamp']
        h5_vtec = h5_meas['vtec_tecu']
        
        # Find matching CSV entry (within 1 second)
        matched = False
        for csv_meas in csv_data:
            if abs(csv_meas['timestamp'] - h5_ts) < 1.0:
                vtec_diff = abs(csv_meas['vtec'] - h5_vtec)
                vtec_diffs.append(vtec_diff)
                
                if vtec_diff < 0.1:  # 0.1 TECU threshold
                    matches += 1
                else:
                    mismatches += 1
                    if mismatches <= 5:  # Show first 5 mismatches
                        print(f'  Mismatch: CSV={csv_meas["vtec"]:.2f}, HDF5={h5_vtec:.2f}, diff={vtec_diff:.2f} TECU')
                matched = True
                break
        
        if not matched:
            unmatched_hdf5 += 1
    
    # Statistics
    print(f"\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    if vtec_diffs:
        import statistics
        print(f'Total comparisons: {len(vtec_diffs)}')
        print(f'Matches: {matches} ({matches/len(vtec_diffs)*100:.1f}%)')
        print(f'Mismatches: {mismatches}')
        print(f'Unmatched HDF5 records: {unmatched_hdf5}')
        print(f'\nDifference Statistics:')
        print(f'  Mean: {statistics.mean(vtec_diffs):.3f} TECU')
        print(f'  Max: {max(vtec_diffs):.3f} TECU')
        print(f'  Std dev: {statistics.stdev(vtec_diffs) if len(vtec_diffs) > 1 else 0:.3f} TECU')
        
        # Pass/Fail
        pass_rate = matches / len(vtec_diffs)
        print(f'\nAcceptance Criteria:')
        print(f'  Pass rate >= 99%: {"✓" if pass_rate >= 0.99 else "✗"} ({pass_rate*100:.1f}%)')
        print(f'  Max diff < 1 TECU: {"✓" if max(vtec_diffs) < 1.0 else "✗"} ({max(vtec_diffs):.3f})')
        
        if pass_rate >= 0.99 and max(vtec_diffs) < 1.0:
            print(f'\n{"=" * 60}')
            print('✓ VTEC EQUIVALENCE TEST PASSED')
            print("=" * 60)
            return True
        else:
            print(f'\n{"=" * 60}')
            print('✗ VTEC EQUIVALENCE TEST FAILED')
            print("=" * 60)
            return False
    else:
        print('No overlapping data found')
        return False

if __name__ == '__main__':
    import sys
    success = test_vtec_equivalence()
    sys.exit(0 if success else 1)
