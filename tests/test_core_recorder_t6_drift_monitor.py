"""Tests for the T6 drift monitor (Layer 2).

Two independent signals are checked:

* **Signal A — anchor consistency.**  At each poll the captured anchor
  ``(gps_time, rtp_timesnap)`` is projected forward by elapsed gps_time and
  compared to radiod's freshly reported rtp_timesnap.  A counter rollback
  raises ``_t6_drift_flag_anchor_discontinuity`` immediately; a large
  *residual* raises it only after breaching the threshold on
  ``T6_ANCHOR_DISCONTINUITY_POLLS`` consecutive polls (the persistence gate
  — a lone noisy reading must not trigger a re-capture).

* **Signal B — sustained Δ breach.**  When ``|Δ| > T6_DRIFT_HARD_THRESHOLD_NS``
  for at least ``T6_DRIFT_SUSTAINED_SEC`` continuous seconds,
  ``_t6_drift_flag_sustained`` is raised.  Brief breaches do NOT raise the
  flag; return-to-normal clears it.

Layer 2 is monitor-only — flags are surfaced via ``_write_status`` and
forwarded by ``BpskPpsProbe`` into ``authority.json`` but do not yet drive
re-capture (that is Layer 3).  These tests pin only the detection logic.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


def _bare_recorder(sample_rate: int = 24000) -> CoreRecorderV2:
    """Build a CoreRecorderV2 with just the drift-monitor state populated.

    Mirrors the style of ``test_core_recorder_t6_step_recovery.py`` —
    ``__new__`` bypass + manual attribute population so the test does
    not need to construct a full channel set.
    """
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._t6_drift_first_breach_wall = None
    cr._t6_drift_flag_sustained = False
    cr._t6_drift_flag_anchor_discontinuity = False
    cr._t6_drift_anchor_residual_samples = None
    cr._t6_drift_residual_breach_count = 0
    cr._t6_drift_last_check_wall = None
    cr._t6_drift_anchor_gps_ns = None
    cr._t6_drift_anchor_rtp_timesnap = None
    cr._t6_calibrator = MagicMock(sample_rate=sample_rate)
    return cr


class TestAnchorConsistency(unittest.TestCase):
    def test_first_call_seeds_anchor(self):
        cr = _bare_recorder()
        cr._t6_check_anchor_consistency(1_000_000_000, 500_000)
        self.assertEqual(cr._t6_drift_anchor_gps_ns, 1_000_000_000)
        self.assertEqual(cr._t6_drift_anchor_rtp_timesnap, 500_000)
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)
        self.assertIsNotNone(cr._t6_drift_last_check_wall)

    def test_healthy_drift_keeps_flag_clear(self):
        """One second of elapsed gps_time should advance rtp_timesnap by
        exactly sample_rate samples.  A handful of samples of slop is the
        normal radiod status-emit jitter and must NOT trip the flag."""
        cr = _bare_recorder(sample_rate=24000)
        cr._t6_check_anchor_consistency(0, 0)  # seed
        # 10 s elapsed → 240_000 samples expected.  Add 50 samples noise.
        cr._t6_check_anchor_consistency(10_000_000_000, 240_050)
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_residual_samples, 50)

    def test_single_large_residual_does_not_raise_flag(self):
        """The dominant failure mode: a lone noisy reading must NOT raise
        the flag — that was what re-captured the anchor every minute."""
        cr = _bare_recorder(sample_rate=24000)
        cr._t6_check_anchor_consistency(0, 0)  # seed
        # 1 s elapsed expects 24_000 samples; deliver 34_000 → +10_000.
        cr._t6_check_anchor_consistency(1_000_000_000, 34_000)
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_residual_samples, 10_000)
        self.assertEqual(cr._t6_drift_residual_breach_count, 1)

    def test_sustained_residual_raises_flag_after_N_polls(self):
        """A genuine discontinuity — a fixed offset present on every poll —
        raises the flag, but only on the Nth consecutive breaching poll."""
        cr = _bare_recorder(sample_rate=24000)
        cr._t6_check_anchor_consistency(0, 0)  # seed
        n = CoreRecorderV2.T6_ANCHOR_DISCONTINUITY_POLLS
        # Each poll carries a fixed +10_000-sample offset (radiod restart
        # / clock step): rtp = elapsed×rate + 10_000.
        for k in range(1, n):
            cr._t6_check_anchor_consistency(
                k * 1_000_000_000, k * 24_000 + 10_000,
            )
            self.assertFalse(
                cr._t6_drift_flag_anchor_discontinuity,
                f"must not flag before {n} consecutive breaches (k={k})",
            )
            self.assertEqual(cr._t6_drift_residual_breach_count, k)
        cr._t6_check_anchor_consistency(n * 1_000_000_000, n * 24_000 + 10_000)
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_residual_samples, 10_000)

    def test_sustained_negative_residual_also_raises_flag(self):
        """A sustained residual in the other direction is just as much a
        discontinuity once it persists."""
        cr = _bare_recorder(sample_rate=24000)
        cr._t6_check_anchor_consistency(0, 0)  # seed
        n = CoreRecorderV2.T6_ANCHOR_DISCONTINUITY_POLLS
        for k in range(1, n + 1):
            cr._t6_check_anchor_consistency(
                k * 1_000_000_000, k * 24_000 - 10_000,
            )
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)
        self.assertEqual(cr._t6_drift_anchor_residual_samples, -10_000)

    def test_isolated_outliers_never_raise_flag(self):
        """Outlier readings interleaved with clean ones — the real noise
        pattern — must never reach the persistence threshold; each clean
        reading resets the counter."""
        cr = _bare_recorder(sample_rate=24000)
        cr._t6_check_anchor_consistency(0, 0)  # seed
        for k in range(1, 40, 2):
            # an outlier (~+80_000 ≈ a stale status packet)
            cr._t6_check_anchor_consistency(
                k * 1_000_000_000, k * 24_000 + 80_000,
            )
            self.assertEqual(cr._t6_drift_residual_breach_count, 1)
            # a clean reading immediately after — counter resets
            cr._t6_check_anchor_consistency(
                (k + 1) * 1_000_000_000, (k + 1) * 24_000,
            )
            self.assertEqual(cr._t6_drift_residual_breach_count, 0)
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)

    def test_gps_rollback_raises_flag(self):
        """radiod restart resets gps_time to a smaller value.  Rollback is
        an unambiguous namespace change — the projection math doesn't even
        need to run."""
        cr = _bare_recorder()
        cr._t6_check_anchor_consistency(10_000_000_000, 240_000)
        cr._t6_check_anchor_consistency(5_000_000_000, 0)
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)

    def test_rtp_rollback_raises_flag(self):
        cr = _bare_recorder()
        cr._t6_check_anchor_consistency(10_000_000_000, 240_000)
        cr._t6_check_anchor_consistency(11_000_000_000, 100_000)
        self.assertTrue(cr._t6_drift_flag_anchor_discontinuity)

    def test_missing_sample_rate_does_not_crash(self):
        """If the calibrator hasn't published sample_rate yet, Signal A's
        projection math is skipped — rollback detection still runs."""
        cr = _bare_recorder()
        cr._t6_calibrator = None  # no rate available
        cr._t6_check_anchor_consistency(0, 0)
        cr._t6_check_anchor_consistency(1_000_000_000, 99_999)
        # Residual not computed, no crash, rollback didn't fire (numbers grew).
        self.assertFalse(cr._t6_drift_flag_anchor_discontinuity)
        self.assertIsNone(cr._t6_drift_anchor_residual_samples)


class TestDeltaBreach(unittest.TestCase):
    def test_below_threshold_keeps_flag_clear(self):
        cr = _bare_recorder()
        cr._t6_check_delta_breach(100_000)  # 100 µs
        self.assertFalse(cr._t6_drift_flag_sustained)
        self.assertIsNone(cr._t6_drift_first_breach_wall)

    def test_single_breach_arms_timer_only(self):
        """The first breach starts the duration clock but does NOT raise
        the flag — Layer 2 only flags *sustained* breaches."""
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(2_000_000)  # 2 ms — above threshold
        self.assertFalse(cr._t6_drift_flag_sustained)
        self.assertEqual(cr._t6_drift_first_breach_wall, 1000.0)

    def test_short_breach_clears_on_recovery(self):
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(2_000_000)
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1010.0):  # 10 s later — well below 60 s window
            cr._t6_check_delta_breach(50_000)  # back to 50 µs
        self.assertFalse(cr._t6_drift_flag_sustained)
        self.assertIsNone(cr._t6_drift_first_breach_wall)

    def test_sustained_breach_raises_flag(self):
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(2_000_000)  # arm timer at t=1000
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1061.0):  # 61 s later, still breaching
            cr._t6_check_delta_breach(2_000_000)
        self.assertTrue(cr._t6_drift_flag_sustained)

    def test_just_under_sustained_threshold_stays_clear(self):
        """Boundary check — 59 s of breach is not yet "sustained"."""
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(2_000_000)
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1059.0):
            cr._t6_check_delta_breach(2_000_000)
        self.assertFalse(cr._t6_drift_flag_sustained)

    def test_sustained_breach_clears_on_recovery(self):
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(2_000_000)
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1100.0):
            cr._t6_check_delta_breach(2_000_000)  # flag now raised
        self.assertTrue(cr._t6_drift_flag_sustained)
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1200.0):
            cr._t6_check_delta_breach(50_000)  # recovery
        self.assertFalse(cr._t6_drift_flag_sustained)
        self.assertIsNone(cr._t6_drift_first_breach_wall)

    def test_negative_delta_uses_absolute_value(self):
        """The threshold is on |Δ| — a large *negative* offset must also
        arm the timer."""
        cr = _bare_recorder()
        with patch('hf_timestd.core.core_recorder_v2.time.monotonic',
                   return_value=1000.0):
            cr._t6_check_delta_breach(-2_000_000)
        self.assertEqual(cr._t6_drift_first_breach_wall, 1000.0)


class TestProbeForwarding(unittest.TestCase):
    """BpskPpsProbe must forward the ``drift_monitor`` block into its
    ProbeResult.detail when the producer publishes one.  Probes from
    older producers (no drift_monitor key) must still work."""

    def test_forwards_drift_monitor_when_present(self):
        import json
        import tempfile
        from datetime import datetime, timezone
        from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe

        now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
        status = {
            'timestamp': now.isoformat().replace('+00:00', 'Z'),
            'l6_pps': {
                'enabled': True,
                'locked': True,
                'pps_ok': 100,
                'pps_noise': 0,
                'pps_consecutive': 50,
                'chain_delay_ns': 1234,
                'local_minus_source_ns': 500,
                'drift_monitor': {
                    'sustained_breach': True,
                    'anchor_discontinuity': False,
                    'anchor_residual_samples': 42,
                    'breach_duration_sec': 75.0,
                    'last_check_age_sec': 3.0,
                    'hard_threshold_ns': 1_000_000,
                    'sustained_threshold_sec': 60.0,
                    'anchor_discontinuity_samples_threshold': 1000,
                },
            },
        }
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(status, f)
            path = Path(f.name)
        try:
            probe = BpskPpsProbe(status_path=path, now_fn=lambda: now)
            result = probe.poll()
            self.assertTrue(result.available)
            self.assertIn('drift_monitor', result.detail)
            self.assertTrue(result.detail['drift_monitor']['sustained_breach'])
            self.assertEqual(
                result.detail['drift_monitor']['anchor_residual_samples'], 42
            )
        finally:
            path.unlink()

    def test_absent_drift_monitor_omits_key(self):
        """An older producer (no Layer-2 block) must not error and must
        not synthesize a fake drift_monitor entry."""
        import json
        import tempfile
        from datetime import datetime, timezone
        from hf_timestd.core.bpsk_pps_probe import BpskPpsProbe

        now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
        status = {
            'timestamp': now.isoformat().replace('+00:00', 'Z'),
            'l6_pps': {
                'enabled': True,
                'locked': True,
                'pps_ok': 100,
                'pps_noise': 0,
                'pps_consecutive': 50,
                'chain_delay_ns': 1234,
                'local_minus_source_ns': 500,
            },
        }
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(status, f)
            path = Path(f.name)
        try:
            probe = BpskPpsProbe(status_path=path, now_fn=lambda: now)
            result = probe.poll()
            self.assertTrue(result.available)
            self.assertNotIn('drift_monitor', result.detail)
        finally:
            path.unlink()


if __name__ == '__main__':
    unittest.main()
