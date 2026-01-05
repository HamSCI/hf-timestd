#!/usr/bin/env python3
"""
Coherent Multi-Frequency TEC Estimator
================================================================================
Calculates Total Electron Content (TEC) and Ionospheric Group Delay using
multi-frequency Time-of-Arrival (ToA) measurements.

Physics:
--------
The ionosphere introduces a frequency-dependent group delay:
    τ(f) = K · TEC / f²
    Where K ≈ 40.3 m³/s² (constant)

The observed arrival time is:
    T_obs(f) = T_vacuum + τ(f) + ε

    T_obs(f) = T_vacuum + (40.3 · TEC) / f²

Algorithm:
----------
We treat this as a linear regression problem where we solve for two unknowns:
1. T_vacuum (True arrival time if space were empty)
2. TEC (Total Electron Content)

Model: y = mx + c
    y = T_obs(f)
    x = 1/f²
    m = 40.3 · TEC
    c = T_vacuum

We use Least Squares Fitting to find 'm' and 'c' from N measurements.
This is superior to pairwise comparison because it uses all available data (redundancy).

Units:
------
    f: Hz
    T: seconds
    TEC: electrons / m² (TECU = 10^16 el/m2)
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict
import logging

logger = logging.getLogger(__name__)

# Physical constants
K_IONOSPHERE = 40.3  # m³/s²
TECU_SCALE = 1e16    # 1 TECU = 10^16 el/m²


@dataclass
class TECResult:
    """Result of TEC estimation."""
    station: str
    timestamp: float
    
    # Solved Parameters
    tec_electrons_m2: float         # Total Electron Content
    tec_u: float                    # TEC in TECU
    t_vacuum_error_ms: float        # True timing error (vacuum path)
    
    # Corrections
    group_delay_ms: Dict[float, float]  # Calculated delay per frequency (MHz)
    
    # Quality Metrics
    confidence: float               # 0.0 - 1.0 (R² of fit)
    residuals_ms: float             # RMS calculation error
    n_frequencies: int              # Number of points used


class TECEstimator:
    """
    Estimates Ionospheric parameters from multi-frequency measurements.
    """
    
    def __init__(self, high_precision_mode: bool = True):
        self.high_precision = high_precision_mode

    def estimate_tec(
        self, 
        measurements: List[Dict[str, float]], 
        station: str,
        timestamp: float
    ) -> Optional[TECResult]:
        """
        Estimate TEC from a list of measurements for a single timestamp/station.
        
        Args:
            measurements: List of dicts with keys:
                'frequency_hz': float (e.g. 5e6)
                'toa_ms': float (observed time of arrival offset in ms)
                'uncertainty_ms': float (optional weighting)
            station: Station name (e.g. "WWV")
            timestamp: Unix timestamp
            
        Returns:
            TECResult if successful (needs at least 2 frequencies), else None
        """
        # Need at least 2 distinct frequencies to solve 2 unknowns
        if len(measurements) < 2:
            return None
            
        # Extract vectors
        freqs = []
        toas = []
        weights = []
        
        for m in measurements:
            f = m['frequency_hz']
            t = m['toa_ms'] / 1000.0  # Convert ms to seconds
            u_ms = m.get('uncertainty_ms', 1.0)
            
            # Use inverse variance weighting (1/sigma^2)
            # Default uncertainty 1ms -> weight 1.0
            w = 1.0 / (max(u_ms, 0.1) ** 2)
            
            freqs.append(f)
            toas.append(t)
            weights.append(w)
            
        freqs = np.array(freqs)
        toas = np.array(toas)
        weights = np.array(weights)
        
        # Check for frequency diversity (prevent singular matrix if freq is same)
        if np.std(freqs) < 1000.0: # Less than 1kHz spread
            logger.warning(f"Insufficient frequency diversity for {station}: {freqs}")
            return None
            
        # Formulate Least Squares
        # y = T_obs
        # x = 1 / f^2
        # T_obs = T_vac + (40.3 · TEC) * (1/f^2)
        # y = c + m * x
        
        x = 1.0 / (freqs ** 2)
        y = toas
        
        # Weighted Least Squares
        # V = [1, x]
        # Y = [y]
        # W = diag(weights)
        # Solution = inv(V'WV) * V'Wy
        
        # Using numpy polyfit approach with weights (cov matrix scaling)
        # Note: polyfit(x, y, 1) returns [m, c] i.e [slope, intercept]
        # weights in polyfit are 1/sigma? No, polyfit w is 1/sigma.
        # My 'weights' variable is 1/sigma^2. So sqrt it.
        
        try:
            # Polyfit solves p[0]*x + p[1] = y
            # p[0] = m = 40.3 * TEC
            # p[1] = c = T_vacuum
            
            poly_weights = np.sqrt(weights)
            
            # Use cov=True only if we have degrees of freedom (N > 2)
            # Otherwise we get a LinAlgError or undetermined covariance
            if len(x) > 2:
                p, cov = np.polyfit(x, y, 1, w=poly_weights, cov=True)
            else:
                p = np.polyfit(x, y, 1, w=poly_weights, cov=False)
                cov = None
            
            m = p[0]
            c = p[1]
            
            # Extract Physics
            tec = m / K_IONOSPHERE
            t_vacuum = c
            
            # Calculate R^2 (Coefficient of Determination)
            y_pred = m * x + c
            ss_res = np.sum(weights * (y - y_pred) ** 2)
            ss_tot = np.sum(weights * (y - np.average(y, weights=weights)) ** 2)
            
            # Handle perfect fit (ss_tot = 0) or numerical noise
            if ss_tot < 1e-20:
                confidence = 0.0 # Can't determine confidence on flat line (though unlikely)
            else:
                 r2 = 1.0 - (ss_res / ss_tot)
                 confidence = max(0.0, min(1.0, r2))
                 
            # Calculate per-frequency group delays
            group_delays_ms = {}
            for f_hz in freqs:
                 # delay = T_obs - T_vac = K * TEC / f^2 = m / f^2
                 delay_sec = m / (f_hz ** 2)
                 f_mhz = f_hz / 1e6
                 group_delays_ms[f_mhz] = delay_sec * 1000.0
                 
            # Convert units for output
            tec_u = tec / TECU_SCALE
            t_vacuum_ms = t_vacuum * 1000.0
            
            # Sanity checks
            # TEC should be positive (physically). 
            # If slope is negative, it means high freq arrived SLOWER than low freq.
            # This is unphysical for group delay (dispersion). 
            # Could imply measurement error or extreme multipath.
            if tec < 0:
                logger.debug(f"Negative TEC detected for {station}: {tec:.2e}. Setting confidence low.")
                confidence *= 0.1 # Penalize unphysical result
            
            # DIAGNOSTIC: Log "flat" or suspiciously perfect data (0.0 TEC issue)
            if tec < 1.0 or confidence > 0.99:
                log_level = logging.WARNING if tec < 1.0 else logging.DEBUG
                logger.log(log_level, 
                    f"Suspicious TEC result for {station}: TEC={tec:.2f}, R2={confidence:.4f}\n"
                    f"  Inputs (Freq MHz -> ToA ms): " + 
                    ", ".join([f"{f/1e6:.1f}->{t*1000:.3f}" for f, t in zip(freqs, toas)])
                )
            
            # Calculate RMS residual in ms
            rms_residual_ms = np.sqrt(np.mean((y - y_pred)**2)) * 1000.0

            return TECResult(
                station=station,
                timestamp=timestamp,
                tec_electrons_m2=tec,
                tec_u=tec_u,
                t_vacuum_error_ms=t_vacuum_ms,
                group_delay_ms=group_delays_ms,
                confidence=confidence,
                residuals_ms=rms_residual_ms,
                n_frequencies=len(freqs)
            )
            
        except Exception as e:
            logger.error(f"TEC Least Squares failed: {e}")
            return None

