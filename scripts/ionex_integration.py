#!/usr/bin/env python3
"""
IONEX Data Integration - Download and Parse GPS TEC Maps

This module handles downloading and parsing IONEX (IONosphere Map EXchange) files
from NASA CDDIS for GPS TEC validation.

IONEX Format:
- 2.5° × 5° grid (latitude × longitude)
- 2-hour cadence (12 maps per day)
- VTEC in TECU (Total Electron Content Units, 10^16 electrons/m²)

Data Source: NASA CDDIS
- URL: https://cddis.nasa.gov/archive/gnss/products/ionex/YYYY/DDD/
- Files: jplgDDD0.YYi.Z (JPL Global Ionosphere Maps)
- Format: Compressed IONEX text files

Usage:
    # Download IONEX for a specific date
    ionex_file = download_ionex('2025-12-23')
    
    # Parse and interpolate
    vtec = interpolate_ionex_vtec(ionex_file, lat=40.5, lon=-100.2, timestamp='2025-12-23T12:00:00Z')
"""

import os
import gzip
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, List
import numpy as np


class IONEXParser:
    """
    Parser for IONEX format files.
    
    IONEX files contain global TEC maps at 2-hour intervals.
    """
    
    def __init__(self, ionex_file: Path):
        """
        Initialize parser with IONEX file.
        
        Args:
            ionex_file: Path to IONEX file (.YYi or .YYi.Z)
        """
        self.ionex_file = Path(ionex_file)
        self.maps = []  # List of (epoch, lat_grid, lon_grid, tec_grid)
        self._parse()
    
    def _parse(self):
        """Parse IONEX file and extract TEC maps."""
        # Handle compressed files (.Z format - Unix compress)
        if str(self.ionex_file).endswith('.Z'):
            # Decompress using uncompress command
            import subprocess
            decompressed = str(self.ionex_file)[:-2]  # Remove .Z
            try:
                subprocess.run(['uncompress', '-c', str(self.ionex_file)], 
                              stdout=open(decompressed, 'w'), check=True)
            except FileNotFoundError:
                # Try gunzip -d (some systems use this for .Z)
                subprocess.run(['gunzip', '-d', '-c', str(self.ionex_file)], 
                              stdout=open(decompressed, 'w'), check=True)
            with open(decompressed, 'r') as f:
                lines = f.readlines()
        elif str(self.ionex_file).endswith('.gz'):
            with gzip.open(self.ionex_file, 'rt') as f:
                lines = f.readlines()
        else:
            with open(self.ionex_file, 'r') as f:
                lines = f.readlines()
        
        # Parse header
        lat_min, lat_max, lat_step = None, None, None
        lon_min, lon_max, lon_step = None, None, None
        
        for line in lines:
            if 'LAT1 / LAT2 / DLAT' in line:
                parts = line.split()
                lat_min, lat_max, lat_step = float(parts[0]), float(parts[1]), float(parts[2])
            elif 'LON1 / LON2 / DLON' in line:
                parts = line.split()
                lon_min, lon_max, lon_step = float(parts[0]), float(parts[1]), float(parts[2])
            elif 'END OF HEADER' in line:
                break
        
        if lat_min is None or lon_min is None:
            raise ValueError("Failed to parse IONEX header")
        
        # Create grids
        lat_grid = np.arange(lat_min, lat_max + lat_step/2, lat_step)
        lon_grid = np.arange(lon_min, lon_max + lon_step/2, lon_step)
        
        # Parse TEC maps
        current_map = None
        current_epoch = None
        lat_idx = 0
        
        for line in lines:
            if 'START OF TEC MAP' in line:
                # New map
                current_map = np.zeros((len(lat_grid), len(lon_grid)))
                lat_idx = 0
            elif 'EPOCH OF CURRENT MAP' in line:
                # Extract epoch timestamp
                parts = line.split()
                year, month, day, hour, minute, second = map(int, parts[:6])
                current_epoch = datetime(year, month, day, hour, minute, second)
            elif 'LAT/LON1/LON2/DLON/H' in line:
                # TEC data for this latitude
                parts = line.split()
                lat_value = float(parts[0])
                # Find latitude index
                lat_idx = np.argmin(np.abs(lat_grid - lat_value))
            elif 'END OF TEC MAP' in line:
                # Save completed map
                if current_map is not None and current_epoch is not None:
                    self.maps.append((current_epoch, lat_grid, lon_grid, current_map.copy()))
            elif current_map is not None and line.strip() and not line.strip().startswith('START'):
                # TEC values (5 values per line)
                values = [int(v) for v in line.split()]
                # IONEX uses 0.1 TECU units
                tec_values = np.array(values) * 0.1
                # Fill longitude values for this latitude
                lon_start = 0
                for i, val in enumerate(tec_values):
                    if lon_start + i < len(lon_grid):
                        current_map[lat_idx, lon_start + i] = val
                lon_start += len(tec_values)
        
        print(f"Parsed {len(self.maps)} TEC maps from {self.ionex_file}")
    
    def interpolate(self, lat: float, lon: float, timestamp: datetime) -> Optional[float]:
        """
        Interpolate VTEC at specific location and time.
        
        Args:
            lat: Latitude (degrees, -90 to 90)
            lon: Longitude (degrees, -180 to 180 or 0 to 360)
            timestamp: UTC timestamp
        
        Returns:
            VTEC in TECU, or None if not available
        """
        if not self.maps:
            return None
        
        # Normalize longitude to 0-360
        if lon < 0:
            lon += 360
        
        # Find surrounding epochs
        epochs = [m[0] for m in self.maps]
        
        # Find closest epochs
        before_idx = None
        after_idx = None
        for i, epoch in enumerate(epochs):
            if epoch <= timestamp:
                before_idx = i
            if epoch >= timestamp and after_idx is None:
                after_idx = i
        
        if before_idx is None:
            before_idx = 0
        if after_idx is None:
            after_idx = len(epochs) - 1
        
        # Interpolate in time
        if before_idx == after_idx:
            # Exact match or single map
            epoch, lat_grid, lon_grid, tec_map = self.maps[before_idx]
            vtec = self._bilinear_interpolate(lat, lon, lat_grid, lon_grid, tec_map)
        else:
            # Linear interpolation between two maps
            epoch_before, lat_grid, lon_grid, tec_before = self.maps[before_idx]
            epoch_after, _, _, tec_after = self.maps[after_idx]
            
            vtec_before = self._bilinear_interpolate(lat, lon, lat_grid, lon_grid, tec_before)
            vtec_after = self._bilinear_interpolate(lat, lon, lat_grid, lon_grid, tec_after)
            
            # Time weight
            dt_total = (epoch_after - epoch_before).total_seconds()
            dt_before = (timestamp - epoch_before).total_seconds()
            weight = dt_before / dt_total if dt_total > 0 else 0
            
            vtec = vtec_before * (1 - weight) + vtec_after * weight
        
        return vtec
    
    def _bilinear_interpolate(
        self, lat: float, lon: float,
        lat_grid: np.ndarray, lon_grid: np.ndarray, tec_map: np.ndarray
    ) -> float:
        """Bilinear interpolation on 2D grid."""
        # Find surrounding grid points
        lat_idx = np.searchsorted(lat_grid, lat)
        lon_idx = np.searchsorted(lon_grid, lon)
        
        # Clamp to grid bounds
        lat_idx = max(1, min(lat_idx, len(lat_grid) - 1))
        lon_idx = max(1, min(lon_idx, len(lon_grid) - 1))
        
        # Get surrounding points
        lat0, lat1 = lat_grid[lat_idx - 1], lat_grid[lat_idx]
        lon0, lon1 = lon_grid[lon_idx - 1], lon_grid[lon_idx]
        
        # Get TEC values at corners
        tec00 = tec_map[lat_idx - 1, lon_idx - 1]
        tec01 = tec_map[lat_idx - 1, lon_idx]
        tec10 = tec_map[lat_idx, lon_idx - 1]
        tec11 = tec_map[lat_idx, lon_idx]
        
        # Bilinear interpolation
        lat_weight = (lat - lat0) / (lat1 - lat0) if lat1 != lat0 else 0
        lon_weight = (lon - lon0) / (lon1 - lon0) if lon1 != lon0 else 0
        
        tec_bottom = tec00 * (1 - lon_weight) + tec01 * lon_weight
        tec_top = tec10 * (1 - lon_weight) + tec11 * lon_weight
        
        vtec = tec_bottom * (1 - lat_weight) + tec_top * lat_weight
        
        return vtec


