"""
Correlation Service - Analyze relationships between space weather and HF propagation.

Provides correlation analysis between:
- SNR vs Solar Zenith Angle
- SNR vs X-ray flux (SID detection)
- TEC vs F10.7 solar flux
- Propagation mode vs Kp index
"""

import logging
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
from pathlib import Path
from scipy import stats

from services.space_weather_service import SpaceWeatherService
from services.propagation_service import PropagationService
from hf_timestd.core.solar_zenith_calculator import calculate_midpoint, solar_position

logger = logging.getLogger(__name__)


class CorrelationService:
    """Service for analyzing space weather / propagation correlations."""
    
    def __init__(self, data_root: Path, space_weather: Optional[SpaceWeatherService] = None):
        """Initialize correlation service."""
        self.data_root = data_root
        self.space_weather = space_weather if space_weather is not None else SpaceWeatherService()
        self.propagation = PropagationService(data_root)
        logger.info("Correlation Service initialized")
    
    def analyze_snr_solar_zenith(
        self,
        station: str,
        frequency: float,
        start: datetime,
        end: datetime,
        station_coords: Tuple[float, float],
        rx_coords: Tuple[float, float]
    ) -> Dict[str, Any]:
        """
        Analyze correlation between SNR and solar zenith angle.
        
        Args:
            station: Station ID (WWV, WWVH, CHU, BPM)
            frequency: Frequency in MHz
            start: Start time
            end: End time
            station_coords: (lat, lon) of transmitter
            rx_coords: (lat, lon) of receiver
        
        Returns:
            Correlation analysis results
        """
        # Get propagation timeline for this station/frequency
        timeline = self.propagation.get_mode_timeline(start, end, station=station)
        
        if not timeline or not timeline.get('timestamps'):
            return {'error': 'No propagation data available'}
        
        # Filter for matching frequency
        timestamps = []
        snr_values = []
        for i, (ts, freq, snr) in enumerate(zip(
            timeline.get('timestamps', []),
            timeline.get('frequencies', []),
            timeline.get('snr_db', [])
        )):
            if freq is not None and abs(freq - frequency) < 0.1:
                timestamps.append(ts)
                snr_values.append(snr)
        
        if not timestamps or not snr_values:
            return {'error': f'No data for {station} {frequency} MHz'}
        
        # Calculate solar zenith angles at path midpoint
        tx_lat, tx_lon = station_coords
        rx_lat, rx_lon = rx_coords
        mid_lat, mid_lon = calculate_midpoint(rx_lat, rx_lon, tx_lat, tx_lon)
        
        solar_elevations = []
        solar_zenith_angles = []
        
        for ts_str in timestamps:
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', ''))
                az, el = solar_position(ts, mid_lat, mid_lon)
                solar_elevations.append(el)
                solar_zenith_angles.append(90 - el)  # Zenith = 90 - elevation
            except Exception as e:
                logger.warning(f"Error calculating solar position: {e}")
                solar_elevations.append(None)
                solar_zenith_angles.append(None)
        
        # Filter out None values
        valid_pairs = [
            (snr, sza) for snr, sza in zip(snr_values, solar_zenith_angles)
            if snr is not None and sza is not None
        ]
        
        if len(valid_pairs) < 10:
            return {'error': 'Insufficient valid data points'}
        
        snr_clean, sza_clean = zip(*valid_pairs)
        
        # Calculate correlation
        correlation, p_value = stats.pearsonr(sza_clean, snr_clean)
        
        # Fit linear model: SNR = a * SZA + b
        slope, intercept, r_value, p_val, std_err = stats.linregress(sza_clean, snr_clean)
        
        result = {
            'station': station,
            'frequency_mhz': frequency,
            'period': {
                'start': start.isoformat() + 'Z',
                'end': end.isoformat() + 'Z'
            },
            'path_midpoint': {
                'lat': mid_lat,
                'lon': mid_lon
            },
            'statistics': {
                'n_points': len(valid_pairs),
                'correlation': round(correlation, 3),
                'p_value': round(p_value, 6),
                'r_squared': round(r_value**2, 3),
                'slope': round(slope, 3),
                'intercept': round(intercept, 1),
                'std_error': round(std_err, 3)
            },
            'data': {
                'timestamps': timestamps,
                'snr_db': snr_values,
                'solar_elevation_deg': solar_elevations,
                'solar_zenith_angle_deg': solar_zenith_angles
            },
            'interpretation': self._interpret_snr_solar_correlation(correlation, slope)
        }
        
        return result
    
    def _interpret_snr_solar_correlation(self, correlation: float, slope: float) -> str:
        """Interpret SNR-solar correlation results."""
        if abs(correlation) < 0.3:
            return "Weak correlation - propagation may be dominated by other factors"
        elif correlation < -0.5:
            return "Strong negative correlation - SNR decreases as sun gets higher (unusual, check for interference)"
        elif correlation > 0.5:
            return "Strong positive correlation - SNR increases with solar illumination (expected for F-layer propagation)"
        else:
            return "Moderate correlation - solar angle is one of several factors affecting SNR"
    
    def detect_sid_correlation(
        self,
        start: datetime,
        end: datetime
    ) -> Dict[str, Any]:
        """
        Detect correlation between X-ray flares and SNR drops (SID events).
        
        Args:
            start: Start time
            end: End time
        
        Returns:
            Detected SID events with X-ray correlation
        """
        # Get X-ray data
        hours = int((end - start).total_seconds() / 3600)
        xray_data = self.space_weather.get_xray_flux(hours=hours)
        
        # Get propagation timeline for all stations
        timeline = self.propagation.get_mode_timeline(start, end, station=None)
        
        if not timeline or 'channels' not in timeline:
            return {'error': 'No propagation data available'}
        
        # Detect SID events from space weather
        sid_events = self.space_weather.detect_sid_events(hours=hours)
        
        # For each SID event, find corresponding SNR drops
        correlated_events = []
        
        for sid in sid_events:
            sid_time = datetime.fromisoformat(sid['timestamp'].replace('Z', ''))
            
            # Look for SNR drops within ±30 minutes
            window_start = sid_time - timedelta(minutes=30)
            window_end = sid_time + timedelta(minutes=30)
            
            affected_channels = []
            
            for channel in timeline['channels']:
                if not channel.get('snr_db'):
                    continue
                
                # Find SNR values in time window
                snr_drops = []
                for i, ts_str in enumerate(channel['timestamps']):
                    ts = datetime.fromisoformat(ts_str.replace('Z', ''))
                    if window_start <= ts <= window_end:
                        if i > 0 and channel['snr_db'][i] is not None:
                            # Check for drop relative to previous value
                            prev_snr = channel['snr_db'][i-1]
                            curr_snr = channel['snr_db'][i]
                            if prev_snr is not None and curr_snr < prev_snr - 5:
                                snr_drops.append({
                                    'timestamp': ts_str,
                                    'snr_drop_db': round(prev_snr - curr_snr, 1)
                                })
                
                if snr_drops:
                    affected_channels.append({
                        'station': channel['station'],
                        'frequency_mhz': channel['frequency_mhz'],
                        'snr_drops': snr_drops,
                        'max_drop_db': max(d['snr_drop_db'] for d in snr_drops)
                    })
            
            if affected_channels:
                correlated_events.append({
                    'sid_timestamp': sid['timestamp'],
                    'xray_class': sid['xray_class'],
                    'peak_flux': sid['peak_flux'],
                    'affected_channels': affected_channels,
                    'correlation_confidence': sid['confidence']
                })
        
        result = {
            'period': {
                'start': start.isoformat() + 'Z',
                'end': end.isoformat() + 'Z'
            },
            'sid_events_detected': len(sid_events),
            'correlated_events': len(correlated_events),
            'events': correlated_events
        }
        
        return result
    
    def analyze_tec_f107_correlation(
        self,
        station: str,
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Analyze correlation between TEC and F10.7 solar flux.
        
        Args:
            station: Station ID
            days: Number of days to analyze
        
        Returns:
            TEC-F10.7 correlation analysis
        """
        # Get TEC data
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        
        tec_summary = self.propagation.get_tec_summary(start, end)
        
        if not tec_summary or 'paths' not in tec_summary:
            return {'error': 'No TEC data available'}
        
        if station not in tec_summary['paths']:
            return {'error': f'No TEC data for station {station}'}
        
        path_data = tec_summary['paths'][station]
        
        # Get F10.7 data (placeholder - not yet implemented in space weather service)
        # For now, return structure without F10.7 correlation
        
        result = {
            'station': station,
            'period': {
                'start': start.isoformat() + 'Z',
                'end': end.isoformat() + 'Z',
                'days': days
            },
            'tec_statistics': {
                'mean': path_data.get('mean_tec'),
                'min': path_data.get('min_tec'),
                'max': path_data.get('max_tec'),
                'n_points': len(path_data.get('tec_tecu', []))
            },
            'f107_correlation': {
                'status': 'not_implemented',
                'message': 'F10.7 data ingestion not yet implemented'
            }
        }
        
        return result
    
    def analyze_propagation_mode_kp(
        self,
        start: datetime,
        end: datetime
    ) -> Dict[str, Any]:
        """
        Analyze relationship between propagation modes and Kp index.
        
        High Kp (geomagnetic storms) should correlate with:
        - Increased auroral absorption (high-latitude paths like CHU)
        - Mode changes (E-layer enhancement, F-layer irregularities)
        
        Args:
            start: Start time
            end: End time
        
        Returns:
            Propagation mode vs Kp analysis
        """
        # Get Kp data
        hours = int((end - start).total_seconds() / 3600)
        kp_data = self.space_weather.get_kp_index(hours=hours)
        
        # Get propagation timeline
        timeline = self.propagation.get_mode_timeline(start, end, station=None)
        
        if not timeline or 'channels' not in timeline:
            return {'error': 'No propagation data available'}
        
        if not kp_data:
            return {'error': 'No Kp data available'}
        
        # Bin data by Kp level
        kp_bins = {
            'quiet': {'kp_range': [0, 3], 'channels': []},
            'unsettled': {'kp_range': [3, 5], 'channels': []},
            'storm': {'kp_range': [5, 10], 'channels': []}
        }
        
        # For each channel, calculate average SNR and mode distribution by Kp level
        for channel in timeline['channels']:
            if not channel.get('snr_db'):
                continue
            
            station = channel['station']
            frequency = channel['frequency_mhz']
            
            # Bin SNR values by concurrent Kp
            for bin_name, bin_data in kp_bins.items():
                snr_in_bin = []
                
                for i, ts_str in enumerate(channel['timestamps']):
                    ts = datetime.fromisoformat(ts_str.replace('Z', ''))
                    
                    # Find closest Kp measurement
                    closest_kp = None
                    min_delta = timedelta(hours=24)
                    
                    for kp_meas in kp_data:
                        kp_ts = datetime.strptime(kp_meas.timestamp, '%Y-%m-%d %H:%M:%S.%f')
                        delta = abs(ts - kp_ts)
                        if delta < min_delta:
                            min_delta = delta
                            closest_kp = kp_meas.kp_index
                    
                    if closest_kp is not None:
                        kp_min, kp_max = bin_data['kp_range']
                        if kp_min <= closest_kp < kp_max:
                            if channel['snr_db'][i] is not None:
                                snr_in_bin.append(channel['snr_db'][i])
                
                if snr_in_bin:
                    bin_data['channels'].append({
                        'station': station,
                        'frequency_mhz': frequency,
                        'mean_snr_db': round(np.mean(snr_in_bin), 1),
                        'std_snr_db': round(np.std(snr_in_bin), 1),
                        'n_points': len(snr_in_bin)
                    })
        
        result = {
            'period': {
                'start': start.isoformat() + 'Z',
                'end': end.isoformat() + 'Z'
            },
            'kp_bins': kp_bins,
            'interpretation': self._interpret_kp_effects(kp_bins)
        }
        
        return result
    
    def _interpret_kp_effects(self, kp_bins: Dict) -> str:
        """Interpret Kp effects on propagation."""
        quiet_count = len(kp_bins['quiet']['channels'])
        storm_count = len(kp_bins['storm']['channels'])
        
        if storm_count == 0:
            return "No geomagnetic storm data available for comparison"
        
        # Compare average SNR between quiet and storm conditions
        quiet_snrs = [ch['mean_snr_db'] for ch in kp_bins['quiet']['channels']]
        storm_snrs = [ch['mean_snr_db'] for ch in kp_bins['storm']['channels']]
        
        if quiet_snrs and storm_snrs:
            quiet_avg = np.mean(quiet_snrs)
            storm_avg = np.mean(storm_snrs)
            diff = quiet_avg - storm_avg
            
            if diff > 5:
                return f"Geomagnetic storms degrade SNR by ~{diff:.1f} dB on average"
            elif diff < -5:
                return f"Geomagnetic storms enhance SNR by ~{abs(diff):.1f} dB (unusual)"
            else:
                return "Geomagnetic storms have minimal effect on average SNR"
        
        return "Insufficient data for interpretation"
