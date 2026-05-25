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

    def test_chrony_gate_called_with_active_t_level(self) -> None:
        from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate

        class _RecordingGate(ChronyRefclockGate):
            def __init__(self):
                super().__init__(refid="HFSN", dry_run=True)
                self.calls = []
            def apply(self, t_level_active):
                self.calls.append(t_level_active)
                return super().apply(t_level_active)

        gate = _RecordingGate()
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            chrony_gate=gate,
        )
        mgr.tick()
        self.assertEqual(gate.calls, ["T3"])
        # Flip to unavailable; gate should now see None.
        probe.set(_unavail("T3"))
        mgr.tick()
        self.assertEqual(gate.calls, ["T3", None])

    def test_governor_radiod_surfaced_in_authority_json(self) -> None:
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            governor_radiod_provider=lambda: "bee1-hf-status.local",
        )
        mgr.tick()
        payload = self._read()
        self.assertEqual(payload["governor_radiod"], "bee1-hf-status.local")

    def test_governor_radiod_omitted_when_no_provider(self) -> None:
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = self._mgr([probe], upgrade_hysteresis=1)
        mgr.tick()
        self.assertNotIn("governor_radiod", self._read())

    def test_governor_radiod_provider_exception_is_soft_fail(self) -> None:
        def boom():
            raise RuntimeError("kaboom")
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            governor_radiod_provider=boom,
        )
        mgr.tick()  # must not raise
        self.assertNotIn("governor_radiod", self._read())

    def test_mdns_advertiser_called_with_state_and_governor(self) -> None:
        from hf_timestd.core.mdns_fusion_advertiser import (
            AdvertiseResult, MdnsFusionAdvertiser,
        )

        class _RecordingAdv(MdnsFusionAdvertiser):
            def __init__(self):
                super().__init__(dry_run=True)
                self.apply_calls = []
            def apply(self, state, governor_radiod=None):
                self.apply_calls.append((state.t_level_active, governor_radiod))
                return AdvertiseResult(target_state="advertising", applied=False, reason="test")

        adv = _RecordingAdv()
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        mgr = AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            governor_radiod_provider=lambda: "gov-radiod.local",
            mdns_advertiser=adv,
        )
        mgr.tick()
        self.assertEqual(adv.apply_calls, [("T3", "gov-radiod.local")])


class _FakeBootstrap:
    """Minimal BootstrapCoordinator stand-in for the manager tests."""
    def __init__(self, state):
        self._state = state
        self.calls = 0

    def check_and_step(self, now_fn):
        self.calls += 1
        return self._state


class TestAuthorityManagerBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"
        self.clock = _Clock(datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self, probes, coord, upgrade_hysteresis=1) -> AuthorityManager:
        return AuthorityManager(
            probes=probes,
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=upgrade_hysteresis,
            now_fn=self.clock,
            bootstrap_coordinator=coord,
        )

    def _read(self) -> dict:
        with self.out.open() as f:
            return json.load(f)

    def test_bootstrap_pending_suppresses_probing_and_publishes_bootstrap_block(self) -> None:
        from hf_timestd.core.bootstrap_coordinator import BootstrapState
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        coord = _FakeBootstrap(
            BootstrapState(complete=False, reason="no_coarse_time"),
        )
        mgr = self._mgr([probe], coord)
        s = mgr.tick()
        # Probes suppressed — active must be None even though T3 was available.
        self.assertIsNone(s.t_level_active)
        self.assertEqual(s.t_level_available, [])
        payload = self._read()
        self.assertIn("bootstrap", payload)
        self.assertFalse(payload["bootstrap"]["complete"])
        self.assertEqual(payload["bootstrap"]["reason"], "no_coarse_time")

    def test_bootstrap_complete_lets_probes_run(self) -> None:
        from hf_timestd.core.bootstrap_coordinator import BootstrapState
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        coord = _FakeBootstrap(
            BootstrapState(complete=True, reason="within_threshold", delta_sec=1.0),
        )
        mgr = self._mgr([probe], coord)
        s = mgr.tick()
        self.assertEqual(s.t_level_active, "T3")
        payload = self._read()
        self.assertIn("bootstrap", payload)
        self.assertTrue(payload["bootstrap"]["complete"])

    def test_bootstrap_pending_also_disables_chrony_gate(self) -> None:
        # When bootstrap hasn't completed, the refclock should be
        # DISABLED (no active level to justify offering it). Verifies
        # the gate is called from the bootstrap-pending branch.
        from hf_timestd.core.bootstrap_coordinator import BootstrapState
        from hf_timestd.core.chrony_refclock_gate import ChronyRefclockGate

        class _RecordingGate(ChronyRefclockGate):
            def __init__(self):
                super().__init__(refid="HFSN", dry_run=True)
                self.calls = []
            def apply(self, t_level_active):
                self.calls.append(t_level_active)
                return super().apply(t_level_active)

        gate = _RecordingGate()
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        coord = _FakeBootstrap(BootstrapState(complete=False, reason="no_coarse_time"))
        mgr = AuthorityManager(
            probes=[probe],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            bootstrap_coordinator=coord,
            chrony_gate=gate,
        )
        mgr.tick()
        # Gate was called with None (no active level during bootstrap-pending)
        self.assertEqual(gate.calls, [None])

    def test_bootstrap_stepped_records_reason_and_delta(self) -> None:
        from hf_timestd.core.bootstrap_coordinator import BootstrapState
        from hf_timestd.core.coarse_time_source import CoarseTimeObservation
        obs = CoarseTimeObservation(
            utc=datetime(2026, 4, 23, 11, 58, 13, tzinfo=timezone.utc),
            source="BCD", station="WWV", max_error_sec=1.0,
        )
        probe = FakeProbe("T3", _measure("T3", 0.5, 0.3))
        coord = _FakeBootstrap(
            BootstrapState(
                complete=True, reason="stepped",
                delta_sec=107.0, stepped=True, coarse=obs,
            ),
        )
        mgr = self._mgr([probe], coord)
        mgr.tick()
        payload = self._read()
        self.assertTrue(payload["bootstrap"]["stepped"])
        self.assertAlmostEqual(payload["bootstrap"]["delta_sec"], 107.0)
        self.assertEqual(payload["bootstrap"]["coarse_source"], "BCD")
        self.assertEqual(payload["bootstrap"]["coarse_station"], "WWV")


