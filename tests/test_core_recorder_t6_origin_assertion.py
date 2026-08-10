"""T6 origin assertion — chain delay is asserted, never derived.

Spec: docs/design/T6_ORIGIN_ASSERTION_DESIGN.md §5
"""
import time

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


class _FakeReading:
    def __init__(self, pps_utc_sec):
        self.pps_utc_sec = pps_utc_sec


class _FakeProbe:
    def __init__(self, reading):
        self._reading = reading

    def get_latest(self):
        return self._reading


class TestDiffLayerBGuard:
    """Layer-B plausibility guard on the diff/HFPS path
    (``_t6_diff_disambiguate_via_t5_lb1421``).

    The guard exists to reject sidelobe/phantom-peak captures.  It
    must test the *derived* residual (``reported_residual_ns``), not
    the *asserted* constant (``effective_chain_delay_ns``) — the
    latter tracks ``chain_delay_calib_s`` and, at the default 0.0,
    can never exceed the bound on its own, which would silently
    disable the guard.  These tests fail if that swap is reverted.

    Reachable with only ``_lb1421_probe`` and
    ``_t6_chain_delay_calib_s`` stubbed — the method touches neither
    ``_t6_diff_calibrator`` nor ``_t6_channel_info``.
    """

    class _Fake:
        T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0
        _t6_diff_disambiguate_via_t5_lb1421 = (
            CoreRecorderV2._t6_diff_disambiguate_via_t5_lb1421)
        _t6_resolve_chain_delay_ns = staticmethod(
            CoreRecorderV2._t6_resolve_chain_delay_ns)
        _t6_report_derived_residual = (
            CoreRecorderV2._t6_report_derived_residual)

        def __init__(self, pps_utc_sec):
            self._lb1421_probe = _FakeProbe(_FakeReading(pps_utc_sec))
            self._t6_chain_delay_calib_s = 0.0

    def test_large_residual_trips_guard_even_with_zero_calib(self):
        # 350 ms > the 250 ms bound, but chain_delay_calib_s is 0.0 so
        # the ASSERTED value (effective_chain_delay_ns) is 0 — well
        # inside the bound.  Only testing reported_residual_ns catches
        # this.  A reverted swap would let this fall through to a
        # (bogus) True.
        pps_utc_sec = int(time.time())
        fake = self._Fake(pps_utc_sec)
        result = fake._t6_diff_disambiguate_via_t5_lb1421(
            chain_delay_ns_raw=0,
            raw_wall_time_sec=pps_utc_sec + 0.35,
            edge_rtp=123,
        )
        assert result is False
        assert not hasattr(fake, '_t6_diff_disambiguation_ns')

    def test_small_residual_does_not_trip_guard(self):
        # 30 ms is well inside the 250 ms bound: the guard must not
        # fire and the method must run to completion.
        pps_utc_sec = int(time.time())
        fake = self._Fake(pps_utc_sec)
        result = fake._t6_diff_disambiguate_via_t5_lb1421(
            chain_delay_ns_raw=0,
            raw_wall_time_sec=pps_utc_sec + 0.03,
            edge_rtp=123,
        )
        assert result is True
        # asserted (effective_chain_delay_ns) is 0 at calib=0.0
        assert fake._t6_diff_disambiguation_ns == 0


class TestHppsLayerBGuard:
    """Same guard, mirrored on the MF/HPPS path
    (``_t6_disambiguate_via_t5_lb1421``).

    Unlike the diff/HFPS path, this method does consult
    ``_t6_calibrator`` (for ``_last_edge_rtp`` and, on the success
    path, ``sample_rate``) and ``_t6_channel_info`` (via the real
    ``ka9q.rtp_recorder.rtp_to_utc``, since the whole point of this
    method is deriving ``raw_wall_time_sec`` from the RTP anchor).
    Rather than duck-type a fake channel, this uses the real
    ``ka9q.discovery.ChannelInfo`` with ``gps_time``/``rtp_timesnap``
    chosen so ``rtp_to_utc`` returns a controlled wall-clock value —
    the class already supports this via its documented backward-compat
    fallback in ``get_anchor()``.
    """

    @staticmethod
    def _make_channel(sample_rate, rtp_timesnap, desired_wall_sec):
        from ka9q.discovery import ChannelInfo
        from ka9q.rtp_recorder import (
            GPS_UTC_OFFSET, GPS_LEAP_SECONDS, BILLION)
        # Invert rtp_to_utc's sender_time formula (rtp_delta=0 since
        # rtp_timesnap == the RTP value the method will look up) so the
        # real conversion returns exactly desired_wall_sec.  See
        # ka9q/rtp_recorder.py::rtp_to_utc.
        gps_time = (round(desired_wall_sec * BILLION)
                    - BILLION * (GPS_UTC_OFFSET - GPS_LEAP_SECONDS))
        return ChannelInfo(
            ssrc=1, preset='iq', sample_rate=sample_rate, frequency=10e6,
            snr=20.0, multicast_address='239.1.2.3', port=5004,
            gps_time=gps_time, rtp_timesnap=rtp_timesnap,
        )

    class _Fake:
        T6_RESIDUAL_REPORT_PERIOD_SEC = 300.0
        _t6_disambiguate_via_t5_lb1421 = (
            CoreRecorderV2._t6_disambiguate_via_t5_lb1421)
        _t6_resolve_chain_delay_ns = staticmethod(
            CoreRecorderV2._t6_resolve_chain_delay_ns)
        _t6_report_derived_residual = (
            CoreRecorderV2._t6_report_derived_residual)
        # Real implementation: getattr-guarded, safe no-op without
        # ``_t6_rate_est`` set (see its docstring).
        _t6_rate_reset = CoreRecorderV2._t6_rate_reset

        def __init__(self, pps_utc_sec, channel_info, last_edge_rtp):
            self._lb1421_probe = _FakeProbe(_FakeReading(pps_utc_sec))
            self._t6_chain_delay_calib_s = 0.0
            self._t6_channel_info = channel_info

            class _Calibrator:
                sample_rate = channel_info.sample_rate
                _last_edge_rtp = last_edge_rtp
            self._t6_calibrator = _Calibrator()

    def test_large_residual_trips_guard_even_with_zero_calib(self):
        pps_utc_sec = int(time.time())
        last_edge_rtp = 123456
        channel = self._make_channel(
            96000, last_edge_rtp, pps_utc_sec + 0.35)
        fake = self._Fake(pps_utc_sec, channel, last_edge_rtp)
        result = fake._t6_disambiguate_via_t5_lb1421(
            type('R', (), {'chain_delay_ns': 0})())
        assert result is False
        assert not hasattr(fake, '_t6_native_anchor')

    def test_small_residual_does_not_trip_guard(self):
        pps_utc_sec = int(time.time())
        last_edge_rtp = 123456
        channel = self._make_channel(
            96000, last_edge_rtp, pps_utc_sec + 0.03)
        fake = self._Fake(pps_utc_sec, channel, last_edge_rtp)
        result = fake._t6_disambiguate_via_t5_lb1421(
            type('R', (), {'chain_delay_ns': 0})())
        assert result is True
        # asserted (effective_chain_delay_ns) is 0 at calib=0.0
        assert fake._t6_native_anchor.chain_delay_ns == 0
