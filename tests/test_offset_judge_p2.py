"""Offset Judge P2 tests (docs/OFFSET-JUDGE-SPEC-2026-08-05.md §10 P2).

Covers:
  * T5 pairing math (audit G5b: anchor_offset_ns finally produced) +
    sigma honesty (latency floor, spread widening, refusal gates,
    window fracture);
  * T6 NativeAnchorBench projection correctness vs NativeAnchor
    arithmetic;
  * tier cascade prefers T6 > T5 > T4 with the existing hysteresis
    discipline (upgrade needs N polls, degrade immediate);
  * revalidation tick: a wrong-but-steady radiod pair keeps being
    re-judged (P1 mechanism, asserted); a CHANGED pair is re-observed
    and adopted; a steady pair causes no mapping churn;
  * ring-vs-writer anchor consistency after judged correction
    (audit G6) incl. the 5 ms drift-re-anchor hysteresis.

Runnable with SYSTEM python3 + numpy only — reuses the P1 namespace
bootstrap and additionally stubs `ka9q` (stream_recorder_v2 imports it
at module level; none of its functionality is exercised here).
"""
from __future__ import annotations

import importlib
import json
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _install_namespace() -> None:
    """Register hf_timestd / hf_timestd.core without running __init__.py;
    stub `toml` and `ka9q` when absent (P1 trick + the P2 extension)."""
    if "hf_timestd" not in sys.modules:
        pkg = types.ModuleType("hf_timestd")
        pkg.__path__ = [str(SRC / "hf_timestd")]
        sys.modules["hf_timestd"] = pkg
    if "hf_timestd.core" not in sys.modules:
        core = types.ModuleType("hf_timestd.core")
        core.__path__ = [str(SRC / "hf_timestd" / "core")]
        sys.modules["hf_timestd.core"] = core
    try:
        import toml  # noqa: F401
    except ImportError:
        stub = types.ModuleType("toml")
        stub.load = lambda *a, **k: {}
        stub.loads = lambda *a, **k: {}
        sys.modules["toml"] = stub
    try:
        import ka9q  # noqa: F401
    except ImportError:
        kq = types.ModuleType("ka9q")

        class _Placeholder:  # never instantiated in these tests
            def __init__(self, *a, **k):
                raise RuntimeError("ka9q stub must not be instantiated")

        kq.RadiodStream = _Placeholder
        kq.ChannelInfo = _Placeholder
        kq.StreamQuality = _Placeholder
        kq.RadiodControl = _Placeholder
        sys.modules["ka9q"] = kq


_install_namespace()

oj = importlib.import_module("hf_timestd.core.offset_judge")
t5m = importlib.import_module("hf_timestd.core.t5_rtp_pairing")
na = importlib.import_module("hf_timestd.core.native_anchor")
baw = importlib.import_module("hf_timestd.core.binary_archive_writer")
srv2 = importlib.import_module("hf_timestd.core.stream_recorder_v2")
btm = importlib.import_module("hf_timestd.core.buffer_timing")

OffsetJudge = oj.OffsetJudge
OffsetVerdict = oj.OffsetVerdict
BenchReading = oj.BenchReading
NativeAnchorBench = oj.NativeAnchorBench
LbeT5Bench = oj.LbeT5Bench
T5RtpPairing = t5m.T5RtpPairing
T5PairingProduct = t5m.T5PairingProduct
NativeAnchor = na.NativeAnchor
utc_ns_at_rtp = na.utc_ns_at_rtp
BinaryArchiveWriter = baw.BinaryArchiveWriter
BinaryArchiveConfig = baw.BinaryArchiveConfig
StreamRecorderV2 = srv2.StreamRecorderV2


# ────────────────────────────────────────────────────────────────────
# Helpers (mirroring the P1 suite)
# ────────────────────────────────────────────────────────────────────

WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0x1234ABCD)
SR = 16000