def download_ionex(date: str, output_dir: Path = Path('/tmp/ionex')) -> Optional[Path]:
    """
    Download IONEX file for a specific date from IGS.
    
    Args:
        date: Date string (YYYY-MM-DD)
        output_dir: Directory to save IONEX files
    
    Returns:
        Path to downloaded file, or None if failed
    """
    dt = datetime.strptime(date, '%Y-%m-%d')
    year = dt.year
    doy = dt.timetuple().tm_yday  # Day of year
    
    # JPL Global Ionosphere Maps from NASA CDDIS (requires Earthdata auth)
    # Format: jplgDDD0.YYi.Z
    filename = f"jplg{doy:03d}0.{str(year)[2:]}i.Z"
    
    # NASA CDDIS (requires .netrc authentication)
    url = f"https://cddis.nasa.gov/archive/gnss/products/ionex/{year}/{doy:03d}/{filename}"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / filename
    
    if output_file.exists():
        print(f"IONEX file already exists: {output_file}")
        return output_file
    
    print(f"Downloading IONEX from NASA CDDIS: {url}")
    
    try:
        # NASA Earthdata authentication using urllib
        import urllib.request
        from http.cookiejar import CookieJar
        
        # Read credentials from .netrc
        import netrc
        try:
            credentials = netrc.netrc()
            username, _, password = credentials.authenticators("urs.earthdata.nasa.gov")
        except (FileNotFoundError, TypeError):
            print("Error: ~/.netrc not found or doesn't contain urs.earthdata.nasa.gov credentials")
            return None
        
        # Setup authentication
        password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(None, "https://urs.earthdata.nasa.gov", username, password)
        
        # Cookie jar for session management
        cookie_jar = CookieJar()
        
        # Build opener with authentication
        opener = urllib.request.build_opener(
            urllib.request.HTTPBasicAuthHandler(password_manager),
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )
        urllib.request.install_opener(opener)
        
        # Download file
        request = urllib.request.Request(url)
        response = urllib.request.urlopen(request, timeout=30)
        
        with open(output_file, 'wb') as f:
            f.write(response.read())
        
        print(f"Downloaded: {output_file}")
        return output_file
    
    except Exception as e:
        print(f"Failed to download IONEX: {e}")
        return None


