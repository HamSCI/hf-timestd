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
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import logging

from config import config

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

    def _get_latest_from_sqlite(self) -> Optional[Dict[str, Any]]:
        """Fall back to L2_chu_fsk SQLite for the most recent successful
        decode (last 3 days)."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

        import math

        try:
            from hf_timestd.io import make_data_product_reader
        except Exception as e:
            logger.debug(f"make_data_product_reader unavailable: {e}")
            return None

        storage_config = getattr(config, 'storage', {}) or {}

        now = datetime.now(timezone.utc)
        start_iso = (now - timedelta(days=3)).isoformat().replace('+00:00', 'Z')
        end_iso = now.isoformat().replace('+00:00', 'Z')

        best: Optional[Dict[str, Any]] = None
        best_ts = ''
        best_channel: Optional[str] = None

        phase2_dir = self.data_root / 'phase2'

        for channel in self.chu_channels:
            try:
                reader = make_data_product_reader(
                    data_dir=phase2_dir / channel / 'broadcast:fsk',
                    product_level='L2',
                    product_name='chu_fsk',
                    channel=channel,
                    storage_config=storage_config,
                )
            except Exception as e:
                logger.warning(f"L2_chu_fsk reader init failed for {channel}: {e}")
                continue

            try:
                try:
                    rows = reader.read_time_range(start=start_iso, end=end_iso)
                except Exception as e:
                    logger.warning(f"L2_chu_fsk read failed for {channel}: {e}")
                    rows = []
            finally:
                close_fn = getattr(reader, 'close', None)
                if close_fn is not None:
                    try:
                        close_fn()
                    except Exception:
                        pass

            # Walk rows newest→oldest, pick the most recent fsk_valid=True
            for row in reversed(rows):
                if not row.get('fsk_valid'):
                    continue
                ts = row.get('timestamp_utc') or ''
                if ts <= best_ts:
                    break  # rows are timestamp-ordered, older won't beat current
                best_ts = ts
                best_channel = channel
                dut1 = row.get('dut1_seconds')
                if dut1 is not None and math.isnan(dut1):
                    dut1 = None
                tai = row.get('tai_utc')
                if tai == 0:
                    tai = None
                best = {
                    'timestamp_utc': ts,
                    'frames_decoded': row.get('frames_decoded'),
                    'decode_confidence': row.get('decode_confidence'),
                    'decoded_day': row.get('decoded_day') or None,
                    'decoded_hour': row.get('decoded_hour') or None,
                    'decoded_minute': row.get('decoded_minute'),
                    'dut1_seconds': dut1,
                    'tai_utc': tai,
                    'timing_offset_ms': row.get('timing_offset_ms'),
                }
                break

        if best is None:
            return None

        return {
            'available': True,
            'channel': best_channel,
            'dut1_seconds': best.get('dut1_seconds'),
            'tai_utc': best.get('tai_utc'),
            'year': None,
            'timing_offset_ms': best.get('timing_offset_ms'),
            'tick_timing_offset_ms': None,
            'tick_timing_count': 0,
            'decode_confidence': best.get('decode_confidence'),
            'frames_decoded': best.get('frames_decoded'),
            'frames_total': 9,
            'snr_db': None,
            'decoded_day': best.get('decoded_day'),
            'decoded_hour': best.get('decoded_hour'),
            'decoded_minute': best.get('decoded_minute'),
            'last_decode': best_ts,
            'minute_boundary': None,
            'frame_results': [],
        }

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
                # Fall back to HDF5 for most recent successful decode
                sqlite_result = self._get_latest_from_sqlite()
                if sqlite_result:
                    return sqlite_result

                # No HDF5 history but we have a recent JSON attempt — return it
                # so the UI shows decode activity (frames, confidence) rather than
                # a generic "no data" message.
                if best is not None:
                    mb = best.get('minute_boundary')
                    last_decode = (
                        datetime.utcfromtimestamp(mb).isoformat() + 'Z'
                        if mb else None
                    )
                    return {
                        'available': True,
                        'detected': False,
                        'channel': best_channel,
                        'dut1_seconds': None,
                        'tai_utc': None,
                        'year': None,
                        'timing_offset_ms': None,
                        'tick_timing_offset_ms': None,
                        'tick_timing_count': best.get('tick_timing_count', 0),
                        'decode_confidence': best.get('decode_confidence'),
                        'frames_decoded': best.get('frames_decoded', 0),
                        'frames_total': 9,
                        'snr_db': best.get('snr_db'),
                        'decoded_day': None,
                        'decoded_hour': None,
                        'decoded_minute': None,
                        'last_decode': last_decode,
                        'minute_boundary': mb,
                        'frame_results': best.get('frame_results', []),
                    }

                # No JSON data at all — return channel status summary
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

                # Read FSK listener startup status for diagnostics
                listener_status = None
                try:
                    status_path = FSK_RESULTS_DIR / '_status.json'
                    if status_path.exists():
                        import json as _json
                        listener_status = _json.loads(status_path.read_text())
                except Exception:
                    pass

                # Build human-readable message
                if listener_status and listener_status.get('n_channels_ok', 0) == 0:
                    failed = [
                        f"{v['freq_mhz']:.2f} MHz: {v.get('error', 'unknown')}"
                        for v in listener_status.get('channels', {}).values()
                        if not v.get('ok')
                    ]
                    message = (
                        'CHU FSK listener failed to start USB channels. '
                        + ('; '.join(failed) if failed else 'Check core-recorder log.')
                    )
                elif not channel_status:
                    message = 'CHU FSK listener not running (no recent decode attempts)'
                else:
                    message = 'No CHU FSK data available in last 24 hours'

                return {
                    'available': False,
                    'message': message,
                    'channel_status': channel_status,
                    'listener_status': listener_status,
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
        from hf_timestd.io import make_data_product_reader

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
                reader = make_data_product_reader(
                    data_dir=channel_dir,
                    product_level='L2',
                    product_name='chu_fsk',
                    channel=channel,
                    storage_config=config.storage,
                )
                measurements = reader.read_time_range(
                    start=start.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                    end=end.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                )
                for m in measurements:
                    timestamps.append(m.get('timestamp_utc'))
                    dut1_values.append(m.get('dut1_seconds'))
                    timing_offsets.append(m.get('timing_offset_ms'))
                    confidences.append(m.get('decode_confidence'))
                    channel_names.append(channel)
            except Exception as e:
                logger.warning(f"Could not read FSK history from {channel}: {e}")

        return {
            'timestamps': timestamps,
            'dut1_seconds': dut1_values,
            'timing_offset_ms': timing_offsets,
            'decode_confidence': confidences,
            'channels': channel_names,
            'count': len(timestamps)
        }
