#!/usr/bin/env python3
"""
ScintPI Data Integration - Local GPS TEC for Bias Correction

This module reads ScintPI receiver data to provide local vertical TEC measurements
for bias correction of IONEX maps.

ScintPI Data Format:
- CSV files with columns: timestamp, vtec, stec, azimuth, elevation, prn
- VTEC: Vertical TEC (TECU)
- STEC: Slant TEC (TECU)

Usage Strategy:
1. Read ScintPI VTEC at receiver location
2. Compare with IONEX VTEC at same location
3. Calculate bias = VTEC_ScintPI - VTEC_IONEX
4. Apply bias correction to IONEX midpoint value

This anchors the GPS TEC map with local ground truth.
"""

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
import numpy as np


class ScintPIReader:
    """
    Reader for ScintPI GPS TEC data.
    """
    
    def __init__(self, data_dir: Path):
        """
        Initialize reader with ScintPI data directory.
        
        Args:
            data_dir: Directory containing ScintPI CSV files
        """
        self.data_dir = Path(data_dir)
        
        if not self.data_dir.exists():
            raise FileNotFoundError(f"ScintPI data directory not found: {data_dir}")
    
    def read_vtec(
        self,
        start_time: datetime,
        end_time: datetime,
        elevation_min: float = 30.0
    ) -> List[Dict]:
        """
        Read VTEC measurements from ScintPI data.
        
        Args:
            start_time: Start of time range
            end_time: End of time range
            elevation_min: Minimum satellite elevation (degrees) to avoid multipath
        
        Returns:
            List of dicts with keys: timestamp, vtec, stec, elevation, prn
        """
        measurements = []
        
        # Find CSV files for date range
        current_date = start_time.date()
        end_date = end_time.date()
        
        while current_date <= end_date:
            # ScintPI filename format: scintpi_YYYYMMDD.csv
            csv_file = self.data_dir / f"scintpi_{current_date.strftime('%Y%m%d')}.csv"
            
            if csv_file.exists():
                measurements.extend(self._read_csv(csv_file, start_time, end_time, elevation_min))
            
            current_date += timedelta(days=1)
        
        return measurements
    
    def _read_csv(
        self,
        csv_file: Path,
        start_time: datetime,
        end_time: datetime,
        elevation_min: float
    ) -> List[Dict]:
        """Read and filter ScintPI CSV file."""
        measurements = []
        
        try:
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    # Parse timestamp
                    timestamp = datetime.fromisoformat(row['timestamp'])
                    
                    # Filter by time range
                    if not (start_time <= timestamp <= end_time):
                        continue
                    
                    # Filter by elevation
                    elevation = float(row.get('elevation', 0))
                    if elevation < elevation_min:
                        continue
                    
                    # Extract data
                    measurements.append({
                        'timestamp': timestamp,
                        'vtec': float(row['vtec']),
                        'stec': float(row.get('stec', 0)),
                        'elevation': elevation,
                        'azimuth': float(row.get('azimuth', 0)),
                        'prn': int(row.get('prn', 0))
                    })
        
        except Exception as e:
            print(f"Error reading {csv_file}: {e}")
        
        return measurements
    
    def get_average_vtec(
        self,
        timestamp: datetime,
        window_minutes: int = 5
    ) -> Optional[float]:
        """
        Get average VTEC around a specific timestamp.
        
        Args:
            timestamp: Target timestamp
            window_minutes: Time window for averaging (±minutes)
        
        Returns:
            Average VTEC in TECU, or None if no data
        """
        start_time = timestamp - timedelta(minutes=window_minutes)
        end_time = timestamp + timedelta(minutes=window_minutes)
        
        measurements = self.read_vtec(start_time, end_time)
        
        if not measurements:
            return None
        
        # Average VTEC
        vtec_values = [m['vtec'] for m in measurements]
        return np.mean(vtec_values)


