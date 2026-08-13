"""audit F15: _set_filter_edges must call the public
RadiodControl.set_filter() instead of hand-encoding LOW_EDGE/HIGH_EDGE
TLVs via ka9q.control internals (encode_int/encode_double/encode_eol/CMD).
set_filter() sends the identical TLV field set (order differs; radiod's
TLV decode is a tag-keyed linear scan, so order is irrelevant)."""

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


def _make_recorder(low_edge=None, high_edge=None):
    config = StreamRecorderConfig(
        ssrc=None, frequency_hz=7_850_000, sample_rate=24_000,
        preset='iq', encoding=4, agc_enable=0, gain=0.0,
        description='TEST_FILTER', output_dir=Path('/tmp'),
        receiver_grid='AA00aa', station_config={},
        archive=False,      # skip BinaryArchiveWriter init
        ring_seconds=0,     # skip RingBuffer create
        low_edge=low_edge, high_edge=high_edge,
    )
    control = MagicMock()
    return StreamRecorderV2(config=config, control=control), control


class TestSetFilterEdges(unittest.TestCase):

    def test_uses_public_set_filter(self):
        sr, control = _make_recorder(low_edge=-25_000, high_edge=25_000)
        sr._set_filter_edges(0xCAFE)
        control.set_filter.assert_called_once_with(
            0xCAFE, low_edge=-25_000.0, high_edge=25_000.0)
        control.send_command.assert_not_called()   # no hand-built TLV buffer

    def test_partial_edges_pass_none_through(self):
        # set_filter omits None fields — same wire behavior as the old code.
        sr, control = _make_recorder(low_edge=-3_000)
        sr._set_filter_edges(1)
        control.set_filter.assert_called_once_with(
            1, low_edge=-3_000.0, high_edge=None)

    def test_noop_when_unconfigured(self):
        sr, control = _make_recorder()
        sr._set_filter_edges(1)
        control.set_filter.assert_not_called()
        control.send_command.assert_not_called()

    def test_set_filter_failure_is_swallowed(self):
        # Best-effort semantics preserved: failures log a warning, never raise.
        sr, control = _make_recorder(low_edge=-3_000, high_edge=3_000)
        control.set_filter.side_effect = RuntimeError("radiod down")
        sr._set_filter_edges(1)   # must not raise
