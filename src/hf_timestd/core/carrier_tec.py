#!/usr/bin/env python3
"""
Carrier-Phase Differential TEC (dTEC) Estimator
================================================================================
Derives relative TEC(t) directly from carrier phase and anchors it to an
absolute reference (GNSS VTEC).

Physics:
--------
Carrier phase measures the PHASE path (integral of refractive index n_φ):
    φ(t) = (2π f / c) ∫ n_φ ds

For the ionosphere, n_φ = 1 - f_p²/(2f²), so:
    φ_iono(t) = -(2π / c) · (40.3 / f) · sTEC(t)

Relative TEC therefore follows DIRECTLY from the unwrapped phase — no
differentiation or re-integration:
    ΔsTEC(t) = sTEC(t) - sTEC(t₀) = -(c · f) / (2π · 40.3) · (φ(t) - φ(t₀))

(P-M3: an earlier version went phase → Doppler → re-integrate, which
amplified noise and introduced a half-sample lag. The direct conversion
above avoids both.)

Note the OPPOSITE sign convention from group delay: increasing TEC causes
increasing group delay but DECREASING phase delay (phase advance).

Carrier phase gives ~1000× better temporal resolution than group delay because:
- Group delay: 1 measurement per minute per frequency (minute marker)
- Carrier phase: ~55 measurements per minute per frequency (per-tick)

But carrier phase is AMBIGUOUS — it gives only dTEC, not absolute TEC.
We anchor the relative dTEC to absolute TEC from GNSS VTEC (an independent
absolute measurement) at minute boundaries.

Integration with existing pipeline:
------------------------------------
Reads: L2/tick_phase HDF5 (carrier_phase_rad, ~55 points/min/station)
Writes: L3/dtec HDF5 (dTEC time series, anchored to GNSS VTEC)

================================================================================
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

import numpy as np

from hamsci_dsp.propagation import dtec_from_phase as _dtec_from_phase

# §4.4 Low: previously imported `math`, `datetime`, `timezone`, and
# `Tuple` — none of which were referenced.  Removed.

logger = logging.getLogger(__name__)

# The phase→dTEC kernel (unwrap → coast across cycle slips / gaps → direct
# ΔsTEC = -(c·f)/(2π·K)·(φ-φ₀)), and its constants (c, K=40.3, TECU=1e16,
# GAP_THRESHOLD_S=120, the >5 Hz/s cycle-slip gate) now live in
# hamsci_dsp.propagation.carrier — the canonical shared home (extracted
# math-identical from here, P-M3). This class wraps that kernel with
# hf-timestd's sorting/dedup, anchoring to absolute TEC, the noise-floor
# estimate, and the richer per-channel result below.


@dataclass
class CarrierTECResult:
    """Result of carrier-phase dTEC estimation for one channel."""
    station: str
    channel: str
    frequency_mhz: float
    start_epoch: float
    end_epoch: float

    # Time series — all on the carrier-phase SAMPLE grid (P-M3); the three
    # arrays are the same length and index-aligned.
    epochs: List[float] = field(default_factory=list)
    dtec_tecu: List[float] = field(default_factory=list)  # Relative TEC, direct from phase
    dtec_rate_tecu_per_s: List[float] = field(default_factory=list)  # d(dTEC)/dt

    # Anchoring
    anchor_tec_tecu: float = 0.0       # Absolute TEC reference at the anchor point
    anchor_epoch: float = 0.0           # When the anchor was applied
    is_anchored: bool = False
    anchor_uncertainty_tecu: float = 0.0  # 1σ of the anchor TEC (P-M5);
                                          # absolute σ = hypot(sigma_dtec, this)

    # Quality
    n_points: int = 0
    sigma_dtec_tecu: float = 0.0       # 1σ per-sample dTEC noise (direct from
                                       # phase, P-M3 — constant, NOT √N growth);
                                       # NaN if it cannot be estimated
    mean_snr_db: float = 0.0
    unwrap_quality: float = 1.0        # unwrap-RISK score (not proof); see compute_dtec
    n_phase_jumps: int = 0             # inter-sample steps near the π unwrap boundary
    n_cycle_slips: int = 0             # detected cycle slips (phase-rate spikes)
    n_gaps: int = 0                    # data gaps the series coasted across (P-M3)


class CarrierTECEstimator:
    """
    Estimates differential TEC (dTEC) from carrier phase measurements.

    Pipeline:
    1. Read carrier_phase_rad time series from L2/tick_phase
    2. Unwrap phase for continuity
    3. Convert unwrapped phase DIRECTLY to relative TEC(t) (P-M3)
    4. Coast flat across detected cycle slips and data gaps
    5. Anchor to absolute TEC from GNSS VTEC at minute boundaries

    The result is a high-temporal-resolution TEC time series with sub-TECU
    precision, anchored to the absolute scale from GNSS VTEC.
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
        anchor_uncertainty_tecu: Optional[float] = None,
        anchor_max_age_seconds: float = 90.0,
    ) -> Optional[CarrierTECResult]:
        """
        Compute dTEC from a carrier phase time series.

        Args:
            epochs: UTC epoch timestamps (seconds)
            carrier_phase_rad: Carrier phase measurements (radians)
            frequency_mhz: Carrier frequency in MHz
            station: Station name
            channel: Channel name
            anchor_tec_tecu: Absolute TEC to anchor to. Per PHYSICS_CONTRACT
                this should be GNSS VTEC — an independent absolute
                measurement — not the (noisy) group-delay TEC.
            anchor_epoch: Epoch of the anchor point.
            anchor_uncertainty_tecu: 1σ uncertainty of the anchor TEC; stored
                on the result so the absolute-TEC uncertainty can be formed
                as hypot(sigma_dtec_tecu, anchor_uncertainty_tecu) (P-M5).
            anchor_max_age_seconds: The anchor is applied only if a sample
                lies within this many seconds of anchor_epoch; otherwise the
                series is left unanchored rather than offset to a far-away,
                meaningless sample (P-M5).

        Returns:
            CarrierTECResult with dTEC time series, or None if insufficient data
        """
        if len(epochs) < 3:
            return None

        # §4.4 Low: guard against non-positive frequency.  The 1/f²
        # iono-delay rescaling at the end of this method would divide
        # by zero (or by a meaningless negative).
        if not (frequency_mhz > 0 and np.isfinite(frequency_mhz)):
            logger.warning(
                f"carrier_tec: invalid frequency_mhz={frequency_mhz!r} "
                f"for {station}/{channel}; skipping"
            )
            return None

        # Sort by time
        sort_idx = np.argsort(epochs)
        epochs = np.asarray(epochs, dtype=float)[sort_idx]
        carrier_phase_rad = np.asarray(carrier_phase_rad, dtype=float)[sort_idx]

        # Drop exact-duplicate timestamps — a strictly increasing grid keeps
        # the rate derivative and gap detection well-defined.
        keep = np.concatenate(([True], np.diff(epochs) > 0))
        epochs = epochs[keep]
        carrier_phase_rad = carrier_phase_rad[keep]
        if len(epochs) < 3:
            return None

        # Delegate the phase→dTEC kernel to the shared hamsci_dsp
        # implementation (P-M3: unwrap → coast across cycle slips / long gaps →
        # direct ΔsTEC = -(c·f)/(2π·K)·(φ-φ₀), with the >5 Hz/s slip gate and
        # the 120 s gap threshold). It returns the dTEC series, its derivative,
        # the cycle-slip / gap counts, and the unwrap-RISK score + raw jump
        # count (P-H3 — a marginal-cadence risk indicator, NOT proof of correct
        # unwrapping). hf-timestd layers sorting/dedup (above) and anchoring +
        # noise-floor (below) around this kernel.
        core = _dtec_from_phase(epochs, carrier_phase_rad, frequency_mhz)
        if core is None:
            return None

        dtec_tecu = np.asarray(core.dtec_tecu, dtype=float)
        dtec_rate_tecu_per_s = np.asarray(core.dtec_rate_tecu_per_s, dtype=float)
        unwrap_quality = core.unwrap_quality
        n_jumps = core.n_phase_jumps
        n_cycle_slips = core.n_cycle_slips
        n_gaps = core.n_gaps

        if n_jumps > 0:
            logger.debug(
                f"Phase unwrap quality: {station}/{channel} "
                f"{frequency_mhz:.2f} MHz — {n_jumps} steps |Δφ|>π/2, "
                f"quality={unwrap_quality:.2f}"
            )
        if n_cycle_slips > 0:
            logger.debug(
                f"Coasted {n_cycle_slips} cycle slip(s) for "
                f"{station}/{channel} at {frequency_mhz}MHz"
            )

        # Anchor the relative dTEC to an absolute reference (P-M5). The anchor
        # is applied only if a sample lies within anchor_max_age_seconds of
        # the anchor epoch — argmin alone would always find *a* nearest
        # sample, even one hours away, and offset the whole series to it.
        is_anchored = False
        anchor_tec = 0.0
        anchor_ep = 0.0
        anchor_u = 0.0
        if anchor_tec_tecu is not None and anchor_epoch is not None:
            age = np.abs(epochs - anchor_epoch)
            anchor_idx = int(np.argmin(age))
            if age[anchor_idx] <= anchor_max_age_seconds:
                offset = anchor_tec_tecu - dtec_tecu[anchor_idx]
                dtec_tecu = dtec_tecu + offset
                is_anchored = True
                anchor_tec = float(anchor_tec_tecu)
                anchor_ep = float(anchor_epoch)
                anchor_u = float(anchor_uncertainty_tecu
                                 if anchor_uncertainty_tecu is not None
                                 else 0.0)
            else:
                logger.warning(
                    f"dTEC anchor for {station}/{channel} rejected: nearest "
                    f"sample is {age[anchor_idx]:.0f}s from the anchor epoch "
                    f"(> {anchor_max_age_seconds}s) — leaving dTEC unanchored"
                )

        # Uncertainty of the dTEC series. With P-M3's direct-from-phase
        # conversion the series is no longer a re-integrated random walk, so
        # its 1σ does NOT grow as √N — each sample is φ(t)−φ(t₀) scaled, whose
        # noise is the (constant) per-sample phase noise. _estimate_noise_floor
        # measures exactly that detrended per-sample scatter, and returns NaN
        # (never 0.0) when it cannot be estimated, so an unknown noise floor
        # stays honestly unknown (P-H7).
        sigma = self._estimate_noise_floor(epochs, dtec_tecu)

        return CarrierTECResult(
            station=station,
            channel=channel,
            frequency_mhz=frequency_mhz,
            start_epoch=float(epochs[0]),
            end_epoch=float(epochs[-1]),
            epochs=epochs.tolist(),
            dtec_tecu=dtec_tecu.tolist(),
            dtec_rate_tecu_per_s=dtec_rate_tecu_per_s.tolist(),
            anchor_tec_tecu=anchor_tec,
            anchor_epoch=anchor_ep,
            is_anchored=is_anchored,
            anchor_uncertainty_tecu=anchor_u,
            n_points=len(epochs),
            sigma_dtec_tecu=sigma,
            mean_snr_db=0.0,  # Caller should set this
            unwrap_quality=unwrap_quality,
            n_phase_jumps=n_jumps,
            n_cycle_slips=n_cycle_slips,
            n_gaps=n_gaps,
        )

    def compute_dtec_from_records(
        self,
        records: List[Dict[str, Any]],
        frequency_mhz: float,
        station: str = '',
        channel: str = '',
        anchor_tec_tecu: Optional[float] = None,
        anchor_epoch: Optional[float] = None,
        anchor_uncertainty_tecu: Optional[float] = None,
    ) -> Optional[CarrierTECResult]:
        """
        Convenience method: compute dTEC from a list of tick_phase records.

        Args:
            records: List of dicts with 'utc_epoch' and 'carrier_phase_rad' keys
            frequency_mhz: Carrier frequency in MHz
            station, channel: Identifiers
            anchor_tec_tecu, anchor_epoch: Optional absolute TEC anchor
            anchor_uncertainty_tecu: Optional 1σ of the anchor TEC (P-M5)

        Returns:
            CarrierTECResult or None
        """
        if len(records) < 3:
            return None

        epochs = np.array([r['utc_epoch'] for r in records])
        phases = np.array([r.get('carrier_phase_rad', 0.0) for r in records])
        snrs = np.array([r.get('snr_db', 0.0) for r in records])

        # Reject records with no real phase measurement.  The producing
        # code (tick_edge_detector) initialises `carrier_phase_rad` to
        # 0.0 and overwrites it only when the IQ phase extraction
        # succeeds, so == 0.0 is treated as "no measurement".
        #
        # Known limitation (§4.4 Low): a *genuine* phase measurement
        # that lands on exactly 0.0 rad (real-positive complex carrier)
        # is therefore dropped along with missing/failed measurements.
        # The probability of any float64 phase being exactly 0.0 is
        # negligibly small (~1e-15 for a uniform distribution over
        # (-π, π]); accepting that as the cost of disambiguating
        # missing-vs-zero without a separate quality flag in the
        # producing schema.
        nonzero_mask = phases != 0.0
        if np.sum(nonzero_mask) < 3:
            return None
        epochs = epochs[nonzero_mask]
        phases = phases[nonzero_mask]
        snrs = snrs[nonzero_mask]

        result = self.compute_dtec_from_phase(
            epochs=epochs,
            carrier_phase_rad=phases,
            frequency_mhz=frequency_mhz,
            station=station,
            channel=channel,
            anchor_tec_tecu=anchor_tec_tecu,
            anchor_epoch=anchor_epoch,
            anchor_uncertainty_tecu=anchor_uncertainty_tecu,
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

        Each input dTEC series is RELATIVE — anchored, if at all, to its own
        reference — so each carries an arbitrary constant offset. The two
        series are therefore mean-removed over the common window before
        differencing (P-M4); the result is the difference in dTEC *variation*,
        which is the physical quantity. For a consistent ionosphere it stays
        near zero (both frequencies see the same TEC); deviations indicate
        mode changes or scintillation.

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

        # P-M4: mean-remove each series over the common window before
        # differencing. Each is a RELATIVE dTEC with its own arbitrary offset;
        # differencing them raw would report that offset difference as a
        # physical dispersive signal. After mean-removal the difference
        # reflects only the dTEC variation, which is the physical quantity.
        interp_1 = interp_1 - np.mean(interp_1)
        interp_2 = interp_2 - np.mean(interp_2)

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
        Estimate the per-tick dTEC noise floor from detrended short windows.

        Uses the median absolute deviation of detrended segments as a robust
        noise estimator. Returns NaN — not 0.0 — when the noise floor cannot
        be estimated (too few points, non-positive cadence, no usable
        windows): an unknown noise floor must read as unknown, not perfect
        (P-H7). With P-M3's direct-from-phase dTEC this per-sample scatter is
        the dTEC uncertainty itself (the series is not a re-integrated random
        walk, so there is no √N growth to propagate).
        """
        if len(epochs) < 10:
            return float('nan')

        median_dt = float(np.median(np.diff(epochs)))
        if median_dt <= 0:
            return float('nan')

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
            return float('nan')

        # Convert MAD to sigma: sigma ≈ 1.4826 × MAD
        return float(np.median(mad_values)) * 1.4826
