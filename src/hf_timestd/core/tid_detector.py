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
- Compute cross-correlation between path pairs
- Detect peaks at non-zero lag indicating TID passage
- Estimate TID parameters from correlation structure

================================================================================
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# Physical constants
EARTH_RADIUS_KM = 6371.0


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
        sample_interval_seconds: float = 60.0
    ):
        """
        Initialize TID detector.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            buffer_minutes: Length of residual buffer (default 2 hours)
            min_correlation: Minimum correlation for TID detection
            min_lag_minutes: Minimum lag to consider (excludes zero-lag)
            sample_interval_seconds: Expected sample interval
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.buffer_minutes = buffer_minutes
        self.min_correlation = min_correlation
        self.min_lag_minutes = min_lag_minutes
        self.sample_interval_seconds = sample_interval_seconds
        
        # Rolling buffers of residuals per path
        # Key: (station, frequency_mhz)
        self._residual_buffers: Dict[Tuple[str, float], List[PathResidual]] = defaultdict(list)
        
        # Path geometry (computed on first residual)
        self._path_azimuths: Dict[Tuple[str, float], float] = {}
        self._path_distances: Dict[Tuple[str, float], float] = {}
        
        # Detected events
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
                   f"min_corr={min_correlation}, min_lag={min_lag_minutes}min")
    
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
        import math
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
        import math
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
        
        Returns:
            TIDEvent if detected, None otherwise
        """
        paths = list(self._residual_buffers.keys())
        
        if len(paths) < 2:
            return None  # Need at least 2 paths for cross-correlation
        
        # Get aligned residual time series for each path
        aligned_series = self._align_residuals(paths)
        
        if aligned_series is None or len(aligned_series) < 2:
            return None
        
        # Compute cross-correlations between all path pairs
        best_correlation = 0.0
        best_lag = 0
        best_pair = None
        
        path_keys = list(aligned_series.keys())
        for i in range(len(path_keys)):
            for j in range(i + 1, len(path_keys)):
                path1, path2 = path_keys[i], path_keys[j]
                series1 = aligned_series[path1]
                series2 = aligned_series[path2]
                
                if len(series1) < 10 or len(series2) < 10:
                    continue
                
                # Cross-correlation
                corr, lag = self._cross_correlate(series1, series2)
                
                # Check if this is a TID signature
                # TID: high correlation at non-zero lag
                lag_minutes = abs(lag) * self.sample_interval_seconds / 60.0
                
                if corr > best_correlation and lag_minutes >= self.min_lag_minutes:
                    best_correlation = corr
                    best_lag = lag
                    best_pair = (path1, path2)
        
        # Check if we found a TID
        if best_correlation < self.min_correlation or best_pair is None:
            return None
        
        # Create TID event
        lag_minutes = best_lag * self.sample_interval_seconds / 60.0
        
        # Determine leading/lagging path
        if best_lag > 0:
            leading_path = f"{best_pair[0][0]}@{best_pair[0][1]}MHz"
            lagging_path = f"{best_pair[1][0]}@{best_pair[1][1]}MHz"
        else:
            leading_path = f"{best_pair[1][0]}@{best_pair[1][1]}MHz"
            lagging_path = f"{best_pair[0][0]}@{best_pair[0][1]}MHz"

        # P3-B: 3D TDOA TID Velocity/Direction
        # Find all paths that correlate well with the best path to form an array
        correlated_paths = [best_pair[0]]
        for path in path_keys:
            if path != best_pair[0]:
                corr, _ = self._cross_correlate(aligned_series[best_pair[0]], aligned_series[path])
                if corr >= self.min_correlation * 0.8:  # Slightly lower threshold for array inclusion
                    correlated_paths.append(path)

        velocity_m_s = None
        direction_deg = None

        if len(correlated_paths) >= 3:
            # We have enough paths to solve TDOA unambiguously
            v_tdoa, dir_tdoa = self._solve_tdoa_velocity(correlated_paths, aligned_series)
            if v_tdoa is not None and dir_tdoa is not None:
                velocity_m_s = v_tdoa
                direction_deg = dir_tdoa
                logger.info(f"Resolved TID via TDOA ({len(correlated_paths)} paths): {velocity_m_s:.0f} m/s @ {direction_deg:.0f}°")

        if velocity_m_s is None or direction_deg is None:
            # Fallback to 2-path geometry estimation
            velocity_m_s = self._estimate_tid_velocity(best_pair, abs(lag_minutes))
            direction_deg = self._estimate_tid_direction(best_pair, best_lag)
        
        # Estimate period from autocorrelation
        period_minutes = self._estimate_period(aligned_series[best_pair[0]])
        
        # Estimate amplitude
        amplitude_ms = np.std(aligned_series[best_pair[0]])
        
        event = TIDEvent(
            start_time=datetime.now(timezone.utc),
            period_minutes=period_minutes,
            amplitude_ms=amplitude_ms,
            velocity_m_s=velocity_m_s,
            direction_deg=direction_deg,
            correlation_coefficient=best_correlation,
            n_paths_correlated=2,
            confidence=min(1.0, best_correlation * 1.2),
            leading_path=leading_path,
            lagging_path=lagging_path,
            lag_minutes=abs(lag_minutes)
        )
        
        logger.info(f"TID detected: corr={best_correlation:.2f}, "
                   f"lag={lag_minutes:.1f}min, vel={velocity_m_s:.0f}m/s, "
                   f"dir={direction_deg:.0f}°")
        
        return event
    
    def _align_residuals(
        self,
        paths: List[Tuple[str, float]]
    ) -> Optional[Dict[Tuple[str, float], np.ndarray]]:
        """
        Align residual time series to common time grid.
        
        Returns dict of path -> residual array, or None if insufficient data.
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
        
        # Interpolate each path to common grid
        aligned = {}
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
            
            # Detrend (remove linear trend)
            aligned_values = aligned_values - np.polyval(
                np.polyfit(np.arange(len(aligned_values)), aligned_values, 1),
                np.arange(len(aligned_values))
            )
            
            aligned[path] = aligned_values
        
        return aligned if len(aligned) >= 2 else None
    
    def _cross_correlate(
        self,
        series1: np.ndarray,
        series2: np.ndarray
    ) -> Tuple[float, int]:
        """
        Compute normalized cross-correlation and find peak lag.
        
        Returns:
            (max_correlation, lag_at_max) where correlation is absolute value
        """
        # Normalize
        s1 = (series1 - np.mean(series1)) / (np.std(series1) + 1e-10)
        s2 = (series2 - np.mean(series2)) / (np.std(series2) + 1e-10)
        
        # Cross-correlation
        corr = np.correlate(s1, s2, mode='full')
        corr = corr / len(s1)  # Normalize
        
        # Find peak (excluding small lags near zero)
        center = len(corr) // 2
        min_lag_samples = int(self.min_lag_minutes * 60 / self.sample_interval_seconds)
        
        # Search for peak outside the exclusion zone
        best_corr = 0.0
        best_lag = 0
        
        for i in range(len(corr)):
            lag = i - center
            if abs(lag) >= min_lag_samples:
                if abs(corr[i]) > abs(best_corr):
                    best_corr = corr[i]
                    best_lag = lag
        
        return float(abs(best_corr)), int(best_lag)
    

    def _compute_pierce_point(self, station: str) -> Tuple[float, float]:
        if station not in self._station_locations:
            return self.receiver_lat, self.receiver_lon
        
        st_lat, st_lon = self._station_locations[station]
        import math
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
        import math
        R = 6371.0
        lat_rad, lon_rad = math.radians(lat), math.radians(lon)
        ref_lat_rad, ref_lon_rad = math.radians(self.receiver_lat), math.radians(self.receiver_lon)
        d_lat = lat_rad - ref_lat_rad
        d_lon = lon_rad - ref_lon_rad
        y = d_lat * R
        x = d_lon * R * math.cos(ref_lat_rad)
        return x, y

    def _solve_tdoa_velocity(
        self,
        correlated_paths: List[Tuple[str, float]],
        aligned_series: Dict[Tuple[str, float], np.ndarray]
    ) -> Tuple[Optional[float], Optional[float]]:
        import itertools
        import math
        
        if len(correlated_paths) < 3:
            return None, None
            
        A = []
        B = []
        
        points = []
        for p in correlated_paths:
            station = p[0]
            lat, lon = self._compute_pierce_point(station)
            x, y = self._get_enu_coords(lat, lon)
            points.append((x, y))
            
        for i, j in itertools.combinations(range(len(correlated_paths)), 2):
            p1, p2 = correlated_paths[i], correlated_paths[j]
            corr, lag_samples = self._cross_correlate(aligned_series[p1], aligned_series[p2])
            
            dt_seconds = lag_samples * self.sample_interval_seconds
            
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            
            A.append([dx, dy])
            B.append(dt_seconds)
            
        A = np.array(A)
        B = np.array(B)
        
        try:
            sol, _, _, _ = np.linalg.lstsq(A, B, rcond=None)
            sx, sy = sol
            
            v_km_s = 1.0 / np.sqrt(sx**2 + sy**2)
            az_rad = math.atan2(sx, sy)
            az_deg = (math.degrees(az_rad)) % 360
            
            return v_km_s * 1000.0, az_deg
        except Exception:
            return None, None
    def _estimate_tid_velocity(
        self,
        path_pair: Tuple[Tuple[str, float], Tuple[str, float]],
        lag_minutes: float
    ) -> float:
        """Estimate TID velocity from path geometry and lag."""
        if lag_minutes <= 0:
            return 0.0
        
        path1, path2 = path_pair
        
        # Get path midpoints (approximate ionospheric pierce points)
        # For simplicity, use path azimuths to estimate separation
        az1 = self._path_azimuths.get(path1, 0)
        az2 = self._path_azimuths.get(path2, 0)
        
        # Angular separation
        delta_az = abs(az1 - az2)
        if delta_az > 180:
            delta_az = 360 - delta_az
        
        # Approximate separation at ionospheric height (~300 km)
        # This is a rough estimate - proper calculation needs pierce point geometry
        iono_height_km = 300.0
        separation_km = 2 * iono_height_km * np.sin(np.radians(delta_az / 2))
        
        # Velocity = distance / time
        velocity_m_s = (separation_km * 1000) / (lag_minutes * 60)
        
        return velocity_m_s
    
    def _estimate_tid_direction(
        self,
        path_pair: Tuple[Tuple[str, float], Tuple[str, float]],
        lag: int
    ) -> float:
        """Estimate TID propagation direction from which path leads."""
        path1, path2 = path_pair
        
        az1 = self._path_azimuths.get(path1, 0)
        az2 = self._path_azimuths.get(path2, 0)
        
        # TID travels from leading path toward lagging path
        if lag > 0:
            # path1 leads, TID coming from az1 direction
            direction = az1
        else:
            # path2 leads
            direction = az2
        
        return direction
    
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
