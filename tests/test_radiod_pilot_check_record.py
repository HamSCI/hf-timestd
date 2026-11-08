"""check_record's two signal measurements, on synthetic signals with a known answer."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from radiod_pilot import check_record as cr  # noqa: E402


def bandlimited_flip(n, at, fs=96_000, bw=25_000, phase=0.7, amp=4e-4):
    """A carrier whose polarity flips at fractional sample ``at``, band-limited
    to +-bw by an ideal (FFT) filter, as radiod's channel filter does."""
    from scipy.special import sici
    k = np.arange(n) - at
    # an ideal low-pass of a +-1 step: 2 Si(2 pi B t) / pi
    step = 2 * sici(np.pi * 2 * bw / fs * k)[0] / np.pi
    return amp * step * np.exp(1j * phase)


@pytest.mark.parametrize("at", [5000.0, 5000.25, 5000.5, 5000.8125])
def test_edge_position_finds_a_subsample_flip(at):
    y = bandlimited_flip(10_000, at)
    e = cr.edge_position(y, at + 0.4)
    assert e == pytest.approx(at, abs=2e-3)      # 2e-3 samples = 21 ns at 96 kHz


def test_edge_position_either_polarity():
    y = bandlimited_flip(10_000, 5000.3)
    assert cr.edge_position(-y, 5000.0) == pytest.approx(5000.3, abs=2e-3)


def _tick_side(label_offset_samples, fs=24_000, nsec=10, delay_n=102):
    """A tick burst delay_n samples after each second of the sample grid; the
    labelled second sits ``label_offset_samples`` before the grid's second."""
    n = np.arange(fs * nsec)
    env = np.ones(len(n))
    for s in range(nsec):
        on = (n >= s * fs + delay_n) & (n < s * fs + delay_n + 120)
        env[on] += 0.5 * np.sin(2 * np.pi * 1000 * (n[on] - s * fs - delay_n) / fs)
    period = 1e9 / fs
    side = {"output_samprate": fs, "sample_period_ns": period, "chain_delay_ns": 0.0,
            "window": {"seconds": nsec},
            "first_sample": {"after_window_start_ns": label_offset_samples * period}}
    return env * np.exp(1j * 0.3), side


def test_tick_arrival_measures_a_known_delay():
    """Absolute: within 0.03 ms.  The analytic template also meets the real
    tick's negative-frequency half, which skews the correlation triangle by
    up to about half a sample (21 us at 24 kHz); proof2 quotes that limit."""
    x, side = _tick_side(0.0)
    r = cr.tick_arrival(x, side, 1000.0, start_second_of_minute=1)
    assert r["arrival_ms_after_labelled_second"] == pytest.approx(102 / 24, abs=0.03)
    assert r["seconds_folded"] == 9          # second 0 has no 10 ms before it


def test_tick_arrival_follows_the_label_sub_sample():
    """Relative: moving the label by 0.4 sample moves the arrival by exactly that."""
    a = cr.tick_arrival(*_tick_side(0.0), 1000.0, start_second_of_minute=1)
    b = cr.tick_arrival(*_tick_side(0.4), 1000.0, start_second_of_minute=1)
    d = b["arrival_ms_after_labelled_second"] - a["arrival_ms_after_labelled_second"]
    assert d == pytest.approx(0.4 / 24, abs=1e-6)
