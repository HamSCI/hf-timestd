#!/usr/bin/env python3
"""
Generate Metrology Report
=========================

Produces high-quality visualizations for the HF-TimeStd metrology dashboard.
Target audience: "Time Nuts" / Precision Metrology community.

Plots:
1. Multi-Source Allan Deviation (ADEV) - The "Money Plot"
2. Residuals vs Truth - Time Series
3. Consensus & Weighting - Heatmap
4. VTEC Correlation - Dual Axis

Usage:
    python3 scripts/generate_metrology_report.py --output-dir docs/images/metrology
"""

import argparse
import logging
import sys
import shutil
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import h5py

try:
    import allantools
    ALLANTOOLS_AVAILABLE = True
except ImportError:
    ALLANTOOLS_AVAILABLE = False

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
FUSION_CSV_PATH = Path('/var/lib/timestd/phase2/fusion/fused_d_clock.csv')
VTEC_DIR = Path('/var/lib/timestd/data/gnss_vtec')

def setup_plotting_style():
    """Apply a "Time Nut" approved style (Scientific/Engineering)."""
    plt.style.use('bmh') # Clean, professional grid
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.size': 10,
        'axes.labelsize': 11,
        'axes.titlesize': 12,
        'xtick.labelsize': 9,
        'ytick.labelsize': 9,
        'grid.alpha': 0.6,
        'lines.linewidth': 1.5,
        'figure.dpi': 150
    })