def interpolate_ionex_vtec(
    ionex_file: Path,
    lat: float,
    lon: float,
    timestamp: str
) -> Optional[float]:
    """
    Convenience function to parse and interpolate IONEX VTEC.
    
    Args:
        ionex_file: Path to IONEX file
        lat: Latitude (degrees)
        lon: Longitude (degrees)
        timestamp: ISO 8601 timestamp string
    
    Returns:
        VTEC in TECU, or None if not available
    """
    parser = IONEXParser(ionex_file)
    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    return parser.interpolate(lat, lon, dt)


if __name__ == '__main__':
    # Example usage
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python ionex_integration.py YYYY-MM-DD")
        print("Example: python ionex_integration.py 2025-12-23")
        sys.exit(1)
    
    date = sys.argv[1]
    
    # Download IONEX
    ionex_file = download_ionex(date)
    
    if ionex_file:
        # Example: Interpolate at WWV midpoint
        lat, lon = 40.5, -100.2
        timestamp = f"{date}T12:00:00Z"
        
        vtec = interpolate_ionex_vtec(ionex_file, lat, lon, timestamp)
        
        if vtec is not None:
            print(f"\nGPS VTEC at ({lat}°N, {lon}°W) @ {timestamp}:")
            print(f"  {vtec:.2f} TECU")