def unix_to_gps_ns(unix_s: float) -> int:
    guess = int((unix_s - 315964800) * 1e9)
    back = oj.gps_time_ns_to_unix(guess)
    return guess + int(round((unix_s - back) * 1e9))


class FakeClock:
    def __init__(self, wall: float = WALL0, mono: float = 1000.0):
        self.wall = wall
        self.mono_v = mono

    def time(self) -> float:
        return self.wall

    def mono(self) -> float:
        return self.mono_v

    def advance(self, dt: float) -> None:
        self.wall += dt
        self.mono_v += dt


class FakeBench:
    def __init__(self, clock, tier="T4", sigma_ns=1e5,
                 bench_error_s=0.0, available=True):
        self.clock = clock
        self.tier = tier
        self.sigma_ns = sigma_ns
        self.bench_error_s = bench_error_s
        self.available = available

    def poll(self):
        if not self.available:
            return None
        return BenchReading(
            tier=self.tier,
            utc=self.clock.wall + self.bench_error_s,
            sigma_ns=self.sigma_ns,
            mono=self.clock.mono_v,
        )


class FakeReading:
    """Duck-typed Lb1421Reading."""

    def __init__(self, pps_utc_sec, mono, valid_fix=True):
        self.pps_utc_sec = int(pps_utc_sec)
        self.host_monotonic_at_read = float(mono)
        self.valid_fix = bool(valid_fix)


def make_pairing(clock: FakeClock) -> T5RtpPairing:
    return T5RtpPairing(time_fn=clock.time, mono_fn=clock.mono)


def make_anchor(truth_utc: float, anchor_rtp: int = 5000,
                sr: int = SR, chain_delay_ns: int = 0) -> NativeAnchor:
    """Anchor whose label for anchor_rtp is exactly truth_utc."""
    utc_ns = int(round(truth_utc * 1e9))
    return NativeAnchor(
        anchor_rtp=anchor_rtp & 0xFFFFFFFF,
        anchor_utc_ns=utc_ns,
        sample_rate_hz=sr,
        chain_delay_ns=chain_delay_ns,
        captured_at_utc_ns=utc_ns,
        captured_via_tier="T5",
    )


class FakeRing:
    """Captures update_anchor calls (item 4)."""

    def __init__(self):
        self.anchors = []

    def update_anchor(self, gps_time_ns, rtp_timesnap):
        self.anchors.append((int(gps_time_ns), int(rtp_timesnap)))


class StubJudge:
    def __init__(self, verdict=None):
        self.verdict = verdict
        self.registered = []

    def register_radiod_pair(self, key, gps_ns, snap, rate):
        self.registered.append((key, int(gps_ns), int(snap), int(rate)))

    def offset_for(self, key, rtp):
        return self.verdict


def make_writer(tmp_path, judge=None, key=None, sample_rate=100):
    cfg = BinaryArchiveConfig(
        channel_name="TEST_10_MHz",
        frequency_hz=10e6,
        sample_rate=sample_rate,
        output_dir=tmp_path / "raw",
        compression="none",
        file_duration_sec=2,
    )
    return BinaryArchiveWriter(cfg, offset_judge=judge, source_key=key)


def make_recorder(*, channel_info=None, writer=None, judge=None,
                  key=KEY, ring=None, sample_rate=100, description="TEST"):
    """__new__-bypassed StreamRecorderV2 with just the state the P2
    revalidation/ring paths touch (the production pattern for unit
    tests noted in core_recorder_v2)."""
    rec = StreamRecorderV2.__new__(StreamRecorderV2)
    rec.config = types.SimpleNamespace(
        description=description, sample_rate=sample_rate,
        ssrc=key[1] if key else 0,
    )
    rec.channel_info = channel_info
    rec.archive_writer = writer
    rec._offset_judge = judge
    rec._judge_source_key = key
    rec.ring_buffer = ring
    rec._ring_anchor_state = None
    rec._control = None
    return rec


# ────────────────────────────────────────────────────────────────────
# T5 pairing math + sigma honesty (item 1)
# ────────────────────────────────────────────────────────────────────