def load_fusion_data(csv_path: Path, hours: int = 24) -> pd.DataFrame:
    """Load fusion data from CSV."""
    if not csv_path.exists():
        logger.error(f"Fusion CSV not found: {csv_path}")
        sys.exit(1)
    
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_numeric(df['timestamp'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
    
    # Filter last N hours
    cutoff = datetime.now(df['datetime'].dt.tz) - timedelta(hours=hours)
    df = df[df['datetime'] > cutoff].copy()
    
    # Sort
    df = df.sort_values('timestamp')
    return df

def load_vtec_data(data_dir: Path, hours: int = 24) -> pd.DataFrame:
    """Load VTEC data from HDF5 files."""
    # Find recent files
    files = sorted(data_dir.glob("*.h5"))
    if not files:
        logger.warning(f"No VTEC HDF5 files found in {data_dir}")
        return pd.DataFrame()
        
    records = []
    cutoff_ts = (datetime.now() - timedelta(hours=hours)).timestamp()
    
    for fpath in files[-2:]: # Look at last 2 days to cover 24h
        temp_path = Path(f"/tmp/{fpath.name}")
        try:
            # Copy to temp to avoid locking issues if file is open for writing
            shutil.copy2(fpath, temp_path)
            
            with h5py.File(temp_path, 'r', libver='latest', swmr=True) as f:
                if 'vtec_tecu' not in f:
                    continue
                
                # Reading whole arrays for efficiency (assuming fits in RAM)
                ts = f['unix_timestamp'][:]
                vtec = f['vtec_tecu'][:]
                
                # Filter in memory
                mask = ts > cutoff_ts
                if np.any(mask):
                    df_chunk = pd.DataFrame({
                        'timestamp': ts[mask],
                        'vtec': vtec[mask]
                    })
                    records.append(df_chunk)
            
            # Clean up temp file
            temp_path.unlink(missing_ok=True)
            
        except Exception as e:
            logger.warning(f"Error reading {fpath}: {e}")
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            
    if records:
        df = pd.concat(records)
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
        return df.sort_values('timestamp')
    return pd.DataFrame()


def plot_adev(df: pd.DataFrame, output_path: Path):
    """Plot Modified Allan Deviation (MDEV) or OADEV."""
    logger.info("Generating ADEV plot...")
    
    if not ALLANTOOLS_AVAILABLE:
        logger.warning("allantools not installed. Skipping ADEV calculation.")
        return

    fig, ax = plt.subplots(figsize=(10, 8))
    
    # Plot Fusion
    # Data is sampled at ~1Hz or so? Intervals in CSV. 
    # Need to verify sample rate. Assuming approx constant.
    cols = {
        'Fusion Estimate': 'd_clock_fused_ms',
        'WWV': 'wwv_mean_ms',
        'WWVH': 'wwvh_mean_ms',
        'CHU': 'chu_mean_ms',
        'BPM': 'bpm_mean_ms'
    }
    
    colors = {'Fusion Estimate': 'firebrick', 'WWV': 'grey', 'WWVH': 'silver', 'CHU': 'darkgrey', 'BPM': 'gainsboro'}
    linestyles = {'Fusion Estimate': '-', 'WWV': '--', 'WWVH': ':', 'CHU': '-.', 'BPM': ':'}
    alphas = {'Fusion Estimate': 1.0, 'WWV': 0.6, 'WWVH': 0.6, 'CHU': 0.6, 'BPM': 0.6}
    widths = {'Fusion Estimate': 3, 'WWV': 1, 'WWVH': 1, 'CHU': 1, 'BPM': 1}

    for label, col in cols.items():
        if col not in df.columns:
            continue
            
        data = df[col].dropna()
        if len(data) < 100:
            continue
            
        # milliseconds -> seconds for ADEV standard unit
        phase_data_s = data.values * 1e-3 
        rate = 1.0 # Assuming 1Hz output, should verify from timestamp diff
        
        # Calculate mean interval
        mean_dt = np.diff(df['timestamp']).mean()
        if mean_dt > 0:
            rate = 1.0 / mean_dt

        # Calculate ADEV
        (taus, adev, errors, ns) = allantools.oadev(phase_data_s, rate=rate, data_type="phase", taus='decade')
        
        ax.loglog(taus, adev, label=label, color=colors.get(label, 'black'), 
                  linestyle=linestyles.get(label, '-'), alpha=alphas.get(label, 0.5),
                  linewidth=widths.get(label, 1))

    # Add Reference Line (e.g. 1e-6 / tau)
    # x = np.logspace(0, 4, 100)
    # y = 1e-6 / x
    # ax.loglog(x, y, 'k:', label='1/tau Reference', alpha=0.3)

    ax.set_title("Multi-Source Stability Analysis (OADEV)")
    ax.set_xlabel("Tau (s)")
    ax.set_ylabel("Sigma(tau)")
    ax.grid(True, which="both", ls="-", color='0.65')
    ax.legend()
    
    plt.savefig(output_path)
    plt.close()
    logger.info(f"Saved {output_path}")

def plot_residuals(df: pd.DataFrame, output_path: Path):
    """Plot Time Series residuals."""
    logger.info("Generating Residuals plot...")
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    # Raw vs Fused
    # Raw is essentially the mean of all raw measurements before calibration/fusion
    # Fused is the final output
    
    ax.plot(df['datetime'], df['d_clock_raw_ms'], label='Raw (Multi-Station Mean)', color='grey', alpha=0.5, lw=1)
    ax.plot(df['datetime'], df['d_clock_fused_ms'], label='Fusion Estimate', color='crimson', lw=2)
    
    # Add error bands if possible
    ax.fill_between(df['datetime'], 
                    df['d_clock_fused_ms'] - df['uncertainty_ms'],
                    df['d_clock_fused_ms'] + df['uncertainty_ms'],
                    color='crimson', alpha=0.1, label='Uncertainty (1σ)')

    ax.set_title("Clock Offset Residuals: Raw vs Fused")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Offset (ms)")
    ax.legend(loc='upper right')
    
    # Format X axis
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info(f"Saved {output_path}")

def plot_heatmap(df: pd.DataFrame, output_path: Path):
    """Plot Station Weight/Trust Heatmap (Stacked Area)."""
    logger.info("Generating Heatmap...")
    
    # Calculate inverse variance weights (proxy for trust)
    # If intra_std_ms is high, trust is low.
    # Weight ~ 1 / (std^2 + epsilon)
    
    stations = ['wwv', 'wwvh', 'chu', 'bpm']
    weights = pd.DataFrame(index=df['datetime'])
    
    for st in stations:
        col_std = f"{st}_intra_std_ms"
        if col_std in df.columns:
            # Replace 0 or NaN with high value (low trust)
            std = df[col_std].replace(0, np.nan).fillna(100) 
            w = 1.0 / (std**2)
            weights[st.upper()] = w.values
            
    # Normalize to 100%
    weights_pct = weights.div(weights.sum(axis=1), axis=0)
    
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.stackplot(weights_pct.index, weights_pct.T, labels=weights_pct.columns, alpha=0.8)
    
    ax.set_title("Station Trust Allocation (Fusion Weights)")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Weight Fraction")
    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.set_xlim(weights_pct.index.min(), weights_pct.index.max())
    ax.set_ylim(0, 1)
    
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info(f"Saved {output_path}")

def plot_vtec_correlation(fusion_df: pd.DataFrame, vtec_df: pd.DataFrame, output_path: Path):
    """Plot Timing Uncertainty vs VTEC."""
    logger.info("Generating VTEC Correlation plot...")
    
    if vtec_df.empty:
        logger.warning("No VTEC data available for correlation plot.")
        return

    fig, ax1 = plt.subplots(figsize=(12, 6))
    
    # Left Axis: Timing Uncertainty
    color1 = 'tab:blue'
    ax1.set_xlabel('Time (UTC)')
    ax1.set_ylabel('Timing Uncertainty (ms)', color=color1)
    ax1.plot(fusion_df['datetime'], fusion_df['uncertainty_ms'], color=color1, lw=1.5, label='Traceability Uncertainty')
    ax1.tick_params(axis='y', labelcolor=color1)
    
    # Right Axis: VTEC
    ax2 = ax1.twinx()
    color2 = 'tab:orange'
    ax2.set_ylabel('Vertical TEC (TECU)', color=color2)
    
    # Resample VTEC to match timescale if needed, or just plot
    # VTEC is high rate?
    ax2.plot(vtec_df['datetime'], vtec_df['vtec'], color=color2, lw=1.5, alpha=0.8, label='Local VTEC')
    ax2.tick_params(axis='y', labelcolor=color2)
    
    ax1.set_title("Ionospheric Impact: Timing Error vs Space Weather")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    
    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left')
    
    plt.tight_layout()
    plt.savefig(output_path)
    plt.close()
    logger.info(f"Saved {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Generate Metrology Report Plots")
    parser.add_argument('--output-dir', type=Path, default=Path('docs/images/metrology'))
    parser.add_argument('--hours', type=int, default=24, help="Analysis window in hours")
    args = parser.parse_args()
    
    args.output_dir.mkdir(parents=True, exist_ok=True)
    setup_plotting_style()
    
    # Load Data
    logger.info("Loading data...")
    fusion_df = load_fusion_data(FUSION_CSV_PATH, args.hours)
    vtec_df = load_vtec_data(VTEC_DIR, args.hours)
    
    logger.info(f"loaded {len(fusion_df)} fusion records")
    logger.info(f"loaded {len(vtec_df)} VTEC records")
    
    # Generate Plots
    plot_adev(fusion_df, args.output_dir / '1_adev_stability.png')
    plot_residuals(fusion_df, args.output_dir / '2_residuals_timeseries.png')
    plot_heatmap(fusion_df, args.output_dir / '3_trust_heatmap.png')
    plot_vtec_correlation(fusion_df, vtec_df, args.output_dir / '4_vtec_correlation.png')
    
    logger.info("Report generation complete.")

if __name__ == '__main__':
    main()
