"""
CHU FSK Data Service.

Provides access to decoded CHU FSK time code data including:
- DUT1 (UT1-UTC) corrections
- TAI-UTC leap second count
- Decoded time verification
- FSK timing offset measurements

Reads from JSON files written by the FSK decoder at
/dev/shm/timestd/fsk_results/{iq_channel}.json
"""

import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)

FSK_RESULTS_DIR = Path('/dev/shm/timestd/fsk_results')


class CHUFSKService:
    """Service for accessing CHU FSK decoded data."""
    
    def __init__(self, data_root: Path):
        """
        Initialize CHU FSK service.
        
        Args:
            data_root: Root directory for data products (unused, kept for API compat)
        """
        self.data_root = Path(data_root)
        
        # CHU IQ channel names (match iq_channel in FSK config)
        self.chu_channels = ['CHU_3330', 'CHU_7850', 'CHU_14670']
    
    def _read_json(self, channel: str) -> Optional[Dict[str, Any]]:
        """Read the latest FSK result JSON for a channel."""
        path = FSK_RESULTS_DIR / f'{channel}.json'
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"Could not read {path}: {e}")
            return None

    def get_latest(self) -> Optional[Dict[str, Any]]:
        """
        Get latest CHU FSK decoded data from all channels.
        
        Reads JSON files written by the FSK decoder at
        /dev/shm/timestd/fsk_results/{channel}.json
        
        Returns:
            Dictionary with latest FSK data or unavailable placeholder
        """
        try:
            now = time.time()
            best = None
            best_channel = None
            
            for channel in self.chu_channels:
                data = self._read_json(channel)
                if data is None:
                    continue
                # Only consider results from last 5 minutes
                written_at = data.get('written_at', 0)
                if now - written_at > 300:
                    continue
                # Prefer detected results; among those, prefer highest confidence
                if best is None:
                    best = data
                    best_channel = channel
                elif data.get('detected') and not best.get('detected'):
                    best = data
                    best_channel = channel
                elif (data.get('detected') == best.get('detected')
                      and data.get('decode_confidence', 0) > best.get('decode_confidence', 0)):
                    best = data
                    best_channel = channel
            
            if best is None or not best.get('detected'):
                # Return all channel summaries even if not detected
                channel_status = []
                for channel in self.chu_channels:
                    data = self._read_json(channel)
                    if data and now - data.get('written_at', 0) < 300:
                        channel_status.append({
                            'channel': channel,
                            'detected': data.get('detected', False),
                            'frames_decoded': data.get('frames_decoded', 0),
                            'decode_confidence': data.get('decode_confidence', 0),
                            'written_at': data.get('written_at'),
                        })
                
                return {
                    'available': False,
                    'message': 'No CHU FSK decode in last 5 minutes',
                    'channel_status': channel_status,
                    'dut1_seconds': None,
                    'tai_utc': None,
                    'year': None,
                    'timing_offset_ms': None,
                    'decode_confidence': None,
                    'last_decode': None
                }
            
            # Format the minute boundary as ISO timestamp
            mb = best.get('minute_boundary')
            last_decode = (
                datetime.utcfromtimestamp(mb).isoformat() + 'Z'
                if mb else None
            )
            
            return {
                'available': True,
                'channel': best_channel,
                'dut1_seconds': best.get('dut1_seconds'),
                'tai_utc': best.get('tai_utc'),
                'year': best.get('year'),
                'timing_offset_ms': best.get('timing_offset_ms'),
                'tick_timing_offset_ms': best.get('tick_timing_offset_ms'),
                'tick_timing_count': best.get('tick_timing_count'),
                'decode_confidence': best.get('decode_confidence'),
                'frames_decoded': best.get('frames_decoded'),
                'frames_total': 9,
                'snr_db': best.get('snr_db'),
                'decoded_day': best.get('decoded_day'),
                'decoded_hour': best.get('decoded_hour'),
                'decoded_minute': best.get('decoded_minute'),
                'last_decode': last_decode,
                'minute_boundary': mb,
                'frame_results': best.get('frame_results', []),
            }
            
        except Exception as e:
            logger.error(f"Error getting CHU FSK data: {e}")
            return {
                'available': False,
                'message': f'Error: {str(e)}',
                'dut1_seconds': None,
                'tai_utc': None,
                'year': None,
                'timing_offset_ms': None,
                'decode_confidence': None,
                'last_decode': None
            }
    
    def get_all_channels(self) -> Dict[str, Any]:
        """
        Get current FSK status for all CHU channels.
        
        Returns:
            Dictionary with per-channel status
        """
        now = time.time()
        channels = {}
        
        for channel in self.chu_channels:
            data = self._read_json(channel)
            if data is None:
                channels[channel] = {'available': False}
                continue
            
            age = now - data.get('written_at', 0)
            mb = data.get('minute_boundary')
            channels[channel] = {
                'available': age < 300,
                'detected': data.get('detected', False),
                'frames_decoded': data.get('frames_decoded', 0),
                'decode_confidence': data.get('decode_confidence', 0),
                'timing_offset_ms': data.get('timing_offset_ms'),
                'tick_timing_offset_ms': data.get('tick_timing_offset_ms'),
                'minute_boundary': mb,
                'last_decode': (
                    datetime.utcfromtimestamp(mb).isoformat() + 'Z'
                    if mb else None
                ),
                'age_seconds': round(age, 1),
            }
        
        return channels

    def get_history(
        self,
        start: datetime,
        end: datetime
    ) -> Dict[str, Any]:
        """
        Get CHU FSK history from HDF5 files.
        
        Reads L2/chu_fsk HDF5 products written by the FSK listener.
        """
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))
        from hf_timestd.io.hdf5_reader import DataProductReader

        timestamps = []
        dut1_values = []
        timing_offsets = []
        confidences = []
        channel_names = []

        phase2_dir = self.data_root / 'phase2'

        for channel in self.chu_channels:
            channel_dir = phase2_dir / channel
            if not channel_dir.exists():
                continue
            try:
                reader = DataProductReader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='chu_fsk',
                    channel=channel,
                )
                measurements = reader.read_time_range(
                    start=start.isoformat() + 'Z',
                    end=end.isoformat() + 'Z',
                )
                for m in measurements:
                    timestamps.append(m.get('timestamp_utc'))
                    dut1_values.append(m.get('dut1_seconds'))
                    timing_offsets.append(m.get('timing_offset_ms'))
                    confidences.append(m.get('decode_confidence'))
                    channel_names.append(channel)
            except Exception as e:
                logger.debug(f"Could not read FSK history from {channel}: {e}")

        return {
            'timestamps': timestamps,
            'dut1_seconds': dut1_values,
            'timing_offset_ms': timing_offsets,
            'decode_confidence': confidences,
            'channels': channel_names,
            'count': len(timestamps)
        }
