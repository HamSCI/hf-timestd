#!/usr/bin/env python3
"""
Carrier-Phase Differential TEC (dTEC) Estimator
================================================================================
Converts carrier phase rate-of-change (Doppler) to dTEC/dt, integrates to get
relative TEC(t), and anchors to absolute TEC from group-delay estimates.

Physics:
--------
Carrier phase measures the PHASE path (integral of refractive index n_φ):
    φ(t) = (2π f / c) ∫ n_φ ds

For the ionosphere, n_φ = 1 - f_p²/(2f²), so:
    φ_iono(t) = -(2π / c) · (40.3 / f) · sTEC(t)

The Doppler shift from changing TEC is:
    f_D = dφ/dt / (2π) = -(40.3 / (c · f)) · d(sTEC)/dt

Rearranging:
    d(sTEC)/dt = -f_D · c · f / 40.3

Note the OPPOSITE sign convention from group delay: increasing TEC causes
increasing group delay but DECREASING phase delay (phase advance).

Carrier phase gives ~1000× better temporal resolution than group delay because:
- Group delay: 1 measurement per minute per frequency (minute marker)
- Carrier phase: ~55 measurements per minute per frequency (per-tick)

But carrier phase is AMBIGUOUS — it gives only dTEC, not absolute TEC.
We anchor the integrated dTEC to the group-delay absolute TEC at minute
boundaries.

Integration with existing pipeline:
------------------------------------
Reads: L2/tick_phase HDF5 (carrier_phase_rad, ~55 points/min/station)
Writes: L3/dtec HDF5 (dTEC time series, anchored to group-delay TEC)

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
C_LIGHT = 299792458.0       # m/s
K_GROUP_DELAY = 40.3        # m³/s² (ionospheric constant)
TECU_SCALE = 1e16           # 1 TECU = 10^16 el/m²


@dataclass
class CarrierTECResult:
    """Result of carrier-phase dTEC estimation for one channel."""
    station: str
    channel: str
    frequency_mhz: float
    start_epoch: float
    end_epoch: float

    # Time series
    epochs: List[float] = field(default_factory=list)
    dtec_tecu: List[float] = field(default_factory=list)  # Relative TEC (integrated dTEC)
    dtec_rate_tecu_per_s: List[float] = field(default_factory=list)  # dTEC/dt

    # Anchoring
    anchor_tec_tecu: float = 0.0       # Absolute TEC from group delay at anchor point
    anchor_epoch: float = 0.0           # When the anchor was applied
    is_anchored: bool = False

    # Quality
    n_points: int = 0
    sigma_dtec_tecu: float = 0.0       # Noise floor estimate
    mean_snr_db: float = 0.0
    unwrap_quality: float = 1.0        # 1.0 = clean, <1.0 = ambiguous unwrapping
    n_phase_jumps: int = 0             # Number of inter-sample |Δφ| > π/2 steps


class CarrierTECEstimator:
    """
    Estimates differential TEC (dTEC) from carrier phase measurements.

    Pipeline:
    1. Read carrier_phase_rad time series from L2/tick_phase
    2. Unwrap phase for continuity
    3. Compute phase rate (Doppler) via finite differences
    4. Convert Doppler → dTEC/dt using frequency
    5. Integrate dTEC/dt → relative TEC(t)
    6. Anchor to absolute TEC from group-delay estimator at minute boundaries

    The result is a high-temporal-resolution TEC time series with sub-TECU
    precision, anchored to the absolute scale from group delay.
    """

    def __init__(self, data_root: Optional[Path] = None):
        self.data_root = Path(data_root) if data_root else None

    def compute_dtec_from_phase(
        self,
        epochs: np.ndarray,
        carrier_phase_rad: np.ndarray,
        frequency_mhz: float,
        station: str = '',
        channel: str = '',
        anchor_tec_tecu: Optional[float] = None,
        anchor_epoch: Optional[float] = None,
    ) -> Optional[CarrierTECResult]:
        """
        Compute dTEC from a carrier phase time series.

        Args:
            epochs: UTC epoch timestamps (seconds)
            carrier_phase_rad: Carrier phase measurements (radians)
            frequency_mhz: Carrier frequency in MHz
            station: Station name
            channel: Channel name
            anchor_tec_tecu: Absolute TEC to anchor to (from group delay)
            anchor_epoch: Epoch of the anchor point

        Returns:
            CarrierTECResult with dTEC time series, or None if insufficient data
        """
        if len(epochs) < 3:
            return None

        # Sort by time
        sort_idx = np.argsort(epochs)
        epochs = epochs[sort_idx]
        carrier_phase_rad = carrier_phase_rad[sort_idx]

        # Unwrap phase for continuity
        phase_unwrapped = np.unwrap(carrier_phase_rad)

        # P3-A: Phase unwrapping quality check.
        # If any inter-sample raw phase step |Δφ_raw| > π/2, np.unwrap may have
        # chosen the wrong 2π branch.  Count such steps and compute a quality
        # score.  Caller can gate on unwrap_quality < threshold.
        dphi_raw = np.diff(carrier_phase_rad)
        # Wrap raw differences to (-π, π] to measure the true step size
        dphi_raw_wrapped = (dphi_raw + np.pi) % (2 * np.pi) - np.pi
        n_jumps = int(np.sum(np.abs(dphi_raw_wrapped) > (np.pi / 2)))
        unwrap_quality = max(0.0, 1.0 - n_jumps / max(len(dphi_raw_wrapped), 1))
        if n_jumps > 0:
            logger.debug(
                f"Phase unwrap quality: {station}/{channel} "
                f"{frequency_mhz:.2f} MHz — {n_jumps}/{len(dphi_raw_wrapped)} "
                f"steps |Δφ|>π/2, quality={unwrap_quality:.2f}"
            )

        # Compute phase rate via finite differences (central where possible)
        dt = np.diff(epochs)
        dphi = np.diff(phase_unwrapped)

        # Filter out zero or negative dt (duplicate timestamps)
        valid = dt > 0
        if not np.any(valid):
            return None

        # Doppler: f_D = -(1/2π) · dφ/dt
        doppler_hz = np.zeros(len(dt))
        doppler_hz[valid] = -(1.0 / (2.0 * np.pi)) * dphi[valid] / dt[valid]

        # P3-A: Cycle-Slip Detection
        # Deep fades cause cycle slips, which manifest as massive, brief spikes in phase rate (Doppler).
        # We detect these by looking at the second derivative (phase acceleration).
        d2phi = np.zeros_like(doppler_hz)
        d2phi[1:] = np.diff(doppler_hz)
        
        # Threshold: > 5 Hz/s acceleration is almost certainly a cycle slip for ionospheric HF
        slip_mask = np.abs(d2phi) > 5.0
        if np.any(slip_mask):
            logger.debug(f"Detected {np.sum(slip_mask)} cycle slips for {station}/{channel} at {frequency_mhz}MHz")
            doppler_hz[slip_mask] = 0.0 # Freeze dTEC rate during the slip

        # Midpoint epochs for the derivative
        mid_epochs = (epochs[:-1] + epochs[1:]) / 2.0

        # Convert Doppler to dTEC/dt
        # d(sTEC)/dt = -f_D · c · f / 40.3
        # where f is in Hz, result in el/m²/s
        freq_hz = frequency_mhz * 1e6
        dtec_rate_el_m2_per_s = -doppler_hz * C_LIGHT * freq_hz / K_GROUP_DELAY
        dtec_rate_tecu_per_s = dtec_rate_el_m2_per_s / TECU_SCALE

        # Integrate dTEC/dt to get relative TEC(t)
        # Use trapezoidal integration
        dtec_tecu = np.zeros(len(mid_epochs))
        for i in range(1, len(mid_epochs)):
            dt_i = mid_epochs[i] - mid_epochs[i - 1]
            if dt_i > 0 and dt_i < 120:  # Skip gaps > 2 minutes
                avg_rate = (dtec_rate_tecu_per_s[i] + dtec_rate_tecu_per_s[i - 1]) / 2.0
                dtec_tecu[i] = dtec_tecu[i - 1] + avg_rate * dt_i
            else:
                dtec_tecu[i] = dtec_tecu[i - 1]  # Hold through gaps

        # Anchor to absolute TEC if provided
        is_anchored = False
        anchor_tec = 0.0
        anchor_ep = 0.0
        if anchor_tec_tecu is not None and anchor_epoch is not None:
            # Find the closest point to the anchor epoch
            anchor_idx = np.argmin(np.abs(mid_epochs - anchor_epoch))
            offset = anchor_tec_tecu - dtec_tecu[anchor_idx]
            dtec_tecu += offset
            is_anchored = True
            anchor_tec = anchor_tec_tecu
            anchor_ep = anchor_epoch

        # Noise floor estimate: std of detrended dTEC over short windows
        sigma = self._estimate_noise_floor(mid_epochs, dtec_tecu)

        return CarrierTECResult(
            station=station,
            channel=channel,
            frequency_mhz=frequency_mhz,
            start_epoch=float(mid_epochs[0]),
            end_epoch=float(mid_epochs[-1]),
            epochs=mid_epochs.tolist(),
            dtec_tecu=dtec_tecu.tolist(),
            dtec_rate_tecu_per_s=dtec_rate_tecu_per_s.tolist(),
            anchor_tec_tecu=anchor_tec,
            anchor_epoch=anchor_ep,
            is_anchored=is_anchored,
            n_points=len(mid_epochs),
            sigma_dtec_tecu=sigma,
            mean_snr_db=0.0,  # Caller should set this
            unwrap_quality=unwrap_quality,
            n_phase_jumps=n_jumps,
        )

    def compute_dtec_from_records(
        self,
        records: List[Dict[str, Any]],
        frequency_mhz: float,
        station: str = '',
        channel: str = '',
        anchor_tec_tecu: Optional[float] = None,
        anchor_epoch: Optional[float] = None,
    ) -> Optional[CarrierTECResult]:
        """
        Convenience method: compute dTEC from a list of tick_phase records.

        Args:
            records: List of dicts with 'utc_epoch' and 'carrier_phase_rad' keys
            frequency_mhz: Carrier frequency in MHz
            station, channel: Identifiers
            anchor_tec_tecu, anchor_epoch: Optional absolute TEC anchor

        Returns:
            CarrierTECResult or None
        """
        if len(records) < 3:
            return None

        epochs = np.array([r['utc_epoch'] for r in records])
        phases = np.array([r.get('carrier_phase_rad', 0.0) for r in records])
        snrs = np.array([r.get('snr_db', 0.0) for r in records])

        result = self.compute_dtec_from_phase(
            epochs=epochs,
            carrier_phase_rad=phases,
            frequency_mhz=frequency_mhz,
            station=station,
            channel=channel,
            anchor_tec_tecu=anchor_tec_tecu,
            anchor_epoch=anchor_epoch,
        )

        if result is not None and len(snrs) > 0:
            result.mean_snr_db = float(np.mean(snrs[snrs > 0])) if np.any(snrs > 0) else 0.0

        return result

    def compute_differential_dtec(
        self,
        result_f1: CarrierTECResult,
        result_f2: CarrierTECResult,
    ) -> Optional[Dict[str, Any]]:
        """
        Compute differential dTEC between two frequencies for the same station.

        The differential removes common-mode errors (clock, geometry) and
        isolates the dispersive ionospheric component.

        dTEC_diff(t) = TEC_f1(t) - TEC_f2(t)

        For a consistent ionosphere, this should be near zero (both frequencies
        see the same TEC). Deviations indicate mode changes or scintillation.

        Args:
            result_f1: CarrierTECResult for frequency 1
            result_f2: CarrierTECResult for frequency 2

        Returns:
            Dict with differential dTEC time series, or None
        """
        if result_f1.n_points < 3 or result_f2.n_points < 3:
            return None

        # Interpolate to common time grid (use the sparser one)
        epochs_1 = np.array(result_f1.epochs)
        epochs_2 = np.array(result_f2.epochs)
        dtec_1 = np.array(result_f1.dtec_tecu)
        dtec_2 = np.array(result_f2.dtec_tecu)

        # Use the time range common to both
        t_start = max(epochs_1[0], epochs_2[0])
        t_end = min(epochs_1[-1], epochs_2[-1])

        if t_end <= t_start:
            return None

        # Interpolate both to a common grid (1-second spacing)
        n_points = int(t_end - t_start)
        if n_points < 3:
            return None

        common_epochs = np.linspace(t_start, t_end, min(n_points, 3600))
        interp_1 = np.interp(common_epochs, epochs_1, dtec_1)
        interp_2 = np.interp(common_epochs, epochs_2, dtec_2)

        diff = interp_1 - interp_2

        return {
            'station': result_f1.station,
            'freq1_mhz': result_f1.frequency_mhz,
            'freq2_mhz': result_f2.frequency_mhz,
            'epochs': common_epochs.tolist(),
            'dtec_diff_tecu': diff.tolist(),
            'rms_diff_tecu': float(np.std(diff)),
            'n_points': len(common_epochs),
        }

    @staticmethod
    def _estimate_noise_floor(
        epochs: np.ndarray,
        dtec: np.ndarray,
        window_seconds: float = 60.0
    ) -> float:
        """
        Estimate dTEC noise floor from detrended short windows.

        Uses the median absolute deviation of detrended segments as a
        robust noise estimator.
        """
        if len(epochs) < 10:
            return 0.0

        median_dt = float(np.median(np.diff(epochs)))
        if median_dt <= 0:
            return 0.0

        window_samples = max(5, int(window_seconds / median_dt))
        mad_values = []

        for i in range(0, len(epochs) - window_samples + 1, max(1, window_samples // 2)):
            chunk = dtec[i:i + window_samples]
            t_chunk = epochs[i:i + window_samples]

            if len(chunk) < 5:
                continue

            # Detrend: remove linear fit
            coeffs = np.polyfit(t_chunk - t_chunk[0], chunk, 1)
            trend = np.polyval(coeffs, t_chunk - t_chunk[0])
            detrended = chunk - trend

            # MAD (robust std estimate)
            mad = float(np.median(np.abs(detrended - np.median(detrended))))
            mad_values.append(mad)

        if not mad_values:
            return 0.0

        # Convert MAD to sigma: sigma ≈ 1.4826 × MAD
        return float(np.median(mad_values)) * 1.4826
