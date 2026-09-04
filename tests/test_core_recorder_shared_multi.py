"""Tests for CoreRecorderV2._initialize_channels' shared-MultiStream path.

Plan: tasks/todo.md, step 2.

When ``recorder.shared_multistream = true`` in config, the init path
must:

  * build exactly one ka9q-python MultiStream;
  * call ``register_with(multi)`` on every StreamRecorderV2 it created;
  * record the resulting SSRC in the radiod LIFETIME keepalive list;
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
):
    """Build a CoreRecorderV2 with the minimum attributes
    ``_initialize_channels`` reads, bypassing the heavy __init__."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    # The loaded TOML dict; _initialize_channels reads [timing.authority_manager]
    # for the TimeMap wiring (2026-09-04).  Empty = every default.
    cr.config = {}

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
    cr._use_shared_multistream = use_shared
    cr._multi = None
    # _initialize_channels appends to this when opting channels into
    # radiod's LIFETIME tag (commit 0f8b622+, added 2026-05-08).  Real
    # __init__ initialises it to []; the __new__ fast-path has to too.
    cr._lifetime_entries = []
    cr._radiod_lifetime_frames = 0  # 0 = opt-out, no keep-alive thread
    # _initialize_channels passes this straight into every StreamRecorderV2
    # it builds (core_recorder_v2 ~:1504).  Real __init__ sets it to None
    # before the judge is constructed, so None is the faithful stand-in for
    # "no judge configured".
    cr._offset_judge = None
    # Passed to every StreamRecorderV2 as its status stream
    # (core_recorder_v2 ~:1505).
    cr.status_address = None
    return cr


def _fake_streamrecorder_factory(call_log: list):
    """Returns a class that stands in for StreamRecorderV2.

    Construction is recorded; ``register_with(multi)`` is forwarded
    to the multi mock (via add_channel) and sets ``config.ssrc`` to
    a deterministic SSRC so the LIFETIME-registration path is exercised.
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

    def test_shared_mode_records_each_ssrc_for_lifetime(self):
        cr = _make_core_recorder(use_shared=True, n_channels=2)
        sr_log: list = []
        with patch(
            'hf_timestd.core.core_recorder_v2.StreamRecorderV2',
            side_effect=_fake_streamrecorder_factory(sr_log),
        ):
            with patch('ka9q.MultiStream', create=True):
                cr._initialize_channels()

        # One LIFETIME entry per channel, each carrying a real SSRC.
        self.assertEqual(len(cr._lifetime_entries), 2)
        for _control, ssrc in cr._lifetime_entries:
            self.assertGreater(ssrc, 0)

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


class TestFastPathStaysComplete(unittest.TestCase):
    """The __new__ fast path must cover what _initialize_channels reads.

    These tests bypass __init__ and set "only the attributes
    _initialize_channels consumes" — which silently rots every time
    production reads one more.  The history is in this file: _lifetime_entries
    added 2026-05-08, then _offset_judge, then status_address, each found by
    a failure that named an attribute rather than a behaviour.

    This compares the two sets directly, so the next one is a clear message
    at the point of change instead of an AttributeError buried in a mock.
    """

    def test_no_attribute_is_missing_from_the_fast_path(self):
        import ast
        import re

        root = Path(__file__).resolve().parent.parent
        src = (root / "src" / "hf_timestd" / "core" / "core_recorder_v2.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_initialize_channels")

        reads, writes = set(), set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                (writes if isinstance(node.ctx, ast.Store) else reads).add(node.attr)
        methods = {n.name for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}
        needed = {a for a in reads - writes - methods if not a.startswith("__")}

        provided = set(re.findall(r"cr\.([A-Za-z_][A-Za-z_0-9]*)\s*=",
                                  Path(__file__).read_text()))
        missing = sorted(needed - provided)
        self.assertEqual(
            missing, [],
            f"_initialize_channels reads {missing} but _make_core_recorder "
            f"does not set them — add them to the fast path (the value real "
            f"__init__ would have given) rather than deleting the assertion")
