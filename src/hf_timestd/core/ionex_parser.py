"""
IONEX parser — reads IGS Global Ionosphere Map (GIM) files.

This is the canonical home of IONEXParser (P-H18). It previously lived in
``scripts/ionex_integration.py`` and was loaded into the package via
``importlib.exec_module`` on every cache miss — slow, and broken under a wheel
install where ``scripts/`` is not packaged. ``scripts/ionex_integration.py``
now re-exports IONEXParser from here.

IONEX files contain global VTEC maps on a lat/lon grid at a 2-hour cadence
(12 maps per day); VTEC is in TECU (10^16 electrons/m^2).
"""

import gzip
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


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
        # Handle compressed files
        if str(self.ionex_file).endswith('.gz'):
            # Modern format: gzip compression
            try:
                with gzip.open(self.ionex_file, 'rt') as f:
                    lines = f.readlines()
            except Exception as e:
                logger.error(f"Failed to decompress .gz file: {e}")
                raise
        elif str(self.ionex_file).endswith('.Z'):
            # Legacy format: Unix compress (.Z)
            import subprocess
            decompressed = str(self.ionex_file)[:-2]  # Remove .Z
            try:
                # Try uncompress command first
                subprocess.run(['uncompress', '-c', str(self.ionex_file)],
                              stdout=open(decompressed, 'w'), check=True, stderr=subprocess.PIPE)
            except (FileNotFoundError, subprocess.CalledProcessError):
                try:
                    # Fallback to gunzip (some systems use this for .Z)
                    subprocess.run(['gunzip', '-d', '-c', str(self.ionex_file)],
                                  stdout=open(decompressed, 'w'), check=True, stderr=subprocess.PIPE)
                except Exception as e:
                    logger.error(f"Failed to decompress .Z file: {e}")
                    raise
            with open(decompressed, 'r') as f:
                lines = f.readlines()
        else:
            # Uncompressed file
            with open(self.ionex_file, 'r') as f:
                lines = f.readlines()

        # Parse header
        # IONEX format uses fixed columns: data in columns 1-60, label in 61-80
        lat_min, lat_max, lat_step = None, None, None
        lon_min, lon_max, lon_step = None, None, None

        for line in lines:
            if 'LAT1 / LAT2 / DLAT' in line:
                try:
                    # Extract first 60 characters (data section)
                    data_section = line[:60].strip()
                    values = data_section.split()
                    if len(values) >= 3:
                        lat_min = float(values[0])
                        lat_max = float(values[1])
                        lat_step = float(values[2])
                except Exception as e:
                    logger.warning(f"Failed to parse LAT line: {line.strip()} - {e}")
                    continue

            elif 'LON1 / LON2 / DLON' in line:
                try:
                    data_section = line[:60].strip()
                    values = data_section.split()
                    if len(values) >= 3:
                        lon_min = float(values[0])
                        lon_max = float(values[1])
                        lon_step = float(values[2])
                except Exception as e:
                    logger.warning(f"Failed to parse LON line: {line.strip()} - {e}")
                    continue

            elif 'END OF HEADER' in line:
                break

        if lat_min is None or lon_min is None:
            raise ValueError(f"Failed to parse IONEX header - lat_min={lat_min}, lon_min={lon_min}")

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
                # Format: LAT LON1 LON2 DLON H (but LAT and LON1 may be concatenated like "87.5-180.0")
                try:
                    data_section = line[:60].strip()
                    # Handle case where lat and lon1 are concatenated (e.g., "87.5-180.0")
                    # Split on whitespace first
                    parts = data_section.split()
                    if len(parts) >= 1:
                        # First part might be "87.5-180.0" or just "87.5"
                        first_part = parts[0]
                        # Check if it contains a negative sign after the first character
                        if '-' in first_part[1:]:  # Skip first char in case lat is negative
                            # Split on the second occurrence of '-'
                            split_idx = first_part.index('-', 1)
                            lat_value = float(first_part[:split_idx])
                        else:
                            lat_value = float(first_part)

                        # Find latitude index
                        lat_idx = np.argmin(np.abs(lat_grid - lat_value))
                except Exception as e:
                    logger.debug(f"Failed to parse LAT/LON line: {line.strip()} - {e}")
                    continue
            elif 'END OF TEC MAP' in line:
                # Save completed map
                if current_map is not None and current_epoch is not None:
                    self.maps.append((current_epoch, lat_grid, lon_grid, current_map.copy()))
            elif current_map is not None and line.strip():
                # TEC values - only parse if line contains numeric data
                # Skip lines with keywords
                if any(keyword in line for keyword in ['START', 'EPOCH', 'LAT/LON', 'END', 'COMMENT']):
                    continue
                try:
                    values = [int(v) for v in line.split()]
                    # IONEX uses 0.1 TECU units
                    tec_values = np.array(values) * 0.1
                    # Fill longitude values for this latitude
                    lon_start = 0
                    for i, val in enumerate(tec_values):
                        if lon_start + i < len(lon_grid):
                            current_map[lat_idx, lon_start + i] = val
                    lon_start += len(tec_values)
                except (ValueError, IndexError):
                    # Skip lines that don't contain valid TEC data
                    continue

        logger.info(f"Parsed {len(self.maps)} TEC maps from {self.ionex_file}")

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
