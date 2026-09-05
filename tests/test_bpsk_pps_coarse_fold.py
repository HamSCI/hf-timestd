"""Coherent folding for the T6 COARSE stage.

2026-08-28 (reference_t6_cn0_cliff_agc): the coarse matched filter integrates
one half-second per edge and falls off a stochastic lock cliff near 58 dB-Hz;
AC0G-B4's pilot sits at 48-57 dB-Hz after dark and the detector floods with
noise edges (2026-09-05 01:04Z: 104 ok / 1349 noise).  Folding the MF output
over K seconds modulo the sample rate, sign-alternated per second, gains
10*log10(K) dB -- 17.8 dB at K = 60 -- and moves the cliff to ~40 dB-Hz.
The fold grants the same lock a run of consecutive edges would; a fold with
no edge in it grants nothing.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bpsk_pps_calibrator_mf import _make_bpsk_signal  # noqa: E402

from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF  # noqa: E402

SR = 96000
BATCH = 1920
EDGE = 47916.1672
TOL = 30            # edge_tolerance_samples at 96 kHz (B4's config)


def _noise_std_for(cn0_db_hz: float) -> float:
    """Per-component complex-noise sigma giving this C/N0 at SR (A = 1)."""
    snr = 10 ** ((cn0_db_hz - 10 * math.log10(SR)) / 10.0)
    return 1.0 / math.sqrt(2.0 * snr)


def _drive(cal, signal):
    """Feed a signal in BATCH-sized pieces; return (first lock batch, last result)."""
    first_lock = None
    last = None
    for k, i in enumerate(range(0, len(signal), BATCH)):
        r = cal.process_samples(signal[i:i + BATCH], i)
        if r is not None:
            last = r
            if first_lock is None and r.locked:
                first_lock = k
    return first_lock, last


def _wrapped(a, b, period=SR):
    d = (a - b) % period
    return d - period if d >= period / 2 else d


@pytest.mark.slow
def test_fold_locks_at_50_dbhz_where_the_edge_detector_cannot():
    sig = _make_bpsk_signal(duration_s=130.0, sample_rate=SR, edge_offset_samples=EDGE,
                            noise_std=_noise_std_for(50.0), seed=11)
    legacy = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL,
                                 fold_seconds=0)
    folded = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL,
                                 fold_seconds=60)
    lock_legacy, _ = _drive(legacy, sig)
    lock_fold, res = _drive(folded, sig)
    assert lock_legacy is None, "the per-edge detector locked at 50 dB-Hz; the premise changed"
    assert lock_fold is not None and res is not None and res.locked
    assert abs(_wrapped(res.chain_delay_samples, EDGE)) <= TOL
    assert folded.fold_locks >= 1 and folded.fold_last_snr >= folded.fold_min_snr


@pytest.mark.slow
def test_fold_does_not_lock_on_noise_alone():
    rng = np.random.default_rng(5)
    n = int(130.0 * SR)
    noise = (rng.normal(0, 0.5, n) + 1j * rng.normal(0, 0.5, n)).astype(np.complex64)
    cal = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL, fold_seconds=60)
    lock, res = _drive(cal, noise)
    assert lock is None and res is None
    # On noise alone the Costas loop never reports lock, so the fold never
    # accumulates; if it ever did evaluate, it must not have passed.
    assert cal.fold_locks == 0
    assert cal.fold_last_snr is None or cal.fold_last_snr < cal.fold_min_snr


def test_fold_leaves_a_clean_lock_where_it_was():
    sig = _make_bpsk_signal(duration_s=15.0, sample_rate=SR, edge_offset_samples=EDGE, noise_std=0.1)
    legacy = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL, fold_seconds=0)
    folded = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL, fold_seconds=60)
    _, r0 = _drive(legacy, sig)
    _, r1 = _drive(folded, sig)
    assert r0 is not None and r1 is not None and r0.locked and r1.locked
    assert abs(r0.chain_delay_samples - r1.chain_delay_samples) < 0.01
    assert folded.fold_locks == 0        # the fold window never elapsed; the edges did the work


def test_fold_is_off_when_fold_seconds_is_zero_and_reset_clears_it():
    cal = BpskPpsCalibratorMF(SR, fold_seconds=60)
    assert cal.fold_seconds == 60 and cal.fold_evaluations == 0
    cal.reset()
    assert cal.fold_evaluations == 0 and cal.fold_locks == 0
    off = BpskPpsCalibratorMF(SR, fold_seconds=0)
    assert off.fold_seconds == 0


@pytest.mark.slow
def test_fold_evaluator_rejects_noise_in_magnitude_mode():
    """Magnitude mode folds without the Costas gate, so pure noise reaches
    the evaluator; its apex must not clear fold_min_snr."""
    rng = np.random.default_rng(7)
    n = int(65.0 * SR)
    noise = (rng.normal(0, 0.5, n) + 1j * rng.normal(0, 0.5, n)).astype(np.complex64)
    cal = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL,
                              fold_seconds=60, use_magnitude_correlation=True)
    lock, res = _drive(cal, noise)
    assert lock is None and res is None
    assert cal.fold_evaluations >= 1 and cal.fold_locks == 0
    assert cal.fold_last_snr < cal.fold_min_snr


@pytest.mark.slow
@pytest.mark.parametrize("cn0", [45.0, 40.0])
def test_fold_floor_reaches_40_dbhz_with_white_noise(cn0):
    """The 2026-08-28 estimate: 60 s of folding moves the cliff to ~40 dB-Hz.
    White noise over the full 96 kHz; the real channel's noise is confined
    to +-25 kHz and correlated, so allow a few dB on the station."""
    sig = _make_bpsk_signal(duration_s=65.0, sample_rate=SR, edge_offset_samples=EDGE,
                            noise_std=_noise_std_for(cn0), seed=23)
    cal = BpskPpsCalibratorMF(SR, consecutive_required=10, edge_tolerance_samples=TOL, fold_seconds=60)
    lock, res = _drive(cal, sig)
    assert lock is not None and res is not None and res.locked
    assert cal.fold_locks == 1
    assert abs(_wrapped(res.chain_delay_samples, EDGE)) <= 2.0    # sub-sample class, not just in tolerance
