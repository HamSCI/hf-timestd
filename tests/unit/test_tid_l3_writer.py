"""The L3 TID product writes through the frozen data contract.

The TID *detector* and its product tests moved to hamsci-physics in the
2026-08-24 split.  The web API that read this product moved to
station-web in the 2026-09-06 split (Phase 5) — `tests/test_routes.py`
there covers `/api/tid`.  What stays here is the writer side: this repo
still lays the product down under `phase2/fusion/tid/` through
`DataProductRegistry` + `make_data_product_writer`, and a reader must
find it there.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hf_timestd.data_product_registry import DataProductRegistry


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace('+00:00', 'Z')


class TestTidL3Writer(unittest.TestCase):

    def test_writer_lays_the_event_down_where_the_contract_says(self):
        """Write one event via the standard writer, read it back through
        this repo's own reader — no web layer involved."""
        from hf_timestd.io import (
            make_data_product_reader,
            make_data_product_writer,
        )

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            tid_dir = DataProductRegistry.get_fusion_data_dir(
                td / 'phase2',
                product_level='L3',
                product_name='tid',
                create=True,
            )
            storage_config = {'sqlite_path': str(td / 'timestd.db')}
            writer = make_data_product_writer(
                output_dir=tid_dir,
                product_level='L3',
                product_name='tid',
                channel='AGGREGATED',
                processing_version='1.0.0',
                storage_config=storage_config,
            )
            now = datetime.now(timezone.utc)
            event_id = now.strftime('%Y%m%d_%H%M%S') + '_3'
            writer.write_measurement({
                'timestamp_utc': now.isoformat().replace('+00:00', 'Z'),
                'minute_boundary_utc': int(now.timestamp()),
                'event_id': event_id,
                'period_minutes': 30.0,
                'amplitude_ms': 0.6,
                'velocity_m_s': 200.0,
                'direction_deg': 90.0,
                'correlation_coefficient': 0.75,
                'significance_p': 0.003,
                'confidence': 0.997,
                'n_paths_correlated': 3,
                'leading_path': 'WWV_15.0',
                'lagging_path': 'WWVH_15.0',
                'lag_minutes': 2.0,
                'processing_version': '1.0.0',
            })
            writer.close()

            reader = make_data_product_reader(
                data_dir=tid_dir,
                product_level='L3',
                product_name='tid',
                channel='AGGREGATED',
                storage_config=storage_config,
            )
            try:
                rows = reader.read_time_range(
                    _iso(now - timedelta(hours=1)),
                    _iso(now + timedelta(hours=1)),
                )
            finally:
                reader.close()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['event_id'], event_id)
            self.assertAlmostEqual(rows[0]['amplitude_ms'], 0.6, places=6)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
