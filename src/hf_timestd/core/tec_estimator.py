#!/usr/bin/env python3
"""
Bayesian Multi-Frequency TEC Estimator
================================================================================
Calculates Total Electron Content (TEC) and Ionospheric Group Delay using
multi-frequency D_clock or ToA measurements with propagation mode priors.

Physics:
--------
The ionosphere introduces a frequency-dependent group delay:
    τ(f) = K · TEC / f²
    Where K ≈ 40.3 m³/s² (constant)

The observed D_clock (after geometric delay removal per-mode) is:
    D_clock(f) = D_common + K·TEC/f² + ε

where D_common absorbs any common-mode timing error.

Algorithm:
----------
Weighted Least Squares with iterative 3σ outlier rejection:
1. Fit D_clock(f) = slope/f² + intercept
2. Reject measurements with residuals > 3σ (mode misidentification)
3. Re-fit with cleaned measurements
4. slope = K_IONOSPHERE · TEC → extract TEC

Key improvement over v1: uses D_clock (geometric delay already subtracted
per-mode) instead of raw ToA. This eliminates mode-mixing contamination
because D_clock is mode-independent — the 1/f² residual IS the ionospheric
dispersion signal regardless of which propagation mode was used.

Units:
------
    f: Hz
    T: seconds
    TEC: electrons / m² (TECU = 10^16 el/m2)
"""

import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)

# Physical constants
# K ≈ 40.3 m³/s² (standard ionospheric constant for group delay in meters)
# For measurements in seconds, we divide by c
C_LIGHT = 299792458.0
K_IONOSPHERE = 40.3 / C_LIGHT  # seconds * Hz² * m² / electrons
TECU_SCALE = 1e16              # 1 TECU = 10^16 el/m²

# Confidence caps
MAX_CONFIDENCE_N2 = 0.3        # N=2 has zero residual DOF — cap confidence
OUTLIER_SIGMA = 3.0            # Reject measurements beyond this many σ
MAX_OUTLIER_ITERATIONS = 3     # Maximum outlier rejection passes


@dataclass
class TECResult:
    """Result of TEC estimation."""
    station: str
    timestamp: float
    tec_electrons_m2: float
    tec_u: float
    t_vacuum_error_ms: float
    # Corrections
    group_delay_ms: Dict[float, float]  # Calculated delay per frequency (MHz)

    # Quality Metrics
    confidence: float               # 0.0 - 1.0
    residuals_ms: float             # RMS calculation error
    n_frequencies: int              # Number of points used
    propagation_mode: str = 'UNKNOWN'  # Dominant mode or 'MIXED'
    n_rejected: int = 0             # Measurements rejected as outliers
    rejection_reason: Optional[str] = None


