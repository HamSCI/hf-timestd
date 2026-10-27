"""T6 health reaches the judge: re-home counter and bench health detail."""

import numpy as np

from hf_timestd.core.bpsk_pps_calibrator_mf import (
    BpskPpsCalibratorMF,
    STEP_CONFIRM_EDGES,
)
from hf_timestd.core.native_anchor import NativeAnchor
from hf_timestd.core.offset_judge import NativeAnchorBench

SR = 96_000


def _acquired_calibrator() -> BpskPpsCalibratorMF:
    cal = BpskPpsCalibratorMF(
        sample_rate=SR, consecutive_required=10, edge_tolerance_samples=30,
    )
    cal._last_edge_rtp = 1_000_000
    cal.pps_consecutive = cal.consecutive_required
    cal.pps_ok = 50
    cal._acquired = True
    cal._costas_locked = True
    cal._peak_running = 100.0
    cal._chain_delay_samples = float(1_000_000 % SR)
    return cal


def _drive_one_peak(cal, rtp_at_peak, magnitude=120.0):
    window_len, pi = 1024, 100
    y = np.zeros(window_len, dtype=np.float64)
    for k in range(-3, 4):
        y[pi + k] = magnitude * (1.0 - 0.05 * abs(k))
    rtp = (np.arange(window_len, dtype=np.int64) - pi + rtp_at_peak) & 0xFFFFFFFF
    cal._detect_and_record_peaks(y, rtp)


def _bench(health_provider=None):
    wall, mono = 1_800_000_000.0, 100.0
    anchor = NativeAnchor(
        anchor_rtp=5000,
        anchor_utc_ns=int(round((wall - 20.0) * 1e9)),
        sample_rate_hz=SR,
        chain_delay_ns=0,
        captured_at_utc_ns=int(round((wall - 20.0) * 1e9)),
        captured_via_tier="T5",
    )
    return NativeAnchorBench(
        provider=lambda: (anchor, 5000 + 20 * SR, mono),
        mono_fn=lambda: mono,
        health_provider=health_provider,
    )


def test_rehome_counter_moves():
    cal = _acquired_calibrator()
    assert cal.rehome_count == 0
    assert cal.last_rehome_mono is None
    ref0 = cal._last_edge_rtp
    for i in range(STEP_CONFIRM_EDGES):
        _drive_one_peak(cal, ref0 + (i + 1) * SR + 5_000)
    assert cal.rehome_count == 1
    assert cal.last_rehome_mono is not None


def test_bench_carries_health():
    health = {"authority_state": "AUTHORITATIVE", "state_for_s": 700.0,
              "rehome_age_s": None}
    r = _bench(lambda: health).poll()
    assert r is not None
    assert r.detail["t6_health"]["authority_state"] == "AUTHORITATIVE"
    assert r.detail["t6_health"]["state_for_s"] == 700.0
    assert "authoritative" not in r.detail


def test_bench_without_health_provider_has_no_health():
    r = _bench().poll()
    assert r is not None
    assert "t6_health" not in r.detail


def test_bench_health_provider_returning_none_or_raising():
    assert "t6_health" not in _bench(lambda: None).poll().detail

    def boom():
        raise RuntimeError("x")
    assert "t6_health" not in _bench(boom).poll().detail
