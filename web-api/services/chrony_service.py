"""
Chrony statistics service for the web API.

Reads chrony source comparison data from:
1. Live chronyc queries (current snapshot)
2. The DIAG_chrony_stats SQLite table written by the fusion service
   (historical data, populated since Phase 1).
"""

import sys
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from hf_timestd.core.chrony_stats import (
    collect_chrony_snapshot,
    ChronySnapshot,
)

try:
    from config import config as _web_config
except Exception:
    _web_config = None

from hf_timestd.io import make_data_product_reader

logger = logging.getLogger(__name__)


class ChronyService:
    """Service for chrony source comparison data."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        self.fusion_dir = self.data_root / 'phase2' / 'fusion'

    def get_live_snapshot(self) -> Optional[Dict[str, Any]]:
        """Get a live chrony snapshot from chronyc."""
        snap = collect_chrony_snapshot()
        if snap is None:
            return None
        return snap.to_dict()

    def get_source_comparison(self) -> Dict[str, Any]:
        """Get a formatted source comparison table (like the report we generated)."""
        snap = collect_chrony_snapshot()
        if snap is None:
            return {'error': 'chronyc not available', 'sources': []}

        sources = []
        for src in snap.sources:
            offset_ms = src.offset_us / 1000.0
            error_ms = src.error_us / 1000.0
            stddev_ms = src.std_dev_us / 1000.0

            source_type = 'unknown'
            if src.mode == '#':
                source_type = 'refclock'
            elif src.mode == '^':
                source_type = 'server'
            elif src.mode == '=':
                source_type = 'peer'

            state_desc = {
                '*': 'selected', '+': 'combined', '-': 'not_combined',
                'x': 'falseticker', '~': 'too_variable', '?': 'unusable',
            }.get(src.state, 'unknown')

            sources.append({
                'name': src.name,
                'type': source_type,
                'state': src.state,
                'state_desc': state_desc,
                'stratum': src.stratum,
                'offset_ms': round(offset_ms, 4),
                'error_ms': round(error_ms, 4),
                'std_dev_ms': round(stddev_ms, 4),
                'n_samples': src.n_samples,
                'frequency_ppm': round(src.frequency_ppm, 4),
                'freq_skew_ppm': round(src.freq_skew_ppm, 4),
                'reach': src.reach,
                'reach_oct': f'{src.reach:03o}',
                'poll': src.poll,
            })

        tracking = None
        if snap.tracking:
            t = snap.tracking
            tracking = {
                'reference_id': t.reference_id,
                'stratum': t.stratum,
                'system_time_offset_us': round(t.system_time_offset_s * 1e6, 2),
                'rms_offset_us': round(t.rms_offset_s * 1e6, 2),
                'frequency_ppm': round(t.frequency_ppm, 4),
                'residual_freq_ppm': round(t.residual_freq_ppm, 4),
                'skew_ppm': round(t.skew_ppm, 4),
                'root_delay_ms': round(t.root_delay_s * 1000, 4),
                'root_dispersion_ms': round(t.root_dispersion_s * 1000, 4),
                'update_interval_s': round(t.update_interval_s, 1),
                'leap_status': t.leap_status,
            }

        return {
            'timestamp_utc': snap.timestamp_utc,
            'tracking': tracking,
            'sources': sources,
        }

    def get_history(self, hours: int = 24) -> Dict[str, Any]:
        """Get historical chrony stats from the DIAG_chrony_stats SQLite
        table.  Returns per-source time series of offset and std_dev,
        plus a deduplicated system-tracking offset series.
        """
        now = datetime.now(timezone.utc)
        start_iso = (now - timedelta(hours=hours)).isoformat().replace('+00:00', 'Z')
        end_iso = now.isoformat().replace('+00:00', 'Z')

        storage_config = getattr(_web_config, 'storage', {}) if _web_config else {}

        try:
            reader = make_data_product_reader(
                data_dir=self.fusion_dir,
                product_level='DIAG',
                product_name='chrony_stats',
                channel='fusion',
                storage_config=storage_config,
            )
        except Exception as e:
            logger.warning(f"chrony history reader init failed: {e}")
            return {'sources': {}, 'system': []}

        try:
            try:
                rows = reader.read_time_range(start=start_iso, end=end_iso)
            except Exception as e:
                logger.warning(f"chrony history read failed: {e}")
                return {'sources': {}, 'system': []}
        finally:
            close_fn = getattr(reader, 'close', None)
            if close_fn is not None:
                try:
                    close_fn()
                except Exception:
                    pass

        if not rows:
            return {'sources': {}, 'system': []}

        sources_data: Dict[str, Dict[str, Any]] = {}
        seen_sys_times: set = set()
        system_ts: List[Dict[str, Any]] = []

        for row in rows:
            src_name = row.get('source_name')
            unix_time = row.get('unix_time')
            if not src_name or unix_time is None:
                continue
            entry = sources_data.setdefault(src_name, {
                'timestamps': [],
                'offset_ms': [],
                'std_dev_ms': [],
                'states': [],
                'n_points': 0,
            })
            offset_us = row.get('offset_us')
            std_dev_us = row.get('std_dev_us')
            state = row.get('source_state', '')
            entry['timestamps'].append(unix_time)
            entry['offset_ms'].append(
                round(offset_us / 1000.0, 4) if offset_us is not None else None
            )
            entry['std_dev_ms'].append(
                round(std_dev_us / 1000.0, 4) if std_dev_us is not None else None
            )
            entry['states'].append(state)
            entry['n_points'] += 1

            sys_offset_s = row.get('sys_offset_s')
            if sys_offset_s is not None:
                bucket = round(unix_time, 1)
                if bucket not in seen_sys_times:
                    seen_sys_times.add(bucket)
                    system_ts.append({
                        'unix_time': unix_time,
                        'sys_offset_us': round(sys_offset_s * 1e6, 2),
                    })

        return {
            'sources': sources_data,
            'system': system_ts,
            'hours': hours,
            'n_sources': len(sources_data),
        }
