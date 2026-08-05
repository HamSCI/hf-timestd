"""Offset Judge P3 tests (docs/OFFSET-JUDGE-SPEC-2026-08-05.md §10 P3).

The frequency loop: rate is MEASURED and RECORDED — never corrected
into the samples or labels (spec §11, audit G7).

Covers:
  * regress_rate_ppm math (known slope ± noise → ppm with honest
    standard-error sigma; degenerate series refused);
  * judge offset-slope estimator: synthetic bench drift of known ppm
    is recovered; null below the minimum span; segment fracture
    restarts the estimator;
  * T6ResidualRateEstimator: residual-walk differentiation, ±0.5 s
    wrap unwrapping, step/gap window restarts, duplicate-edge refusal;
  * combination: inverse-variance blend, source labeling
    (offset-slope | t6-residual | combined);
  * publication: rate fields in offset_judge.json and in the verdict;
    sidecar timing.rate_ppm round-trip (filled + null cases);
  * sustained-|rate| CRITICAL alarm naming the channel and both
    estimates; threshold and sustain-window gating;
  * GPSDO discipline honesty: locked / holdover / unlocked / absent
    from schema-v1 health files; surfaced in offset_judge.json as
    metadata only.

Runnable with SYSTEM python3 + numpy only (P1 namespace bootstrap).
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _install_namespace() -> None:
    """Register hf_timestd / hf_timestd.core without running __init__.py."""
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


_install_namespace()

oj = importlib.import_module("hf_timestd.core.offset_judge")
gp = importlib.import_module("hf_timestd.core.gpsdo_probe")
baw = importlib.import_module("hf_timestd.core.binary_archive_writer")

OffsetJudge = oj.OffsetJudge
OffsetVerdict = oj.OffsetVerdict
BenchReading = oj.BenchReading
RateEstimate = oj.RateEstimate
T6ResidualRateEstimator = oj.T6ResidualRateEstimator
regress_rate_ppm = oj.regress_rate_ppm
combine_rate_estimates = oj.combine_rate_estimates
GpsdoProbe = gp.GpsdoProbe
BinaryArchiveWriter = baw.BinaryArchiveWriter
BinaryArchiveConfig = baw.BinaryArchiveConfig

WALL0 = 1_800_000_000.0
KEY = ("hf-status.local", 0x1234ABCD)
BILLION = 1_000_000_000


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


class DriftingBench:
    """Bench whose truth walks linearly: emulates radiod-clock-vs-bench
    rate disagreement of `drift_ppm` (offset slope = drift_ppm)."""

    def __init__(self, clock: FakeClock, tier: str = "T4",
                 sigma_ns: float = 1e5, drift_ppm: float = 0.0,
                 noise_ns: float = 0.0, seed: int = 7):
        self.clock = clock
        self.tier = tier
        self.sigma_ns = sigma_ns
        self.drift_ppm = drift_ppm
        self.noise_ns = noise_ns
        self.mono0 = clock.mono_v
        self.rng = np.random.default_rng(seed)

    def poll(self):
        elapsed = self.clock.mono_v - self.mono0
        err = self.drift_ppm * 1e-6 * elapsed
        if self.noise_ns:
            err += float(self.rng.normal(0.0, self.noise_ns)) / 1e9
        return BenchReading(
            tier=self.tier,
            utc=self.clock.wall + err,
            sigma_ns=self.sigma_ns,
            mono=self.clock.mono_v,
        )


def make_judge(clock: FakeClock, tmp_path: Path, bench, **cfg) -> OffsetJudge:
    config = {"enabled": True, "tick_seconds": 10.0,
              "gpsdo_enabled": False, **cfg}
    return OffsetJudge(
        config=config,
        benches=[bench],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
    )


def run_ticks(judge: OffsetJudge, clock: FakeClock, n: int,
              dt: float = 10.0) -> None:
    for _ in range(n):
        judge.tick()
        clock.advance(dt)


# ────────────────────────────────────────────────────────────────────
# Regression math
# ────────────────────────────────────────────────────────────────────

class TestRegressRatePpm:
    def test_known_slope_noiseless(self):
        t = np.arange(0.0, 300.0, 10.0)
        y = 5000.0 + 700.0 * t          # 700 ns/s = 0.7 ppm
        ppm, sigma = regress_rate_ppm(t, y)
        assert ppm == pytest.approx(0.7, abs=1e-9)
        assert sigma == pytest.approx(0.0, abs=1e-9)

    def test_known_slope_with_noise_within_sigma(self):
        rng = np.random.default_rng(42)
        t = np.arange(0.0, 600.0, 1.0)
        y = 300.0 * t + rng.normal(0.0, 500.0, len(t))  # 0.3 ppm + 500 ns rms
        ppm, sigma = regress_rate_ppm(t, y)
        assert sigma > 0.0
        assert abs(ppm - 0.3) < 5.0 * sigma
        assert abs(ppm - 0.3) < 0.05      # and absolutely tight at n=600

    def test_degenerate_refused(self):
        assert regress_rate_ppm(np.array([0.0, 1.0]), np.array([0.0, 1.0])) is None
        assert regress_rate_ppm(np.zeros(5), np.arange(5.0)) is None  # zero spread
        assert regress_rate_ppm(np.arange(4.0), np.arange(3.0)) is None


class TestCombine:
    def test_inverse_variance_blend(self):
        a = RateEstimate(1.0, 0.1, 10, 100.0, "offset-slope")
        b = RateEstimate(0.0, 0.1, 20, 200.0, "t6-residual")
        c = combine_rate_estimates(a, b)
        assert c.source == "combined"
        assert c.ppm == pytest.approx(0.5)
        assert c.sigma_ppm == pytest.approx(0.1 / np.sqrt(2.0))
        assert c.n == 30 and c.span_s == 200.0

    def test_unequal_sigmas_weight_the_tighter(self):
        a = RateEstimate(1.0, 0.01, 10, 100.0, "offset-slope")
        b = RateEstimate(0.0, 0.1, 10, 100.0, "t6-residual")
        c = combine_rate_estimates(a, b)
        assert c.ppm == pytest.approx(1.0 * 100 / 101, rel=1e-6)

    def test_single_estimate_passthrough(self):
        a = RateEstimate(0.4, 0.02, 10, 100.0, "offset-slope")
        assert combine_rate_estimates(a, None) is a
        assert combine_rate_estimates(None, a) is a
        assert combine_rate_estimates(None, None) is None

    def test_zero_sigma_never_infinite_weight(self):
        a = RateEstimate(1.0, 0.0, 10, 100.0, "offset-slope")
        b = RateEstimate(0.0, 0.0, 10, 100.0, "t6-residual")
        c = combine_rate_estimates(a, b)
        assert c.ppm == pytest.approx(0.5)
        assert np.isfinite(c.sigma_ppm)


# ────────────────────────────────────────────────────────────────────
# Judge offset-slope estimator (spec P3 observable #1)
# ────────────────────────────────────────────────────────────────────

class TestOffsetSlope:
    def test_known_drift_recovered(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 30)
        v = judge.offset_for(KEY, 5000)
        assert v.rate_ppm == pytest.approx(0.5, abs=0.05)
        assert v.rate_source == "offset-slope"
        assert v.rate_sigma_ppm is not None and v.rate_sigma_ppm < 0.05

    def test_noisy_drift_recovered_within_sigma(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5, noise_ns=2000.0)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 60)
        v = judge.offset_for(KEY, 5000)
        assert v.rate_ppm == pytest.approx(0.5, abs=0.05)

    def test_null_below_minimum_span(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=120.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 5)          # 40 s of history < 120 s span
        v = judge.offset_for(KEY, 5000)
        assert v is not None
        assert v.rate_ppm is None
        assert v.rate_sigma_ppm is None and v.rate_source is None

    def test_fracture_resets_estimator(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 20)
        assert judge.offset_for(KEY, 5000).rate_ppm is not None
        judge.mark_fracture(KEY, "usb_sample_loss")
        judge.tick()                        # refresh caches post-fracture
        v = judge.offset_for(KEY, 5000)
        assert v is None or v.rate_ppm is None  # fresh segment, no span yet
        # ...and it recovers within the new segment:
        run_ticks(judge, clock, 20)
        v = judge.offset_for(KEY, 5000)
        assert v.segment_id == 2
        assert v.rate_ppm == pytest.approx(0.5, abs=0.05)

    def test_zero_drift_reads_zero(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.0)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 30)
        v = judge.offset_for(KEY, 5000)
        assert v.rate_ppm == pytest.approx(0.0, abs=0.01)


# ────────────────────────────────────────────────────────────────────
# T6 residual-walk estimator (spec P3 observable #2)
# ────────────────────────────────────────────────────────────────────

def wrap_residual(x_ns: float) -> float:
    """Fold into the ±0.5 s band the SHM site's rounding produces."""
    return ((x_ns + BILLION / 2) % BILLION) - BILLION / 2


