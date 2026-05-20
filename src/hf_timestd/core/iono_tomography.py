#!/usr/bin/env python3
"""
Multi-Layer E/F Ionospheric Tomography
================================================================================
Separates E-layer and F-layer TEC contributions using the geometric diversity
of 17 HF ray paths at different elevation angles and azimuths.

Physics:
--------
Each ray path's slant TEC (sTEC) is the integral of electron density along
the path. For a two-shell model:

    sTEC_i = Ne_E · L_E_i + Ne_F · L_F_i

where:
    Ne_E, Ne_F = column electron density in E and F shells (el/m²)
    L_E_i = path length through E-shell for ray i (obliquity factor)
    L_F_i = path length through F-shell for ray i (obliquity factor)

The obliquity factor for a thin shell at height h is:
    M(h, elev) = 1 / sqrt(1 - (R_E · cos(elev) / (R_E + h))²)

Low-elevation paths (BPM: ~5-10°) traverse more E-layer relative to F-layer
than high-elevation paths (CHU: ~30-40°). This geometric diversity allows
separation of the two contributions.

The system of equations is:
    [sTEC_1]   [M_E_1  M_F_1] [TEC_E]
    [sTEC_2] = [M_E_2  M_F_2] [TEC_F]
    [  ...  ]   [ ...    ... ]
    [sTEC_N]   [M_E_N  M_F_N]

Solved via constrained least squares with:
- TEC_E >= 0, TEC_F >= 0
- TEC_E << TEC_F during daytime (E-layer is ~10% of total)
- TEC_E ≈ 0 at night (no solar ionization)
- Ne(h) profile shape constrained by Chapman layer model

================================================================================
"""

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

# Physical constants
EARTH_RADIUS_KM = 6371.0

# Shell boundaries (km)
E_SHELL_BOTTOM = 90.0
E_SHELL_TOP = 150.0
E_SHELL_CENTER = 110.0

F_SHELL_BOTTOM = 150.0
F_SHELL_TOP = 500.0
F_SHELL_CENTER = 300.0

# Below this posterior-vs-prior variance reduction for the E-layer, the
# E/F split is treated as prior-dominated rather than a measurement (P-H11).
PRIOR_DOMINATED_VR_THRESHOLD = 0.5


@dataclass
class TomographyResult:
    """Result of E/F layer tomographic separation."""
    timestamp: float
    
    # Layer TEC estimates (TECU)
    tec_e_tecu: float           # E-layer TEC
    tec_f_tecu: float           # F-layer TEC
    tec_total_tecu: float       # E + F total
    
    # Layer parameters.  §4.4 Low note: ``effective_hmF2_km`` is the
    # **input** F-shell height the tomography assumed, echoed back on
    # the result for traceability -- *not* a fitted "effective" value.
    # The tomography has no DOF to constrain hmF2 (it carries one
    # column of unknowns per layer and uses a single height as a
    # geometric anchor); a real effective-hmF2 estimate would require
    # multi-shell ray bending.
    effective_hmF2_km: float    # Input F-shell height (echoed; see note above)
    e_f_ratio: float            # TEC_E / TEC_F ratio
    
    # Quality metrics
    n_paths: int                # Number of ray paths used
    rms_residual_tecu: float    # RMS fit residual
    condition_number: float     # Matrix condition (higher = less stable)
    confidence: float           # 0-1 overall confidence

    # E/F-split identifiability (P-H11): how much the data sharpened each
    # layer relative to its prior.  Near 0 => the value is essentially the
    # prior, not a measurement.
    variance_reduction_e: float = 0.0   # 1 - Var_posterior(E)/Var_prior(E)
    variance_reduction_f: float = 0.0   # 1 - Var_posterior(F)/Var_prior(F)
    prior_dominated: bool = True        # True => data does not constrain the split

    # Per-path diagnostics
    path_residuals: Dict[str, float] = field(default_factory=dict)
    
    # Solar context
    is_daytime: bool = True
    solar_elevation_deg: float = 0.0

    # True if the optimiser converged. A non-converged solve (P-M10) keeps
    # its estimate but is heavily down-confidenced; consumers gate on this.
    converged: bool = True


