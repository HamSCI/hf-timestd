"""Offset Judge P5 tests: cross-bench consistency gate + T5 decoupling.

Covers the two 2026-08-05 follow-ons to the T6 displaced-peak incident
(docs/T6-DISPLACED-PEAK-62MS-2026-08-05.md):

  * cross-bench gate (docs/JUDGE-CROSS-BENCH-GATE-2026-08-05.md, spec
    §2 amendment): a biased upper bench is BLOCKED from adoption
    (judge stays on the trusted lower tier), the conflict flag +
    per-bench shadow residuals are published, the CRITICAL is
    rate-limited, recovery restarts the advance window cleanly,
    single-bench sites are unaffected, degrade-on-loss stays
    immediate, and the loss of the reference tier never admits a
    blocked candidate;
  * T5 bench decoupled from the T6 stream (core_recorder_v2 P5): with
    [timing.t6_pps] off, the LB-142x pairing falls back to an archive
    stream's (gps_time, rtp_timesnap, sample_rate) + arrivals — the
    pairing product names its grounding stream (source-key honesty),
    the T6 stream stays preferred when present, and the judge reaches
    T5 from the fallback only when T5 agrees with T4 within bound.

Runnable with SYSTEM python3 + numpy only — reuses the P1 namespace
bootstrap and the P2 `ka9q` stub (extended with the names
core_recorder_v2 imports at module level).
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import types
from pathlib import Path

import numpy as np  # noqa: F401 — offset_judge dependency
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _install_namespace() -> None:
    """Register hf_timestd / hf_timestd.core without running __init__.py;
    stub `toml` and `ka9q` when absent (P1/P2 trick).  The ka9q stub is
    top-up style: another suite in the same pytest run may already have
    installed a smaller stub, so missing names are added, never
    clobbered."""
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
        import ka9q
    except ImportError:
        ka9q = types.ModuleType("ka9q")
        sys.modules["ka9q"] = ka9q

    class _Placeholder:  # never instantiated in these tests
        def __init__(self, *a, **k):
            raise RuntimeError("ka9q stub must not be instantiated")

    for name in ("RadiodStream", "ChannelInfo", "StreamQuality",
                 "RadiodControl", "MultiStream"):
        if not hasattr(ka9q, name):
            setattr(ka9q, name, _Placeholder)
    if not hasattr(ka9q, "discover_channels"):
        ka9q.discover_channels = lambda *a, **k: []
    if not hasattr(ka9q, "Encoding"):
        class _Encoding:
            F32 = 4
            S16 = 1
        ka9q.Encoding = _Encoding


_install_namespace()

oj = importlib.import_module("hf_timestd.core.offset_judge")
t5m = importlib.import_module("hf_timestd.core.t5_rtp_pairing")
crm = importlib.import_module("hf_timestd.core.core_recorder_v2")

OffsetJudge = oj.OffsetJudge
BenchReading = oj.BenchReading
LbeT5Bench = oj.LbeT5Bench
T5RtpPairing = t5m.T5RtpPairing
CoreRecorderV2 = crm.CoreRecorderV2

WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0x1234ABCD)
KEY_STR = "hf-status.local/1234abcd"
SR = 24000
RTP = 480_000


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
    """Bench whose truth is wallclock + bench_error (0 = perfect)."""

    def __init__(self, clock: FakeClock, tier: str = "T4",
                 sigma_ns: float = 1e5, bench_error_s: float = 0.0,
                 available: bool = True):
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


def make_judge(clock: FakeClock, tmp_path: Path, benches, **cfg) -> OffsetJudge:
    config = {"enabled": True, "tick_seconds": 10.0, **cfg}
    return OffsetJudge(
        config=config,
        benches=list(benches),
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
    )


def tick_n(judge: OffsetJudge, clock: FakeClock, n: int,
           step: float = 10.0) -> None:
    for _ in range(n):
        clock.advance(step)
        judge.tick()


def register_healthy(judge: OffsetJudge, clock: FakeClock) -> None:
    judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, SR)


def read_json(path: Path):
    return json.loads(path.read_text())


def tier_of(judge: OffsetJudge) -> str:
    v = judge.offset_for(KEY, 5000)
    assert v is not None
    return v.tier


# ────────────────────────────────────────────────────────────────────
# Cross-bench consistency gate (feature 1)
# ────────────────────────────────────────────────────────────────────

class TestCrossBenchGate:
    def _blocked_pair(self, clock, tmp_path, **cfg):
        """Healthy T5 adopted first; then a 12 ms-biased T6 appears
        (the incident shape, with sigmas tight enough to resolve it:
        bound = 5*sqrt((0.1ms)^2+(0.15ms)^2) ≈ 0.9 ms < 12 ms; the T6
        sigma also sits inside sigma_regression_margin so these tests
        isolate the CROSS-BENCH gate from the precision hold)."""
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1.5e5,
                       bench_error_s=0.012, available=False)
        judge = make_judge(clock, tmp_path, [t5, t6], **cfg)
        register_healthy(judge, clock)
        judge.tick()                       # T5 adopted (only reading)
        assert tier_of(judge) == "T5"
        t6.available = True
        return judge, t5, t6

    def test_gate_blocks_biased_upper_bench(self, tmp_path):
        clock = FakeClock()
        judge, t5, t6 = self._blocked_pair(clock, tmp_path)
        tick_n(judge, clock, 6)            # >> upgrade_polls
        assert tier_of(judge) == "T5"      # never advanced
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["judge"]["tier"] == "T5"
        c = snap["cross_bench_conflict"]
        assert c["upper"] == "T6" and c["lower"] == "T5"
        assert c["delta_ns"] == pytest.approx(12e6, rel=1e-3)
        assert "since_utc" in c and c["since_utc"].endswith("Z")
        # contract_v07 additive slot mirrors it
        assert snap["contract_v07"]["cross_bench_conflict"]["upper"] == "T6"

    def test_shadow_residual_published_for_rejected_bench(self, tmp_path):
        clock = FakeClock()
        judge, t5, t6 = self._blocked_pair(clock, tmp_path)
        tick_n(judge, clock, 4)
        snap = read_json(tmp_path / "offset_judge.json")
        sh = snap["shadow_residuals"]["T6"]
        assert sh["shadow_residual_ns"] == pytest.approx(12e6, rel=1e-3)
        assert sh["vs_tier"] == "T5"
        assert sh["sigma_ns"] == pytest.approx(1.5e5, rel=1e-3)

    def test_gate_admits_agreeing_bench(self, tmp_path):
        clock = FakeClock()
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1.5e5, available=False)
        judge = make_judge(clock, tmp_path, [t5, t6])
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T5"
        t6.available = True                # agreeing candidate
        tick_n(judge, clock, 2)
        assert tier_of(judge) == "T5"      # hysteresis still applies
        tick_n(judge, clock, 1)            # 3rd consecutive good poll
        assert tier_of(judge) == "T6"
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is None
        # Shadow channel now measures the non-adopted T5 vs adopted T6.
        assert snap["shadow_residuals"]["T5"]["vs_tier"] == "T6"

    def test_recovery_restarts_advance_window_cleanly(self, tmp_path):
        clock = FakeClock()
        judge, t5, t6 = self._blocked_pair(clock, tmp_path)
        tick_n(judge, clock, 5)            # blocked for a while
        assert tier_of(judge) == "T5"
        t6.bench_error_s = 0.0             # bias clears
        tick_n(judge, clock, 1)            # agreeing poll 1 (window restart)
        assert tier_of(judge) == "T5"
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is None   # cleared on agreement
        tick_n(judge, clock, 1)            # poll 2
        assert tier_of(judge) == "T5"
        tick_n(judge, clock, 1)            # poll 3 -> full fresh window
        assert tier_of(judge) == "T6"

    def test_single_bench_unaffected(self, tmp_path):
        clock = FakeClock()
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, [t4])
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 2.0), 5000, SR)  # +2 s wedge
        judge.tick()                       # adopted on the first tick
        v = judge.offset_for(KEY, 5000)
        assert v is not None and v.tier == "T4"
        assert v.offset_ns == pytest.approx(2e9, rel=1e-3)
        tick_n(judge, clock, 9)
        assert judge.offset_for(KEY, 5000).in_violation is True
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is None
        assert snap["shadow_residuals"] == {}

    def test_bootstrap_adopts_highest_consistent_tier(self, tmp_path):
        """Judge (re)start with everything answering at once: the
        chain gate keeps the biased top bench out at tick 1 — the
        restart hole the incident would otherwise re-open."""
        clock = FakeClock()
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1e6, bench_error_s=0.012)
        judge = make_judge(clock, tmp_path, [t4, t5, t6])
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T5"      # highest CONSISTENT tier
        snap = read_json(tmp_path / "offset_judge.json")
        c = snap["cross_bench_conflict"]
        assert c["upper"] == "T6" and c["lower"] == "T5"

    def test_reference_loss_does_not_admit_blocked_candidate(self, tmp_path):
        """T5 (the reference) dies while a biased T6 is blocked: the
        gate re-references the next lower tier (T4) instead of letting
        the candidate through on the degrade path."""
        clock = FakeClock()
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1e6,
                       bench_error_s=0.012, available=False)
        judge = make_judge(clock, tmp_path, [t4, t5, t6])
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T5"
        t6.available = True
        tick_n(judge, clock, 4)
        assert tier_of(judge) == "T5"      # blocked, as above
        t5.available = False               # reference lost
        tick_n(judge, clock, 1)
        assert tier_of(judge) == "T4"      # NOT the biased T6
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"]["lower"] == "T4"

    def test_degrade_on_loss_unchanged_and_immediate(self, tmp_path):
        clock = FakeClock()
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1e6)   # agreeing
        judge = make_judge(clock, tmp_path, [t5, t6])
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T6"      # consistent chain, adopted
        t6.available = False
        tick_n(judge, clock, 1)
        assert tier_of(judge) == "T5"      # immediate, no hysteresis

    def test_conflict_critical_is_rate_limited(self, tmp_path, caplog):
        clock = FakeClock()
        judge, t5, t6 = self._blocked_pair(clock, tmp_path)
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            tick_n(judge, clock, 5)        # 50 s < 60 s log interval
        msgs = [r.message for r in caplog.records
                if "CROSS-BENCH CONFLICT" in r.message]
        assert len(msgs) == 1              # rate-limited to one
        assert "T6" in msgs[0] and "T5" in msgs[0]
        assert "delta_ns" in msgs[0]
        caplog.clear()
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            tick_n(judge, clock, 7)        # crosses the 60 s interval
        again = [r.message for r in caplog.records
                 if "CROSS-BENCH CONFLICT" in r.message]
        assert len(again) >= 1             # re-asserted after interval

    def test_cross_bench_k_flows_from_config(self, tmp_path):
        clock = FakeClock()
        # Default mirrors k's config path.
        assert make_judge(clock, tmp_path, []).cross_bench_k == 5.0
        # A huge k_x turns the gate off: the 12 ms bias fits the bound
        # and the chain adopts the biased T6 at bootstrap.
        t5 = FakeBench(clock, tier="T5", sigma_ns=1e5)
        t6 = FakeBench(clock, tier="T6", sigma_ns=1e6, bench_error_s=0.012)
        judge = make_judge(clock, tmp_path, [t5, t6], cross_bench_k=1e6)
        assert judge.cross_bench_k == 1e6
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T6"
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is None


class TestPrecisionNonRegression:
    """Sigma non-regression clause: a voluntary upgrade must not
    materially regress the judge's precision (the AC0G-B4 deploy
    shape: T4 chrony at ~200 µs vs the stream-arrival T5 pairing's
    25 ms floor — tier-rank adoption would widen the k*sigma
    violation bound ~100x and stop flagging ms-scale anomalies)."""

    def _t4_incumbent_wide_t5(self, clock, tmp_path, t5_sigma=25e6, **cfg):
        t4 = FakeBench(clock, tier="T4", sigma_ns=2e5)     # ~200 us chrony
        t5 = FakeBench(clock, tier="T5", sigma_ns=t5_sigma,
                       available=False)                    # agreeing, wide
        judge = make_judge(clock, tmp_path, [t4, t5], **cfg)
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T4"
        t5.available = True
        return judge, t4, t5

    def test_wide_sigma_candidate_refused_over_tight_incumbent(self, tmp_path):
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(clock, tmp_path)
        tick_n(judge, clock, 6)            # >> upgrade_polls
        assert tier_of(judge) == "T4"      # precision hold, not adopted
        snap = read_json(tmp_path / "offset_judge.json")
        h = snap["precision_hold"]
        assert h["candidate"] == "T5" and h["incumbent"] == "T4"
        assert h["sigma_candidate_ns"] == pytest.approx(25e6, rel=1e-3)
        assert h["sigma_incumbent_ns"] == pytest.approx(2e5, rel=1e-3)
        # A hold is not a cross-bench fault; the refused bench stays
        # under shadow measurement.
        assert snap["cross_bench_conflict"] is None
        assert snap["shadow_residuals"]["T5"]["vs_tier"] == "T4"

    def test_hold_warning_rate_limited_and_not_critical(self, tmp_path, caplog):
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(clock, tmp_path)
        with caplog.at_level(logging.WARNING, logger=oj.__name__):
            tick_n(judge, clock, 5)        # 50 s < 60 s log interval
        holds = [r for r in caplog.records
                 if "PRECISION HOLD" in r.message]
        assert len(holds) == 1             # rate-limited to one
        assert holds[0].levelno == logging.WARNING   # a hold, not a fault
        assert "T5" in holds[0].message and "T4" in holds[0].message
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=oj.__name__):
            tick_n(judge, clock, 7)        # crosses the 60 s interval
        assert [r for r in caplog.records if "PRECISION HOLD" in r.message]

    def test_adopted_when_sigma_within_margin(self, tmp_path):
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(
            clock, tmp_path, t5_sigma=3.5e5)   # 1.75x <= margin 2.0
        tick_n(judge, clock, 2)
        assert tier_of(judge) == "T4"      # hysteresis still applies
        tick_n(judge, clock, 1)
        assert tier_of(judge) == "T5"
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["precision_hold"] is None

    def test_adopted_regardless_on_incumbent_loss(self, tmp_path):
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(clock, tmp_path)
        tick_n(judge, clock, 4)
        assert tier_of(judge) == "T4"      # held while T4 answers
        t4.available = False               # incumbent dies
        tick_n(judge, clock, 1)
        assert tier_of(judge) == "T5"      # wide honest bench beats none
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["precision_hold"] is None   # hold released on adoption

    def test_margin_configurable(self, tmp_path):
        clock = FakeClock()
        assert make_judge(clock, tmp_path, []).sigma_regression_margin == 2.0
        judge, t4, t5 = self._t4_incumbent_wide_t5(
            clock, tmp_path, sigma_regression_margin=1000.0)
        assert judge.sigma_regression_margin == 1000.0
        tick_n(judge, clock, 3)            # 25e6 <= 2e5 * 1000 -> admitted
        assert tier_of(judge) == "T5"

    def test_hold_released_when_candidate_sigma_tightens(self, tmp_path):
        """A DCD-grounded pairing (or converged bench) tightening its
        sigma lifts the hold; adoption then needs the full window."""
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(clock, tmp_path)
        tick_n(judge, clock, 4)
        assert tier_of(judge) == "T4"
        t5.sigma_ns = 3e5                  # tightens to 1.5x incumbent
        tick_n(judge, clock, 1)
        assert tier_of(judge) == "T4"      # window restarts
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["precision_hold"] is None
        tick_n(judge, clock, 2)
        assert tier_of(judge) == "T5"


# ────────────────────────────────────────────────────────────────────
# T5 bench decoupling from the T6 stream (feature 2)
# ────────────────────────────────────────────────────────────────────


    def test_precision_hold_does_not_go_stale_when_the_cross_gate_takes_over(
            self, tmp_path):
        """AC0G-B4 2026-08-15: the hold kept publishing
        sigma_candidate_ns = 25 ms long after the candidate's measured
        sigma had fallen to 0.98 ms.

        _sigma_gate_ok_locked only runs when the cross gate PASSES, so
        once a cross-bench conflict starts blocking, the hold is never
        re-evaluated and freezes whatever it last held.  A stale number
        in the published snapshot is worse than no number: it was read
        as the candidate's current precision.
        """
        clock = FakeClock()
        judge, t4, t5 = self._t4_incumbent_wide_t5(clock, tmp_path)
        tick_n(judge, clock, 6)
        assert read_json(tmp_path / "offset_judge.json")[
            "precision_hold"]["sigma_candidate_ns"] == pytest.approx(25e6,
                                                                     rel=1e-3)

        # The candidate's sigma is measured honestly now, and that same
        # honesty exposes a real disagreement — so the CROSS gate blocks.
        t5.sigma_ns = 1e5
        t5.bench_error_s = 0.012
        tick_n(judge, clock, 3)

        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is not None
        # The operative reason is the conflict; the hold must not linger
        # advertising a sigma the candidate no longer has.
        assert snap["precision_hold"] is None

class FakeReading:
    """Duck-typed Lb1421Reading (mirrors the P2 suite)."""

    def __init__(self, pps_utc_sec, mono, valid_fix=True):
        self.pps_utc_sec = int(pps_utc_sec)
        self.host_monotonic_at_read = float(mono)
        self.valid_fix = bool(valid_fix)


class FakeProbe:
    def __init__(self, clock: FakeClock, available: bool = True):
        self.clock = clock
        self.available = available

    def get_latest(self):
        if not self.available:
            return None
        return FakeReading(int(self.clock.wall) - 2, self.clock.mono_v)


def make_core(clock: FakeClock) -> CoreRecorderV2:
    """__new__-bypassed CoreRecorderV2 with only the T5 provider state
    (the established unit-test pattern for this class)."""
    cr = CoreRecorderV2.__new__(CoreRecorderV2)
    cr._lb1421_probe = FakeProbe(clock)
    cr._t5_pairing = None
    cr._t6_channel_info = None
    cr._t5_fallback_pairings = {}
    return cr


def add_fallback(cr, clock: FakeClock, desc: str, sr: int = SR,
                 rtp: int = RTP, arrived: bool = True) -> T5RtpPairing:
    """Register one fallback stream: per-stream pairing + a recorder
    stub carrying listener-refreshed ChannelInfo."""
    pairing = T5RtpPairing(time_fn=clock.time, mono_fn=clock.mono,
                           source=f"stream:{desc}")
    if arrived:
        pairing.note_arrival(rtp)
    rec = types.SimpleNamespace(
        channel_info=types.SimpleNamespace(
            gps_time=unix_to_gps_ns(clock.wall), rtp_timesnap=rtp,
            sample_rate=sr),
        config=types.SimpleNamespace(sample_rate=sr),
    )
    cr._t5_fallback_pairings[desc] = (pairing, rec)
    return pairing


class TestT5BenchDecoupling:
    def test_fallback_grounds_t5_bench_when_t6_absent(self, clock=None):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        product = cr._t5_bench_state()
        assert product is not None
        assert product.source == "stream:WWV_10_MHz"
        # Honest pairing: healthy mapping reads ~0 offset.
        assert abs(product.anchor_offset_ns) < 1e3
        assert product.sigma_ns >= T5RtpPairing.SIGMA_FLOOR_NS

    def test_bench_lights_t5_from_fallback(self):
        """lb1421_enabled alone (T6 stream off) lights the T5 bench."""
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        bench = LbeT5Bench(provider=cr._t5_bench_state, mono_fn=clock.mono)
        r = bench.poll()
        assert r is not None and r.tier == "T5"
        assert r.detail["source"] == "stream:WWV_10_MHz"
        assert r.utc == pytest.approx(clock.wall, abs=1e-6)

    def test_t6_stream_preferred_when_present(self):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        t6_pairing = T5RtpPairing(time_fn=clock.time, mono_fn=clock.mono,
                                  source="t6")
        t6_pairing.note_arrival(RTP)
        cr._t5_pairing = t6_pairing
        cr._t6_channel_info = types.SimpleNamespace(
            gps_time=unix_to_gps_ns(clock.wall), rtp_timesnap=RTP,
            sample_rate=16000)
        product = cr._t5_bench_state()
        assert product is not None
        assert product.source == "t6"

    def test_fallback_prefers_highest_sample_rate(self):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WSPR_slow", sr=12000)
        add_fallback(cr, clock, "WWV_25_MHz", sr=24000)
        product = cr._t5_bench_state()
        assert product is not None
        assert product.source == "stream:WWV_25_MHz"

    def test_fallback_skips_stream_without_arrival(self):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "DEAD_fast", sr=48000, arrived=False)
        add_fallback(cr, clock, "WWV_10_MHz", sr=24000)
        product = cr._t5_bench_state()
        assert product is not None
        assert product.source == "stream:WWV_10_MHz"

    def test_no_probe_reading_no_product(self):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        cr._lb1421_probe.available = False
        assert cr._t5_bench_state() is None
        cr._lb1421_probe = None
        assert cr._t5_bench_state() is None

    def test_wire_tap_feeds_pairing_like_t6_does(self):
        """_wire_t5_fallback_arrival taps the recorder with the same
        (samples, quality) callback shape the T6 arrival note uses."""
        clock = FakeClock()
        cr = make_core(clock)

        class FakeRecorder:
            def __init__(self):
                self.taps = []

            def add_tap(self, cb):
                self.taps.append(cb)

        rec = FakeRecorder()
        cr._wire_t5_fallback_arrival("CHU_7_MHz", rec)
        assert "CHU_7_MHz" in cr._t5_fallback_pairings
        pairing = cr._t5_fallback_pairings["CHU_7_MHz"][0]
        assert pairing.source == "stream:CHU_7_MHz"
        assert len(rec.taps) == 1
        rec.taps[0](None, types.SimpleNamespace(last_rtp_timestamp=1234))
        assert pairing.latest_arrival[0] == 1234
        # quality without an RTP timestamp is a no-op, not a crash
        rec.taps[0](None, types.SimpleNamespace(last_rtp_timestamp=None))
        assert pairing.latest_arrival[0] == 1234

    def test_pairing_product_source_key_honesty(self):
        clock = FakeClock()
        reading = FakeReading(int(clock.wall) - 2, clock.mono_v)
        # Default identity: the T6 stream.
        p_default = T5RtpPairing(time_fn=clock.time, mono_fn=clock.mono)
        p_default.note_arrival(RTP)
        prod = p_default.compute(reading, unix_to_gps_ns(clock.wall), RTP, SR)
        assert prod is not None and prod.source == "t6"
        # Named grounding stream carries through compute().
        p_named = T5RtpPairing(time_fn=clock.time, mono_fn=clock.mono,
                               source="stream:WWV_5_MHz")
        p_named.note_arrival(RTP)
        prod2 = p_named.compute(reading, unix_to_gps_ns(clock.wall), RTP, SR)
        assert prod2 is not None and prod2.source == "stream:WWV_5_MHz"


class TestJudgeTierViaFallback:
    def test_judge_reaches_t5_when_agreeing_with_t4(self, tmp_path):
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, [t4])
        judge.add_bench(LbeT5Bench(provider=cr._t5_bench_state,
                                   mono_fn=clock.mono))
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T5"      # fallback-grounded T5 judged
        snap = read_json(tmp_path / "offset_judge.json")
        assert snap["cross_bench_conflict"] is None
        assert snap["judge"]["bench_detail"]["source"] == "stream:WWV_10_MHz"

    def test_judge_stays_t4_when_fallback_t5_disagrees(self, tmp_path):
        """The gate applies to the fallback-grounded T5 exactly as to
        any candidate: a 0.2 s disagreement (> 5*sqrt(sigma^2 sums)
        with the T5 latency floor of 25 ms) blocks advancement."""
        clock = FakeClock()
        cr = make_core(clock)
        add_fallback(cr, clock, "WWV_10_MHz")
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5, bench_error_s=0.2)
        judge = make_judge(clock, tmp_path, [t4])
        judge.add_bench(LbeT5Bench(provider=cr._t5_bench_state,
                                   mono_fn=clock.mono))
        register_healthy(judge, clock)
        judge.tick()
        assert tier_of(judge) == "T4"
        snap = read_json(tmp_path / "offset_judge.json")
        c = snap["cross_bench_conflict"]
        assert c["upper"] == "T5" and c["lower"] == "T4"
        assert c["delta_ns"] == pytest.approx(-0.2e9, rel=1e-2)
        assert snap["shadow_residuals"]["T5"]["shadow_residual_ns"] == \
            pytest.approx(-0.2e9, rel=1e-2)
