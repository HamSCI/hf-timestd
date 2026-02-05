"""
TEC (Total Electron Content) service for v6.5.0 data access.

Provides access to HF-derived TEC estimates from the TimingConsistencyValidator
and archived TEC data products.
"""

import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging
import h5py

logger = logging.getLogger(__name__)


class TECService:
    """
    Service for accessing TEC (Total Electron Content) data products.
    
    TEC is derived from multi-frequency HF observations using the
    ionospheric dispersion relation: Δτ = K·TEC·(1/f₁² - 1/f₂²)
    
    Data locations (v6.5.0):
    - /var/lib/timestd/phase2/science/tec/ - Archived TEC estimates
    - Real-time TEC from TimingConsistencyValidator callback
    """
    
    def __init__(self, data_root: Path):
        """
        Initialize TEC service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.tec_dir = self.data_root / 'phase2' / 'science' / 'tec'
        
    def get_current_tec(self) -> Optional[Dict[str, Any]]:
        """
        Get the most recent TEC estimates.
        
        Returns:
            Dictionary with current TEC values per path, or None if no data
        """
        try:
            # Find most recent TEC file
            if not self.tec_dir.exists():
                logger.warning(f"TEC directory does not exist: {self.tec_dir}")
                return None
            
            # TEC files are organized by date: YYYY-MM-DD/tec_HHMMSS.h5
            today = datetime.utcnow().strftime('%Y-%m-%d')
            today_dir = self.tec_dir / today
            
            if not today_dir.exists():
                # Try yesterday
                yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
                today_dir = self.tec_dir / yesterday
                
            if not today_dir.exists():
                return None
            
            # Find most recent file
            tec_files = sorted(today_dir.glob('tec_*.h5'), reverse=True)
            if not tec_files:
                return None
            
            latest_file = tec_files[0]
            return self._read_tec_file(latest_file)
            
        except Exception as e:
            logger.error(f"Error getting current TEC: {e}")
            return None
    
    def get_tec_history(
        self,
        start: datetime,
        end: datetime,
        station: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get TEC history for a time range.
        
        Args:
            start: Start time
            end: End time
            station: Optional station filter (e.g., 'WWV', 'CHU')
        
        Returns:
            Dictionary with timestamps and TEC values per path
        """
        try:
            result = {
                'timestamps': [],
                'paths': {},  # path_name -> {'tec_tecu': [], 'uncertainty_tecu': []}
                'n_points': 0,
                'time_range': {
                    'start': start.isoformat() + 'Z',
                    'end': end.isoformat() + 'Z'
                }
            }
            
            if not self.tec_dir.exists():
                return result
            
            # Iterate through date directories
            current_date = start.date()
            end_date = end.date()
            
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                date_dir = self.tec_dir / date_str
                
                if date_dir.exists():
                    for tec_file in sorted(date_dir.glob('tec_*.h5')):
                        file_data = self._read_tec_file(tec_file)
                        if file_data and self._in_time_range(file_data, start, end):
                            self._merge_tec_data(result, file_data, station)
                
                current_date += timedelta(days=1)
            
            result['n_points'] = len(result['timestamps'])
            return result
            
        except Exception as e:
            logger.error(f"Error getting TEC history: {e}")
            return {'timestamps': [], 'paths': {}, 'n_points': 0, 'error': str(e)}
    
    def get_tec_by_station(self, station: str, hours: int = 24) -> Dict[str, Any]:
        """
        Get TEC data for a specific station.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            hours: Number of hours of history
        
        Returns:
            Dictionary with TEC data for all frequencies from that station
        """
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        return self.get_tec_history(start, end, station=station)
    
    def _read_tec_file(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Read a single TEC HDF5 file."""
        try:
            with h5py.File(filepath, 'r') as f:
                data = {
                    'timestamp': f.attrs.get('timestamp_utc', ''),
                    'paths': {}
                }
                
                # Read each path group
                for path_name in f.keys():
                    if path_name.startswith('_'):
                        continue
                    
                    path_grp = f[path_name]
                    data['paths'][path_name] = {
                        'tec_tecu': float(path_grp.attrs.get('tec_tecu', 0)),
                        'uncertainty_tecu': float(path_grp.attrs.get('uncertainty_tecu', 0)),
                        'station': path_grp.attrs.get('station', ''),
                        'frequency_mhz': float(path_grp.attrs.get('frequency_mhz', 0)),
                        'quality': path_grp.attrs.get('quality', 'unknown')
                    }
                
                return data
                
        except Exception as e:
            logger.error(f"Error reading TEC file {filepath}: {e}")
            return None
    
    def _in_time_range(self, data: Dict, start: datetime, end: datetime) -> bool:
        """Check if data timestamp is within range."""
        try:
            ts_str = data.get('timestamp', '')
            if not ts_str:
                return False
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
            return start <= ts <= end
        except:
            return False
    
    def _merge_tec_data(
        self,
        result: Dict,
        file_data: Dict,
        station_filter: Optional[str]
    ) -> None:
        """Merge file data into result dictionary."""
        timestamp = file_data.get('timestamp', '')
        if timestamp:
            result['timestamps'].append(timestamp)
        
        for path_name, path_data in file_data.get('paths', {}).items():
            # Apply station filter if specified
            if station_filter and path_data.get('station', '') != station_filter:
                continue
            
            if path_name not in result['paths']:
                result['paths'][path_name] = {
                    'tec_tecu': [],
                    'uncertainty_tecu': [],
                    'station': path_data.get('station', ''),
                    'frequency_mhz': path_data.get('frequency_mhz', 0)
                }
            
            result['paths'][path_name]['tec_tecu'].append(path_data.get('tec_tecu', 0))
            result['paths'][path_name]['uncertainty_tecu'].append(
                path_data.get('uncertainty_tecu', 0)
            )
