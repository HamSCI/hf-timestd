"""v3.85 item 5: T2 must show its own health before it may refuse T6.

WB6CXC-7, 2026-11-01: no T4, no T5, so the judge's lowest -- and
vacuously trusted -- tier read T2, chrony on internet NTP.  A ``qm
snapshot`` stepped the guest 185 ms behind; chrony kept a 17-minute-old
sample from its selected source, flagged every fresh server a
falseticker, and logged "no majority" at 14:44Z.  For fifteen minutes the
cross-bench gate refused a correct T6 (battery passed, second named by the
GPS-mini) on that T2's word.

The judge now reads chrony's selection health from the sources output it
already fetches (chrony_source_health.py).  A credible T6 is not refused
by a T2 that shows a fault; a healthy T2 that disagrees still refuses.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from hf_timestd.core.chrony_source_health import (
    FRESHER_BY_S, SELECTED_STALE_S,
    chrony_source_health, chrony_unhealthy_reason)
from hf_timestd.core.offset_judge import BenchReading, ChronyBench, OffsetJudge

LOGGER = "hf_timestd.core.offset_judge"

# chronyc -n -c sources: mode,state,name,stratum,poll,reach,LastRx,...
HEALTHY_POOL = (
    "^,*,162.159.200.1,3,10,377,300,0.0001,0.0002,0.02\n"
    "^,+,162.159.200.123,3,10,377,280,0.0001,0.0002,0.02\n"
    "^,+,204.17.205.27,2,10,377,900,0.0001,0.0002,0.02\n"
    "^,-,23.150.40.242,2,10,377,40,0.0001,0.0002,0.02\n")
# WB6CXC-7 after the snapshot step: the selected sample 17 min old, every
# server with a fresh one voted out.
WB6CXC_STUCK = (
    "^,*,162.159.200.1,3,10,377,1020,0.0001,0.0002,0.02\n"
    "^,x,162.159.200.123,3,6,377,30,0.1850,0.0002,0.02\n"
    "^,x,204.17.205.27,2,6,377,45,0.1851,0.0002,0.02\n"
    "^,x,23.150.40.242,2,6,377,60,0.1849,0.0002,0.02\n")
NO_MAJORITY = (
    "^,x,162.159.200.1,3,6,377,30,0.0001,0.0002,0.02\n"
    "^,x,162.159.200.123,3,6,377,30,0.1850,0.0002,0.02\n")


# ── reading chrony's selection ────────────────────────────────────────

class TestChronySourceHealth:

    def test_a_healthy_pool_shows_no_fault(self):
        assert chrony_unhealthy_reason(
            chrony_source_health(HEALTHY_POOL)) is None

    def test_wb6cxcs_stuck_selection_is_a_fault(self):
        h = chrony_source_health(WB6CXC_STUCK)
        assert h["n_falseticker"] == 3 and h["n_combined"] == 1
        assert "falsetickers" in chrony_unhealthy_reason(h)

    def test_no_selected_source_is_a_fault(self):
        assert "selects no source" in chrony_unhealthy_reason(
            chrony_source_health(NO_MAJORITY))

    def test_one_voted_out_server_is_chrony_doing_its_job(self):
        csv = HEALTHY_POOL.replace("^,-,23.150.40.242", "^,x,23.150.40.242")
        assert chrony_unhealthy_reason(chrony_source_health(csv)) is None

    def test_as_many_falsetickers_as_combined_is_not_a_fault(self):
        csv = ("^,*,a,3,10,377,30,0,0,0\n"
               "^,+,b,3,10,377,30,0,0,0\n"
               "^,x,c,3,6,377,30,0,0,0\n"
               "^,x,d,3,6,377,30,0,0,0\n")
        assert chrony_unhealthy_reason(chrony_source_health(csv)) is None
        csv += "^,x,e,3,6,377,30,0,0,0\n"
        assert "falsetickers" in chrony_unhealthy_reason(
            chrony_source_health(csv))

    def test_falsetickers_must_outnumber_the_combined(self):
        csv = (HEALTHY_POOL
               + "^,x,1.2.3.4,2,6,377,30,0.1,0.0002,0.02\n"
               + "^,x,1.2.3.5,2,6,377,30,0.1,0.0002,0.02\n")
        # 2 falsetickers against 3 combined
        assert chrony_unhealthy_reason(chrony_source_health(csv)) is None

    def test_a_stale_selection_against_a_fresher_falseticker_is_a_fault(self):
        sel = SELECTED_STALE_S + 100
        csv = (f"^,*,a,3,10,377,{sel:.0f},0,0,0\n"
               f"^,+,b,3,10,377,{sel:.0f},0,0,0\n"
               f"^,x,c,3,6,377,{sel - FRESHER_BY_S:.0f},0,0,0\n")
        assert "old" in chrony_unhealthy_reason(chrony_source_health(csv))

    def test_a_stale_selection_alone_is_a_slow_poll_not_a_fault(self):
        """maxpoll 10 polls every 1024 s; a fresher NON-falseticker is a
        healthy pool mid-cycle."""
        csv = ("^,*,a,3,10,377,1000,0,0,0\n"
               "^,+,b,3,10,377,20,0,0,0\n")
        assert chrony_unhealthy_reason(chrony_source_health(csv)) is None

    def test_a_falseticker_only_slightly_fresher_is_not_a_fault(self):
        sel = SELECTED_STALE_S + 100
        csv = (f"^,*,a,3,10,377,{sel:.0f},0,0,0\n"
               f"^,+,b,3,10,377,{sel:.0f},0,0,0\n"
               f"^,x,c,3,6,377,{sel - FRESHER_BY_S + 1:.0f},0,0,0\n")
        assert chrony_unhealthy_reason(chrony_source_health(csv)) is None

    def test_no_summary_is_not_evidence_of_a_fault(self):
        assert chrony_unhealthy_reason(None) is None
        assert chrony_source_health("") is None

    def test_the_bench_carries_health_from_its_one_sources_call(self):
        calls = []

        def runner(args, **kw):
            calls.append(tuple(args))
            if "tracking" in args:
                out = ("Reference ID    : A29FC801 (162.159.200.1)\n"
                       "Stratum         : 4\n"
                       "System time     : 0.185 seconds slow of NTP time\n"
                       "Leap status     : Normal\n")
            else:
                out = WB6CXC_STUCK
            return subprocess.CompletedProcess(args, 0, out, "")
        r = ChronyBench(runner=runner).poll()
        assert r.tier == "T2"
        assert r.detail["chrony_health"]["n_falseticker"] == 3
        assert sum(1 for c in calls if "sources" in c) == 1


# ── the judge ─────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.wall = 1_700_000_000.0
        self.mono_v = 1_000.0

    def time(self):
        return self.wall

    def mono(self):
        return self.mono_v


CREDIBLE = {"authority_state": "AUTHORITATIVE", "state_for_s": 900.0,
            "rehome_age_s": None, "battery_passed": True,
            "second_named": True}


class _Bench:
    def __init__(self, make):
        self.make = make

    def poll(self):
        return self.make()


def _judge(tmp_path, clock, t2_sources, t6_health=CREDIBLE,
           t2_offset_s=-0.185, t6_live=True):
    """A T2 stepped 185 ms behind true UTC, and a T6 telling the truth.

    ``t6_live=False`` starts with T6 silent, so the judge settles on T2
    first and T6 must later come up through the upgrade gate -- the
    WB6CXC-7 order.  Flip ``j._fixture_state["t6"]`` to bring it in.
    """
    state = {"sources": t2_sources, "t6": t6_live}

    def t2():
        return BenchReading(
            tier="T2", utc=clock.wall + t2_offset_s, sigma_ns=20e6,
            mono=clock.mono_v,
            detail={"chrony_health": chrony_source_health(state["sources"])})

    def t6():
        if not state["t6"]:
            return None
        return BenchReading(
            tier="T6", utc=clock.wall, sigma_ns=900_000.0, mono=clock.mono_v,
            detail={"anchor_tier": "T6", "t6_health": dict(t6_health)},
            plane="label")

    j = OffsetJudge(config={"enabled": True, "tick_seconds": 10.0,
                            "label_plane_measure": False},
                    benches=[_Bench(t2), _Bench(t6)],
                    publish_path=tmp_path / "oj.json",
                    time_fn=clock.time, mono_fn=clock.mono)
    j._fixture_state = state
    return j


def _tick(j, clock, n=1):
    for _ in range(n):
        clock.wall += 10.0
        clock.mono_v += 10.0
        j.tick()


class TestTheJudge:

    def _settled_on_t2(self, tmp_path, sources, **kw):
        clock = _Clock()
        j = _judge(tmp_path, clock, sources, t6_live=False, **kw)
        _tick(j, clock, 2)
        assert j._best.tier == "T2"
        j._fixture_state["t6"] = True
        return j, clock

    def test_a_healthy_t2_still_refuses_a_disagreeing_t6(self, tmp_path):
        j, clock = self._settled_on_t2(tmp_path, HEALTHY_POOL)
        _tick(j, clock, 6)
        assert j._best.tier == "T2"
        assert j._cross_conflict is not None
        assert j._t2_overrule is None

    def test_a_faulted_t2_does_not_refuse_a_credible_t6(self, tmp_path):
        j, clock = self._settled_on_t2(tmp_path, WB6CXC_STUCK)
        _tick(j, clock, 6)
        assert j._best.tier == "T6"
        assert "falsetickers" in j._t2_overrule["reason"]

    def test_no_majority_does_not_refuse_a_credible_t6(self, tmp_path):
        j, clock = self._settled_on_t2(tmp_path, NO_MAJORITY)
        _tick(j, clock, 6)
        assert j._best.tier == "T6"

    def test_a_judge_restart_inside_the_fault_trusts_t6_too(self, tmp_path):
        """Bootstrap builds trust bottom-up from the lowest tier; a
        faulted T2 must not anchor that chain either."""
        clock = _Clock()
        j = _judge(tmp_path, clock, WB6CXC_STUCK)
        _tick(j, clock)
        assert j._best.tier == "T6"

    def test_the_episode_is_published(self, tmp_path):
        import json
        j, clock = self._settled_on_t2(tmp_path, WB6CXC_STUCK)
        _tick(j, clock)
        data = json.loads((tmp_path / "oj.json").read_text())
        assert data["t2_overruled"]["reason"]

    @pytest.mark.parametrize("gap", ["battery_passed", "second_named"])
    def test_a_t6_without_credentials_stays_refused(self, tmp_path, gap):
        j, clock = self._settled_on_t2(
            tmp_path, WB6CXC_STUCK, t6_health=dict(CREDIBLE, **{gap: False}))
        _tick(j, clock, 6)
        assert j._best.tier == "T2"

    def test_a_t6_not_yet_authoritative_stays_refused(self, tmp_path):
        j, clock = self._settled_on_t2(
            tmp_path, WB6CXC_STUCK,
            t6_health=dict(CREDIBLE, authority_state="ACQUIRING"))
        _tick(j, clock, 6)
        assert j._best.tier == "T2"

    def test_one_entry_line_one_exit_line(self, tmp_path, caplog):
        j, clock = self._settled_on_t2(tmp_path, WB6CXC_STUCK)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _tick(j, clock, 6)
            j._fixture_state["sources"] = HEALTHY_POOL
            _tick(j, clock, 4)
        entries = [r for r in caplog.records
                   if "NOT refused on T2's word" in r.getMessage()]
        exits = [r for r in caplog.records
                 if "T2's word counts against T6 again" in r.getMessage()]
        assert (len(entries), len(exits)) == (1, 1)
        assert "no fault" in exits[0].getMessage()
        assert j._t2_overrule is None

    def test_a_refused_t6_whose_t2_heals_ends_the_episode(self, tmp_path,
                                                         caplog):
        """Still under the upgrade window when T2 heals: the gate refuses
        on the healthy T2's word again, and says so once."""
        j, clock = self._settled_on_t2(tmp_path, WB6CXC_STUCK)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            _tick(j, clock, 1)
            j._fixture_state["sources"] = HEALTHY_POOL
            _tick(j, clock, 6)
        assert j._best.tier == "T2"
        exits = [r for r in caplog.records
                 if "T2's word counts against T6 again" in r.getMessage()]
        assert len(exits) == 1

    def test_agreement_needs_no_overrule(self, tmp_path):
        j, clock = self._settled_on_t2(tmp_path, WB6CXC_STUCK,
                                       t2_offset_s=0.0)
        _tick(j, clock, 6)
        assert j._best.tier == "T6"
        assert j._t2_overrule is None


