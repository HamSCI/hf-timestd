#!/usr/bin/env python3
"""
VTEC Map Generator
================================================================================
Converts slant TEC (sTEC) measurements to vertical TEC (vTEC), computes
ionospheric pierce points (IPPs) for each path, and fits a 2D polynomial
surface to generate regional VTEC maps.

Output formats:
- IONEX-format text file (compatible with GPS community tools)
- L3 HDF5 data product

Physics:
--------
Slant-to-vertical conversion uses the thin-shell mapping function:

    vTEC = sTEC · cos(χ')

where χ' is the zenith angle at the ionospheric pierce point:

    sin(χ') = R_E / (R_E + h_shell) · cos(elevation)

The IPP is where the ray path intersects the thin shell at height h_shell.
For HF skywave, the IPP is at the midpoint of the great circle path
(single-hop) or at each reflection point (multi-hop).

The 2D polynomial surface fit:
    vTEC(lat, lon) = Σ_ij a_ij · (lat - lat0)^i · (lon - lon0)^j

captures the regional ionospheric gradient. With 17 paths providing
IPPs spread over ~2000 km, we can resolve gradients at ~500 km scale.

================================================================================
"""

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

# Physical constants
EARTH_RADIUS_KM = 6371.0
THIN_SHELL_HEIGHT_KM = 350.0  # Standard thin-shell height for vTEC mapping


@dataclass
class IPPMeasurement:
    """A single vTEC measurement at an ionospheric pierce point."""
    station: str
    frequency_mhz: float
    ipp_lat: float              # IPP latitude (degrees)
    ipp_lon: float              # IPP longitude (degrees)
    stec_tecu: float            # Measured slant TEC
    vtec_tecu: float            # Converted vertical TEC
    mapping_factor: float       # Slant-to-vertical factor applied
    elevation_deg: float        # Ray elevation angle
    uncertainty_tecu: float     # vTEC uncertainty
    propagation_mode: str = '1F'


@dataclass
class VTECMapResult:
    """Result of VTEC map generation."""
    timestamp: float
    center_lat: float
    center_lon: float

    # Polynomial coefficients (flattened, row-major)
    poly_degree: int
    poly_coeffs: List[float] = field(default_factory=list)

    # Grid evaluation
    grid_lats: List[float] = field(default_factory=list)
    grid_lons: List[float] = field(default_factory=list)
    grid_vtec: List[List[float]] = field(default_factory=list)  # 2D array

    # Input measurements
    ipp_measurements: List[IPPMeasurement] = field(default_factory=list)

    # Quality
    n_ipps: int = 0
    rms_residual_tecu: float = 0.0
    confidence: float = 0.0
    spatial_coverage_km: float = 0.0