class TestT5Pairing:
    def _reading(self, clock, pps_age_s=2.0):
        return FakeReading(int(clock.wall) - int(pps_age_s), clock.mono_v)

    def test_honest_pair_reads_zero(self):
        clock = FakeClock()
        p = make_pairing(clock)
        snap = 1000
        gps_ns = unix_to_gps_ns(clock.wall - 10.0)  # pair snapped 10 s ago
        p.note_arrival(snap + 10 * SR)              # counter agrees with truth
        prod = p.compute(self._reading(clock), gps_ns, snap, SR)
        assert prod is not None
        assert abs(prod.anchor_offset_ns) < 1e6
        assert prod.sigma_ns == pytest.approx(T5RtpPairing.SIGMA_FLOOR_NS)
        assert prod.pps_utc_sec == int(clock.wall) - 2
        assert prod.truth_utc == pytest.approx(clock.wall)

    def test_wedged_pair_reads_prediction_minus_truth(self):
        """radiod epoch 1203 s behind truth ⇒ anchor_offset ≈ −1203 s
        (prediction − truth, the documented sign)."""
        clock = FakeClock()
        p = make_pairing(clock)
        snap = 1000
        gps_ns = unix_to_gps_ns(clock.wall - 10.0 - 1203.0)
        p.note_arrival(snap + 10 * SR)
        prod = p.compute(self._reading(clock), gps_ns, snap, SR)
        assert prod.anchor_offset_ns == pytest.approx(-1203e9, abs=1e7)

    def test_sigma_widens_with_observed_jitter(self):
        """Alternating ±60 ms raw jitter must widen sigma beyond the
        25 ms latency floor — honesty over optimism."""
        clock = FakeClock()
        p = make_pairing(clock)
        snap = 1000
        gps_ns = unix_to_gps_ns(clock.wall - 10.0)  # pair fixed, truth-consistent
        prod = None
        for i in range(8):
            jitter_s = 0.060 if i % 2 == 0 else -0.060
            # Arrival whose counter position embeds the jitter.
            rtp = snap + int((10.0 + i + jitter_s) * SR)
            p.note_arrival(rtp)
            prod = p.compute(self._reading(clock), gps_ns, snap, SR)
            clock.advance(1.0)
        assert prod is not None
        assert prod.sigma_ns > T5RtpPairing.SIGMA_FLOOR_NS
        assert prod.sigma_ns > 50e6  # ≈ 1.4826 × 60 ms MAD

    def test_never_claims_better_than_latency_floor(self):
        clock = FakeClock()
        p = make_pairing(clock)
        snap = 1000
        gps_ns = unix_to_gps_ns(clock.wall - 10.0)  # pair fixed, truth-consistent
        for i in range(6):  # perfectly repeatable measurements
            p.note_arrival(snap + int((10.0 + i) * SR))
            prod = p.compute(self._reading(clock), gps_ns, snap, SR)
            clock.advance(1.0)
        assert prod.sigma_ns == pytest.approx(T5RtpPairing.SIGMA_FLOOR_NS)

    def test_refuses_without_arrival(self):
        clock = FakeClock()
        p = make_pairing(clock)
        assert p.compute(self._reading(clock),
                         unix_to_gps_ns(clock.wall), 0, SR) is None

    def test_refuses_stale_arrival(self):
        clock = FakeClock()
        p = make_pairing(clock)
        p.note_arrival(123456)
        clock.advance(T5RtpPairing.ARRIVAL_MAX_AGE_S + 1.0)
        assert p.compute(self._reading(clock),
                         unix_to_gps_ns(clock.wall), 0, SR) is None

    def test_refuses_nmea_wall_disagreement(self):
        """Host wall vs NMEA integer second outside the attestable
        window ⇒ refuse rather than emit a poisoned pairing."""
        clock = FakeClock()
        p = make_pairing(clock)
        p.note_arrival(1000)
        bad = FakeReading(int(clock.wall) + 30, clock.mono_v)  # NMEA 'ahead'
        assert p.compute(bad, unix_to_gps_ns(clock.wall), 1000, SR) is None

    def test_window_fractures_on_epoch_step(self):
        """A >5 s raw step (radiod restart) clears the window — fresh
        estimate, no smoothing across the fracture."""
        clock = FakeClock()
        p = make_pairing(clock)
        snap = 1000
        wall0 = clock.wall
        gps_ns = unix_to_gps_ns(wall0 - 10.0)  # pair fixed, truth-consistent
        for i in range(5):
            p.note_arrival(snap + int((10.0 + i) * SR))
            p.compute(self._reading(clock), gps_ns, snap, SR)
            clock.advance(1.0)
        # radiod re-announces an epoch 1203 s off (same counter space)
        gps_wedged = unix_to_gps_ns(wall0 - 10.0 - 1203.0)
        p.note_arrival(snap + int((10.0 + 5.0) * SR))
        prod = p.compute(self._reading(clock), gps_wedged, snap, SR)
        assert prod.n_window == 1  # window restarted
        assert prod.anchor_offset_ns == pytest.approx(-1203e9, abs=1e7)