class TestT6ResidualRate:
    def make(self, **kw):
        kw.setdefault("min_span_s", 60.0)
        kw.setdefault("min_points", 10)
        return T6ResidualRateEstimator(**kw)

    def test_known_walk_recovered(self):
        est = self.make()
        t0 = 1_754_000_000
        for i in range(300):                      # 5 min of 1 Hz edges
            est.add_edge(t0 + i, 100.0 + 500.0 * i)   # 0.5 ppm walk
        cur = est.current()
        assert cur is not None
        assert cur.source == "t6-residual"
        assert cur.ppm == pytest.approx(0.5, abs=1e-6)
        assert cur.n == 300 and cur.span_s == pytest.approx(299.0)

    def test_noisy_walk_recovered_with_sigma(self):
        rng = np.random.default_rng(3)
        est = self.make()
        t0 = 1_754_000_000
        for i in range(300):
            est.add_edge(t0 + i, 1000.0 * i + rng.normal(0, 300.0))  # 1 ppm
        cur = est.current()
        assert cur.sigma_ppm > 0.0
        assert abs(cur.ppm - 1.0) < 5 * cur.sigma_ppm
        assert cur.ppm == pytest.approx(1.0, abs=0.05)

    def test_wrap_boundary_unwrapped(self):
        """Walk crossing +0.5 s: raw residual jumps −1e9; slope survives."""
        est = self.make()
        t0 = 1_754_000_000
        base = BILLION / 2 - 20_000.0             # 20 µs shy of the boundary
        for i in range(300):
            est.add_edge(t0 + i, wrap_residual(base + 1000.0 * i))  # 1 ppm
        cur = est.current()
        assert cur is not None
        assert cur.ppm == pytest.approx(1.0, abs=1e-6)

    def test_step_restarts_window(self):
        est = self.make(min_span_s=30.0)
        t0 = 1_754_000_000
        for i in range(100):
            est.add_edge(t0 + i, 500.0 * i)
        assert est.current() is not None
        resets_before = est.resets
        # 100 µs anchor event at edge 100 (>> STEP_RESET_NS)
        est.add_edge(t0 + 100, 500.0 * 100 + 100_000.0)
        assert est.resets == resets_before + 1
        assert est.current() is None              # fresh window, no span
        for i in range(101, 160):
            est.add_edge(t0 + i, 500.0 * i + 100_000.0)
        cur = est.current()
        assert cur is not None                    # recovered post-restart
        assert cur.ppm == pytest.approx(0.5, abs=1e-3)
        assert cur.n <= 60                        # old window really gone

    def test_gap_restarts_window(self):
        est = self.make(min_span_s=30.0)
        t0 = 1_754_000_000
        for i in range(100):
            est.add_edge(t0 + i, 0.0)
        assert est.current() is not None
        est.add_edge(t0 + 100 + 40, 0.0)          # 40 s stall > MAX_GAP_S
        assert est.current() is None

    def test_duplicate_and_backward_edges_ignored(self):
        est = self.make()
        t0 = 1_754_000_000
        est.add_edge(t0, 10.0)
        est.add_edge(t0, 11.0)                    # duplicate second
        est.add_edge(t0 - 1, 12.0)                # out of order
        assert len(est._window) == 1

    def test_reset_clears(self):
        est = self.make(min_span_s=30.0)
        t0 = 1_754_000_000
        for i in range(100):
            est.add_edge(t0 + i, 500.0 * i)
        assert est.current() is not None
        est.reset("anchor recapture")
        assert est.current() is None

    def test_below_min_span_null(self):
        est = self.make(min_span_s=120.0)
        t0 = 1_754_000_000
        for i in range(60):
            est.add_edge(t0 + i, 500.0 * i)       # 59 s span < 120
        assert est.current() is None


