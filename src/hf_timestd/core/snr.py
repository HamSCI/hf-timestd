#!/usr/bin/env python3
"""
Correlation-peak SNR — single source of truth (review item S4).

This module is now a thin re-export shim over :mod:`hamsci_dsp.dsp`, the
canonical shared home for the peak-SNR estimators (extracted math-identical
from this file). The definitions below live in hamsci-dsp; hf-timestd imports
them so a fix propagates suite-wide without divergent copies.

The canonical definition (per the review) is::

    SNR_dB = 20 · log10(|peak| / σ̂)

where ``σ̂`` estimates the *underlying* noise std σ. Two estimators cover the
two noise distributions in the codebase:

* Rayleigh envelope (modulus of zero-mean complex Gaussian, e.g. complex-IQ
  matched-filter output): ``σ̂ = median(env) / √(2 ln 2)`` — robust to outliers.
  → :func:`rayleigh_envelope_sigma`, :func:`peak_snr_db_envelope`.
* Zero-mean signed Gaussian (e.g. raw real correlation of an AM-demodulated
  signal against a real template): ``σ̂ = std(samples)``.
  → :func:`peak_snr_db_signed`.

Both end at the same reported units (``peak/σ``) so cross-site comparison and
the contract's "≥ 10 dB" gate stay unambiguous.
"""

from hamsci_dsp.dsp import (
    _RAYLEIGH_MEDIAN_FACTOR,
    peak_snr_db_envelope,
    peak_snr_db_signed,
    rayleigh_envelope_sigma,
)

__all__ = [
    "rayleigh_envelope_sigma",
    "peak_snr_db_envelope",
    "peak_snr_db_signed",
    "_RAYLEIGH_MEDIAN_FACTOR",
]