def calculate_ionex_bias(
    scintpi_vtec: float,
    ionex_vtec: float
) -> float:
    """
    Calculate bias between ScintPI and IONEX.
    
    Args:
        scintpi_vtec: Local ScintPI VTEC (TECU)
        ionex_vtec: IONEX VTEC at receiver location (TECU)
    
    Returns:
        Bias in TECU (positive means IONEX underestimates)
    """
    return scintpi_vtec - ionex_vtec


def apply_bias_correction(
    ionex_midpoint_vtec: float,
    bias: float
) -> float:
    """
    Apply local bias correction to IONEX midpoint value.
    
    Args:
        ionex_midpoint_vtec: IONEX VTEC at midpoint (TECU)
        bias: Calculated bias from local comparison (TECU)
    
    Returns:
        Corrected VTEC (TECU)
    """
    return ionex_midpoint_vtec + bias


def get_scintpi_corrected_vtec(
    scintpi_dir: Path,
    ionex_file: Path,
    rx_lat: float,
    rx_lon: float,
    mid_lat: float,
    mid_lon: float,
    timestamp: datetime
) -> Dict:
    """
    Get bias-corrected IONEX VTEC using local ScintPI data.
    
    Args:
        scintpi_dir: ScintPI data directory
        ionex_file: IONEX file path
        rx_lat, rx_lon: Receiver location
        mid_lat, mid_lon: Midpoint location
        timestamp: Measurement timestamp
    
    Returns:
        Dict with vtec_scintpi, vtec_ionex_local, vtec_ionex_midpoint, bias, vtec_corrected
    """
    from ionex_integration import IONEXParser
    
    # Read ScintPI local VTEC
    scintpi = ScintPIReader(scintpi_dir)
    vtec_scintpi = scintpi.get_average_vtec(timestamp, window_minutes=5)
    
    if vtec_scintpi is None:
        return {'error': 'No ScintPI data available'}
    
    # Parse IONEX
    ionex = IONEXParser(ionex_file)
    
    # Get IONEX at receiver location
    vtec_ionex_local = ionex.interpolate(rx_lat, rx_lon, timestamp)
    
    # Get IONEX at midpoint
    vtec_ionex_midpoint = ionex.interpolate(mid_lat, mid_lon, timestamp)
    
    if vtec_ionex_local is None or vtec_ionex_midpoint is None:
        return {'error': 'IONEX interpolation failed'}
    
    # Calculate bias
    bias = calculate_ionex_bias(vtec_scintpi, vtec_ionex_local)
    
    # Apply correction
    vtec_corrected = apply_bias_correction(vtec_ionex_midpoint, bias)
    
    return {
        'vtec_scintpi': vtec_scintpi,
        'vtec_ionex_local': vtec_ionex_local,
        'vtec_ionex_midpoint': vtec_ionex_midpoint,
        'bias': bias,
        'vtec_corrected': vtec_corrected
    }


if __name__ == '__main__':
    # Example usage
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python scintpi_integration.py /path/to/scintpi/data")
        sys.exit(1)
    
    scintpi_dir = Path(sys.argv[1])
    
    # Example: Read VTEC for a time range
    start_time = datetime(2025, 12, 23, 12, 0, 0)
    end_time = datetime(2025, 12, 23, 13, 0, 0)
    
    reader = ScintPIReader(scintpi_dir)
    measurements = reader.read_vtec(start_time, end_time)
    
    print(f"Found {len(measurements)} ScintPI measurements")
    
    if measurements:
        # Calculate average
        avg_vtec = np.mean([m['vtec'] for m in measurements])
        print(f"Average VTEC: {avg_vtec:.2f} TECU")
        
        # Show first few measurements
        print("\nFirst 5 measurements:")
        for m in measurements[:5]:
            print(f"  {m['timestamp']}: {m['vtec']:.2f} TECU (el={m['elevation']:.1f}°, PRN={m['prn']})")