class VTECMapper:
    """
    Generates regional VTEC maps from multi-path HF sTEC measurements.

    Pipeline:
    1. Convert each sTEC to vTEC using thin-shell mapping
    2. Compute IPP (ionospheric pierce point) for each path
    3. Fit 2D polynomial surface to vTEC at IPPs
    4. Evaluate on a regular lat/lon grid
    5. Output as IONEX-format file and L3 HDF5
    """

    def __init__(
        self,
        receiver_lat: float = 38.92,
        receiver_lon: float = -92.13,
        shell_height_km: float = THIN_SHELL_HEIGHT_KM,
        poly_degree: int = 2,
        grid_resolution_deg: float = 1.0,
        grid_extent_deg: float = 15.0,
    ):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.shell_height_km = shell_height_km
        self.poly_degree = poly_degree
        self.grid_resolution_deg = grid_resolution_deg
        self.grid_extent_deg = grid_extent_deg

    def stec_to_vtec(
        self,
        stec_tecu: float,
        elevation_deg: float,
    ) -> Tuple[float, float]:
        """
        Convert slant TEC to vertical TEC using thin-shell mapping.

        Args:
            stec_tecu: Slant TEC in TECU
            elevation_deg: Ray elevation angle (degrees)

        Returns:
            (vtec_tecu, mapping_factor)
        """
        mf = self._mapping_function(elevation_deg)
        vtec = stec_tecu / mf
        return vtec, mf

    def compute_ipp(
        self,
        station_lat: float,
        station_lon: float,
        n_hops: int = 1,
        hop_index: int = 0,
    ) -> Tuple[float, float]:
        """
        Compute ionospheric pierce point for a ray path.

        For single-hop: IPP is at the great-circle midpoint.
        For multi-hop: IPP is at the reflection point for the given hop.

        Args:
            station_lat, station_lon: Transmitter coordinates (degrees)
            n_hops: Number of ionospheric hops
            hop_index: Which hop (0-indexed) to compute IPP for

        Returns:
            (ipp_lat, ipp_lon) in degrees
        """
        if n_hops <= 1:
            fraction = 0.5
        else:
            fraction = (2 * hop_index + 1) / (2 * n_hops)

        return self._intermediate_point(
            self.receiver_lat, self.receiver_lon,
            station_lat, station_lon,
            fraction
        )

    def generate_map(
        self,
        measurements: List[IPPMeasurement],
        timestamp: float = 0.0,
    ) -> Optional[VTECMapResult]:
        """
        Generate a VTEC map from IPP measurements.

        Args:
            measurements: List of IPPMeasurement objects
            timestamp: UTC epoch timestamp

        Returns:
            VTECMapResult or None if insufficient data
        """
        if len(measurements) < 3:
            logger.debug(f"VTEC map needs at least 3 IPPs, got {len(measurements)}")
            return None

        # Extract IPP coordinates and vTEC values
        lats = np.array([m.ipp_lat for m in measurements])
        lons = np.array([m.ipp_lon for m in measurements])
        vtecs = np.array([m.vtec_tecu for m in measurements])
        uncertainties = np.array([m.uncertainty_tecu for m in measurements])

        # Center coordinates for polynomial stability
        center_lat = float(np.mean(lats))
        center_lon = float(np.mean(lons))

        # Normalized coordinates (degrees from center)
        dlat = lats - center_lat
        dlon = lons - center_lon

        # Spatial coverage
        lat_range = float(np.ptp(lats))
        lon_range = float(np.ptp(lons))
        coverage_km = math.sqrt(lat_range ** 2 + lon_range ** 2) * 111.0  # ~111 km/deg

        # Determine polynomial degree based on data
        # Need (degree+1)*(degree+2)/2 coefficients for 2D polynomial
        max_degree = self.poly_degree
        n_coeffs = (max_degree + 1) * (max_degree + 2) // 2
        while n_coeffs > len(measurements) and max_degree > 0:
            max_degree -= 1
            n_coeffs = (max_degree + 1) * (max_degree + 2) // 2

        if max_degree < 0:
            return None

        # Build design matrix for 2D polynomial
        A = self._build_poly_matrix(dlat, dlon, max_degree)

        # Weights from uncertainties
        W = 1.0 / np.maximum(uncertainties, 0.1) ** 2

        # Weighted least squares: minimize ||W^(1/2)(A·c - vtec)||²
        W_sqrt = np.sqrt(W)
        A_w = A * W_sqrt[:, np.newaxis]
        b_w = vtecs * W_sqrt

        try:
            coeffs, residuals_arr, rank, sv = np.linalg.lstsq(A_w, b_w, rcond=None)
        except np.linalg.LinAlgError as e:
            logger.warning(f"VTEC polynomial fit failed: {e}")
            return None

        # Compute fit residuals
        fitted = A @ coeffs
        residuals = vtecs - fitted
        rms_residual = float(np.sqrt(np.mean(residuals ** 2)))

        # Evaluate on a regular grid
        grid_lats, grid_lons, grid_vtec = self._evaluate_grid(
            coeffs, max_degree, center_lat, center_lon
        )

        # Confidence metric
        conf_n = min(1.0, len(measurements) / 8.0)
        conf_residual = max(0.0, 1.0 - rms_residual / 5.0)
        conf_coverage = min(1.0, coverage_km / 1000.0)
        confidence = conf_n * conf_residual * conf_coverage

        return VTECMapResult(
            timestamp=timestamp,
            center_lat=center_lat,
            center_lon=center_lon,
            poly_degree=max_degree,
            poly_coeffs=coeffs.tolist(),
            grid_lats=grid_lats,
            grid_lons=grid_lons,
            grid_vtec=grid_vtec,
            ipp_measurements=measurements,
            n_ipps=len(measurements),
            rms_residual_tecu=rms_residual,
            confidence=confidence,
            spatial_coverage_km=coverage_km,
        )

    def write_ionex(
        self,
        result: VTECMapResult,
        output_path: Path,
        station_id: str = 'HFTD',
    ) -> bool:
        """
        Write VTEC map in IONEX format.

        IONEX (IONosphere map EXchange) is the standard format for TEC maps,
        used by the GPS community. This allows direct comparison with
        GPS-derived TEC maps from IGS.

        Args:
            result: VTECMapResult to write
            output_path: Output file path
            station_id: 4-character station identifier

        Returns:
            True if successful
        """
        try:
            ts = datetime.fromtimestamp(result.timestamp, tz=timezone.utc)

            with open(output_path, 'w') as f:
                # IONEX header
                f.write(f"     1.0            IONOSPHERE MAPS     {'HF-TIMESTD':20s}IONEX VERSION / TYPE\n")
                f.write(f"hf-timestd          {station_id:20s}{ts.strftime('%Y%m%d %H%M%S'):20s}PGM / RUN BY / DATE\n")
                f.write(f"HF-derived regional VTEC map from multi-path measurements   COMMENT\n")
                f.write(f"  {ts.year:6d}{ts.month:6d}{ts.day:6d}{ts.hour:6d}{ts.minute:6d}{ts.second:6d}                        EPOCH OF FIRST MAP\n")
                f.write(f"  {ts.year:6d}{ts.month:6d}{ts.day:6d}{ts.hour:6d}{ts.minute:6d}{ts.second:6d}                        EPOCH OF LAST MAP\n")
                f.write(f"  3600                                                        INTERVAL\n")
                f.write(f"     1                                                        # OF MAPS IN FILE\n")
                f.write(f"  NONE                                                        MAPPING FUNCTION\n")
                f.write(f"   {result.center_lat:6.1f}                                                  BASE RADIUS\n")
                f.write(f"     2                                                        MAP DIMENSION\n")

                # Grid definition
                lat_min = result.center_lat - self.grid_extent_deg
                lat_max = result.center_lat + self.grid_extent_deg
                lon_min = result.center_lon - self.grid_extent_deg
                lon_max = result.center_lon + self.grid_extent_deg
                dlat = self.grid_resolution_deg
                dlon = self.grid_resolution_deg

                f.write(f"   {lat_min:6.1f}{lat_max:6.1f}{dlat:6.1f}                                    HGT1 / HGT2 / DHGT\n")
                f.write(f"   {lat_min:6.1f}{lat_max:6.1f}{dlat:6.1f}                                    LAT1 / LAT2 / DLAT\n")
                f.write(f"   {lon_min:6.1f}{lon_max:6.1f}{dlon:6.1f}                                    LON1 / LON2 / DLON\n")
                f.write(f"    -1                                                        EXPONENT\n")
                f.write(f"                                                              END OF HEADER\n")

                # TEC map
                f.write(f"     1                                                        START OF TEC MAP\n")
                f.write(f"  {ts.year:6d}{ts.month:6d}{ts.day:6d}{ts.hour:6d}{ts.minute:6d}{ts.second:6d}                        EPOCH OF CURRENT MAP\n")

                # Write grid values
                if result.grid_vtec:
                    for i, lat in enumerate(result.grid_lats):
                        f.write(f"   {lat:6.1f}{lon_min:6.1f}{lon_max:6.1f}{dlon:6.1f}{self.shell_height_km:6.1f}                LAT/LON1/LON2/DLON/H\n")
                        row = result.grid_vtec[i] if i < len(result.grid_vtec) else []
                        # IONEX values are in 0.1 TECU
                        values = [int(v * 10) if not math.isnan(v) else 9999 for v in row]
                        # Write 16 values per line
                        for j in range(0, len(values), 16):
                            chunk = values[j:j + 16]
                            f.write(''.join(f'{v:5d}' for v in chunk) + '\n')

                f.write(f"     1                                                        END OF TEC MAP\n")
                f.write(f"                                                              END OF FILE\n")

            logger.info(f"IONEX file written: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to write IONEX file: {e}")
            return False

    def _mapping_function(self, elevation_deg: float) -> float:
        """
        Thin-shell mapping function: converts vTEC to sTEC.

        M(elev) = 1 / sqrt(1 - (R_E · cos(elev) / (R_E + h))²)

        Returns mapping factor M >= 1.0.
        """
        if elevation_deg >= 90.0:
            return 1.0
        if elevation_deg < 5.0:
            elevation_deg = 5.0

        R = EARTH_RADIUS_KM
        h = self.shell_height_km
        cos_elev = math.cos(math.radians(elevation_deg))

        ratio = R * cos_elev / (R + h)
        sin_sq = 1.0 - ratio * ratio

        if sin_sq <= 0.01:
            return 10.0

        return 1.0 / math.sqrt(sin_sq)

    @staticmethod
    def _intermediate_point(
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        fraction: float
    ) -> Tuple[float, float]:
        """Compute intermediate point on great circle at given fraction."""
        lat1_r = math.radians(lat1)
        lon1_r = math.radians(lon1)
        lat2_r = math.radians(lat2)
        lon2_r = math.radians(lon2)

        d = 2 * math.asin(math.sqrt(
            math.sin((lat2_r - lat1_r) / 2) ** 2 +
            math.cos(lat1_r) * math.cos(lat2_r) *
            math.sin((lon2_r - lon1_r) / 2) ** 2
        ))

        if d < 1e-12:
            return lat1, lon1

        a = math.sin((1 - fraction) * d) / math.sin(d)
        b = math.sin(fraction * d) / math.sin(d)

        x = a * math.cos(lat1_r) * math.cos(lon1_r) + b * math.cos(lat2_r) * math.cos(lon2_r)
        y = a * math.cos(lat1_r) * math.sin(lon1_r) + b * math.cos(lat2_r) * math.sin(lon2_r)
        z = a * math.sin(lat1_r) + b * math.sin(lat2_r)

        lat = math.degrees(math.atan2(z, math.sqrt(x ** 2 + y ** 2)))
        lon = math.degrees(math.atan2(y, x))
        return lat, lon

    @staticmethod
    def _build_poly_matrix(dlat: np.ndarray, dlon: np.ndarray, degree: int) -> np.ndarray:
        """Build design matrix for 2D polynomial of given degree."""
        n = len(dlat)
        cols = []
        for i in range(degree + 1):
            for j in range(degree + 1 - i):
                cols.append(dlat ** i * dlon ** j)
        return np.column_stack(cols)

    def _evaluate_grid(
        self,
        coeffs: np.ndarray,
        degree: int,
        center_lat: float,
        center_lon: float,
    ) -> Tuple[List[float], List[float], List[List[float]]]:
        """Evaluate polynomial on a regular lat/lon grid."""
        lat_min = center_lat - self.grid_extent_deg
        lat_max = center_lat + self.grid_extent_deg
        lon_min = center_lon - self.grid_extent_deg
        lon_max = center_lon + self.grid_extent_deg

        lats = np.arange(lat_min, lat_max + 0.01, self.grid_resolution_deg)
        lons = np.arange(lon_min, lon_max + 0.01, self.grid_resolution_deg)

        grid = []
        for lat in lats:
            row = []
            for lon in lons:
                dlat = lat - center_lat
                dlon = lon - center_lon
                val = 0.0
                idx = 0
                for i in range(degree + 1):
                    for j in range(degree + 1 - i):
                        if idx < len(coeffs):
                            val += coeffs[idx] * dlat ** i * dlon ** j
                        idx += 1
                row.append(max(0.0, val))  # vTEC >= 0
            grid.append(row)

        return lats.tolist(), lons.tolist(), grid