class TECEstimator:
    """
    Bayesian TEC estimator with mode priors and outlier rejection.

    Supports two input modes:
    1. Legacy: raw ToA measurements (backward compatible)
    2. D_clock: geometric-delay-subtracted measurements (preferred)

    The D_clock approach eliminates mode-mixing contamination because
    D_clock is mode-independent after per-mode geometric delay removal.
    """

    def __init__(self):
        pass

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
                'toa_ms': float (observed D_clock or ToA offset in ms)
                'uncertainty_ms': float (optional, timing uncertainty)
                'snr_db': float (optional, for SNR-based weighting)
                'mode_confidence': float (optional, 0-1 model confidence for assigned mode)
            station: Station name (e.g. "WWV")
            timestamp: Unix timestamp

        Returns:
            TECResult if a fit was obtained (needs at least 2 frequencies),
            else None. A negative ``tec_u`` IS returned (with confidence 0.0):
            negative TEC is a noisy estimate of a near-zero quantity and is
            retained, not rejected — see CR-2 in DATA_CONTRACT.md. None is
            returned only for genuine fit failure (too few frequencies,
            insufficient frequency diversity, or a singular fit).
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

            # Weight = inverse variance from timing uncertainty
            w = 1.0 / (max(u_ms, 0.1) ** 2)

            # Optionally boost weight by SNR (higher SNR = more reliable)
            snr_db = m.get('snr_db')
            if snr_db is not None and snr_db > 0:
                # Linear SNR weighting: 20 dB → 1.0, 40 dB → 2.0
                snr_factor = max(0.5, min(3.0, snr_db / 20.0))
                w *= snr_factor

            # Optionally weight by mode confidence (from propagation model)
            mode_conf = m.get('mode_confidence')
            if mode_conf is not None:
                w *= max(0.1, mode_conf)

            freqs.append(f)
            toas.append(t)
            weights.append(w)

        freqs = np.array(freqs)
        toas = np.array(toas)
        weights = np.array(weights)

        # Check for frequency diversity (prevent singular matrix if freq is same)
        if np.std(freqs) < 1000.0:  # Less than 1kHz spread
            logger.warning(f"Insufficient frequency diversity for {station}: {freqs}")
            return None

        # Iterative WLS with outlier rejection
        mask = np.ones(len(freqs), dtype=bool)
        n_rejected = 0
        rejection_reason = None

        for iteration in range(MAX_OUTLIER_ITERATIONS + 1):
            active = mask.sum()
            if active < 2:
                logger.warning(f"Too few measurements after outlier rejection for {station}")
                return None

            x = 1.0 / (freqs[mask] ** 2)
            y = toas[mask]
            w = weights[mask]

            result = self._fit_wls(x, y, w, freqs[mask], station)
            if result is None:
                return None

            m_slope, c_intercept, r2, rms_ms, y_pred = result

            # Outlier rejection: only if N > 2 and not last iteration
            # Use MAD (Median Absolute Deviation) instead of RMS for robustness:
            # a single outlier inflates RMS, making itself harder to detect.
            # MAD is resistant to up to 50% contamination.
            if active > 2 and iteration < MAX_OUTLIER_ITERATIONS:
                residuals = np.abs(y - y_pred)
                mad = float(np.median(residuals))
                # Convert MAD to sigma-equivalent: σ ≈ 1.4826 × MAD
                sigma_est = max(1e-15, mad * 1.4826)
                if sigma_est > 0:
                    outlier_mask_local = residuals > OUTLIER_SIGMA * sigma_est
                    if np.any(outlier_mask_local):
                        # Map back to full mask
                        active_indices = np.where(mask)[0]
                        for local_idx in np.where(outlier_mask_local)[0]:
                            global_idx = active_indices[local_idx]
                            mask[global_idx] = False
                            n_rejected += 1
                            f_rej = freqs[global_idx] / 1e6
                            logger.info(
                                f"TEC outlier rejected for {station}: "
                                f"{f_rej:.1f} MHz, residual={residuals[local_idx]*1000:.2f}ms "
                                f"(>{OUTLIER_SIGMA}σ={OUTLIER_SIGMA * sigma_est * 1000:.2f}ms)"
                            )
                        rejection_reason = 'outlier_3sigma'
                        continue  # Re-fit without outliers
            break  # No outliers found or last iteration

        # Negative slope means the TEC ESTIMATE is negative (lower freq appears
        # to arrive earlier than higher). True TEC is non-negative, but a
        # negative estimate is a normal noisy realisation — group-delay TEC is
        # below the noise floor for these stations. Per CR-2 (settled
        # 2026-05-17, see DATA_CONTRACT.md) the record is RETAINED, not
        # rejected: discarding on sign censors the estimator and biases every
        # downstream aggregate high. Confidence is forced to 0.0 below (via the
        # ``m_slope > 0`` guards), so the result is flagged, not trusted.
        if m_slope < 0:
            logger.warning(
                f"Negative TEC slope for {station}: m={m_slope:.2e} — "
                f"retaining with confidence 0 (noise-dominated: mode mixing or "
                f"sub-noise-floor signal). "
                f"Inputs: " + ", ".join(
                    [f"{f/1e6:.1f}MHz->{t*1000:.3f}ms"
                     for f, t in zip(freqs[mask], toas[mask])]
                )
            )

        tec = m_slope / K_IONOSPHERE
        tec_u = tec / TECU_SCALE
        t_vacuum_ms = c_intercept * 1000.0

        # Confidence metric
        n_active = mask.sum()
        if n_active <= 2:
            # N=2: zero residual DOF, R² is always 1.0 — cap confidence
            confidence = min(MAX_CONFIDENCE_N2, r2) if m_slope > 0 else 0.0
        else:
            confidence = max(0.0, min(1.0, r2)) if m_slope > 0 else 0.0

        # Penalize confidence if many outliers were rejected
        if n_rejected > 0:
            confidence *= max(0.5, 1.0 - 0.1 * n_rejected)

        group_delays_ms = {}
        for f_hz in freqs[mask]:
            delay_sec = m_slope / (f_hz ** 2)
            f_mhz = f_hz / 1e6
            group_delays_ms[f_mhz] = delay_sec * 1000.0

        # DIAGNOSTIC: Log near-zero or suspiciously perfect results
        if (abs(tec_u) < 0.1 and m_slope > 0) or confidence > 0.999:
            log_level = logging.DEBUG
            if abs(tec_u) < 0.01:
                log_level = logging.WARNING
            logger.log(
                log_level,
                f"Suspicious TEC for {station}: {tec_u:.2f} TECU (conf={confidence:.4f})\n"
                f"  Inputs: " + ", ".join(
                    [f"{f/1e6:.1f}MHz->{t*1000:.3f}ms"
                     for f, t in zip(freqs[mask], toas[mask])]
                )
            )

        return TECResult(
            station=station,
            timestamp=timestamp,
            tec_electrons_m2=tec,
            tec_u=tec_u,
            t_vacuum_error_ms=t_vacuum_ms,
            group_delay_ms=group_delays_ms,
            confidence=confidence,
            residuals_ms=rms_ms,
            n_frequencies=n_active,
            n_rejected=n_rejected,
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def _fit_wls(x, y, weights, freqs, station):
        """
        Weighted least squares fit: y = m*x + c.

        Returns:
            (slope, intercept, r2, rms_residual_ms, y_predicted) or None on failure.
        """
        try:
            poly_weights = np.sqrt(weights)

            if len(x) > 2:
                p, cov = np.polyfit(x, y, 1, w=poly_weights, cov=True)
            else:
                p = np.polyfit(x, y, 1, w=poly_weights, cov=False)

            m = p[0]
            c = p[1]

            y_pred = m * x + c
            ss_res = np.sum(weights * (y - y_pred) ** 2)
            ss_tot = np.sum(weights * (y - np.average(y, weights=weights)) ** 2)

            if ss_tot < 1e-20:
                r2 = 0.0
            else:
                r2 = max(0.0, min(1.0, 1.0 - (ss_res / ss_tot)))

            rms_residual_ms = float(np.sqrt(np.mean((y - y_pred) ** 2))) * 1000.0

            return m, c, r2, rms_residual_ms, y_pred

        except Exception as e:
            logger.error(f"TEC WLS fit failed for {station}: {e}")
            return None

