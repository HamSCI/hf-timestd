"""Acquisition runs on the fold; the namer only says which second."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
from hf_timestd.core.t6_battery import T6Battery

SR = 96000
K = 30
NAMED_SECOND = 1_700_000_000


def _estimate(edge_rtp, **over):
    kw = dict(
        edge_offset_samples=47916.0, edge_rtp=edge_rtp, edge_subsample=0.0,
        n_seconds_folded=K, plateau_amplitude=1.0, fit_rms=0.02,
        fold_retention=0.98, split_half_delta_samples=0.05,
        peak_prominence=0.001, apex_distance_samples=0.5,
        transition_width_samples=6.0,
    )
    kw.update(over)
    return FineEdgeEstimate(**kw)


def _recorder(named=NAMED_SECOND):
    """A bare namespace carrying only what the method touches."""
    r = SimpleNamespace()
    for name in ("_t6_disambiguate_via_external_reference",
                 "_t6_say_once", "_t6_reported_sigma_ms"):
        setattr(r, name, getattr(CoreRecorderV2, name).__get__(r))
    r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
    r._t6_say_once_at = {}
    r._t6_name_integer_second = lambda _rtp: named
    r._t6_battery = T6Battery(sample_rate=SR, fold_seconds=K)
    r._t6_last_verdict = None
    r._t6_disambiguation_ns = 0
    r._t6_native_anchor = None
    r._t6_rate_reset = lambda _why: None
    r._t6_channel_info = SimpleNamespace(chain_delay_correction_ns=None)
    r._t6_calibrator = SimpleNamespace(sample_rate=SR,
                                       _last_edge_rtp=1_000_000 + 47916)
    r._t6_last_fine_est = None
    r._t6_chain_delay_history = [16_618_000, 16_618_100, 16_617_900]
    return r


def _drive(r, result, n=4, **over):
    with mock.patch("ka9q.rtp_recorder.rtp_to_utc",
                    return_value=float(NAMED_SECOND) + 0.499125 + 0.016618):
        for i in range(n):
            r._t6_last_fine_est = _estimate(
                1_000_000 + 47916 + i * SR * K, **over)
            r._t6_disambiguate_via_external_reference(result)


class TestAcquisitionUsesTheFold(unittest.TestCase):

    def test_a_passing_battery_captures_an_anchor(self):
        r = _recorder()
        _drive(r, SimpleNamespace(chain_delay_ns=16_618_000))
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertTrue(r._t6_last_verdict.passed,
                        r._t6_last_verdict.failures)

    def test_a_failing_battery_captures_nothing(self):
        r = _recorder()
        _drive(r, SimpleNamespace(chain_delay_ns=16_618_000),
               fold_retention=0.006)
        self.assertIsNone(r._t6_native_anchor)
        self.assertIn("retention", r._t6_last_verdict.failures)

    def test_no_named_second_refuses_without_capturing(self):
        """_t6_name_integer_second returns None when neither NMEA nor the
        radiod-pair wall lands within 0.4 s of an integer."""
        r = _recorder(named=None)
        _drive(r, SimpleNamespace(chain_delay_ns=16_618_000))
        self.assertIsNone(r._t6_native_anchor)

    def test_the_plausibility_guard_still_refuses_a_gross_wrap(self):
        r = _recorder()
        _drive(r, SimpleNamespace(chain_delay_ns=400_000_000))
        self.assertIsNone(r._t6_native_anchor)

    def test_the_anchor_is_built_on_the_named_second(self):
        r = _recorder()
        _drive(r, SimpleNamespace(chain_delay_ns=16_618_000))
        self.assertIsNotNone(r._t6_native_anchor)
        self.assertAlmostEqual(
            r._t6_native_anchor.captured_at_utc_ns / 1e9,
            float(NAMED_SECOND), places=3)

    def test_the_old_sigma_gate_is_no_longer_consulted(self):
        """_get_disambiguation_reference must not be called: its 10 us bar
        is the blocker this task removes."""
        r = _recorder()
        r._get_disambiguation_reference = mock.Mock(
            side_effect=AssertionError("old gate consulted"))
        _drive(r, SimpleNamespace(chain_delay_ns=16_618_000))
        r._get_disambiguation_reference.assert_not_called()


class TestReportedSigma(unittest.TestCase):

    def test_sigma_comes_from_the_chain_delay_history(self):
        r = _recorder()
        sigma = r._t6_reported_sigma_ms()
        self.assertIsNotNone(sigma)
        self.assertLess(sigma, 1.0)

    def test_too_few_samples_reports_none(self):
        r = _recorder()
        r._t6_chain_delay_history = [16_618_000]
        self.assertIsNone(r._t6_reported_sigma_ms())


if __name__ == "__main__":
    unittest.main()
