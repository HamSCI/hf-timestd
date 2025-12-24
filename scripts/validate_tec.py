#!/usr/bin/env python3
"""
TEC Validation Script - Compare HF TEC with GPS TEC

This script performs rigorous validation of HF-derived TEC against GPS TEC,
addressing the volume mismatch problem with geometric corrections.

Workflow:
1. Read HF TEC data (slant TEC)
2. Convert to VTEC at midpoint using obliquity factor
3. Download/parse IONEX maps
4. Interpolate GPS VTEC at midpoint
5. Optional: Apply ScintPI local bias correction
6. Compare and generate validation report

Usage:
    python validate_tec.py --date 2025-12-23 --station WWV
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta
import csv
import numpy as np
import matplotlib.pyplot as plt

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from hf_timestd.core.tec_geometry import (
    calculate_geometry_for_station,
    convert_slant_to_vertical
)
from scripts.ionex_integration import download_ionex, IONEXParser
from scripts.scintpi_integration import ScintPIReader, calculate_ionex_bias


def read_hf_tec(tec_dir: Path, date_str: str, station: str):
    """Read HF TEC data from Science Aggregator output."""
    csv_file = tec_dir / f"tec_{date_str}.csv"
    
    if not csv_file.exists():
        print(f"HF TEC file not found: {csv_file}")
        return []
    
    measurements = []
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['station'] == station:
                measurements.append({
                    'timestamp': datetime.fromisoformat(row['timestamp_utc']),
                    'tec_slant': float(row['tec_tecu']),
                    'confidence': float(row['confidence']),
                    'n_frequencies': int(row['n_frequencies'])
                })
    
    return measurements


def validate_tec(
    date: str,
    station: str,
    rx_lat: float,
    rx_lon: float,
    tec_dir: Path,
    ionex_dir: Path,
    scintpi_dir: Optional[Path] = None,
    output_dir: Path = Path('/tmp/tec_validation')
):
    """
    Perform TEC validation for a specific date and station.
    
    Args:
        date: Date string (YYYY-MM-DD)
        station: Station code (WWV, WWVH, CHU, BPM)
        rx_lat, rx_lon: Receiver location
        tec_dir: HF TEC data directory
        ionex_dir: IONEX files directory
        scintpi_dir: Optional ScintPI data directory
        output_dir: Output directory for plots and reports
    """
    print(f"\n{'='*60}")
    print(f"TEC Validation: {station} on {date}")
    print(f"{'='*60}\n")
    
    # Calculate geometry
    print(f"1. Calculating geometry...")
    geom = calculate_geometry_for_station(station, rx_lat, rx_lon)
    print(f"   Midpoint: {geom['midpoint_lat']:.4f}°N, {geom['midpoint_lon']:.4f}°W")
    print(f"   Elevation: {geom['elevation_deg']:.2f}°")
    print(f"   Distance: {geom['distance_km']:.1f} km")
    
    # Read HF TEC data
    print(f"\n2. Reading HF TEC data...")
    date_str = date.replace('-', '')
    hf_measurements = read_hf_tec(tec_dir, date_str, station)
    print(f"   Found {len(hf_measurements)} HF measurements")
    
    if not hf_measurements:
        print("   ERROR: No HF TEC data found!")
        return
    
    # Convert slant to vertical
    print(f"\n3. Converting slant TEC to vertical TEC...")
    for m in hf_measurements:
        vtec, obliquity = convert_slant_to_vertical(
            m['tec_slant'],
            geom['elevation_deg']
        )
        m['vtec_hf'] = vtec
        m['obliquity_factor'] = obliquity
    
    print(f"   Obliquity factor: {hf_measurements[0]['obliquity_factor']:.3f}")
    print(f"   Example: {hf_measurements[0]['tec_slant']:.1f} TECU (slant) → "
          f"{hf_measurements[0]['vtec_hf']:.1f} TECU (vertical)")
    
    # Download/parse IONEX
    print(f"\n4. Downloading IONEX data...")
    ionex_file = download_ionex(date, ionex_dir)
    
    if not ionex_file:
        print("   ERROR: Failed to download IONEX!")
        return
    
    print(f"   Parsing IONEX...")
    ionex = IONEXParser(ionex_file)
    
    # Optional: ScintPI bias correction
    bias = 0.0
    if scintpi_dir and scintpi_dir.exists():
        print(f"\n5. Calculating ScintPI bias correction...")
        try:
            scintpi = ScintPIReader(scintpi_dir)
            # Use midday for bias calculation
            midday = datetime.strptime(f"{date} 12:00:00", "%Y-%m-%d %H:%M:%S")
            vtec_scintpi = scintpi.get_average_vtec(midday, window_minutes=30)
            
            if vtec_scintpi:
                vtec_ionex_local = ionex.interpolate(rx_lat, rx_lon, midday)
                if vtec_ionex_local:
                    bias = calculate_ionex_bias(vtec_scintpi, vtec_ionex_local)
                    print(f"   ScintPI VTEC: {vtec_scintpi:.2f} TECU")
                    print(f"   IONEX local: {vtec_ionex_local:.2f} TECU")
                    print(f"   Bias: {bias:+.2f} TECU")
        except Exception as e:
            print(f"   Warning: ScintPI bias calculation failed: {e}")
    
    # Interpolate GPS VTEC at midpoint
    print(f"\n6. Interpolating GPS VTEC at midpoint...")
    for m in hf_measurements:
        vtec_gps = ionex.interpolate(
            geom['midpoint_lat'],
            geom['midpoint_lon'],
            m['timestamp']
        )
        if vtec_gps is not None:
            m['vtec_gps'] = vtec_gps + bias  # Apply bias correction
        else:
            m['vtec_gps'] = None
    
    # Filter out measurements without GPS data
    valid_measurements = [m for m in hf_measurements if m['vtec_gps'] is not None]
    print(f"   {len(valid_measurements)} measurements with GPS data")
    
    if not valid_measurements:
        print("   ERROR: No valid GPS TEC data!")
        return
    
    # Calculate validation metrics
    print(f"\n7. Calculating validation metrics...")
    vtec_hf = np.array([m['vtec_hf'] for m in valid_measurements])
    vtec_gps = np.array([m['vtec_gps'] for m in valid_measurements])
    
    correlation = np.corrcoef(vtec_hf, vtec_gps)[0, 1]
    r_squared = correlation ** 2
    rms_error = np.sqrt(np.mean((vtec_hf - vtec_gps) ** 2))
    mean_bias = np.mean(vtec_hf - vtec_gps)
    slab_thickness = np.mean(vtec_gps / vtec_hf)
    
    print(f"\n{'='*60}")
    print(f"VALIDATION RESULTS")
    print(f"{'='*60}")
    print(f"Correlation (R²):     {r_squared:.3f}")
    print(f"RMS Error:            {rms_error:.2f} TECU")
    print(f"Mean Bias (HF-GPS):   {mean_bias:+.2f} TECU")
    print(f"Slab Thickness:       {slab_thickness:.2f}")
    print(f"Measurements:         {len(valid_measurements)}")
    print(f"{'='*60}\n")
    
    # Success criteria
    success = r_squared > 0.7 and rms_error < 10.0
    print(f"Validation: {'✅ PASS' if success else '❌ FAIL'}")
    
    if r_squared > 0.7:
        print(f"  ✅ R² > 0.7 (strong correlation)")
    else:
        print(f"  ❌ R² < 0.7 (weak correlation)")
    
    if rms_error < 10.0:
        print(f"  ✅ RMS < 10 TECU (acceptable accuracy)")
    else:
        print(f"  ❌ RMS > 10 TECU (poor accuracy)")
    
    # Generate plots
    print(f"\n8. Generating plots...")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Time series plot
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
    
    timestamps = [m['timestamp'] for m in valid_measurements]
    
    # Plot 1: TEC comparison
    ax1.plot(timestamps, vtec_hf, 'o-', label='HF VTEC (Midpoint)', markersize=4)
    ax1.plot(timestamps, vtec_gps, 's-', label='GPS VTEC (IONEX)', markersize=4, alpha=0.7)
    ax1.set_ylabel('VTEC (TECU)')
    ax1.set_title(f'TEC Validation: {station} - {date}\\nR²={r_squared:.3f}, RMS={rms_error:.2f} TECU, Bias={mean_bias:+.2f} TECU')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # Plot 2: Residuals
    residuals = vtec_hf - vtec_gps
    ax2.plot(timestamps, residuals, 'o-', color='red', markersize=4)
    ax2.axhline(0, color='black', linestyle='--', alpha=0.5)
    ax2.axhline(mean_bias, color='blue', linestyle='--', alpha=0.5, label=f'Mean Bias: {mean_bias:+.2f} TECU')
    ax2.set_xlabel('Time (UTC)')
    ax2.set_ylabel('Residual (HF - GPS) [TECU]')
    ax2.set_title('Residuals')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plot_file = output_dir / f'tec_validation_{station}_{date_str}.png'
    plt.savefig(plot_file, dpi=150)
    print(f"   Saved plot: {plot_file}")
    
    # Scatter plot
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.scatter(vtec_gps, vtec_hf, alpha=0.6, s=50)
    
    # 1:1 line
    min_val = min(vtec_gps.min(), vtec_hf.min())
    max_val = max(vtec_gps.max(), vtec_hf.max())
    ax.plot([min_val, max_val], [min_val, max_val], 'k--', alpha=0.5, label='1:1 Line')
    
    # Best fit line
    coeffs = np.polyfit(vtec_gps, vtec_hf, 1)
    fit_line = np.poly1d(coeffs)
    ax.plot(vtec_gps, fit_line(vtec_gps), 'r-', alpha=0.7, label=f'Fit: y={coeffs[0]:.2f}x+{coeffs[1]:.2f}')
    
    ax.set_xlabel('GPS VTEC (TECU)')
    ax.set_ylabel('HF VTEC (TECU)')
    ax.set_title(f'HF vs GPS TEC: {station} - {date}\\nR²={r_squared:.3f}')
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal')
    
    scatter_file = output_dir / f'tec_scatter_{station}_{date_str}.png'
    plt.savefig(scatter_file, dpi=150)
    print(f"   Saved scatter plot: {scatter_file}")
    
    # Write validation report
    report_file = output_dir / f'validation_report_{station}_{date_str}.txt'
    with open(report_file, 'w') as f:
        f.write(f"TEC Validation Report\\n")
        f.write(f"{'='*60}\\n")
        f.write(f"Date: {date}\\n")
        f.write(f"Station: {station}\\n")
        f.write(f"Receiver: {rx_lat:.6f}°N, {rx_lon:.6f}°W\\n")
        f.write(f"Midpoint: {geom['midpoint_lat']:.6f}°N, {geom['midpoint_lon']:.6f}°W\\n")
        f.write(f"\\nGeometry:\\n")
        f.write(f"  Distance: {geom['distance_km']:.1f} km\\n")
        f.write(f"  Elevation: {geom['elevation_deg']:.2f}°\\n")
        f.write(f"  Obliquity Factor: {hf_measurements[0]['obliquity_factor']:.3f}\\n")
        f.write(f"\\nValidation Metrics:\\n")
        f.write(f"  R²: {r_squared:.3f}\\n")
        f.write(f"  RMS Error: {rms_error:.2f} TECU\\n")
        f.write(f"  Mean Bias: {mean_bias:+.2f} TECU\\n")
        f.write(f"  Slab Thickness: {slab_thickness:.2f}\\n")
        f.write(f"  Measurements: {len(valid_measurements)}\\n")
        if bias != 0:
            f.write(f"  ScintPI Bias Correction: {bias:+.2f} TECU\\n")
        f.write(f"\\nResult: {'PASS' if success else 'FAIL'}\\n")
    
    print(f"   Saved report: {report_file}")
    print(f"\\n{'='*60}")
    print(f"Validation complete!")
    print(f"{'='*60}\\n")


def main():
    parser = argparse.ArgumentParser(description='Validate HF TEC against GPS TEC')
    parser.add_argument('--date', required=True, help='Date (YYYY-MM-DD)')
    parser.add_argument('--station', required=True, choices=['WWV', 'WWVH', 'CHU', 'BPM'], help='Station code')
    parser.add_argument('--rx-lat', type=float, default=38.918461, help='Receiver latitude')
    parser.add_argument('--rx-lon', type=float, default=-92.127974, help='Receiver longitude')
    parser.add_argument('--tec-dir', type=Path, default=Path('/var/lib/timestd/phase2/science/tec'), help='HF TEC data directory')
    parser.add_argument('--ionex-dir', type=Path, default=Path('/tmp/ionex'), help='IONEX files directory')
    parser.add_argument('--scintpi-dir', type=Path, help='ScintPI data directory (optional)')
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/tec_validation'), help='Output directory')
    
    args = parser.parse_args()
    
    validate_tec(
        date=args.date,
        station=args.station,
        rx_lat=args.rx_lat,
        rx_lon=args.rx_lon,
        tec_dir=args.tec_dir,
        ionex_dir=args.ionex_dir,
        scintpi_dir=args.scintpi_dir,
        output_dir=args.output_dir
    )


if __name__ == '__main__':
    main()
