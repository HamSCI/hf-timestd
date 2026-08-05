"""Offset Judge P1 tests (docs/OFFSET-JUDGE-SPEC-2026-08-05.md).

Covers:
  * offset math — co-located source reads ~0; a wedged radiod pair
    (the AC0G-B4 1203 s incident) is recovered in the labels;
  * filtering: a step beyond the plausibility band opens a new segment
    (radiod_pair_step / offset_step / rtp_wrap / mark_fracture);
  * violation k*sigma logic with the 60 s sustain window;
  * bench parsing (chrony T4/T2 classification, fusion T3);
  * split-detector classification (synthetic BACKLOG vs ANCHOR FAULT);
  * sidecar "timing" block round-trip through resolve_buffer_timing
    (corrected UTC out);
  * judge-absent behavioral equivalence of BinaryArchiveWriter.

Runnable with SYSTEM python3 + numpy only (no venv, no ka9q, no toml):
the bootstrap below installs the hf_timestd package namespaces without
executing hf_timestd/__init__.py (which imports the ka9q-dependent
stream subsystem) and stubs `toml` if absent (paths.py imports it at
module level but the functions used here don't need it).
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
import types
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
baw = importlib.import_module("hf_timestd.core.binary_archive_writer")
bt = importlib.import_module("hf_timestd.core.buffer_timing")

OffsetJudge = oj.OffsetJudge
OffsetVerdict = oj.OffsetVerdict
BenchReading = oj.BenchReading
ChronyBench = oj.ChronyBench
FusionBench = oj.FusionBench
BinaryArchiveWriter = baw.BinaryArchiveWriter
BinaryArchiveConfig = baw.BinaryArchiveConfig


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

WALL0 = 1_800_000_000.0  # ~2027-01-15, a plain modern epoch
KEY = ("hf-status.local", 0x1234ABCD)


def unix_to_gps_ns(unix_s: float) -> int:
    """Invert gps_time_ns_to_unix without hardcoding the leap count."""
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


def make_judge(clock: FakeClock, tmp_path: Path, bench: FakeBench,
               **cfg) -> OffsetJudge:
    config = {"enabled": True, "tick_seconds": 10.0, **cfg}
    return OffsetJudge(
        config=config,
        benches=[bench],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
    )


class StubJudge:
    """Judge stand-in for writer tests: fixed verdict, records calls."""

    def __init__(self, verdict=None):
        self.verdict = verdict
        self.registered = []
        self.flags = []

    def register_radiod_pair(self, key, gps_ns, snap, rate):
        self.registered.append((key, int(gps_ns), int(snap), int(rate)))

    def offset_for(self, key, rtp):
        return self.verdict

    def flag_anchor_fault(self, key, lag):
        self.flags.append((key, lag))


def make_writer(tmp_path: Path, judge=None, key=None, *,
                sample_rate: int = 100, file_duration_sec: int = 2):
    cfg = BinaryArchiveConfig(
        channel_name="TEST_10_MHz",
        frequency_hz=10e6,
        sample_rate=sample_rate,
        output_dir=tmp_path / "raw",
        compression="none",
        file_duration_sec=file_duration_sec,
    )
    return BinaryArchiveWriter(cfg, offset_judge=judge, source_key=key)


def read_sidecars(writer: BinaryArchiveWriter):
    writer.flush()
    sidecars = sorted(writer.archive_dir.rglob("*.json"))
    return [json.loads(p.read_text()) for p in sidecars]


# ────────────────────────────────────────────────────────────────────
# Offset math
# ────────────────────────────────────────────────────────────────────

class TestOffsetMath:
    def test_colocated_offset_near_zero(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, bench)
        # radiod pair agrees with truth (co-located GPS)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        for _ in range(5):
            judge.tick()
            clock.advance(10.0)
        v = judge.offset_for(KEY, 5000)
        assert v is not None
        assert abs(v.offset_ns) < 1e6  # < 1 ms (numeric noise only)
        assert v.tier == "T4"
        assert not v.in_violation
        assert v.segment_id == 1

    def test_wedged_pair_recovered_1203s(self, tmp_path):
        """The B4 incident: radiod epoch 1203 s wrong -> offset +1203 s."""
        clock = FakeClock()
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, bench)
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 1203.0), 5000, 24000)
        for _ in range(3):
            judge.tick()
            clock.advance(10.0)
        v = judge.offset_for(KEY, 5000)
        assert v is not None
        assert v.offset_ns == pytest.approx(1203.0 * 1e9, abs=1e7)  # ±10 ms

    def test_unregistered_source_returns_none(self, tmp_path):
        clock = FakeClock()
        judge = make_judge(clock, tmp_path, FakeBench(clock))
        judge.tick()
        assert judge.offset_for(("other", 1), 0) is None

    def test_disabled_judge_returns_none(self, tmp_path):
        clock = FakeClock()
        judge = make_judge(clock, tmp_path, FakeBench(clock), enabled=False)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 0, 24000)
        judge.tick()
        assert judge.offset_for(KEY, 0) is None


# ────────────────────────────────────────────────────────────────────
# Filtering / steps / segments (spec §3 + §5)
# ────────────────────────────────────────────────────────────────────

class TestSegments:
    def _converged_judge(self, clock, tmp_path):
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, bench)
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 5000, 24000)
        for _ in range(3):
            judge.tick()
            clock.advance(10.0)
        return judge, bench

    def test_radiod_pair_step_opens_segment(self, tmp_path):
        clock = FakeClock()
        judge, _ = self._converged_judge(clock, tmp_path)
        assert judge.offset_for(KEY, 5000).segment_id == 1
        # radiod re-announces an epoch 1203 s away at a plausible snap
        new_snap = 5000 + 24000 * 40  # 40 s later on the counter
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 1203.0), new_snap, 24000)
        for _ in range(3):
            judge.tick()
            clock.advance(10.0)
        v = judge.offset_for(KEY, new_snap)
        assert v.segment_id == 2
        assert v.offset_ns == pytest.approx(1203.0 * 1e9, abs=1e7)

    def test_pair_dedupe_no_new_segment(self, tmp_path):
        clock = FakeClock()
        judge, _ = self._converged_judge(clock, tmp_path)
        gps_ns = judge._sources[(KEY[0], KEY[1])].gps_time_ns
        judge.register_radiod_pair(KEY, gps_ns, 5000, 24000)  # same pair
        assert judge.offset_for(KEY, 5000).segment_id == 1

    def test_bench_step_beyond_band_opens_segment(self, tmp_path):
        clock = FakeClock()
        judge, bench = self._converged_judge(clock, tmp_path)
        bench.bench_error_s = 5.0  # measured offset steps 5 s and STAYS
        # One outlier tick is rejected by the median filter (status
        # jitter defense); a sustained step must fracture, not smooth.
        judge.tick()
        assert judge.offset_for(KEY, 5000).segment_id == 1  # outlier rejected
        for _ in range(3):
            clock.advance(10.0)
            judge.tick()
        v = judge.offset_for(KEY, 5000)
        assert v.segment_id == 2  # fresh re-estimate, not smoothed
        assert v.offset_ns == pytest.approx(5.0 * 1e9, abs=1e7)

    def test_small_jitter_is_smoothed_not_fractured(self, tmp_path):
        clock = FakeClock()
        judge, bench = self._converged_judge(clock, tmp_path)
        bench.bench_error_s = 20e-6  # 20 µs jitter, inside the band
        for _ in range(3):
            judge.tick()
            clock.advance(10.0)
        v = judge.offset_for(KEY, 5000)
        assert v.segment_id == 1
        assert 0 < v.offset_ns < 20e-6 * 1e9 + 1e3

    def test_rtp_wrap_opens_segment(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock)
        judge = make_judge(clock, tmp_path, bench)
        snap0 = 0xFFFFF000
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), snap0, 24000)
        judge.tick()
        clock.advance(1.0)
        # Counter wrapped; epoch still consistent (elapsed = delta/rate)
        elapsed = ((0x2000 - snap0) & 0xFFFFFFFF) / 24000.0
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 1.0 + elapsed), 0x2000, 24000)
        st = judge._sources[(KEY[0], KEY[1])]
        assert st.segment_id == 2
        assert st.segment_cause == "rtp_wrap"

    def test_mark_fracture(self, tmp_path):
        clock = FakeClock()
        judge, _ = self._converged_judge(clock, tmp_path)
        judge.mark_fracture(KEY, "usb_sample_loss")
        st = judge._sources[(KEY[0], KEY[1])]
        assert st.segment_id == 2
        assert st.segment_cause == "usb_sample_loss"
        assert st.ema_offset_ns is None  # re-estimated fresh


# ────────────────────────────────────────────────────────────────────
# Violation logic (spec §9 step 1)
# ────────────────────────────────────────────────────────────────────

class TestViolation:
    def test_violation_requires_sustain_window(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock, sigma_ns=1e6)  # 1 ms sigma, k=5 -> 5 ms bound
        judge = make_judge(clock, tmp_path, bench, k=5.0, sustain_window_s=60.0)
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 0.05), 0, 24000)  # 50 ms off
        judge.tick()
        assert judge.offset_for(KEY, 0).in_violation is False  # not sustained yet
        for _ in range(7):  # 70 s of sustained excess
            clock.advance(10.0)
            judge.tick()
        assert judge.offset_for(KEY, 0).in_violation is True

    def test_below_k_sigma_never_violates(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock, sigma_ns=1e9)  # 1 s sigma -> 5 s bound
        judge = make_judge(clock, tmp_path, bench, k=5.0)
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 0.05), 0, 24000)  # 50 ms off
        for _ in range(10):
            judge.tick()
            clock.advance(10.0)
        assert judge.offset_for(KEY, 0).in_violation is False

    def test_violation_clears_when_offset_returns(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock, sigma_ns=1e6)
        judge = make_judge(clock, tmp_path, bench, k=5.0, sustain_window_s=60.0)
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 0.05), 0, 24000)
        for _ in range(8):
            judge.tick()
            clock.advance(10.0)
        assert judge.offset_for(KEY, 0).in_violation is True
        # radiod fixed (restart re-announces a correct pair) -> new segment
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 10**6, 24000)
        judge.tick()
        assert judge.offset_for(KEY, 10**6).in_violation is False


# ────────────────────────────────────────────────────────────────────
# Publication file
# ────────────────────────────────────────────────────────────────────

class TestPublication:
    def test_publish_schema(self, tmp_path):
        clock = FakeClock()
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, bench)
        judge.register_radiod_pair(
            KEY, unix_to_gps_ns(clock.wall - 1203.0), 5000, 24000)
        judge.tick()
        data = json.loads((tmp_path / "offset_judge.json").read_text())
        assert data["schema"] == "offset-judge-v1"
        assert data["k"] == 5.0
        assert data["judge"]["tier"] == "T4"
        src = data["sources"][f"{KEY[0]}/{KEY[1]:08x}"]
        assert src["offset_ns"] == pytest.approx(1203e9, abs=1e7)
        assert src["segment_id"] == 1
        assert src["radiod_rtp_timesnap"] == 5000
        assert src["sample_rate"] == 24000
        assert "sigma_ns" in src and "judge_age_s" in src


# ────────────────────────────────────────────────────────────────────
# Benches
# ────────────────────────────────────────────────────────────────────

TRACKING_OUT = """\
Reference ID    : C0A800CB (192.168.0.203)
Stratum         : 2
Ref time (UTC)  : Thu Aug 05 12:00:00 2026
System time     : 0.000001000 seconds fast of NTP time
Last offset     : +0.000000500 seconds
RMS offset      : 0.000002000 seconds
Frequency       : 1.000 ppm slow
Residual freq   : +0.001 ppm
Skew            : 0.010 ppm
Root delay      : 0.000100000 seconds
Root dispersion : 0.000050000 seconds
Update interval : 16.0 seconds
Leap status     : Normal
"""


def _chrony_runner(sources_csv: str):
    def runner(args, **kwargs):
        if "tracking" in args:
            return subprocess.CompletedProcess(args, 0, TRACKING_OUT, "")
        return subprocess.CompletedProcess(args, 0, sources_csv, "")
    return runner


class TestChronyBench:
    def test_lan_selected_source_is_t4(self):
        clock = FakeClock()
        bench = ChronyBench(
            runner=_chrony_runner("^,*,192.168.0.203,1,6,377,10,0.000001,0.000050\n"),
            time_fn=clock.time, mono_fn=clock.mono)
        r = bench.poll()
        assert r is not None
        assert r.tier == "T4"
        # host clock 1 µs FAST -> true UTC = wall - 1 µs
        assert r.utc == pytest.approx(clock.wall - 1e-6, abs=1e-9)
        # sigma from chrony's own budget: disp + delay/2 + rms
        assert r.sigma_ns == pytest.approx(
            (50e-6 + 100e-6 / 2 + 2e-6) * 1e9, rel=1e-6)

    def test_refclock_selected_is_t4(self):
        bench = ChronyBench(
            runner=_chrony_runner("#,*,PPS0,0,4,377,5,0.000000,0.000001\n"))
        assert bench.poll().tier == "T4"

    def test_wan_selected_source_is_t2(self):
        bench = ChronyBench(
            runner=_chrony_runner("^,*,143.107.229.211,2,6,377,10,0.0001,0.001\n"))
        assert bench.poll().tier == "T2"

    def test_chronyc_missing_returns_none(self):
        def runner(args, **kwargs):
            raise FileNotFoundError("chronyc")
        assert ChronyBench(runner=runner).poll() is None


class TestFusionBench:
    def _write_status(self, path: Path, wall: float, *, age: float = 5.0,
                      available: bool = True, n_stations: int = 3,
                      kalman_state: str = "LOCKED"):
        from datetime import datetime, timezone
        pub = datetime.fromtimestamp(wall - age, tz=timezone.utc)
        path.write_text(json.dumps({
            "schema": "v1",
            "utc_published": pub.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "fusion": {
                "available": available,
                "n_stations": n_stations,
                "kalman_state": kalman_state,
                "d_clock_fused_ms": 0.812,
                "uncertainty_ms": 0.94,
            },
        }))

    def test_fresh_locked_status_is_t3(self, tmp_path):
        clock = FakeClock()
        p = tmp_path / "fusion_status.json"
        self._write_status(p, clock.wall)
        bench = FusionBench(status_path=p, time_fn=clock.time, mono_fn=clock.mono)
        r = bench.poll()
        assert r is not None and r.tier == "T3"
        assert r.utc == pytest.approx(clock.wall + 0.812e-3, abs=1e-9)
        assert r.sigma_ns == pytest.approx(0.94e6, rel=1e-9)

    def test_stale_or_unavailable_rejected(self, tmp_path):
        clock = FakeClock()
        p = tmp_path / "fusion_status.json"
        self._write_status(p, clock.wall, age=120.0)
        assert FusionBench(status_path=p, time_fn=clock.time,
                           mono_fn=clock.mono).poll() is None
        self._write_status(p, clock.wall, available=False)
        assert FusionBench(status_path=p, time_fn=clock.time,
                           mono_fn=clock.mono).poll() is None
        self._write_status(p, clock.wall, n_stations=1)
        assert FusionBench(status_path=p, time_fn=clock.time,
                           mono_fn=clock.mono).poll() is None
        self._write_status(p, clock.wall, kalman_state="REACQUIRING")
        assert FusionBench(status_path=p, time_fn=clock.time,
                           mono_fn=clock.mono).poll() is None


class TestTierCascade:
    def test_degrade_immediately_upgrade_with_hysteresis(self, tmp_path):
        clock = FakeClock()
        t4 = FakeBench(clock, tier="T4", sigma_ns=1e5)
        t2 = FakeBench(clock, tier="T2", sigma_ns=5e6)
        judge = OffsetJudge(
            config={"enabled": True},
            benches=[t4, t2],
            publish_path=tmp_path / "offset_judge.json",
            time_fn=clock.time, mono_fn=clock.mono,
        )
        judge.register_radiod_pair(KEY, unix_to_gps_ns(clock.wall), 0, 24000)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T4"
        t4.available = False  # T4 dies -> immediate degrade
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T2"
        t4.available = True  # T4 back -> needs 3 consecutive polls
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T2"
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T2"
        clock.advance(10.0)
        judge.tick()
        assert judge.offset_for(KEY, 0).tier == "T4"


# ────────────────────────────────────────────────────────────────────
# Split detector (spec §6) — synthetic streams
# ────────────────────────────────────────────────────────────────────

class TestSplitDetector:
    def test_backlog_signature_drops(self, tmp_path):
        """lag growing ~1 s/s at half-real-time arrival ⇒ BACKLOG ⇒ drop."""
        w = make_writer(tmp_path, sample_rate=1000)
        try:
            wall = WALL0
            rtp = 0
            dropped = []
            for i in range(20):
                lag = 130.0 + i * 1.0          # growing past the 120 s limit
                rtp += 500                      # 0.5x real-time arrival
                wall += 1.0
                dropped.append(w._split_detector_should_drop(
                    wall, lag, rtp, wall - lag))
            assert dropped[-1] is True
            assert any(dropped)
        finally:
            w.close()

    def test_anchor_fault_signature_keeps_data_and_flags_judge(self, tmp_path):
        """Constant lag at real-time arrival ⇒ ANCHOR FAULT ⇒ never drop."""
        judge = StubJudge()
        w = make_writer(tmp_path, judge=judge, key=KEY, sample_rate=1000)
        try:
            wall = WALL0
            rtp = 0
            for i in range(20):
                lag = 130.0                     # constant — epoch wrong, flow fine
                rtp += 1000                     # 1x real-time
                wall += 1.0
                assert w._split_detector_should_drop(
                    wall, lag, rtp, wall - lag) is False
            assert w.anchor_fault_events > 0
            assert judge.flags and judge.flags[0][0] == KEY
        finally:
            w.close()

    def test_future_dated_labels_are_watched_too(self, tmp_path):
        """Negative lag beyond the limit ⇒ anchor-fault class, kept."""
        w = make_writer(tmp_path, sample_rate=1000)
        try:
            wall = WALL0
            rtp = 0
            for i in range(20):
                lag = -130.0                    # labels in the future
                rtp += 1000
                wall += 1.0
                assert w._split_detector_should_drop(
                    wall, lag, rtp, wall - lag) is False
            assert w.anchor_fault_events > 0
        finally:
            w.close()

    def test_normal_lag_untouched(self, tmp_path):
        w = make_writer(tmp_path, sample_rate=1000)
        try:
            wall = WALL0
            rtp = 0
            for i in range(10):
                rtp += 1000
                wall += 1.0
                assert w._split_detector_should_drop(
                    wall, 0.001, rtp, wall - 0.001) is False
            assert w.anchor_fault_events == 0 and w.stale_drops == 0
        finally:
            w.close()


# ────────────────────────────────────────────────────────────────────
# Writer integration: labels, sidecar, judge-absent equivalence
# ────────────────────────────────────────────────────────────────────

def _drive_writer(writer, *, raw_epoch_offset_s: float = 0.0,
                  sample_rate: int = 100, seconds: int = 6):
    """Seed timing near real now (split detector stays quiet) and write
    `seconds` of samples.  radiod's raw epoch is wrong by
    raw_epoch_offset_s (0 = honest radiod)."""
    now = time.time()
    raw_utc = now - raw_epoch_offset_s
    gps_ns = unix_to_gps_ns(raw_utc)
    snap = 10_000
    writer.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=snap)
    n = sample_rate  # one second per batch
    for i in range(seconds):
        samples = np.ones(n, dtype=np.complex64) * (i + 1)
        writer.write_samples(samples, rtp_timestamp=(snap + i * n) & 0xFFFFFFFF)
    return raw_utc, snap


class TestWriterIntegration:
    def test_judge_absent_behaves_as_today(self, tmp_path):
        w = make_writer(tmp_path)  # no judge at all
        try:
            _drive_writer(w)
            sidecars = read_sidecars(w)
            assert sidecars, "expected at least one flushed chunk"
            for m in sidecars:
                assert "timing" not in m       # legacy chunk, no judge block
                t = bt.resolve_buffer_timing(m, sample_rate=100)
                assert t.source == "rtp_gps"
                assert t.offset_applied_ns == 0.0
                assert t.sample0_utc == pytest.approx(m["minute_boundary"], abs=1.0 / 100)  # 1 sample: boundary-RTP int truncation predates the judge
            assert w.stale_drops == 0
        finally:
            w.close()

    def test_judge_none_verdict_identical_to_no_judge(self, tmp_path):
        """A wired judge with no verdict yet must not change behavior."""
        judge = StubJudge(verdict=None)
        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            _drive_writer(w)
            sidecars = read_sidecars(w)
            assert sidecars
            for m in sidecars:
                assert "timing" not in m
            # ...but the pair DID reach the judge (anchor adoption point)
            assert judge.registered and judge.registered[0][0] == KEY
        finally:
            w.close()

    def test_wedged_radiod_recovered_in_labels_and_sidecar(self, tmp_path):
        """radiod 1203 s wrong + judge verdict ⇒ labels correct, spec §8
        timing block present, resolve_buffer_timing returns corrected UTC."""
        verdict = OffsetVerdict(
            offset_ns=1203.0 * 1e9, sigma_ns=1e5, tier="T4",
            judge_age_s=2.0, segment_id=3, in_violation=True,
        )
        w = make_writer(tmp_path, judge=StubJudge(verdict=verdict), key=KEY)
        try:
            _drive_writer(w, raw_epoch_offset_s=1203.0)
            sidecars = read_sidecars(w)
            assert sidecars, "wedged radiod must still produce chunks (no drops)"
            now = time.time()
            for m in sidecars:
                blk = m["timing"]
                assert blk["offset_ns"] == pytest.approx(1203e9)
                assert blk["offset_sigma_ns"] == pytest.approx(1e5)
                assert blk["judge_tier"] == "T4"
                assert blk["segment_id"] == 3
                assert blk["rate_ppm"] is None
                assert blk["radiod_gps_time_ns"] == m["gps_time_ns"]  # raw pair kept
                assert blk["radiod_rtp_timesnap"] == m["rtp_timesnap"]
                # Round-trip: corrected UTC out of resolve_buffer_timing
                t = bt.resolve_buffer_timing(m, sample_rate=100)
                assert t.offset_applied_ns == pytest.approx(1203e9)
                assert t.judge_tier == "T4"
                assert t.sample0_utc == pytest.approx(m["minute_boundary"], abs=1.0 / 100)  # 1 sample: boundary-RTP int truncation predates the judge
                # The corrected boundary is near NOW, not 1203 s ago:
                assert abs(now - m["minute_boundary"]) < 60
            assert w.stale_drops == 0  # never dropped for timing reasons
        finally:
            w.close()

    def test_legacy_sidecar_resolves_unchanged(self):
        """Pre-judge sidecars (no timing block) keep their exact timing."""
        gps_ns = unix_to_gps_ns(WALL0)
        meta = {
            "start_rtp_timestamp": 24000 * 10,
            "gps_time_ns": gps_ns,
            "rtp_timesnap": 0,
        }
        t = bt.resolve_buffer_timing(meta, sample_rate=24000)
        assert t.source == "rtp_gps"
        assert t.offset_applied_ns == 0.0
        assert t.judge_tier is None
        assert t.sample0_utc == pytest.approx(WALL0 + 10.0, abs=1e-6)

    def test_snapshot_fallback_also_corrected(self):
        """timing block applies on the timing_snapshots[] fallback path too."""
        gps_ns = unix_to_gps_ns(WALL0)
        meta = {
            "start_rtp_timestamp": 24000 * 10,
            "timing_snapshots": [
                {"gps_time_ns": gps_ns, "rtp_timesnap": 0,
                 "local_receipt_time": 0.0},
            ],
            "timing": {"offset_ns": 2.5e9, "judge_tier": "T3"},
        }
        t = bt.resolve_buffer_timing(meta, sample_rate=24000)
        assert t.sample0_utc == pytest.approx(WALL0 + 10.0 + 2.5, abs=1e-6)
        assert t.offset_applied_ns == pytest.approx(2.5e9)
        assert t.judge_tier == "T3"

    def test_set_offset_judge_late_bind_registers_existing_pair(self, tmp_path):
        w = make_writer(tmp_path)
        try:
            gps_ns = unix_to_gps_ns(time.time())
            w.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=777)
            judge = StubJudge()
            w.set_offset_judge(judge, KEY)
            assert judge.registered == [(KEY, gps_ns, 777, 100)]
        finally:
            w.close()


# ────────────────────────────────────────────────────────────────────
# End-to-end: real OffsetJudge + writer (fault injection in miniature)
# ────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    def test_judge_corrects_writer_labels(self, tmp_path):
        """Full loop: wedged radiod pair -> judge measures vs bench ->
        writer labels come out corrected."""
        clock = FakeClock(wall=time.time())  # bench truth = real now
        bench = FakeBench(clock, tier="T4", sigma_ns=1e5)
        judge = make_judge(clock, tmp_path, bench)

        w = make_writer(tmp_path, judge=judge, key=KEY)
        try:
            now = clock.wall
            raw_utc = now - 1203.0
            gps_ns = unix_to_gps_ns(raw_utc)
            snap = 10_000
            # Anchor adoption flows writer -> judge automatically:
            w.add_timing_snapshot(gps_time_ns=gps_ns, rtp_timesnap=snap)
            assert (KEY[0], KEY[1]) in judge._sources
            judge.tick()  # bench measurement -> verdict available

            n = 100
            for i in range(6):
                w.write_samples(
                    np.ones(n, dtype=np.complex64),
                    rtp_timestamp=(snap + i * n) & 0xFFFFFFFF,
                )
            sidecars = read_sidecars(w)
            assert sidecars
            m = sidecars[0]
            assert m["timing"]["offset_ns"] == pytest.approx(1203e9, abs=1e7)
            t = bt.resolve_buffer_timing(m, sample_rate=100)
            # Corrected label lands at 'now', not 1203 s in the past.
            assert abs(t.sample0_utc - now) < 30
        finally:
            w.close()
            judge.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
