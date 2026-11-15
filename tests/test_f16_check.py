"""f16_check.measure: the trial's fine-detail bar on synthetic noise + carrier."""
import numpy as np

from hf_timestd.core import f16_check
from hf_timestd.core.iq_codec import F16_SCALE

SR = 24000


def _f16(x):
    s = np.float32(F16_SCALE)
    return ((x.view(np.float32) * s).astype(np.float16).astype(np.float32) / s).view(np.complex64)


def test_measure_on_noise_meets_trial_bar():
    rng = np.random.default_rng(1)
    n = SR * 60
    sigma = 10 ** (-109 / 20) / np.sqrt(2)          # RMS -109 dB re 1.0
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    t = np.arange(n) / SR
    # tone power 40 dB over the per-bin noise floor (0.75 Hz bins): A^2 = P_noise * 1e4 / 32768
    carrier = 10 ** (-109 / 20) * np.sqrt(1e4 / 32768) * np.exp(2j * np.pi * 5.0 * t)
    x = (noise + carrier).astype(np.complex64)
    r = f16_check.measure(x, _f16(x), SR)
    assert r["seconds"] == 60
    assert r["err_over_floor_db_max"] < -40
    assert r["carrier_dphi_rms_mrad"] < 1


def test_measure_short_chunk_uses_whole_chunk():
    rng = np.random.default_rng(2)
    x = (1e-4 * (rng.standard_normal(SR * 5) + 1j * rng.standard_normal(SR * 5))).astype(np.complex64)
    r = f16_check.measure(x, _f16(x), SR)
    assert r["seconds"] == 5


def _coarse(x, bits=6):
    """Mutation: round to `bits` mantissa bits (F16 keeps 10)."""
    m, e = np.frexp(x.view(np.float32).astype(np.float64))
    return np.ldexp(np.round(m * 2 ** bits) / 2 ** bits, e).astype(np.float32).view(np.complex64)


def _carrier(cn0_dbhz, fade, seed=3):
    rng = np.random.default_rng(seed)
    n = SR * 60
    t = np.arange(n) / SR
    sigma = 10 ** (-109 / 20) / np.sqrt(2)
    noise = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    amp = np.sqrt(2 * sigma ** 2 / SR * 10 ** (cn0_dbhz / 10))
    env = np.abs(np.sin(2 * np.pi * 0.05 * t)) ** 3 if fade else 1.0   # fades to zero
    return (noise + amp * env * np.exp(2j * np.pi * 3.0 * t)).astype(np.complex64)


def test_deep_fades_pass_the_phase_ratio():
    """ND 20 MHz, 2026-11-14: fade blocks failed a flat 1 mrad; against each
    block's thermal phase noise F16 stays far under."""
    x = _carrier(60, fade=True)
    r = f16_check.measure(x, _f16(x), SR)
    assert r["carrier_dphi_over_thermal_rms"] < 0.01
    assert r["err_over_floor_db_mean"] < -50
    assert r["err_over_floor_db_mean"] < r["err_over_floor_db_max"]


def test_strong_carrier_passes_new_limits():
    x = _carrier(80, fade=False)
    r = f16_check.measure(x, _f16(x), SR)
    assert r["err_over_floor_db_mean"] < -30
    assert r["carrier_dphi_over_thermal_rms"] < 0.03


def test_coarse_rounding_fails_both_new_metrics():
    """Mutation: 6 mantissa bits must fail where F16's 10 pass."""
    x = _carrier(65, fade=False)
    ok = f16_check.measure(x, _f16(x), SR)
    bad = f16_check.measure(x, _coarse(x), SR)
    assert ok["err_over_floor_db_mean"] < -30 < bad["err_over_floor_db_mean"]
    assert ok["carrier_dphi_over_thermal_rms"] < 0.03 < bad["carrier_dphi_over_thermal_rms"]
