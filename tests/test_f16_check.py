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
