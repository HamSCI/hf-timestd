"""Cross-bench plane correction (CONTENT_TIME_LABELING_CONVENTION.md §5.2).

Benches ground their truth in different planes: T4/T3/T2 in host-now,
T5 in sample arrival, T6 in the anchor label plane.  Under the
content-time labeling convention the label plane sits one transport
latency (~16.6 ms measured on AC0G-B4, 2026-08-24 A/B) earlier than the
host plane, and every cross-plane comparison — the adoption gate and
the shadow residuals, which share ``_cross_bench_delta_ns`` — must
correct for the expected plane difference instead of reading it as a
bench fault.

Mechanism now, activation later: ``label_plane_offset_ns`` defaults to
0.0, which is byte-identical to today's behavior; it becomes the
floor-measured transport when the convention is adopted.
"""
import pytest
from pathlib import Path

from hf_timestd.core.offset_judge import BenchReading, OffsetJudge


class FakeClock:
    def __init__(self, wall=1_700_000_000.0, mono=1_000.0):
        self.wall = wall
        self.mono_v = mono

    def time(self):
        return self.wall

    def mono(self):
        return self.mono_v


def reading(tier, utc, mono, sigma_ns=1e6, plane=None):
    kw = {}
    if plane is not None:
        kw["plane"] = plane
    return BenchReading(tier=tier, utc=utc, sigma_ns=sigma_ns, mono=mono, **kw)


def make_judge(tmp_path, **cfg):
    clock = FakeClock()
    return OffsetJudge(
        config={"enabled": True, "tick_seconds": 10.0, **cfg},
        benches=[],
        publish_path=tmp_path / "offset_judge.json",
        time_fn=clock.time,
        mono_fn=clock.mono,
    )


class TestBenchReadingPlane:
    def test_plane_defaults_to_host(self):
        r = reading("T4", 100.0, 1.0)
        assert r.plane == "host"

    def test_label_plane_is_expressible(self):
        r = reading("T6", 100.0, 1.0, plane="label")
        assert r.plane == "label"


class TestConfigKnob:
    def test_defaults_to_zero(self, tmp_path):
        j = make_judge(tmp_path)
        assert j.label_plane_offset_ns == 0.0

    def test_configurable(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_600_000.0)
        assert j.label_plane_offset_ns == -16_600_000.0


class TestDeltaCorrection:
    """The choke point: gate and shadows both ride _cross_bench_delta_ns."""

    T = 1_700_000_000.0

    def test_default_zero_is_todays_behavior(self, tmp_path):
        j = make_judge(tmp_path)
        label = reading("T6", self.T - 0.0165, 1.0, plane="label")
        host = reading("T4", self.T, 1.0)
        d = j._cross_bench_delta_ns(label, host, mono_now=1.0)
        assert abs(d - (-16_500_000.0)) < 100.0  # float64 ULP at Unix-epoch scale is ~240 ns/op

    def test_label_vs_host_corrected_by_the_term(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_500_000.0)
        label = reading("T6", self.T - 0.0165, 1.0, plane="label")
        host = reading("T4", self.T, 1.0)
        d = j._cross_bench_delta_ns(label, host, mono_now=1.0)
        assert abs(d) < 100.0  # float64 ULP at Unix-epoch scale is ~240 ns/op  # the expected plane difference cancels

    def test_correction_is_antisymmetric(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_500_000.0)
        label = reading("T6", self.T - 0.0165, 1.0, plane="label")
        host = reading("T4", self.T, 1.0)
        d = j._cross_bench_delta_ns(host, label, mono_now=1.0)
        assert abs(d) < 100.0  # float64 ULP at Unix-epoch scale is ~240 ns/op

    def test_same_plane_pairs_are_never_corrected(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_500_000.0)
        a = reading("T4", self.T, 1.0)
        b = reading("T3", self.T - 0.001, 1.0)
        d = j._cross_bench_delta_ns(b, a, mono_now=1.0)
        assert abs(d - (-1_000_000.0)) < 100.0  # float64 ULP at Unix-epoch scale is ~240 ns/op
        la = reading("T6", self.T, 1.0, plane="label")
        lb = reading("T6", self.T - 0.001, 1.0, plane="label")
        d2 = j._cross_bench_delta_ns(lb, la, mono_now=1.0)
        assert abs(d2 - (-1_000_000.0)) < 100.0  # float64 ULP at Unix-epoch scale is ~240 ns/op


class TestGateUsesCorrectedDelta:
    T = 1_700_000_000.0

    def _pair(self):
        label = reading("T6", self.T - 0.0165, 1.0, sigma_ns=1e6, plane="label")
        host = reading("T4", self.T, 1.0, sigma_ns=6.5e5)
        return label, host

    def test_without_term_a_16ms_label_delta_is_blocked(self, tmp_path):
        j = make_judge(tmp_path)
        label, host = self._pair()
        assert j._cross_gate_ok_locked(label, host, mono_now=1.0) is False

    def test_with_term_the_same_pair_passes(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_500_000.0)
        label, host = self._pair()
        assert j._cross_gate_ok_locked(label, host, mono_now=1.0) is True


