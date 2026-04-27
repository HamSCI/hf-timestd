"""Tests for CoreRecorderV2._initialize_channels' shared-MultiStream path.

Plan: tasks/todo.md, step 2.

When ``recorder.shared_multistream = true`` in config, the init path
must:

  * build exactly one ka9q-python MultiStream;
  * call ``register_with(multi)`` on every StreamRecorderV2 it created;
  * register the resulting SSRC with the timing calibrator (when one
    is configured);
  * NOT call ``multi.start()`` — that's deferred until the T6 channel
    is also added (step 4 of the plan).

CoreRecorderV2.__init__ pulls in radiod discovery and a real
RadiodControl, so the tests bypass it via ``__new__`` and set only
the attributes ``_initialize_channels`` consumes. This is fragile to
internal changes by design; the live integration verification in step
7 is the real proof.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _make_core_recorder(
    *,
    use_shared: bool,
    n_channels: int = 3,
    with_calibrator: bool = True,
):
    """Build a CoreRecorderV2 with the minimum attributes
    ``_initialize_channels`` reads, bypassing the heavy __init__."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)

    # Channels (all SHARED-style 24 kHz IQ — keeps the spec simple)
    cr.channel_specs = [
        {
            'frequency_hz': 5_000_000 + 1_000_000 * i,
            'description': f'TEST_CH_{i}',
            'preset': 'iq',
            'sample_rate': 24_000,
            'encoding': 4,
            'agc': 0,
            'gain': 0.0,
            'archive': False,
        }
        for i in range(n_channels)
    ]
    cr.channel_defaults = {
        'preset': 'iq',
        'sample_rate': 24_000,
        'encoding': 4,
        'agc': 0,
        'gain': 0.0,
    }
    cr.engine_type = 'radiod'
    cr.station_config = {'callsign': 'TEST', 'grid_square': 'AA00aa'}
    cr.output_dir = Path('/tmp/timestd-shared-test')
    cr.output_dir.mkdir(parents=True, exist_ok=True)
    cr.recorder_config = {
        'archive': False,
        'ring_buffer': False,
        'file_duration_sec': 600,
    }
    cr.data_destination = None
    cr.recorders = {}
    cr.control = MagicMock()
    cr.calibrator = MagicMock() if with_calibrator else None
    cr._use_shared_multistream = use_shared
    cr._multi = None
    return cr


def _fake_streamrecorder_factory(call_log: list):
    """Returns a class that stands in for StreamRecorderV2.

    Construction is recorded; ``register_with(multi)`` is forwarded
    to the multi mock (via add_channel) and sets ``config.ssrc`` to
    a deterministic SSRC so the calibrator path is exercised.
    """
    def make(config, control, **kwargs):  # noqa: ARG001 — match signature
        instance = MagicMock()
        instance.config = config
        instance._handle_samples = MagicMock(name='_handle_samples')

        def _register_with(multi):
            # Mirror real register_with's externally observable effects:
            # set ssrc on config and call multi.add_channel.
            ssrc = (hash(config.description) & 0xFFFFFFFF) or 0xDEADBEEF
            config.ssrc = ssrc
            multi.add_channel(
                frequency_hz=float(config.frequency_hz),
                preset=config.preset,
                sample_rate=config.sample_rate,
                encoding=config.encoding,
                agc_enable=config.agc_enable,
                gain=config.gain,
                on_samples=instance._handle_samples,
            )

        instance.register_with = MagicMock(side_effect=_register_with)
        instance.start = MagicMock()
        call_log.append(instance)
        return instance
    return make


class TestSharedMultiStreamInit(unittest.TestCase):

    def test_shared_mode_builds_one_multi_and_registers_each_channel(self):
        cr = _make_core_recorder(use_shared=True, n_channels=4)

        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ):
            with patch('ka9q.MultiStream', create=True) as MockMulti:
                multi_instance = MagicMock()
                MockMulti.return_value = multi_instance
                ok = cr._initialize_channels()

        self.assertTrue(ok)
        # Exactly one MultiStream constructed for the whole service.
        MockMulti.assert_called_once()
        # Captured on the recorder for run() / shutdown to find later.
        self.assertIs(cr._multi, multi_instance)
        # Every recorder registered exactly once.
        self.assertEqual(len(sr_log), 4)
        for sr in sr_log:
            sr.register_with.assert_called_once_with(multi_instance)
        # And add_channel fired once per channel — that's our proof
        # the kernel will see one socket joining the multicast group
        # instead of N.
        self.assertEqual(multi_instance.add_channel.call_count, 4)
        # multi.start() is NOT called yet — the T6 channel needs to be
        # added first (step 3) and run() starts the multi (step 4).
        multi_instance.start.assert_not_called()

    def test_shared_mode_registers_ssrc_with_calibrator(self):
        cr = _make_core_recorder(use_shared=True, n_channels=2)
        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ):
            with patch('ka9q.MultiStream', create=True):
                cr._initialize_channels()

        # Calibrator gets one register_channel_ssrc call per channel.
        self.assertEqual(cr.calibrator.register_channel_ssrc.call_count, 2)
        # Verify the (description, ssrc) pairing makes sense.
        for call_args in cr.calibrator.register_channel_ssrc.call_args_list:
            description, ssrc = call_args.args
            self.assertTrue(description.startswith('TEST_CH_'))
            self.assertGreater(ssrc, 0)

    def test_shared_mode_skipped_when_calibrator_absent(self):
        # No calibrator is a valid configuration; init must not crash.
        cr = _make_core_recorder(
            use_shared=True, n_channels=2, with_calibrator=False,
        )
        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ):
            with patch('ka9q.MultiStream', create=True):
                ok = cr._initialize_channels()
        self.assertTrue(ok)

    def test_shared_multi_uses_correct_packet_size(self):
        # Hf-timestd's 24 kHz IQ channels carry 200 samples per RTP packet.
        # If the shared MultiStream uses ka9q-python's default of 320, the
        # resequencer's gap-detection skews by ~1.6× and reports phantom
        # losses.  Pin samples_per_packet=200 / resequence_buffer_size=128.
        cr = _make_core_recorder(use_shared=True, n_channels=1)
        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ):
            with patch('ka9q.MultiStream', create=True) as MockMulti:
                cr._initialize_channels()

        kwargs = MockMulti.call_args.kwargs
        self.assertEqual(kwargs['samples_per_packet'], 200)
        self.assertEqual(kwargs['resequence_buffer_size'], 128)

    def test_legacy_mode_skips_shared_wiring(self):
        # When the flag is off, _initialize_channels must NOT create a
        # MultiStream or call register_with — the existing run()-driven
        # recorder.start() loop owns the per-channel RadiodStreams.
        cr = _make_core_recorder(use_shared=False, n_channels=3)
        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ) as FakeSR:
            with patch('ka9q.MultiStream', create=True) as MockMulti:
                cr._initialize_channels()

        FakeSR.assert_called()
        MockMulti.assert_not_called()
        self.assertIsNone(cr._multi)
        for sr in sr_log:
            sr.register_with.assert_not_called()


if __name__ == '__main__':
    unittest.main()
