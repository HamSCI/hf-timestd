#!/usr/bin/env python3
"""
Verify Dispersion in HDF5 Data
==============================

This script reads daily HDF5 timing measurement files and analyzes the
relationship between Frequency and Time of Arrival (ToA).

Purpose:
--------
To diagnose the "0.0 TEC" issue where the system seemingly observes zero
ionospheric delay. Physics dictates that lower frequencies must arrive
LATER than higher frequencies (dispersion).

    T_obs(f) = T_vac + K * TEC / f^2

If the slope of T_obs vs 1/f^2 is zero, then the data lacks dispersion.
This script calculates this slope for every minute of data.

Usage:
------
    python3 verify_dispersion.py --date 2026-01-05
    python3 verify_dispersion.py --latest
"""

import argparse
import sys
import numpy as np
import h5py
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import logging

# Setup logging
# Force config to override anything matplotlib might have done
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
logger = logging.getLogger(__name__)

# Constants
DATA_DIR = Path("/var/lib/timestd/phase2")

def find_files(date_str=None):
    """Find timing measurement files recursively."""
    if date_str:
        pattern = f"*{date_str}.h5"
    else:
        pattern = "*.h5"
        
    # We want files ending in _timing_measurements_YYYYMMDD.h5
    # They are deep in channel directories
    matches = list(DATA_DIR.rglob(pattern))
    
    # Filter for timing measurements
    timing_files = [f for f in matches if "_timing_measurements_" in f.name]
    return sorted(timing_files)

