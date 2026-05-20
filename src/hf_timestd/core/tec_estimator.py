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

Operational status — NOT an operational product:
-------------------------------------------------
Group-delay TEC across the WWV/WWVH/CHU/BPM frequency spans is at or below the
timing noise floor. The 1/f² delay difference between, say, 5 and 15 MHz for a
realistic TEC is comparable to — or smaller than — the per-measurement timing
uncertainty, so the fitted slope is rarely detectable above noise. ``tec_u`` is
therefore a caveated, research-grade estimate, NOT an operational deliverable.
``confidence`` reports slope detectability (slope / σ_slope) and is honestly ~0
for most epochs; ``tec_uncertainty_tecu`` carries the 1σ slope-derived
uncertainty. Per PHYSICS_CONTRACT §1/§4 and METROLOGY_PHYSICS_SPLIT, claiming
group-delay TEC is operational is a contract failure condition — consumers
must treat ``tec_u`` as advisory and gate on ``confidence``.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)

# Physical constants
# K ≈ 40.3 m³/s² (standard ionospheric constant for group delay in meters)
# For measurements in seconds, we divide by c
C_LIGHT = 299792458.0
K_IONOSPHERE = 40.3 / C_LIGHT  # seconds * Hz² * m² / electrons
TECU_SCALE = 1e16              # 1 TECU = 10^16 el/m²

OUTLIER_SIGMA = 3.0            # Reject measurements beyond this many σ
MAX_OUTLIER_ITERATIONS = 3     # Maximum outlier rejection passes