# ────────────────────────────────────────────────────────────────────
# Cross-check combination inside the judge
# ────────────────────────────────────────────────────────────────────

class TestJudgeCombination:
    def test_both_observables_combined(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.set_t6_rate_provider(
            lambda: RateEstimate(0.48, 0.02, 200, 199.0, "t6-residual"))
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 30)
        v = judge.offset_for(KEY, 5000)
        assert v.rate_source == "combined"
        assert v.rate_ppm == pytest.approx(0.5, abs=0.05)

    def test_t6_only_before_slope_span(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=300.0)
        judge.set_t6_rate_provider(
            lambda: RateEstimate(0.51, 0.02, 200, 199.0, "t6-residual"))
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 3)                # slope far below span
        v = judge.offset_for(KEY, 5000)
        assert v.rate_source == "t6-residual"
        assert v.rate_ppm == pytest.approx(0.51)

    def test_provider_failure_never_kills_tick(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.0)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)

        def bad_provider():
            raise RuntimeError("estimator exploded")

        judge.set_t6_rate_provider(bad_provider)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 12)               # must not raise
        v = judge.offset_for(KEY, 5000)
        assert v.rate_source == "offset-slope"    # falls back to slope alone


# ────────────────────────────────────────────────────────────────────
# Publication: offset_judge.json + sidecar round-trip (spec §8)
# ────────────────────────────────────────────────────────────────────

