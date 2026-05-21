"""
TEC (Total Electron Content) service for v6.5.0 data access.

Provides access to HF-derived TEC estimates from the
TimingConsistencyValidator and archived TEC data products via the
L3_tec SQLite table (Phase 4 cutover).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging

from config import config

logger = logging.getLogger(__name__)

from hf_timestd.io import make_data_product_reader, SqliteDataProductReader

# Retained for callers that still annotate the return type as
# DataProductReader; SqliteDataProductReader exposes the same
# read_time_range API.
DataProductReader = SqliteDataProductReader
DATA_READER_AVAILABLE = True


class TECService:
    """
    Service for accessing TEC (Total Electron Content) data products.
    
    TEC is derived from multi-frequency HF observations using the
    ionospheric dispersion relation: Δτ = K·TEC·(1/f₁² - 1/f₂²)
    
    Data locations (v6.5.0):
    - /var/lib/timestd/phase2/science/tec/ - HDF5 TEC estimates (AGGREGATED_tec_YYYYMMDD.h5)
    """
    
    def __init__(self, data_root: Path):
        """
        Initialize TEC service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.tec_dir = self.data_root / 'phase2' / 'science' / 'tec'
        self._reader = None
        
    def _get_reader(self) -> Optional['DataProductReader']:
        """Get or create the data-product reader for L3_tec."""
        if self._reader is None and self.tec_dir.exists():
            try:
                self._reader = make_data_product_reader(
                    data_dir=self.tec_dir,
                    product_level='L3',
                    product_name='tec',
                    channel='AGGREGATED',
                    storage_config=config.storage
                )
            except Exception as e:
                logger.error(f"Failed to initialize TEC reader: {e}")
                return None
        return self._reader
        
    def get_current_tec(self) -> Optional[Dict[str, Any]]:
        """
        Get the most recent TEC estimates.
        
        Returns:
            Dictionary with current TEC values per station, or None if no data
        """
        try:
            reader = self._get_reader()
            if not reader:
                logger.warning("TEC reader not available")
                return None
            
            # Read last 5 minutes of data
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=5)
            
            measurements = reader.read_time_range(
                start=start.isoformat().replace('+00:00', 'Z'),
                end=now.isoformat().replace('+00:00', 'Z')
            )
            
            if not measurements:
                logger.debug("No recent TEC measurements found")
                return None
            
            # Get most recent measurement per station
            latest_by_station = {}
            for m in measurements:
                station = m.get('station', 'UNKNOWN')
                ts = m.get('unix_timestamp', 0)
                if station not in latest_by_station or ts > latest_by_station[station]['unix_timestamp']:
                    latest_by_station[station] = m
            
            return {
                'timestamp': now.isoformat() + 'Z',
                'stations': {
                    station: {
                        'tec_tecu': m.get('tec_tecu', 0),
                        'confidence': m.get('confidence', 0),
                        'mode': m.get('mode', 'UNKNOWN'),
                        'unix_timestamp': m.get('unix_timestamp', 0)
                    }
                    for station, m in latest_by_station.items()
                },
                'n_stations': len(latest_by_station)
            }
            
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
            start: Start time (naive datetime assumed UTC)
            end: End time (naive datetime assumed UTC)
            station: Optional station filter (e.g., 'WWV', 'CHU')
        
        Returns:
            Dictionary with timestamps and TEC values per station
        """
        try:
            result = {
                'timestamps': [],
                'stations': {},  # station -> {'tec_tecu': [], 'confidence': [], 'mode': []}
                'n_points': 0,
                'time_range': {
                    'start': start.isoformat() + 'Z',
                    'end': end.isoformat() + 'Z'
                }
            }
            
            reader = self._get_reader()
            if not reader:
                return result
            
            # Convert naive datetime to ISO format for reader
            start_iso = start.isoformat() + 'Z' if start.tzinfo is None else start.isoformat().replace('+00:00', 'Z')
            end_iso = end.isoformat() + 'Z' if end.tzinfo is None else end.isoformat().replace('+00:00', 'Z')
            
            measurements = reader.read_time_range(start=start_iso, end=end_iso)
            
            if not measurements:
                return result
            
            # Group by station and collect time series
            for m in measurements:
                station_name = m.get('station', 'UNKNOWN')
                
                # Apply station filter if specified
                if station and station_name != station:
                    continue
                
                ts_iso = m.get('timestamp_iso', '')
                if ts_iso and ts_iso not in result['timestamps']:
                    result['timestamps'].append(ts_iso)
                
                if station_name not in result['stations']:
                    result['stations'][station_name] = {
                        'tec_tecu': [],
                        'confidence': [],
                        'mode': [],
                        'timestamps': []
                    }
                
                result['stations'][station_name]['tec_tecu'].append(m.get('tec_tecu', 0))
                result['stations'][station_name]['confidence'].append(m.get('confidence', 0))
                result['stations'][station_name]['mode'].append(m.get('mode', 'UNKNOWN'))
                result['stations'][station_name]['timestamps'].append(ts_iso)
            
            result['timestamps'] = sorted(set(result['timestamps']))
            result['n_points'] = sum(len(s['tec_tecu']) for s in result['stations'].values())
            return result
            
        except Exception as e:
            logger.error(f"Error getting TEC history: {e}")
            return {'timestamps': [], 'stations': {}, 'n_points': 0, 'error': str(e)}
    
    def get_tec_by_station(self, station: str, hours: int = 24) -> Dict[str, Any]:
        """
        Get TEC data for a specific station.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            hours: Number of hours of history
        
        Returns:
            Dictionary with TEC data for the specified station
        """
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        return self.get_tec_history(start, end, station=station)
