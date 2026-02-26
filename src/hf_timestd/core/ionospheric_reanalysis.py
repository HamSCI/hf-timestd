#!/usr/bin/env python3
"""
Ionospheric Reanalysis Service
================================================================================
Offline hourly job that applies ionospheric physics to clean up propagation mode
assignments and TEC estimates from the real-time pipeline.

The real-time mode solver (propagation_mode_solver.py) assigns modes purely by
timing delay geometry — it has no awareness of ionospheric state. This leads to
physically impossible mode assignments (e.g., 25 MHz labeled "4F2" at night when
the F2 layer cannot support it).

This service fixes that by:
1. Computing solar elevation at each path midpoint
2. Estimating foF2 (F2 critical frequency) from a Chapman layer model
3. Computing the oblique MUF for each candidate mode geometry
4. Rejecting modes where frequency > oblique MUF
5. Gating on SNR to exclude noise-floor detections
6. Re-estimating TEC using only mode-consistent, high-SNR measurements
7. Writing cleaned L3C propagation stats with corrected MUF

Designed to run hourly at nice 19 via systemd timer.

Architecture:
    L2 HDF5 (timing_measurements) -> [Reanalysis] -> L3C HDF5 (propagation_stats)
                                                   -> L3A HDF5 (tec, reanalyzed)
"""

import logging
import math
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict, Counter
from dataclasses import dataclass

import numpy as np

from hf_timestd.core.solar_zenith_calculator import (
    solar_position, calculate_midpoint, grid_to_latlon,
    WWV_LOCATION, WWVH_LOCATION, CHU_LOCATION, BPM_LOCATION
)
from hf_timestd.core.tec_estimator import TECEstimator
from hf_timestd.core.wwv_constants import (
    SPEED_OF_LIGHT_KM_S, EARTH_RADIUS_KM,
    E_LAYER_HEIGHT_KM, F_LAYER_HEIGHT_KM,
    WWV_FREQUENCIES, WWVH_FREQUENCIES, CHU_FREQUENCIES, BPM_FREQUENCIES,
    ANCHOR_SNR_HIGH,
)
from hf_timestd.io import DataProductReader, DataProductWriter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# Physical constants for ionospheric modeling
# =============================================================================

# Chapman layer model parameters for foF2 estimation
# foF2 ≈ foF2_noon * cos^0.25(χ) where χ is solar zenith angle
# Typical midlatitude foF2_noon: 8-12 MHz at solar max, 4-7 MHz at solar min
# We use a moderate value; this is approximate but far better than nothing.
FOF2_NOON_MHZ = 9.0  # Moderate solar activity estimate

# Nighttime foF2 floor (F2 layer persists at night but weakens)
FOF2_NIGHT_FLOOR_MHZ = 3.0

# Minimum SNR (dB) to consider a detection credible for mode/MUF analysis
MIN_SNR_CREDIBLE_DB = 12.0

# Minimum SNR for TEC estimation (higher bar — need good timing)
MIN_SNR_TEC_DB = 15.0

# Minimum measurements per broadcast for inclusion in stats
MIN_MEASUREMENTS_CREDIBLE = 2

# Valid station/frequency combinations
VALID_STATION_FREQS = {
    'WWV': set(WWV_FREQUENCIES),
    'WWVH': set(WWVH_FREQUENCIES),
    'CHU': set(CHU_FREQUENCIES),
    'BPM': set(BPM_FREQUENCIES),
}

# Station locations as (lat, lon) tuples for midpoint calculation
STATION_COORDS = {
    'WWV': WWV_LOCATION,
    'WWVH': WWVH_LOCATION,
    'CHU': CHU_LOCATION,
    'BPM': BPM_LOCATION,
}