def test_thresholds_are_the_documented_ones():
    assert (SELECTED_STALE_S, FRESHER_BY_S) == (600.0, 300.0)


def test_only_t2_loses_its_vacuous_trust(tmp_path):
    """A T4 (or any tier but T2) that disagrees keeps its refusal even
    with chrony faults in its detail: T4 qualifies on its own gates."""
    clock = _Clock()
    j = _judge(tmp_path, clock, WB6CXC_STUCK)
    t6 = BenchReading(tier="T6", utc=clock.wall, sigma_ns=9e5,
                      mono=clock.mono_v, plane="label",
                      detail={"anchor_tier": "T6",
                              "t6_health": dict(CREDIBLE)})
    for tier in ("T4", "T3"):
        ref = BenchReading(
            tier=tier, utc=clock.wall - 0.185, sigma_ns=2e5,
            mono=clock.mono_v,
            detail={"chrony_health": chrony_source_health(WB6CXC_STUCK)})
        assert j._t2_overrule_reason_locked(t6, ref) is None
        assert not j._cross_gate_ok_locked(t6, ref, clock.mono_v)


class TestAHeldT6:
    """With a chain-delay record in force the judge re-tests a held T6
    every tick against the tightest host reading; a faulted T2 must not
    drive it out either."""

    def _held(self, tmp_path, sources):
        from hf_timestd.core.chain_delay_store import ChainDelayRecord
        clock = _Clock()
        j = OffsetJudge(config={"enabled": True, "tick_seconds": 10.0,
                                "label_plane_measure": False,
                                "chain_delay_path": str(tmp_path / "cd.json")},
                        benches=[], publish_path=tmp_path / "oj.json",
                        time_fn=clock.time, mono_fn=clock.mono)
        j.chain_delay_store.set_record(ChainDelayRecord(
            offset_ns=0.0, sigma_ns=1e6, spread_ns=1e6, n=500, n_eff=100.0,
            span_s=7200.0, learned_from_utc="x", learned_to_utc="y",
            lower_tier="T2", state="confirmed"))
        t6 = BenchReading(tier="T6", utc=clock.wall, sigma_ns=900_000.0,
                          mono=clock.mono_v, plane="label",
                          detail={"anchor_tier": "T6",
                                  "t6_health": dict(CREDIBLE)})
        t2 = BenchReading(tier="T2", utc=clock.wall - 0.185, sigma_ns=20e6,
                          mono=clock.mono_v, detail={
                              "chrony_health": chrony_source_health(sources)})
        return j, clock, [t6, t2]

    def test_a_faulted_t2_never_starts_the_drop_clock(self, tmp_path):
        j, clock, by_rank = self._held(tmp_path, WB6CXC_STUCK)
        for _ in range(100):
            clock.mono_v += 10.0
            assert j._held_t6_check_locked(by_rank, clock.mono_v) == "ok"

    def test_a_healthy_t2_still_drops_it(self, tmp_path):
        j, clock, by_rank = self._held(tmp_path, HEALTHY_POOL)
        verdicts = set()
        for _ in range(100):
            clock.mono_v += 10.0
            verdicts.add(j._held_t6_check_locked(by_rank, clock.mono_v))
        assert verdicts & {"drop", "absent"}


def test_the_recorder_publishes_t6s_credentials():
    from types import SimpleNamespace
    from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
    r = CoreRecorderV2.__new__(CoreRecorderV2)
    r._t6_authority = SimpleNamespace(state=SimpleNamespace(
        name="AUTHORITATIVE"))
    r._t6_last_verdict = SimpleNamespace(passed=True)
    r._t6_block_named = (123, 1_700_000_000)
    h = r._t6_health_state()
    assert h["battery_passed"] is True and h["second_named"] is True
    r._t6_block_named = (123, None)
    r._t6_last_verdict = SimpleNamespace(passed=False)
    h = r._t6_health_state()
    assert h["battery_passed"] is False and h["second_named"] is False
