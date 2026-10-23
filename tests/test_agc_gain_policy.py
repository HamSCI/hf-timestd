"""Channel gain policy (Michael, 2026-10-23).

No per-channel AGC in any client.  Every channel request sends
agc_enable=0 and gain 0.0 dB, whatever the config says.  F32 carries the
dynamic range.  Config that sets agc or a non-zero gain draws ONE warning
at start and has no effect.

Mutation that must fail these tests: in CoreRecorderV2._initialize_channels
read agc/gain from the channel spec again (the pre-policy lines), or in
_start_t6_stream read `t6.get('gain_db', 0.0)` again.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.stream_recorder_v2 import StreamRecorderConfig
from test_core_recorder_shared_multi import (
    _fake_streamrecorder_factory, _make_core_recorder)
from test_core_recorder_t6_shared import _make_t6_core_recorder


def _hostile_recorder(n=3):
    cr = _make_core_recorder(use_shared=False, n_channels=n)
    for spec in cr.channel_specs:
        spec['agc'] = True
        spec['gain'] = 12
    cr.channel_defaults['agc'] = 1
    cr.channel_defaults['gain'] = 12.0
    return cr


class TestChannelProvisioningClamped(unittest.TestCase):

    def test_stream_config_gets_agc_off_gain_zero(self):
        cr = _hostile_recorder()
        seen = []
        real_cfg = StreamRecorderConfig

        def spy(**kw):
            seen.append((kw.get('agc_enable'), kw.get('gain')))
            return real_cfg(**kw)

        with patch('hf_timestd.core.core_recorder_v2.StreamRecorderConfig', side_effect=spy), \
             patch('hf_timestd.core.core_recorder_v2.StreamRecorderV2',
                   side_effect=_fake_streamrecorder_factory([])):
            cr._initialize_channels()
        self.assertEqual(len(seen), 3)
        for agc, gain in seen:
            self.assertEqual(int(agc), 0)
            self.assertEqual(float(gain), 0.0)

    def test_stream_recorder_config_itself_clamps(self):
        c = StreamRecorderConfig(ssrc=None, frequency_hz=5e6, sample_rate=24000,
                                 agc_enable=1, gain=12.0)
        self.assertEqual(c.agc_enable, 0)
        self.assertEqual(c.gain, 0.0)

    def test_one_warning_not_per_channel(self):
        cr = _hostile_recorder(n=4)
        cr._t6_config = {'enabled': False, 'gain_db': 6.0}
        with self.assertLogs('hf_timestd.core.core_recorder_v2', level='WARNING') as lg, \
             patch('hf_timestd.core.core_recorder_v2.StreamRecorderV2',
                   side_effect=_fake_streamrecorder_factory([])):
            cr._initialize_channels()
        policy = [r for r in lg.records if 'policy ignores' in r.getMessage()]
        self.assertEqual(len(policy), 1)

    def test_no_warning_when_config_is_clean(self):
        cr = _make_core_recorder(use_shared=False, n_channels=2)
        cr._t6_config = {}
        logger = logging.getLogger('hf_timestd.core.core_recorder_v2')
        with patch.object(logger, 'warning') as w, \
             patch('hf_timestd.core.core_recorder_v2.StreamRecorderV2',
                   side_effect=_fake_streamrecorder_factory([])):
            cr._initialize_channels()
        self.assertFalse([c for c in w.call_args_list if 'policy ignores' in str(c)])


class TestT6GainClamped(unittest.TestCase):

    def test_t6_request_ignores_gain_db(self):
        cr = _make_t6_core_recorder(use_shared=False)
        cr._t6_config['gain_db'] = 6.0
        info = MagicMock()
        info.multicast_address = '239.241.146.159'
        info.ssrc = 0xC0FFEE
        cr.control.ensure_channel.return_value = info
        with patch('ka9q.RadiodStream', create=True):
            cr._start_t6_stream()
        kw = cr.control.ensure_channel.call_args.kwargs
        self.assertEqual(int(kw['agc_enable']), 0)
        self.assertEqual(float(kw['gain']), 0.0)


if __name__ == '__main__':
    unittest.main()