@dataclass
class ReanalyzedMeasurement:
    """A single L2 measurement with reanalysis annotations."""
    timestamp: str
    station: str
    frequency_mhz: float
    snr_db: float
    original_mode: str
    original_n_hops: int
    raw_arrival_time_ms: float
    propagation_delay_ms: float
    confidence: float
    quality_flag: str

    # Reanalysis results
    solar_elevation_deg: float
    estimated_fof2_mhz: float
    oblique_muf_mhz: float  # For the assigned mode
    mode_physically_valid: bool
    validated_mode: str  # After physics check
    validated_n_hops: int
    rejection_reason: Optional[str] = None


def estimate_fof2(solar_elevation_deg: float) -> float:
    """
    Estimate F2 critical frequency from solar elevation using Chapman model.

    The Chapman layer model gives:
        foF2 ≈ foF2_noon × cos^0.25(χ)
    where χ is the solar zenith angle (90° - elevation).

    At night (elevation < 0), foF2 decays but doesn't vanish — the F2 layer
    persists due to slow recombination at high altitudes.

    Args:
        solar_elevation_deg: Solar elevation at path midpoint in degrees

    Returns:
        Estimated foF2 in MHz
    """
    if solar_elevation_deg <= -18:
        # Deep night — astronomical twilight ended
        return FOF2_NIGHT_FLOOR_MHZ

    if solar_elevation_deg <= 0:
        # Civil/nautical twilight — linear interpolation to night floor
        # At elevation 0: ~60% of daytime value
        # At elevation -18: night floor
        frac = (solar_elevation_deg + 18) / 18.0  # 0 at -18°, 1 at 0°
        daytime_val = FOF2_NOON_MHZ * 0.6
        return FOF2_NIGHT_FLOOR_MHZ + frac * (daytime_val - FOF2_NIGHT_FLOOR_MHZ)

    # Daytime: Chapman model
    zenith_deg = 90.0 - solar_elevation_deg
    zenith_rad = math.radians(zenith_deg)
    cos_zenith = math.cos(zenith_rad)

    # Clamp to avoid issues near horizon
    cos_zenith = max(cos_zenith, 0.01)

    fof2 = FOF2_NOON_MHZ * (cos_zenith ** 0.25)
    return max(fof2, FOF2_NIGHT_FLOOR_MHZ)


def compute_oblique_muf(fof2_mhz: float, elevation_angle_deg: float) -> float:
    """
    Compute the Maximum Usable Frequency for oblique incidence.

    MUF = foF2 × sec(θ_i)

    where θ_i is the angle of incidence at the ionospheric layer.
    For a flat-earth approximation:
        θ_i ≈ 90° - elevation_angle
    For more accuracy with Earth curvature, we use the secant law.

    Args:
        fof2_mhz: F2 critical frequency in MHz
        elevation_angle_deg: Ray elevation angle at the ground in degrees

    Returns:
        Oblique MUF in MHz
    """
    if elevation_angle_deg <= 0:
        return fof2_mhz  # Grazing — essentially vertical

    if elevation_angle_deg >= 89:
        return fof2_mhz  # Near-vertical incidence

    # Incidence angle at the layer (complement of elevation)
    # With Earth curvature correction for the secant factor
    elev_rad = math.radians(elevation_angle_deg)

    # Simple secant law: MUF = foF2 / sin(elevation)
    # (sin(elev) = cos(incidence) for flat earth, but sec(incidence) = 1/cos(incidence))
    # Actually: incidence angle θ_i at the layer satisfies:
    #   cos(θ_i) = sin(elevation) for flat earth
    #   sec(θ_i) = 1/sin(elevation)
    # With Earth curvature, the factor is slightly larger.

    sin_elev = math.sin(elev_rad)
    if sin_elev < 0.05:
        sin_elev = 0.05  # Cap the secant factor at ~20

    # Earth curvature correction factor
    # For reflection at height h: sec_factor = sqrt(1 + 2*h/R_E) / sin(elev)
    # This is a small correction (~5-10%) but matters for low angles
    h_km = F_LAYER_HEIGHT_KM
    curvature_factor = math.sqrt(1 + 2 * h_km / EARTH_RADIUS_KM)
    sec_factor = curvature_factor / sin_elev

    # Cap at reasonable maximum (MUF can't exceed ~4× foF2 for realistic geometries)
    sec_factor = min(sec_factor, 4.5)

    return fof2_mhz * sec_factor


