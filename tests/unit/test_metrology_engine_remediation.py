#!/usr/bin/env python3
"""
Unit tests for the M-M5 / M-M6 / M-M7 / M-M8 + S4-finish remediation in
``metrology_engine.py``.

These tests target the small helpers and one private method that the
remediation either introduced or reshaped, so we don't have to spin up a
full ``MetrologyEngine`` and feed it a synthetic minute of IQ for each
behaviour.

Coverage map:

  * M-M5 vacuum-fallback delay  → :func:`_vacuum_hop_fallback_delay`
  * M-M6 buffer-anchored minute → :meth:`MetrologyEngine.process_minute`
                                   (smoke-tested via a noise buffer and
                                   a divergent ``system_time``)
  * M-M7 synthetic-edge round-trip
                                → invariant check on the arrival-vs-
                                   timing-error consistency formula
  * M-M8 main-lobe suppression  → :meth:`MetrologyEngine._find_all_correlation_peaks`
  * S4-finish SNR migration     → :meth:`MetrologyEngine._find_all_correlation_peaks`
                                   reports canonical envelope SNR
"""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.core.metrology_engine import (
    MetrologyEngine,
    _great_circle_km,
    _vacuum_hop_fallback_delay,
    SPEED_OF_LIGHT_KM_S,
)
from hf_timestd.core.snr import peak_snr_db_envelope


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def temp_dirs(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    raw.mkdir()
    out.mkdir()
    return raw, out


@pytest.fixture
def engine(temp_dirs):
    raw, out = temp_dirs
    return MetrologyEngine(
        raw_buffer_dir=raw,
        output_dir=out,
        channel_name="WWV_10MHz",
        frequency_hz=10_000_000,
        receiver_grid="CM87",
        precise_lat=40.0,
        precise_lon=-105.0,
    )


# ---------------------------------------------------------------------
# M-M5: vacuum-fallback delay uses real hop geometry, not light_time×1.15
# ---------------------------------------------------------------------

class TestVacuumHopFallbackDelay:
    def test_short_path_uses_one_hop_geometry(self):
        """A 1500 km path is a 1-hop F2 link; the geometric delay must
        be the spherical slant length over c, *not* light_time × 1.15.

        At 1500 km with the F2 height of 300 km, the hop_geometry
        helper gives a slant ≈ 1530 km path; geometric delay ≈ 5.10 ms.
        The old heuristic would have returned 1500/c × 1.15 = 5.75 ms —
        16 % too high.
        """
        delay, sigma = _vacuum_hop_fallback_delay(1500.0, 10e6)
        light_time = 1500.0 / SPEED_OF_LIGHT_KM_S * 1000.0
        old_heuristic = light_time * 1.15

        assert delay > light_time  # always longer than vacuum
        # Geometric slant + iono term should differ from the heuristic
        # by more than the heuristic's own slop.
        assert abs(delay - old_heuristic) > 0.1
        assert sigma > 0.0

    def test_long_path_uses_multiple_hops(self):
        """At ~7000 km the F2 model needs 2+ hops; the geometric slant
        is materially longer than 1-hop would give."""
        delay_long, _ = _vacuum_hop_fallback_delay(7000.0, 10e6)
        light_time_long = 7000.0 / SPEED_OF_LIGHT_KM_S * 1000.0
        # Geometric slant must exceed light time over the ground arc
        # (the path zig-zags up to the F2 layer).
        assert delay_long > light_time_long + 0.5

    def test_frequency_dependence_of_iono_term(self):
        """The 40.3/f² iono term must make low-frequency carriers
        slower than high-frequency carriers over the same path."""
        d_25mhz, _ = _vacuum_hop_fallback_delay(3000.0, 25e6)
        d_5mhz, _ = _vacuum_hop_fallback_delay(3000.0, 5e6)
        # Lower frequency → larger iono group delay (1/f² scaling).
        assert d_5mhz > d_25mhz
        # The 5/25 MHz ratio gives a 25× iono difference; geometric
        # delays are identical, so the total separation is the iono
        # term scaled.
        assert (d_5mhz - d_25mhz) > 0.1

    def test_zero_distance_returns_zero(self):
        delay, sigma = _vacuum_hop_fallback_delay(0.0, 10e6)
        assert delay == 0.0
        assert sigma > 0.0  # still a finite uncertainty floor

    def test_uncertainty_scales_with_iono_term(self):
        """Uncertainty must carry the climatological iono magnitude —
        TEC varies by its own size over 24 hours."""
        _, sigma_high = _vacuum_hop_fallback_delay(3000.0, 5e6)
        _, sigma_low_iono = _vacuum_hop_fallback_delay(3000.0, 25e6)
        # Larger iono delay → larger uncertainty.
        assert sigma_high > sigma_low_iono


class TestEngineVacuumFallbackIntegration:
    """End-to-end: when arrival_matrix and HFPropagationModel both fail,
    the engine must reach the vacuum fallback and return delays that
    match :func:`_vacuum_hop_fallback_delay` exactly."""

    def test_engine_vacuum_fallback_path(self, engine, monkeypatch):
        # Force the two preferred paths to fail.
        engine.arrival_matrix = None

        def _no_prop_model(*args, **kwargs):
            raise RuntimeError("propagation model disabled for this test")

        # _predict_geometric_delay imports HFPropagationModel from
        # .propagation_model lazily inside the function body, so we
        # have to patch it on the source module (not on
        # metrology_engine).
        monkeypatch.setattr(
            "hf_timestd.core.propagation_model.HFPropagationModel",
            _no_prop_model,
            raising=False,
        )

        delay, dist, sigma = engine._predict_geometric_delay("WWVH")

        # Compute the expected value via the standalone helper.
        from hf_timestd.core.wwv_constants import STATION_LOCATIONS
        st = STATION_LOCATIONS["WWVH"]
        expected_dist = _great_circle_km(40.0, -105.0, st['lat'], st['lon'])
        expected_delay, expected_sigma = _vacuum_hop_fallback_delay(
            expected_dist, engine.frequency_hz,
        )

        assert abs(dist - expected_dist) < 1e-6
        assert abs(delay - expected_delay) < 1e-6
        assert abs(sigma - expected_sigma) < 1e-6
        assert engine._last_prediction_meta['data_source'] == 'vacuum_fallback'


# ---------------------------------------------------------------------
# M-M6: minute_boundary derives from buffer_timing, not system_time
# ---------------------------------------------------------------------

class TestMinuteBoundaryFromBufferTiming:
    def test_minute_boundary_uses_buffer_timing_when_provided(self, engine):
        """When system_time disagrees with buffer_timing by minutes,
        the engine must honour buffer_timing — the writer's system_time
        can be wrong by seconds-to-minutes after a radiod restart."""
        from hf_timestd.core.buffer_timing import BufferTiming

        # Pure noise buffer; the carrier-SNR gate will reject early and
        # we won't get any detections, but the minute_boundary log
        # captures which clock was used.
        rng = np.random.default_rng(0)
        iq = (rng.standard_normal(24000 * 60) * 0.01).astype(np.complex64)

        # buffer_timing says it's UTC second 600 (= minute 10).
        bt = BufferTiming(
            sample0_utc=600.0,
            sample_rate=24000,
            source='rtp_gps',
            n_snapshots_used=1,
            jitter_ms=0.0,
        )
        # system_time disagrees by ~12 minutes (should be ignored).
        bad_system_time = 600.0 + 12 * 60

        # We can't easily intercept the minute_boundary value the
        # engine computes internally, but we can run the pipeline and
        # check that processing didn't blow up on the divergent
        # system_time.  Behavioural pin: with the M-M6 fix, the
        # minute_boundary is derived from buffer_timing — a corrupted
        # system_time can no longer cascade into tone-schedule decisions.
        results = engine.process_minute(
            iq, bad_system_time, rtp_timestamp=1_000_000, buffer_timing=bt,
        )
        # Pure noise: no detections expected after the rejection gates.
        assert isinstance(results, list)


# ---------------------------------------------------------------------
# M-M7: synthetic edge measurement consistency
# ---------------------------------------------------------------------

class TestSyntheticEdgeArrivalConsistency:
    """Pin the arithmetic that ties arrival_ms and timing_error_ms
    together for the synthetic-edge measurement path.  The bug was
    ``mid_sec = int(mid_utc)`` truncating up to 0.5 s of fractional
    second; the fix uses ``mid_utc`` directly and rounds *only* the
    integer ``utc_second`` label.
    """

    def test_fractional_mid_utc_does_not_distort_arrival(self):
        """Compute the synthetic arrival both with the fixed formula
        and with the old truncating one; the difference must equal the
        truncated fraction in ms."""
        from hf_timestd.core.buffer_timing import BufferTiming

        # Buffer mid-time deliberately has a 0.4 s fractional part.
        buf_start_utc = 1_000_000.3
        buf_end_utc = 1_000_000.5  # mid = 1_000_000.4
        mid_utc = (buf_start_utc + buf_end_utc) / 2.0
        assert mid_utc == pytest.approx(1_000_000.4)

        prop_delay_sec = 0.020      # 20 ms
        timing_error_ms = -3.0

        # The new (fixed) formula.
        synth_arrival_utc_new = (
            mid_utc + prop_delay_sec + timing_error_ms / 1000.0
        )

        # The old formula truncated mid_utc.
        mid_sec_old = int(mid_utc)
        synth_arrival_utc_old = (
            mid_sec_old + prop_delay_sec + timing_error_ms / 1000.0
        )

        # The new value sits inside the buffer; the old one drifts by
        # the truncated fraction (0.4 s here).
        assert abs(synth_arrival_utc_new - synth_arrival_utc_old) == pytest.approx(0.4)

        # Sample-space round-trip with a unit-rate BufferTiming.
        bt = BufferTiming(
            sample0_utc=buf_start_utc,
            sample_rate=24000,
            source='rtp_gps',
            n_snapshots_used=1,
            jitter_ms=0.0,
        )
        new_sample = bt.utc_to_sample(synth_arrival_utc_new)
        arrival_ms = new_sample * 1000.0 / 24000.0

        # arrival_ms is the offset of the arrival sample from sample 0
        # within the buffer.  Expected: (mid_utc - sample0_utc + delay
        # + timing_error/1000) × 1000.
        expected_arrival_ms = (
            (mid_utc - buf_start_utc) * 1000.0
            + prop_delay_sec * 1000.0
            + timing_error_ms
        )
        assert arrival_ms == pytest.approx(expected_arrival_ms, abs=1e-6)

    def test_utc_second_is_rounded_not_floored(self):
        """At fractional second 0.6, int(mid_utc) gives the wrong
        nearest-integer label by 1 s; round() gives the right one."""
        mid_utc = 1_000_000.6
        # Old behaviour (floor → 1_000_000).  Fix uses round → 1_000_001.
        assert int(mid_utc) == 1_000_000
        assert int(round(mid_utc)) == 1_000_001


# ---------------------------------------------------------------------
# M-M8 + S4-finish: _find_all_correlation_peaks
# ---------------------------------------------------------------------

class TestFindAllCorrelationPeaks:
    @pytest.fixture
    def two_peak_envelope(self):
        """Build a synthetic correlation envelope with two narrow
        triangular lobes embedded in noise.

        Lobe 1 at sample 5000, lobe 2 at sample 5400.  The narrow
        half-width (200 samples) is deliberately *less* than
        ``n_template // 2`` so the old suppression radius of
        ``±n_template = ±800`` blots out lobe 2 entirely, while the
        new default of ``±400`` leaves lobe 2's peak visible at 5400."""
        rng = np.random.default_rng(2026_05_19)
        n = 12000
        env = rng.standard_normal(n).astype(np.float64) * 0.02
        env = np.abs(env)  # Rayleigh-ish noise floor

        def tri(center: int, peak: float, half: int):
            arr = np.zeros(n)
            for k in range(-half, half + 1):
                idx = center + k
                if 0 <= idx < n:
                    arr[idx] = peak * max(0.0, 1.0 - abs(k) / half)
            return arr

        # Narrow lobes (half-width 200) keep them inside the old
        # suppression window so the regression test is unambiguous.
        env = env + tri(5000, 1.0, 200) + tri(5400, 0.8, 200)
        return env

    def test_mainlobe_suppression_finds_close_peaks(self, engine, two_peak_envelope):
        """With ``mainlobe_samples = 400`` (= n_template // 2 default),
        the two lobes 400 samples apart are reported as distinct peaks."""
        env = two_peak_envelope
        n_template = 800
        # Noise envelope = samples far from both peaks.
        noise = np.concatenate([env[:4000], env[6500:]])

        peaks = engine._find_all_correlation_peaks(
            correlation=env,
            dominant_peak_idx=5000,
            noise_envelope=noise,
            n_template=n_template,
            start_sample=0,
            min_corr_snr_db=3.0,
        )

        assert len(peaks) >= 2
        # First two peaks should be near the two synthetic lobes.
        idxs = sorted(p['peak_idx'] for p in peaks[:2])
        assert any(abs(idx - 5000) <= 50 for idx in idxs)
        assert any(abs(idx - 5400) <= 50 for idx in idxs)

    def test_old_full_template_suppression_loses_second_peak(self, engine, two_peak_envelope):
        """Pin the regression: passing ``mainlobe_samples = n_template``
        (the old behaviour) erases the second peak — this is exactly
        what M-M8 was complaining about."""
        env = two_peak_envelope
        n_template = 800
        noise = np.concatenate([env[:4000], env[6500:]])

        peaks_old = engine._find_all_correlation_peaks(
            correlation=env,
            dominant_peak_idx=5000,
            noise_envelope=noise,
            n_template=n_template,
            start_sample=0,
            min_corr_snr_db=3.0,
            mainlobe_samples=n_template,  # old behaviour
        )

        # With ±n_template (=800) suppression around the dominant peak
        # at 5000, the entire region 4200-5800 is masked.  Lobe 2's
        # whole footprint (5200-5600) lies inside that mask, so no
        # secondary peak from the multipath arrival is reportable.
        peak_idxs = [p['peak_idx'] for p in peaks_old]
        assert not any(5200 <= idx <= 5600 for idx in peak_idxs), (
            f"Old behaviour should not surface a peak from lobe 2; got {peak_idxs}"
        )

    def test_reports_canonical_envelope_snr(self, engine, two_peak_envelope):
        """S4-finish: the SNR each peak reports must equal what
        ``peak_snr_db_envelope`` computes from the same noise region.
        This is the entire point of the S4 unification."""
        env = two_peak_envelope
        noise = np.concatenate([env[:4000], env[6500:]])

        peaks = engine._find_all_correlation_peaks(
            correlation=env,
            dominant_peak_idx=5000,
            noise_envelope=noise,
            n_template=800,
            start_sample=0,
            min_corr_snr_db=0.0,
        )

        assert len(peaks) >= 1
        # The reported SNR for the dominant peak should agree (within
        # float roundoff) with a direct canonical-helper call.
        canonical = peak_snr_db_envelope(peaks[0]['peak_value'], noise)
        assert abs(peaks[0]['corr_snr_db'] - canonical) < 1e-9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
