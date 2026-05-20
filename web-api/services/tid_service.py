"""
TID (Traveling Ionospheric Disturbance) service.

Reads the L3 ``tid`` data product written by ``PhysicsFusionService``
(P-H29).  One row per event detected by ``TIDDetector.detect_tid()``;
see ``src/hf_timestd/schemas/l3_tid_v1.json`` for the field list.

History: prior to v7 / P-H29 wiring this service read a bespoke
per-date directory structure (``phase2/science/tid/<YYYY-MM-DD>/
tid_events.json`` + ``tid_<event_id>.h5``) that *nothing* in the
pipeline wrote.  Replaced with the standard ``DataProductReader``
backed by HDF5+SQLite, the same machinery the rest of the L3 web
endpoints use.
"""

import sys
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

import numpy as np

# Add parent directory to path for hf_timestd imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'src'))

from hf_timestd.io import make_data_product_reader
from hf_timestd.data_product_registry import DataProductRegistry
from config import config

logger = logging.getLogger(__name__)


class TIDService:
    """
    Service for accessing TID (Traveling Ionospheric Disturbance)
    detections from the L3 ``tid`` data product.

    Data location (resolved via DataProductRegistry):
        ``<data_root>/phase2/fusion/tid/``
    """

    def __init__(self, data_root: Path):
        """
        Initialize TID service.

        Args:
            data_root: Root directory for data products (typically
                ``/var/lib/timestd``).
        """
        self.data_root = Path(data_root)
        # DataProductRegistry handles the ``fusion:tid`` subdirectory
        # convention so we don't hard-code the path here.  Resolve from
        # the registered location; falls back to the explicit path if
        # the registry is unavailable for any reason.
        try:
            tid_dir = DataProductRegistry.get_fusion_data_dir(
                self.data_root / 'phase2',
                product_level='L3',
                product_name='tid',
            )
        except Exception:  # pragma: no cover - registry should never fail
            tid_dir = self.data_root / 'phase2' / 'fusion' / 'tid'
        self.tid_dir = Path(tid_dir)
        self.reader: Optional[Any]
        try:
            self.reader = make_data_product_reader(
                data_dir=self.tid_dir,
                product_level='L3',
                product_name='tid',
                channel='AGGREGATED',
                storage_config=config.storage,
            )
        except Exception as e:
            logger.warning(f"TID reader init failed ({e}); TID endpoints will return empty results")
            self.reader = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_recent_events(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get recent TID events.

        Args:
            hours: Number of hours to look back from now (UTC).

        Returns:
            List of TID event dictionaries, newest first.  Empty list
            on missing data or read failure -- never raises.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        return self.get_events_in_range(start, end)

    def get_events_in_range(
        self,
        start: datetime,
        end: datetime,
    ) -> List[Dict[str, Any]]:
        """
        Get TID events within a UTC time range.

        Args:
            start: Start time (timezone-aware; naive is treated as UTC).
            end: End time (timezone-aware; naive is treated as UTC).

        Returns:
            List of TID event dictionaries sorted newest-first.
        """
        if self.reader is None:
            return []
        try:
            start_utc = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            end_utc = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
            rows = self.reader.read_time_range(
                start=start_utc.isoformat().replace('+00:00', 'Z'),
                end=end_utc.isoformat().replace('+00:00', 'Z'),
            )
            events = [self._row_to_event(r) for r in rows]
            events.sort(
                key=lambda e: e.get('timestamp_utc', ''),
                reverse=True,
            )
            return events
        except Exception as e:
            logger.error(f"Error getting TID events: {e}")
            return []

    def get_event_details(self, event_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single TID event by ``event_id``.

        Event IDs are minted by the writer as ``YYYYMMDD_HHMMSS_<n_paths>``;
        we search the day the event_id encodes plus the surrounding day
        as a safety margin against minute-boundary edge cases.

        Args:
            event_id: Event identifier from the L3 record.

        Returns:
            Event dictionary, or None if not found.
        """
        if self.reader is None or len(event_id) < 8:
            return None
        try:
            yyyymmdd = event_id[:8]
            day_start = datetime.strptime(yyyymmdd, '%Y%m%d').replace(tzinfo=timezone.utc)
            # Search ±1 day so an event at 00:00 UTC isn't missed.
            events = self.get_events_in_range(
                day_start - timedelta(days=1),
                day_start + timedelta(days=2),
            )
            for ev in events:
                if ev.get('event_id') == event_id:
                    return ev
            return None
        except Exception as e:
            logger.error(f"Error getting TID event details: {e}")
            return None

    def get_statistics(self, days: int = 7) -> Dict[str, Any]:
        """
        Compute summary statistics over the last ``days`` of events.

        Args:
            days: Lookback window (days).

        Returns:
            Dictionary with ``n_events``, per-day rate, velocity stats,
            period stats, direction histogram.  Empty/None fields when
            no events.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        events = self.get_events_in_range(start, end)

        if not events:
            return {
                'n_events': 0,
                'period_days': days,
                'events_per_day': 0,
                'velocity_stats': None,
                'period_stats': None,
                'direction_distribution': None,
            }

        velocities = [
            e['velocity_m_s'] for e in events
            if isinstance(e.get('velocity_m_s'), (int, float))
            and math.isfinite(e['velocity_m_s'])
        ]
        directions = [
            e['direction_deg'] for e in events
            if isinstance(e.get('direction_deg'), (int, float))
            and math.isfinite(e['direction_deg'])
        ]
        periods = [
            e['period_minutes'] for e in events
            if isinstance(e.get('period_minutes'), (int, float))
            and math.isfinite(e['period_minutes'])
        ]

        return {
            'n_events': len(events),
            'period_days': days,
            'events_per_day': len(events) / max(days, 1),
            'velocity_stats': {
                'mean_m_s': float(np.mean(velocities)),
                'std_m_s': float(np.std(velocities)),
                'min_m_s': float(np.min(velocities)),
                'max_m_s': float(np.max(velocities)),
            } if velocities else None,
            'period_stats': {
                'mean_minutes': float(np.mean(periods)),
                'std_minutes': float(np.std(periods)),
            } if periods else None,
            'direction_distribution': (
                self._compute_direction_histogram(directions)
                if directions else None
            ),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: Dict[str, Any]) -> Dict[str, Any]:
        """Pass-through with NaN→None and explicit numeric coercion so
        the FastAPI JSON serializer doesn't choke."""
        def _safe(v: Any) -> Any:
            if isinstance(v, float) and not math.isfinite(v):
                return None
            return v
        return {k: _safe(v) for k, v in row.items()}

    @staticmethod
    def _compute_direction_histogram(directions: List[float]) -> Dict[str, int]:
        """Bin direction azimuths into 8 compass sectors."""
        sectors = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
        counts = {s: 0 for s in sectors}
        for az in directions:
            # 0° = N, 45° = NE, etc.; bin width 45°, centred on each sector.
            idx = int(((az + 22.5) % 360) // 45)
            counts[sectors[idx]] += 1
        return counts