class TestPublicationP3:
    def test_json_rate_fields(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.set_t6_rate_provider(
            lambda: RateEstimate(0.52, 0.03, 150, 149.0, "t6-residual"))
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 30)
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        src = data["sources"][f"{KEY[0]}/{KEY[1]:08x}"]
        assert src["rate_ppm"] == pytest.approx(0.5, abs=0.06)
        assert src["rate_sigma_ppm"] is not None
        assert src["rate_source"] == "combined"
        assert src["rate_alarm"] is False
        assert src["d_offset_dt_ppm"] == pytest.approx(0.5, abs=0.06)
        t6r = data["t6_residual_rate"]
        assert t6r["ppm"] == pytest.approx(0.52)
        assert t6r["sigma_ppm"] == pytest.approx(0.03)
        assert data["gpsdo_discipline"] == "absent"   # gpsdo disabled in tests

    def test_json_rate_null_below_span(self, tmp_path):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=600.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        run_ticks(judge, clock, 5)
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        src = data["sources"][f"{KEY[0]}/{KEY[1]:08x}"]
        assert src["rate_ppm"] is None
        assert src["rate_source"] is None
        assert data["t6_residual_rate"] is None

    def _make_writer(self, tmp_path, judge):
        cfg = BinaryArchiveConfig(
            channel_name="TEST_10_MHz",
            frequency_hz=10e6,
            sample_rate=100,
            output_dir=tmp_path / "raw",
            compression="none",
            file_duration_sec=2,
        )
        return BinaryArchiveWriter(cfg, offset_judge=judge, source_key=KEY)

    def _drive(self, w):
        now = time.time()
        gps_ns = unix_to_gps_ns(now)
        snap = 10_000
        w.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=snap)
        for i in range(6):
            w.write_samples(np.ones(100, dtype=np.complex64),
                            rtp_timestamp=(snap + i * 100) & 0xFFFFFFFF)
        w.flush()
        return [json.loads(p.read_text())
                for p in sorted(w.archive_dir.rglob("*.json"))]

    class _StubJudge:
        def __init__(self, verdict):
            self.verdict = verdict

        def register_radiod_pair(self, *a, **k):
            pass

        def offset_for(self, key, rtp):
            return self.verdict

        def flag_anchor_fault(self, *a, **k):
            pass

    def test_sidecar_rate_ppm_roundtrip(self, tmp_path):
        verdict = OffsetVerdict(
            offset_ns=1000.0, sigma_ns=1e5, tier="T4",
            judge_age_s=2.0, segment_id=1, in_violation=False,
            rate_ppm=0.42, rate_sigma_ppm=0.05, rate_source="combined",
        )
        w = self._make_writer(tmp_path, self._StubJudge(verdict))
        try:
            sidecars = self._drive(w)
            assert sidecars
            for m in sidecars:
                assert m["timing"]["rate_ppm"] == pytest.approx(0.42)
        finally:
            w.close()

    def test_sidecar_rate_ppm_null_when_unmeasured(self, tmp_path):
        verdict = OffsetVerdict(
            offset_ns=1000.0, sigma_ns=1e5, tier="T4",
            judge_age_s=2.0, segment_id=1, in_violation=False,
        )
        w = self._make_writer(tmp_path, self._StubJudge(verdict))
        try:
            sidecars = self._drive(w)
            assert sidecars
            for m in sidecars:
                assert m["timing"]["rate_ppm"] is None
        finally:
            w.close()


