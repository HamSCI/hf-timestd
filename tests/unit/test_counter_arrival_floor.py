"""The T3 bench's least-delayed-arrival floor, per RTP counter.

AC0G-B4 shadow record (2026-10-31/11-01): the judge's T3 reading against
T6 minus the direct label comparison t3_minus_t6_ms ran one-sided, median
-6.6 ms, p25 -11.7, p5 -18, on every channel.  T6's bench hands its label
to the monotonic clock through the least-delayed arrival; T3's took the
latest arrival and so read early by its delivery latency.
"""

import random

import pytest

from hf_timestd.core.counter_arrival_floor import CounterArrivalFloor
from hf_timestd.core.t6_arrival_floor import ArrivalFloorTracker

SR = 24000


def _feed(floor, n, t0=100.0, rtp0=1_000_000, latency=lambda i: 0.0, step=0.02):
    """n deliveries, every ``step`` s: the newest sample was taken at
    t0 + i*step and arrives ``latency(i)`` later."""
    last = None
    for i in range(n):
        taken = t0 + i * step
        rtp = (rtp0 + int(round(i * step * SR))) & 0xFFFFFFFF
        mono = taken + latency(i)
        floor.note(rtp, mono)
        last = (rtp, mono)
    return last


def test_the_lead_restores_the_latency_of_the_latest_arrival():
    """Latency sweeps 0..19.2 ms (a whole blocktime, B4's measured
    pattern).  The latest arrival came 15 ms late; the floor's least
    delayed arrival came 0 ms late, so the lead is +15 ms."""
    f = CounterArrivalFloor(SR)
    lat = lambda i: (i % 17) * 0.0012  # 0 .. 19.2 ms
    rtp, mono = _feed(f, 60, latency=lat)
    lag = lat(59)
    got = f.lead(rtp, mono, mono_now=mono)
    assert got is not None
    lead_s, est = got
    assert lead_s == pytest.approx(lag, abs=2.0 / SR)
    assert est.n > 1


def test_the_floor_follows_the_counter_through_a_wrap():
    f = CounterArrivalFloor(SR)
    rtp, mono = _feed(f, 60, rtp0=2**32 - 300, latency=lambda i: 0.004 * (i % 3))
    lead_s, est = f.lead(rtp, mono, mono_now=mono)
    assert lead_s == pytest.approx(0.004 * (59 % 3), abs=2.0 / SR)
    assert est.n == 60, "a wrap is no discontinuity: the window survives it"


def test_a_counter_restart_resets_the_floor():
    """A radiod restart renumbers the samples: the old run's arrivals
    describe another counter and leave the window at once."""
    f = CounterArrivalFloor(SR)
    _feed(f, 30, latency=lambda i: 0.0)
    rtp, mono = _feed(f, 5, t0=100.6, rtp0=77, latency=lambda i: 0.010)
    lead_s, est = f.lead(rtp, mono, mono_now=mono)
    assert est.n == 5
    assert lead_s == pytest.approx(0.0, abs=2.0 / SR)


def test_no_arrival_in_the_window_gives_no_lead():
    f = CounterArrivalFloor(SR)
    rtp, mono = _feed(f, 10)
    assert f.lead(rtp, mono, mono_now=mono + 10.0) is None
    assert CounterArrivalFloor(SR).lead(1, 1.0, mono_now=1.0) is None


def test_sigma_is_the_shared_trackers_measured_sigma():
    """One floor machine for T6 and T3: the sigma comes from
    ArrivalFloorTracker's own history of floor estimates."""
    f = CounterArrivalFloor(SR)
    rng = random.Random(3)
    rtp = mono = None
    for k in range(5):
        rtp, mono = _feed(f, 100, t0=100.0 + 2.0 * k,
                          rtp0=1_000_000 + int(2.0 * k * SR),
                          latency=lambda i: rng.uniform(0.0, 0.0192))
        _, est = f.lead(rtp, mono, mono_now=mono)
    assert est.sigma_ns < ArrivalFloorTracker.UNMEASURED_SIGMA_NS
    assert est.sigma_ns >= ArrivalFloorTracker.MIN_SIGMA_NS
