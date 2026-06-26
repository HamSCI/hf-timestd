"""Tests for _start_t6_stream — T6 always uses a dedicated RadiodStream.

Rationale: when T6 rode the shared MultiStream alongside the archive
channels, an archive-channel rollover (zstd compression + fsync of a
~73 MB chunk every 10 min) blocked the shared receive thread for
3-5 s.  During that window the kernel UDP buffer overflowed and T6
dropped samples, the Costas loop unlocked, and chrony saw TSL3 ``?``
(reach=0) at every UTC :00/:10/:20/:30/:40/:50 boundary.  Fix: T6
always owns its own UDP socket and reader thread, isolated from
whatever the archive thread is doing.  The shared MultiStream is
still used for the archive channels.
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
    # Attributes touched by _start_t6_stream / shutdown that real
    # __init__ provides but our __new__ fast-path has to mirror by
    # hand.  Keep this list in sync with CoreRecorderV2.__init__.
    cr._t6_channel_info = None    # set by _start_t6_stream from ChannelInfo
    cr._lifetime_entries = []     # appended to when LIFETIME tag is opted in
    cr._radiod_lifetime_frames = 0  # 0 = opt-out (no keep-alive)
    # T6 timing-anchor refresh thread (V1 fix per TIMING-PIPELINE-WIRING
    # §10.3).  Legacy-mode _start_t6_stream checks the thread is None
    # and constructs a new one; without these attrs it AttributeErrors
    # before reaching the RadiodStream branch.
    cr._t6_timing_poll_thread = None
    import threading as _t
    cr._t6_timing_poll_stop = _t.Event()
    return cr


class TestT6DedicatedStream(unittest.TestCase):
    """T6 must build a dedicated RadiodStream regardless of the
    archive-channel ``shared_multistream`` setting.  When archive
    channels share a socket the rollover-flush would block T6 reads;
    T6 always gets its own socket so it stays insulated."""

    def _start_with_mocked_radiodstream(self, cr):
        """Patch ka9q.RadiodStream and ensure_channel, then call
        _start_t6_stream.  Returns (MockRS, stream_instance)."""
        channel_info = MagicMock()
        channel_info.multicast_address = '239.241.146.159'
        channel_info.ssrc = 0xC0FFEE
        cr.control.ensure_channel.return_value = channel_info

        # _start_t6_stream uses a function-local
        # ``from ka9q import RadiodStream``, so the patch target is
        # ka9q.RadiodStream itself (re-resolved at call time), not a
        # name imported into core_recorder_v2.
        with patch('ka9q.RadiodStream', create=True) as MockRS:
            stream_instance = MagicMock()
            MockRS.return_value = stream_instance
            cr._start_t6_stream()
        return MockRS, stream_instance, channel_info

    def test_shared_mode_skips_multi_add_channel_for_t6(self):
        cr = _make_t6_core_recorder(use_shared=True)
        self._start_with_mocked_radiodstream(cr)
        # T6 must NOT register on the shared MultiStream — archive
        # rollover would otherwise stall T6 reads every 10 min.
        cr._multi.add_channel.assert_not_called()

    def test_shared_mode_creates_dedicated_radiod_stream(self):
        cr = _make_t6_core_recorder(use_shared=True)
        MockRS, stream_instance, _ = self._start_with_mocked_radiodstream(cr)
        cr.control.ensure_channel.assert_called_once()
        MockRS.assert_called_once()
        stream_instance.start.assert_called_once()
        self.assertIs(cr._t6_stream, stream_instance)

    def test_legacy_mode_creates_dedicated_radiod_stream(self):
        cr = _make_t6_core_recorder(use_shared=False)
        MockRS, stream_instance, _ = self._start_with_mocked_radiodstream(cr)
        cr.control.ensure_channel.assert_called_once()
        MockRS.assert_called_once()
        stream_instance.start.assert_called_once()
        self.assertIs(cr._t6_stream, stream_instance)

    def test_t6_callback_wired_into_radiod_stream(self):
        cr = _make_t6_core_recorder(use_shared=True)
        MockRS, _, _ = self._start_with_mocked_radiodstream(cr)
        rs_kwargs = MockRS.call_args.kwargs
        # The calibrator's callback drives the PPS lock detector — it
        # must be wired into the dedicated stream's sample dispatch.
        self.assertEqual(rs_kwargs['on_samples'], cr._t6_on_samples)

    def test_captures_data_destination_from_channel_info(self):
        cr = _make_t6_core_recorder(use_shared=True)
        self._start_with_mocked_radiodstream(cr)
        self.assertEqual(cr.data_destination, '239.241.146.159')


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
        # _shutdown reads _t6_channel_info to evict the T6 channel from
        # radiod's table on stop (2026-04, T6 SSRC-orphan cleanup).
        # Real __init__ sets this to None; the __new__ fast-path has to
        # too, otherwise _shutdown raises AttributeError.
        cr._t6_channel_info = None
        # _shutdown also stops the WWVB decode loop/stream; real __init__
        # sets these unconditionally, so the __new__ fast-path must mirror
        # them or _shutdown raises AttributeError.
        cr._wwvb_decode_stop = MagicMock()
        cr._wwvb_decode_thread = None
        cr._wwvb_stream = None
        cr._wwvb_ledger = None
        cr._wwvb_l1_writer = None
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