# ────────────────────────────────────────────────────────────────────
# T6 NativeAnchorBench (item 2)
# ────────────────────────────────────────────────────────────────────

class TestNativeAnchorBench:
    def test_projection_matches_native_anchor_arithmetic(self):
        clock = FakeClock()
        anchor = make_anchor(truth_utc=clock.wall - 20.0, anchor_rtp=5000)
        arrival_rtp = 5000 + 20 * SR  # sample labelled exactly clock.wall
        bench = NativeAnchorBench(
            provider=lambda: (anchor, arrival_rtp, clock.mono_v),
            mono_fn=clock.mono,
        )
        r = bench.poll()
        assert r is not None and r.tier == "T6"
        expected = utc_ns_at_rtp(arrival_rtp, anchor) / 1e9
        assert r.utc == pytest.approx(expected, abs=1e-9)
        assert r.utc == pytest.approx(clock.wall, abs=1e-6)
        assert r.sigma_ns == pytest.approx(
            NativeAnchorBench.LATENCY_SIGMA_FLOOR_NS)
        assert r.mono == clock.mono_v

    def test_no_anchor_no_reading(self):
        clock = FakeClock()
        bench = NativeAnchorBench(provider=lambda: None, mono_fn=clock.mono)
        assert bench.poll() is None

    def test_stale_arrival_refused(self):
        clock = FakeClock()
        anchor = make_anchor(clock.wall)
        stale_mono = clock.mono_v - NativeAnchorBench.ARRIVAL_MAX_AGE_S - 1
        bench = NativeAnchorBench(
            provider=lambda: (anchor, 5000, stale_mono), mono_fn=clock.mono)
        assert bench.poll() is None

    def test_judge_measures_wedge_via_t6_bench(self):
        """Full chain: honest anchor + wedged radiod pair ⇒ verdict
        tier T6 with offset ≈ +1203 s (judge − radiod)."""
        clock = FakeClock()
        anchor = make_anchor(truth_utc=clock.wall - 20.0, anchor_rtp=5000)
        state = {"rtp": 5000 + 20 * SR, "mono": clock.mono_v}
        bench = NativeAnchorBench(
            provider=lambda: (anchor, state["rtp"], state["mono"]),
            mono_fn=clock.mono,
        )
        judge = OffsetJudge(
            config={"enabled": True}, benches=[bench],
            publish_path=Path("/dev/null"),  # publish failure is tolerated
            time_fn=clock.time, mono_fn=clock.mono,
        )
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 1203.0), 9000, SR)
        for _ in range(4):
            judge.tick()
            clock.advance(10.0)
            state["rtp"] += 10 * SR       # stream keeps flowing
            state["mono"] = clock.mono_v
        v = judge.offset_for(KEY, 9000)
        assert v is not None
        assert v.tier == "T6"
        assert v.offset_ns == pytest.approx(1203e9, abs=1e7)