def hop_elevation_angle(ground_distance_km: float, layer_height_km: float,
                        n_hops: int) -> float:
    """
    Calculate the elevation angle for an N-hop ionospheric path.

    Args:
        ground_distance_km: Great-circle distance
        layer_height_km: Ionospheric layer height
        n_hops: Number of hops

    Returns:
        Elevation angle in degrees (0 = horizon)
    """
    if n_hops <= 0:
        return 0.0

    hop_ground = ground_distance_km / n_hops
    half_hop = hop_ground / 2.0

    if half_hop < 1.0:
        return 89.0  # Nearly vertical

    return math.degrees(math.atan(layer_height_km / half_hop))


def great_circle_distance(lat1: float, lon1: float,
                          lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in km."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


class IonosphericReanalysis:
    """
    Offline reanalysis of L2 timing measurements with ionospheric physics.

    Reads L2 data, applies solar-zenith-aware mode validation and SNR gating,
    re-estimates TEC with cleaned inputs, and writes L3C propagation stats.
    """

    def __init__(self, data_root: Path, receiver_grid: str = 'EM38ww'):
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        self.receiver_grid = receiver_grid
        self.rx_lat, self.rx_lon = grid_to_latlon(receiver_grid)

        # Pre-compute path midpoints and distances
        self.midpoints: Dict[str, Tuple[float, float]] = {}
        self.distances: Dict[str, float] = {}
        for station, (slat, slon) in STATION_COORDS.items():
            mid_lat, mid_lon = calculate_midpoint(self.rx_lat, self.rx_lon, slat, slon)
            self.midpoints[station] = (mid_lat, mid_lon)
            self.distances[station] = great_circle_distance(
                self.rx_lat, self.rx_lon, slat, slon
            )

        self.tec_estimator = TECEstimator()

        # L3C writer for propagation stats
        self.stats_dir = self.phase2_dir / 'science' / 'propagation_stats'
        self.stats_writer = DataProductWriter(
            output_dir=self.stats_dir,
            product_level='L3C',
            product_name='propagation_stats',
            channel='REANALYSIS',
            processing_version='6.0.0',
            station_metadata={'description': 'Ionospheric Reanalysis Service'}
        )

        # L3A writer for reanalyzed TEC
        self.tec_dir = self.phase2_dir / 'science' / 'tec_reanalyzed'
        self.tec_writer = DataProductWriter(
            output_dir=self.tec_dir,
            product_level='L3',
            product_name='tec',
            channel='REANALYZED',
            processing_version='6.0.0',
            station_metadata={'description': 'Ionospheric Reanalysis TEC'}
        )

        logger.info(
            f"IonosphericReanalysis initialized: grid={receiver_grid}, "
            f"distances: " + ", ".join(
                f"{s}={d:.0f}km" for s, d in sorted(self.distances.items())
            )
        )

    def _discover_channels(self) -> List[str]:
        """Discover available L2 channel directories."""
        channels = []
        if self.phase2_dir.exists():
            for subdir in sorted(self.phase2_dir.iterdir()):
                if subdir.is_dir() and subdir.name not in ('fusion', 'science', 'phase2', 'ionex'):
                    if (subdir / 'clock_offset').exists():
                        channels.append(subdir.name)
        return channels

    def _read_l2_measurements(self, start_time: datetime,
                              end_time: datetime) -> List[Dict[str, Any]]:
        """Read all L2 timing measurements in the time range."""
        channels = self._discover_channels()
        all_measurements = []

        start_iso = start_time.isoformat().replace('+00:00', 'Z')
        end_iso = end_time.isoformat().replace('+00:00', 'Z')

        for channel in channels:
            try:
                channel_dir = self.phase2_dir / channel
                reader_dir = channel_dir / 'clock_offset' if (channel_dir / 'clock_offset').exists() else channel_dir

                reader = DataProductReader(
                    data_dir=reader_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel,
                    use_registry=False
                )

                items = reader.read_time_range(start=start_iso, end=end_iso)
                for item in items:
                    item['_channel'] = channel
                all_measurements.extend(items)

            except Exception as e:
                logger.debug(f"Could not read channel {channel}: {e}")
                continue

        logger.info(f"Read {len(all_measurements)} L2 measurements from {len(channels)} channels")
        return all_measurements

    def _validate_measurement(self, m: Dict[str, Any],
                              timestamp_dt: datetime) -> Optional[ReanalyzedMeasurement]:
        """
        Apply ionospheric physics to validate/correct a single measurement.

        Returns ReanalyzedMeasurement with physics annotations, or None if
        the measurement is fundamentally invalid.
        """
        station = m.get('station', '')
        freq_mhz = m.get('frequency_mhz', 0)
        snr_db = m.get('snr_db', 0) or 0
        original_mode = m.get('propagation_mode', 'UNKNOWN') or 'UNKNOWN'
        n_hops = m.get('n_hops', 0) or 0
        raw_toa = m.get('raw_arrival_time_ms')
        prop_delay = m.get('propagation_delay_ms', 0) or 0
        confidence = m.get('confidence', 0) or 0
        quality_flag = m.get('quality_flag', 'MARGINAL')
        tone_detected = m.get('tone_detected', False)

        # Basic validity
        if not station or freq_mhz <= 0:
            return None
        if not tone_detected:
            return None
        if raw_toa is None or np.isnan(raw_toa):
            return None

        # Validate station/frequency combination
        valid_freqs = VALID_STATION_FREQS.get(station)
        if valid_freqs and not any(abs(freq_mhz - vf) < 0.1 for vf in valid_freqs):
            return None

        # Compute solar elevation at path midpoint
        midpoint = self.midpoints.get(station)
        if not midpoint:
            return None
        _, solar_elev = solar_position(timestamp_dt, midpoint[0], midpoint[1])

        # Estimate foF2 from solar elevation
        fof2 = estimate_fof2(solar_elev)

        # Determine the elevation angle for the claimed mode
        distance = self.distances.get(station, 0)
        if n_hops > 0 and 'F' in original_mode.upper():
            layer_height = F_LAYER_HEIGHT_KM
        elif n_hops > 0 and 'E' in original_mode.upper():
            layer_height = E_LAYER_HEIGHT_KM
        else:
            layer_height = F_LAYER_HEIGHT_KM  # Default assumption

        if n_hops > 0:
            elev_angle = hop_elevation_angle(distance, layer_height, n_hops)
        else:
            elev_angle = 0

        # Compute oblique MUF for this mode geometry
        if 'F' in original_mode.upper() and n_hops > 0:
            oblique_muf = compute_oblique_muf(fof2, elev_angle)
        elif 'E' in original_mode.upper() and n_hops > 0:
            # E-layer MUF: foE is roughly foF2 * 0.3 during daytime
            foe = fof2 * 0.3 if solar_elev > 0 else 0.5  # E-layer mostly gone at night
            oblique_muf = compute_oblique_muf(foe, elev_angle)
        else:
            oblique_muf = 999.0  # Unknown mode — don't reject

        # Physics validation: is the frequency below the oblique MUF?
        mode_valid = freq_mhz <= oblique_muf
        rejection_reason = None

        if not mode_valid:
            rejection_reason = (
                f"freq {freq_mhz:.1f} MHz > oblique MUF {oblique_muf:.1f} MHz "
                f"(foF2={fof2:.1f}, elev_angle={elev_angle:.1f}°, "
                f"solar_elev={solar_elev:.1f}°)"
            )

        # Determine validated mode
        if mode_valid:
            validated_mode = original_mode
            validated_n_hops = n_hops
        else:
            # Try to find a valid mode by increasing hop count
            # More hops = steeper angle = higher oblique MUF
            validated_mode = 'REJECTED'
            validated_n_hops = 0
            for try_hops in range(1, 5):
                try_elev = hop_elevation_angle(distance, F_LAYER_HEIGHT_KM, try_hops)
                try_muf = compute_oblique_muf(fof2, try_elev)
                if freq_mhz <= try_muf:
                    validated_mode = f"{try_hops}F2"
                    validated_n_hops = try_hops
                    rejection_reason = None
                    mode_valid = True
                    break

            # If still rejected, check if it could be sporadic E
            if not mode_valid and solar_elev > 0 and snr_db > 20:
                # Strong daytime signal above MUF — possible sporadic E
                validated_mode = 'Es'
                validated_n_hops = 1
                rejection_reason = None
                mode_valid = True

        return ReanalyzedMeasurement(
            timestamp=m.get('timestamp_utc', ''),
            station=station,
            frequency_mhz=freq_mhz,
            snr_db=snr_db,
            original_mode=original_mode,
            original_n_hops=n_hops,
            raw_arrival_time_ms=raw_toa,
            propagation_delay_ms=prop_delay,
            confidence=confidence,
            quality_flag=quality_flag,
            solar_elevation_deg=round(solar_elev, 2),
            estimated_fof2_mhz=round(fof2, 2),
            oblique_muf_mhz=round(oblique_muf, 2),
            mode_physically_valid=mode_valid,
            validated_mode=validated_mode,
            validated_n_hops=validated_n_hops,
            rejection_reason=rejection_reason,
        )

    def _estimate_tec_cleaned(
        self,
        measurements: List[ReanalyzedMeasurement],
        station: str,
        timestamp: float
    ) -> Optional[Dict[str, Any]]:
        """
        Re-estimate TEC using D_clock values from high-SNR, physics-validated
        measurements.

        Strategy: Use D_clock (raw_arrival_time_ms) directly. D_clock already
        has the geometric propagation delay removed per-mode, so any residual
        1/f² pattern across frequencies IS the ionospheric dispersion signal.

        This avoids the mode-mixing problem: even if 2.5 MHz was assigned "2E"
        and 5 MHz was assigned "1F2", both D_clock values represent the same
        thing — timing error after subtracting the mode-specific geometric delay.
        The ionospheric group delay component was NOT removed (the propagation
        model uses a fixed approximation), so the 1/f² residual remains.

        For each frequency, we take the MEDIAN D_clock across all valid
        measurements in the hour (robust to outliers from mode mis-assignment).
        """
        # Filter to valid, high-SNR measurements for this station
        valid = [
            m for m in measurements
            if m.station == station
            and m.mode_physically_valid
            and m.snr_db >= MIN_SNR_TEC_DB
            and m.validated_mode != 'REJECTED'
        ]

        if len(valid) < 2:
            return None

        # Group by frequency, take median D_clock per frequency
        by_freq: Dict[float, List[float]] = defaultdict(list)
        for m in valid:
            key = round(m.frequency_mhz, 1)
            by_freq[key].append(m.raw_arrival_time_ms)

        if len(by_freq) < 2:
            return None

        # Build TEC estimator input using median D_clock per frequency.
        # We add a large constant offset so all values are positive
        # (the TEC estimator fits T_obs = T_vacuum + K*TEC/f², and
        # T_vacuum absorbs the constant offset).
        tec_input = []
        freq_list = []
        for freq_mhz, d_clocks in sorted(by_freq.items()):
            median_dclock = float(np.median(d_clocks))
            n_samples = len(d_clocks)
            # Uncertainty from spread of D_clock values at this frequency
            if n_samples > 1:
                iqr = float(np.percentile(d_clocks, 75) - np.percentile(d_clocks, 25))
                uncertainty = max(0.1, iqr / 1.35)  # IQR to std estimate
            else:
                uncertainty = 1.0
            tec_input.append({
                'frequency_hz': freq_mhz * 1e6,
                'toa_ms': median_dclock,
                'uncertainty_ms': uncertainty,
            })
            freq_list.append(freq_mhz)

        result = self.tec_estimator.estimate_tec(tec_input, station, timestamp)
        if result is None:
            return None

        # Additional validation: reject clearly unphysical TEC
        if result.tec_u < 0 or result.tec_u > 200:
            logger.debug(
                f"TEC rejected for {station}: {result.tec_u:.1f} TECU "
                f"(out of physical range)"
            )
            return None

        # Determine dominant mode from valid measurements
        mode_counts = Counter(m.validated_mode for m in valid)
        dominant_mode = mode_counts.most_common(1)[0][0] if mode_counts else 'UNKNOWN'

        return {
            'station': station,
            'tec_tecu': float(result.tec_u),
            't_vacuum_error_ms': float(result.t_vacuum_error_ms),
            'confidence': float(result.confidence),
            'n_frequencies': len(freq_list),
            'residuals_ms': float(result.residuals_ms),
            'frequencies_mhz': ','.join(f"{f:.2f}" for f in freq_list),
            'propagation_mode': dominant_mode,
            'quality_flag': 'GOOD' if result.confidence > 0.8 and len(freq_list) >= 3 else 'MARGINAL',
        }

    def process_hour(self, hour_start: datetime) -> Dict[str, Any]:
        """
        Process one hour of L2 data and produce reanalyzed products.

        Args:
            hour_start: Start of the hour to process (UTC)

        Returns:
            Summary dict with statistics
        """
        hour_end = hour_start + timedelta(hours=1)
        logger.info(
            f"Reanalyzing {hour_start.strftime('%Y-%m-%d %H:%M')} - "
            f"{hour_end.strftime('%H:%M')} UTC"
        )

        # 1. Read L2 measurements
        raw_measurements = self._read_l2_measurements(hour_start, hour_end)
        if not raw_measurements:
            logger.warning("No L2 measurements found for this hour")
            return {'status': 'no_data', 'n_raw': 0}

        # 2. Validate each measurement with ionospheric physics
        reanalyzed: List[ReanalyzedMeasurement] = []
        n_rejected = 0
        n_reclassified = 0

        for m in raw_measurements:
            ts_str = m.get('timestamp_utc', '')
            try:
                if ts_str.endswith('Z'):
                    ts_str = ts_str[:-1] + '+00:00'
                ts_dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            result = self._validate_measurement(m, ts_dt)
            if result is None:
                continue

            reanalyzed.append(result)

            if not result.mode_physically_valid and result.validated_mode == 'REJECTED':
                n_rejected += 1
            elif result.validated_mode != result.original_mode:
                n_reclassified += 1

        logger.info(
            f"Reanalysis: {len(reanalyzed)} valid, "
            f"{n_rejected} rejected, {n_reclassified} reclassified"
        )

        # 3. Compute per-station, per-frequency propagation stats
        stats_by_broadcast = defaultdict(list)
        for m in reanalyzed:
            key = (m.station, m.frequency_mhz)
            stats_by_broadcast[key].append(m)

        # 4. Re-estimate TEC per station
        hour_ts = hour_start.timestamp()
        tec_results = {}
        for station in set(m.station for m in reanalyzed):
            tec = self._estimate_tec_cleaned(reanalyzed, station, hour_ts)
            if tec:
                tec_results[station] = tec
                logger.info(
                    f"TEC {station}: {tec['tec_tecu']:.1f} TECU "
                    f"(R²={tec['confidence']:.2f}, n_freq={tec['n_frequencies']}, "
                    f"mode={tec['propagation_mode']})"
                )

        # 5. Compute MUF from validated modes
        # Only count broadcasts with: validated F-layer mode, SNR >= threshold, n >= threshold
        import re
        f_layer_pattern = re.compile(r'^\d+F')
        credible_f_freqs = []

        for (station, freq), measurements in stats_by_broadcast.items():
            valid_f = [
                m for m in measurements
                if f_layer_pattern.match(m.validated_mode)
                and m.snr_db >= MIN_SNR_CREDIBLE_DB
                and m.mode_physically_valid
            ]
            if len(valid_f) >= MIN_MEASUREMENTS_CREDIBLE:
                avg_snr = sum(m.snr_db for m in valid_f) / len(valid_f)
                credible_f_freqs.append({
                    'station': station,
                    'frequency_mhz': freq,
                    'avg_snr_db': avg_snr,
                    'n_valid': len(valid_f),
                    'dominant_mode': Counter(m.validated_mode for m in valid_f).most_common(1)[0][0],
                })

        muf_estimate = None
        muf_confidence = 0.0
        if credible_f_freqs:
            highest = max(credible_f_freqs, key=lambda x: x['frequency_mhz'])
            muf_estimate = highest['frequency_mhz'] * 1.15
            # Confidence based on SNR and measurement count
            muf_confidence = min(1.0, highest['avg_snr_db'] / 30.0 * highest['n_valid'] / 10.0)

        # 6. Build per-station/frequency L3C records
        ts_iso = hour_start.isoformat().replace('+00:00', 'Z')
        period_end_iso = hour_end.isoformat().replace('+00:00', 'Z')

        for (station, freq), measurements in stats_by_broadcast.items():
            mode_counts = Counter(m.validated_mode for m in measurements)
            total = len(measurements)
            if total == 0:
                continue

            # Compute mode probabilities
            mode_probs = {
                '1E': 0.0, '1F': 0.0, '2F': 0.0, '3F': 0.0,
                'ground_wave': 0.0, 'unknown': 0.0
            }
            for mode, count in mode_counts.items():
                prob = count / total
                if mode in ('1E', '2E'):
                    mode_probs['1E'] += prob
                elif mode in ('1F2', '1F1'):
                    mode_probs['1F'] += prob
                elif mode == '2F2':
                    mode_probs['2F'] += prob
                elif mode in ('3F2', '4F2'):
                    mode_probs['3F'] += prob
                elif mode == 'ground_wave':
                    mode_probs['ground_wave'] += prob
                elif mode in ('REJECTED', 'Es'):
                    mode_probs['unknown'] += prob
                else:
                    mode_probs['unknown'] += prob

            avg_snr = sum(m.snr_db for m in measurements) / total
            valid_count = sum(1 for m in measurements if m.mode_physically_valid)

            record = {
                'timestamp_utc': period_end_iso,
                'period_start': ts_iso,
                'aggregation_period': 'HOURLY',
                'station': station,
                'frequency_mhz': float(freq),
                'mode_1e_probability': round(mode_probs['1E'], 4),
                'mode_1f_probability': round(mode_probs['1F'], 4),
                'mode_2f_probability': round(mode_probs['2F'], 4),
                'mode_3f_probability': round(mode_probs['3F'], 4),
                'mode_gw_probability': round(mode_probs['ground_wave'], 4),
                'mode_unknown_probability': round(mode_probs['unknown'], 4),
                'estimated_muf_mhz': round(muf_estimate, 2) if muf_estimate else None,
                'muf_confidence': round(muf_confidence, 4) if muf_estimate else None,
                'mean_snr_db': round(avg_snr, 2),
                'n_observations': total,
                'data_completeness': round(valid_count / max(total, 1), 4),
                'quality_flag': 'GOOD' if valid_count >= 40 else ('MARGINAL' if valid_count >= 20 else 'BAD'),
                'processing_version': '6.0.0',
            }

            try:
                self.stats_writer.write_measurement(record)
            except Exception as e:
                logger.error(f"Failed to write L3C stats for {station} {freq}: {e}")

        # 7. Write reanalyzed TEC records
        for station, tec in tec_results.items():
            record = {
                'timestamp_utc': ts_iso,
                'minute_boundary': int(hour_ts),
                'station': station,
                'tec_tecu': tec['tec_tecu'],
                't_vacuum_error_ms': tec['t_vacuum_error_ms'],
                'confidence': tec['confidence'],
                'n_frequencies': tec['n_frequencies'],
                'residuals_ms': tec['residuals_ms'],
                'frequencies_mhz': tec['frequencies_mhz'],
                'quality_flag': tec['quality_flag'],
                'validation_flag': 'UNVALIDATED',
                'propagation_mode': tec['propagation_mode'],
                'processing_version': '6.0.0',
            }
            try:
                self.tec_writer.write_measurement(record)
            except Exception as e:
                logger.error(f"Failed to write reanalyzed TEC for {station}: {e}")

        # 8. Summary
        summary = {
            'status': 'ok',
            'period': ts_iso,
            'n_raw': len(raw_measurements),
            'n_reanalyzed': len(reanalyzed),
            'n_rejected': n_rejected,
            'n_reclassified': n_reclassified,
            'muf_estimate_mhz': round(muf_estimate, 2) if muf_estimate else None,
            'muf_confidence': round(muf_confidence, 4) if muf_estimate else None,
            'tec_stations': {s: round(t['tec_tecu'], 2) for s, t in tec_results.items()},
            'credible_f_layer': [
                f"{c['station']} {c['frequency_mhz']:.1f}MHz ({c['dominant_mode']}, "
                f"SNR={c['avg_snr_db']:.1f}dB, n={c['n_valid']})"
                for c in sorted(credible_f_freqs, key=lambda x: x['frequency_mhz'])
            ],
        }

        logger.info(
            f"Reanalysis complete: MUF={summary['muf_estimate_mhz']} MHz, "
            f"TEC={summary['tec_stations']}, "
            f"{n_rejected} rejected, {n_reclassified} reclassified"
        )

        return summary

    def run_backfill(self, hours_back: int = 1):
        """
        Process the last N hours of data.

        Args:
            hours_back: Number of hours to process (default: 1)
        """
        now = datetime.now(timezone.utc)
        # Align to hour boundary
        current_hour = now.replace(minute=0, second=0, microsecond=0)

        for i in range(hours_back, 0, -1):
            hour_start = current_hour - timedelta(hours=i)
            try:
                summary = self.process_hour(hour_start)
                logger.info(f"Hour {hour_start.strftime('%H:%M')}: {summary.get('status')}")
            except Exception as e:
                logger.error(f"Failed to process hour {hour_start}: {e}", exc_info=True)


def _load_config(config_path: str) -> dict:
    """Load and return the parsed TOML config, or empty dict on failure."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # Python < 3.11
    try:
        with open(config_path, 'rb') as f:
            return tomllib.load(f)
    except Exception as e:
        logger.warning(f"Could not load config {config_path}: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(
        description='Ionospheric Reanalysis Service - offline mode/TEC cleanup'
    )
    parser.add_argument(
        '--config', type=str,
        default='/etc/hf-timestd/timestd-config.toml',
        help='Path to timestd-config.toml'
    )
    parser.add_argument(
        '--data-root', type=str, default=None,
        help='Data root directory (overrides config)'
    )
    parser.add_argument(
        '--grid', type=str, default=None,
        help='Receiver Maidenhead grid square (overrides config)'
    )
    parser.add_argument(
        '--hours', type=int, default=1,
        help='Number of hours to process (default: 1)'
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Enable debug logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config, then apply CLI overrides
    cfg = _load_config(args.config)
    data_root = args.data_root or os.environ.get('TIMESTD_DATA_ROOT') or \
        cfg.get('recorder', {}).get('production_data_root', '/var/lib/timestd')
    grid = args.grid or os.environ.get('TIMESTD_GRID') or \
        cfg.get('station', {}).get('grid_square', '')

    if not grid:
        logger.error("grid not set (provide --grid or set station.grid_square in config)")
        sys.exit(1)

    logger.info(f"Ionospheric Reanalysis starting: data_root={data_root}, "
                f"grid={grid}, hours={args.hours}")

    start_time = time.time()

    reanalysis = IonosphericReanalysis(
        data_root=Path(data_root),
        receiver_grid=grid,
    )
    reanalysis.run_backfill(hours_back=args.hours)

    elapsed = time.time() - start_time
    logger.info(f"Ionospheric Reanalysis complete in {elapsed:.1f}s")


if __name__ == '__main__':
    main()
