"""
CHU FSK Data Service.

Provides access to decoded CHU FSK time code data including:
- DUT1 (UT1-UTC) corrections
- TAI-UTC leap second count
- Decoded time verification
- FSK timing offset measurements
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging
import math

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io.hdf5_reader import DataProductReader

logger = logging.getLogger(__name__)


class CHUFSKService:
    """Service for accessing CHU FSK decoded data."""
    
    def __init__(self, data_root: Path):
        """
        Initialize CHU FSK service.
        
        Args:
            data_root: Root directory for data products
        """
        self.data_root = Path(data_root)
        self.phase2_dir = self.data_root / 'phase2'
        
        # CHU channels
        self.chu_channels = ['CHU_3330', 'CHU_7850', 'CHU_14670']
        
    def _clean_value(self, val: Any) -> Any:
        """Clean value for JSON serialization."""
        if val is None:
            return None
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
        elif isinstance(obj, (np.integer, np.int64, np.int32)):
            return int(obj)
        elif isinstance(obj, (np.floating, np.float64, np.float32)):
            val = float(obj)
            if math.isnan(val) or math.isinf(val):
                return None
            return val
        elif isinstance(obj, np.bool_):
            return bool(obj)
        elif isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        else:
            return obj
    
    def get_latest(self) -> Optional[Dict[str, Any]]:
        """
        Get latest CHU FSK decoded data.
        
        Returns:
            Dictionary with latest FSK data or None if unavailable
        """
        try:
            # Try to find FSK data from any CHU channel
            all_fsk_data = []
            
            for channel in self.chu_channels:
                channel_dir = self.phase2_dir / channel
                if not channel_dir.exists():
                    continue
                
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='chu_fsk',
                        channel=channel
                    )
                    
                    # Try last 24 hours
                    end_time = datetime.utcnow()
                    start_time = end_time - timedelta(hours=24)
                    
                    measurements = reader.read_time_range(
                        start=start_time.isoformat() + 'Z',
                        end=end_time.isoformat() + 'Z'
                    )
                    
                    for m in measurements:
                        m['channel'] = channel
                        all_fsk_data.append(m)
                        
                except Exception as e:
                    logger.debug(f"Could not read FSK data from {channel}: {e}")
                    continue
            
            if not all_fsk_data:
                # Return placeholder with explanation
                return {
                    'available': False,
                    'message': 'No CHU FSK data available in last 24 hours',
                    'dut1_seconds': None,
                    'tai_utc': None,
                    'year': None,
                    'timing_offset_ms': None,
                    'decode_confidence': None,
                    'last_decode': None
                }
            
            # Sort by timestamp and get most recent
            all_fsk_data.sort(key=lambda x: x.get('timestamp_utc', ''), reverse=True)
            latest = self._convert_to_native(all_fsk_data[0])
            
            return {
                'available': True,
                'dut1_seconds': latest.get('dut1_seconds'),
                'tai_utc': latest.get('tai_utc'),
                'year': latest.get('year'),
                'timing_offset_ms': latest.get('timing_offset_ms'),
                'decode_confidence': latest.get('decode_confidence'),
                'frames_decoded': latest.get('frames_decoded'),
                'frames_total': latest.get('frames_total', 9),
                'snr_db': latest.get('snr_db'),
                'bit_error_rate': latest.get('bit_error_rate'),
                'channel': latest.get('channel'),
                'last_decode': latest.get('timestamp_utc'),
                'decoded_day': latest.get('decoded_day'),
                'decoded_hour': latest.get('decoded_hour'),
                'decoded_minute': latest.get('decoded_minute')
            }
            
        except Exception as e:
            logger.error(f"Error getting CHU FSK data: {e}")
            return {
                'available': False,
                'message': f'Error retrieving CHU FSK data: {str(e)}',
                'dut1_seconds': None,
                'tai_utc': None,
                'year': None,
                'timing_offset_ms': None,
                'decode_confidence': None,
                'last_decode': None
            }
    
    def get_history(
        self,
        start: datetime,
        end: datetime
    ) -> Dict[str, Any]:
        """
        Get CHU FSK history.
        
        Args:
            start: Start datetime
            end: End datetime
            
        Returns:
            Dictionary with time series data
        """
        try:
            all_fsk_data = []
            
            for channel in self.chu_channels:
                channel_dir = self.phase2_dir / channel
                if not channel_dir.exists():
                    continue
                
                try:
                    reader = DataProductReader(
                        data_dir=channel_dir,
                        product_level='L2',
                        product_name='chu_fsk',
                        channel=channel
                    )
                    
                    measurements = reader.read_time_range(
                        start=start.isoformat() + 'Z',
                        end=end.isoformat() + 'Z'
                    )
                    
                    for m in measurements:
                        m['channel'] = channel
                        all_fsk_data.append(m)
                        
                except Exception as e:
                    logger.debug(f"Could not read FSK history from {channel}: {e}")
                    continue
            
            # Sort by timestamp
            all_fsk_data.sort(key=lambda x: x.get('timestamp_utc', ''))
            
            # Convert to native types
            converted = [self._convert_to_native(m) for m in all_fsk_data]
            
            # Extract time series
            timestamps = [m.get('timestamp_utc') for m in converted]
            dut1_values = [m.get('dut1_seconds') for m in converted]
            timing_offsets = [m.get('timing_offset_ms') for m in converted]
            confidences = [m.get('decode_confidence') for m in converted]
            channels = [m.get('channel') for m in converted]
            
            return {
                'timestamps': timestamps,
                'dut1_seconds': dut1_values,
                'timing_offset_ms': timing_offsets,
                'decode_confidence': confidences,
                'channels': channels,
                'count': len(converted)
            }
            
        except Exception as e:
            logger.error(f"Error getting CHU FSK history: {e}")
            return {
                'timestamps': [],
                'dut1_seconds': [],
                'timing_offset_ms': [],
                'decode_confidence': [],
                'channels': [],
                'count': 0
            }