# ────────────────────────────────────────────────────────────────────
# T5 bench + cascade (item 2)
# ────────────────────────────────────────────────────────────────────

class TestLbeT5Bench:
    def _product(self, clock, offset_ns=0):
        return T5PairingProduct(
            anchor_offset_ns=int(offset_ns), sigma_ns=25e6,
            truth_utc=clock.wall, arrival_mono=clock.mono_v,
            arrival_age_s=0.1, arrival_rtp=1234,
            pps_utc_sec=int(clock.wall) - 1, n_window=5,
        )

    def test_reframes_pairing_product(self):
        clock = FakeClock()
        prod = self._product(clock, offset_ns=-42_000_000)
        bench = LbeT5Bench(provider=lambda: prod, mono_fn=clock.mono)
        r = bench.poll()
        assert r is not None and r.tier == "T5"
        assert r.utc == pytest.approx(clock.wall)
        assert r.sigma_ns == pytest.approx(25e6)
        assert r.detail["anchor_offset_ns"] == -42_000_000

    def test_stale_product_refused(self):
        clock = FakeClock()
        prod = self._product(clock)
        clock.advance(LbeT5Bench.ARRIVAL_MAX_AGE_S + 1)
        bench = LbeT5Bench(provider=lambda: prod, mono_fn=clock.mono)
        assert bench.poll() is None


class TestCascadeT6T5T4:
    def test_prefers_t6_then_t5_then_t4_with_hysteresis(self, tmp_path):
        clock = FakeClock()
        t6 = FakeBench(clock, tier="T6", sigma_ns=25e6)
        t5 = FakeBench(clock, tier="T5", sigma_ns=25e6)
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = OffsetJudge(
            config={"enabled": True}, benches=[t4],
            publish_path=tmp_path / "offset_judge.json",
            time_fn=clock.time, mono_fn=clock.mono,
        )
        # P2 wiring path: substrate benches join via add_bench().
        judge.add_bench(t6)
        judge.add_bench(t5)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 0, SR)

        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T6"

        t6.available = False          # T6 dies -> immediate degrade to T5
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T5"

        t5.available = False          # T5 dies too -> T4 immediately
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T4"

        t6.available = True           # T6 back -> 3 consecutive polls
        for _ in range(2):
            clock.advance(10.0)
            judge.tick()
            assert judge.offset_for(KEY, 0).tier == "T4"
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T6"


# ────────────────────────────────────────────────────────────────────
# Revalidation tick (item 3)
# ────────────────────────────────────────────────────────────────────

