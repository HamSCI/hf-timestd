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
        # Registration is by detection now: T6 and T5 self-register and
        # report unavailable when their hardware is absent, so an empty
        # config no longer hides a tier. T3 remains the floor.
        self.assertIn("T3", t_levels)
        self.assertIn("T6", t_levels)
        self.assertIn("T5", t_levels)
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
        # T6 now self-registers alongside the configured tiers.
        self.assertEqual(sorted(t_levels), ["T2", "T3", "T4", "T5", "T6"])
        self.assertEqual(runner.interval_sec, 10.0)
        self.assertEqual(runner.manager.upgrade_hysteresis, 2)

    def test_frontend_probe_wired_when_radiod_and_t6_are_configured(self) -> None:
        """The receiver's analog operating point is a T6 uncertainty term
        (0.52 dB of C/N0 per dB of front-end gain, B4 2026-08-28), so the
        snapshot must be able to record it."""
        cfg = {
            "ka9q": {"status": "AC0G-B4-status.local"},
            "timing": {"t6_pps": {"enabled": True, "frequency_hz": 45375000}},
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertIsNotNone(runner.manager.frontend_probe)

    def test_frontend_probe_wired_when_caller_supplies_governor(self) -> None:
        """resolve_ka9q_status is imported inside the governor branch; a
        caller that supplies its own governor must not skip that import
        out from under the front-end probe."""
        cfg = {
            "ka9q": {"status": "AC0G-B4-status.local"},
            "timing": {"t6_pps": {"enabled": True, "frequency_hz": 45375000}},
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
            governor_radiod_provider=lambda: "someone-else",
        )
        self.assertIsNotNone(runner.manager.frontend_probe)

    def test_no_frontend_probe_without_a_radiod_address(self) -> None:
        cfg = {"timing": {"t6_pps": {"enabled": True, "frequency_hz": 45375000}}}
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertIsNone(runner.manager.frontend_probe)

    def test_no_frontend_probe_without_a_t6_frequency(self) -> None:
        """Nothing to point the poll at — and guessing an SSRC is
        forbidden, so there is no fallback."""
        cfg = {"ka9q": {"status": "AC0G-B4-status.local"}, "timing": {}}
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertIsNone(runner.manager.frontend_probe)

    def test_t5_lb1421_precedence_over_chrony_refid(self) -> None:
        """When both lb1421_status_path and chrony refid are configured,
        the substrate-grounded LbeT5DirectProbe must win — chrony T5 is
        the legacy fallback for hosts without LBE-1421."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "t5": {
                        "lb1421_status_path": "/var/lib/timestd/status/core-recorder-status.json",
                        "refid": "GPS",
                        "sigma_floor_ms": 3.0,
                    },
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        t5_probes = [p for p in runner.manager.probes if p.t_level == "T5"]
        self.assertEqual(len(t5_probes), 1)
        self.assertIsInstance(t5_probes[0], LbeT5DirectProbe)
        self.assertEqual(t5_probes[0].sigma_floor_ms, 3.0)

    def test_t5_lb1421_enabled_flag_alone_registers_direct_probe(self) -> None:
        """Operators can opt into LBE-direct T5 with just the
        enabled flag (defaults take care of status_path)."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "t5": {"lb1421_enabled": True},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        from hf_timestd.core.lbe_t5_direct_probe import LbeT5DirectProbe
        t5_probes = [p for p in runner.manager.probes if p.t_level == "T5"]
        self.assertEqual(len(t5_probes), 1)
        self.assertIsInstance(t5_probes[0], LbeT5DirectProbe)
        # Defaults pick up the canonical status file path.
        self.assertEqual(
            str(t5_probes[0].status_path),
            "/var/lib/timestd/status/core-recorder-status.json",
        )

    def test_t5_chrony_refid_still_works_without_lb1421(self) -> None:
        """Hosts without LBE-1421 keep the chrony-shaped T5 — the
        existing config shape (refid only) must still build a
        ChronyTrackingProbe."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "t5": {"refid": "GPS"},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        from hf_timestd.core.chrony_tracking_probe import ChronyTrackingProbe
        t5_probes = [p for p in runner.manager.probes if p.t_level == "T5"]
        self.assertEqual(len(t5_probes), 1)
        self.assertIsInstance(t5_probes[0], ChronyTrackingProbe)

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
        # T6/T5 self-register; the point of this test is that T2 matches
        # any server when no T4 peers are configured.
        self.assertIn("T2", t_levels)
        self.assertIn("T3", t_levels)
        self.assertNotIn("T4", t_levels)

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
        # Defaults applied — and, crucially, the tiers survive.
        #
        # This assertion used to read `== ["T3"]`, which locked in the very
        # bug the docstring describes. Not raising was necessary but not
        # sufficient: B4 ships exactly this config with a working TS-1, and
        # for months reported T3 because the scalar silently zeroed every
        # tier's settings and T6 was never registered. A malformed operator
        # preference must not be able to take a hardware tier off the board.
        t_levels = [p.t_level for p in runner.manager.probes]
        self.assertIn("T3", t_levels)
        self.assertIn("T6", t_levels, "scalar authority key suppressed T6")
        self.assertIn("T5", t_levels)
        self.assertEqual(runner.interval_sec, 30.0)
        self.assertEqual(runner.manager.upgrade_hysteresis, 3)

    def test_gpsdo_probe_wires_as_a_level_provider_when_enabled(self) -> None:
        """[timing.authority_manager.gpsdo].enabled swaps the static
        a_level string for GpsdoProbe.poll(). With an empty run_dir the
        probe returns "A0" — proves the wiring goes through the probe
        rather than echoing the `a_level` fallback."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "a_level": "A1",   # would be used if probe were disabled
                    "gpsdo": {
                        "enabled": True,
                        "run_dir": str(self.tmp / "gpsdo-empty"),
                    },
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertEqual(runner.manager.a_level_provider(), "A0")

    def test_gpsdo_probe_disabled_falls_back_to_static_a_level(self) -> None:
        cfg = {
            "timing": {
                "authority_manager": {
                    "a_level": "A1",
                    "gpsdo": {"enabled": False},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertEqual(runner.manager.a_level_provider(), "A1")

    def test_explicit_a_level_provider_overrides_gpsdo_config(self) -> None:
        """Callers that pass an a_level_provider directly (embedded use
        case, e.g. a test harness) retain full control — the gpsdo
        block is ignored when a provider is supplied."""
        cfg = {
            "timing": {
                "authority_manager": {
                    "gpsdo": {"enabled": True, "run_dir": "/run/gpsdo"},
                }
            }
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
            a_level_provider=lambda: "A1",
        )
        self.assertEqual(runner.manager.a_level_provider(), "A1")


class TestBuildAuthorityRunnerPhase2BConfig(unittest.TestCase):
    """Phase 2C — demote-on-breach knobs flow from [t6] into the
    AuthorityManager constructor.  ON by default (Phase 2C cutover);
    operators opt out with an explicit ``demote_on_breach = false``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_to_on(self) -> None:
        runner = build_authority_runner_from_config(
            config={},
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertTrue(runner.manager.demote_t6_on_breach)
        self.assertEqual(runner.manager.demote_t6_on_breach_min_cycles, 3)

    def test_explicit_false_opts_out(self) -> None:
        cfg = {
            "timing": {
                "authority_manager": {
                    "t6": {"demote_on_breach": False},
                },
            },
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertFalse(runner.manager.demote_t6_on_breach)

    def test_t6_demote_on_breach_flag_wires_through(self) -> None:
        cfg = {
            "timing": {
                "authority_manager": {
                    "t6": {
                        "demote_on_breach": True,
                        "demote_on_breach_min_cycles": 5,
                    },
                },
            },
        }
        runner = build_authority_runner_from_config(
            config=cfg,
            fusion_status_path=self.tmp / "fusion_status.json",
            authority_output_path=self.tmp / "authority.json",
        )
        self.assertTrue(runner.manager.demote_t6_on_breach)
        self.assertEqual(runner.manager.demote_t6_on_breach_min_cycles, 5)


if __name__ == "__main__":
    unittest.main()