class TestSnapshotStore(unittest.TestCase):
    """V1 Layer 4 — every tick that writes authority.json also mirrors
    a per-cycle snapshot row into a local SQLite store."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"
        self.clock = _Clock(datetime(2026, 4, 23, 12, 0, 0, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr_with_store(self, probes, store):
        return AuthorityManager(
            probes=probes,
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,                # promote on first tick
            now_fn=self.clock,
            snapshot_store=store,
        )

    def test_one_row_per_tick(self) -> None:
        from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
        import sqlite3
        db = self.tmp / "auth.db"
        store = AuthoritySnapshotStore(db)
        try:
            p = FakeProbe("T3", _measure("T3", 0.5, 0.3))
            mgr = self._mgr_with_store([p], store)
            mgr.tick()
            self.clock.advance(30)
            mgr.tick()
        finally:
            store.close()
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute(
                "SELECT t_level_active, rtp_to_utc_offset_ns, t3_offset_ms "
                "FROM authority_snapshot ORDER BY utc_published"
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "T3")
        self.assertAlmostEqual(rows[0][2], 0.5)

    def test_t6_drift_monitor_flattened(self) -> None:
        """Drift-monitor + recapture fields from BpskPpsProbe detail
        round-trip into their dedicated columns."""
        from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
        import sqlite3
        db = self.tmp / "auth.db"
        store = AuthoritySnapshotStore(db)
        try:
            t6 = FakeProbe("T6")
            t6.set(ProbeResult(
                "T6", available=True,
                offset_ms=0.0024, sigma_ms=0.050,
                detail={
                    "local_minus_source_ns": 2384,
                    "pps_ok": 12345,
                    "pps_consecutive": 50,
                    "chain_delay_ns": 174147000,
                    "drift_monitor": {
                        "sustained_breach": False,
                        "anchor_discontinuity": False,
                        "anchor_residual_samples": 12,
                        "recapture_count": 2,
                        "last_recapture_reason": "anchor_discontinuity",
                        "last_recapture_age_sec": 145.3,
                    },
                },
            ))
            mgr = self._mgr_with_store([t6], store)
            mgr.tick()
        finally:
            store.close()
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM authority_snapshot"
            ).fetchone()
        self.assertEqual(row["t_level_active"], "T6")
        self.assertEqual(row["t6_local_minus_source_ns"], 2384)
        self.assertEqual(row["t6_recapture_count"], 2)
        self.assertEqual(row["t6_last_recapture_reason"], "anchor_discontinuity")
        self.assertEqual(row["t6_anchor_discontinuity"], 0)
        self.assertEqual(row["t6_anchor_residual_samples"], 12)

    def test_t5_substrate_fields_flattened(self) -> None:
        """LbeT5DirectProbe substrate fields (valid_fix, pps_utc_sec,
        nmea_age_sec, anchor_age_sec) round-trip into their dedicated
        columns.  The offset/sigma fields come from the generic
        ProbeResult shape and use the same code path as T4/T3."""
        from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
        import sqlite3
        db = self.tmp / "auth.db"
        store = AuthoritySnapshotStore(db)
        try:
            t5 = FakeProbe("T5")
            t5.set(ProbeResult(
                "T5", available=True,
                offset_ms=0.0, sigma_ms=5.0,
                detail={
                    "valid_fix": True,
                    "pps_utc_sec": 1716501000,
                    "nmea_age_sec": 0.42,
                    "anchor_age_sec": 12.345,
                    "device": "/dev/lb1421-nmea",
                },
            ))
            mgr = self._mgr_with_store([t5], store)
            mgr.tick()
        finally:
            store.close()
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM authority_snapshot"
            ).fetchone()
        self.assertEqual(row["t5_available"], 1)
        self.assertEqual(row["t5_offset_ms"], 0.0)
        self.assertEqual(row["t5_sigma_ms"], 5.0)
        self.assertEqual(row["t5_valid_fix"], 1)
        self.assertEqual(row["t5_pps_utc_sec"], 1716501000)
        self.assertAlmostEqual(row["t5_nmea_age_sec"], 0.42)
        self.assertAlmostEqual(row["t5_anchor_age_sec"], 12.345)

    def test_t5_anchor_age_nulls_when_substrate_omits_it(self) -> None:
        """Pre-Phase-2B core_recorder versions (or any probe path that
        doesn't populate ``detail['anchor_age_sec']``) must land as
        NULL in the column — not crash, not 0."""
        from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
        import sqlite3
        db = self.tmp / "auth.db"
        store = AuthoritySnapshotStore(db)
        try:
            t5 = FakeProbe("T5")
            t5.set(ProbeResult(
                "T5", available=True,
                offset_ms=0.0, sigma_ms=5.0,
                detail={
                    "valid_fix": True,
                    "pps_utc_sec": 1716501000,
                    "nmea_age_sec": 0.42,
                    # No anchor_age_sec — Phase 2A / pre-2B path.
                    "device": "/dev/lb1421-nmea",
                },
            ))
            mgr = self._mgr_with_store([t5], store)
            mgr.tick()
        finally:
            store.close()
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT t5_anchor_age_sec FROM authority_snapshot"
            ).fetchone()
        self.assertIsNone(row["t5_anchor_age_sec"])

    def test_t5_unavailable_flattens_with_zero_available(self) -> None:
        """Unavailable T5 should populate t5_available=0 (so historical
        queries can distinguish 'never configured' (NULL) from
        'configured but currently unavailable')."""
        from hf_timestd.io.authority_snapshot_store import AuthoritySnapshotStore
        import sqlite3
        db = self.tmp / "auth.db"
        store = AuthoritySnapshotStore(db)
        try:
            t5 = FakeProbe("T5")
            t5.set(ProbeResult(
                "T5", available=False,
                reason="no valid fix",
            ))
            mgr = self._mgr_with_store([t5], store)
            mgr.tick()
        finally:
            store.close()
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM authority_snapshot"
            ).fetchone()
        self.assertEqual(row["t5_available"], 0)
        # Substrate detail fields stay NULL — nothing to flatten when
        # the detail dict is empty.
        self.assertIsNone(row["t5_valid_fix"])
        self.assertIsNone(row["t5_pps_utc_sec"])

    def test_no_store_is_legacy_noop(self) -> None:
        """When no snapshot_store is provided, tick still works and
        authority.json is written as before — no DB activity."""
        # No store at all; just construct the manager with default args.
        mgr = AuthorityManager(
            probes=[FakeProbe("T3", _measure("T3", 0.5, 0.3))],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
        )
        mgr.tick()
        # authority.json was written; that's the legacy contract.
        self.assertTrue(self.out.exists())

    def test_store_exception_does_not_block_authority_json(self) -> None:
        """A failing store must not stop authority.json from being
        published — the cycle's primary deliverable is the JSON."""
        class BrokenStore:
            def insert(self, snapshot):
                raise RuntimeError("disk full")
        mgr = AuthorityManager(
            probes=[FakeProbe("T3", _measure("T3", 0.5, 0.3))],
            output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=1,
            now_fn=self.clock,
            snapshot_store=BrokenStore(),
        )
        mgr.tick()
        self.assertTrue(self.out.exists())


def _t6_breached(offset_ms=0.0, sigma_ms=0.05):
    """T6 ProbeResult with drift_monitor.sustained_breach=True."""
    return ProbeResult(
        "T6", True, offset_ms=offset_ms, sigma_ms=sigma_ms,
        detail={"drift_monitor": {"sustained_breach": True}},
    )


def _t6_calm(offset_ms=0.0, sigma_ms=0.05):
    """T6 ProbeResult with drift_monitor.sustained_breach=False."""
    return ProbeResult(
        "T6", True, offset_ms=offset_ms, sigma_ms=sigma_ms,
        detail={"drift_monitor": {"sustained_breach": False}},
    )


def _t5_anchor_grounded(offset_ms, sigma_ms):
    """T5 ProbeResult with the Phase 2B rtp_anchor_grounded marker."""
    return ProbeResult(
        "T5", True, offset_ms=offset_ms, sigma_ms=sigma_ms,
        detail={"rtp_anchor_grounded": True,
                "anchor_offset_ns": int(round(offset_ms * 1_000_000))},
    )


class TestAuthorityManagerPhase2BDemoteOnBreach(unittest.TestCase):
    """Phase 2B — demote T6 → T5 when drift_monitor.sustained_breach
    has been sticky for ``demote_t6_on_breach_min_cycles`` consecutive
    ticks AND T5 is available past hysteresis.  Default off; flag-on
    is the operator opt-in for the Phase 2C cutover.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"
        self.clock = _Clock(datetime(2026, 5, 25, 9, 0, 0, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self, *, demote, min_cycles=3, hyst=1, probes):
        return AuthorityManager(
            probes=probes, output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=hyst,
            now_fn=self.clock,
            demote_t6_on_breach=demote,
            demote_t6_on_breach_min_cycles=min_cycles,
        )

    def test_default_off_no_demotion_even_after_many_breach_ticks(self):
        """The Phase 2A invariant: with demote_t6_on_breach=False (the
        default), T6 stays active through any number of breach ticks.
        Byte-compat is the entire point of the default-off ship plan."""
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        mgr = self._mgr(demote=False, probes=[t6, t5])
        for _ in range(20):
            state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6")
        self.assertNotIn(
            "demote-t6-breach",
            "".join(state.disagreement_flags),
        )
        # The counter is still maintained for telemetry — but does
        # nothing to selection.
        self.assertGreaterEqual(mgr._t6_consecutive_breach_ticks, 1)

    def test_breach_below_min_cycles_does_not_demote(self):
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        mgr = self._mgr(demote=True, min_cycles=3, probes=[t6, t5])
        for _ in range(2):
            state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6")
        self.assertEqual(mgr._t6_consecutive_breach_ticks, 2)

    def test_breach_at_min_cycles_demotes_to_t5_with_flag(self):
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        mgr = self._mgr(demote=True, min_cycles=3, probes=[t6, t5])
        for _ in range(3):
            state = mgr.tick()
        self.assertEqual(state.t_level_active, "T5")
        self.assertTrue(any(
            f.startswith("demote-t6-breach->t5:")
            for f in state.disagreement_flags
        ))

    def test_breach_clears_resets_counter_and_recovers_to_t6(self):
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        mgr = self._mgr(demote=True, min_cycles=3, hyst=1, probes=[t6, t5])
        # Build up to demotion.
        for _ in range(3):
            mgr.tick()
        self.assertEqual(mgr._t_active, "T5")
        # Breach clears.  Counter resets; T6 (which ranks higher than
        # T5) wins again on the next tick.
        t6.set(_t6_calm())
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6")
        self.assertEqual(mgr._t6_consecutive_breach_ticks, 0)

    def test_demotion_blocked_when_t5_unavailable(self):
        """If T5 isn't actually available (e.g., LBE-1421 disconnected),
        the demotion must not happen — we'd be promoting nothing.
        T6 stays active even past the breach threshold; the operator
        sees the breach flag persisting and the lack of T5 fallback
        is visible in t_level_available."""
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _unavail("T5"))
        mgr = self._mgr(demote=True, min_cycles=3, probes=[t6, t5])
        for _ in range(6):
            state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6")
        self.assertNotIn(
            "demote-t6-breach",
            "".join(state.disagreement_flags),
        )

    def test_demotion_respects_t5_upgrade_hysteresis(self):
        """T5 must have been continuously available for
        upgrade_hysteresis ticks before it can be the demotion
        target — otherwise we'd flap into a not-yet-trusted T5.

        Both T6 and T5 share the same upgrade_hysteresis value, so we
        choose a moderate value (2) and walk through the state
        transitions: T6 promotes to active first while T5 is still
        unavailable, then T5 comes up but stays in warm-up for the
        first tick before becoming eligible for demotion.
        """
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _unavail("T5"))
        mgr = self._mgr(demote=True, min_cycles=1, hyst=2, probes=[t6, t5])
        # Tick 1: T6 avail=1 (< 2), active=None, no T5.
        mgr.tick()
        # Tick 2: T6 avail=2 → active=T6.  T5 still unavailable.
        # Breach counter ticks to 1 but no demote (T5 not eligible).
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6")
        # Tick 3: T5 comes up but its avail=1 (< 2) → still in warm-up.
        # T6 stays active even though breach is sticky.
        t5.set(_t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T6",
                         "should hold T6 while T5 still in warm-up")
        # Tick 4: T5 avail=2 ≥ hyst → eligible.  Breach still sticky.
        # Demote fires.
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T5")

    def test_counter_resets_when_t6_picks_returns_to_calm(self):
        """A flapping breach (breach ticks interleaved with calm ticks)
        should NOT accumulate toward demotion — only consecutive
        breaches count, mirroring the Layer 2 sustained-breach
        philosophy already enforced inside core_recorder."""
        t6 = FakeProbe("T6", _t6_breached())
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=150.0, sigma_ms=150.0))
        mgr = self._mgr(demote=True, min_cycles=3, probes=[t6, t5])
        mgr.tick()
        mgr.tick()
        t6.set(_t6_calm())
        mgr.tick()  # resets counter
        t6.set(_t6_breached())
        state = mgr.tick()  # counter = 1 again
        self.assertEqual(state.t_level_active, "T6")
        self.assertEqual(mgr._t6_consecutive_breach_ticks, 1)


class TestAuthorityManagerPhase2BTrustTierAnchorGrounded(unittest.TestCase):
    """Phase 2B — when a trust-tier probe carries
    detail.rtp_anchor_grounded=True, _build_state must publish that
    probe's offset_ms / sigma_ms as rtp_to_utc_offset_ns and sigma_ns.
    Without the marker (ChronyTrackingProbe-as-T5 sites, T4/T2/T1
    trust witnesses), legacy behaviour stands: offset=0 with
    TRUST_SIGMA_MS for the tier."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "authority.json"
        self.clock = _Clock(datetime(2026, 5, 25, 9, 0, 0, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mgr(self, probes, hyst=1):
        return AuthorityManager(
            probes=probes, output_path=self.out,
            a_level_provider=lambda: "A1",
            upgrade_hysteresis=hyst,
            now_fn=self.clock,
        )

    def _read(self):
        with self.out.open() as f:
            return json.load(f)

    def test_anchor_grounded_t5_publishes_probe_offset(self):
        t5 = FakeProbe("T5", _t5_anchor_grounded(offset_ms=42.0, sigma_ms=42.0))
        mgr = self._mgr([t5])
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T5")
        self.assertEqual(state.rtp_to_utc_offset_ns, 42_000_000)
        self.assertEqual(state.sigma_ns, 42_000_000)
        payload = self._read()
        self.assertEqual(payload["rtp_to_utc_offset_ns"], 42_000_000)
        self.assertEqual(payload["sigma_ns"], 42_000_000)

    def test_t5_without_marker_stays_trust_tier(self):
        """ChronyTrackingProbe-as-T5 path: forwards a chrony-residual
        offset_ms (not RTP-anchor-grounded) and no detail marker.
        Manager must publish offset=0 + tier σ — the chrony residual
        is for cross-check only, never as rtp_to_utc_offset_ns."""
        chrony_t5 = ProbeResult(
            "T5", True, offset_ms=12.0, sigma_ms=TRUST_SIGMA_MS["T5"],
            detail={"name": "GPS", "state": "*"},
        )
        t5 = FakeProbe("T5", chrony_t5)
        mgr = self._mgr([t5])
        state = mgr.tick()
        self.assertEqual(state.t_level_active, "T5")
        self.assertEqual(state.rtp_to_utc_offset_ns, 0)
        self.assertEqual(
            state.sigma_ns,
            int(round(TRUST_SIGMA_MS["T5"] * 1_000_000)),
        )

    def test_t4_t2_t1_stay_trust_tier_byte_compat(self):
        """T4/T2/T1 never gain the rtp_anchor_grounded marker — they
        remain pure trust witnesses.  Confirm published shape is
        offset=0 with TRUST_SIGMA_MS even when probe forwards an
        offset (e.g., from chrony tracking)."""
        for tier in ("T4", "T2", "T1"):
            with self.subTest(tier=tier):
                probe_result = ProbeResult(
                    tier, True, offset_ms=5.0,
                    sigma_ms=TRUST_SIGMA_MS[tier],
                    detail={"name": "trust-witness"},
                )
                mgr = self._mgr([FakeProbe(tier, probe_result)])
                state = mgr.tick()
                self.assertEqual(state.t_level_active, tier)
                self.assertEqual(state.rtp_to_utc_offset_ns, 0)
                self.assertEqual(
                    state.sigma_ns,
                    int(round(TRUST_SIGMA_MS[tier] * 1_000_000)),
                )


if __name__ == "__main__":
    unittest.main()
