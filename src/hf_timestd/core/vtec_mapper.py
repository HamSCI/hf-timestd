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

# 2D polynomial fit conditioning (P-H10)
RIDGE_LAMBDA = 0.1            # Column-scaled Tikhonov ridge strength
MAX_CONDITION_NUMBER = 1.0e8  # Above this the weighted design matrix is
                              # treated as effectively rank-deficient
                              # (clustered IPPs) and confidence collapses


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
    rms_residual_tecu: float = 0.0      # in-sample fit residual (optimistic)
    cv_rms_residual_tecu: float = 0.0   # leave-one-out CV residual — the
                                        # honest out-of-sample metric (P-M8);
                                        # NaN if N is too small to cross-validate
    confidence: float = 0.0
    spatial_coverage_km: float = 0.0
    condition_number: float = 0.0   # cond(weighted design matrix); large => clustered IPPs


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
        receiver_lat: float,
        receiver_lon: float,
        shell_height_km: float = THIN_SHELL_HEIGHT_KM,
        poly_degree: int = 2,
        grid_resolution_deg: float = 1.0,
        grid_extent_deg: float = 15.0,
    ):
        # §4.4 Low: `receiver_lat`/`receiver_lon` used to default to
        # 38.92 / -92.13 (a Missouri-area host).  The METROLOGY contract
        # forbids hard-coded site coords because they silently corrupt
        # geometry for every other host.  Required positional args now;
        # callers must source coords from their config / station model.
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

        # Conditioning of the weighted design matrix. Clustered IPPs make the
        # polynomial basis nearly collinear; a plain lstsq then oscillates
        # wildly off-cluster while the in-sample RMS still looks good (P-H10).
        A_w = A * np.sqrt(W)[:, np.newaxis]
        try:
            cond = float(np.linalg.cond(A_w))
        except np.linalg.LinAlgError:
            cond = float('inf')
        if not math.isfinite(cond) or cond > MAX_CONDITION_NUMBER:
            logger.warning(
                f"VTEC fit ill-conditioned (cond={cond:.2e}, {len(measurements)} "
                f"IPPs over {coverage_km:.0f} km) - regularised, confidence reduced"
            )

        # Primary fit — weighted, ridge-regularised least squares (the solve
        # lives in _solve_vtec_poly so leave-one-out below refits identically).
        coeffs = self._solve_vtec_poly(A, vtecs, W)
        if coeffs is None:
            logger.warning("VTEC polynomial fit failed")
            return None

        # In-sample fit residual — kept for reference, but it is optimistic
        # (the polynomial interpolates its own training points).
        rms_residual = float(np.sqrt(np.mean((vtecs - A @ coeffs) ** 2)))

        # P-M8: leave-one-out cross-validation. The in-sample residual above
        # systematically overstates map quality, so `confidence` is based on
        # an out-of-sample metric instead: refit on N-1 points, predict the
        # held-out one, and take the RMS of those errors. LOO needs N-1 >=
        # n_coeffs so each refit stays determined; when it does not, the map
        # cannot be honestly cross-validated and cv_rms is left NaN.
        cv_rms = float('nan')
        if len(measurements) - 1 >= n_coeffs:
            cv_errors = []
            for i in range(len(measurements)):
                hold = np.ones(len(measurements), dtype=bool)
                hold[i] = False
                ci = self._solve_vtec_poly(A[hold], vtecs[hold], W[hold])
                if ci is not None:
                    cv_errors.append(float(vtecs[i] - A[i] @ ci))
            if cv_errors:
                cv_rms = float(np.sqrt(np.mean(np.square(cv_errors))))

        # Evaluate on a regular grid; cells outside the convex hull of the
        # IPPs are pure extrapolation and are masked to NaN (P-H10).
        grid_lats, grid_lons, grid_vtec = self._evaluate_grid(
            coeffs, max_degree, center_lat, center_lon, lats, lons
        )

        # Confidence metric — the residual term uses the cross-validated RMS
        # when available; if the map was too small to cross-validate, fall
        # back to the in-sample RMS but halve the term (quality unverified).
        conf_n = min(1.0, len(measurements) / 8.0)
        if math.isfinite(cv_rms):
            conf_residual = max(0.0, 1.0 - cv_rms / 5.0)
        else:
            conf_residual = 0.5 * max(0.0, 1.0 - rms_residual / 5.0)
        conf_coverage = min(1.0, coverage_km / 1000.0)
        if math.isfinite(cond) and cond > 0.0:
            conf_cond = float(np.clip(
                (math.log10(MAX_CONDITION_NUMBER) - math.log10(cond)) /
                (math.log10(MAX_CONDITION_NUMBER) - 4.0), 0.0, 1.0))
        else:
            conf_cond = 0.0
        confidence = conf_n * conf_residual * conf_coverage * conf_cond

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
            cv_rms_residual_tecu=cv_rms,
            confidence=confidence,
            spatial_coverage_km=coverage_km,
            condition_number=cond,
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
                f.write(f"  6371.0                                                      BASE RADIUS\n")
                f.write(f"     2                                                        MAP DIMENSION\n")

                # Grid definition — taken from the ACTUAL evaluated grid
                # (result.grid_lats / grid_lons) so the bounds declared in the
                # header always match the data rows written below (P-M8);
                # recomputing them from center ± extent risked the header
                # drifting from the data. An empty grid cannot produce a
                # conformant IONEX file, so refuse rather than emit one.
                if not (result.grid_lats and result.grid_lons):
                    logger.warning(
                        "VTEC map has no evaluated grid — IONEX file not written")
                    return False
                lat_min = result.grid_lats[0]
                lat_max = result.grid_lats[-1]
                lon_min = result.grid_lons[0]
                lon_max = result.grid_lons[-1]
                dlat = (result.grid_lats[1] - result.grid_lats[0]
                        if len(result.grid_lats) > 1 else self.grid_resolution_deg)
                dlon = (result.grid_lons[1] - result.grid_lons[0]
                        if len(result.grid_lons) > 1 else self.grid_resolution_deg)

                f.write(f"  {self.shell_height_km:6.1f}{self.shell_height_km:6.1f}   0.0                                    HGT1 / HGT2 / DHGT\n")
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
    def _solve_vtec_poly(
        A: np.ndarray, vtecs: np.ndarray, weights: np.ndarray
    ) -> Optional[np.ndarray]:
        """
        Weighted, ridge-regularised least-squares solve for the 2D-polynomial
        VTEC coefficients. Factored out of generate_map so the primary fit and
        the leave-one-out refits (P-M8) use an identical procedure.

        Tikhonov (ridge) regularisation damps the non-constant polynomial
        terms that clustered IPPs cannot constrain: the penalty is scaled per
        column to each term's own magnitude, the constant term (column 0) is
        left unpenalised so the mean VTEC level is not shrunk toward zero, and
        the augmented system is full column rank so it also resolves the
        rank-deficiency a plain lstsq would hit. Returns None if the solve
        fails.
        """
        w_sqrt = np.sqrt(weights)
        a_w = A * w_sqrt[:, np.newaxis]
        b_w = vtecs * w_sqrt
        col_norms = np.linalg.norm(a_w, axis=0)
        penalty = RIDGE_LAMBDA * col_norms
        penalty[0] = 0.0
        a_aug = np.vstack([a_w, np.diag(penalty)])
        b_aug = np.concatenate([b_w, np.zeros(a_w.shape[1])])
        try:
            coeffs, *_ = np.linalg.lstsq(a_aug, b_aug, rcond=None)
            return coeffs
        except np.linalg.LinAlgError:
            return None

    @staticmethod
    def _poly_term_indices(degree: int) -> List[Tuple[int, int]]:
        """Triangular enumeration of 2-D polynomial monomial indices.

        Returns ``[(i, j)]`` pairs in the canonical fit/evaluate order:
        ``i+j <= degree``, with ``i`` outer.  Used by both the design-
        matrix builder and the point-evaluation loop so the term
        ordering is defined in exactly one place (§4.4 Low).
        """
        return [
            (i, j)
            for i in range(degree + 1)
            for j in range(degree + 1 - i)
        ]

    @classmethod
    def _build_poly_matrix(cls, dlat: np.ndarray, dlon: np.ndarray, degree: int) -> np.ndarray:
        """Build design matrix for 2D polynomial of given degree."""
        cols = [dlat ** i * dlon ** j for i, j in cls._poly_term_indices(degree)]
        return np.column_stack(cols)

    def _evaluate_grid(
        self,
        coeffs: np.ndarray,
        degree: int,
        center_lat: float,
        center_lon: float,
        ipp_lats: np.ndarray,
        ipp_lons: np.ndarray,
    ) -> Tuple[List[float], List[float], List[List[float]]]:
        """
        Evaluate the polynomial on a regular lat/lon grid.

        Cells outside the convex hull of the IPPs are pure extrapolation — the
        polynomial is unconstrained there — and are masked to NaN (P-H10).
        """
        lat_min = center_lat - self.grid_extent_deg
        lat_max = center_lat + self.grid_extent_deg
        lon_min = center_lon - self.grid_extent_deg
        lon_max = center_lon + self.grid_extent_deg

        lats = np.arange(lat_min, lat_max + 0.01, self.grid_resolution_deg)
        lons = np.arange(lon_min, lon_max + 0.01, self.grid_resolution_deg)

        # Convex hull of the IPPs defines the interpolation domain; cells
        # outside it are extrapolation and are not evaluated.
        hull = self._convex_hull(np.column_stack([ipp_lats, ipp_lons]))
        if hull is not None:
            lon_mesh, lat_mesh = np.meshgrid(lons, lats)
            grid_pts = np.column_stack([lat_mesh.ravel(), lon_mesh.ravel()])
            inside = self._points_in_hull(grid_pts, hull).reshape(lat_mesh.shape)
        else:
            # Degenerate IPP geometry (collinear/coincident): no interpolation
            # domain can be defined — fall back to evaluating every cell.
            inside = np.ones((len(lats), len(lons)), dtype=bool)

        grid = []
        for i, lat in enumerate(lats):
            row = []
            for j, lon in enumerate(lons):
                if not inside[i, j]:
                    row.append(float('nan'))
                    continue
                dlat = lat - center_lat
                dlon = lon - center_lon
                val = 0.0
                # §4.4 Low: share the term enumeration with the
                # design-matrix builder via `_poly_term_indices` so the
                # two can't disagree on monomial ordering.
                for idx, (ii, jj) in enumerate(self._poly_term_indices(degree)):
                    if idx < len(coeffs):
                        val += coeffs[idx] * dlat ** ii * dlon ** jj
                row.append(max(0.0, val))  # vTEC >= 0
            grid.append(row)

        return lats.tolist(), lons.tolist(), grid

    @staticmethod
    def _convex_hull(points: np.ndarray) -> Optional[np.ndarray]:
        """
        2D convex hull via Andrew's monotone chain.

        Args:
            points: (n, 2) array of (x, y) coordinates.

        Returns:
            Hull vertices (m, 2) in counter-clockwise order, or None if the
            points are degenerate (fewer than 3 distinct, or all collinear).
        """
        pts = np.unique(np.asarray(points, dtype=float), axis=0)
        if len(pts) < 3:
            return None
        pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

        def cross(o, a, b):
            return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

        lower: List[np.ndarray] = []
        for p in pts:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
                lower.pop()
            lower.append(p)
        upper: List[np.ndarray] = []
        for p in pts[::-1]:
            while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
                upper.pop()
            upper.append(p)

        hull = lower[:-1] + upper[:-1]
        if len(hull) < 3:
            return None
        return np.array(hull)

    @staticmethod
    def _points_in_hull(grid_pts: np.ndarray, hull: np.ndarray) -> np.ndarray:
        """
        Vectorised point-in-convex-polygon test.

        Args:
            grid_pts: (n, 2) query points.
            hull: (m, 2) convex polygon vertices in counter-clockwise order.

        Returns:
            Boolean array (n,): True where the point is inside or on the hull.
        """
        inside = np.ones(len(grid_pts), dtype=bool)
        m = len(hull)
        for i in range(m):
            a = hull[i]
            b = hull[(i + 1) % m]
            edge_x, edge_y = b[0] - a[0], b[1] - a[1]
            rel_x = grid_pts[:, 0] - a[0]
            rel_y = grid_pts[:, 1] - a[1]
            cross = edge_x * rel_y - edge_y * rel_x
            inside &= cross >= -1e-9
        return inside