# HF-band sanity bounds for input frequencies (P-H6): covers WWV 2.5 MHz
# through 25 MHz with margin; rejects 0 / NaN / Inf and absurd values before
# they reach the 1/f² term.
HF_BAND_MIN_HZ = 1.0e6
HF_BAND_MAX_HZ = 60.0e6


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
    confidence: float               # 0.0 - 1.0  (slope detectability, slope/σ)
    tec_uncertainty_tecu: float     # 1σ uncertainty of tec_u (from slope σ)
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
        uncerts = []  # raw timing uncertainty (seconds) — outlier-σ floor (P-M1)

        for m in measurements:
            f = m['frequency_hz']
            # P-H6: reject a non-finite or out-of-band frequency before it
            # reaches the 1/f² term — a 0 / NaN / Inf frequency would poison
            # the whole polyfit.
            if not (np.isfinite(f) and HF_BAND_MIN_HZ <= f <= HF_BAND_MAX_HZ):
                logger.warning(
                    f"{station}: skipping measurement with invalid "
                    f"frequency_hz={f!r} (must be finite and within "
                    f"{HF_BAND_MIN_HZ/1e6:.0f}-{HF_BAND_MAX_HZ/1e6:.0f} MHz)"
                )
                continue
            t = m['toa_ms'] / 1000.0  # Convert ms to seconds
            u_ms = m.get('uncertainty_ms', 1.0)

            # Weight = inverse variance from timing uncertainty
            w = 1.0 / (max(u_ms, 0.1) ** 2)

            # Optionally boost weight by SNR (higher SNR = more reliable).
            #
            # §4.4 Low: spelling out the linear SNR weighting formula
            # that was previously a single-line comment.  The weight
            # factor is ``snr_db / 20``, *clamped* into [0.5, 3.0]:
            #
            #     SNR (dB) :   0    10    20    40    60+
            #     factor   :  0.5  0.5   1.0   2.0   3.0  (clamped)
            #
            # The choice is heuristic rather than from first principles:
            # 20 dB is the contract's "≥ 10 dB SNR" target plus a
            # ~10 dB margin, and is treated as the "reference quality"
            # weight of 1.0.  The 0.5 floor keeps very low-SNR samples
            # from being dropped entirely (they still contribute, just
            # less); the 3.0 cap prevents one very-high-SNR sample from
            # dominating the WLS fit at the expense of geometric
            # diversity across frequencies.  Both clamps are wider than
            # the per-measurement uncertainty term (``1/u_ms²``) on
            # purpose -- the uncertainty term carries the formal
            # statistical weight and this is the soft "trustworthiness"
            # multiplier on top.
            snr_db = m.get('snr_db')
            if snr_db is not None and snr_db > 0:
                snr_factor = max(0.5, min(3.0, snr_db / 20.0))
                w *= snr_factor

            # Optionally weight by mode confidence (from propagation model)
            mode_conf = m.get('mode_confidence')
            if mode_conf is not None:
                w *= max(0.1, mode_conf)

            freqs.append(f)
            toas.append(t)
            weights.append(w)
            uncerts.append(max(u_ms, 0.1) / 1000.0)

        # P-H6: too few measurements survived frequency validation.
        if len(freqs) < 2:
            logger.warning(
                f"{station}: fewer than 2 valid-frequency measurements"
            )
            return None

        freqs = np.array(freqs)
        toas = np.array(toas)
        weights = np.array(weights)
        uncerts = np.array(uncerts)

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

            m_slope, c_intercept, sigma_slope, rms_ms, y_pred = result

            # Outlier rejection (P-M1): only with N ≥ 4 and not on the last
            # iteration. At N ≤ 3 a 2-parameter line fit leaves ≤ 1 residual
            # DOF, so MAD cannot tell a real outlier from ordinary scatter —
            # and rejecting there can collapse the fit below 2 points. We drop
            # at most ONE point (the worst) per pass, then re-fit, so a single
            # bad measurement cannot drag a good one out with it. MAD's
            # 50%-breakdown property is a distribution-estimation result; for
            # a handful of regression residuals it is only a robust scatter
            # estimate, not contamination-proof.
            if active > 3 and iteration < MAX_OUTLIER_ITERATIONS:
                residuals = np.abs(y - y_pred)
                mad = float(np.median(residuals))
                # σ ≈ 1.4826 × MAD, floored against the measurement noise:
                # residual scatter below the timing uncertainty is not
                # evidence of an outlier, so a lucky near-zero MAD must not
                # make every point look discrepant.
                u_floor = float(np.median(uncerts[mask]))
                sigma_est = max(mad * 1.4826, u_floor)
                if sigma_est > 0:
                    threshold = OUTLIER_SIGMA * sigma_est
                    worst_local = int(np.argmax(residuals))
                    if residuals[worst_local] > threshold:
                        active_indices = np.where(mask)[0]
                        global_idx = active_indices[worst_local]
                        mask[global_idx] = False
                        n_rejected += 1
                        f_rej = freqs[global_idx] / 1e6
                        logger.info(
                            f"TEC outlier rejected for {station}: "
                            f"{f_rej:.1f} MHz, "
                            f"residual={residuals[worst_local]*1000:.2f}ms "
                            f"(>{OUTLIER_SIGMA}σ={threshold*1000:.2f}ms)"
                        )
                        rejection_reason = 'outlier_3sigma'
                        continue  # Re-fit without the worst outlier
            break  # No outlier found or last iteration

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
        n_active = int(mask.sum())

        # 1σ uncertainty of the TEC estimate, propagated from the slope σ.
        tec_uncertainty_tecu = (
            sigma_slope / K_IONOSPHERE / TECU_SCALE
            if np.isfinite(sigma_slope) else float('nan')
        )

        # Confidence = slope detectability (P-H2):
        #   confidence = 1 - σ_slope/slope,  clamped to [0, 1]
        # → ~0 once σ_slope approaches or exceeds the slope. r² is NOT used —
        # it measures fit-to-a-line, which sits near 1 even when the slope is
        # pure noise. Group-delay TEC for these stations is at/below the noise
        # floor, so this is honestly ~0 for most epochs (see module docstring).
        if m_slope > 0 and np.isfinite(sigma_slope):
            if sigma_slope <= 0.0:
                confidence = 1.0  # zero residual scatter — slope fully determined
            else:
                confidence = max(0.0, min(1.0, 1.0 - sigma_slope / m_slope))
        else:
            confidence = 0.0  # negative/zero slope, or σ_slope undetermined

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
            tec_uncertainty_tecu=tec_uncertainty_tecu,
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
            (slope, intercept, sigma_slope, rms_residual_ms, y_predicted)
            or None on failure. sigma_slope is the 1σ uncertainty of the
            fitted slope — from the polyfit covariance for N>2, propagated
            analytically from the two points' variances for N=2.
        """
        try:
            poly_weights = np.sqrt(weights)

            if len(x) > 2:
                p, cov = np.polyfit(x, y, 1, w=poly_weights, cov=True)
                sigma_slope = float(np.sqrt(abs(cov[0, 0])))
            else:
                p = np.polyfit(x, y, 1, w=poly_weights, cov=False)
                # N=2: an exact fit with zero residual DOF — the slope
                # uncertainty comes from the two measurements' variances
                # (1/weight), not from (non-existent) residual scatter.
                dx = abs(x[1] - x[0])
                if dx > 0:
                    var_sum = 1.0 / weights[0] + 1.0 / weights[1]
                    sigma_slope = float(np.sqrt(var_sum) / dx)
                else:
                    sigma_slope = float('inf')

            m = p[0]
            c = p[1]
            y_pred = m * x + c
            rms_residual_ms = float(np.sqrt(np.mean((y - y_pred) ** 2))) * 1000.0

            return m, c, sigma_slope, rms_residual_ms, y_pred

        except (np.linalg.LinAlgError, ValueError) as e:
            # The fit genuinely could not be done (singular system, NaN/Inf
            # input, degenerate shapes). P-M2: catch only these — a bare
            # `except Exception` here would swallow real bugs (typos,
            # attribute errors) as a silent None.
            logger.error(f"TEC WLS fit failed for {station}: {e}")
            return None

