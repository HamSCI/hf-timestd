"""Regression test for M-H22: L2 startup seed must not swallow parse errors.

`L2CalibrationService._seed_last_processed` read the newest L2 output file per
channel to resume from the last calibrated minute. A bare `except: continue`
swallowed every per-file error: a corrupt file left the cursor at 0 and the
next cycle silently reprocessed the entire lookback window — a storm with no
logged cause.

Fix: log at WARNING when a file fails to parse, and again when a channel ends
up with no readable file at all.
"""

import tempfile
import time
import unittest
from pathlib import Path

import h5py
import numpy as np

from hf_timestd.core.l2_calibration_service import L2CalibrationService


def _bare_service(data_root: Path, channel: str) -> L2CalibrationService:
    """An L2CalibrationService with just the attributes _seed_last_processed
    needs — bypassing the heavy constructor (propagation solver, writers)."""
    svc = object.__new__(L2CalibrationService)
    svc.data_root = data_root
    svc.channels = [channel]
    svc.last_processed = {channel: 0}
    svc.lookback_minutes = 10
    return svc


def _l2_dir(data_root: Path, channel: str) -> Path:
    d = data_root / "phase2" / channel / "clock_offset"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSeedLogging(unittest.TestCase):

    def test_corrupt_l2_file_is_logged_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = 'wwv10'
            l2_dir = _l2_dir(root, channel)
            # The only L2 file for this channel is unreadable garbage.
            (l2_dir / f"{channel}_timing_measurements_20260518.h5").write_bytes(
                b"\x00 definitely not an HDF5 file \x00"
            )
            svc = _bare_service(root, channel)

            with self.assertLogs(level='WARNING') as cm:
                svc._seed_last_processed()
            blob = '\n'.join(cm.output)

            # Per-file parse failure is logged ...
            self.assertIn('could not read L2 file', blob)
            self.assertIn(channel, blob)
            # ... and so is the channel-level consequence (the reprocess storm).
            self.assertIn('reprocesses the full lookback window', blob)
            # The cursor is left at 0 (unseeded) — but now visibly so.
            self.assertEqual(svc.last_processed[channel], 0)

    def test_valid_l2_file_seeds_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            channel = 'wwv10'
            l2_dir = _l2_dir(root, channel)
            recent = time.time() - 120.0  # 2 minutes ago
            with h5py.File(l2_dir / f"{channel}_timing_measurements_20260518.h5",
                           'w') as f:
                f.create_dataset('minute_boundary_utc',
                                 data=np.array([recent], dtype=np.float64))
            svc = _bare_service(root, channel)

            # A readable file seeds the cursor and emits no WARNING.
            with self.assertNoLogs(level='WARNING'):
                svc._seed_last_processed()
            self.assertGreater(svc.last_processed[channel], 0)
            self.assertEqual(svc.last_processed[channel],
                             int(recent // 60) * 60)


if __name__ == '__main__':
    unittest.main()
