"""T6 origin assertion — chain delay is asserted, never derived.

Spec: docs/design/T6_ORIGIN_ASSERTION_DESIGN.md §5
"""
from hf_timestd.core.core_recorder_v2 import CoreRecorderV2


class TestResolveChainDelay:
    def test_asserts_calib_and_reports_residual(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=0.03184, chain_delay_calib_s=0.0)
        assert asserted == 0
        assert reported == 31_840_000

    def test_asserted_tracks_calib_not_residual(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=0.10889, chain_delay_calib_s=0.000250)
        assert asserted == 250_000
        assert reported == 108_890_000

    def test_differing_residuals_yield_one_origin(self):
        # The defect this fixes: 31.84 ms and 47.30 ms were measured at
        # identical 96 kHz/+-25 kHz config 15 minutes apart and produced
        # two different origins.  They must now produce one.
        a1, r1 = CoreRecorderV2._t6_resolve_chain_delay_ns(0.03184, 0.0)
        a2, r2 = CoreRecorderV2._t6_resolve_chain_delay_ns(0.04730, 0.0)
        assert a1 == a2 == 0
        assert r1 != r2  # the diagnostic still distinguishes them

    def test_negative_residual_reported_signed(self):
        asserted, reported = CoreRecorderV2._t6_resolve_chain_delay_ns(
            residual_sec=-0.002, chain_delay_calib_s=0.0)
        assert asserted == 0
        assert reported == -2_000_000


class TestReporter:
    def test_reporter_records_and_throttles(self):
        class Fake:
            T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0
            _t6_report_derived_residual = (
                CoreRecorderV2._t6_report_derived_residual)
        f = Fake()
        f._t6_report_derived_residual("HPPS", 31_840_000, 0)
        first = f._t6_residual_report_wall
        f._t6_report_derived_residual("HFPS", 47_300_000, 0)
        # Second call inside the window must not move the throttle stamp,
        # but the latest value is always recorded for the status path.
        assert f._t6_residual_report_wall == first
        assert f._t6_derived_residual_ns == 47_300_000