def read_file_data(filepath):
    """Read timing data from a single HDF5 file."""
    logger.info(f"Reading {filepath}")
    
    import shutil
    import os
    
    # Copy to temp file to avoid locking issues
    temp_path = Path("/tmp") / f"dispersion_check_{filepath.name}"
    try:
        shutil.copy2(filepath, temp_path)
        
        # CRITICAL FIX: Clear status flags
        import subprocess
        try:
            subprocess.run(["h5clear", "-s", str(temp_path)], check=True, capture_output=True)
        except Exception:
            pass # Best effort
            
    except Exception as e:
        logger.error(f"Failed to copy/prep file: {e}")
        return [], [], [], []

    data_tuples = []
    
    try:
        with h5py.File(temp_path, 'r') as f:
            # Handle both 'measurements' group (v1.1) and root-level datasets (v1.0/flat)
            if 'measurements' in f:
                grp = f['measurements']
            else:
                grp = f
            
            # Efficiently read columns
            if 'minute_boundary_utc' in grp:
                ts = grp['minute_boundary_utc'][:]
            elif 'minute_boundary' in grp:
                ts = grp['minute_boundary'][:]
            elif 'timestamp_utc' in grp:
                 ts = grp['timestamp_utc'][:] 
            else:
                return [], [], [], []

            if 'station' in grp:
                stations = grp['station'][:]
            else:
                stations = np.array([b'UNKNOWN'] * len(ts))
                
            if 'frequency_mhz' in grp:
                freqs = grp['frequency_mhz'][:]
            else:
                 return [], [], [], []
            
            # CRITICAL FIX: Prefer d_clock + propagation_delay for correct ToA reconstruction
            # The 'raw_arrival_time_ms' field in older files incorrectly contained d_clock
            
            # Map localized names
            d_clock_key = 'd_clock_ms'
            if 'd_clock_ms' not in grp and 'clock_offset_ms' in grp:
                d_clock_key = 'clock_offset_ms'
                
            if d_clock_key in grp and 'propagation_delay_ms' in grp:
                d_clocks = grp[d_clock_key][:]
                delays = grp['propagation_delay_ms'][:]
                
                # Handle possible shape mismatch or NaNs
                min_len_local = min(len(d_clocks), len(delays))
                d_clocks = d_clocks[:min_len_local]
                delays = delays[:min_len_local]
                
                toas = d_clocks + delays
                
                # Filter NaNs from reconstruction immediately
                valid_recon = ~np.isnan(toas)
                if np.sum(valid_recon) < len(toas):
                    print(f"DEBUG: filtered {len(toas) - np.sum(valid_recon)} NaN reconstructed ToAs")
                    # Need to slice all arrays to maintain synchronization
                    # Slice 'ts' if it matches length, otherwise it might be 1D per group?
                    # The script earlier reads users 'ts', 'stations', 'freqs' which correspond to the data
                    
                    # NOTE: This function reads separate arrays. We must slice ALL of them.
                    # But wait, stations/freqs/ts are read BEFORE this block.
                    # Need to be careful. Arrays in this block are local 'd_clocks', 'delays'
                    # The main arrays 'stations', 'freqs', 'ts' need slicing too.
                    
                    ts = ts[:min_len_local][valid_recon]
                    stations = stations[:min_len_local][valid_recon]
                    freqs = freqs[:min_len_local][valid_recon]
                    toas = toas[valid_recon]
                
                print(f"DEBUG: Reconstructed {len(toas)} ToAs from {d_clock_key} + delay")

            elif 'raw_arrival_time_ms' in grp:
                toas = grp['raw_arrival_time_ms'][:]
                print(f"DEBUG: Using raw_arrival_time_ms directly (Legacy Warning: may be d_clock)")
            else:
                return [], [], [], []

            # Convert bytes to string if needed
            if stations.dtype.kind == 'S':
                 stations = np.char.decode(stations)
            
            print(f"DEBUG: {filepath.name} - Lengths: ts={len(ts)}, stations={len(stations)}, freqs={len(freqs)}, toas={len(toas)}")
            
            # Inspect values
            if len(ts) > 0:
                print(f"DEBUG: Sample TS: {ts[:5]} ... {ts[-5:]}")
                print(f"DEBUG: Sample Freq: {freqs[:5]} ...")
                print(f"DEBUG: Sample ToA: {toas[:5]} ...")
                 
            # Fallback for zero timestamps
            if len(ts) > 0 and ts[0] == 0 and 'timestamp_utc' in grp:
                print("DEBUG: Timestamps are 0, falling back to timestamp_utc")
                ts_raw = grp['timestamp_utc'][:]
                
                # Convert ISO bytes to epoch
                ts_epoch = []
                for t_bytes in ts_raw:
                    try:
                        t_str = t_bytes.decode('utf-8') if isinstance(t_bytes, bytes) else str(t_bytes)
                        # Format: 2026-01-05T01:49:00Z
                        # Handle trailing Z
                        t_str = t_str.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(t_str)
                        ts_epoch.append(dt.timestamp())
                    except Exception as e:
                        # Fallback or NaN
                        ts_epoch.append(0.0)
                ts = np.array(ts_epoch)
                print(f"DEBUG: New Sample TS (Epoch): {ts[:5]} ...")
            
            # Additional cleanup: Filter out invalid Freq=0 or Timestamp=0
            # Also truncate all arrays to common length (min length)
            min_len = min(len(ts), len(stations), len(freqs), len(toas))
            if min_len < len(ts): print(f"DEBUG: Truncating from {len(ts)} to {min_len}")
            
            ts = ts[:min_len]
            stations = stations[:min_len]
            freqs = freqs[:min_len]
            toas = toas[:min_len]

            # Filter masks
            valid_mask = (ts > 0) & (freqs > 0) & (~np.isnan(toas))
            
            print(f"DEBUG: valid_mask sum={np.sum(valid_mask)}, shape={valid_mask.shape}")
            if np.sum(valid_mask) == 0:
                 print(f"DEBUG: Mask details - TS>0: {np.sum(ts>0)}, Freq>0: {np.sum(freqs>0)}, NotNaN: {np.sum(~np.isnan(toas))}")
            
            ts = ts[valid_mask]
            stations = stations[valid_mask]
            freqs = freqs[valid_mask]
            toas = toas[valid_mask]

            # Ensure stations are strings
            if stations.size > 0:
                try:
                    if stations.dtype.kind == 'S':
                        stations = np.char.decode(stations)
                    elif stations.dtype.kind == 'O' and isinstance(stations[0], bytes):
                        stations = np.array([s.decode('utf-8', errors='ignore') for s in stations])
                    elif stations.dtype.kind == 'U':
                        pass # Already unicode
                except Exception as e:
                    logger.warning(f"Failed to decode stations: {e}. Using raw repr.")
                    stations = np.array([str(s) for s in stations])

            return ts, stations, freqs, toas

    except Exception as e:
        logger.error(f"EXCEPTION in read_file_data: {e}")
        import traceback
        traceback.print_exc()
        return [], [], [], []
    finally:
        if temp_path.exists():
            os.remove(temp_path)

