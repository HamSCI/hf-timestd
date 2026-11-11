"""Fine-detail check for F16 archive chunks.

Measures what the F16 rounding costs, on one chunk's first minute:

* ``err_over_floor_db_max``: the rounding error's Welch PSD against the
  signal's own noise floor, worst bin inside |f| < 4.5 kHz (carrier
  included).  Negative means the error sits below the receiver noise.
* ``carrier_dphi_rms_mrad``: the carrier decimated to 10 Hz (block mean of
  ``sample_rate // 10`` samples), rms phase difference between x and y.

Method follows tasks/f16-archive/finedetail.py.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

_NPERSEG = 32768
_PASSBAND_HZ = 4500.0
_CARRIER_HZ = 20.0
_CARRIER_RATE_HZ = 10


def measure(x: np.ndarray, y: np.ndarray, sample_rate: int) -> dict:
    """Compare stored samples ``y`` against the original ``x``.

    Uses the first 60 s (or all of a shorter chunk).
    """
    n = min(len(x), len(y), int(sample_rate) * 60)
    xs = np.asarray(x[:n], dtype=np.complex128)
    ys = np.asarray(y[:n], dtype=np.complex128)
    e = xs - ys

    nper = min(_NPERSEG, n)
    fx, px = signal.welch(xs, fs=sample_rate, nperseg=nper,
                          return_onesided=False, scaling="density")
    _, pe = signal.welch(e, fs=sample_rate, nperseg=nper,
                         return_onesided=False, scaling="density")
    passband = np.abs(fx) < _PASSBAND_HZ
    carrier = np.abs(fx) < _CARRIER_HZ
    floor = np.median(px[passband & ~carrier])
    with np.errstate(divide="ignore"):
        err_db = float(np.max(10.0 * np.log10(pe[passband] / floor)))

    blk = max(1, int(sample_rate) // _CARRIER_RATE_HZ)
    nb = n // blk * blk
    if nb:
        cx = xs[:nb].reshape(-1, blk).mean(axis=1)
        cy = ys[:nb].reshape(-1, blk).mean(axis=1)
        dphi_mrad = float(1000.0 * np.sqrt(np.mean(np.angle(cy * np.conj(cx)) ** 2)))
    else:
        dphi_mrad = 0.0

    return {
        "err_over_floor_db_max": err_db,
        "carrier_dphi_rms_mrad": dphi_mrad,
        "seconds": int(n // sample_rate),
    }
