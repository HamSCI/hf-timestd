"""tools/t6_two_channel_offset.py: recover a known edge offset between two
synthetic TS-1 channels whose RTP origins differ, as two radiod channels' do."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from tests.test_bpsk_edge_fine_stage import make_bpsk

TOOL = Path(__file__).resolve().parents[1] / "tools" / "t6_two_channel_offset.py"
spec = importlib.util.spec_from_file_location("t6_two_channel_offset", TOOL)
tco = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tco)

SR = 96_000
OLEN = 1920            # 96 kHz x 20 ms
SECONDS = 40
FOLD = 10
EDGE = 31_337.0        # ref edge, samples into the second
TRUE_DELTA = 0.355     # samples: 3.70 us, the test edge arrives later


def _write(d: Path, name: str, x: np.ndarray, rtp0: int, batch: int = 1800):
    x.astype(np.complex64).tofile(d / f"{name}.c64")
    lines = [f"{(rtp0 + i) & 0xFFFFFFFF} {min(batch, len(x) - i)}"
             for i in range(0, len(x), batch)]
    (d / f"{name}.batches").write_text("\n".join(lines) + "\n")


def _recording(tmp_path, ref_rtp0, test_rtp0, test_cfo_hz=0.0, noise=0.05):
    ref = make_bpsk(SR, SECONDS, EDGE, noise_rms=noise, seed=1)
    test = make_bpsk(SR, SECONDS, EDGE + TRUE_DELTA, phase=1.1,
                     noise_rms=noise, seed=2)
    if test_cfo_hz:
        t = np.arange(len(test)) / SR
        test = (test * np.exp(2j * np.pi * test_cfo_hz * t)).astype(np.complex64)
    # Both arrays start at the same instant of the shared ADC clock; only
    # the RTP labels differ.
    _write(tmp_path, "ref", ref, ref_rtp0)
    _write(tmp_path, "test", test, test_rtp0)
    (tmp_path / "meta.json").write_text(json.dumps({
        "ref_hz": 45_375_000, "test_hz": 28_600_000,
        "sample_rate": SR, "olen": OLEN}))
    return tmp_path


def _delta(rep):
    return np.mean([r["delta_samples"] for r in rep["blocks"]])


def test_reduce_difference_removes_whole_blocks_across_the_wrap():
    # test channel started 7 blocks later, and its counter sits on the far
    # side of the 32-bit wrap from the ref's.
    raw = (7 * OLEN + TRUE_DELTA) - (1 << 32)
    d32, red = tco.reduce_difference(raw, OLEN)
    assert d32 == pytest.approx(7 * OLEN + TRUE_DELTA)
    assert red == pytest.approx(TRUE_DELTA)


def test_known_offset_recovered_with_different_origins(tmp_path):
    # radiod starts each channel at 2**32 - olen on its first block; the
    # test channel began 3 blocks after the ref.
    ref0 = (1 << 32) - OLEN + 5_000
    test0 = (ref0 - 3 * OLEN) & 0xFFFFFFFF
    rep = tco.analyze(_recording(tmp_path, ref0, test0), FOLD, cfo=True)
    assert len(rep["blocks"]) == SECONDS // FOLD
    assert _delta(rep) == pytest.approx(TRUE_DELTA, abs=0.02)
    for r in rep["blocks"]:
        assert r["blocks_apart"] == pytest.approx(-3, abs=0.01)


def test_origins_straddling_the_wrap(tmp_path):
    # ref labels sit just below 2**32, test labels just above 0: the two
    # stages unwrap from opposite sides.  Without the mod-2**32 step this
    # reads 256 samples (2.7 ms) off.
    ref0 = (1 << 32) - 4 * SR
    test0 = 2 * OLEN
    rep = tco.analyze(_recording(tmp_path, ref0, test0), FOLD, cfo=True)
    assert _delta(rep) == pytest.approx(TRUE_DELTA, abs=0.02)


def test_carrier_offset_is_estimated_and_removed(tmp_path):
    d = _recording(tmp_path, 1_000_000, 1_000_000 + 521 * OLEN, test_cfo_hz=0.37)
    rep = tco.analyze(d, FOLD, cfo=True)
    assert rep["channels"]["test"]["carrier_offset_hz"] == pytest.approx(0.37, abs=0.005)
    assert rep["channels"]["ref"]["carrier_offset_hz"] == pytest.approx(0.0, abs=0.005)
    assert _delta(rep) == pytest.approx(TRUE_DELTA, abs=0.02)
    assert min(r["test_retention"] for r in rep["blocks"]) > 0.9


def test_without_the_correction_the_fold_loses_the_drifting_carrier(tmp_path):
    d = _recording(tmp_path, 1_000_000, 1_000_000 + 521 * OLEN, test_cfo_hz=0.37)
    rep = tco.analyze(d, FOLD, cfo=False)
    rets = [r["test_retention"] for r in rep["blocks"]]
    # Either no block survives, or what survives kept little of the carrier.
    assert not rets or max(rets) < 0.5


class _Q:
    def __init__(self, rtp):
        self.delivered_rtp_start = rtp


def test_sink_flags_a_doubled_sample_count(tmp_path):
    # The 2026-10-11 failure: packets and RTP steps as usual, but each batch
    # holds twice the samples the RTP clock advanced.
    ok, bad = tco._Sink(tmp_path, "ok"), tco._Sink(tmp_path, "bad")
    rtp = (1 << 32) - 3 * 1740          # cross the wrap on the way
    for _ in range(8):
        ok(np.zeros(1740, np.complex64), _Q(rtp))
        bad(np.zeros(3480, np.complex64), _Q(rtp))
        rtp = (rtp + 1740) & 0xFFFFFFFF
    ok.close(), bad.close()
    assert ok.miscounts == 0
    assert bad.miscounts >= tco.MISCOUNT_LIMIT
