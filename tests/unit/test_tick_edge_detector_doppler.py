#!/usr/bin/env python3
"""
Unit tests for TickEdgeDetector._estimate_doppler (M-M2 remediation).

The Doppler estimator was historically:

  * indexed by ``sec_in_minute`` (an integer 0–59 that wraps),
  * unwrapped with bare ``np.unwrap`` (adjacency in array order, not time),
  * and fit with an unweighted ``np.polyfit``.

This regressed in two ways:

  (a) A missing tick produces a Δt = 2 s sample where the ±π adjacency
      rule misclassifies any real |f_D| > 0.25 Hz as a 2π cycle slip.
  (b) An unweighted fit treats a 3 dB tick the same as a 15 dB tick,
      even though matched-filter phase σ scales as 1/SNR_amp.

These tests pin the four guarantees of the new estimator:

  1. Clean linear phase ramp → Doppler recovered to a few mHz.
  2. SNR-weighting: a single noisy low-SNR tick cannot drag the fit.
  3. A missing tick straddling a wrap point does not flip the slope sign.
  4. Indexing is by absolute UTC time, not sec_in_minute — a window that
     starts late in the minute fits the same as one that starts at 0.
"""

import numpy as np
import pytest

from hf_timestd.core.tick_edge_detector import (
    TickDetection,
    TickEdgeDetector,
)


def _make_tick(utc_second: int, phase_rad: float, snr_db: float = 12.0) -> TickDetection:
    """Build a minimal detected TickDetection — only the fields the
    Doppler estimator reads need to be meaningful."""
    return TickDetection(
        utc_second=utc_second,
        sec_in_minute=utc_second % 60,
        expected_sample=0,
        peak_sample=0.0,
        front_edge_sample=0.0,
        corr_snr_db=snr_db,
        timing_error_ms=0.0,
        detected=True,
        is_clean_minute=True,
        is_doubled_tick=False,
        carrier_phase_rad=phase_rad,
    )


def _ramp(utc_seconds, doppler_hz, phase0=0.0):
    return [
        2.0 * np.pi * doppler_hz * (s - utc_seconds[0]) + phase0
        for s in utc_seconds
    ]


class TestEstimateDopplerClean:
    def test_returns_doppler_for_clean_linear_phase(self):
        utc = list(range(100, 130))  # 30 contiguous seconds
        doppler_true = 0.13  # Hz
        phases = _ramp(utc, doppler_true)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]

        f_hat, sigma = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        assert sigma is not None
        # Noise-free input — should be tight (< 1 mHz).
        assert abs(f_hat - doppler_true) < 1e-3
        # Uncertainty for noise-free data is numerically tiny.
        assert sigma >= 0.0

    def test_negative_doppler_recovered(self):
        utc = list(range(0, 30))
        doppler_true = -0.27
        phases = _ramp(utc, doppler_true)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        assert abs(f_hat - doppler_true) < 1e-3

    def test_too_few_ticks_returns_none(self):
        utc = [0, 1, 2, 3]  # 4 < 5
        phases = _ramp(utc, 0.1)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]

        f_hat, sigma = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is None
        assert sigma is None

    def test_short_span_returns_none(self):
        # 5 ticks but only 4 seconds of span — under the 5 s minimum.
        utc = [10, 11, 12, 13, 14]
        phases = _ramp(utc, 0.1)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]

        f_hat, sigma = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is None
        assert sigma is None


class TestEstimateDopplerSnrWeighting:
    def test_low_snr_outlier_does_not_dominate(self):
        # 29 clean high-SNR ticks at f=0.1 Hz, plus one low-SNR tick whose
        # phase is way off. Unweighted polyfit would let the outlier
        # swing the fit; the SNR-weighted fit must not.
        utc = list(range(0, 30))
        doppler_true = 0.10
        phases = _ramp(utc, doppler_true)

        ticks = [_make_tick(s, p, snr_db=18.0) for s, p in zip(utc, phases)]
        # Corrupt the middle tick: 0.6 rad phase error, low SNR (≈ 0 dB).
        ticks[15] = _make_tick(utc[15], phases[15] + 0.6, snr_db=0.0)

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        # 18 dB tick has 8x the amplitude SNR of a 0 dB tick → 64x weight.
        # The fit should be dominated by the 29 clean ticks.
        assert abs(f_hat - doppler_true) < 0.005

    def test_endpoint_outlier_pinned_by_weighting(self):
        # Endpoint outliers have maximum leverage on a linear fit's
        # slope. A 0.6 rad shift at second 0 with low SNR (0 dB) would
        # drag an unweighted fit by ~0.6 mHz; the SNR-weighted fit must
        # absorb it as noise on a single low-weight sample.
        utc = list(range(0, 30))
        doppler_true = 0.10
        phases = _ramp(utc, doppler_true)
        ticks = [_make_tick(s, p, snr_db=18.0) for s, p in zip(utc, phases)]
        # Corrupt the first (highest-leverage) tick.
        ticks[0] = _make_tick(utc[0], phases[0] + 0.6, snr_db=0.0)

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        # Without weighting, the bias would be ~0.6 mHz (computed
        # closed-form from leverage). The weighted fit holds it well
        # under that.
        assert abs(f_hat - doppler_true) < 1e-4


