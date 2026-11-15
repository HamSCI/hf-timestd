"""Fine-detail check for F16 archive chunks.

Measures what the F16 rounding costs, on one chunk's first minute:

* ``err_over_floor_db_max``: the rounding error's Welch PSD against the
  signal's own noise floor, worst bin inside |f| < 4.5 kHz (carrier
  included).  Negative means the error sits below the receiver noise.
* ``err_over_floor_db_mean``: the same ratio averaged over the passband:
  what the rounding adds to the noise.  The worst bin of ~6000 Welch
  estimates runs about 2 dB over it by chance.  The report judges this one.
* ``carrier_dphi_rms_mrad``: the carrier decimated to 10 Hz (block mean of
  ``sample_rate // 10`` samples), rms phase difference between x and y.
* ``carrier_dphi_over_thermal_rms``: each block's phase difference over that
  block's own thermal phase noise, rms over the blocks.  A flat mrad limit
  fails deep fades, where the thermal phase noise reaches radians; the
  ratio says whether F16 adds anything a measurement could see.  The report
  judges this one.

Method follows tasks/f16-archive/finedetail.py.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

_NPERSEG = 32768
_PASSBAND_HZ = 4500.0
_CARRIER_HZ = 20.0
_CARRIER_RATE_HZ = 10


def _finite(v):
    """A float, or None (JSON null) when v is NaN or infinite."""
    v = float(v)
    return v if np.isfinite(v) else None


def measure(x: np.ndarray, y: np.ndarray, sample_rate: int) -> dict:
    """Compare stored samples ``y`` against the original ``x``.

    Uses the first 60 s (or all of a shorter chunk).  A metric that is not
    finite (silent or NaN chunk) comes back as None.
    """
    n = min(len(x), len(y), int(sample_rate) * 60)
    xs = np.asarray(x[:n], dtype=np.complex128)
    ys = np.array(y[:n], dtype=np.complex128)   # a copy: the error overwrites it

    nper = min(_NPERSEG, n)
    fx, px = signal.welch(xs, fs=sample_rate, nperseg=nper,
                          return_onesided=False, scaling="density")
    passband = np.abs(fx) < _PASSBAND_HZ
    carrier = np.abs(fx) < _CARRIER_HZ
    with np.errstate(all="ignore"):
        floor = np.median(px[passband & ~carrier])
    floor_ok = bool(np.isfinite(floor) and floor > 0)

    blk = max(1, int(sample_rate) // _CARRIER_RATE_HZ)
    nb = n // blk * blk
    dphi_mrad = ratio = None
    if nb:
        with np.errstate(all="ignore"):
            cx = xs[:nb].reshape(-1, blk).mean(axis=1)
            cy = ys[:nb].reshape(-1, blk).mean(axis=1)
            dphi = np.angle(cy * np.conj(cx))
            dphi_mrad = _finite(1000.0 * np.sqrt(np.mean(dphi ** 2)))
            if floor_ok:
                # White noise of density `floor` has per-sample variance
                # floor * fs; a block mean divides it by blk, and half of it
                # lies across the carrier's phase.
                thermal = np.sqrt(floor * sample_rate / blk / 2.0) / np.abs(cx)
                ratio = _finite(np.sqrt(np.mean((dphi / thermal) ** 2)))

    # The error reuses ys's buffer, and xs goes before the second Welch,
    # so at most two minute-long complex128 arrays live at once.
    e = np.subtract(xs, ys, out=ys)
    del xs, ys
    _, pe = signal.welch(e, fs=sample_rate, nperseg=nper,
                         return_onesided=False, scaling="density")
    del e
    err_db = err_mean_db = None
    if floor_ok:
        with np.errstate(all="ignore"):
            err_db = _finite(np.max(10.0 * np.log10(pe[passband] / floor)))
            err_mean_db = _finite(10.0 * np.log10(np.mean(pe[passband]) / floor))

    return {
        "err_over_floor_db_max": err_db,
        "err_over_floor_db_mean": err_mean_db,
        "carrier_dphi_rms_mrad": dphi_mrad,
        "carrier_dphi_over_thermal_rms": ratio,
        "seconds": int(n // sample_rate),
    }