class TestRevalidation:
    def test_wrong_but_steady_pair_is_rejudged_within_tick_budget(self, tmp_path):
        """The item-3 acceptance case: a steady-wrong pair needs no
        re-observation — the P1 judge re-judges it every 10 s tick.
        One tick after seeding, the verdict carries the full wedge."""
        clock = FakeClock(wall=time.time())
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = OffsetJudge(
            config={"enabled": True}, benches=[bench],
            publish_path=tmp_path / "offset_judge.json",
            time_fn=clock.time, mono_fn=clock.mono,
        )
        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            gps_ns = unix_to_gps_ns(clock.wall - 1203.0)
            w.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=5000)
            judge.tick()  # one 10 s tick
            v = judge.offset_for((KEY[0], KEY[1]), 5000)
            assert v is not None
            assert v.offset_ns == pytest.approx(1203e9, abs=1e7)
        finally:
            w.close()

    def test_changed_pair_is_adopted(self, tmp_path):
        """radiod restarted (new counter space + healed epoch) while the
        stream kept flowing: the tick must re-observe and adopt."""
        clock = FakeClock(wall=time.time())
        judge = StubJudge()
        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            gps0 = unix_to_gps_ns(clock.wall - 100.0)   # wedged seed
            w.add_timing_snapshot(gps_time_ns=gps0, rtp_timesnap=5000)
            # Listener-refreshed channel_info now shows the healed pair.
            gps1 = unix_to_gps_ns(clock.wall)
            ci = types.SimpleNamespace(gps_time=gps1, rtp_timesnap=777_000,
                                       ssrc=KEY[1])
            ring = FakeRing()
            rec = make_recorder(channel_info=ci, writer=w, judge=judge,
                                ring=ring)
            diff_before = w.evaluate_pair(gps1, 777_000)
            assert abs(diff_before) > StreamRecorderV2.REVALIDATE_ADOPT_THRESHOLD_S
            rec.revalidate_radiod_pair()
            # Writer mapping now consistent with the fresh pair...
            assert abs(w.evaluate_pair(gps1, 777_000)) < 1e-6
            # ...the judge saw the adoption...
            assert (KEY, gps1, 777_000, 100) in judge.registered
            # ...and the ring was re-anchored at the adoption.
            assert ring.anchors and ring.anchors[-1][1] == 777_000
        finally:
            w.close()

    def test_steady_pair_no_mapping_churn(self, tmp_path):
        """Sub-threshold agreement: the steel-ruler mapping and the ring
        are left alone (radiod status jitter is judged, not adopted)."""
        clock = FakeClock(wall=time.time())
        judge = StubJudge()
        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            gps0 = unix_to_gps_ns(clock.wall)
            w.add_timing_snapshot(gps_time_ns=gps0, rtp_timesnap=5000)
            n_registered = len(judge.registered)
            # Fresh pair 0.3 s off (< 0.75 s threshold), 10 s later on
            # the counter.
            gps1 = unix_to_gps_ns(clock.wall + 10.0 + 0.3)
            ci = types.SimpleNamespace(gps_time=gps1,
                                       rtp_timesnap=5000 + 10 * 100,
                                       ssrc=KEY[1])
            ring = FakeRing()
            rec = make_recorder(channel_info=ci, writer=w, judge=judge,
                                ring=ring)
            rec.revalidate_radiod_pair()
            assert len(judge.registered) == n_registered  # no re-adoption
            assert ring.anchors == []                     # no ring churn
            # Mapping unchanged: the jittered pair still reads +0.3 s.
            assert w.evaluate_pair(gps1, 5000 + 10 * 100) == pytest.approx(
                0.3, abs=1e-6)
        finally:
            w.close()

    def test_archive_false_channel_feeds_judge_directly(self):
        judge = StubJudge()
        gps = unix_to_gps_ns(WALL0)
        ci = types.SimpleNamespace(gps_time=gps, rtp_timesnap=42, ssrc=KEY[1])
        rec = make_recorder(channel_info=ci, writer=None, judge=judge,
                            sample_rate=SR)
        rec.revalidate_radiod_pair()
        assert judge.registered == [(KEY, gps, 42, SR)]

    def test_missing_timing_is_harmless(self):
        ci = types.SimpleNamespace(gps_time=None, rtp_timesnap=None, ssrc=1)
        rec = make_recorder(channel_info=ci, writer=None, judge=StubJudge())
        rec.revalidate_radiod_pair()  # must not raise
        rec.channel_info = None
        rec.revalidate_radiod_pair()  # must not raise


# ────────────────────────────────────────────────────────────────────
# Ring/writer unification (item 4)
# ────────────────────────────────────────────────────────────────────

