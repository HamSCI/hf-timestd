"""
Propagation analysis service for ionospheric and propagation mode data.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import Counter
import logging

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class PropagationService:
    """Service for accessing propagation and ionospheric data."""
    
    # Valid station/frequency combinations (MHz)
    # Discrimination only makes sense on shared frequencies: 2.5, 5, 10, 15 MHz
    VALID_BROADCASTS = {
        'WWV': [2.5, 5.0, 10.0, 15.0, 20.0, 25.0],
        'WWVH': [2.5, 5.0, 10.0, 15.0],  # WWVH does NOT broadcast on 20/25 MHz
        'CHU': [3.33, 7.85, 14.67],      # CHU-only frequencies
        'BPM': [2.5, 5.0, 10.0, 15.0]    # BPM only on shared frequencies
    }
    
    # Shared frequencies where discrimination is required (WWV, WWVH, BPM)
    SHARED_FREQUENCIES = [2.5, 5.0, 10.0, 15.0]
    
    # Station-specific frequencies (no discrimination needed)
    STATION_SPECIFIC = {
        20.0: 'WWV',   # WWV only
        25.0: 'WWV',   # WWV only
        3.33: 'CHU',   # CHU only
        7.85: 'CHU',   # CHU only
        14.67: 'CHU'   # CHU only
    }
    
    def __init__(self, data_root: Path):
        """
        Initialize propagation service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        self.science_dir = self.phase2_dir / 'science'
        self.tec_dir = self.science_dir / 'tec'
        self.prop_stats_dir = self.science_dir / 'propagation_stats'
    
    def _is_valid_broadcast(self, station: str, frequency_mhz: float) -> bool:
        """
        Check if station/frequency combination is valid.
        
        Args:
            station: Station callsign
            frequency_mhz: Frequency in MHz
        
        Returns:
            True if valid broadcast, False if impossible combination
        """
        if station not in self.VALID_BROADCASTS:
            return True  # Unknown station, allow it
        
        valid_freqs = self.VALID_BROADCASTS[station]
        # Check if frequency is close to any valid frequency (within 0.1 MHz)
        return any(abs(frequency_mhz - valid_freq) < 0.1 for valid_freq in valid_freqs)
    
    def get_current_conditions(self) -> Optional[Dict[str, Any]]:
        """
        Get current propagation conditions with per-broadcast analysis.
        
        Returns propagation modes organized by station/frequency pairs,
        showing path-specific characteristics.
        """
        try:
            # Get L2 timing measurements from last hour
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(hours=1)
            
            # Collect measurements from all channels
            all_measurements = []
            
            # Check each channel directory
            for channel_dir in self.phase2_dir.iterdir():
                if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
                    continue
                
                # L2 timing measurements are directly in channel directory
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='timing_measurements',
                        channel=channel_dir.name
                    )
                    
                    measurements = reader.read_time_range(
                        start=start_time.isoformat() + 'Z',
                        end=end_time.isoformat() + 'Z'
                    )
                    
                    all_measurements.extend(measurements)
                    
                except Exception as e:
                    logger.debug(f"Could not read {channel_dir.name}: {e}")
                    continue
            
            if not all_measurements:
                return None
            
            # Analyze per-broadcast (station + frequency)
            broadcast_stats = {}
            muf_by_time = []
            
            for m in all_measurements:
                station = m.get('station', 'UNKNOWN')
                freq = m.get('frequency_mhz', 0)
                mode = m.get('propagation_mode', 'UNKNOWN')
                snr = m.get('snr_db')
                timestamp = m.get('timestamp_utc')
                
                # Validate station/frequency combination
                if not self._is_valid_broadcast(station, freq):
                    logger.debug(f"Rejecting invalid broadcast: {station} at {freq:.2f} MHz")
                    continue
                
                # Create broadcast key
                broadcast_key = f"{station}_{freq:.1f}"
                
                if broadcast_key not in broadcast_stats:
                    broadcast_stats[broadcast_key] = {
                        'station': station,
                        'frequency_mhz': freq,
                        'mode_counts': Counter(),
                        'snr_values': [],
                        'n_measurements': 0
                    }
                
                broadcast_stats[broadcast_key]['mode_counts'][mode] += 1
                broadcast_stats[broadcast_key]['n_measurements'] += 1
                
                if snr is not None:
                    broadcast_stats[broadcast_key]['snr_values'].append(snr)
                
                # Track MUF over time
                if 'F' in mode and freq > 0:
                    muf_by_time.append({'timestamp': timestamp, 'frequency': freq})
            
            # Calculate per-broadcast statistics
            broadcasts = []
            for key, stats in broadcast_stats.items():
                total = stats['n_measurements']
                mode_probs = {mode: count/total for mode, count in stats['mode_counts'].items()}
                dominant_mode = stats['mode_counts'].most_common(1)[0][0] if stats['mode_counts'] else 'UNKNOWN'
                
                avg_snr = sum(stats['snr_values']) / len(stats['snr_values']) if stats['snr_values'] else None
                
                broadcasts.append({
                    'station': stats['station'],
                    'frequency_mhz': stats['frequency_mhz'],
                    'dominant_mode': dominant_mode,
                    'mode_distribution': dict(stats['mode_counts']),
                    'mode_probabilities': mode_probs,
                    'n_measurements': total,
                    'avg_snr_db': avg_snr
                })
            
            # Sort by frequency
            broadcasts.sort(key=lambda x: x['frequency_mhz'])
            
            # Estimate current MUF (highest F-layer frequency)
            muf_estimate = None
            if muf_by_time:
                recent_f_freqs = [item['frequency'] for item in muf_by_time[-20:]]  # Last 20 F-layer obs
                if recent_f_freqs:
                    muf_estimate = max(recent_f_freqs) * 1.15
            
            return {
                'timestamp': end_time.isoformat() + 'Z',
                'time_span_hours': 1.0,
                'n_measurements': len(all_measurements),
                'broadcasts': broadcasts,
                'muf_estimate_mhz': muf_estimate,
                'n_broadcasts': len(broadcasts)
            }
            
        except Exception as e:
            logger.error(f"Error getting current conditions: {e}")
            return None
    
    def get_mode_timeline(
        self,
        start: datetime,
        end: datetime,
        station: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get propagation mode timeline.
        
        Args:
            start: Start time
            end: End time
            station: Optional station filter (WWV, WWVH, CHU, BPM)
        
        Returns:
            Timeline of propagation modes with timestamps
        """
        try:
            # Collect measurements from all channels
            all_measurements = []
            
            for channel_dir in self.phase2_dir.iterdir():
                if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
                    continue
                
                # L2 timing measurements are directly in channel directory
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='timing_measurements',
                        channel=channel_dir.name
                    )
                    
                    measurements = reader.read_time_range(
                        start=start.isoformat() + 'Z',
                        end=end.isoformat() + 'Z'
                    )
                    
                    # Filter by station if specified
                    if station:
                        measurements = [m for m in measurements if m.get('station') == station]
                    
                    all_measurements.extend(measurements)
                    
                except Exception as e:
                    logger.debug(f"Could not read {channel_dir.name}: {e}")
                    continue
            
            if not all_measurements:
                return None
            
            # Sort by timestamp
            all_measurements.sort(key=lambda m: m.get('timestamp_utc', ''))
            
            # Extract timeline data
            timestamps = [m.get('timestamp_utc') for m in all_measurements]
            modes = [m.get('propagation_mode', 'UNKNOWN') for m in all_measurements]
            stations = [m.get('station', 'UNKNOWN') for m in all_measurements]
            frequencies = [m.get('frequency_mhz', 0) for m in all_measurements]
            snrs = [m.get('snr_db') for m in all_measurements]
            
            return {
                'timestamps': timestamps,
                'modes': modes,
                'stations': stations,
                'frequencies': frequencies,
                'snr_db': snrs,
                'count': len(all_measurements),
            }
            
        except Exception as e:
            logger.error(f"Error getting mode timeline: {e}")
            return None
    
    def get_tec_summary(
        self,
        start: datetime,
        end: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Get TEC (Total Electron Content) by path.
        
        Returns per-station TEC time series for WWV, WWVH, CHU, and BPM paths.
        
        Args:
            start: Start time
            end: End time
        
        Returns:
            TEC measurements organized by station/path
        """
        try:
            if not self.tec_dir.exists():
                return None
            
            # Try to read aggregated TEC data
            reader = DataProductReader(
                data_dir=self.tec_dir,
                product_level='L3',
                product_name='tec',
                channel='AGGREGATED'
            )
            
            measurements = reader.read_time_range(
                start=start.isoformat() + 'Z',
                end=end.isoformat() + 'Z'
            )
            
            # If no data in requested range, try last 7 days
            if not measurements:
                logger.info(f"No TEC data in requested range, trying last 7 days")
                start_fallback = datetime.utcnow() - timedelta(days=7)
                end_fallback = datetime.utcnow()
                measurements = reader.read_time_range(
                    start=start_fallback.isoformat() + 'Z',
                    end=end_fallback.isoformat() + 'Z'
                )
            
            if not measurements:
                return None
            
            # Organize by station/path with uncertainty
            paths = {}
            for m in measurements:
                station = m.get('station', 'UNKNOWN')
                tec = m.get('tec_tecu')
                timestamp = m.get('timestamp_utc')
                uncertainty = m.get('t_vacuum_error_ms')  # Timing uncertainty propagates to TEC
                confidence = m.get('confidence')
                quality = m.get('quality_flag', 'UNKNOWN')
                n_freqs = m.get('n_frequencies', 0)
                
                if tec is None or station == 'UNKNOWN':
                    continue
                
                if station not in paths:
                    paths[station] = {
                        'timestamps': [],
                        'tec_tecu': [],
                        'uncertainty_tecu': [],
                        'confidence': [],
                        'quality': [],
                        'n_frequencies': []
                    }
                
                paths[station]['timestamps'].append(timestamp)
                paths[station]['tec_tecu'].append(float(tec) if tec is not None else 0)
                paths[station]['uncertainty_tecu'].append(float(uncertainty) if uncertainty else 0)
                paths[station]['confidence'].append(float(confidence) if confidence else 0)
                paths[station]['quality'].append(quality)
                paths[station]['n_frequencies'].append(int(n_freqs) if n_freqs else 0)
            
            if not paths:
                return None
            
            # Calculate statistics per path
            for station, data in paths.items():
                if data['tec_tecu']:
                    data['mean_tec'] = sum(data['tec_tecu']) / len(data['tec_tecu'])
                    data['min_tec'] = min(data['tec_tecu'])
                    data['max_tec'] = max(data['tec_tecu'])
                    data['count'] = len(data['tec_tecu'])
                    
                    # Calculate mean uncertainty
                    if data['uncertainty_tecu']:
                        data['mean_uncertainty'] = sum(data['uncertainty_tecu']) / len(data['uncertainty_tecu'])
                    
                    # Count quality measurements
                    good_count = sum(1 for q in data['quality'] if q == 'GOOD')
                    data['quality_ratio'] = good_count / len(data['quality']) if data['quality'] else 0
            
            return {
                'paths': paths,
                'stations': list(paths.keys()),
                'total_measurements': sum(len(p['tec_tecu']) for p in paths.values())
            }
            
        except Exception as e:
            logger.error(f"Error getting TEC summary: {e}")
            return None