class TestEstimateDopplerSlipSafeUnwrap:
    def test_missing_tick_does_not_flip_sign(self):
        # f_D = +0.4 Hz is comfortably within the ±0.5 Hz adjacent-tick
        # Nyquist for 1-sec spacing — but a 2-sec gap exceeds it
        # (0.4·2 = 0.8 cycles → wraps). The old np.unwrap, indexed by
        # detected-tick number, sees Δφ ≈ +0.8·2π ≡ −0.4·2π after the
        # gap and would invert the slope. The two-step seed-then-wrap
        # must keep the sign correct.
        doppler_true = 0.40
        utc = list(range(0, 30))
        # Remove tick at second 15 to create a 2-sec gap.
        utc.remove(15)
        phases = _ramp(utc, doppler_true)
        ticks = [_make_tick(s, p, snr_db=15.0) for s, p in zip(utc, phases)]

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        # Must recover sign and magnitude; the gap is bridged by the
        # seed-line residual wrap, not by a per-index unwrap.
        assert f_hat > 0.0
        assert abs(f_hat - doppler_true) < 0.01

    def test_long_gap_is_excluded_from_seed_but_fit_still_works(self):
        # First 10 ticks contiguous; then a 4-second gap; then 10 more.
        # Only the contiguous pairs (Δt = 1 s) feed the seed.
        doppler_true = 0.20
        utc = list(range(0, 10)) + list(range(14, 24))
        phases = _ramp(utc, doppler_true)
        ticks = [_make_tick(s, p, snr_db=12.0) for s, p in zip(utc, phases)]

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        assert abs(f_hat - doppler_true) < 0.01

    def test_returns_none_when_no_short_gap_pairs(self):
        # Every adjacent pair is > 1.5 s apart → no seed possible.
        utc = [0, 2, 4, 6, 8, 10, 12, 14, 16]  # 9 ticks, all Δt = 2 s
        phases = _ramp(utc, 0.1)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]

        f_hat, sigma = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is None
        assert sigma is None


class TestEstimateDopplerAbsoluteUtc:
    def test_invariant_under_utc_offset(self):
        # The estimator must key off the absolute utc_second baseline,
        # not sec_in_minute. A window starting at second 55 and running
        # into the next minute (so sec_in_minute wraps 55→59→0→14) must
        # produce the same Doppler as the same shape rooted at second 0.
        doppler_true = 0.15

        utc_a = list(range(0, 20))
        phases_a = _ramp(utc_a, doppler_true)
        ticks_a = [_make_tick(s, p) for s, p in zip(utc_a, phases_a)]

        utc_b = list(range(55, 75))  # crosses minute boundary
        phases_b = _ramp(utc_b, doppler_true)
        ticks_b = [_make_tick(s, p) for s, p in zip(utc_b, phases_b)]

        f_a, _ = TickEdgeDetector._estimate_doppler(ticks_a)
        f_b, _ = TickEdgeDetector._estimate_doppler(ticks_b)

        assert f_a is not None and f_b is not None
        # Both noise-free — must match to numerical precision.
        assert abs(f_a - f_b) < 1e-6
        assert abs(f_a - doppler_true) < 1e-3


class TestEstimateDopplerRobustness:
    def test_drops_non_finite_phases(self):
        utc = list(range(0, 30))
        phases = _ramp(utc, 0.1)
        ticks = [_make_tick(s, p) for s, p in zip(utc, phases)]
        # Inject a NaN phase tick that should be silently dropped.
        ticks[10] = _make_tick(utc[10], float("nan"))

        f_hat, _ = TickEdgeDetector._estimate_doppler(ticks)

        assert f_hat is not None
        assert abs(f_hat - 0.1) < 1e-3

    def test_uncertainty_grows_with_noise(self):
        rng = np.random.default_rng(2026_05_19)
        utc = list(range(0, 30))
        doppler_true = 0.10
        clean_phases = _ramp(utc, doppler_true)

        noisy_phases = [p + 0.2 * rng.standard_normal() for p in clean_phases]
        ticks_clean = [_make_tick(s, p) for s, p in zip(utc, clean_phases)]
        ticks_noisy = [_make_tick(s, p) for s, p in zip(utc, noisy_phases)]

        _, sigma_clean = TickEdgeDetector._estimate_doppler(ticks_clean)
        _, sigma_noisy = TickEdgeDetector._estimate_doppler(ticks_noisy)

        assert sigma_clean is not None and sigma_noisy is not None
        assert sigma_noisy > sigma_clean


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
