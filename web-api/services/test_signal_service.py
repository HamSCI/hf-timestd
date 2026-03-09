"""
Test Signal Data Service.

Provides access to WWV/WWVH scientific test signal analysis:
- Channel characterization (delay spread, coherence time)
- Multi-tone power measurements
- Frequency selectivity
- Anomaly detection
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging
import math

sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader
from hf_timestd.core.solar_zenith_calculator import (
    solar_position, calculate_midpoint
)
from hf_timestd.core.wwv_constants import (
    WWV_LAT, WWV_LON, WWVH_LAT, WWVH_LON
)

logger = logging.getLogger(__name__)


class TestSignalService:
    """Service for accessing test signal analysis data."""
    
    # Valid broadcast frequencies for each station (MHz)
    # WWV broadcasts on: 2.5, 5, 10, 15, 20, 25 MHz
    # WWVH broadcasts on: 2.5, 5, 10, 15 MHz (does NOT broadcast on 20 or 25 MHz)
    VALID_STATION_FREQUENCIES = {
        'WWV': [2.5, 5.0, 10.0, 15.0, 20.0, 25.0],
        'WWVH': [2.5, 5.0, 10.0, 15.0],  # No 20 or 25 MHz!
    }
    
    def _is_valid_measurement(self, measurement: Dict[str, Any]) -> bool:
        """
        Check if measurement has a valid station/frequency combination.
        
        Filters out spurious detections like WWVH on 20/25 MHz which are
        impossible since WWVH does not broadcast on those frequencies.
        """
        station = measurement.get('station')
        freq_mhz = measurement.get('frequency_mhz')
        
        if station is None or freq_mhz is None:
            return False
        
        valid_freqs = self.VALID_STATION_FREQUENCIES.get(station)
        if valid_freqs is None:
            # Unknown station, allow it through
            return True
        
        # Use tolerance-based comparison (handles int vs float mismatch)
        return any(abs(freq_mhz - vf) < 0.1 for vf in valid_freqs)
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        
        # Channels that receive test signals (WWV at minute 8, WWVH at minute 44)
        self.channels = [
            'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
            'WWV_20000', 'WWV_25000'
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
    
    def _convert_to_native(self, obj: Any) -> Any:
        """Convert numpy types to native Python types."""
        import numpy as np
        
        if isinstance(obj, dict):
            return {k: self._convert_to_native(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._convert_to_native(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            return [self._convert_to_native(item) for item in obj.tolist()]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            val = float(obj)
            return None if (math.isnan(val) or math.isinf(val)) else val
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, float):
            return None if (math.isnan(obj) or math.isinf(obj)) else obj
        else:
            return obj
    
    def get_latest(self) -> Dict[str, Any]:
        """
        Get latest test signal results.
        
        Returns:
            Dictionary with latest test signal analysis per frequency
        """
        try:
            results = []
            
            for channel in self.channels:
                channel_dir = self.phase2_dir / channel
                if not channel_dir.exists():
                    continue
                
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='test_signal',
                        channel=channel
                    )
                    
                    # Get last 24 hours (test signals are hourly)
                    end_time = datetime.utcnow()
                    start_time = end_time - timedelta(hours=24)
                    
                    measurements = reader.read_time_range(
                        start=start_time.isoformat() + 'Z',
                        end=end_time.isoformat() + 'Z'
                    )
                    
                    for m in measurements:
                        if m.get('detected'):
                            record = {
                                'timestamp': m.get('timestamp_utc'),
                                'station': m.get('station'),
                                'frequency_mhz': m.get('frequency_mhz'),
                                'channel': channel,
                                'detected': True,
                                'detection_confidence': self._clean_value(m.get('detection_confidence')),
                                'snr_db': self._clean_value(m.get('snr_db')),
                                'effective_snr_db': self._clean_value(m.get('effective_snr_db')),
                                'delay_spread_ms': self._clean_value(m.get('delay_spread_ms')),
                                'coherence_time_sec': self._clean_value(m.get('coherence_time_sec')),
                                'frequency_selectivity_db': self._clean_value(m.get('frequency_selectivity_db')),
                                'channel_quality': m.get('channel_quality'),
                                'multipath_detected': m.get('multipath_detected'),
                                'scintillation_index': self._clean_value(m.get('scintillation_index')),
                                's4_2khz': self._clean_value(m.get('s4_2khz')),
                                's4_3khz': self._clean_value(m.get('s4_3khz')),
                                's4_4khz': self._clean_value(m.get('s4_4khz')),
                                's4_5khz': self._clean_value(m.get('s4_5khz')),
                                's4_frequency_slope': self._clean_value(m.get('s4_frequency_slope')),
                                'noise_toa_offset_ms': self._clean_value(m.get('noise_toa_offset_ms')),
                                'noise_correlation_peak': self._clean_value(m.get('noise_correlation_peak')),
                                'anomaly_detected': m.get('anomaly_detected'),
                                'anomaly_type': m.get('anomaly_type'),
                                'multitone_score': self._clean_value(m.get('multitone_score')),
                                'chirp_score': self._clean_value(m.get('chirp_score')),
                                'burst_score': self._clean_value(m.get('burst_score')),
                                'tone_power_2khz_db': self._clean_value(m.get('tone_power_2khz_db')),
                                'tone_power_3khz_db': self._clean_value(m.get('tone_power_3khz_db')),
                                'tone_power_4khz_db': self._clean_value(m.get('tone_power_4khz_db')),
                                'tone_power_5khz_db': self._clean_value(m.get('tone_power_5khz_db'))
                            }
                            # Filter out invalid station/frequency combinations
                            # (e.g., WWVH on 20/25 MHz is impossible)
                            if self._is_valid_measurement(record):
                                results.append(record)
                            
                except Exception as e:
                    logger.warning(f"Could not read test signal from {channel}: {e}")
                    continue
            
            # Sort by timestamp descending
            results.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            
            # Group by frequency for latest per frequency
            latest_by_freq = {}
            for r in results:
                freq = r.get('frequency_mhz')
                if freq and freq not in latest_by_freq:
                    latest_by_freq[freq] = r
            
            return self._convert_to_native({
                'latest_by_frequency': latest_by_freq,
                'all_recent': results[:20],  # Last 20 results
                'n_results': len(results),
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            })
            
        except Exception as e:
            logger.error(f"Error getting test signal data: {e}")
            return {'latest_by_frequency': {}, 'all_recent': [], 'n_results': 0, 'error': str(e)}
    
    def get_channel_summary(self) -> Dict[str, Any]:
        """
        Get channel characterization summary across all frequencies.
        
        Returns:
            Summary of channel conditions per frequency
        """
        try:
            data = self.get_latest()
            
            summary = {
                'frequencies': [],
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }
            
            for freq, result in sorted(data.get('latest_by_frequency', {}).items()):
                summary['frequencies'].append({
                    'frequency_mhz': freq,
                    'station': result.get('station'),
                    'channel_quality': result.get('channel_quality'),
                    'snr_db': result.get('snr_db'),
                    'delay_spread_ms': result.get('delay_spread_ms'),
                    'coherence_time_sec': result.get('coherence_time_sec'),
                    'multipath_detected': result.get('multipath_detected'),
                    'last_update': result.get('timestamp')
                })
            
            return self._convert_to_native(summary)
            
        except Exception as e:
            logger.error(f"Error getting channel summary: {e}")
            return {'frequencies': [], 'error': str(e)}
    
    def get_history(
        self,
        start: datetime,
        end: datetime,
        frequency_mhz: Optional[float] = None,
        station: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get test signal history with full metrics.
        
        Args:
            start: Start datetime
            end: End datetime
            frequency_mhz: Optional frequency filter
            station: Optional station filter (WWV or WWVH)
            
        Returns:
            Time series of test signal results with all metrics
        """
        try:
            results = []
            
            for channel in self.channels:
                channel_dir = self.phase2_dir / channel
                if not channel_dir.exists():
                    continue
                
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='test_signal',
                        channel=channel
                    )
                    
                    measurements = reader.read_time_range(
                        start=start.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                        end=end.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                    )
                    
                    for m in measurements:
                        if frequency_mhz and m.get('frequency_mhz') != frequency_mhz:
                            continue
                        if station and m.get('station') != station:
                            continue
                        if m.get('detected'):
                            record = {
                                'timestamp': m.get('timestamp_utc'),
                                'station': m.get('station'),
                                'frequency_mhz': m.get('frequency_mhz'),
                                'channel': channel,
                                'snr_db': self._clean_value(m.get('snr_db')),
                                'effective_snr_db': self._clean_value(m.get('effective_snr_db')),
                                'delay_spread_ms': self._clean_value(m.get('delay_spread_ms')),
                                'coherence_time_sec': self._clean_value(m.get('coherence_time_sec')),
                                'frequency_selectivity_db': self._clean_value(m.get('frequency_selectivity_db')),
                                'channel_quality': m.get('channel_quality'),
                                'multipath_detected': m.get('multipath_detected'),
                                'scintillation_index': self._clean_value(m.get('scintillation_index')),
                                's4_2khz': self._clean_value(m.get('s4_2khz')),
                                's4_3khz': self._clean_value(m.get('s4_3khz')),
                                's4_4khz': self._clean_value(m.get('s4_4khz')),
                                's4_5khz': self._clean_value(m.get('s4_5khz')),
                                's4_frequency_slope': self._clean_value(m.get('s4_frequency_slope')),
                                'noise_toa_offset_ms': self._clean_value(m.get('noise_toa_offset_ms')),
                                'noise_correlation_peak': self._clean_value(m.get('noise_correlation_peak')),
                                'multitone_score': self._clean_value(m.get('multitone_score')),
                                'chirp_score': self._clean_value(m.get('chirp_score')),
                                'burst_score': self._clean_value(m.get('burst_score')),
                                'detection_confidence': self._clean_value(m.get('detection_confidence')),
                                'anomaly_detected': m.get('anomaly_detected'),
                                'anomaly_type': m.get('anomaly_type'),
                                'tone_power_2khz_db': self._clean_value(m.get('tone_power_2khz_db')),
                                'tone_power_3khz_db': self._clean_value(m.get('tone_power_3khz_db')),
                                'tone_power_4khz_db': self._clean_value(m.get('tone_power_4khz_db')),
                                'tone_power_5khz_db': self._clean_value(m.get('tone_power_5khz_db')),
                                'field_strength_db': self._clean_value(m.get('field_strength_db'))
                            }
                            # Filter out invalid station/frequency combinations
                            if self._is_valid_measurement(record):
                                results.append(record)
                            
                except Exception as e:
                    logger.warning(f"Could not read from {channel}: {e}")
                    continue
            
            results.sort(key=lambda x: x.get('timestamp', ''))
            
            return self._convert_to_native({
                'measurements': results,
                'count': len(results)
            })
            
        except Exception as e:
            logger.error(f"Error getting test signal history: {e}")
            return {'measurements': [], 'count': 0, 'error': str(e)}
    
    def _compute_solar_zenith(
        self, start: datetime, end: datetime, interval_minutes: int = 15
    ) -> Optional[Dict[str, Any]]:
        """
        Compute solar elevation at WWV and WWVH path midpoints.
        
        Uses receiver lat/lon from config and transmitter coordinates
        from wwv_constants to find the path midpoint, then computes
        solar elevation every interval_minutes.
        
        Returns:
            Dict with timestamps, wwv_elevation_deg, wwvh_elevation_deg,
            or None on error.
        """
        try:
            from config import config
            rx_lat = config.station_metadata.get('latitude', 0.0)
            rx_lon = config.station_metadata.get('longitude', 0.0)
            if rx_lat == 0.0 and rx_lon == 0.0:
                return None
            
            wwv_mid = calculate_midpoint(rx_lat, rx_lon, WWV_LAT, WWV_LON)
            wwvh_mid = calculate_midpoint(rx_lat, rx_lon, WWVH_LAT, WWVH_LON)
            
            timestamps = []
            wwv_elev = []
            wwvh_elev = []
            
            curr = start
            while curr < end:
                timestamps.append(curr.strftime('%Y-%m-%dT%H:%M:%SZ'))
                _, el_wwv = solar_position(curr, wwv_mid[0], wwv_mid[1])
                _, el_wwvh = solar_position(curr, wwvh_mid[0], wwvh_mid[1])
                wwv_elev.append(round(el_wwv, 2))
                wwvh_elev.append(round(el_wwvh, 2))
                curr += timedelta(minutes=interval_minutes)
            
            return {
                'timestamps': timestamps,
                'wwv_elevation_deg': wwv_elev,
                'wwvh_elevation_deg': wwvh_elev,
                'wwv_midpoint': {
                    'lat': round(wwv_mid[0], 4),
                    'lon': round(wwv_mid[1], 4)
                },
                'wwvh_midpoint': {
                    'lat': round(wwvh_mid[0], 4),
                    'lon': round(wwvh_mid[1], 4)
                },
                'interval_minutes': interval_minutes
            }
        except Exception as e:
            logger.warning(f"Failed to compute solar zenith: {e}")
            return None
    
    def get_daily_comparison(self, date: Optional[datetime] = None, hours: Optional[int] = None) -> Dict[str, Any]:
        """
        Get test signal data organized for daily comparison views.
        
        Returns data structured for:
        - WWV vs WWVH comparison at same frequencies
        - Same station across different frequencies
        - Evolution over the UTC day
        
        Args:
            date: Date to analyze (defaults to today, falls back to most recent data)
            hours: If specified, return this many hours of data ending at now
            
        Returns:
            Structured comparison data
        """
        try:
            if hours is not None:
                end = datetime.utcnow()
                start = end - timedelta(hours=hours)
            else:
                if date is None:
                    date = datetime.utcnow()
                
                # Get full day of data
                start = datetime(date.year, date.month, date.day, 0, 0, 0)
                end = start + timedelta(days=1)
            
            history = self.get_history(start=start, end=end)
            measurements = history.get('measurements', [])
            
            # If no data for requested date, try to find most recent data (up to 14 days back)
            if not measurements and hours is None and date.date() == datetime.utcnow().date():
                for days_back in range(1, 15):
                    fallback_date = datetime.utcnow() - timedelta(days=days_back)
                    start = datetime(fallback_date.year, fallback_date.month, fallback_date.day, 0, 0, 0)
                    end = start + timedelta(days=1)
                    history = self.get_history(start=start, end=end)
                    measurements = history.get('measurements', [])
                    if measurements:
                        date = fallback_date
                        logger.info(f"No data for today, using {date.date()} ({len(measurements)} measurements)")
                        break
            
            # Organize by station
            by_station = {'WWV': [], 'WWVH': []}
            for m in measurements:
                station = m.get('station')
                if station in by_station:
                    by_station[station].append(m)
            
            # Organize by frequency
            frequencies = sorted(set(m.get('frequency_mhz') for m in measurements if m.get('frequency_mhz')))
            by_frequency = {freq: [] for freq in frequencies}
            for m in measurements:
                freq = m.get('frequency_mhz')
                if freq in by_frequency:
                    by_frequency[freq].append(m)
            
            # Find matching pairs (same time, same frequency, different station)
            wwv_wwvh_pairs = []
            wwv_by_time_freq = {}
            for m in by_station['WWV']:
                # Extract hour from timestamp
                ts = m.get('timestamp', '')
                freq = m.get('frequency_mhz')
                if ts and freq:
                    hour = ts[:13]  # YYYY-MM-DDTHH
                    wwv_by_time_freq[(hour, freq)] = m
            
            for m in by_station['WWVH']:
                ts = m.get('timestamp', '')
                freq = m.get('frequency_mhz')
                if ts and freq:
                    hour = ts[:13]
                    wwv_match = wwv_by_time_freq.get((hour, freq))
                    if wwv_match:
                        wwv_wwvh_pairs.append({
                            'hour': hour,
                            'frequency_mhz': freq,
                            'wwv': wwv_match,
                            'wwvh': m,
                            'snr_diff_db': (wwv_match.get('snr_db') or 0) - (m.get('snr_db') or 0) if wwv_match.get('snr_db') and m.get('snr_db') else None,
                            's4_diff': (wwv_match.get('scintillation_index') or 0) - (m.get('scintillation_index') or 0) if wwv_match.get('scintillation_index') is not None and m.get('scintillation_index') is not None else None,
                            'toa_diff_ms': (wwv_match.get('noise_toa_offset_ms') or 0) - (m.get('noise_toa_offset_ms') or 0) if wwv_match.get('noise_toa_offset_ms') is not None and m.get('noise_toa_offset_ms') is not None else None
                        })
            
            # Statistics
            def calc_stats(values):
                values = [v for v in values if v is not None]
                if not values:
                    return None
                return {
                    'min': min(values),
                    'max': max(values),
                    'mean': sum(values) / len(values),
                    'count': len(values)
                }
            
            stats = {
                'wwv': {
                    'count': len(by_station['WWV']),
                    'snr_db': calc_stats([m.get('snr_db') for m in by_station['WWV']]),
                    's4': calc_stats([m.get('scintillation_index') for m in by_station['WWV']]),
                    'toa_offset_ms': calc_stats([m.get('noise_toa_offset_ms') for m in by_station['WWV']])
                },
                'wwvh': {
                    'count': len(by_station['WWVH']),
                    'snr_db': calc_stats([m.get('snr_db') for m in by_station['WWVH']]),
                    's4': calc_stats([m.get('scintillation_index') for m in by_station['WWVH']]),
                    'toa_offset_ms': calc_stats([m.get('noise_toa_offset_ms') for m in by_station['WWVH']])
                }
            }
            
            # Compute solar zenith angles at WWV and WWVH path midpoints
            solar = self._compute_solar_zenith(start, end)
            
            return self._convert_to_native({
                'date': start.strftime('%Y-%m-%d'),
                'by_station': by_station,
                'by_frequency': by_frequency,
                'wwv_wwvh_pairs': wwv_wwvh_pairs,
                'frequencies': frequencies,
                'stats': stats,
                'total_measurements': len(measurements),
                'solar': solar
            })
            
        except Exception as e:
            logger.error(f"Error getting daily comparison: {e}")
            return {'error': str(e)}
