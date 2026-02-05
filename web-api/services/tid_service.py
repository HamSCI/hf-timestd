"""
TID (Traveling Ionospheric Disturbance) service for v6.5.0 data access.

Provides access to TID detection events from the tid_detector module
and archived TID data products.
"""

import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging
import h5py
import json

logger = logging.getLogger(__name__)


class TIDService:
    """
    Service for accessing TID (Traveling Ionospheric Disturbance) data products.
    
    TIDs are detected via cross-path correlation of timing residuals.
    When a TID passes through the ionosphere, it creates correlated
    perturbations in arrival times across multiple propagation paths.
    
    Data locations (v6.5.0):
    - /var/lib/timestd/phase2/science/tid/ - Archived TID events
    - Real-time TID detection from tid_detector.py
    """
    
    def __init__(self, data_root: Path):
        """
        Initialize TID service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.tid_dir = self.data_root / 'phase2' / 'science' / 'tid'
        
    def get_recent_events(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get recent TID events.
        
        Args:
            hours: Number of hours to look back
        
        Returns:
            List of TID event dictionaries
        """
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        return self.get_events_in_range(start, end)
    
    def get_events_in_range(
        self,
        start: datetime,
        end: datetime
    ) -> List[Dict[str, Any]]:
        """
        Get TID events within a time range.
        
        Args:
            start: Start time
            end: End time
        
        Returns:
            List of TID event dictionaries
        """
        events = []
        
        try:
            if not self.tid_dir.exists():
                logger.warning(f"TID directory does not exist: {self.tid_dir}")
                return events
            
            # TID files are organized by date: YYYY-MM-DD/tid_events.json
            current_date = start.date()
            end_date = end.date()
            
            while current_date <= end_date:
                date_str = current_date.strftime('%Y-%m-%d')
                date_dir = self.tid_dir / date_str
                
                if date_dir.exists():
                    # Check for JSON event file
                    events_file = date_dir / 'tid_events.json'
                    if events_file.exists():
                        day_events = self._read_events_json(events_file)
                        for event in day_events:
                            if self._event_in_range(event, start, end):
                                events.append(event)
                    
                    # Also check for HDF5 files
                    for h5_file in date_dir.glob('tid_*.h5'):
                        event = self._read_event_h5(h5_file)
                        if event and self._event_in_range(event, start, end):
                            events.append(event)
                
                current_date += timedelta(days=1)
            
            # Sort by timestamp
            events.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
            return events
            
        except Exception as e:
            logger.error(f"Error getting TID events: {e}")
            return []
    
    def get_event_details(self, event_id: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information about a specific TID event.
        
        Args:
            event_id: Event identifier (typically timestamp-based)
        
        Returns:
            Detailed event dictionary or None
        """
        try:
            # Event ID format: YYYYMMDD_HHMMSS
            if len(event_id) < 8:
                return None
            
            date_str = f"{event_id[:4]}-{event_id[4:6]}-{event_id[6:8]}"
            date_dir = self.tid_dir / date_str
            
            if not date_dir.exists():
                return None
            
            # Look for specific event file
            event_file = date_dir / f'tid_{event_id}.h5'
            if event_file.exists():
                return self._read_event_h5(event_file, detailed=True)
            
            # Fall back to events JSON
            events_file = date_dir / 'tid_events.json'
            if events_file.exists():
                events = self._read_events_json(events_file)
                for event in events:
                    if event.get('event_id') == event_id:
                        return event
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting TID event details: {e}")
            return None
    
    def get_statistics(self, days: int = 7) -> Dict[str, Any]:
        """
        Get TID detection statistics.
        
        Args:
            days: Number of days to analyze
        
        Returns:
            Statistics dictionary
        """
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        events = self.get_events_in_range(start, end)
        
        if not events:
            return {
                'n_events': 0,
                'period_days': days,
                'events_per_day': 0,
                'velocity_stats': None,
                'direction_distribution': None
            }
        
        # Compute statistics
        velocities = [e.get('velocity_m_s', 0) for e in events if e.get('velocity_m_s')]
        directions = [e.get('direction_deg', 0) for e in events if e.get('direction_deg') is not None]
        periods = [e.get('period_minutes', 0) for e in events if e.get('period_minutes')]
        
        stats = {
            'n_events': len(events),
            'period_days': days,
            'events_per_day': len(events) / days,
            'velocity_stats': {
                'mean_m_s': float(np.mean(velocities)) if velocities else None,
                'std_m_s': float(np.std(velocities)) if velocities else None,
                'min_m_s': float(np.min(velocities)) if velocities else None,
                'max_m_s': float(np.max(velocities)) if velocities else None
            } if velocities else None,
            'period_stats': {
                'mean_minutes': float(np.mean(periods)) if periods else None,
                'std_minutes': float(np.std(periods)) if periods else None
            } if periods else None,
            'direction_distribution': self._compute_direction_histogram(directions) if directions else None
        }
        
        return stats
    
    def _read_events_json(self, filepath: Path) -> List[Dict[str, Any]]:
        """Read TID events from JSON file."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                elif isinstance(data, dict) and 'events' in data:
                    return data['events']
                return []
        except Exception as e:
            logger.error(f"Error reading TID events JSON {filepath}: {e}")
            return []
    
    def _read_event_h5(self, filepath: Path, detailed: bool = False) -> Optional[Dict[str, Any]]:
        """Read a single TID event from HDF5 file."""
        try:
            with h5py.File(filepath, 'r') as f:
                event = {
                    'event_id': f.attrs.get('event_id', filepath.stem),
                    'timestamp': f.attrs.get('timestamp_utc', ''),
                    'velocity_m_s': float(f.attrs.get('velocity_m_s', 0)),
                    'direction_deg': float(f.attrs.get('direction_deg', 0)),
                    'period_minutes': float(f.attrs.get('period_minutes', 0)),
                    'confidence': float(f.attrs.get('confidence', 0)),
                    'affected_paths': list(f.attrs.get('affected_paths', [])),
                }
                
                if detailed:
                    # Add correlation data if available
                    if 'correlation' in f:
                        corr_grp = f['correlation']
                        event['correlation'] = {
                            'lags': corr_grp['lags'][:].tolist() if 'lags' in corr_grp else [],
                            'values': corr_grp['values'][:].tolist() if 'values' in corr_grp else [],
                            'peak_lag_s': float(corr_grp.attrs.get('peak_lag_s', 0))
                        }
                    
                    # Add residual time series if available
                    if 'residuals' in f:
                        res_grp = f['residuals']
                        event['residuals'] = {
                            'timestamps': res_grp['timestamps'][:].tolist() if 'timestamps' in res_grp else [],
                            'values_ms': res_grp['values_ms'][:].tolist() if 'values_ms' in res_grp else []
                        }
                
                return event
                
        except Exception as e:
            logger.error(f"Error reading TID event HDF5 {filepath}: {e}")
            return None
    
    def _event_in_range(self, event: Dict, start: datetime, end: datetime) -> bool:
        """Check if event timestamp is within range."""
        try:
            ts_str = event.get('timestamp', '')
            if not ts_str:
                return False
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')).replace(tzinfo=None)
            return start <= ts <= end
        except:
            return False
    
    def _compute_direction_histogram(self, directions: List[float]) -> Dict[str, int]:
        """Compute histogram of TID propagation directions."""
        bins = {
            'N': 0, 'NE': 0, 'E': 0, 'SE': 0,
            'S': 0, 'SW': 0, 'W': 0, 'NW': 0
        }
        
        for d in directions:
            d = d % 360
            if d < 22.5 or d >= 337.5:
                bins['N'] += 1
            elif d < 67.5:
                bins['NE'] += 1
            elif d < 112.5:
                bins['E'] += 1
            elif d < 157.5:
                bins['SE'] += 1
            elif d < 202.5:
                bins['S'] += 1
            elif d < 247.5:
                bins['SW'] += 1
            elif d < 292.5:
                bins['W'] += 1
            else:
                bins['NW'] += 1
        
        return bins