def analyze_aggregated_data(grouped_data):
    """Analyze aggregated data for dispersion."""
    print(f"\n{'TIMESTAMP':<20} {'STATION':<8} {'N_FREQ':<6} {'SLOPE (TEC)':<12} {'R2':<8} {'RANGE_MS':<10} {'STATUS':<10}")
    print("-" * 80)
    
    results = []
    
    for (t, s), data in grouped_data.items():
        if len(data) < 2:
            continue
            
        freqs_vec = np.array([x[0] for x in data])
        toas_vec = np.array([x[1] for x in data])
        
        # Check uniqueness of freqs to avoid same freq duplicates
        unique_freqs = len(np.unique(freqs_vec))
        if unique_freqs < 2:
             continue
        
        x = 1.0 / (freqs_vec ** 2)
        y = toas_vec
        
        # Fit line
        try:
            m, c = np.polyfit(x, y, 1)
            
            # Calculate R2
            y_pred = m * x + c
            y_mean = np.mean(y)
            sst = np.sum((y - y_mean)**2)
            sse = np.sum((y - y_pred)**2)
            r2 = 1.0 - (sse/sst) if sst > 1e-9 else 0.0
            
            # Range of ToAs
            trange = np.max(y) - np.min(y)
            
            ts_str = datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M')
            
            status = "OK"
            if abs(m) <= 0.0001: status = "FLAT" # No dispersion
            if m < -0.1: status = "INVERT" # Unphysical
            
            print(f"{ts_str:<20} {s:<8} {len(data):<6} {m:>.4f}       {r2:.4f}   {trange:.4f}     {status:<10}")
            
            results.append({
                'timestamp': t,
                'station': s,
                'status': status
            })
            
        except Exception:
            pass

    # Summary
    n_flat = sum(1 for r in results if r['status'] == 'FLAT')
    n_total = len(results)
    print("\n" + "="*80)
    print(f"Summary")
    print(f"Total Minute-Station Groups: {n_total}")
    print(f"Flat/Zero Dispersion Events: {n_flat} ({n_flat/n_total*100:.1f}%)" if n_total > 0 else "Flat: 0")

def main():
    parser = argparse.ArgumentParser(description="Verify ionospheric dispersion in timing data")
    parser.add_argument('--date', type=str, help='YYYY-MM-DD date to analyze')
    parser.add_argument('--latest', action='store_true', help='Analyze latest available file')
    args = parser.parse_args()
    
    files_to_analyze = []
    
    if args.date:
        date_str = args.date.replace('-', '')
        files_to_analyze = find_files(date_str)
        if not files_to_analyze:
            logger.error(f"No timing files found for date {args.date} in {DATA_DIR}")
            sys.exit(1)
            
    elif args.latest:
        all_files = find_files()
        if not all_files:
            logger.error(f"No timing files found in {DATA_DIR}")
            sys.exit(1)
            
        # Find latest file to determine latest date
        latest_file = max(all_files, key=lambda f: f.stat().st_mtime)
        
        # Extract date from filename: ...timing_measurements_YYYYMMDD.h5
        try:
            latest_date = latest_file.name.split('_')[-1].split('.')[0]
            logger.info(f"Latest date identified as: {latest_date}")
            files_to_analyze = find_files(latest_date)
        except Exception:
            logger.warning("Could not parse date from filename. analyzing only latest file.")
            files_to_analyze = [latest_file]
        
    else:
        # Default to today
        today_str = datetime.utcnow().strftime('%Y%m%d')
        files_to_analyze = find_files(today_str)
        
        if not files_to_analyze:
            logger.info(f"No files found for today ({today_str}). Checking for most recent...")
            all_files = find_files()
            if all_files:
                # Group by date
                by_date = defaultdict(list)
                for f in all_files:
                    # extract YYYYMMDD from filename
                    try:
                        d = f.name.split('_')[-1].split('.')[0]
                        by_date[d].append(f)
                    except:
                        pass
                
                if by_date:
                    latest_date = sorted(by_date.keys())[-1]
                    files_to_analyze = by_date[latest_date]
                    logger.info(f"Falling back to files from {latest_date}")

    if not files_to_analyze:
        logger.error("No files found to analyze.")
        sys.exit(1)

    print(f"Reading {len(files_to_analyze)} files...")
    
    # Global aggregation
    grouped_data = defaultdict(list)
    
    for f in files_to_analyze:
        ts, stations, freqs, toas = read_file_data(f)
        
        # Aggregate
        for t, s, freq, toa in zip(ts, stations, freqs, toas):
            grouped_data[(t, s)].append((freq, toa))
            
    # Analyze
    analyze_aggregated_data(grouped_data)

if __name__ == "__main__":
    main()
