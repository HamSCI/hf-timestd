"""Regression test for M-H22: L2 startup seed must not swallow read errors.

`L2CalibrationService._seed_last_processed` reads the latest L2 row per
channel to resume from the last calibrated minute. A bare `except: continue`
would swallow any read error: an unseedable channel left the cursor at 0 and
the next cycle silently reprocessed the entire lookback window — a storm
with no logged cause.

Fix: log at WARNING when the read fails for any reason (corrupt DB, missing
table, unreadable row), and again when a channel ends up with no usable
seed value.

Post-Phase-4 (HDF5 → SQLite): the seed reads SQLite via
``make_data_product_reader`` rather than walking `*.h5` files; the
regression contract is unchanged.
"""

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hf_timestd.core.l2_calibration_service import L2CalibrationService
from hf_timestd.io.sqlite_writer import SqliteDataProductWriter


def _bare_service(
    data_root: Path,
    channel: str,
    db_path: Path,
) -> L2CalibrationService:
    """An L2CalibrationService with just the attributes _seed_last_processed
    needs — bypassing the heavy constructor (propagation solver, writers)."""
    svc = object.__new__(L2CalibrationService)
    svc.data_root = data_root
    svc.channels = [channel]
    svc.last_processed = {channel: 0}
    svc.lookback_minutes = 10
    svc._storage_config = {
        "read_sqlite": True,
        "sqlite_path": str(db_path),
    }
    return svc


class TestSeedLogging(unittest.TestCase):

    def test_unreadable_db_is_logged_not_swallowed(self) -> None:
        """A garbage SQLite file is logged at WARNING; cursor stays at 0."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = 'wwv10'
            db_path = root / 'phase2' / 'timestd.db'
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # Existing-but-corrupt DB: the reader fails on first query.
            db_path.write_bytes(b"\x00 definitely not a SQLite file \x00")

            svc = _bare_service(root, channel, db_path)

            with self.assertLogs(level='WARNING') as cm:
                svc._seed_last_processed()
            blob = '\n'.join(cm.output)

            # Per-channel failure is logged with the storm-cause hint.
            self.assertIn(channel, blob)
            self.assertIn('reprocesses the full lookback window', blob)
            # The cursor is left at 0 (unseeded) — but now visibly so.
            self.assertEqual(svc.last_processed[channel], 0)

    def test_valid_l2_row_seeds_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = 'wwv10'
            db_path = root / 'phase2' / 'timestd.db'
            db_path.parent.mkdir(parents=True, exist_ok=True)

            recent = time.time() - 120.0  # 2 minutes ago
            minute_boundary = int(recent // 60) * 60
            ts_iso = datetime.fromtimestamp(
                recent, tz=timezone.utc
            ).isoformat().replace('+00:00', 'Z')

            writer = SqliteDataProductWriter(
                output_dir=root / 'phase2' / channel / 'clock_offset',
                product_level='L2',
                product_name='timing_measurements',
                channel=channel,
                db_path=db_path,
            )
            try:
                writer.write_measurement({
                    'timestamp_utc': ts_iso,
                    'minute_boundary_utc': minute_boundary,
                    'rtp_timestamp': 0,
                    'station': 'WWV',
                    'frequency_mhz': 10.0,
                    'discrimination_method': 'TONE',
                    'discrimination_confidence': 1.0,
                    'tone_detected': True,
                    'raw_arrival_time_ms': 0.0,
                    'clock_offset_ms': 0.0,
                    'uncertainty_ms': 0.1,
                    'expanded_uncertainty_ms': 0.2,
                    'coverage_factor': 2.0,
                    'confidence_level': 0.95,
                    'u_rtp_timestamp_ms': 0.0,
                    'u_ionospheric_ms': 0.0,
                    'u_multipath_ms': 0.0,
                    'u_discrimination_ms': 0.0,
                    'u_gpsdo_ms': 0.0,
                    'u_propagation_model_ms': 0.0,
                    'degrees_of_freedom': 1,
                    'quality_grade': 'A',
                    'confidence': 1.0,
                    'quality_flag': 'GOOD',
                    'traceability_chain': 'test',
                    'processing_version': 'test',
                    'processed_at': ts_iso,
                    'calibration_date': ts_iso,
                    'gpsdo_locked': True,
                })
            finally:
                writer.close()

            svc = _bare_service(root, channel, db_path)

            # A readable row seeds the cursor and emits no WARNING.
            with self.assertNoLogs(level='WARNING'):
                svc._seed_last_processed()
            self.assertEqual(svc.last_processed[channel], minute_boundary)

    def test_missing_db_leaves_cursor_at_zero_silently(self) -> None:
        """No DB file = no prior data — silent skip, not a warning."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = 'wwv10'
            db_path = root / 'phase2' / 'timestd.db'
            # Note: db_path intentionally does NOT exist.

            svc = _bare_service(root, channel, db_path)

            # A missing DB is the cold-start case; the reader returns []
            # without logging, and the seed silently skips the channel.
            with self.assertNoLogs(level='WARNING'):
                svc._seed_last_processed()
            self.assertEqual(svc.last_processed[channel], 0)


if __name__ == '__main__':
    unittest.main()
