"""
Physics data service.

Provides access to L3 Physics data (TEC, UTC Consistency) using DataProductReader.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class PhysicsService:
    """Service for accessing physics fusion data."""
    
    def __init__(self, data_root: Path):
        """
        Initialize physics service.
        
        Args:
            data_root: Path to data root directory (containing phase2/fusion)
            # Actually PhysicsService writes to phase2/fusion with channel='global'?
            # PhysicsService used output_dir=.../phase2/fusion
            # channel='global'
            # DataProductWriter puts it in {output_dir}/{channel}_{product_name}_{date}.h5
            # So: output_dir/global_physics_YYYYMMDD.h5
        """
        # PhysicsFusionService writes to /var/lib/timestd/phase2/fusion
        # With channel='global'
        # So files are in /var/lib/timestd/phase2/fusion/global_physics_*.h5
        
        self.fusion_dir = Path(data_root) / 'phase2' / 'fusion'
        
        self.reader = DataProductReader(
            data_dir=self.fusion_dir,
            product_level='L3',
            product_name='physics',
            channel='global'
        )
    
    def get_latest(self) -> Optional[Dict[str, Any]]:
        """
        Get latest physics estimate.
        
        Returns:
            Dictionary with latest physics data or None if unavailable
        """
        try:
            # Try to read from today's file (last 1 hour)
            end_time = datetime.utcnow().isoformat() + 'Z'
            start_time = (datetime.utcnow() - timedelta(hours=1)).isoformat() + 'Z'
            
            measurements = self.reader.read_time_range(
                start=start_time,
                end=end_time
            )
            
            if not measurements:
                return None
            
            # Get most recent measurement
            latest = measurements[-1]
            return latest
            
        except Exception as e:
            logger.error(f"Error getting latest physics data: {e}")
            return None
    
    def get_history(
        self,
        start: datetime,
        end: datetime
    ) -> Dict[str, Any]:
        """
        Get physics history.
        
        Args:
            start: Start datetime
            end: End datetime
            
        Returns:
            Dictionary with time series data
        """
        try:
            start_iso = start.isoformat() + 'Z'
            end_iso = end.isoformat() + 'Z'
            
            measurements = self.reader.read_time_range(
                start=start_iso,
                end=end_iso
            )
            
            timestamps = []
            utc_consistent = []
            
            # TEC is a map/dict in schema, flattened or handled potentially
            # For specific station extraction, we might need logic.
            # Assuming 'stations_used' indicates which keys exist in tec_estimates.
            
            # Since HDF5 reader returns flattened dicts usually, or handles complex types?
            # DataProductReader returns whatever h5py returns.
            # If we flattened it in writer, we read it back.
            # Currently PhysicsFusionService writes it as map if supported.
            # Let's return the raw list of measurements for now, client can parse.
            
            return {
                'measurements': measurements,
                'count': len(measurements)
            }
        
        except Exception as e:
            logger.error(f"Error getting physics history: {e}")
            return {'measurements': [], 'count': 0}
