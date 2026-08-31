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


class TestMeasuredPlaneMustEarnItsUse:
    """A plane term is only worth applying if it is better than nothing.

    AC0G-B4 2026-08-25, during the content-convention window:

        label_plane: offset_ns -25,846,958   sigma_ns 197,134,152   n 27

    A -25.8 ms correction with a **197 ms** standard deviation.  The
    tracker reported that sigma honestly; the consumer used the estimate
    anyway, because `effective_label_plane_offset_ns()` took the measured
    value whenever one existed and never looked at its uncertainty.

    That term is subtracted at the single choke point the adoption gate
    and the shadow residuals share, against bounds of order 5 ms.  A
    correction whose own sigma is forty times the bound it perturbs makes
    every comparison worse than leaving it alone, and it is why the
    cross-bench delta wandered -16.877 -> +8.548 -> +5.815 ms instead of
    sitting near a stable -16.6 ms.
    """

    LIVE_BAD = (-25_846_958.0, 197_134_152.0, 27)     # the flip window
    GOOD = (-16_600_000.0, 500_000.0, 60)

    def _seed(self, j, offset_ns, sigma_ns, n, t0=1000.0):
        """Drive the tracker to a chosen estimate via real observations."""
        for i in range(n):
            host = 100.0 + i
            j.observe_label_plane(
                reading("T6", host + offset_ns / 1e9 +
                        (sigma_ns / 1e9 if i % 2 else -sigma_ns / 1e9),
                        t0 + i, plane="label"),
                reading("T4", host, t0 + i, sigma_ns=1.0, plane="host"),
            )

    def test_a_useless_estimate_is_refused(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=0.0,
                       label_plane_measure=True)
        self._seed(j, *self.LIVE_BAD)
        est = j._label_plane.estimate()
        assert est is not None and est.sigma_ns > 5_000_000
        # ... and must NOT be the term in force
        assert j.effective_label_plane_offset_ns() == 0.0

    def test_a_tight_estimate_is_used(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=0.0,
                       label_plane_measure=True)
        self._seed(j, *self.GOOD)
        assert j.effective_label_plane_offset_ns() == pytest.approx(
            -16_600_000.0, abs=1_000_000)

    def test_status_reports_what_was_actually_applied(self, tmp_path):
        """The JSON must not say 'measured' about a term that was refused
        — the whole point of publishing it is auditability."""
        j = make_judge(tmp_path, label_plane_offset_ns=0.0,
                       label_plane_measure=True)
        self._seed(j, *self.LIVE_BAD)
        st = j.label_plane_status()
        assert st["source"] == "config"
        assert st["offset_ns"] == 0.0
        # the refused estimate stays visible, so nobody has to guess why
        est = j._label_plane.estimate()
        assert st["rejected_sigma_ns"] == pytest.approx(est.sigma_ns, abs=1)
        assert st["rejected_offset_ns"] == pytest.approx(est.offset_ns, abs=1)
        assert st["rejected_n"] == est.n
        assert "exceeds" in st["rejected_reason"]
        # and the applied term carries no measured provenance
        assert st["sigma_ns"] is None and st["n"] == 0

    def test_no_estimate_still_falls_back_cleanly(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=-16_618_000.0,
                       label_plane_measure=True)
        assert j.effective_label_plane_offset_ns() == -16_618_000.0
        assert j.label_plane_status()["source"] == "config"


class TestViolationPathUsesTheSameTerm:
    """The plane term reached the cross-bench gate but not the alarm.

    AC0G-B4, 2026-08-30/31: the offset judge logged ~295 CRITICAL
    violations an hour against radiod's advertised epoch, every one of
    them negative and clustered at -13 to -14 ms across all five
    channels at once.  That is not a fault signature -- a fault does not
    arrive on every channel simultaneously with the same sign and the
    same magnitude.  It is Λ, the radiod processing interval, which the
    content-time convention excludes from the label BY DEFINITION.

    ``_cross_bench_delta_ns`` already corrected for exactly this, but the
    sustained-violation test compared ``ema_offset_ns`` raw.  So one half
    of the judge knew about labeling conventions and the other half
    screamed at one.  Same term, same choke point, both halves.
    """

    T = 1_700_000_000.0
    LAMBDA_NS = -13_500_000.0        # label reads EARLIER than host

    def _bench(self, plane, sigma_ns=880_000.0, utc=None):
        return BenchReading(tier="T6" if plane == "label" else "T4",
                            utc=self.T if utc is None else utc,
                            sigma_ns=sigma_ns, mono=0.0, plane=plane)

    def test_a_pure_plane_gap_is_not_a_violation(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=self.LAMBDA_NS)
        # The source sits exactly one plane-gap from a label-plane bench.
        assert not j._violates(self.LAMBDA_NS, self._bench("label")), (
            "Λ is excluded from the label by definition, not a contradiction")

    def test_a_real_epoch_error_still_violates(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=self.LAMBDA_NS)
        # 30 ms beyond the plane gap is a genuine disagreement.
        off = self.LAMBDA_NS - 30_000_000.0
        assert j._violates(off, self._bench("label")), \
            "the correction must not blind the judge to a true epoch error"

    def test_a_host_plane_bench_is_never_plane_corrected(self, tmp_path):
        j = make_judge(tmp_path, label_plane_offset_ns=self.LAMBDA_NS)
        # Against a host-plane bench there is no plane gap to remove, so
        # the same raw offset that was innocent above is a violation here.
        assert j._violates(self.LAMBDA_NS, self._bench("host")), \
            "correcting a same-plane comparison would hide a real fault"

    def test_the_plane_terms_own_uncertainty_widens_the_bound(self, tmp_path):
        """Subtracting a measured term imports that term's sigma."""
        j = make_judge(tmp_path, label_plane_offset_ns=self.LAMBDA_NS)
        bench = self._bench("label")
        # Just outside k*sigma_bench alone (5 * 0.88 ms = 4.4 ms)...
        off = self.LAMBDA_NS - 5_000_000.0
        assert j._violates(off, bench)
        # ...but inside it once a 3 ms plane-term sigma is combined in.
        j._plane_sigma_for_test = 3_000_000.0
        assert not j._violates(off, bench, plane_sigma_ns=3_000_000.0)
