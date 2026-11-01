"""Fix 1: the judge's T3 bench hands its label to the monotonic clock
through the least-delayed arrival, as the T6 bench does.

AC0G-B4 shadow record (2026-10-31/11-01): the judge's T3-vs-T6 residual
minus the direct label comparison t3_minus_t6_ms ran one-sided, median
-6.6 ms, p25 -11.7, p5 -18, one -972 ms, the same on every channel: T6's
bench used its floor, T3's the latest arrival.
"""

import math
import types

import pytest

from hf_timestd.core.core_recorder_v2 import CoreRecorderV2
from hf_timestd.core.counter_arrival_floor import CounterArrivalFloor
from hf_timestd.core.offset_judge import HfAcquiredBench, NativeAnchorBench
from hf_timestd.core.registration_acquirer import Registration
from hf_timestd.core.registration_store import RegistrationStore
from hf_timestd.core.t6_arrival_floor import ArrivalFloorTracker
from tests.unit.synth_origins import ND_K0, counter_map_for

SR = 24000
R0 = 1_000_000
U0 = 1_800_000_000.0   # UTC of R0: the plane is exactly right
MONO_MINUS_UTC = -1_799_000_000.0  # mono = utc + this
MAP = counter_map_for(ND_K0[0], SR)  # one radiod run on both sides


def _store(tmp_path, now):
    st = RegistrationStore(tmp_path / "reg", tmp_path / "registration.json",
                           time_fn=lambda: now)
    st.write_summary(
        Registration("ep-1", rtp_ref=R0, utc_ref=U0, sample_rate=SR, sigma_ms=0.8,
                     channel="fused", verified=True, n_minutes=10,
                     rtp_domain="SHARED_10000", counter_map=MAP),
        ["SHARED_10000"], "WITNESS", {}, writer="SHARED_10000")
    return st


def _deliveries(n=80, step=0.02, latency=lambda i: (i % 17) * 0.0012):
    """(rtp, mono) of each batch's newest sample, taken at U0 + 1 + i*step
    and arriving latency(i) later."""
    out = []
    for i in range(n):
        taken = U0 + 1.0 + i * step
        rtp = R0 + int(round((taken - U0) * SR))
        out.append((rtp, taken + latency(i) + MONO_MINUS_UTC))
    return out


def _bench(st, arrival, mono_now):
    return HfAcquiredBench(provider=lambda: {"SHARED_10000": arrival}, store=st,
                           mono_fn=lambda: mono_now, time_fn=lambda: U0 + 5.0)


def _error_ms(reading):
    """The reading's UTC minus true UTC at its mono instant."""
    return (reading.utc - (reading.mono - MONO_MINUS_UTC)) * 1000.0


def test_without_the_floor_the_bench_reads_early_by_the_latency(tmp_path):
    """The pre-fix contract (a four-element arrival) keeps its behaviour:
    the latest arrival, 13.2 ms late, makes the reading 13.2 ms early."""
    st = _store(tmp_path, U0 + 5.0)
    d = _deliveries()
    rtp, mono = d[-1]
    r = _bench(st, (rtp, mono, SR, MAP), mono).poll()
    assert _error_ms(r) == pytest.approx(-(79 % 17) * 1.2, abs=0.1)


def test_the_floor_removes_the_delivery_latency(tmp_path):
    st = _store(tmp_path, U0 + 5.0)
    f = CounterArrivalFloor(SR)
    d = _deliveries()
    for rtp, mono in d:
        f.note(rtp, mono)
    rtp, mono = d[-1]
    floor = f.lead(rtp, mono, mono)
    r = _bench(st, (rtp, mono, SR, MAP, floor), mono).poll()
    assert _error_ms(r) == pytest.approx(0.0, abs=1000.0 / SR)
    assert r.detail["floor_n"] == floor[1].n
    assert r.detail["latest_arrival_lag_ns"] == pytest.approx(13.2e6, abs=1e5)
    # sigma: the registration's accuracy floor and the floor's own sigma,
    # in quadrature
    assert r.sigma_ns == pytest.approx(math.hypot(1e6, floor[1].sigma_ns))


def test_an_empty_floor_widens_sigma_to_the_transport_bound(tmp_path):
    """A provider that names the floor but has none in its window falls
    back to the latest arrival with T6's conservative transport bound."""
    st = _store(tmp_path, U0 + 5.0)
    rtp, mono = _deliveries()[-1]
    r = _bench(st, (rtp, mono, SR, MAP, None), mono).poll()
    assert r.sigma_ns == pytest.approx(
        math.hypot(1e6, NativeAnchorBench.LATENCY_SIGMA_FLOOR_NS))
    assert r.detail["floor_n"] is None


class _FakeStream:
    def __init__(self):
        self.tap = None

    def add_tap(self, cb):
        self.tap = cb


def test_the_recorder_feeds_one_floor_per_channel_and_hands_it_to_the_bench():
    rec = CoreRecorderV2.__new__(CoreRecorderV2)
    a, b = _FakeStream(), _FakeStream()
    rec._wire_t5_fallback_arrival("SHARED_10000", a, SR)
    rec._wire_t5_fallback_arrival("WWV_20000", b, SR)
    q = lambda r: types.SimpleNamespace(  # noqa: E731
        last_rtp_timestamp=r, delivered_rtp_start=r, batch_samples_delivered=240)
    for k in range(3):
        a.tap([0.0] * 240, q(1000 + 240 * k))
    b.tap([0.0] * 240, q(5000))
    got = rec._hf_acquired_bench_state()
    lead_s, est = got["SHARED_10000"][4]
    assert est.n == 3 and lead_s >= -1e-9
    assert got["WWV_20000"][4][1].n == 1
    assert est.sigma_ns == ArrivalFloorTracker.UNMEASURED_SIGMA_NS