# ────────────────────────────────────────────────────────────────────
# Rate alarm (P3: alarm only — no correction, no escalation)
# ────────────────────────────────────────────────────────────────────

class TestRateAlarm:
    def test_sustained_excess_raises_critical_naming_both(self, tmp_path, caplog):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=3.0)   # way past 1.0 ppm
        judge = make_judge(clock, tmp_path, bench,
                           rate_min_span_s=60.0, rate_sustain_window_s=60.0)
        judge.set_t6_rate_provider(
            lambda: RateEstimate(2.9, 0.05, 300, 299.0, "t6-residual"))
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            run_ticks(judge, clock, 30)
        msgs = [r.message for r in caplog.records
                if "RATE VIOLATION" in r.message]
        assert msgs, "sustained 3 ppm must raise a CRITICAL"
        assert f"{KEY[0]}/{KEY[1]:08x}" in msgs[0]
        assert "offset-slope" in msgs[0] and "t6-residual" in msgs[0]
        assert "+2.9" in msgs[0]                     # both estimates named
        # rate-limited: far fewer messages than ticks
        assert len(msgs) < 10
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        assert data["sources"][f"{KEY[0]}/{KEY[1]:08x}"]["rate_alarm"] is True

    def test_below_threshold_never_alarms(self, tmp_path, caplog):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.8)   # under the 1.0 default
        judge = make_judge(clock, tmp_path, bench, rate_min_span_s=60.0)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            run_ticks(judge, clock, 30)
        assert not [r for r in caplog.records if "RATE VIOLATION" in r.message]
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        assert data["sources"][f"{KEY[0]}/{KEY[1]:08x}"]["rate_alarm"] is False

    def test_not_sustained_no_alarm(self, tmp_path, caplog):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=3.0)
        judge = make_judge(clock, tmp_path, bench,
                           rate_min_span_s=60.0,
                           rate_sustain_window_s=10_000.0)  # never sustained
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            run_ticks(judge, clock, 30)
        assert not [r for r in caplog.records if "RATE VIOLATION" in r.message]

    def test_configurable_threshold(self, tmp_path, caplog):
        clock = FakeClock()
        bench = DriftingBench(clock, drift_ppm=0.5)
        judge = make_judge(clock, tmp_path, bench,
                           rate_min_span_s=60.0, rate_alarm_ppm=0.2)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        with caplog.at_level(logging.CRITICAL, logger=oj.__name__):
            run_ticks(judge, clock, 30)
        assert [r for r in caplog.records if "RATE VIOLATION" in r.message]


