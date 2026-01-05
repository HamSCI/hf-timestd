#!/usr/bin/env python3
"""
Verify TEC Fix - Check that raw_arrival_time_ms is present and TEC values are realistic

Run this script after 2026-01-06 00:00 UTC to verify the TEC fix is working.

Usage:
    python3 scripts/verify_tec_fix.py
"""

import h5py
import numpy as np
from pathlib import Path
from datetime import datetime
import sys

def check_hdf5_schema(file_path: Path) -> dict:
    """Check if HDF5 file has raw_arrival_time_ms dataset."""
    results = {
        'file_exists': False,
        'has_raw_arrival': False,
        'schema_version': None,
        'total_records': 0,
        'non_nan_raw_arrival': 0,
        'sample_values': []
    }
    
    if not file_path.exists():
        return results
    
    results['file_exists'] = True
    
    try:
        with h5py.File(file_path, 'r') as f:
            results['schema_version'] = f.attrs.get('schema_version', 'unknown')
            
            if 'raw_arrival_time_ms' in f:
                results['has_raw_arrival'] = True
                raw_data = f['raw_arrival_time_ms'][:]
                results['total_records'] = len(raw_data)
                results['non_nan_raw_arrival'] = int(np.sum(~np.isnan(raw_data)))
                
                # Get sample values (non-NaN)
                non_nan_values = raw_data[~np.isnan(raw_data)]
                if len(non_nan_values) > 0:
                    results['sample_values'] = non_nan_values[:5].tolist()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    
    return results

def check_tec_values(tec_dir: Path, date_str: str) -> dict:
    """Check TEC values in HDF5 file."""
    results = {
        'file_exists': False,
        'total_measurements': 0,
        'tec_range': (None, None),
        'mean_tec': None,
        'good_quality_count': 0,
        'bad_quality_count': 0,
        'sample_tec_values': []
    }
    
    tec_file = tec_dir / f'AGGREGATED_tec_{date_str}.h5'
    if not tec_file.exists():
        return results
    
    results['file_exists'] = True
    
    try:
        with h5py.File(tec_file, 'r') as f:
            if 'tec_tecu' in f:
                tec_data = f['tec_tecu'][:]
                quality_data = f['quality_flag'][:]
                
                # Filter out NaN values
                valid_tec = tec_data[~np.isnan(tec_data)]
                
                if len(valid_tec) > 0:
                    results['total_measurements'] = len(valid_tec)
                    results['tec_range'] = (float(np.min(valid_tec)), float(np.max(valid_tec)))
                    results['mean_tec'] = float(np.mean(valid_tec))
                    results['sample_tec_values'] = valid_tec[:5].tolist()
                
                # Count quality flags
                for q in quality_data:
                    q_str = q.decode() if isinstance(q, bytes) else str(q)
                    if q_str == 'GOOD':
                        results['good_quality_count'] += 1
                    elif q_str == 'BAD':
                        results['bad_quality_count'] += 1
    except Exception as e:
        print(f"Error reading TEC file: {e}")
    
    return results

def main():
    """Main verification routine."""
    print("=" * 80)
    print("TEC Fix Verification Script")
    print("=" * 80)
    print()
    
    # Determine date to check
    today = datetime.now().strftime('%Y%m%d')
    print(f"Checking date: {today}")
    print()
    
    # Check HDF5 timing measurement files
    print("1. Checking HDF5 Timing Measurement Files")
    print("-" * 80)
    
    phase2_dir = Path('/var/lib/timestd/phase2')
    channels_to_check = ['CHU_3330', 'CHU_7850', 'CHU_14670', 'WWV_20000']
    
    all_have_field = True
    total_non_nan = 0
    
    for channel in channels_to_check:
        file_path = phase2_dir / channel / f'{channel}_timing_measurements_{today}.h5'
        results = check_hdf5_schema(file_path)
        
        if results['file_exists']:
            status = "✓" if results['has_raw_arrival'] else "✗"
            print(f"{status} {channel}:")
            print(f"  Schema version: {results['schema_version']}")
            print(f"  Has raw_arrival_time_ms: {results['has_raw_arrival']}")
            
            if results['has_raw_arrival']:
                print(f"  Total records: {results['total_records']}")
                print(f"  Non-NaN values: {results['non_nan_raw_arrival']}")
                if results['sample_values']:
                    print(f"  Sample values: {[f'{v:.3f}' for v in results['sample_values']]}")
                total_non_nan += results['non_nan_raw_arrival']
            else:
                all_have_field = False
        else:
            print(f"⚠ {channel}: File not found (may not have data yet)")
        print()
    
    # Check TEC data
    print("2. Checking TEC Data Quality")
    print("-" * 80)
    
    tec_dir = phase2_dir / 'science' / 'tec'
    tec_results = check_tec_values(tec_dir, today)
    
    if tec_results['file_exists']:
        print(f"TEC file exists: ✓")
        print(f"Total measurements: {tec_results['total_measurements']}")
        
        if tec_results['mean_tec'] is not None:
            min_tec, max_tec = tec_results['tec_range']
            mean_tec = tec_results['mean_tec']
            
            # Check if TEC values are realistic (2-50 TECU range)
            realistic = 2.0 <= mean_tec <= 50.0
            status = "✓" if realistic else "✗"
            
            print(f"{status} TEC Range: {min_tec:.2f} - {max_tec:.2f} TECU")
            print(f"{status} Mean TEC: {mean_tec:.2f} TECU")
            print(f"  Sample values: {[f'{v:.2f}' for v in tec_results['sample_tec_values']]}")
            print(f"  Good quality: {tec_results['good_quality_count']}")
            print(f"  Bad quality: {tec_results['bad_quality_count']}")
            
            if not realistic:
                print()
                print("⚠ WARNING: TEC values are not in realistic range (2-50 TECU)")
                print("  This suggests raw_arrival_time_ms is still not being used correctly")
        else:
            print("✗ No valid TEC measurements found")
    else:
        print("⚠ TEC file not found (may not have data yet)")
    
    print()
    print("=" * 80)
    print("Summary")
    print("=" * 80)
    
    if all_have_field and total_non_nan > 0:
        print("✓ All checked files have raw_arrival_time_ms dataset")
        print(f"✓ Total non-NaN raw_arrival_time_ms values: {total_non_nan}")
    else:
        print("✗ Some files missing raw_arrival_time_ms dataset")
        print("  Wait for new files to be created at midnight UTC")
    
    if tec_results['mean_tec'] is not None:
        if 2.0 <= tec_results['mean_tec'] <= 50.0:
            print(f"✓ TEC values are realistic: {tec_results['mean_tec']:.2f} TECU")
            print("✓ TEC fix is working correctly!")
            return 0
        else:
            print(f"✗ TEC values are unrealistic: {tec_results['mean_tec']:.2e} TECU")
            print("  Expected range: 2-50 TECU")
            return 1
    else:
        print("⚠ No TEC data available yet")
        print("  Wait for science aggregator to process data")
        return 2

if __name__ == '__main__':
    sys.exit(main())
