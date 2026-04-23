#!/usr/bin/env python3
"""Unit tests for AuthorityManager — state machine, cross-check, atomic writer.

Conventions used in these tests:
  - T-levels rank high→low as T6, T5, T4, T3, T2, T1, T0 (§4.5).
  - Measuring probes (T3, T6) report their actual RTP→UTC offset.
  - Trust-based probes (T5, T4, T2, T1) report offset=0 with tier-sigma,
    representing the trust claim "RTP is already UTC." This is what
    makes cross-check meaningful between a measuring level and a
    trust-based one.
"""

import json
import shutil
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hf_timestd.core.authority_manager import (
    SCHEMA_VERSION,
    TRUST_SIGMA_MS,
    AuthorityManager,
    ProbeResult,
)


@dataclass
class FakeProbe:
    """Runtime-settable probe for tests."""
    t_level: str
    _result: ProbeResult = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._result is None:
            self._result = ProbeResult(self.t_level, available=False, reason="default")

    def set(self, result: ProbeResult) -> None:
        self._result = result

    def poll(self) -> ProbeResult:
        return self._result


def _measure(t: str, offset_ms: float, sigma_ms: float, stations=None) -> ProbeResult:
    detail = {}
    if stations is not None:
        detail["stations_used"] = stations
    return ProbeResult(t, True, offset_ms=offset_ms, sigma_ms=sigma_ms, detail=detail)


def _trust(t: str, offset_ms: float = 0.0) -> ProbeResult:
    return ProbeResult(t, True, offset_ms=offset_ms, sigma_ms=TRUST_SIGMA_MS[t])


def _unavail(t: str, reason: str = "down") -> ProbeResult:
    return ProbeResult(t, False, reason=reason)


class _Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = datetime.fromtimestamp(self.t.timestamp() + seconds, tz=timezone.utc)


class TestAuthorityManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"
        self.clock = _Clock(datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self, probes, upgrade_hysteresis=3, a_level="A1") -> AuthorityManager:
        return AuthorityManager(
            probes=probes,
            output_path=self.out,
            a_level_provider=lambda: a_level,
            upgrade_hysteresis=upgrade_hysteresis,
            now_fn=self.clock,
        )

    def _read(self) -> dict:
        with self.out.open() as f:
            return json.load(f)

    # ----- hysteresis & selection -----

    def test_cold_start_no_active_until_hysteresis_met(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.8, 0.5))
        mgr = self._mgr([p], upgrade_hysteresis=3)
        self.assertIsNone(mgr.tick().t_level_active)
        self.assertIsNone(mgr.tick().t_level_active)
        self.assertEqual(mgr.tick().t_level_active, "T3")

    def test_downgrade_is_immediate(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.8, 0.5))
        mgr = self._mgr([p], upgrade_hysteresis=3)
        for _ in range(4):
            mgr.tick()
        self.assertEqual(mgr._t_active, "T3")
        p.set(_unavail("T3"))
        self.assertIsNone(mgr.tick().t_level_active)

    def test_highest_available_level_wins(self) -> None:
        # T4 ranks higher than T3. Both available → T4 active, T3 witness.
        t4 = FakeProbe("T4", _trust("T4"))
        t3 = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = self._mgr([t4, t3], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T4")
        self.assertIn("T3", s.t_level_witnesses)

    # ----- offset publication semantics -----

    def test_t3_active_publishes_measured_offset(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.812, 0.94, stations=["WWV", "CHU"]))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T3")
        self.assertEqual(s.rtp_to_utc_offset_ns, 812_000)
        self.assertEqual(s.sigma_ns, 940_000)
        self.assertEqual(s.stations_contributing, ["WWV", "CHU"])

    def test_t4_active_publishes_zero_offset_with_tier_sigma(self) -> None:
        p = FakeProbe("T4", _trust("T4"))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T4")
        self.assertEqual(s.rtp_to_utc_offset_ns, 0)
        self.assertEqual(s.sigma_ns, int(round(TRUST_SIGMA_MS["T4"] * 1_000_000)))

    def test_no_active_leaves_offset_null(self) -> None:
        p = FakeProbe("T3", _unavail("T3"))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertIsNone(s.t_level_active)
        self.assertIsNone(s.rtp_to_utc_offset_ns)
        self.assertIsNone(s.sigma_ns)

    # ----- cross-check & witnesses -----

    def test_witness_agreement_within_combined_ci_is_silent(self) -> None:
        # T4 active (trust claim 0), T3 witness measures 0.3 ms.
        # Combined CI: 3*sqrt(T4_trust² + 0.5²) = 3*sqrt(4 + 0.25) ≈ 6.2 ms
        # (T4 trust sigma is 2 ms). 0.3 < 6.2 and < T3↔T4 floor 2.0 ms → silent.
        t4 = FakeProbe("T4", _trust("T4"))
        t3 = FakeProbe("T3", _measure("T3", 0.3, 0.5))
        mgr = self._mgr([t4, t3], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T4")
        self.assertEqual(s.disagreement_flags, [])
        self.assertIn("T3", s.t_level_witnesses)

    def test_single_witness_disagreement_raises_flag_without_downgrade(self) -> None:
        # T4 active (trust 0), T3 witness measures 20 ms — well past combined CI.
        t4 = FakeProbe("T4", _trust("T4"))
        t3 = FakeProbe("T3", _measure("T3", 20.0, 0.5))
        mgr = self._mgr([t4, t3], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T4")  # single witness → no downgrade
        self.assertTrue(
            any("T4<->T3" in f for f in s.disagreement_flags),
            f"expected T4<->T3 disagreement, got {s.disagreement_flags}",
        )

    def test_majority_witnesses_downgrade_active(self) -> None:
        # T3 measures 100 ms (way off); T2 and T1 both claim 0 (trust).
        # T3 is active (highest available — T4/T5/T6 not registered).
        # Witnesses T2 and T1 agree with each other, disagree with T3 →
        # majority downgrade to T2 (highest-ranked disagreeing witness).
        t3 = FakeProbe("T3", _measure("T3", 100.0, 1.0))
        t2 = FakeProbe("T2", _trust("T2"))
        t1 = FakeProbe("T1", _trust("T1"))
        mgr = self._mgr([t3, t2, t1], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T2")
        self.assertTrue(
            any("majority-downgrade:T3->T2" in f for f in s.disagreement_flags),
            f"expected majority downgrade, got {s.disagreement_flags}",
        )

    def test_asymmetric_t3_t2_rule_forces_t3_down_on_huge_delta(self) -> None:
        # T3 measures 10 ms, T2 trust claim 0, but we simulate T2 saying
        # 2010 ms (impossible under real trust convention, but the rule
        # is defined on T2's reported offset so we test it directly).
        t3 = FakeProbe("T3", _measure("T3", 10.0, 1.0))
        t2 = FakeProbe("T2", ProbeResult("T2", True, offset_ms=2010.0, sigma_ms=20.0))
        mgr = self._mgr([t3, t2], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T2")
        self.assertTrue(
            any("asymmetric-T3-T2" in f for f in s.disagreement_flags),
            f"expected asymmetric rule, got {s.disagreement_flags}",
        )

    # ----- output contract -----

    def test_published_schema_is_v1_and_fields_present(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        mgr.tick()
        payload = self._read()
        self.assertEqual(payload["schema"], SCHEMA_VERSION)
        for key in (
            "utc_published", "a_level", "t_level_active", "t_level_available",
            "t_level_witnesses", "rtp_to_utc_offset_ns", "sigma_ns",
            "stations_contributing", "last_transition_utc", "disagreement_flags",
        ):
            self.assertIn(key, payload)

    def test_atomic_write_leaves_no_leftover_tempfiles(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        mgr.tick()
        mgr.tick()
        leftovers = [pp for pp in self.tmp.iterdir() if pp.name != "authority.json"]
        self.assertEqual(leftovers, [])

    def test_transition_timestamp_only_updates_on_change(self) -> None:
        p = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = self._mgr([p], upgrade_hysteresis=1)
        mgr.tick()
        t1 = mgr._last_transition_utc
        self.clock.advance(30)
        mgr.tick()
        t2 = mgr._last_transition_utc
        self.assertEqual(t1, t2, "no transition should leave timestamp unchanged")
        # Flip to unavailable — transition timestamp advances
        p.set(_unavail("T3"))
        self.clock.advance(30)
        mgr.tick()
        self.assertNotEqual(mgr._last_transition_utc, t2)

    def test_probe_exception_treated_as_unavailable(self) -> None:
        class BoomProbe:
            t_level = "T3"
            def poll(self):
                raise RuntimeError("kaboom")
        mgr = self._mgr([BoomProbe()], upgrade_hysteresis=1)
        s = mgr.tick()
        self.assertIsNone(s.t_level_active)


if __name__ == "__main__":
    unittest.main()
