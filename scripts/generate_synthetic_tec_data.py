#!/usr/bin/env python3
"""
Generate synthetic TEC-affected data for verification.
Populates /tmp/timestd-test with WWV 10/15/20 data exhibiting 50 TECU.
"""

import json
import logging
import random
import shutil
import time
from pathlib import Path

import numpy as np

from hf_timestd.paths import TimeStdPaths

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gen_synthetic")

def generate_data():
    paths = TimeStdPaths("/tmp/timestd-test")
    
    # 1. Clean existing phase2 data
    phase2_dir = paths.data_root / "phase2"
    if phase2_dir.exists():
        logger.info(f"Cleaning {phase2_dir}")
        shutil.rmtree(phase2_dir)
    
    # 2. Setup frequencies and parameters
    # WWV at 40N, 105W (approx)
    stations = ['WWV_10_MHz', 'WWV_15_MHz', 'WWV_20_MHz']
    freqs = {
        'WWV_10_MHz': 10.0,
        'WWV_15_MHz': 15.0,
        'WWV_20_MHz': 20.0
    }
    
    # Physics Model
    # Delay = 40.3 * TEC / f^2
    # f in Hz. Delay in seconds.
    TEC_U = 50.0 # 50 TECU
    TEC_EL = TEC_U * 1e16
    K = 40.3
    
    # True "Vacuum" offset (D_clock) we want to find
    TRUE_OFFSET_MS = 5.0 
    
    # Generate 1 hour of data (60 files)
    start_time = int(time.time()) - 3600
    start_time = (start_time // 60) * 60 # Align to minute
    
    files_created = 0
    
    for i in range(60):
        t = start_time + (i * 60)
        
        for ch_name in stations:
            f_mhz = freqs[ch_name]
            f_hz = f_mhz * 1e6
            
            # Calculate Ionospheric Delay
            delay_sec = (K * TEC_EL) / (f_hz**2)
            delay_ms = delay_sec * 1000.0
            
            # Simulated Measurement = True Offset + Iono Delay + Noise
            # Note: The fusion engine subtracts the *model* delay.
            # Here we assume the "model" delay is 0 for simplicity, 
            # or that this propagation_delay_ms is the TOTAL delay?
            #
            # In fusion: toa_ms = m.d_clock_ms + m.propagation_delay_ms
            # m.d_clock_ms comes from the file.
            #
            # Let's say d_clock_ms in file = (True_Offset + Iono_Delay) - Model_Delay
            # If we set Model_Delay = 0 in the file, then:
            # d_clock_ms = True_Offset + Iono_Delay
            
            sim_d_clock = TRUE_OFFSET_MS + delay_ms + random.gauss(0, 0.5) # 0.5ms noise
            
            # Create JSON structure
            data = {
                "channel": ch_name,
                "unix_time": t,
                "d_clock_ms": sim_d_clock,
                "d_clock_uncertainty_ms": 1.0, # Good confidence
                "propagation_delay_ms": 0.0,   # Unmodelled (so all delay is in d_clock)
                "propagation_mode": "F",       # Synthetic
                "quality_grade": "A",
                "rx_lat": 40.0,
                "rx_lon": -100.0,
                "stats": {
                    "snr_db": 20.0,
                    "tone_consistency": 0.99
                }
            }
            
            # Write file
            out_dir = paths.get_clock_offset_dir(ch_name)
            out_dir.mkdir(parents=True, exist_ok=True)
            
            filename = f"clock_offset_{time.strftime('%Y%m%d_%H%M00', time.gmtime(t))}.json"
            
            with open(out_dir / filename, 'w') as f:
                json.dump(data, f, indent=2)
                
            # Append to CSV
            csv_path = out_dir / 'clock_offset_series.csv'
            csv_exists = csv_path.exists()
            
            with open(csv_path, 'a') as f:
                if not csv_exists:
                    f.write("system_time,station,frequency_mhz,clock_offset_ms,propagation_delay_ms,propagation_mode,confidence,snr_db,quality_grade\n")
                
                # Extract station name from channel (WWV_10_MHz -> WWV)
                station_name = ch_name.split('_')[0]
                
                f.write(f"{t},{station_name},{f_mhz},{sim_d_clock},{0.0},{'F'},{1.0},{20.0},{'A'}\n")
                
            files_created += 1
            
    logger.info(f"Generated {files_created} synthetic measurement files.")
    logger.info(f"Expected Results: TEC ≈ {TEC_U} TECU, D_clock ≈ {TRUE_OFFSET_MS} ms")

if __name__ == "__main__":
    generate_data()
