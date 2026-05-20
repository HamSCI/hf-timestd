#!/usr/bin/env python3
"""
TID Detector - Cross-Path Correlation for Traveling Ionospheric Disturbance Detection

================================================================================
DESIGN PHILOSOPHY
================================================================================

Traveling Ionospheric Disturbances (TIDs) are wave-like perturbations in the
ionosphere that propagate horizontally at speeds of 50-300 m/s (medium-scale)
or 300-1000 m/s (large-scale). They cause systematic timing variations that
appear as correlated fluctuations across different propagation paths.

DETECTION PRINCIPLE:
-------------------
1. Each HF path (receiver → station) samples the ionosphere at different points
2. A TID passing through creates timing perturbations that:
   - Appear at different times on different paths (phase delay)
   - Have similar amplitude and period on all paths
   - Show consistent propagation direction

3. Cross-correlation of timing residuals reveals:
   - TID presence (high correlation at non-zero lag)
   - TID velocity (from lag and path geometry)
   - TID direction (from which path leads/lags)

IMPLEMENTATION:
--------------
- Maintain rolling buffers of timing residuals per path
- Band-pass the residuals to the TID period band (≈10-90 min) so diurnal /
  instrumental drift below the band and measurement noise above it cannot
  masquerade as a disturbance
- Compute a per-lag Pearson cross-correlation between path pairs, excluding
  samples interpolated across long data gaps
- Accept a detection only when the best correlation is statistically
  significant after correcting for the number of path pairs searched
- Estimate TID parameters from correlation structure

STATISTICAL SOUNDNESS (code review P-H30..P-H33, 2026-05):
- P-H30: MSTID/LSTID band-pass before cross-correlation; the dominant period
  must fall inside the TID band for a detection.
- P-H31: ``_cross_correlate`` is a true correlation coefficient — Pearson r on
  the per-lag overlap, not ``np.correlate``/N which is biased low at large lag.
- P-H32: a detection must clear a Bonferroni-corrected significance threshold
  (the "max correlation over all path pairs" search inflates the best value on
  pure noise) and a minimum-amplitude gate.
- P-H33: samples interpolated across data gaps longer than ``max_gap_minutes``
  are masked and excluded from correlation, so ``np.interp`` cannot fabricate
  smooth low-frequency features across multi-hour HF dropouts.

================================================================================
"""

import math
import itertools
# §4.4 Low: `math` and `itertools` were re-imported inside individual
# methods on every call; lifted both to the top-level imports.
import numpy as np
from scipy.signal import butter, filtfilt
from scipy.stats import t as _student_t
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# Physical constants
EARTH_RADIUS_KM = 6371.0

# Minimum number of overlapping, unmasked sample pairs required before a
# per-lag Pearson correlation is trusted. Below this the coefficient is too
# noisy to mean anything — cross-correlation of a handful of points is not a
# measurement.
_MIN_OVERLAP = 10

# Butterworth order for the TID band-pass. Order 3 gives a clean passband with
# a short enough impulse response that a 2-hour residual buffer is not eaten
# up by filter settling.
_FILTER_ORDER = 3

# Relative tolerance for the cross-correlation aliasing tie-break. Lags whose
# correlation is within this fraction of the strongest are treated as tied,
# and the one with the most overlapping samples is chosen — see
# ``_cross_correlate``.
_LAG_TIE_TOL = 0.05

# Minimum pierce-point separation (km) for a TDOA pair to provide spatial
# information (P-M26). Same-station / same-receiver paths share the
# great-circle midpoint regardless of frequency, so pairs whose pierce
# points are closer than this contribute degenerate (≈0, ≈0, ≈0) rows
# to the least-squares design matrix and are dropped.
_MIN_PIERCE_SEPARATION_KM = 10.0


@dataclass
class TIDEvent:
    """Detected TID event."""
    start_time: datetime
    end_time: Optional[datetime] = None

    # TID characteristics
    period_minutes: float = 0.0
    amplitude_ms: float = 0.0
    velocity_m_s: float = 0.0
    direction_deg: float = 0.0  # Azimuth of propagation

    # Detection quality
    correlation_coefficient: float = 0.0
    n_paths_correlated: int = 0
    confidence: float = 0.0
    significance_p: float = 1.0  # Bonferroni-corrected p-value of the detection

    # Path information
    leading_path: str = ""  # Path that sees TID first
    lagging_path: str = ""  # Path that sees TID later
    lag_minutes: float = 0.0


@dataclass
class PathResidual:
    """Timing residual for a single path."""
    timestamp: float  # Unix timestamp
    station: str
    frequency_mhz: float
    residual_ms: float  # Observed - Expected timing
    uncertainty_ms: float = 1.0


