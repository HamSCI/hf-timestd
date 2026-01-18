"""
Scintillation Data Service.

Provides access to ionospheric scintillation indices:
- S4 (amplitude scintillation index)
- σ_φ (phase scintillation index)
- Scintillation severity classification
- Per-path scintillation data
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


class ScintillationService:
    """Service for accessing scintillation data."""
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        
        # All channels that may have scintillation data
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
    
    def get_latest_by_path(self) -> Dict[str, Any]:
        """
        Get latest scintillation data organized by propagation path.
        
        Returns:
            Dictionary with per-station scintillation data
        """
        try:
            # Collect data from timing measurements which include scintillation
            path_data = {}
            
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
                    
                    # Get last 2 hours of data
                    end_time = datetime.utcnow()
                    start_time = end_time - timedelta(hours=2)
                    
                    measurements = reader.read_time_range(
                        start=start_time.isoformat() + 'Z',
                        end=end_time.isoformat() + 'Z'
                    )
                    
                    for m in measurements:
                        station = m.get('station', 'UNKNOWN')
                        if station == 'UNKNOWN':
                            continue
                        
                        # Extract scintillation data if present
                        s4 = m.get('s4_index') or m.get('scintillation_index')
                        sigma_phi = m.get('sigma_phi_rad')
                        severity = m.get('scintillation_severity')
                        
                        if station not in path_data:
                            path_data[station] = {
                                'station': station,
                                's4_values': [],
                                'sigma_phi_values': [],
                                'timestamps': [],
                                'frequencies': [],
                                'severities': []
                            }
                        
                        if s4 is not None:
                            path_data[station]['s4_values'].append(self._clean_value(s4))
                        if sigma_phi is not None:
                            path_data[station]['sigma_phi_values'].append(self._clean_value(sigma_phi))
                        path_data[station]['timestamps'].append(m.get('timestamp_utc'))
                        path_data[station]['frequencies'].append(m.get('frequency_mhz'))
                        if severity:
                            path_data[station]['severities'].append(severity)
                            
                except Exception as e:
                    logger.debug(f"Could not read scintillation from {channel}: {e}")
                    continue
            
            # Calculate summary statistics per path
            result = {'paths': {}, 'timestamp': datetime.utcnow().isoformat() + 'Z'}
            
            for station, data in path_data.items():
                s4_vals = [v for v in data['s4_values'] if v is not None]
                sigma_vals = [v for v in data['sigma_phi_values'] if v is not None]
                
                # Classify severity
                avg_s4 = sum(s4_vals) / len(s4_vals) if s4_vals else None
                if avg_s4 is not None:
                    if avg_s4 < 0.2:
                        severity = 'none'
                    elif avg_s4 < 0.4:
                        severity = 'weak'
                    elif avg_s4 < 0.6:
                        severity = 'moderate'
                    else:
                        severity = 'strong'
                else:
                    severity = 'unknown'
                
                result['paths'][station] = {
                    'station': station,
                    's4_mean': self._clean_value(avg_s4),
                    's4_max': self._clean_value(max(s4_vals)) if s4_vals else None,
                    'sigma_phi_mean': self._clean_value(sum(sigma_vals) / len(sigma_vals)) if sigma_vals else None,
                    'n_measurements': len(data['timestamps']),
                    'severity': severity,
                    'frequencies_observed': list(set(f for f in data['frequencies'] if f))
                }
            
            result['n_paths'] = len(result['paths'])
            return self._convert_to_native(result)
            
        except Exception as e:
            logger.error(f"Error getting scintillation data: {e}")
            return {'paths': {}, 'n_paths': 0, 'error': str(e)}
    
    def get_history(
        self,
        start: datetime,
        end: datetime,
        station: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get scintillation history.
        
        Args:
            start: Start datetime
            end: End datetime
            station: Optional station filter
            
        Returns:
            Time series of scintillation data
        """
        try:
            all_data = []
            
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
                    
                    for m in measurements:
                        if station and m.get('station') != station:
                            continue
                        
                        s4 = m.get('s4_index') or m.get('scintillation_index')
                        if s4 is not None:
                            all_data.append({
                                'timestamp': m.get('timestamp_utc'),
                                'station': m.get('station'),
                                'frequency_mhz': m.get('frequency_mhz'),
                                's4': self._clean_value(s4),
                                'sigma_phi': self._clean_value(m.get('sigma_phi_rad')),
                                'severity': m.get('scintillation_severity')
                            })
                            
                except Exception as e:
                    logger.debug(f"Could not read from {channel}: {e}")
                    continue
            
            # Sort by timestamp
            all_data.sort(key=lambda x: x.get('timestamp', ''))
            
            return self._convert_to_native({
                'measurements': all_data,
                'count': len(all_data)
            })
            
        except Exception as e:
            logger.error(f"Error getting scintillation history: {e}")
            return {'measurements': [], 'count': 0, 'error': str(e)}
