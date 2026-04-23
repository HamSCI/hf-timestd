#!/usr/bin/env python3
"""Unit tests for AuthorityRunner and build_authority_runner_from_config."""

import json
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from hf_timestd.core.authority_manager import (
    AuthorityManager,
    ProbeResult,
)
from hf_timestd.core.authority_runner import (
    AuthorityRunner,
    build_authority_runner_from_config,
)


@dataclass
class _CountingProbe:
    t_level: str = "T3"
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def poll(self) -> ProbeResult:
        with self._lock:
            self.calls += 1
        return ProbeResult(self.t_level, available=True, offset_ms=0.5, sigma_ms=0.3)


class TestAuthorityRunner(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self, probe) -> AuthorityManager:
        return AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
        )

    def test_eager_first_tick_publishes_promptly(self) -> None:
        probe = _CountingProbe()
        runner = AuthorityRunner(self._mgr(probe), interval_sec=10.0)
        runner.start()
        # Eager tick should run immediately; give the scheduler a moment.
        time.sleep(0.2)
        try:
            self.assertGreaterEqual(probe.calls, 1)
            self.assertTrue(self.out.exists())
            with self.out.open() as f:
                payload = json.load(f)
            self.assertEqual(payload["t_level_active"], "T3")
        finally:
            runner.stop(timeout=2.0)

    def test_stop_exits_promptly(self) -> None:
        probe = _CountingProbe()
        runner = AuthorityRunner(self._mgr(probe), interval_sec=5.0)
        runner.start()
        time.sleep(0.1)
        t0 = time.time()
        runner.stop(timeout=2.0)
        elapsed = time.time() - t0
        # Should NOT wait out the 5 s interval.
        self.assertLess(elapsed, 1.0)
        self.assertFalse(runner.is_alive())

    def test_probe_exception_does_not_kill_thread(self) -> None:
        class _BoomProbe:
            t_level = "T3"
            def __init__(self):
                self.calls = 0
            def poll(self):
                self.calls += 1
                raise RuntimeError("probe kaboom")
        probe = _BoomProbe()
        # Tiny interval so we get multiple ticks in the test window.
        runner = AuthorityRunner(self._mgr(probe), interval_sec=0.05)
        runner.start()
        time.sleep(0.25)
        try:
            self.assertGreaterEqual(probe.calls, 2)
            self.assertTrue(runner.is_alive())
        finally:
            runner.stop(timeout=1.0)

    def test_double_start_is_noop(self) -> None:
        probe = _CountingProbe()
        runner = AuthorityRunner(self._mgr(probe), interval_sec=5.0)
        runner.start()
        t1 = runner._thread
        runner.start()  # should not replace the thread
        self.assertIs(runner._thread, t1)
        runner.stop(timeout=2.0)


class TestBuildAuthorityRunnerFromConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_config_still_yields_t3_probe(self) -> None:
        runner = build_authority_runner_from_config(
            config={},
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        t_levels = [p.t_level for p in runner.manager.probes]
        self.assertEqual(t_levels, ["T3"])
        self.assertEqual(runner.interval_sec, 30.0)

    def test_full_config_registers_all_probes(self) -> None:
        cfg = {
            "timing": {
                "authority": {
                    "interval_sec": 10.0,
                    "upgrade_hysteresis": 2,
                    "a_level": "A1",
                    "t3": {"min_stations": 1, "freshness_sec": 45.0},
                    "t4": {"peers": ["timeserver.lan", "192.168.1.80"]},
                    "t5": {"refid": "GPS"},
                    "t2": {"enabled": True},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        t_levels = sorted(p.t_level for p in runner.manager.probes)
        self.assertEqual(t_levels, ["T2", "T3", "T4", "T5"])
        self.assertEqual(runner.interval_sec, 10.0)
        self.assertEqual(runner.manager.upgrade_hysteresis, 2)

    def test_t2_alone_with_no_t4_peers_matches_any_server(self) -> None:
        cfg = {
            "timing": {
                "authority": {
                    "t2": {"enabled": True},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        t_levels = sorted(p.t_level for p in runner.manager.probes)
        self.assertEqual(t_levels, ["T2", "T3"])

    def test_authority_manager_namespace_is_primary(self) -> None:
        """[timing.authority_manager] is the canonical location and
        takes precedence over the legacy [timing.authority] fallback."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "interval_sec": 7.0,
                    "t4": {"peers": ["timeserver.lan"]},
                },
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertEqual(runner.interval_sec, 7.0)
        self.assertIn("T4", [p.t_level for p in runner.manager.probes])

    def test_timing_authority_as_scalar_does_not_raise(self) -> None:
        """Regression: if the deployed config sets `[timing] authority
        = "rtp"` (a scalar operator preference), the old code tried to
        call `.get(...)` on a string and raised AttributeError. Now we
        detect the non-dict and fall back to defaults cleanly."""
        cfg = {
            "timing": {
                "authority": "rtp",  # scalar preference, NOT a sub-table
                "rtp_expected_accuracy_ms": 0.001,
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        # Defaults applied; T3 FusionStatusProbe is the only baseline probe.
        self.assertEqual([p.t_level for p in runner.manager.probes], ["T3"])
        self.assertEqual(runner.interval_sec, 30.0)
        self.assertEqual(runner.manager.upgrade_hysteresis, 3)


if __name__ == "__main__":
    unittest.main()
