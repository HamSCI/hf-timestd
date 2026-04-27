"""Tests for _start_t6_stream's shared-MultiStream branch (plan step 3).

In shared mode the T6 BPSK PPS channel registers on the same
MultiStream as the archive channels (one socket for the whole
service); in legacy mode it owns its own RadiodStream.  The branch
also captures ``data_destination`` from the first ChannelInfo it
sees — needed for inventory reporting when the archive channels are
all silent at startup.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _make_t6_core_recorder(*, use_shared: bool, with_multi: bool = True):
    """Bypass __init__ and stand up just the attributes _start_t6_stream
    consumes."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr.control = MagicMock()
    cr.channel_defaults = {'sample_rate': 24_000}
    cr.recorders = {}
    cr.data_destination = None
    cr._use_shared_multistream = use_shared
    cr._multi = MagicMock() if (use_shared and with_multi) else None
    cr._t6_calibrator = MagicMock()
    cr._t6_stream = None
    cr._t6_config = {
        'enabled': True,
        'frequency_hz': 5_000_000,
        'sample_rate': 24_000,
        'description': 'BPSK_PPS',
    }
    return cr


class TestT6SharedMode(unittest.TestCase):

    def test_shared_mode_calls_multi_add_channel_with_t6_callback(self):
        cr = _make_t6_core_recorder(use_shared=True)
        channel_info = MagicMock()
        channel_info.multicast_address = '239.241.146.159'
        cr._multi.add_channel.return_value = channel_info

        cr._start_t6_stream()

        cr._multi.add_channel.assert_called_once()
        kwargs = cr._multi.add_channel.call_args.kwargs
        self.assertEqual(kwargs['frequency_hz'], 5_000_000.0)
        self.assertEqual(kwargs['preset'], 'iq')
        self.assertEqual(kwargs['sample_rate'], 24_000)
        self.assertEqual(kwargs['agc_enable'], False)
        self.assertEqual(kwargs['gain'], 0.0)
        # Most important: the T6 calibrator's callback drives the PPS lock
        # detector — it must be wired into the parent MultiStream's per-SSRC
        # dispatch.  Bound methods compare equal when wrapping the same
        # function on the same instance.
        self.assertEqual(kwargs['on_samples'], cr._t6_on_samples)

    def test_shared_mode_does_not_create_radiod_stream(self):
        cr = _make_t6_core_recorder(use_shared=True)
        cr._multi.add_channel.return_value = MagicMock()
        cr._start_t6_stream()
        # No legacy per-channel RadiodStream gets built in shared mode.
        self.assertIsNone(cr._t6_stream)

    def test_shared_mode_captures_data_destination(self):
        cr = _make_t6_core_recorder(use_shared=True)
        channel_info = MagicMock()
        channel_info.multicast_address = '239.241.146.159'
        cr._multi.add_channel.return_value = channel_info
        cr._start_t6_stream()
        self.assertEqual(cr.data_destination, '239.241.146.159')

    def test_shared_mode_without_multi_logs_error_and_returns(self):
        # If shared-mode init never built _multi (config error), don't
        # crash — log and return.
        cr = _make_t6_core_recorder(use_shared=True, with_multi=False)
        # _multi is None — no add_channel call to verify.  Must not raise.
        cr._start_t6_stream()
        self.assertIsNone(cr._t6_stream)

    def test_legacy_mode_still_creates_radiod_stream(self):
        # Verify the legacy code path is preserved verbatim for rollback.
        cr = _make_t6_core_recorder(use_shared=False)
        channel_info = MagicMock()
        channel_info.multicast_address = '239.241.146.159'
        cr.control.ensure_channel.return_value = channel_info

        # _start_t6_stream uses a function-local `from ka9q import RadiodStream`,
        # so the patch target is ka9q.RadiodStream itself (re-resolved at
        # call time), not a name imported into core_recorder_v2.
        with patch('ka9q.RadiodStream', create=True) as MockRS:
            stream_instance = MagicMock()
            MockRS.return_value = stream_instance
            cr._start_t6_stream()

        cr.control.ensure_channel.assert_called_once()
        MockRS.assert_called_once()
        rs_kwargs = MockRS.call_args.kwargs
        # Ensure the legacy RadiodStream still gets the T6 callback wired.
        self.assertEqual(rs_kwargs['on_samples'], cr._t6_on_samples)
        stream_instance.start.assert_called_once()
        self.assertIs(cr._t6_stream, stream_instance)


class TestSharedMultiShutdown(unittest.TestCase):
    """Step 4 of the plan: _shutdown() must stop the shared MultiStream
    BEFORE iterating recorders, so per-SSRC callbacks aren't dispatched
    into recorders mid-teardown."""

    def _make_cr_for_shutdown(self, *, with_multi: bool):
        cr = CoreRecorderV2.__new__(CoreRecorderV2)
        cr.control = MagicMock()
        cr._multi = MagicMock() if with_multi else None
        cr.recorders = {}  # empty — keeps the recorder loop a no-op
        cr._t6_stream = None
        cr.start_time = 0.0
        cr.output_dir = Path('/tmp/timestd-shared-test')
        cr.output_dir.mkdir(parents=True, exist_ok=True)
        cr.metrics = MagicMock()
        # _write_status reads several attrs we don't care about; stub it.
        cr._write_status = MagicMock()
        return cr

    def test_shutdown_stops_multi_when_present(self):
        cr = self._make_cr_for_shutdown(with_multi=True)
        cr._shutdown()
        cr._multi.stop.assert_called_once()

    def test_shutdown_with_no_multi_does_not_crash(self):
        # Legacy mode: cr._multi stays None.  _shutdown must not call
        # methods on it.
        cr = self._make_cr_for_shutdown(with_multi=False)
        cr._shutdown()  # must complete without raising

    def test_shutdown_stops_multi_before_recorders(self):
        # Order is load-bearing: callbacks must stop firing before
        # recorders go down, otherwise a sample arriving mid-teardown
        # could touch a half-closed archive_writer.
        cr = self._make_cr_for_shutdown(with_multi=True)
        recorder = MagicMock()
        recorder.config.ssrc = 0xC0FFEE
        recorder.config.description = 'TEST'
        recorder.stop.return_value = None
        cr.recorders = {'TEST': recorder}

        sequence: list = []
        cr._multi.stop.side_effect = lambda: sequence.append('multi.stop')
        recorder.stop.side_effect = lambda: sequence.append('recorder.stop')

        cr._shutdown()

        self.assertEqual(sequence[0], 'multi.stop')
        self.assertEqual(sequence[1], 'recorder.stop')


if __name__ == '__main__':
    unittest.main()
