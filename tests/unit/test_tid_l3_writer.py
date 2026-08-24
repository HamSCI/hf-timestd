"""hf-timestd's web-api TIDService reads the L3 TID product.

The TID *detector* and its product tests moved to hamsci-physics in the
2026-08-24 split; what stays here is the consumer side — this repo's
web API reading `phase2/fusion/tid/` through the frozen data contract.
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hf_timestd.data_product_registry import DataProductRegistry


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

class TestTidServiceReadback(unittest.TestCase):
    """The new TIDService reads from `phase2/fusion/tid/`; we don't
    spin up the FastAPI app here, just exercise the service class
    directly with a temporary data root."""

    def _service(self, td: Path):
        # The service module wants to `sys.path.insert` and import
        # `config`, which lives in the web-api directory.  We patch
        # `config.storage` minimally for the test.
        import sys
        web_api = Path(__file__).resolve().parents[2] / 'web-api'
        if str(web_api) not in sys.path:
            sys.path.insert(0, str(web_api))
        # `config` is the live web-api config module; we can't replace
        # it cleanly inside one test, but it exposes a `.storage` dict
        # we can mutate.
        import config as web_config
        web_config.config.storage = {'sqlite_path': str(td / 'timestd.db')}
        from services.tid_service import TIDService
        return TIDService(data_root=td)

    def test_empty_directory_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            svc = self._service(td)
            self.assertEqual(svc.get_recent_events(hours=24), [])
            self.assertEqual(
                svc.get_statistics(days=7)['n_events'], 0,
            )

    def test_writer_then_service_sees_event(self):
        """End-to-end through the public API surface: write one event
        via the standard writer, read it back through TIDService."""
        from hf_timestd.io import make_data_product_writer

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            tid_dir = DataProductRegistry.get_fusion_data_dir(
                td / 'phase2',
                product_level='L3',
                product_name='tid',
                create=True,
            )
            writer = make_data_product_writer(
                output_dir=tid_dir,
                product_level='L3',
                product_name='tid',
                channel='AGGREGATED',
                processing_version='1.0.0',
                storage_config={'sqlite_path': str(td / 'timestd.db')},
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
                'lagging_path': 'CHU_14.67',
                'lag_minutes': 2.0,
                'processing_version': '1.0.0',
            })
            writer.close()

            svc = self._service(td)
            events = svc.get_recent_events(hours=1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['event_id'], event_id)

            details = svc.get_event_details(event_id)
            self.assertIsNotNone(details)
            self.assertEqual(details['event_id'], event_id)

            stats = svc.get_statistics(days=1)
            self.assertEqual(stats['n_events'], 1)
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