class TIDDetector:
    """
    Cross-path correlation detector for Traveling Ionospheric Disturbances.

    Maintains rolling buffers of timing residuals per propagation path and
    computes cross-correlations to detect TID signatures.
    """

    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        buffer_minutes: int = 120,
        min_correlation: float = 0.6,
        min_lag_minutes: float = 1.0,
        sample_interval_seconds: float = 60.0,
        tid_period_min_minutes: float = 10.0,
        tid_period_max_minutes: float = 90.0,
        min_amplitude_ms: float = 0.3,
        significance_alpha: float = 0.01,
        max_gap_minutes: float = 5.0,
    ):
        """
        Initialize TID detector.

        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            buffer_minutes: Length of residual buffer (default 2 hours)
            min_correlation: Minimum correlation coefficient for TID detection.
                Meaningful now that ``_cross_correlate`` returns a true Pearson
                r (P-H31); a detection must additionally clear the significance
                test below.
            min_lag_minutes: Minimum lag to consider (excludes zero-lag)
            sample_interval_seconds: Expected sample interval
            tid_period_min_minutes: Short-period edge of the TID band-pass
                (P-H30). Periods shorter than this are measurement noise.
            tid_period_max_minutes: Long-period edge of the TID band-pass
                (P-H30). Periods longer than this are diurnal / instrumental
                drift, not a travelling disturbance.
            min_amplitude_ms: Minimum band-passed residual amplitude (std, ms)
                for a detection (P-H32). Rejects statistically-significant but
                physically-negligible wiggles.
            significance_alpha: Family-wise false-alarm rate. The best
                correlation found across all path pairs must have a
                Bonferroni-corrected p-value below this (P-H32).
            max_gap_minutes: Residuals interpolated across a data gap longer
                than this are masked out and excluded from correlation (P-H33).
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.buffer_minutes = buffer_minutes
        self.min_correlation = min_correlation
        self.min_lag_minutes = min_lag_minutes
        self.sample_interval_seconds = sample_interval_seconds
        self.tid_period_min_minutes = tid_period_min_minutes
        self.tid_period_max_minutes = tid_period_max_minutes
        self.min_amplitude_ms = min_amplitude_ms
        self.significance_alpha = significance_alpha
        self.max_gap_minutes = max_gap_minutes

        # Rolling buffers of residuals per path
        # Key: (station, frequency_mhz)
        self._residual_buffers: Dict[Tuple[str, float], List[PathResidual]] = defaultdict(list)

        # Path geometry (computed on first residual)
        self._path_azimuths: Dict[Tuple[str, float], float] = {}
        self._path_distances: Dict[Tuple[str, float], float] = {}

        # Detected events.  Intended lifecycle: a new TID event is
        # appended to `_active_events` when first detected; on the
        # cycle the detection drops below threshold the event moves
        # from `_active_events` to `_completed_events`.  Today
        # nothing populates either list -- the detector has no
        # run-loop / writer (P-H29 deferred), so both stay empty.
        # When the L3 writer lands, both lists need a bound to avoid
        # unbounded growth over multi-week runs (§4.4 Low): a sensible
        # default is to cap `_completed_events` at e.g. 1000 entries
        # using a deque(maxlen=1000) so the in-memory state stays
        # constant even when no consumer drains them.  See
        # `core/multi_broadcast_fusion.py:AllanDeviationTracker`
        # for the same pattern applied to a different ring buffer.
        self._active_events: List[TIDEvent] = []
        self._completed_events: List[TIDEvent] = []

        # Station locations
        self._station_locations = {
            'WWV': (40.6781, -105.0469),
            'WWVH': (21.9886, -159.7642),
            'CHU': (45.2925, -75.7542),
            'BPM': (34.9500, 109.5500),
        }

        logger.info(f"TIDDetector initialized: {buffer_minutes}min buffer, "
                   f"min_corr={min_correlation}, min_lag={min_lag_minutes}min, "
                   f"band={tid_period_min_minutes}-{tid_period_max_minutes}min, "
                   f"alpha={significance_alpha}")

    def add_residual(self, residual: PathResidual):
        """
        Add a timing residual to the buffer.

        Args:
            residual: PathResidual with timing deviation
        """
        key = (residual.station, residual.frequency_mhz)

        # Compute path geometry if not already done
        if key not in self._path_azimuths:
            self._compute_path_geometry(residual.station, residual.frequency_mhz)

        # Add to buffer
        self._residual_buffers[key].append(residual)

        # Trim old residuals
        max_samples = int(self.buffer_minutes * 60 / self.sample_interval_seconds)
        if len(self._residual_buffers[key]) > max_samples:
            self._residual_buffers[key] = self._residual_buffers[key][-max_samples:]

    def _compute_path_geometry(self, station: str, frequency_mhz: float):
        """Compute azimuth and distance for a path."""
        key = (station, frequency_mhz)

        if station not in self._station_locations:
            logger.warning(f"Unknown station: {station}")
            return

        station_lat, station_lon = self._station_locations[station]

        # Great circle distance
        distance_km = self._haversine_km(
            self.receiver_lat, self.receiver_lon,
            station_lat, station_lon
        )

        # Azimuth from receiver to station
        azimuth_deg = self._compute_azimuth(
            self.receiver_lat, self.receiver_lon,
            station_lat, station_lon
        )

        self._path_distances[key] = distance_km
        self._path_azimuths[key] = azimuth_deg

        logger.debug(f"Path {station}@{frequency_mhz}MHz: "
                    f"dist={distance_km:.0f}km, az={azimuth_deg:.1f}°")

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance using Haversine formula."""

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)

        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))

        return EARTH_RADIUS_KM * c

    @staticmethod
    def _compute_azimuth(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Compute initial azimuth from point 1 to point 2."""

        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lon = math.radians(lon2 - lon1)

        x = math.sin(delta_lon) * math.cos(lat2_rad)
        y = (math.cos(lat1_rad) * math.sin(lat2_rad) -
             math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon))

        azimuth_rad = math.atan2(x, y)
        azimuth_deg = math.degrees(azimuth_rad)

        return (azimuth_deg + 360) % 360

    def detect_tid(self) -> Optional[TIDEvent]:
        """
        Analyze current residual buffers for TID signatures.

        Pipeline: align residuals onto a common grid (masking long gaps) →
        band-pass each path to the TID period band → search every path pair
        for the largest per-lag Pearson correlation → accept only if that
        correlation is statistically significant (Bonferroni-corrected for the
        number of pairs), of sufficient amplitude, and at a TID-band period.

        Returns:
            TIDEvent if a significant TID is detected, None otherwise
        """
        paths = list(self._residual_buffers.keys())

        if len(paths) < 2:
            return None  # Need at least 2 paths for cross-correlation

        # Align residual time series onto a common grid; ``masks`` flags
        # samples interpolated across gaps longer than max_gap_minutes.
        aligned_result = self._align_residuals(paths)
        if aligned_result is None:
            return None
        aligned_series, masks = aligned_result

        # P-H30: band-pass each path to the TID period band. Diurnal /
        # instrumental drift (below the band) cross-correlates strongly between
        # paths and would otherwise be flagged as a TID; measurement noise
        # (above the band) inflates spurious short-lag correlation.
        # The band-pass needs a continuous input, so it runs on the
        # gap-interpolated series — then the gap mask is dilated to drop the
        # filter transient the interpolated segment injects around each gap.
        filtered: Dict[Tuple[str, float], np.ndarray] = {}
        filt_masks: Dict[Tuple[str, float], np.ndarray] = {}
        gap_margin = max(1, int(self.tid_period_min_minutes * 60 /
                                self.sample_interval_seconds))
        for path, series in aligned_series.items():
            band = self._bandpass_filter(series)
            if band is None:
                continue  # too short to filter — drop this path
            filtered[path] = band
            filt_masks[path] = self._dilate_mask(masks[path], gap_margin)

        if len(filtered) < 2:
            return None

        # Search every path pair for the strongest per-lag correlation.
        path_keys = list(filtered.keys())
        n_pairs = len(path_keys) * (len(path_keys) - 1) // 2

        best_correlation = 0.0
        best_lag = 0
        best_overlap = 0
        best_pair = None

        for i in range(len(path_keys)):
            for j in range(i + 1, len(path_keys)):
                path1, path2 = path_keys[i], path_keys[j]
                corr, lag, overlap = self._cross_correlate(
                    filtered[path1], filtered[path2],
                    filt_masks[path1], filt_masks[path2],
                )

                lag_minutes = abs(lag) * self.sample_interval_seconds / 60.0

                if (corr > best_correlation
                        and lag_minutes >= self.min_lag_minutes):
                    best_correlation = corr
                    best_lag = lag
                    best_overlap = overlap
                    best_pair = (path1, path2)

        if best_pair is None:
            return None

        # Gate 1 — correlation-coefficient threshold. Now that
        # _cross_correlate returns a true Pearson r (P-H31), the min_correlation
        # default is a meaningful coefficient cut rather than an artefact of
        # the old length-normalised statistic.
        if best_correlation < self.min_correlation:
            return None

        # Leading/lagging path from the sign of the winning lag.
        if best_lag > 0:
            leading_path = f"{best_pair[0][0]}@{best_pair[0][1]}MHz"
            lagging_path = f"{best_pair[1][0]}@{best_pair[1][1]}MHz"
        else:
            leading_path = f"{best_pair[1][0]}@{best_pair[1][1]}MHz"
            lagging_path = f"{best_pair[0][0]}@{best_pair[0][1]}MHz"

        lag_minutes = best_lag * self.sample_interval_seconds / 60.0

        lead_series = filtered[best_pair[0]]
        lead_mask = filt_masks[best_pair[0]]
        valid = lead_series[lead_mask] if lead_mask.any() else lead_series

        # Gate 2 — period (P-H30). The dominant period must lie inside the TID
        # band. The band-pass should already guarantee this; estimating the
        # period explicitly rejects broadband filter ringing and supplies the
        # cycle count the significance test needs.
        period_minutes = self._estimate_period(lead_series)
        if not (self.tid_period_min_minutes <= period_minutes
                <= self.tid_period_max_minutes):
            logger.debug(
                f"TID candidate rejected: period {period_minutes:.1f}min "
                f"outside TID band [{self.tid_period_min_minutes}, "
                f"{self.tid_period_max_minutes}]min")
            return None

        # Gate 3 — amplitude (P-H32). A significant correlation between two
        # negligibly-small wiggles is not a physically meaningful TID. Measure
        # amplitude over the unmasked (real-data) samples of the leading path.
        amplitude_ms = float(np.std(valid))
        if amplitude_ms < self.min_amplitude_ms:
            logger.debug(
                f"TID candidate rejected: amplitude {amplitude_ms:.3f}ms "
                f"< {self.min_amplitude_ms}ms")
            return None

        # Gate 4 — statistical significance (P-H32). The detection is the
        # largest correlation found over n_pairs path pairs, so the
        # per-comparison p-value is Bonferroni-corrected by n_pairs.
        # The effective sample size is the number of TID cycles actually
        # observed (overlap·dt / period), NOT the raw sample count: band-passed
        # samples within one cycle are strongly autocorrelated, so a raw-N
        # t-test would badly overstate significance. The significance of a
        # periodic detection scales with how many cycles it repeats for — a
        # short buffer or a long-period TID is correctly hard to confirm.
        cycles_observed = (best_overlap * self.sample_interval_seconds
                           / (period_minutes * 60.0))
        significance_p = self._correlation_pvalue(
            best_correlation, cycles_observed, n_pairs)
        if significance_p >= self.significance_alpha:
            logger.debug(
                f"TID candidate rejected: corr={best_correlation:.2f} "
                f"not significant (p={significance_p:.3g} ≥ "
                f"{self.significance_alpha}, cycles={cycles_observed:.1f}, "
                f"pairs={n_pairs})")
            return None

        # P3-B: 3D TDOA TID Velocity/Direction
        # Find all paths that correlate well with the best path to form an array
        correlated_paths = [best_pair[0]]
        for path in path_keys:
            if path != best_pair[0]:
                corr, _, _ = self._cross_correlate(
                    filtered[best_pair[0]], filtered[path],
                    filt_masks[best_pair[0]], filt_masks[path],
                )
                if corr >= self.min_correlation * 0.8:  # Slightly lower threshold for array inclusion
                    correlated_paths.append(path)

        velocity_m_s = None
        direction_deg = None

        if len(correlated_paths) >= 3:
            # We have enough paths to solve TDOA unambiguously
            v_tdoa, dir_tdoa = self._solve_tdoa_velocity(
                correlated_paths, filtered, filt_masks)
            if v_tdoa is not None and dir_tdoa is not None:
                velocity_m_s = v_tdoa
                direction_deg = dir_tdoa
                logger.info(f"Resolved TID via TDOA ({len(correlated_paths)} paths): {velocity_m_s:.0f} m/s @ {direction_deg:.0f}°")

        if velocity_m_s is None or direction_deg is None:
            # Fallback to 2-path geometry estimation
            velocity_m_s = self._estimate_tid_velocity(best_pair, abs(lag_minutes))
            direction_deg = self._estimate_tid_direction(best_pair, best_lag)

        event = TIDEvent(
            start_time=datetime.now(timezone.utc),
            period_minutes=period_minutes,
            amplitude_ms=amplitude_ms,
            velocity_m_s=velocity_m_s,
            direction_deg=direction_deg,
            correlation_coefficient=best_correlation,
            n_paths_correlated=len(correlated_paths),
            # Significance-based confidence (P-M26 — replaces an ad-hoc
            # ``best_correlation × 1.2``). 1 − p maps a Bonferroni-adjusted
            # p-value to [0, 1]: at the detector's α threshold (p = α) the
            # confidence is 1 − α, falling toward 0 as p approaches 1.
            confidence=max(0.0, min(1.0, 1.0 - significance_p)),
            significance_p=significance_p,
            leading_path=leading_path,
            lagging_path=lagging_path,
            lag_minutes=abs(lag_minutes)
        )

        logger.info(f"TID detected: corr={best_correlation:.2f}, "
                   f"p={significance_p:.3g}, lag={lag_minutes:.1f}min, "
                   f"period={period_minutes:.1f}min, "
                   f"vel={velocity_m_s:.0f}m/s, dir={direction_deg:.0f}°")

        return event

    def _align_residuals(
        self,
        paths: List[Tuple[str, float]]
    ) -> Optional[Tuple[Dict[Tuple[str, float], np.ndarray],
                        Dict[Tuple[str, float], np.ndarray]]]:
        """
        Align residual time series to a common time grid.

        Returns ``(aligned, masks)`` where ``aligned[path]`` is the
        detrended residual array on the common grid and ``masks[path]`` is a
        boolean array — True where the grid sample is backed by real data,
        False where it was interpolated across a gap wider than
        ``max_gap_minutes`` (P-H33). Returns None if fewer than two paths
        have usable data.

        The interpolated array is still fully populated (the band-pass
        downstream needs a continuous signal); the mask records which samples
        the correlation is allowed to trust.
        """
        if not paths:
            return None

        # Find common time range
        all_times = []
        for path in paths:
            if path in self._residual_buffers:
                times = [r.timestamp for r in self._residual_buffers[path]]
                all_times.extend(times)

        if not all_times:
            return None

        min_time = min(all_times)
        max_time = max(all_times)

        # Create common time grid
        n_samples = int((max_time - min_time) / self.sample_interval_seconds) + 1
        if n_samples < 10:
            return None

        time_grid = np.linspace(min_time, max_time, n_samples)
        max_gap_seconds = self.max_gap_minutes * 60.0

        # Interpolate each path to common grid
        aligned = {}
        masks = {}
        for path in paths:
            if path not in self._residual_buffers:
                continue

            residuals = self._residual_buffers[path]
            if len(residuals) < 5:
                continue

            times = np.array([r.timestamp for r in residuals])
            values = np.array([r.residual_ms for r in residuals])

            # Simple linear interpolation
            aligned_values = np.interp(time_grid, times, values)

            # P-H33: mark grid samples that fall inside a too-wide gap (or
            # outside the path's observed span) as untrustworthy. np.interp
            # would otherwise draw a straight line across a multi-hour HF
            # dropout, which correlates between paths as a fake slow TID.
            mask = self._gap_mask(time_grid, times, max_gap_seconds)

            # Detrend (remove linear trend) — conditions the series for the
            # band-pass and removes the largest non-TID component.
            aligned_values = aligned_values - np.polyval(
                np.polyfit(np.arange(len(aligned_values)), aligned_values, 1),
                np.arange(len(aligned_values))
            )

            aligned[path] = aligned_values
            masks[path] = mask

        if len(aligned) < 2:
            return None
        return aligned, masks

    @staticmethod
    def _gap_mask(
        time_grid: np.ndarray,
        sample_times: np.ndarray,
        max_gap_seconds: float,
    ) -> np.ndarray:
        """
        Boolean mask over ``time_grid``: True where a grid sample is bracketed
        by real samples no more than ``max_gap_seconds`` apart, False where it
        was interpolated across a wider gap or extrapolated beyond the data.
        """
        mask = np.ones(len(time_grid), dtype=bool)
        first, last = sample_times[0], sample_times[-1]
        for i, t in enumerate(time_grid):
            if t < first or t > last:
                mask[i] = False  # extrapolation
                continue
            # Bracketing real samples around t
            idx = int(np.searchsorted(sample_times, t, side='right'))
            left = sample_times[idx - 1] if idx > 0 else sample_times[0]
            right = sample_times[idx] if idx < len(sample_times) else sample_times[-1]
            if (right - left) > max_gap_seconds:
                mask[i] = False
        return mask

    @staticmethod
    def _dilate_mask(mask: np.ndarray, margin: int) -> np.ndarray:
        """
        Grow the False (invalid) regions of ``mask`` by ``margin`` samples on
        each side. The band-pass smears a gap's interpolated segment into a
        filter-transient that contaminates roughly one short-TID-period of
        neighbouring output, so the gap mask is dilated by that margin before
        the filtered series is correlated.
        """
        if margin <= 0:
            return mask.copy()
        invalid = ~mask
        if not invalid.any():
            return mask.copy()
        dilated = invalid.copy()
        for i in np.where(invalid)[0]:
            lo = max(0, i - margin)
            hi = min(len(mask), i + margin + 1)
            dilated[lo:hi] = True
        return ~dilated

    def _bandpass_filter(self, series: np.ndarray) -> Optional[np.ndarray]:
        """
        Band-pass ``series`` to the TID period band (P-H30).

        The passband is ``[tid_period_max, tid_period_min]`` expressed as
        frequency: it removes diurnal / instrumental drift below the band and
        measurement noise above it, leaving only fluctuations at travelling-
        disturbance periods. Returns None if the series is too short for a
        zero-phase (filtfilt) filter of order ``_FILTER_ORDER``.
        """
        n = len(series)
        # filtfilt pads by 3*max(len(a),len(b)); a band-pass of order k has
        # 2k+1 taps. Require a comfortable margin over that padding length.
        min_len = 3 * (2 * _FILTER_ORDER + 1)
        if n <= min_len:
            return None

        dt = self.sample_interval_seconds
        nyquist = 0.5  # cycles/sample
        # Frequency (cycles/sample) = dt / period_seconds.
        f_high = dt / (self.tid_period_min_minutes * 60.0)  # short period
        f_low = dt / (self.tid_period_max_minutes * 60.0)   # long period
        w_low = f_low / nyquist
        w_high = f_high / nyquist
        if not (0.0 < w_low < w_high < 1.0):
            logger.warning(
                f"TID band [{self.tid_period_min_minutes}, "
                f"{self.tid_period_max_minutes}]min not representable at "
                f"{dt}s sampling — skipping band-pass")
            return None

        try:
            b, a = butter(_FILTER_ORDER, [w_low, w_high], btype='band')
            return filtfilt(b, a, series)
        except ValueError as e:
            logger.debug(f"Band-pass filter failed: {e}")
            return None

    def _cross_correlate(
        self,
        series1: np.ndarray,
        series2: np.ndarray,
        mask1: Optional[np.ndarray] = None,
        mask2: Optional[np.ndarray] = None,
    ) -> Tuple[float, int, int]:
        """
        Per-lag Pearson cross-correlation between two residual series.

        For each candidate lag the Pearson correlation coefficient is computed
        on the overlapping samples that are valid in *both* series' masks.
        This fixes two review findings:

        - P-H31: the coefficient is normalised by the per-lag overlap (it is a
          genuine Pearson r in [-1, 1]), not ``np.correlate(...)/len(s1)``
          which divides by the full length and is biased low at large lag —
          there the threshold would suppress slow LSTIDs.
        - P-H33: samples interpolated across long data gaps (mask False) are
          dropped, so a fabricated straight line across an HF dropout cannot
          contribute a spurious correlation.

        Returns:
            (max_abs_correlation, lag_at_max, overlap_count) — lag in samples
            (positive ⇒ series1 leads series2); overlap_count is the number of
            valid sample pairs the winning coefficient was computed from, for
            the significance test.
        """
        n = min(len(series1), len(series2))
        s1 = np.asarray(series1[:n], dtype=float)
        s2 = np.asarray(series2[:n], dtype=float)
        m1 = (np.ones(n, dtype=bool) if mask1 is None
              else np.asarray(mask1[:n], dtype=bool))
        m2 = (np.ones(n, dtype=bool) if mask2 is None
              else np.asarray(mask2[:n], dtype=bool))

        min_lag_samples = int(
            self.min_lag_minutes * 60 / self.sample_interval_seconds)

        # A lag is only trusted when at least half the record overlaps with
        # enough unmasked samples. This both bounds the search to physically
        # plausible TID lags (|lag| ≤ n/2) and rejects the wild coincidental
        # coefficients a per-lag Pearson r produces on a handful of points at
        # extreme lag — the very noise the old length-normalisation masked.
        min_overlap = max(_MIN_OVERLAP, n // 2)

        candidates = []  # (abs_r, lag, overlap_count)
        for lag in range(-(n - 1), n):
            if abs(lag) < min_lag_samples:
                continue
            # Align series1[lag:] with series2 for lag ≥ 0 (series1 leads).
            if lag >= 0:
                a, b = s1[lag:], s2[:n - lag]
                ma, mb = m1[lag:], m2[:n - lag]
            else:
                a, b = s1[:n + lag], s2[-lag:]
                ma, mb = m1[:n + lag], m2[-lag:]

            valid = ma & mb
            k = int(valid.sum())
            if k < min_overlap:
                continue

            av = a[valid]
            bv = b[valid]
            sa = av.std()
            sb = bv.std()
            if sa < 1e-12 or sb < 1e-12:
                continue  # a constant series has no correlation

            r = float(np.mean((av - av.mean()) * (bv - bv.mean())) / (sa * sb))
            candidates.append((abs(r), lag, k))

        if not candidates:
            return 0.0, 0, 0
        best_abs = max(c[0] for c in candidates)
        if best_abs <= 0.0:
            return 0.0, 0, 0

        # Aliasing tie-break: a (near-)periodic signal correlates almost
        # equally at lag ± period multiples, and finite-window / filter-edge
        # effects mean those peaks are not exactly equal — the strongest can
        # be an alias at the record edge with little overlap. Among lags whose
        # correlation is within _LAG_TIE_TOL of the strongest, take the one
        # with the most overlapping samples: the smaller, physical lag, and
        # the largest honest sample count for the significance test.
        tied = [c for c in candidates
                if c[0] >= best_abs * (1.0 - _LAG_TIE_TOL)]
        chosen = max(tied, key=lambda c: c[2])
        return float(chosen[0]), int(chosen[1]), int(chosen[2])

    @staticmethod
    def _correlation_pvalue(r: float, n_eff: float, n_tests: int) -> float:
        """
        Bonferroni-corrected two-sided p-value for a Pearson correlation
        coefficient ``r`` (P-H32).

        ``n_eff`` is the *effective* sample size — for band-passed residuals
        this is the number of independent TID cycles observed, not the raw
        sample count, because samples within a cycle are autocorrelated.
        Under the null hypothesis of no correlation,
        ``t = r·√((n_eff-2)/(1-r²))`` is t-distributed with ``n_eff-2``
        degrees of freedom. ``detect_tid`` keeps the largest r over
        ``n_tests`` path pairs, so the per-comparison p-value is multiplied by
        ``n_tests`` to bound the family-wise false-alarm rate. Returns 1.0
        (not significant) when ``n_eff`` is too small to support the test —
        i.e. too few cycles were observed to confirm a periodic signal.
        """
        if n_eff <= 2:
            return 1.0
        r_abs = min(abs(r), 1.0 - 1e-12)
        t_stat = r_abs * math.sqrt((n_eff - 2) / (1.0 - r_abs * r_abs))
        p_single = 2.0 * float(_student_t.sf(t_stat, df=n_eff - 2))
        return min(1.0, p_single * max(1, n_tests))

    def _compute_pierce_point(self, station: str) -> Tuple[float, float]:
        if station not in self._station_locations:
            return self.receiver_lat, self.receiver_lon

        st_lat, st_lon = self._station_locations[station]

        rx_lat_rad = math.radians(self.receiver_lat)
        rx_lon_rad = math.radians(self.receiver_lon)
        tx_lat_rad = math.radians(st_lat)
        tx_lon_rad = math.radians(st_lon)

        Bx = math.cos(tx_lat_rad) * math.cos(tx_lon_rad - rx_lon_rad)
        By = math.cos(tx_lat_rad) * math.sin(tx_lon_rad - rx_lon_rad)

        mid_lat_rad = math.atan2(
            math.sin(rx_lat_rad) + math.sin(tx_lat_rad),
            math.sqrt((math.cos(rx_lat_rad) + Bx)**2 + By**2)
        )
        mid_lon_rad = rx_lon_rad + math.atan2(By, math.cos(rx_lat_rad) + Bx)

        return math.degrees(mid_lat_rad), math.degrees(mid_lon_rad)

    def _get_enu_coords(self, lat: float, lon: float) -> Tuple[float, float]:

        R = 6371.0
        lat_rad, lon_rad = math.radians(lat), math.radians(lon)
        ref_lat_rad, ref_lon_rad = math.radians(self.receiver_lat), math.radians(self.receiver_lon)
        d_lat = lat_rad - ref_lat_rad
        d_lon = lon_rad - ref_lon_rad
        y = d_lat * R
        x = d_lon * R * math.cos(ref_lat_rad)
        return x, y

    @staticmethod
    def _great_circle_km(lat1: float, lon1: float,
                         lat2: float, lon2: float) -> float:
        """Haversine great-circle distance in km."""
        lat1r, lat2r = math.radians(lat1), math.radians(lat2)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2
             + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2)
        return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))

    @staticmethod
    def _bearing_deg(lat1: float, lon1: float,
                     lat2: float, lon2: float) -> float:
        """Initial great-circle bearing from (lat1, lon1) to (lat2, lon2),
        degrees in [0, 360)."""
        lat1r, lat2r = math.radians(lat1), math.radians(lat2)
        dlon = math.radians(lon2 - lon1)
        return math.degrees(math.atan2(
            math.sin(dlon) * math.cos(lat2r),
            math.cos(lat1r) * math.sin(lat2r)
            - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon),
        )) % 360.0

    def _solve_tdoa_velocity(
        self,
        correlated_paths: List[Tuple[str, float]],
        aligned_series: Dict[Tuple[str, float], np.ndarray],
        masks: Optional[Dict[Tuple[str, float], np.ndarray]] = None,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Solve the planar TDOA system for TID slowness, returning
        ``(velocity_m_s, azimuth_deg)`` or ``(None, None)``.

        For each pair of correlated paths the lag-vs-baseline relation
        ``[dx, dy] · s = dt`` constrains the 2-D slowness ``s``; the
        least-squares solution gives velocity (1/‖s‖) and azimuth.

        P-M26 — robustness fixes:

        * Pairs whose pierce-point baseline is shorter than
          ``_MIN_PIERCE_SEPARATION_KM`` provide no spatial information
          and are dropped — paths sharing a station collapse to the same
          great-circle midpoint regardless of frequency, so they would
          otherwise contribute degenerate ``(≈0, ≈0, ≈0)`` rows to the
          design matrix.
        * ``np.linalg.lstsq``'s rank output is used: if the kept pairs
          do not span the plane (rank < 2 — pierce points collinear or
          coincident) the solve is ill-posed and ``None`` is returned
          rather than a confident-looking but meaningless velocity.
        """


        if len(correlated_paths) < 3:
            return None, None

        points = []
        for p in correlated_paths:
            station = p[0]
            lat, lon = self._compute_pierce_point(station)
            x, y = self._get_enu_coords(lat, lon)
            points.append((x, y))

        A = []
        B = []
        for i, j in itertools.combinations(range(len(correlated_paths)), 2):
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            if (dx * dx + dy * dy) < _MIN_PIERCE_SEPARATION_KM ** 2:
                continue  # degenerate baseline (same station / coincident pierce points)

            p1, p2 = correlated_paths[i], correlated_paths[j]
            m1 = masks.get(p1) if masks else None
            m2 = masks.get(p2) if masks else None
            _corr, lag_samples, _ = self._cross_correlate(
                aligned_series[p1], aligned_series[p2], m1, m2)
            dt_seconds = lag_samples * self.sample_interval_seconds

            A.append([dx, dy])
            B.append(dt_seconds)

        if len(A) < 2:
            # Fewer informative baselines than unknowns — under-determined.
            return None, None

        A = np.array(A)
        B = np.array(B)

        try:
            sol, _residuals, rank, _sv = np.linalg.lstsq(A, B, rcond=None)
            if rank < 2:
                # Pierce points effectively collinear — the slowness
                # direction perpendicular to the line is unconstrained.
                logger.debug(
                    "TDOA slowness rank-deficient (rank=%d, %d baselines) — "
                    "pierce points collinear; rejecting", rank, len(A))
                return None, None
            sx, sy = sol
            slowness_mag = math.sqrt(sx * sx + sy * sy)
            if slowness_mag <= 0:
                return None, None
            v_km_s = 1.0 / slowness_mag
            az_rad = math.atan2(sx, sy)
            az_deg = math.degrees(az_rad) % 360.0
            return v_km_s * 1000.0, az_deg
        except Exception as e:
            logger.debug(f"TDOA lstsq failed: {e}")
            return None, None

    def _estimate_tid_velocity(
        self,
        path_pair: Tuple[Tuple[str, float], Tuple[str, float]],
        lag_minutes: float
    ) -> float:
        """
        Two-path TID velocity fallback when the 3+ path TDOA solve was
        unavailable.

        Velocity = distance / time, with distance taken as the great-circle
        distance between the two paths' ionospheric pierce points (P-M26 —
        replaces a heuristic ``2·h·sin(Δaz/2)`` that conflated path-azimuth
        difference with pierce-point separation). Returns 0.0 when the lag
        is non-positive or when the two paths share a pierce point (same
        station, multi-freq) — in that case the 2-path geometry carries no
        spatial information and the velocity is undefined.
        """
        if lag_minutes <= 0:
            return 0.0

        path1, path2 = path_pair
        lat1, lon1 = self._compute_pierce_point(path1[0])
        lat2, lon2 = self._compute_pierce_point(path2[0])
        separation_km = self._great_circle_km(lat1, lon1, lat2, lon2)
        if separation_km < _MIN_PIERCE_SEPARATION_KM:
            return 0.0
        return (separation_km * 1000.0) / (lag_minutes * 60.0)

    def _estimate_tid_direction(
        self,
        path_pair: Tuple[Tuple[str, float], Tuple[str, float]],
        lag: int
    ) -> float:
        """
        Two-path TID propagation direction fallback.

        Returns the great-circle bearing from the leading path's pierce
        point to the lagging path's pierce point — the direction the
        wavefront is travelling between the two observation points
        (P-M26). The old fallback returned the leading path's TX→RX
        azimuth, which is unrelated to the TID's propagation direction.
        Returns 0.0 when the two paths share a pierce point.
        """
        path1, path2 = path_pair
        lat1, lon1 = self._compute_pierce_point(path1[0])
        lat2, lon2 = self._compute_pierce_point(path2[0])
        if self._great_circle_km(lat1, lon1, lat2, lon2) < _MIN_PIERCE_SEPARATION_KM:
            return 0.0
        # lag > 0 → path1 leads; TID propagates from pierce1 toward pierce2.
        if lag >= 0:
            return self._bearing_deg(lat1, lon1, lat2, lon2)
        return self._bearing_deg(lat2, lon2, lat1, lon1)

    def _estimate_period(self, series: np.ndarray) -> float:
        """Estimate dominant period from autocorrelation."""
        if len(series) < 20:
            return 0.0

        # Autocorrelation
        s = (series - np.mean(series)) / (np.std(series) + 1e-10)
        acf = np.correlate(s, s, mode='full')
        acf = acf[len(acf)//2:]  # Keep positive lags only
        acf = acf / acf[0]  # Normalize

        # Find first peak after zero
        min_lag = int(5 * 60 / self.sample_interval_seconds)  # At least 5 minutes

        if len(acf) <= min_lag:
            return 0.0

        # Find peaks
        peaks = []
        for i in range(min_lag, len(acf) - 1):
            if acf[i] > acf[i-1] and acf[i] > acf[i+1] and acf[i] > 0.3:
                peaks.append(i)

        if not peaks:
            return 0.0

        # First peak is the period
        period_samples = peaks[0]
        period_minutes = period_samples * self.sample_interval_seconds / 60.0

        return period_minutes

    def get_active_events(self) -> List[TIDEvent]:
        """Get list of currently active TID events."""
        return list(self._active_events)

    def get_recent_events(self, hours: float = 24.0) -> List[TIDEvent]:
        """Get TID events from the last N hours."""
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600

        recent = []
        for event in self._completed_events + self._active_events:
            if event.start_time.timestamp() > cutoff:
                recent.append(event)

        return recent

    def get_statistics(self) -> Dict:
        """Get detector statistics."""
        return {
            'n_paths': len(self._residual_buffers),
            'paths': [f"{k[0]}@{k[1]}MHz" for k in self._residual_buffers.keys()],
            'buffer_samples': {
                f"{k[0]}@{k[1]}MHz": len(v)
                for k, v in self._residual_buffers.items()
            },
            'n_active_events': len(self._active_events),
            'n_completed_events': len(self._completed_events),
        }
