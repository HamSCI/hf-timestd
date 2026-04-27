"""Tests for StreamRecorderV2.register_with() — the shared-MultiStream entry point.

The plan in tasks/todo.md (step 1) introduces register_with() alongside
the legacy start()/_create_channel() path so CoreRecorderV2 can move
all archive channels and the L6/PPS calibrator onto a single shared
MultiStream subscription. These tests cover the contract:

  * ensure_channel() is invoked with the same kwargs the legacy path uses
  * the shared MultiStream's add_channel() receives the per-channel
    callback (self._handle_samples) and the same per-channel parameters
  * archive_writer.add_timing_snapshot and ring_buffer.update_anchor
    are still called with the GPS/RTP pair from the resolved
    ChannelInfo (precision-critical step)
  * no per-channel RadiodStream is created (self.stream stays None)
  * self._parent_multi captures the MultiStream so shutdown can stop it
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.stream_recorder_v2 import (
    StreamRecorderConfig,
    StreamRecorderV2,
)


def _make_config(**overrides) -> StreamRecorderConfig:
    """Minimal StreamRecorderConfig that constructs a StreamRecorderV2
    without spinning up archive writers or ring buffers."""
    base = dict(
        ssrc=None,
        frequency_hz=7_850_000,
        sample_rate=24_000,
        preset='iq',
        encoding=4,
        agc_enable=0,
        gain=0.0,
        description='TEST_CHU_7850',
        output_dir=Path('/tmp'),
        receiver_grid='AA00aa',
        station_config={},
        archive=False,        # skip BinaryArchiveWriter init
        ring_seconds=0,       # skip RingBuffer create
    )
    base.update(overrides)
    return StreamRecorderConfig(**base)


def _make_channel_info(ssrc=0xCAFE_BABE,
                       multicast_address='239.241.146.159',
                       gps_time=1_777_293_000_000_000_000,
                       rtp_timesnap=42_424_242):
    """Mock ChannelInfo with the fields register_with reads."""
    info = MagicMock()
    info.ssrc = ssrc
    info.multicast_address = multicast_address
    info.port = 5004
    info.gps_time = gps_time
    info.rtp_timesnap = rtp_timesnap
    return info


class TestRegisterWith(unittest.TestCase):

    def setUp(self):
        self.control = MagicMock()
        self.channel_info = _make_channel_info()
        self.control.ensure_channel.return_value = self.channel_info
        self.control.get_capabilities.return_value = {}  # not phase-engine

        self.sr = StreamRecorderV2(
            config=_make_config(),
            control=self.control,
        )

        # _set_filter_edges sends bytes to control when low_edge/high_edge
        # are set; with both None it's a no-op.  Stub it anyway so the
        # test isn't sensitive to that path.
        self.sr._set_filter_edges = MagicMock()

        self.multi = MagicMock()

    def test_returns_without_creating_radiod_stream(self):
        self.sr.register_with(self.multi)
        # Legacy per-channel stream MUST NOT exist in the shared-MultiStream path.
        self.assertIsNone(self.sr.stream)
        # Parent MultiStream is captured for shutdown to find later.
        self.assertIs(self.sr._parent_multi, self.multi)

    def test_ensure_channel_kwargs_match_legacy(self):
        self.sr.register_with(self.multi)
        self.control.ensure_channel.assert_called_once()
        kwargs = self.control.ensure_channel.call_args.kwargs
        self.assertEqual(kwargs['frequency_hz'], 7_850_000.0)
        self.assertEqual(kwargs['preset'], 'iq')
        self.assertEqual(kwargs['sample_rate'], 24_000)
        self.assertEqual(kwargs['encoding'], 4)
        self.assertEqual(kwargs['agc_enable'], 0)
        self.assertEqual(kwargs['gain'], 0.0)
        # Idiomatic ka9q-python calls used by the legacy _create_channel.
        self.assertEqual(kwargs['timeout'], 10.0)
        self.assertEqual(kwargs['frequency_tolerance'], 1.0)

    def test_add_channel_wires_handle_samples(self):
        self.sr.register_with(self.multi)
        self.multi.add_channel.assert_called_once()
        kwargs = self.multi.add_channel.call_args.kwargs
        self.assertEqual(kwargs['frequency_hz'], 7_850_000.0)
        self.assertEqual(kwargs['preset'], 'iq')
        self.assertEqual(kwargs['sample_rate'], 24_000)
        self.assertEqual(kwargs['encoding'], 4)
        # Critical: the parent MultiStream dispatches by SSRC to OUR callback.
        # Bound methods are recreated on each attribute access; equality
        # holds when they wrap the same function on the same instance.
        self.assertEqual(kwargs['on_samples'], self.sr._handle_samples)

    def test_ssrc_propagates_to_config(self):
        self.sr.register_with(self.multi)
        self.assertEqual(self.sr.config.ssrc, 0xCAFE_BABE)
        self.assertIs(self.sr.channel_info, self.channel_info)

    def test_archive_writer_seeded_with_gps_rtp(self):
        # Inject a Mock archive_writer post-init (config.archive=False
        # means the real init left it None — emulate the archive=True
        # case by attribute injection).
        self.sr.archive_writer = MagicMock()
        self.sr.register_with(self.multi)
        self.sr.archive_writer.add_timing_snapshot.assert_called_once_with(
            gps_time_ns=1_777_293_000_000_000_000,
            rtp_timesnap=42_424_242,
        )

    def test_ring_buffer_anchor_updated_with_gps_rtp(self):
        self.sr.ring_buffer = MagicMock()
        self.sr.register_with(self.multi)
        self.sr.ring_buffer.update_anchor.assert_called_once_with(
            gps_time_ns=1_777_293_000_000_000_000,
            rtp_timesnap=42_424_242,
        )

    def test_missing_timing_does_not_raise(self):
        # Operator may run against a radiod with no GPSDO yet; we should
        # log a warning and continue, not crash.
        self.channel_info.gps_time = None
        self.channel_info.rtp_timesnap = None
        self.sr.archive_writer = MagicMock()
        self.sr.ring_buffer = MagicMock()
        self.sr.register_with(self.multi)
        self.sr.archive_writer.add_timing_snapshot.assert_not_called()
        self.sr.ring_buffer.update_anchor.assert_not_called()


class TestPhaseEngineKwargsForwarded(unittest.TestCase):
    """When the backend is phase-engine, register_with must forward the
    same extended kwargs that the legacy _create_channel does, so a
    radiod restart that re-creates the channel under our control still
    applies reception_mode/target/etc. correctly."""

    def test_phase_engine_kwargs_added(self):
        control = MagicMock()
        control.ensure_channel.return_value = _make_channel_info()
        control.get_capabilities.return_value = {"backend": "phase-engine"}

        sr = StreamRecorderV2(
            config=_make_config(
                reception_mode='dual',
                target='S001',
                null_targets=['S002'],
                combining_method='wmrc',
            ),
            control=control,
        )
        sr._set_filter_edges = MagicMock()
        sr.register_with(MagicMock())

        kwargs = control.ensure_channel.call_args.kwargs
        self.assertEqual(kwargs['reception_mode'], 'dual')
        self.assertEqual(kwargs['target'], 'S001')
        self.assertEqual(kwargs['null_targets'], ['S002'])
        self.assertEqual(kwargs['combining_method'], 'wmrc')


if __name__ == '__main__':
    unittest.main()
