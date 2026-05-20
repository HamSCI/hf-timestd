#!/usr/bin/env python3
"""
Correlation-peak SNR — single source of truth (review item S4).

Three modules computed peak SNR three incompatible ways:

* ``tick_edge_detector``:  peak / median(envelope)
* ``tick_matched_filter``: peak / std(envelope)
* ``metrology_engine``:    peak / median(envelope)

So the three sites disagreed by 1–5 dB and the contract's "≥ 10 dB SNR"
target was ambiguous. The canonical definition (per the review) is::

    SNR_dB = 20 · log10(|peak| / σ̂)

where ``σ̂`` is an estimate of the *underlying* noise std σ. Two
estimators are needed because the codebase has two noise distributions:

* Rayleigh envelope (modulus of a zero-mean complex Gaussian, e.g. the
  envelope of a complex-IQ matched-filter output): ``median ≈ σ·√(2 ln 2)``,
  so ``σ̂ = median(env) / 1.1774``. This is robust against outliers —
  other peaks in the noise region don't pull σ̂ high the way the mean or
  std would.

* Zero-mean signed Gaussian (e.g. the raw real-valued correlation of an
  AM-demodulated signal against a real template): ``σ̂ = std(samples)``,
  the standard sample standard deviation.

Both estimators end at the *same* reported SNR units — ``peak/σ`` —
so cross-site comparison and the contract's "≥ 10 dB" gate are now
unambiguous.
"""

import math
from typing import Sequence, Union

import numpy as np

# median(Rayleigh) = σ · √(2 · ln 2); to recover σ, divide.
_RAYLEIGH_MEDIAN_FACTOR = math.sqrt(2.0 * math.log(2.0))  # ≈ 1.1774

_FloatArray = Union[np.ndarray, Sequence[float]]


def rayleigh_envelope_sigma(envelope_samples: _FloatArray) -> float:
    """
    Estimate the underlying noise σ from a Rayleigh-distributed envelope.

    ``σ̂ = median(envelope) / √(2 · ln 2)`` — robust to outliers in the
    noise region. Returns 0.0 for an empty input.
    """
    arr = np.asarray(envelope_samples, dtype=np.float64).ravel()
    if arr.size == 0:
        return 0.0
    return float(np.median(arr)) / _RAYLEIGH_MEDIAN_FACTOR


def peak_snr_db_envelope(
    peak: float,
    noise_envelope: _FloatArray,
    *,
    sigma_floor: float = 1e-10,
) -> float:
    """
    Canonical peak SNR (dB) when the noise is a Rayleigh envelope —
    i.e. the modulus of zero-mean complex Gaussian noise, as produced
    by complex-IQ matched filtering.

    Returns ``20·log10(|peak| / σ̂)`` with σ̂ via
    :func:`rayleigh_envelope_sigma`. Returns ``float('nan')`` when σ̂
    cannot be estimated (empty / non-positive-median noise region) —
    callers can treat the SNR as unknown rather than accept a
    misleading sentinel.
    """
    sigma = rayleigh_envelope_sigma(noise_envelope)
    if sigma <= 0:
        return float("nan")
    return 20.0 * math.log10(abs(float(peak)) / max(sigma, sigma_floor))


def peak_snr_db_signed(
    peak: float,
    noise_samples: _FloatArray,
    *,
    sigma_floor: float = 1e-10,
) -> float:
    """
    Canonical peak SNR (dB) when the noise is zero-mean signed Gaussian —
    e.g. the raw real-valued correlation of an AM-demodulated signal
    against a real template, where ``std(samples)`` is a direct estimate
    of σ.

    Returns ``20·log10(|peak| / σ̂)`` with ``σ̂ = std(noise_samples)``.
    Returns ``float('nan')`` when σ̂ cannot be estimated (empty input /
    zero std).
    """
    arr = np.asarray(noise_samples, dtype=np.float64).ravel()
    if arr.size == 0:
        return float("nan")
    sigma = float(np.std(arr))
    if sigma <= 0:
        return float("nan")
    return 20.0 * math.log10(abs(float(peak)) / max(sigma, sigma_floor))