class TestMeasuredPlaneTerm:
    """The term is measured from live readings, not asserted in config.

    Adopting content-time labels makes the label plane sit one pipeline
    latency earlier than the host plane.  That gap is a physical property
    that drifts with load, so the judge learns it from paired
    label/host readings instead of carrying another hand-calibrated
    constant — the exact failure mode the 16.618 ms constant represented.
    """

    def test_falls_back_to_config_until_measured(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_600_000.0)
        assert j.effective_label_plane_offset_ns() == -16_600_000.0

    def test_measurement_supersedes_the_configured_value(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_600_000.0)
        for i in range(30):
            j.observe_label_plane(
                label=reading("T6", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10, plane="label"),
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=20_000.0),
            )
        eff = j.effective_label_plane_offset_ns()
        assert eff == pytest.approx(-15_500_000.0, abs=100_000.0)

    def test_an_undisciplined_host_never_feeds_the_term(self, tmp_path):
        """A 50 ms host says nothing useful about a 16 ms pipeline."""
        j = make_judge(tmp_path, label_plane_offset_ns=-16_600_000.0)
        for i in range(30):
            j.observe_label_plane(
                label=reading("T6", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10, plane="label"),
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=50e6),
            )
        assert j.effective_label_plane_offset_ns() == -16_600_000.0

    def test_same_plane_pairs_are_not_observed(self, tmp_path):
        """Two host-plane benches carry no plane information."""
        j = make_judge(tmp_path, label_plane_offset_ns=-16_600_000.0)
        for i in range(30):
            j.observe_label_plane(
                label=reading("T3", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10),          # plane="host"
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=20_000.0),
            )
        assert j.effective_label_plane_offset_ns() == -16_600_000.0

    def test_the_correction_uses_the_measured_term(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=0.0)
        for i in range(30):
            j.observe_label_plane(
                label=reading("T6", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10, plane="label"),
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=20_000.0),
            )
        label = reading("T6", 1_787_000_100.0 - 0.0155, mono=1_100.0,
                        plane="label")
        host = reading("T4", 1_787_000_100.0, mono=1_100.0, sigma_ns=20_000.0)
        # The plane gap is removed; what is left is the genuine (zero)
        # disagreement, not the 15.5 ms of pipeline.
        d = j._cross_bench_delta_ns(label, host, mono_now=1_100.0)
        assert abs(d) < 100_000.0

    def test_the_term_is_published_for_audit(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=0.0)
        for i in range(30):
            j.observe_label_plane(
                label=reading("T6", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10, plane="label"),
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=20_000.0),
            )
        pub = j.label_plane_status()
        assert pub["source"] == "measured"
        assert pub["offset_ns"] == pytest.approx(-15_500_000.0, abs=100_000.0)
        assert pub["sigma_ns"] >= 20_000.0
        assert pub["n"] == 30


class TestMeasurementIsGatedOnTheConvention:
    """The term may only be measured when the planes actually differ.

    Under the legacy convention the anchor already folds the pipeline
    latency into the label, so the label plane and the host plane
    coincide and the true offset is ~0.  Anything the tracker measures
    there is not a plane difference — it is T6's own residual — and
    feeding it into the cross-bench correction would subtract exactly the
    disagreement the gate exists to detect.  Seen live on AC0G-B4: with
    labels pinned legacy the judge measured a -3.77 ms "plane offset"
    that was really T6 error plus a restart transient.
    """

    def _feed(self, j, n=30):
        for i in range(n):
            j.observe_label_plane(
                label=reading("T6", 1_787_000_000.0 + i * 10 - 0.0155,
                              mono=1_000.0 + i * 10, plane="label"),
                host=reading("T4", 1_787_000_000.0 + i * 10,
                             mono=1_000.0 + i * 10, sigma_ns=20_000.0))

    def test_measuring_is_on_by_default(self, tmp_path):
        j = make_judge(tmp_path)
        self._feed(j)
        assert j.label_plane_status()["source"] == "measured"

    def test_disabled_never_measures(self, tmp_path):
        j = make_judge(tmp_path, label_plane_measure=False,
                       label_plane_offset_ns=0.0)
        self._feed(j)
        assert j.label_plane_status()["source"] == "config"
        assert j.effective_label_plane_offset_ns() == 0.0

    def test_disabled_still_honours_an_explicit_config_term(self, tmp_path):
        """Turning the measurement off is not the same as forcing zero."""
        j = make_judge(tmp_path, label_plane_measure=False,
                       label_plane_offset_ns=-16_600_000.0)
        self._feed(j)
        assert j.effective_label_plane_offset_ns() == -16_600_000.0
