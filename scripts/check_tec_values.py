#!/usr/bin/env python3
"""
Quick verification script to check if TEC values are realistic after the fix.
Run this after the science aggregator completes its hourly cycle.

Usage:
    python3 scripts/check_tec_values.py
"""

import h5py
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

def check_tec_values():
    """Check TEC values in the aggregated HDF5 file."""
    
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    tec_file = Path(f'/var/lib/timestd/phase2/science/tec/AGGREGATED_tec_{today}.h5')
    
    if not tec_file.exists():
        print(f'❌ TEC file not found: {tec_file}')
        return False
    
    print(f'✓ Reading TEC data from: {tec_file}')
    print('=' * 80)
    
    with h5py.File(tec_file, 'r', swmr=True) as f:
        tec_tecu = f['tec_tecu'][:]
        timestamp = f['timestamp_utc'][:]
        station = f['station'][:]
        confidence = f['confidence'][:]
        quality = f['quality_flag'][:]
        n_freq = f['n_frequencies'][:]
        
        # Statistics
        total = len(tec_tecu)
        realistic = np.sum((np.abs(tec_tecu) >= 2.0) & (np.abs(tec_tecu) <= 50.0))
        moderate = np.sum((np.abs(tec_tecu) >= 0.5) & (np.abs(tec_tecu) < 2.0))
        small = np.sum((np.abs(tec_tecu) >= 0.1) & (np.abs(tec_tecu) < 0.5))
        near_zero = np.sum(np.abs(tec_tecu) < 0.1)
        
        good_quality = np.sum([q.decode() if isinstance(q, bytes) else q == 'GOOD' for q in quality])
        marginal_quality = np.sum([q.decode() if isinstance(q, bytes) else q == 'MARGINAL' for q in quality])
        
        print(f'\nTEC Value Distribution:')
        print(f'  Realistic (2-50 TECU):  {realistic:4d} / {total} ({100*realistic/total:.1f}%)')
        print(f'  Moderate (0.5-2 TECU):  {moderate:4d} / {total} ({100*moderate/total:.1f}%)')
        print(f'  Small (0.1-0.5 TECU):   {small:4d} / {total} ({100*small/total:.1f}%)')
        print(f'  Near-zero (<0.1 TECU):  {near_zero:4d} / {total} ({100*near_zero/total:.1f}%)')
        
        print(f'\nQuality Flag Distribution:')
        print(f'  GOOD:     {good_quality:4d} / {total} ({100*good_quality/total:.1f}%)')
        print(f'  MARGINAL: {marginal_quality:4d} / {total} ({100*marginal_quality/total:.1f}%)')
        print(f'  BAD:      {total-good_quality-marginal_quality:4d} / {total} ({100*(total-good_quality-marginal_quality)/total:.1f}%)')
        
        # Show recent measurements
        print(f'\nRecent TEC Measurements (last 15):')
        print('-' * 80)
        for i in range(max(0, len(tec_tecu)-15), len(tec_tecu)):
            ts = timestamp[i].decode() if isinstance(timestamp[i], bytes) else str(timestamp[i])
            st = station[i].decode() if isinstance(station[i], bytes) else str(station[i])
            q = quality[i].decode() if isinstance(quality[i], bytes) else str(quality[i])
            
            # Marker for realistic values
            if 2.0 <= abs(tec_tecu[i]) <= 50.0:
                marker = '✓✓✓'
            elif 0.5 <= abs(tec_tecu[i]) < 2.0:
                marker = '✓✓'
            elif 0.1 <= abs(tec_tecu[i]) < 0.5:
                marker = '✓'
            else:
                marker = '✗'
            
            print(f'{marker} {ts} {st:4s}: TEC={tec_tecu[i]:7.2f} TECU, n_freq={n_freq[i]}, conf={confidence[i]:.3f}, {q}')
        
        print('\n' + '=' * 80)
        
        # Overall assessment
        if realistic > 0:
            print('✓✓✓ SUCCESS! TEC values are in realistic range (2-50 TECU)')
            print('    The propagation.html TEC fix is WORKING!')
            return True
        elif moderate + small > 0:
            print('✓✓ PROGRESS! Some TEC values are non-zero')
            print('   May need more multi-frequency data or better signal conditions')
            return True
        else:
            print('✗ All TEC values still near zero')
            print('  Check if multi-frequency measurements have realistic raw_arrival_time_ms')
            return False

if __name__ == '__main__':
    success = check_tec_values()
    exit(0 if success else 1)
