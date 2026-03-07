"""
Ionospheric Event Detection Service.

Detects and tracks ionospheric events including:
- Day/Night transitions (sunrise/sunset effects)
- Sporadic-E propagation
- Sudden Ionospheric Disturbances (SIDs)
- Anomalous propagation conditions
- Signal quality events
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging
import math

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


# Event type definitions
EVENT_TYPES = {
    'sunrise': {'icon': '🌅', 'color': '#f59e0b', 'description': 'Sunrise transition'},
    'sunset': {'icon': '🌆', 'color': '#8b5cf6', 'description': 'Sunset transition'},
    'sporadic_e': {'icon': '⚡', 'color': '#ef4444', 'description': 'Sporadic-E detected'},
    'sid': {'icon': '☀️', 'color': '#f97316', 'description': 'Sudden Ionospheric Disturbance'},
    'mode_change': {'icon': '📡', 'color': '#3b82f6', 'description': 'Propagation mode change'},
    'signal_loss': {'icon': '📉', 'color': '#ef4444', 'description': 'Signal loss'},
    'signal_recovery': {'icon': '📈', 'color': '#10b981', 'description': 'Signal recovery'},
    'anomaly': {'icon': '⚠️', 'color': '#f59e0b', 'description': 'Anomalous condition'},
    'multipath': {'icon': '🔀', 'color': '#6366f1', 'description': 'Multipath detected'},
    'scintillation': {'icon': '✨', 'color': '#ec4899', 'description': 'Scintillation event'},
}


class EventService:
    """Service for detecting and tracking ionospheric events."""
    
    def __init__(self, data_root: Path, station_lat: float = 40.0, station_lon: float = -105.0):
        """
        Initialize event service.
        
        Args:
            data_root: Root directory for data products
            station_lat: Station latitude for sunrise/sunset calculation
            station_lon: Station longitude for sunrise/sunset calculation
        """
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        self.station_lat = station_lat
        self.station_lon = station_lon
        
        self.channels = [
            'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
            'WWV_20000', 'WWV_25000', 'CHU_3330', 'CHU_7850', 'CHU_14670'
        ]
    
    def _clean_value(self, val: Any) -> Any:
        """Clean value for JSON serialization."""
        import numpy as np
        
        if val is None:
            return None
        if isinstance(val, (np.floating, np.integer)):
            val = float(val) if isinstance(val, np.floating) else int(val)
        if isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                return None
        return val
    
    def _calculate_sun_times(self, date: datetime) -> Dict[str, datetime]:
        """
        Calculate approximate sunrise/sunset times for the station.
        Uses a simplified calculation - for production, use a proper ephemeris.
        """
        from math import sin, cos, tan, asin, acos, radians, degrees
        
        # Day of year
        doy = date.timetuple().tm_yday
        
        # Approximate solar declination
        declination = -23.45 * cos(radians(360 / 365 * (doy + 10)))
        
        # Hour angle at sunrise/sunset
        lat_rad = radians(self.station_lat)
        decl_rad = radians(declination)
        
        try:
            cos_hour_angle = -tan(lat_rad) * tan(decl_rad)
            if cos_hour_angle > 1:
                # Sun never rises (polar night)
                return {'sunrise': None, 'sunset': None, 'polar_night': True}
            elif cos_hour_angle < -1:
                # Sun never sets (midnight sun)
                return {'sunrise': None, 'sunset': None, 'midnight_sun': True}
            
            hour_angle = degrees(acos(cos_hour_angle))
            
            # Solar noon (approximate, ignoring equation of time)
            solar_noon_utc = 12 - self.station_lon / 15
            
            sunrise_utc = solar_noon_utc - hour_angle / 15
            sunset_utc = solar_noon_utc + hour_angle / 15
            
            # Convert to datetime
            sunrise = date.replace(hour=int(sunrise_utc), minute=int((sunrise_utc % 1) * 60), second=0, microsecond=0)
            sunset = date.replace(hour=int(sunset_utc), minute=int((sunset_utc % 1) * 60), second=0, microsecond=0)
            
            return {'sunrise': sunrise, 'sunset': sunset}
        except:
            return {'sunrise': None, 'sunset': None}
    
    def get_recent_events(self, hours: int = 24) -> Dict[str, Any]:
        """
        Get recent ionospheric events.
        
        Args:
            hours: Number of hours to look back
            
        Returns:
            Dictionary with detected events
        """
        try:
            events = []
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(hours=hours)
            
            # Add sunrise/sunset events
            sun_events = self._detect_sun_events(start_time, end_time)
            events.extend(sun_events)
            
            # Detect signal events from timing data
            signal_events = self._detect_signal_events(start_time, end_time)
            events.extend(signal_events)
            
            # Sort by timestamp descending
            events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            return {
                'events': events,
                'count': len(events),
                'start_time': start_time.isoformat() + 'Z',
                'end_time': end_time.isoformat() + 'Z'
            }
            
        except Exception as e:
            logger.error(f"Error getting events: {e}")
            return {'events': [], 'count': 0, 'error': str(e)}
    
    def _detect_sun_events(self, start: datetime, end: datetime) -> List[Dict]:
        """Detect sunrise/sunset events in the time range."""
        events = []
        
        # Check each day in the range
        current = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while current <= end:
            sun_times = self._calculate_sun_times(current)
            
            if sun_times.get('sunrise') and start <= sun_times['sunrise'] <= end:
                events.append({
                    'type': 'sunrise',
                    'timestamp': sun_times['sunrise'].isoformat() + 'Z',
                    'title': 'Local Sunrise',
                    'description': 'D-layer ionization increasing, HF absorption rising',
                    'icon': EVENT_TYPES['sunrise']['icon'],
                    'color': EVENT_TYPES['sunrise']['color'],
                    'severity': 'info',
                    'details': {
                        'effect': 'Increased D-layer absorption on lower HF frequencies',
                        'expected_duration': '1-2 hours transition'
                    }
                })
            
            if sun_times.get('sunset') and start <= sun_times['sunset'] <= end:
                events.append({
                    'type': 'sunset',
                    'timestamp': sun_times['sunset'].isoformat() + 'Z',
                    'title': 'Local Sunset',
                    'description': 'D-layer recombining, HF absorption decreasing',
                    'icon': EVENT_TYPES['sunset']['icon'],
                    'color': EVENT_TYPES['sunset']['color'],
                    'severity': 'info',
                    'details': {
                        'effect': 'Reduced D-layer absorption, improved low-band propagation',
                        'expected_duration': '1-2 hours transition'
                    }
                })
            
            current += timedelta(days=1)
        
        return events
    
    def _detect_signal_events(self, start: datetime, end: datetime) -> List[Dict]:
        """Detect signal-based events from timing measurements."""
        events = []
        
        for channel in self.channels:
            channel_dir = self.phase2_dir / channel
            if not channel_dir.exists():
                continue
            
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='timing_measurements',
                    channel=channel
                )
                
                measurements = reader.read_time_range(
                    start=start.isoformat() + 'Z',
                    end=end.isoformat() + 'Z'
                )
                
                # Analyze for events
                prev_snr = None
                prev_mode = None
                
                for m in measurements:
                    timestamp = m.get('timestamp_utc')
                    snr = m.get('carrier_snr_db')
                    mode = m.get('propagation_mode')
                    station = m.get('station', 'UNKNOWN')
                    freq = m.get('frequency_mhz')
                    
                    # Detect significant SNR drops (potential SID or signal loss)
                    if prev_snr is not None and snr is not None:
                        snr_change = snr - prev_snr
                        if snr_change < -10:  # 10 dB drop
                            events.append({
                                'type': 'signal_loss',
                                'timestamp': timestamp,
                                'title': f'Signal Drop on {station} {freq} MHz',
                                'description': f'SNR dropped {abs(snr_change):.1f} dB',
                                'icon': EVENT_TYPES['signal_loss']['icon'],
                                'color': EVENT_TYPES['signal_loss']['color'],
                                'severity': 'warning',
                                'details': {
                                    'channel': channel,
                                    'station': station,
                                    'frequency_mhz': freq,
                                    'snr_before': self._clean_value(prev_snr),
                                    'snr_after': self._clean_value(snr)
                                }
                            })
                        elif snr_change > 10:  # 10 dB recovery
                            events.append({
                                'type': 'signal_recovery',
                                'timestamp': timestamp,
                                'title': f'Signal Recovery on {station} {freq} MHz',
                                'description': f'SNR increased {snr_change:.1f} dB',
                                'icon': EVENT_TYPES['signal_recovery']['icon'],
                                'color': EVENT_TYPES['signal_recovery']['color'],
                                'severity': 'info',
                                'details': {
                                    'channel': channel,
                                    'station': station,
                                    'frequency_mhz': freq,
                                    'snr_before': self._clean_value(prev_snr),
                                    'snr_after': self._clean_value(snr)
                                }
                            })
                    
                    # Detect mode changes
                    if prev_mode is not None and mode is not None and prev_mode != mode:
                        events.append({
                            'type': 'mode_change',
                            'timestamp': timestamp,
                            'title': f'Mode Change on {station} {freq} MHz',
                            'description': f'Changed from {prev_mode} to {mode}',
                            'icon': EVENT_TYPES['mode_change']['icon'],
                            'color': EVENT_TYPES['mode_change']['color'],
                            'severity': 'info',
                            'details': {
                                'channel': channel,
                                'station': station,
                                'frequency_mhz': freq,
                                'mode_before': prev_mode,
                                'mode_after': mode
                            }
                        })
                    
                    prev_snr = snr
                    prev_mode = mode
                    
            except Exception as e:
                logger.warning(f"Could not analyze events from {channel}: {e}")
                continue
        
        return events
    
    def get_current_conditions(self) -> Dict[str, Any]:
        """
        Get current ionospheric conditions summary.
        
        Returns:
            Dictionary with current conditions
        """
        try:
            now = datetime.utcnow()
            sun_times = self._calculate_sun_times(now)
            
            # Determine if it's day or night
            is_daytime = False
            if sun_times.get('sunrise') and sun_times.get('sunset'):
                sunrise = sun_times['sunrise']
                sunset = sun_times['sunset']
                is_daytime = sunrise.time() <= now.time() <= sunset.time()
            
            # Get recent events
            recent = self.get_recent_events(hours=6)
            
            # Count events by type
            event_counts = {}
            for event in recent.get('events', []):
                etype = event.get('type', 'unknown')
                event_counts[etype] = event_counts.get(etype, 0) + 1
            
            return {
                'timestamp': now.isoformat() + 'Z',
                'is_daytime': is_daytime,
                'sun_times': {
                    'sunrise': sun_times.get('sunrise').isoformat() + 'Z' if sun_times.get('sunrise') else None,
                    'sunset': sun_times.get('sunset').isoformat() + 'Z' if sun_times.get('sunset') else None
                },
                'recent_event_count': recent.get('count', 0),
                'event_counts_by_type': event_counts,
                'conditions_summary': 'Daytime propagation' if is_daytime else 'Nighttime propagation'
            }
            
        except Exception as e:
            logger.error(f"Error getting conditions: {e}")
            return {'error': str(e)}
