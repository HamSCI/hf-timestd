"""A functional T6 is the bench.  Disagreement indicts the namer."""
from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate

SR = 96000
ANCHOR_RTP = 47916
ANCHOR_UTC_NS = 1_700_000_000_000_000_000


def _recorder(anchor=True):
    r = SimpleNamespace()
    r._t6_check_named_second = \
        CoreRecorderV2._t6_check_named_second.__get__(r)
    r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r.T6_ORDINAL_DISAGREEMENT_ALARM_SEC = \
        CoreRecorderV2.T6_ORDINAL_DISAGREEMENT_ALARM_SEC
    r._t6_say_once_at = {}
    r._t6_native_anchor = NativeAnchor(
        anchor_rtp=ANCHOR_RTP, anchor_utc_ns=ANCHOR_UTC_NS,
        sample_rate_hz=SR, chain_delay_ns=16_618_000,
        captured_at_utc_ns=ANCHOR_UTC_NS, captured_via_tier="namer",
    ) if anchor else None
    return r


class TestNamedSecondCrossCheck(unittest.TestCase):

    def test_an_agreeing_namer_raises_nothing(self):
        r = _recorder()
        # Ten seconds of RTP past the anchor: T6 says anchor_utc + 10.
        edge = ANCHOR_RTP + 10 * SR
        named = ANCHOR_UTC_NS // 1_000_000_000 + 10
        with self.assertNoLogs("hf_timestd.core.core_recorder_v2",
                               level="WARNING"):
            d = r._t6_check_named_second(edge, named)
        self.assertIsNotNone(d)
        self.assertLess(abs(d), 0.5)

    def test_a_two_second_error_alarms(self):
        r = _recorder()
        edge = ANCHOR_RTP + 10 * SR
        named = ANCHOR_UTC_NS // 1_000_000_000 + 12   # two seconds wrong
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            d = r._t6_check_named_second(edge, named)
        self.assertAlmostEqual(abs(d), 2.0, places=3)
        self.assertTrue(any("named" in line.lower() for line in cm.output))

    def test_the_alarm_indicts_the_namer_not_t6(self):
        r = _recorder()
        edge = ANCHOR_RTP + 10 * SR
        named = ANCHOR_UTC_NS // 1_000_000_000 + 12
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            r._t6_check_named_second(edge, named)
        joined = " ".join(cm.output).lower()
        self.assertIn("named", joined)
        self.assertNotIn("t6 suspect", joined)

    def test_t6_keeps_its_anchor_through_the_disagreement(self):
        r = _recorder()
        anchor = r._t6_native_anchor
        r._t6_check_named_second(ANCHOR_RTP + 10 * SR,
                                 ANCHOR_UTC_NS // 1_000_000_000 + 12)
        self.assertIs(r._t6_native_anchor, anchor)

    def test_no_anchor_means_no_check(self):
        r = _recorder(anchor=False)
        self.assertIsNone(r._t6_check_named_second(
            ANCHOR_RTP, ANCHOR_UTC_NS // 1_000_000_000))

    def test_no_named_second_means_no_check(self):
        r = _recorder()
        self.assertIsNone(r._t6_check_named_second(ANCHOR_RTP, None))

    def test_the_alarm_is_throttled(self):
        r = _recorder()
        edge = ANCHOR_RTP + 10 * SR
        named = ANCHOR_UTC_NS // 1_000_000_000 + 12
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            for _ in range(20):
                r._t6_check_named_second(edge, named)
        self.assertEqual(len(cm.output), 1, cm.output)

    def test_the_check_reads_no_wall_clock(self):
        """CLAUDE.md forbids a new time.time() in the timing path.  The
        anchor and the edge's RTP are sufficient."""
        import inspect
        src = inspect.getsource(CoreRecorderV2._t6_check_named_second)
        self.assertNotIn("time.time", src)
        self.assertNotIn("datetime.now", src)


def _bare_on_samples_recorder():
    """Minimal recorder able to survive a full ``_t6_on_samples`` call.

    Mirrors ``_bare_on_samples_recorder`` in
    ``test_core_recorder_t6_fine_integration.py``: the calibrator
    reports no result (unlocked/no data this batch) so the huge
    locked-branch is bypassed, and only the fine-stage feed block at
    the top of ``_t6_on_samples`` is under test.
    """
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._use_shared_multistream = True
    cr._t6_first_sample_logged = True
    cr._t6_calibrator = MagicMock()
    cr._t6_calibrator.process_samples.return_value = None
    cr._t6_last_chain_delay_ns = None
    cr._t6_disambiguation_ns = 0
    cr._t6_wrap_rejections = 0
    cr._t6_recent_raw = deque(maxlen=CoreRecorderV2.T6_STEP_RECOVERY_WINDOW)
    cr._t6_last_locked_wall = None
    cr._t6_shm = None
    cr._t6_channel_info = None
    cr.recorders = {}
    return cr


class TestNamedSecondCheckIsFailClosed(unittest.TestCase):
    """Task 7's call sits inside the existing fail-closed guard around
    the fine-stage block (spec §5.3, ⚠ in the brief): a raise from it
    must not skip the authority update that follows, which is the
    route by which T6 actually asserts.  An earlier task in this plan
    broke four passing tests exactly this way.
    """

    def _cr(self):
        cr = _bare_on_samples_recorder()
        cr._t6_fine_stage = MagicMock()
        cr._t6_fine_stage.process_samples.return_value = FineEdgeEstimate(
            edge_offset_samples=43_181.0, edge_rtp=1_000_000,
            edge_subsample=0.0, n_seconds_folded=30,
            plateau_amplitude=30.0, fit_rms=0.05,
        )
        cr._t6_authority = MagicMock()
        cr._t6_authority.on_tick.return_value = None
        cr._t6_name_integer_second = MagicMock(return_value=12345)
        return cr

    def test_a_raise_in_the_crosscheck_does_not_block_the_authority_update(
            self):
        cr = self._cr()
        cr._t6_check_named_second = MagicMock(
            side_effect=RuntimeError("boom"))
        applied = []
        cr._t6_apply_authority_decision = applied.append
        samples = MagicMock()
        quality = MagicMock(last_rtp_timestamp=0)
        cr._t6_on_samples(samples, quality)
        cr._t6_authority.on_fine_estimate.assert_called_once()
        self.assertEqual(len(applied), 1)

    def test_the_crosscheck_is_actually_called_with_the_edge_and_the_name(
            self):
        cr = self._cr()
        cr._t6_check_named_second = MagicMock(return_value=0.0)
        cr._t6_apply_authority_decision = MagicMock()
        samples = MagicMock()
        quality = MagicMock(last_rtp_timestamp=0)
        cr._t6_on_samples(samples, quality)
        cr._t6_check_named_second.assert_called_once_with(1_000_000, 12345)


if __name__ == "__main__":
    unittest.main()
