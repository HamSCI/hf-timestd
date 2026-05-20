"""
Tests for `core/snr` — the canonical correlation-peak SNR (review S4).

Pins down: the Rayleigh-envelope σ̂ = median/1.1774 estimator, the
signed-Gaussian σ̂ = std estimator, the dB ratio, and the NaN return
path that callers use to mark "SNR unknown".
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hf_timestd.core.snr import (
    peak_snr_db_envelope,
    peak_snr_db_signed,
    rayleigh_envelope_sigma,
)


# --------------------------------------------------------------------------
# Rayleigh-envelope σ̂
# --------------------------------------------------------------------------
def test_rayleigh_envelope_sigma_recovers_underlying_sigma():
    """Synthesise a Rayleigh envelope from σ=2 IQ noise and recover σ."""
    rng = np.random.default_rng(20260519)
    sigma_true = 2.0
    # |I + jQ| with I, Q ~ N(0, σ²) is Rayleigh(σ).
    env = np.abs(
        rng.standard_normal(200_000) * sigma_true
        + 1j * rng.standard_normal(200_000) * sigma_true
    )
    sigma_hat = rayleigh_envelope_sigma(env)
    assert sigma_hat == pytest.approx(sigma_true, rel=0.01)


def test_rayleigh_envelope_sigma_empty_input():
    assert rayleigh_envelope_sigma([]) == 0.0
    assert rayleigh_envelope_sigma(np.array([])) == 0.0


# --------------------------------------------------------------------------
# peak_snr_db_envelope — Rayleigh branch
# --------------------------------------------------------------------------
def test_peak_snr_db_envelope_matches_analytical_value():
    """For known σ and peak, SNR_dB should equal 20·log10(peak/σ)."""
    rng = np.random.default_rng(0)
    sigma = 1.5
    env = np.abs(
        rng.standard_normal(200_000) * sigma + 1j * rng.standard_normal(200_000) * sigma
    )
    peak = 30.0
    expected_db = 20.0 * math.log10(peak / sigma)
    got = peak_snr_db_envelope(peak, env)
    assert got == pytest.approx(expected_db, abs=0.05)


def test_peak_snr_db_envelope_returns_nan_for_empty_noise():
    """No noise samples → σ̂ undefined → return NaN, not a sentinel."""
    assert math.isnan(peak_snr_db_envelope(1.0, []))


def test_peak_snr_db_envelope_returns_nan_for_zero_median():
    """A degenerate noise region (all zeros) → σ̂ ≤ 0 → NaN."""
    assert math.isnan(peak_snr_db_envelope(1.0, np.zeros(100)))


def test_peak_snr_db_envelope_robust_to_outliers():
    """The median-based σ̂ ignores other peaks contaminating the noise
    region — std-based estimators would have been pulled high."""
    rng = np.random.default_rng(1)
    sigma = 1.0
    env = np.abs(
        rng.standard_normal(50_000) * sigma + 1j * rng.standard_normal(50_000) * sigma
    )
    # Inject a handful of strong outliers (other peaks).
    env_contaminated = env.copy()
    env_contaminated[::1000] = 50.0
    snr_clean = peak_snr_db_envelope(50.0, env)
    snr_outliers = peak_snr_db_envelope(50.0, env_contaminated)
    # Contaminated noise region barely budges σ̂ — SNR within 0.2 dB.
    assert snr_outliers == pytest.approx(snr_clean, abs=0.2)


# --------------------------------------------------------------------------
# peak_snr_db_signed — Gaussian branch
# --------------------------------------------------------------------------
def test_peak_snr_db_signed_matches_analytical_value():
    """For zero-mean signed Gaussian noise, σ̂ = std and the SNR is
    exactly 20·log10(|peak|/σ̂)."""
    rng = np.random.default_rng(2)
    sigma = 0.5
    noise = rng.standard_normal(100_000) * sigma
    peak = 10.0
    expected_db = 20.0 * math.log10(peak / sigma)
    got = peak_snr_db_signed(peak, noise)
    assert got == pytest.approx(expected_db, abs=0.05)


def test_peak_snr_db_signed_uses_absolute_peak():
    """A negative peak amplitude is still a signal — use |peak|."""
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(10_000)
    pos = peak_snr_db_signed(7.0, noise)
    neg = peak_snr_db_signed(-7.0, noise)
    assert pos == pytest.approx(neg)


def test_peak_snr_db_signed_returns_nan_for_zero_std():
    """A degenerate noise region (constant) → σ̂ = 0 → NaN, not the
    old 40 dB sentinel that let artefacts through downstream gates."""
    assert math.isnan(peak_snr_db_signed(1.0, np.ones(100)))
    assert math.isnan(peak_snr_db_signed(1.0, []))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
