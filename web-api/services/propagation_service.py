"""
Propagation analysis service for ionospheric and propagation mode data.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import Counter
import logging
import re

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
        
        # Channel directories for test signal data
        self.channel_dirs = [
            'SHARED_2500', 'SHARED_5000', 'SHARED_10000', 'SHARED_15000',
            'WWV_20000', 'WWV_25000'
        ]
    
    def _sanitize_value(self, val: Any) -> Any:
        """Sanitize values for JSON serialization (convert NaN/Inf to None, NumPy to Python types)."""
        import math
        import numpy as np
        
        if val is None:
            return None
        
        # Handle NumPy types
        if isinstance(val, (np.floating, np.integer)):
            val = val.item()
        elif isinstance(val, np.ndarray):
            return [self._sanitize_value(x) for x in val.tolist()]
            
        # Handle NaN/Inf
        if isinstance(val, (float, int)):
            if not math.isfinite(val):
                return None
        else:
            # Check if it's still some weird type that might convert to non-finite
            try:
                if hasattr(val, '__float__') and not math.isfinite(float(val)):
                    return None
            except:
                pass
                
        return val

    def _deep_sanitize(self, obj: Any) -> Any:
        """Recursively sanitize dicts and lists for JSON serialization."""
        if isinstance(obj, dict):
            return {str(k): self._deep_sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._deep_sanitize(x) for x in obj]
        else:
            return self._sanitize_value(obj)
    
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
            
            channel_count = 0
            
            # Check each channel directory
            for channel_dir in self.phase2_dir.iterdir():
                if not channel_dir.is_dir() or channel_dir.name in ['fusion', 'science']:
                    continue
                
                channel_count += 1
                
                # DataProductReader automatically resolves subdirectory via registry
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
                    logger.error(f"  Error reading {channel_dir.name}: {e}", exc_info=True)
                    continue
            
            logger.info(f"Total channels scanned: {channel_count}, total measurements: {len(all_measurements)}")
            
            if not all_measurements:
                logger.warning("No measurements found in any channel")
                return None
            
            # Analyze per-broadcast (station + frequency)
            broadcast_stats = {}
            
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
                
            
            # Calculate per-broadcast statistics
            broadcasts = []
            for key, stats in broadcast_stats.items():
                total = stats['n_measurements']
                mode_probs = {mode: count/total for mode, count in stats['mode_counts'].items()}
                dominant_mode = stats['mode_counts'].most_common(1)[0][0] if stats['mode_counts'] else 'UNKNOWN'
                
                avg_snr = self._sanitize_value(sum(stats['snr_values']) / len(stats['snr_values']) if stats['snr_values'] else None)
                
                broadcasts.append({
                    'station': stats['station'],
                    'frequency_mhz': self._sanitize_value(stats['frequency_mhz']),
                    'dominant_mode': dominant_mode,
                    'mode_distribution': dict(stats['mode_counts']),
                    'mode_probabilities': {m: self._sanitize_value(p) for m, p in mode_probs.items()},
                    'n_measurements': total,
                    'avg_snr_db': avg_snr
                })
            
            # Sort by frequency
            broadcasts.sort(key=lambda x: x['frequency_mhz'])
            
            # Estimate current MUF from per-broadcast aggregated stats.
            # Require: (1) dominant mode is F-layer (regex: digit + F),
            #          (2) avg SNR >= 15 dB (real signal, not noise),
            #          (3) at least 3 measurements (not a fluke).
            f_layer_pattern = re.compile(r'^\d+F')
            f_layer_freqs = []
            for b in broadcasts:
                dom = b.get('dominant_mode', '')
                if (f_layer_pattern.match(dom)
                        and (b.get('avg_snr_db') or 0) >= 15.0
                        and b.get('n_measurements', 0) >= 3):
                    f_layer_freqs.append(b['frequency_mhz'])
            
            muf_estimate = None
            if f_layer_freqs:
                # MUF ≈ 1.15 × highest credible F-layer frequency
                muf_estimate = self._sanitize_value(max(f_layer_freqs) * 1.15)
            
            # Check for reanalyzed MUF from ionospheric reanalysis service.
            # The reanalysis applies physics-based mode validation (foF2,
            # oblique MUF, SNR gating) and produces a more reliable estimate.
            reanalyzed_muf = self._get_reanalyzed_muf(start_time, end_time)
            
            # Prefer reanalyzed MUF when available
            best_muf = reanalyzed_muf if reanalyzed_muf is not None else muf_estimate
            
            result = self._deep_sanitize({
                'timestamp': end_time.isoformat() + 'Z',
                'time_span_hours': 1.0,
                'n_measurements': len(all_measurements),
                'broadcasts': broadcasts,
                'muf_estimate_mhz': best_muf,
                'muf_realtime_mhz': muf_estimate,
                'muf_reanalyzed_mhz': reanalyzed_muf,
                'n_broadcasts': len(broadcasts)
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting current conditions: {e}", exc_info=True)
            return None
    
    def _get_reanalyzed_muf(
        self,
        start_time: datetime,
        end_time: datetime
    ) -> Optional[float]:
        """
        Read the most recent reanalyzed MUF from L3C propagation stats.

        The ionospheric reanalysis service writes hourly L3C records with
        physics-validated MUF estimates. Returns the highest non-null MUF
        from the most recent reanalysis hour, or None if unavailable.
        """
        try:
            reanalysis_dir = self.phase2_dir / 'science' / 'propagation_stats'
            if not reanalysis_dir.exists():
                return None

            reader = DataProductReader(
                data_dir=reanalysis_dir,
                product_level='L3C',
                product_name='propagation_stats',
                channel='REANALYSIS',
                use_registry=False
            )

            records = reader.read_time_range(
                start=start_time.isoformat() + 'Z',
                end=end_time.isoformat() + 'Z'
            )

            if not records:
                return None

            # Collect all non-null MUF estimates from the reanalysis period
            muf_values = []
            for r in records:
                muf = r.get('estimated_muf_mhz')
                if muf is not None and muf > 0:
                    muf_values.append(float(muf))

            if not muf_values:
                return None

            # Return the maximum reanalyzed MUF
            return self._sanitize_value(max(muf_values))

        except Exception as e:
            logger.debug(f"Reanalyzed MUF not available: {e}")
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
                
                # DataProductReader automatically resolves subdirectory via registry
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
            import math
            snrs = []
            for m in all_measurements:
                s = m.get('snr_db')
                if s is not None and (math.isnan(s) or math.isinf(s)):
                    s = None
                snrs.append(s)
            
            result = self._deep_sanitize({
                'timestamps': timestamps,
                'modes': modes,
                'stations': stations,
                'frequencies': frequencies,
                'snr_db': snrs,
                'count': len(all_measurements),
            })
            
            return result
            
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
                uncertainty = m.get('residuals_ms')  # RMS residual is the true timing uncertainty
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
                paths[station]['tec_tecu'].append(self._sanitize_value(tec))
                paths[station]['uncertainty_tecu'].append(self._sanitize_value(uncertainty))
                paths[station]['confidence'].append(self._sanitize_value(confidence))
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
            
            result = self._deep_sanitize({
                'paths': paths,
                'stations': list(paths.keys()),
                'total_measurements': sum(len(p['tec_tecu']) for p in paths.values())
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting TEC summary: {e}")
            return None
    
    def get_test_signal_summary(
        self,
        start: datetime,
        end: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Get WWV/WWVH test signal analysis data.
        
        Returns test signal detections and ionospheric metrics from minutes 8 (WWV)
        and 44 (WWVH) with per-frequency field strength, scintillation, and anomalies.
        
        Args:
            start: Start time
            end: End time
        
        Returns:
            Test signal measurements organized by channel/frequency
        """
        try:
            import csv
            from collections import defaultdict
            
            all_measurements = []
            
            # Read test signal CSV files from each channel directory
            for channel_name in self.channel_dirs:
                channel_dir = self.phase2_dir / channel_name / 'test_signal'
                
                if not channel_dir.exists():
                    continue
                
                # Find CSV files in date range
                csv_files = sorted(channel_dir.glob('*_test_signal_*.csv'))
                
                for csv_file in csv_files:
                    try:
                        with open(csv_file, 'r') as f:
                            reader = csv.DictReader(f)
                            for row in reader:
                                # Parse timestamp
                                timestamp_str = row.get('timestamp_utc', '')
                                if not timestamp_str:
                                    continue
                                
                                try:
                                    ts = datetime.fromisoformat(timestamp_str.replace('Z', ''))
                                except:
                                    continue
                                
                                # Filter by time range
                                if ts < start or ts > end:
                                    continue
                                
                                # Parse fields
                                detected = int(row.get('detected', 0)) == 1
                                if not detected:
                                    continue
                                
                                minute_num = int(row.get('minute_number', 0))
                                station = row.get('station', '')
                                
                                # Extract frequency from channel name
                                freq_mhz = None
                                if 'SHARED' in channel_name:
                                    freq_str = channel_name.replace('SHARED_', '')
                                    freq_mhz = float(freq_str) / 1000.0
                                elif 'WWV' in channel_name:
                                    freq_str = channel_name.replace('WWV_', '')
                                    freq_mhz = float(freq_str) / 1000.0
                                
                                measurement = {
                                    'timestamp_utc': timestamp_str,
                                    'minute_boundary': int(row.get('minute_boundary', 0)),
                                    'minute_number': minute_num,
                                    'station': station,
                                    'frequency_mhz': freq_mhz,
                                    'channel': channel_name,
                                    'detected': detected,
                                    'confidence': float(row.get('confidence', 0)) if row.get('confidence') else None,
                                    'multitone_score': float(row.get('multitone_score', 0)) if row.get('multitone_score') else None,
                                    'chirp_score': float(row.get('chirp_score', 0)) if row.get('chirp_score') else None,
                                    'snr_db': float(row.get('snr_db', 0)) if row.get('snr_db') else None,
                                    'frequency_selectivity_db': float(row.get('frequency_selectivity_db', 0)) if row.get('frequency_selectivity_db') else None,
                                    'delay_spread_ms': float(row.get('delay_spread_ms', 0)) if row.get('delay_spread_ms') else None,
                                    'toa_offset_ms': float(row.get('toa_offset_ms', 0)) if row.get('toa_offset_ms') else None,
                                    'coherence_time_sec': float(row.get('coherence_time_sec', 0)) if row.get('coherence_time_sec') else None
                                }
                                
                                all_measurements.append(measurement)
                    
                    except Exception as e:
                        logger.debug(f"Could not read {csv_file}: {e}")
                        continue
            
            if not all_measurements:
                return None
            
            # Sort by timestamp
            all_measurements.sort(key=lambda m: m['timestamp_utc'])
            
            # Organize by station and frequency
            by_station = defaultdict(lambda: {
                'timestamps': [],
                'frequencies': [],
                'snr_db': [],
                'confidence': [],
                'delay_spread_ms': [],
                'coherence_time_sec': [],
                'frequency_selectivity_db': [],
                'count': 0
            })
            
            for m in all_measurements:
                station = m['station']
                by_station[station]['timestamps'].append(m['timestamp_utc'])
                by_station[station]['frequencies'].append(m['frequency_mhz'])
                by_station[station]['snr_db'].append(m['snr_db'])
                by_station[station]['confidence'].append(m['confidence'])
                by_station[station]['delay_spread_ms'].append(m['delay_spread_ms'])
                by_station[station]['coherence_time_sec'].append(m['coherence_time_sec'])
                by_station[station]['frequency_selectivity_db'].append(m['frequency_selectivity_db'])
                by_station[station]['count'] += 1
            
            # Calculate statistics per station
            for station, data in by_station.items():
                # Average SNR
                valid_snr = [s for s in data['snr_db'] if s is not None]
                data['mean_snr_db'] = sum(valid_snr) / len(valid_snr) if valid_snr else None
                
                # Average confidence
                valid_conf = [c for c in data['confidence'] if c is not None]
                data['mean_confidence'] = sum(valid_conf) / len(valid_conf) if valid_conf else None
                
                # Average delay spread
                valid_delay = [d for d in data['delay_spread_ms'] if d is not None]
                data['mean_delay_spread_ms'] = sum(valid_delay) / len(valid_delay) if valid_delay else None
            
            result = self._deep_sanitize({
                'measurements': all_measurements,
                'by_station': dict(by_station),
                'total_detections': len(all_measurements),
                'stations': list(by_station.keys()),
                'time_range': {
                    'start': start.isoformat() + 'Z',
                    'end': end.isoformat() + 'Z'
                }
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting test signal summary: {e}")
            return None
