"""Task A8: the judge's T3 bench follows the admission decision through the
gate, and carries the clock term.

One decision now drives every T3 surface in the judge: adoption, witness
dissent, the label-plane tracker, ``label_plane_anchor`` and §18.  The
judge asks the gate at decision time; it refreshes nothing itself (the
recorder refreshes the gate on its revalidation tick, Task A7).

The plan's test named ``judge._dissent_voters()``; the judge has no such
method.  These tests read the dissent the judge actually publishes,
``judge._dissent`` (``witness_dissent.DissentWatch``), through the
harness of ``test_shadow_only_never_dissents``.
"""

import math

import pytest

from hf_timestd.core.clock_stability import ClockStability
from hf_timestd.core.offset_judge import BenchReading, HfAcquiredBench, OffsetJudge
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.t3_admission_gate import T3AdmissionGate
from tests.unit.test_shadow_only_never_dissents import (
    KEY,
    OFF_S,
    WALL0,
    _Bench,
    _Clock,
    _hpps_floor,
    _t4,
    _unix_to_gps_ns,
)

SR = 24000


class _Gate:
    """A gate the test opens and closes; the judge only reads it."""

    def __init__(self, admitted):
        self._admitted = bool(admitted)
        self.refreshed = 0

    def set(self, admitted):
        self._admitted = bool(admitted)

    def admitted(self):
        return self._admitted

    def reason(self):
        return "admitted (test)" if self._admitted else "withheld (test)"

    def clock(self):
        return None

    def refresh(self):
        self.refreshed += 1


def _hf_acquired(clock, authoritative=True):
    return _Bench(clock, "T3", 1_000_000.0, "hf_acquired", offset_s=OFF_S,
                  plane="label", authoritative=authoritative)


def _tick(judge, clock, n, step=10.0):
    for _ in range(n):
        judge.tick()
        clock.mono += step


def _dissent_judge(tmp_path, gate, hf=_hf_acquired):
    clock = _Clock()
    t6 = _Bench(clock, "T6", 10_000.0, "native_anchor", plane="label")
    judge = OffsetJudge(config={"enabled": True, "upgrade_polls": 1},
                        benches=[t6, _t4(clock), hf(clock)],
                        publish_path=tmp_path / "offset_judge.json",
                        time_fn=clock.wall, mono_fn=lambda: clock.mono,
                        t3_gate=gate)
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(WALL0), 0, SR)
    _tick(judge, clock, 3)              # every witness agrees: T6 adopted
    assert judge._best.tier == "T6"
    clock.departure_s = OFF_S           # then T6 walks 10 ms off
    return judge, clock


# ── the decision drives the dissent vote ─────────────────────────────


def test_admitted_t3_votes_and_unadmitted_t3_does_not(tmp_path):
    gate = _Gate(admitted=False)
    judge, clock = _dissent_judge(tmp_path, gate)
    _tick(judge, clock, 21)
    assert "T3" in judge._shadow_residuals
    assert judge._dissent is None
    gate.set(True)
    _tick(judge, clock, 21)
    assert judge._dissent is not None
    assert "T3" in judge._dissent.tiers
    assert _hpps_floor(judge) is not None
    # Withdrawal takes the vote away at the next decision.
    gate.set(False)
    _tick(judge, clock, 21)
    assert judge._dissent is None
    assert gate.refreshed == 0          # the judge never refreshes the gate


def test_an_admitted_witness_plane_still_never_votes(tmp_path):
    """Conservative choice: a reading the one gate refuses authority
    (``authoritative`` False, a WITNESS-state plane) does not vote, even
    while T3 holds admission."""
    gate = _Gate(admitted=True)
    judge, clock = _dissent_judge(
        tmp_path, gate, hf=lambda c: _hf_acquired(c, authoritative=False))
    _tick(judge, clock, 21)
    assert "T3" in judge._shadow_residuals
    assert judge._dissent is None


# ── the decision drives adoption, under the cross-bench gate ─────────


class _Fixed:
    def __init__(self, reading):
        self.reading = reading

    def poll(self):
        return self.reading


def _wan_judge(tmp_path, gate):
    t2 = BenchReading(tier="T2", utc=WALL0, sigma_ns=20_000_000.0,
                      mono=1000.0, detail={"bench": "chrony"})
    t3 = BenchReading(tier="T3", utc=WALL0, sigma_ns=1_000_000.0, mono=1000.0,
                      detail={"bench": "hf_acquired", "authoritative": True},
                      plane="label")
    judge = OffsetJudge(config={"enabled": True, "upgrade_polls": 3},
                        benches=[_Fixed(t2), _Fixed(t3)],
                        publish_path=tmp_path / "offset_judge.json",
                        time_fn=lambda: WALL0, mono_fn=lambda: 1000.0,
                        t3_gate=gate)
    judge.register_radiod_pair(KEY, _unix_to_gps_ns(WALL0), 0, SR)
    return judge