# ────────────────────────────────────────────────────────────────────
# GPSDO discipline honesty (metadata only)
# ────────────────────────────────────────────────────────────────────

def write_gpsdo_file(run_dir: Path, serial: str, *, wall: float,
                     age: float = 2.0, pll_locked=True, gps_locked=True,
                     gps_fix="3D", probe_interval_sec: int = 10,
                     schema: str = "v1") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    written = datetime.fromtimestamp(wall - age, tz=timezone.utc)
    (run_dir / f"{serial}.json").write_text(json.dumps({
        "schema": schema,
        "written_utc": written.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "probe_interval_sec": probe_interval_sec,
        "host": "test",
        "device": {"model": "LBE-1421", "pid": "2444", "serial": serial,
                   "hid_path": "x"},
        "governs": [],
        "health": {"pll_locked": pll_locked, "outputs_enabled": True,
                   "gps_locked": gps_locked, "gps_fix": gps_fix},
        "outputs": {},
        "pps_study": {},
        "a_level_hint": "A1" if pll_locked else "A0",
        "a_level_reason": "test",
    }))


class TestGpsdoDiscipline:
    def probe(self, tmp_path, wall):
        return GpsdoProbe(run_dir=tmp_path / "gpsdo", now_fn=lambda: wall)

    def test_locked(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0)
        state, detail = self.probe(tmp_path, WALL0).discipline()
        assert state == "locked"
        assert detail[0]["serial"] == "S1"

    def test_holdover_when_gps_lost(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0,
                         gps_locked=False, gps_fix="no_fix")
        state, _ = self.probe(tmp_path, WALL0).discipline()
        assert state == "holdover"

    def test_locked_via_fix_when_no_hid_bit(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0,
                         gps_locked=None, gps_fix="3D")
        state, _ = self.probe(tmp_path, WALL0).discipline()
        assert state == "locked"

    def test_unlocked_pll_down(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0,
                         pll_locked=False)
        state, _ = self.probe(tmp_path, WALL0).discipline()
        assert state == "unlocked"

    def test_stale_file_is_absent(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0, age=300.0)
        state, detail = self.probe(tmp_path, WALL0).discipline()
        assert state == "absent"
        assert detail[0]["state"] == "absent"

    def test_no_dir_is_absent(self, tmp_path):
        state, detail = self.probe(tmp_path, WALL0).discipline()
        assert state == "absent" and detail == []

    def test_best_state_wins_across_devices(self, tmp_path):
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=WALL0,
                         pll_locked=False)
        write_gpsdo_file(tmp_path / "gpsdo", "S2", wall=WALL0)
        state, detail = self.probe(tmp_path, WALL0).discipline()
        assert state == "locked"
        assert len(detail) == 2

    def test_judge_surfaces_discipline_without_gating(self, tmp_path):
        """gpsdo_discipline in offset_judge.json is metadata only —
        verdicts stay identical whatever the GPSDO says."""
        clock = FakeClock()
        write_gpsdo_file(tmp_path / "gpsdo", "S1", wall=clock.wall,
                         gps_locked=False, gps_fix="no_fix")   # holdover
        bench = DriftingBench(clock, drift_ppm=0.0)
        judge = make_judge(clock, tmp_path, bench,
                           gpsdo_enabled=True,
                           gpsdo_run_dir=str(tmp_path / "gpsdo"))
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        judge.tick()
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        assert data["gpsdo_discipline"] == "holdover"
        assert data["gpsdo_detail"][0]["serial"] == "S1"
        # No gating: verdict exists and is healthy regardless.
        v = judge.offset_for(KEY, 5000)
        assert v is not None and not v.in_violation


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
