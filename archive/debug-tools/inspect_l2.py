import h5py
import sys
import numpy as np

filepath = "/var/lib/timestd/phase2/WWV_20000/clock_offset/WWV_20000_timing_measurements_20260107.h5"

try:
    with h5py.File(filepath, 'r', swmr=True, libver='latest') as f:
        print(f"Keys: {list(f.keys())}")
        if 'timestamp_utc' in f:
            n = len(f['timestamp_utc'])
            print(f"Total rows: {n}")
            start = max(0, n - 10)
            
            timestamps = f['timestamp_utc'][start:]
            grades = f['quality_grade'][start:] if 'quality_grade' in f else ['?']*10
            flags = f['quality_flag'][start:] if 'quality_flag' in f else ['?']*10
            
            toa_data = f['raw_arrival_time_ms'][start:] if 'raw_arrival_time_ms' in f else ['?']*10
            
            for i in range(len(timestamps)):
                ts = timestamps[i].decode('utf-8') if isinstance(timestamps[i], bytes) else timestamps[i]
                grade = grades[i].decode('utf-8') if isinstance(grades[i], bytes) else grades[i]
                flag = flags[i].decode('utf-8') if isinstance(flags[i], bytes) else flags[i]
                toa = toa_data[i]
                print(f"{ts} | Grade: {grade} | Flag: {flag} | TOA: {toa}")
        else:
            print("No timestamp_utc dataset")
except Exception as e:
    print(f"Error: {e}")
