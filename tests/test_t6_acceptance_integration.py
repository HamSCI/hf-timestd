"""The five behaviours T6_ACCEPTANCE_CRITERIA.md §7 names."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_bpsk_pps_calibrator_mf import _make_bpsk_signal
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage
from hf_timestd.core.t6_battery import T6Battery

SR = 96000
BATCH = 1920
K = 30
EDGE = 47916.1672


def _noise_std_for(cn0_db_hz: float) -> float:
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _acquire(cn0_db_hz, *, seed=11, n_blocks=5, sigma_ms=0.001,
             chain_ns=16_618_000, carrier_freq_hz=0.0, edge=EDGE):
    """Drive the real stage into the real battery.  Returns verdicts."""
    stage = BpskEdgeFineStage(sample_rate=SR, fold_seconds=K)
    bat = T6Battery(sample_rate=SR, fold_seconds=K)
    sig = _make_bpsk_signal(
        duration_s=K * n_blocks + 1.0, sample_rate=SR,
        edge_offset_samples=edge, noise_std=_noise_std_for(cn0_db_hz),
        seed=seed, carrier_freq_hz=carrier_freq_hz,
    )
    out = []
    for i in range(0, len(sig), BATCH):
        est = stage.process_samples(sig[i:i + BATCH], i)
        if est is None:
            continue
        out.append(bat.evaluate(est, implied_chain_delay_ns=chain_ns,
                                reported_sigma_ms=sigma_ms,
                                cn0_db_hz=cn0_db_hz))
    return out


class TestTheStationsWeRun(unittest.TestCase):

    def test_an_ai6vn_shaped_station_reaches_t6(self):
        """No T5, no T3, T4 at 0.29 ms, an injected pilot at 77 dB-Hz.
        Under the old gate this station sat at T2 while its edge landed
        on one sample position 128 seconds running."""
        verdicts = _acquire(77.0)
        self.assertTrue(verdicts, "no estimate at all")
        self.assertTrue(any(v.passed for v in verdicts),
                        f"{[v.failures for v in verdicts]}")

    @unittest.skipUnless(__import__("os").environ.get("T6_SWEEP"),
                         "slow: set T6_SWEEP=1")
    def test_a_b4_shaped_station_reaches_t6_at_its_worst_hour(self):
        """48.4 dB-Hz, measured 2026-08-28.  A criterion that only holds
        on an injected pilot ships a gate that fails every night."""
        verdicts = _acquire(48.4, n_blocks=6)
        self.assertTrue(verdicts, "no estimate at all")
        self.assertTrue(any(v.passed for v in verdicts),
                        f"{[v.failures for v in verdicts]}")


class TestTheFailuresWeRefuse(unittest.TestCase):

    def test_a_lattice_phantom_is_refused(self):
        """B4 locked onto a 20.000 ms lattice on 2026-09-04 and held it.
        A phantom passes every repeatability check, so the fixture is
        built from prominence and width -- the criteria that read shape."""
        from hf_timestd.core.bpsk_edge_fine_stage import FineEdgeEstimate
        bat = T6Battery(sample_rate=SR, fold_seconds=K)
        v = None
        for i in range(5):
            est = FineEdgeEstimate(
                edge_offset_samples=47916.0,
                edge_rtp=1_000_000 + 47916 + i * SR * K,
                edge_subsample=0.0, n_seconds_folded=K,
                plateau_amplitude=1.0, fit_rms=0.02,
                fold_retention=0.98, split_half_delta_samples=0.02,
                peak_prominence=0.001,             # a clean flip IS present
                apex_distance_samples=-1919.0,     # but the lock is 20 ms off it
                transition_width_samples=6.0,
            )
            v = bat.evaluate(est, implied_chain_delay_ns=16_618_000,
                             reported_sigma_ms=0.001, cn0_db_hz=70.0,
                             search_mode="seeded")
        self.assertFalse(v.passed)
        self.assertIn("shape", v.failures)

    def test_a_477_ms_sigma_is_refused(self):
        """The AI6VN reading of 2026-09-18, which widened the judge's
        bound to ~2.4 s and let a 284 ms disagreement through."""
        verdicts = _acquire(77.0, sigma_ms=477.0)
        self.assertTrue(verdicts)
        self.assertTrue(all("sigma" in v.failures for v in verdicts),
                        f"{[v.failures for v in verdicts]}")

    def test_a_missing_reference_cable_names_itself(self):
        """Two days of detector theory chased a missing coax.  The
        battery must say 'retention', not 'detector'."""
        # 0.5 Hz, NOT 1.0.  The fold runs modulo one second, so a carrier
        # at exactly 1 Hz presents identical phase at the same fold
        # position every second and folds COHERENTLY -- the fixture would
        # prove nothing.  0.5 Hz rotates 180 degrees per second, and the
        # stage's sign alternation exists to undo the SIGNAL's polarity
        # flip rather than the carrier's, so it survives as genuinely
        # destructive.  Measured 2026-09-19: retention 0.998 at 1 Hz,
        # 0.016 at 0.5 Hz.
        verdicts = _acquire(77.0, carrier_freq_hz=0.5)
        if verdicts:
            self.assertTrue(any("retention" in v.failures for v in verdicts),
                            f"{[v.failures for v in verdicts]}")

    def test_a_gross_wrap_is_refused(self):
        verdicts = _acquire(77.0, chain_ns=400_000_000)
        self.assertTrue(verdicts)
        self.assertTrue(all("plausibility" in v.failures for v in verdicts))


class TestTheOrdinalIsNotAJudge(unittest.TestCase):

    def test_an_ordinal_two_seconds_wrong_alarms_without_moving_t6(self):
        # The brief's illustrative code names `_t6_check_ordinal` and
        # `_get_ordinal_reference`; neither exists in core_recorder_v2.py.
        # The real §5.3 implementation is `_t6_check_named_second(edge_rtp,
        # named_second)`, exercised the same way `test_t6_ordinal_
        # crosscheck.py` drives it -- adapted here to the real signature
        # rather than adjusting the test to fit stale names.
        from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
        from hf_timestd.core.native_anchor import NativeAnchor
        r = SimpleNamespace()
        r._t6_check_named_second = \
            CoreRecorderV2._t6_check_named_second.__get__(r)
        r._t6_say_once = CoreRecorderV2._t6_say_once.__get__(r)
        r.T6_REPEAT_PERIOD_SEC = CoreRecorderV2.T6_REPEAT_PERIOD_SEC
        r.T6_ORDINAL_DISAGREEMENT_ALARM_SEC = \
            CoreRecorderV2.T6_ORDINAL_DISAGREEMENT_ALARM_SEC
        r._t6_say_once_at = {}
        anchor = NativeAnchor(
            anchor_rtp=47916, anchor_utc_ns=1_700_000_000_000_000_000,
            sample_rate_hz=SR, chain_delay_ns=16_618_000,
            captured_at_utc_ns=1_700_000_000_000_000_000,
            captured_via_tier="T2")
        r._t6_native_anchor = anchor
        # T6 extrapolates 10 s past the anchor; the namer says 12 s past
        # it -- two seconds wrong, past T6_ORDINAL_DISAGREEMENT_ALARM_SEC.
        edge_rtp = 47916 + 10 * SR
        named_second = 1_700_000_000 + 12
        with self.assertLogs("hf_timestd.core.core_recorder_v2",
                             level="WARNING") as cm:
            d = r._t6_check_named_second(edge_rtp, named_second)
        self.assertAlmostEqual(abs(d), 2.0, places=3)
        self.assertTrue(any("named" in line.lower() for line in cm.output))
        # T6 keeps its own anchor -- the disagreement indicts the namer,
        # never moves T6.
        self.assertIs(r._t6_native_anchor, anchor)


if __name__ == "__main__":
    unittest.main()