def test_adoption_follows_the_gate(tmp_path):
    gate = _Gate(admitted=False)
    judge = _wan_judge(tmp_path, gate)
    for _ in range(6):
        judge.tick()
    assert judge.offset_for(KEY, 0).tier == "T2"
    gate.set(True)
    for _ in range(6):
        judge.tick()
    assert judge.offset_for(KEY, 0).tier == "T3"
    gate.set(False)
    judge.tick()
    assert judge.offset_for(KEY, 0).tier == "T2"


def test_a_bool_builds_a_fixed_gate(tmp_path):
    on = OffsetJudge(publish_path=tmp_path / "a.json", t3_admitted=True)
    off = OffsetJudge(publish_path=tmp_path / "b.json")
    assert on.t3_admitted is True and on._shadow_only("hf_acquired") is False
    assert off.t3_admitted is False and off._shadow_only("hf_acquired") is True
    assert off._shadow_only("native_anchor") is False
    gate = _Gate(admitted=True)
    j = OffsetJudge(publish_path=tmp_path / "c.json", t3_gate=gate,
                    t3_admitted=False)
    assert j.t3_admitted is True        # a gate outranks the bool
    with pytest.raises(AttributeError):
        j.t3_admitted = False           # read-only: the gate decides


# ── the clock term ───────────────────────────────────────────────────


U0 = 1_800_000_000.0
R0 = 1_000_000


def _store(tmp_path, written_at, sigma_ms=0.5):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json",
                           time_fn=lambda: written_at)
    st.write_summary(
        Registration("ep-1", rtp_ref=R0, utc_ref=U0, sample_rate=SR,
                     sigma_ms=sigma_ms, channel="fused", verified=True,
                     n_minutes=10, rtp_domain="SHARED_10000"),
        ["SHARED_10000"], "ACQUIRED", {}, writer="SHARED_10000")
    return st


def _stability(drift, window_s=7200):
    return ClockStability(tau_cross_s=600, window_s=window_s, hold_s=1800,
                          drift_ns_per_s=drift, n_points=1440)


def _bench(tmp_path, age_s, clock=None):
    written = U0
    st = _store(tmp_path, written)
    mono = 5000.0
    return HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (R0, mono, SR)}, store=st,
        mono_fn=lambda: mono, time_fn=lambda: written + age_s,
        clock_provider=lambda: clock)


def test_the_clock_term_widens_t3_sigma_with_age(tmp_path):
    # drift 100 ns/s, registration age 60 s, summary sigma 0.5 ms -> floor 1.0 ms
    reading = _bench(tmp_path, 60.0, _stability(100.0)).poll()
    assert reading is not None
    assert reading.sigma_ns == pytest.approx(math.hypot(1e6, 6000.0))


def test_a_gpsdo_clock_holds_t3_past_180_s(tmp_path):
    # drift 0.001 ns/s, window 7200: a 600 s old registration still reads
    reading = _bench(tmp_path, 600.0, _stability(0.001)).poll()
    assert reading is not None
    # The one gate downstream uses the same limit, so the reading keeps
    # its authority.
    assert reading.detail["authoritative"] is True
    # ... but never past the window.
    assert _bench(tmp_path, 7300.0, _stability(0.001)).poll() is None


def test_a_crystal_clock_drops_t3_sooner(tmp_path):
    # drift 10000 ns/s -> 1e6/1e4 = 100 s, clamps up to 180 s
    assert _bench(tmp_path, 170.0, _stability(10_000.0)).poll() is not None
    assert _bench(tmp_path, 200.0, _stability(10_000.0)).poll() is None


def test_a_middling_clock_sets_the_limit_where_the_term_reaches_1_ms(tmp_path):
    # drift 1000 ns/s -> limit 1000 s
    assert _bench(tmp_path, 990.0, _stability(1000.0)).poll() is not None
    assert _bench(tmp_path, 1010.0, _stability(1000.0)).poll() is None


@pytest.mark.parametrize("clock", [
    None, _stability(None), _stability(0.0), _stability(-5.0),
    _stability(float("nan")), _stability(float("inf")),
])
def test_unmeasured_stability_keeps_todays_bench(tmp_path, clock):
    # clock_provider returns None (or no usable drift): sigma and the
    # 180 s limit unchanged
    reading = _bench(tmp_path, 60.0, clock).poll()
    assert reading.sigma_ns == pytest.approx(1e6)
    assert _bench(tmp_path, 170.0, clock).poll() is not None
    assert _bench(tmp_path, 190.0, clock).poll() is None


def test_a_failing_clock_provider_keeps_todays_bench(tmp_path):
    def boom():
        raise RuntimeError("gate trouble")
    st = _store(tmp_path, U0)
    bench = HfAcquiredBench(
        provider=lambda: {"SHARED_10000": (R0, 5000.0, SR)}, store=st,
        mono_fn=lambda: 5000.0, time_fn=lambda: U0 + 60.0,
        clock_provider=boom)
    assert bench.poll().sigma_ns == pytest.approx(1e6)
