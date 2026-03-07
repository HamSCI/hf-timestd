"""
Chrony statistics service for the web API.

Reads chrony source comparison data from:
1. Live chronyc queries (current snapshot)
2. HDF5 files written by the fusion service (historical data)
"""

import sys
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.core.chrony_stats import (
    collect_chrony_snapshot,
    ChronySnapshot,
)

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
        """Get historical chrony stats from HDF5 files.

        Returns per-source time series of offset and std_dev.
        """
        try:
            import h5py
            import numpy as np
        except ImportError:
            return {'error': 'h5py not available', 'sources': {}}

        # Find today's (and optionally yesterday's) HDF5 files
        now = datetime.now(timezone.utc)
        dates = [now.strftime('%Y%m%d')]
        if hours > 12:
            from datetime import timedelta
            yesterday = (now - timedelta(days=1)).strftime('%Y%m%d')
            dates.insert(0, yesterday)

        all_rows = []
        for date_str in dates:
            h5_path = self.fusion_dir / f'chrony_stats_{date_str}.h5'
            if not h5_path.exists():
                continue
            try:
                with h5py.File(h5_path, 'r', libver='latest', swmr=True) as f:
                    if 'chrony_sources' not in f:
                        continue
                    grp = f['chrony_sources']
                    if 'unix_time' not in grp:
                        continue

                    n = len(grp['unix_time'])
                    if n == 0:
                        continue

                    # Filter to requested time range
                    unix_times = grp['unix_time'][:]
                    cutoff = now.timestamp() - hours * 3600
                    mask = unix_times >= cutoff

                    rows = {
                        'unix_time': unix_times[mask],
                        'source_name': grp['source_name'][:][mask] if 'source_name' in grp else None,
                        'offset_us': grp['offset_us'][:][mask] if 'offset_us' in grp else None,
                        'std_dev_us': grp['std_dev_us'][:][mask] if 'std_dev_us' in grp else None,
                        'source_state': grp['source_state'][:][mask] if 'source_state' in grp else None,
                        'n_samples': grp['n_samples'][:][mask] if 'n_samples' in grp else None,
                        'reach': grp['reach'][:][mask] if 'reach' in grp else None,
                        'sys_offset_s': grp['sys_offset_s'][:][mask] if 'sys_offset_s' in grp else None,
                    }
                    all_rows.append(rows)
            except Exception as e:
                logger.warning(f"Failed to read {h5_path}: {e}")

        if not all_rows:
            return {'sources': {}, 'system': []}

        # Merge and group by source
        import numpy as np
        unix_times = np.concatenate([r['unix_time'] for r in all_rows])
        source_names = np.concatenate([r['source_name'] for r in all_rows]) if all_rows[0]['source_name'] is not None else None
        offsets = np.concatenate([r['offset_us'] for r in all_rows]) if all_rows[0]['offset_us'] is not None else None
        std_devs = np.concatenate([r['std_dev_us'] for r in all_rows]) if all_rows[0]['std_dev_us'] is not None else None
        states = np.concatenate([r['source_state'] for r in all_rows]) if all_rows[0]['source_state'] is not None else None
        sys_offsets = np.concatenate([r['sys_offset_s'] for r in all_rows]) if all_rows[0]['sys_offset_s'] is not None else None

        if source_names is None or offsets is None:
            return {'sources': {}, 'system': []}

        # Decode bytes
        def _decode(arr):
            return [x.decode('utf-8') if isinstance(x, bytes) else str(x) for x in arr]

        source_names_str = _decode(source_names)
        unique_sources = sorted(set(source_names_str))

        sources_data = {}
        for src_name in unique_sources:
            mask = np.array([s == src_name for s in source_names_str])
            ts = unix_times[mask].tolist()
            offs = (offsets[mask] / 1000.0).tolist()  # us -> ms
            sds = (std_devs[mask] / 1000.0).tolist() if std_devs is not None else []
            sts = _decode(states[mask]) if states is not None else []

            sources_data[src_name] = {
                'timestamps': ts,
                'offset_ms': [round(x, 4) for x in offs],
                'std_dev_ms': [round(x, 4) for x in sds],
                'states': sts,
                'n_points': len(ts),
            }

        # System tracking time series
        system_ts = []
        if sys_offsets is not None:
            # Deduplicate — sys_offset is repeated per source row
            seen_times = set()
            for i in range(len(unix_times)):
                t = round(unix_times[i], 1)
                if t not in seen_times and not np.isnan(sys_offsets[i]):
                    seen_times.add(t)
                    system_ts.append({
                        'unix_time': unix_times[i],
                        'sys_offset_us': round(sys_offsets[i] * 1e6, 2),
                    })

        return {
            'sources': sources_data,
            'system': system_ts,
            'hours': hours,
            'n_sources': len(unique_sources),
        }