class TestRingUnification:
    def test_ring_anchor_matches_writer_corrected_labels(self, tmp_path):
        """After a judged correction, ring-resolved UTC must equal the
        writer's corrected label for the same RTP counter (audit G6)."""
        verdict = OffsetVerdict(
            offset_ns=1203e9, sigma_ns=1e5, tier="T6",
            judge_age_s=1.0, segment_id=2, in_violation=True,
        )
        judge = StubJudge(verdict=verdict)
        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            raw_utc = WALL0 - 1203.0
            gps_ns = unix_to_gps_ns(raw_utc)
            snap = 5000
            w.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=snap)
            ring = FakeRing()
            rec = make_recorder(writer=w, judge=judge, ring=ring)
            rec._update_ring_anchor(gps_ns, snap)
            assert ring.anchors
            ring_gps_ns, ring_snap = ring.anchors[-1]
            assert ring_gps_ns == gps_ns + int(1203e9)
            # Same-RTP equality, via the shared resolver both paths use:
            rtp = snap + 30 * 100  # 30 s later at sr=100
            ring_utc = btm.resolve_buffer_timing(
                {"start_rtp_timestamp": rtp, "gps_time_ns": ring_gps_ns,
                 "rtp_timesnap": ring_snap},
                sample_rate=100,
            ).sample0_utc
            writer_label = (w._rtp_to_unix_time(rtp)
                            + verdict.offset_ns / 1e9)
            assert ring_utc == pytest.approx(writer_label, abs=1e-6)
            # And the corrected label is at truth, not 1203 s in the past.
            assert ring_utc == pytest.approx(WALL0 + 30.0, abs=1e-3)
        finally:
            w.close()

    def test_drift_reanchors_beyond_5ms(self):
        judge = StubJudge(verdict=OffsetVerdict(
            offset_ns=0.0, sigma_ns=1e5, tier="T4",
            judge_age_s=1.0, segment_id=1, in_violation=False))
        ring = FakeRing()
        gps_ns = unix_to_gps_ns(WALL0)
        rec = make_recorder(writer=None, judge=judge, ring=ring)
        rec._update_ring_anchor(gps_ns, 5000)
        assert len(ring.anchors) == 1
        # Offset moves 2 ms — inside hysteresis, no ring write.
        judge.verdict = OffsetVerdict(2e6, 1e5, "T4", 1.0, 1, False)
        rec._reanchor_ring_if_offset_drifted()
        assert len(ring.anchors) == 1
        # Offset moves 50 ms — re-anchor with the correction folded in.
        judge.verdict = OffsetVerdict(50e6, 1e5, "T4", 1.0, 1, False)
        rec._reanchor_ring_if_offset_drifted()
        assert len(ring.anchors) == 2
        assert ring.anchors[-1][0] == gps_ns + int(50e6)

    def test_judge_absent_ring_gets_raw_pair(self):
        ring = FakeRing()
        gps_ns = unix_to_gps_ns(WALL0)
        rec = make_recorder(writer=None, judge=None, key=None, ring=ring)
        rec._judge_source_key = None
        rec._update_ring_anchor(gps_ns, 5000)
        assert ring.anchors == [(gps_ns, 5000)]
        rec._reanchor_ring_if_offset_drifted()  # no judge -> offset 0, quiet
        assert len(ring.anchors) == 1

    def test_corrected_anchor_passes_metrology_future_head_gate(self, tmp_path):
        """The metrology gate refuses head_utc > now + 120 s.  A wedge
        that previously future-dated the ring (+22 min incident class)
        resolves to ≈ now once the judged correction is folded in."""
        verdict = OffsetVerdict(-1320e9, 1e5, "T4", 1.0, 2, True)
        judge = StubJudge(verdict=verdict)
        ring = FakeRing()
        now = time.time()
        gps_ns = unix_to_gps_ns(now + 1320.0)  # radiod 22 min in the future
        rec = make_recorder(writer=None, judge=judge, ring=ring)
        rec._update_ring_anchor(gps_ns, 5000)
        ring_gps_ns, ring_snap = ring.anchors[-1]
        head_utc = btm.resolve_buffer_timing(
            {"start_rtp_timestamp": 5000, "gps_time_ns": ring_gps_ns,
             "rtp_timesnap": ring_snap},
            sample_rate=100,
        ).sample0_utc
        _RING_FUTURE_HEAD_SEC = 120.0  # metrology_service gate constant
        assert head_utc <= now + _RING_FUTURE_HEAD_SEC
        assert head_utc == pytest.approx(now, abs=1.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
