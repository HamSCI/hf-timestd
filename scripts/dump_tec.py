import h5py
import numpy as np

# Check multiple channels for the same minute to see mode mixing
minutes = ['2026-01-09T17:34:00Z', '2026-01-09T17:35:00Z', '2026-01-09T17:36:00Z', '2026-01-09T17:37:00Z']
channels = ['SHARED_5000', 'SHARED_10000', 'SHARED_15000', 'WWV_20000', 'WWV_25000']

for minute in minutes:
    print(f"\n--- Minute: {minute} ---")
    for channel in channels:
        file_path = f"/var/lib/timestd/phase2/{channel}/clock_offset/{channel}_timing_measurements_20260109.h5"
        try:
            with h5py.File(file_path, 'r', swmr=True, libver='latest') as f:
                ts = [t.decode('utf-8') for t in f['timestamp_utc'][:]]
                if minute in ts:
                    idx = ts.index(minute)
                    mode = f['propagation_mode'][idx].decode('utf-8')
                    toa = f['tof_kalman_ms'][idx]
                    if np.isnan(toa): toa = f['raw_arrival_time_ms'][idx]
                    print(f"  {channel:12}: Mode={mode:10} ToA={toa:7.3f}ms")
                else:
                    print(f"  {channel:12}: No data")
        except Exception as e:
            print(f"  {channel:12}: Error {e}")
