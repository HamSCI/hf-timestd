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

from hf_timestd.io import make_data_product_reader
from config import config

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
        
        self.reader = make_data_product_reader(
            data_dir=self.fusion_dir,
            product_level='L3',
            product_name='physics',
            channel='global',
            storage_config=config.storage
        )
    
    def _convert_to_native(self, obj: Any) -> Any:
        """Convert numpy types to native Python types for JSON serialization."""
        import numpy as np
        import math
        
        if isinstance(obj, dict):
            return {k: self._convert_to_native(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [self._convert_to_native(item) for item in obj]
        elif isinstance(obj, np.ndarray):
            return [self._convert_to_native(item) for item in obj.tolist()]
        elif isinstance(obj, (np.integer, np.int64, np.int32)):
            return int(obj)
        elif isinstance(obj, (np.floating, np.float64, np.float32)):
            val = float(obj)
            # Handle NaN/Inf which are not JSON compliant
            if math.isnan(val) or math.isinf(val):
                return None
            return val
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, float):
            # Also check native floats for NaN/Inf
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        else:
            return obj
    
    def get_latest(self) -> Optional[Dict[str, Any]]:
        """
        Get latest physics estimate.
        
        Returns:
            Dictionary with latest physics data or None if unavailable
        """
        try:
            # Try progressively longer time ranges to find data
            for hours in [1, 6, 24]:
                end_time = datetime.utcnow().isoformat() + 'Z'
                start_time = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + 'Z'
                
                measurements = self.reader.read_time_range(
                    start=start_time,
                    end=end_time
                )
                
                if measurements:
                    # Get most recent measurement and convert numpy types
                    latest = self._convert_to_native(measurements[-1])
                    return latest
            
            return None
            
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
            start_iso = start.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            end_iso = end.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            
            measurements = self.reader.read_time_range(
                start=start_iso,
                end=end_iso
            )
            
            # Convert numpy types to native Python types
            converted = [self._convert_to_native(m) for m in measurements]
            
            return {
                'measurements': converted,
                'count': len(converted)
            }
        
        except Exception as e:
            logger.error(f"Error getting physics history: {e}")
            return {'measurements': [], 'count': 0}
