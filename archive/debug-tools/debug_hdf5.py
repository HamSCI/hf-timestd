import h5py
import numpy as np
import sys

filepath = sys.argv[1]
try:
    with h5py.File(filepath, 'r', swmr=True) as f:
        print(f"Keys: {list(f.keys())}")
        if 'clock_offset_ms' in f:
            n = len(f['clock_offset_ms'])
            print(f"Total rows: {n}")
            start = max(0, n - 20)
            
            ts = f['timestamp_utc'][start:] if 'timestamp_utc' in f else [b'unknown']*20
            offsets = f['clock_offset_ms'][start:]
            grades = f['quality_grade'][start:] if 'quality_grade' in f else [b'?']*20
            toa_data = f['raw_arrival_time_ms'][start:] if 'raw_arrival_time_ms' in f else [0.0]*20
            
            flags = f['quality_flag'][start:] if 'quality_flag' in f else [b'?']*20
            
            for i in range(len(offsets)):
                t_str = ts[i].decode() if isinstance(ts[i], bytes) else str(ts[i])
                g_str = grades[i].decode() if isinstance(grades[i], bytes) else str(grades[i])
                f_str = flags[i].decode() if isinstance(flags[i], bytes) else str(flags[i])
                print(f"{t_str} | Offset: {offsets[i]:.3f} | Grade: {g_str} | Flag: {f_str} | TOA: {toa_data[i]:.3f}")
        else:
            print("No clock_offset_ms dataset")
except Exception as e:
    print(f"Error: {e}")