@dataclass
class RayPath:
    """A single HF ray path with geometry."""
    station: str
    frequency_mhz: float
    elevation_deg: float        # Launch elevation angle
    azimuth_deg: float          # Azimuth from receiver
    distance_km: float          # Great circle distance
    propagation_mode: str       # e.g., '1F', '2F', '1E'
    n_hops: int
    stec_tecu: float            # Measured slant TEC for this path
    uncertainty_tecu: float     # Measurement uncertainty


class IonoTomography:
    """
    Two-shell ionospheric tomography using multi-path HF measurements.
    
    Uses the geometric diversity of 17 HF ray paths to separate E-layer
    and F-layer TEC contributions via constrained least squares.
    """
    
    def __init__(
        self,
        receiver_lat: float = 38.92,
        receiver_lon: float = -92.13,
        e_shell_height_km: float = E_SHELL_CENTER,
        f_shell_height_km: float = F_SHELL_CENTER,
    ):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.e_shell_height_km = e_shell_height_km
        self.f_shell_height_km = f_shell_height_km
    
    def solve(
        self,
        paths: List[RayPath],
        solar_elevation_deg: float = 45.0,
        prior_tec_e_tecu: Optional[float] = None,
        prior_tec_f_tecu: Optional[float] = None,
    ) -> Optional[TomographyResult]:
        """
        Solve for E-layer and F-layer TEC from multiple ray paths.
        
        Args:
            paths: List of RayPath objects with measured sTEC
            solar_elevation_deg: Solar elevation at receiver (for E-layer prior)
            prior_tec_e_tecu: Optional prior estimate for E-layer TEC
            prior_tec_f_tecu: Optional prior estimate for F-layer TEC
            
        Returns:
            TomographyResult or None if insufficient data
        """
        # §4.4 Low: require at least 3 paths.  With the 2-layer (E, F)
        # model, 2 paths give an exactly-determined system and the
        # residuals are by construction zero — meaningless as a quality
        # signal.  ≥3 leaves at least one DOF for residuals.  The
        # elevation-diversity gate below independently guards against
        # near-singular geometry.
        _MIN_PATHS_FOR_RESIDUAL_DOF = 3

        if len(paths) < _MIN_PATHS_FOR_RESIDUAL_DOF:
            logger.debug(
                f"Tomography needs at least {_MIN_PATHS_FOR_RESIDUAL_DOF} "
                f"paths (got {len(paths)}; need >=1 residual DOF)"
            )
            return None

        # Filter to paths with valid sTEC
        valid_paths = [p for p in paths if p.stec_tecu > 0 and p.uncertainty_tecu > 0]
        if len(valid_paths) < _MIN_PATHS_FOR_RESIDUAL_DOF:
            return None

        # P-M9: restrict the E/F tomography to SINGLE-HOP paths. A multi-hop
        # path's hops pierce the ionosphere at points hundreds of km apart,
        # but the 2-layer (E, F) model carries one column of unknowns —
        # folding a multi-hop path in via n_hops × obliquity silently assumed
        # the ionosphere is horizontally uniform over the whole multi-thousand-
        # km track. Single-hop paths sample one column, where that holds.
        valid_paths = [p for p in valid_paths if p.n_hops == 1]
        if len(valid_paths) < _MIN_PATHS_FOR_RESIDUAL_DOF:
            logger.debug(
                f"Tomography needs >= {_MIN_PATHS_FOR_RESIDUAL_DOF} "
                f"single-hop paths"
            )
            return None

        # Check elevation diversity
        elevations = [p.elevation_deg for p in valid_paths]
        elev_range = max(elevations) - min(elevations)
        if elev_range < 5.0:
            logger.debug(f"Insufficient elevation diversity: {elev_range:.1f}°")
            return None
        
        # Build the observation matrix
        # Each row: [M_E_i, M_F_i] for path i
        # Observation: sTEC_i
        n = len(valid_paths)
        A = np.zeros((n, 2))
        b = np.zeros(n)
        W = np.zeros(n)  # Weights (inverse variance)
        
        for i, path in enumerate(valid_paths):
            # valid_paths is single-hop only (see above), so the obliquity
            # factor maps one shell traversal — no n_hops multiply.
            A[i, 0] = self._obliquity_factor(path.elevation_deg, self.e_shell_height_km)
            A[i, 1] = self._obliquity_factor(path.elevation_deg, self.f_shell_height_km)

            b[i] = path.stec_tecu
            W[i] = 1.0 / (path.uncertainty_tecu ** 2)
        
        # Determine if nighttime (E-layer should vanish)
        is_daytime = solar_elevation_deg > 0
        
        # Set up priors
        if prior_tec_e_tecu is None:
            if is_daytime:
                prior_tec_e_tecu = 3.0  # Typical daytime E-layer
            else:
                prior_tec_e_tecu = 0.1  # Near-zero at night
        
        if prior_tec_f_tecu is None:
            prior_tec_f_tecu = 20.0  # Moderate F-layer
        
        # Constrained least squares
        # Minimize: ||W^(1/2) (A·x - b)||² + λ_E·(x_E - prior_E)² + λ_F·(x_F - prior_F)²
        # Subject to: x_E >= 0, x_F >= 0
        
        # Prior strength: stronger at night for E-layer (we're confident it's near zero)
        lambda_e = 0.1 if is_daytime else 10.0  # Strong night prior
        lambda_f = 0.01  # Weak F-layer prior (let data speak)
        
        W_sqrt = np.sqrt(W)
        
        def objective(x):
            tec_e, tec_f = x
            residuals = W_sqrt * (A @ x - b)
            data_term = np.sum(residuals ** 2)
            prior_term = (lambda_e * (tec_e - prior_tec_e_tecu) ** 2 +
                         lambda_f * (tec_f - prior_tec_f_tecu) ** 2)
            return data_term + prior_term
        
        # Initial guess from priors
        x0 = np.array([prior_tec_e_tecu, prior_tec_f_tecu])
        
        # Bounds: both TEC values must be non-negative
        # E-layer capped at 30 TECU (physical maximum)
        # F-layer capped at 200 TECU (extreme solar max)
        bounds = [(0.0, 30.0), (0.0, 200.0)]
        
        try:
            result = minimize(
                objective, x0, method='L-BFGS-B', bounds=bounds,
                options={'maxiter': 100, 'ftol': 1e-10}
            )
        except (ValueError, np.linalg.LinAlgError) as e:
            # P-M10: catch only the errors a genuine ill-posed solve raises;
            # anything else is a real bug and must propagate.
            logger.warning(f"Tomography optimization failed: {e}")
            return None

        tec_e = float(result.x[0])
        tec_f = float(result.x[1])

        # P-M10: the objective is a smooth convex quadratic with box bounds,
        # so a non-converged L-BFGS-B result signals trouble. The estimate is
        # kept (it may still be roughly right) but `converged` is recorded and
        # the confidence is heavily penalised below — a non-converged solve
        # must not be presented as a normal result.
        converged = bool(result.success)
        if not converged:
            logger.warning(
                f"Tomography did not converge ({result.message}) — "
                f"result down-confidenced"
            )
        
        # Compute fit quality
        fitted = A @ result.x
        residuals = b - fitted
        weighted_residuals = W_sqrt * residuals
        rms_residual = float(np.sqrt(np.mean(residuals ** 2)))
        
        # Condition number of the weighted design matrix
        A_weighted = (W_sqrt[:, np.newaxis]) * A
        try:
            cond = float(np.linalg.cond(A_weighted))
        except np.linalg.LinAlgError:
            cond = 1e6
        
        # Per-path residuals.  §4.4 Low: include mode and use 2-decimal
        # frequency precision so that (a) two paths from the same
        # channel but different propagation modes don't collide on the
        # key, and (b) CHU's fractional-MHz channels (3.33, 7.85, 14.67)
        # don't all collapse to the same integer key.  Falls back to
        # n_hops-as-string when path.mode is absent.
        path_residuals: Dict[str, float] = {}
        for i, path in enumerate(valid_paths):
            mode_tag = getattr(path, 'mode', None) or f"{getattr(path, 'n_hops', 1)}F"
            key = f"{path.station}_{path.frequency_mhz:.2f}_{mode_tag}"
            path_residuals[key] = float(residuals[i])
        
        # Posterior-vs-prior variance reduction for the E/F split (P-H11).
        # The E- and F-shell thin-shell obliquity factors are nearly
        # proportional across the available elevations, so AᵀWA is almost
        # singular in the E/F-split direction and the MAP estimate collapses
        # onto the prior. The posterior covariance (AᵀWA + Λ)⁻¹ versus the
        # prior covariance Λ⁻¹ quantifies how much the data actually
        # sharpened each layer.
        Lambda = np.diag([lambda_e, lambda_f])
        AtWA = A.T @ (W[:, np.newaxis] * A)
        try:
            cov_post = np.linalg.inv(AtWA + Lambda)
            var_reduction_e = float(np.clip(1.0 - cov_post[0, 0] * lambda_e, 0.0, 1.0))
            var_reduction_f = float(np.clip(1.0 - cov_post[1, 1] * lambda_f, 0.0, 1.0))
        except np.linalg.LinAlgError:
            var_reduction_e = 0.0
            var_reduction_f = 0.0
        prior_dominated = var_reduction_e < PRIOR_DOMINATED_VR_THRESHOLD
        if prior_dominated:
            logger.debug(
                f"Tomography E/F split is prior-dominated (E-layer variance "
                f"reduction {var_reduction_e:.0%}): tec_e_tecu={tec_e:.1f} "
                f"reflects the prior, not the data"
            )

        # Confidence based on fit quality and data quantity. Higher confidence
        # with more paths, lower residuals, better conditioning, and a
        # data-constrained (not prior-dominated) E/F split.
        conf_paths = min(1.0, len(valid_paths) / 6.0)  # 6+ paths for full confidence
        conf_residual = max(0.0, 1.0 - rms_residual / 5.0)  # 5 TECU residual = 0 confidence
        conf_cond = max(0.0, 1.0 - math.log10(max(1, cond)) / 4.0)  # cond > 10000 = 0
        conf_split = var_reduction_e  # 0 => the reported E/F split is the prior
        confidence = conf_paths * conf_residual * conf_cond * conf_split
        if not converged:
            confidence *= 0.1  # P-M10: a non-converged solve is not trusted

        # E/F ratio
        tec_total = tec_e + tec_f
        e_f_ratio = tec_e / max(0.01, tec_f)
        
        return TomographyResult(
            timestamp=0.0,  # Caller should set this
            tec_e_tecu=tec_e,
            tec_f_tecu=tec_f,
            tec_total_tecu=tec_total,
            effective_hmF2_km=self.f_shell_height_km,
            e_f_ratio=e_f_ratio,
            n_paths=len(valid_paths),
            rms_residual_tecu=rms_residual,
            condition_number=cond,
            confidence=confidence,
            variance_reduction_e=var_reduction_e,
            variance_reduction_f=var_reduction_f,
            prior_dominated=prior_dominated,
            path_residuals=path_residuals,
            is_daytime=is_daytime,
            solar_elevation_deg=solar_elevation_deg,
            converged=converged,
        )
    
    @staticmethod
    def _obliquity_factor(elevation_deg: float, shell_height_km: float) -> float:
        """
        Compute the thin-shell obliquity (mapping) factor.
        
        M(h, elev) = 1 / sqrt(1 - (R_E · cos(elev) / (R_E + h))²)
        
        This gives the ratio of slant path length through the shell to
        vertical path length.
        
        Args:
            elevation_deg: Ray elevation angle (degrees)
            shell_height_km: Shell height (km)
            
        Returns:
            Obliquity factor (dimensionless, >= 1.0)
        """
        if elevation_deg >= 90.0:
            return 1.0
        if elevation_deg < 3.0:
            elevation_deg = 3.0  # Avoid extreme values
        
        R = EARTH_RADIUS_KM
        h = shell_height_km
        cos_elev = math.cos(math.radians(elevation_deg))
        
        ratio = R * cos_elev / (R + h)
        sin_sq = 1.0 - ratio * ratio
        
        if sin_sq <= 0.01:
            return 10.0  # Cap at reasonable maximum
        
        return 1.0 / math.sqrt(sin_sq)
    
    def build_paths_from_tec_results(
        self,
        tec_results: Dict[str, Any],
        propagation_predictions: Optional[Dict] = None,
    ) -> List[RayPath]:
        """
        Build RayPath objects from TEC estimation results and propagation predictions.
        
        This is a convenience method for integrating with the existing pipeline.
        
        Args:
            tec_results: Dict mapping station → TECResult or dict with tec_tecu, etc.
            propagation_predictions: Dict mapping (station, freq) → PropagationPrediction
            
        Returns:
            List of RayPath objects
        """
        paths = []
        
        for key, result in tec_results.items():
            if isinstance(key, tuple):
                station = key[0]
            else:
                station = str(key)
            
            # Extract TEC and frequency info
            if hasattr(result, 'tec_u'):
                stec = result.tec_u
                confidence = result.confidence
                freqs = list(result.group_delay_ms.keys()) if hasattr(result, 'group_delay_ms') else []
            elif isinstance(result, dict):
                stec = result.get('tec_tecu', 0)
                confidence = result.get('confidence', 0)
                freqs_str = result.get('frequencies_mhz', '')
                freqs = [float(f) for f in freqs_str.split(',') if f.strip()] if freqs_str else []
            else:
                continue
            
            if stec <= 0 or confidence < 0.3:
                continue
            
            # Geometry from propagation predictions. P-M9: real geometry is
            # required — the contract forbids inventing it. A path with no
            # propagation prediction (no primary arrival giving a real
            # elevation and hop count) is SKIPPED, not given a fabricated
            # 30°/1-hop default that would silently corrupt the obliquity
            # mapping and the single-hop filter in solve().
            for freq_mhz in freqs:
                primary = None
                distance = None
                if propagation_predictions:
                    pred = propagation_predictions.get((station, freq_mhz))
                    if pred and hasattr(pred, 'get_primary_arrival'):
                        primary = pred.get_primary_arrival()
                    if pred and hasattr(pred, 'distance_km'):
                        distance = pred.distance_km

                if primary is None:
                    logger.debug(
                        f"Tomography: skipping {station} {freq_mhz} MHz — "
                        f"no propagation prediction (geometry must not be "
                        f"fabricated)"
                    )
                    continue

                uncertainty = max(1.0, stec * (1.0 - confidence))

                paths.append(RayPath(
                    station=station,
                    frequency_mhz=freq_mhz,
                    elevation_deg=primary.elevation_angle_deg,
                    azimuth_deg=0.0,
                    distance_km=distance if distance is not None else 0.0,
                    propagation_mode=primary.mode.label,
                    n_hops=primary.mode.n_hops,
                    stec_tecu=stec,
                    uncertainty_tecu=uncertainty,
                ))
        
        return paths
