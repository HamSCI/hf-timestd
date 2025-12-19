
import pandas as pd
import numpy as np
import sys
from pathlib import Path
from datetime import datetime

def calculate_slope_sliding_window(df, window_minutes=30):
    """
    Calculate slope of d_clock (ms) -> Doppler (Hz) using sliding window.
    Doppler = -f_c * (slope_ms_per_s) / 1000
    slope_ms_per_s is d(clock_offset)/dt.
    """
    # Sort by time
    df = df.sort_values('minute_boundary_utc')
    t = df['minute_boundary_utc'].values
    y = df['clock_offset_ms'].values
    
    dopplers = []
    times = []
    
    half_window = window_minutes * 60 / 2
    
    for i in range(len(df)):
        t_center = t[i]
        # Valid window
        mask = (t >= t_center - half_window) & (t <= t_center + half_window)
        
        if np.sum(mask) < 5:
            dopplers.append(np.nan)
        else:
            # Simple linear regression for speed/robustness check
            t_win = t[mask]
            y_win = y[mask]
            
            # Fit line: y = mx + c
            # slope m is ms/s
            try:
                m, c = np.polyfit(t_win - t_center, y_win, 1)
                dopplers.append(m)
            except:
                dopplers.append(np.nan)
    
    return np.array(dopplers)

def analyze(channel_name, freq_mhz):
    # Load files
    base_dir = Path(f'/tmp/timestd-test/phase2/{channel_name}')
    date_str = '20251218'
    
    # Filenames might differ slightly in casing or dots
    # Usually: NAME_doppler_DATE.csv
    # e.g. WWV_20_MHz_doppler_...
    # Safe name: replace dots with underscores?
    # Actually the FIND command showed: WWV_20_MHz_doppler_20251218.csv
    safe_name = channel_name.replace('.', '_')
    
    # Try finding the file if exact name guess fails
    f_offset = base_dir / 'clock_offset' / f'{safe_name}_clock_offset_{date_str}.csv'
    f_doppler = base_dir / 'doppler' / f'{safe_name}_doppler_{date_str}.csv'
    
    if not f_offset.exists():
        # fallback try replacing dots in channel name
        safe_name = channel_name.replace('.', '_')
        f_offset = base_dir / 'clock_offset' / f'{safe_name}_clock_offset_{date_str}.csv'
        f_doppler = base_dir / 'doppler' / f'{safe_name}_doppler_{date_str}.csv'

    if not f_offset.exists() or not f_doppler.exists():
        print(f"Files not found for {channel_name} at {f_offset}")
        sys.exit(1)
        
    df_off = pd.read_csv(f_offset)
    df_dop = pd.read_csv(f_doppler)
    
    df = pd.merge(df_off, df_dop, left_on='minute_boundary_utc', right_on='minute_boundary', how='inner')
    
    print(f"Channel: {channel_name} ({freq_mhz} MHz)")
    print(f"Data points: {len(df)}")
    
    slopes_ms_per_s = calculate_slope_sliding_window(df)
    
    channel_hz = freq_mhz * 1e6
    df['group_doppler_hz'] = -channel_hz * (slopes_ms_per_s * 1e-3)
    
    # For WWV/WWVH, use wwv_doppler_hz (1000 Hz) usually?
    # Or both?
    # Let's use wwv for now.
    df['phase_doppler_hz'] = df['wwv_doppler_hz']
    
    valid = df.dropna(subset=['group_doppler_hz', 'phase_doppler_hz'])
    
    if len(valid) < 10:
        print("Not enough valid data")
        return

    corr = valid['group_doppler_hz'].corr(valid['phase_doppler_hz'])
    
    print(f"Correlation (Pearson): {corr:.4f}")
    print(valid[['group_doppler_hz', 'phase_doppler_hz']].describe())

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        name = sys.argv[1]
        freq = float(sys.argv[2])
        analyze(name, freq)
    else:
        # Default
        analyze('CHU_14.67_MHz', 14.67)
