"""Replay-harness plumbing for the fine stage (spec §8).

The acceptance RUN uses real captures outside the repo; these tests
cover the harness functions themselves on synthetic data.
"""
import numpy as np
import pytest

import sys
from pathlib import Path

# NOTE: this repo's tests/ directory has no __init__.py and there is no
# root conftest.py, so pytest's default (prepend) import mode only ever
# puts tests/ itself on sys.path -- "tests" is never importable as a
# package (see tests/test_bpsk_pps_calibrator_diff.py, which has the same
# `from tests.<sibling> import ...` pattern and is broken the same way,
# a known pre-existing environment issue). Insert the repo root too so
# "tests" resolves as an implicit namespace package.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

from t6_estimator_sweep import replay_fine, early_late_offset
from tests.test_bpsk_edge_fine_stage import make_bpsk

SR = 96_000
EDGE = 43_181.4


class TestReplayFine:
    def test_split_windows_agree_to_1us(self):
        # Uses the harness's default batch (1740), which does NOT evenly
        # divide 8*96000: this is deliberate -- it's exactly the
        # non-dividing case that used to desync every fold block after
        # the first (BpskEdgeFineStage previously let a flush-triggering
        # batch overshoot the boundary, so `_cont` was never exactly 0
        # at a block start; fixed by splitting at the boundary -- see
        # bpsk_edge_fine_stage.py's process_samples and
        # tests/test_bpsk_edge_fine_stage.py's
        # TestFoldAcrossNonDividingBatches. Before that fix this test
        # needed batch=1920 (which divides evenly) to pass at all; see
        # task-7-report.md for the history.
        iq = make_bpsk(SR, 16, EDGE, noise_rms=0.05)
        ests = replay_fine(iq, SR, coarse_offset=EDGE + 40, fold_seconds=8)
        assert len(ests) == 2
        spread_us = abs(ests[0].edge_offset_samples
                        - ests[1].edge_offset_samples) / SR * 1e6
        assert spread_us < 1.0

    def test_early_late_agrees_with_zero_crossing(self):
        iq = make_bpsk(SR, 8, EDGE, noise_rms=0.02)
        ests = replay_fine(iq, SR, coarse_offset=EDGE, fold_seconds=8)
        # rebuild the folded average the same way to feed the EL gate
        sec = (np.arange(len(iq)) // SR)
        sign = 1 - 2 * (sec & 1)
        avg = ((iq * sign).reshape(8, SR)).mean(axis=0)
        phi = 0.5 * np.angle(np.mean(avg.astype(np.complex128) ** 2))
        I = np.real(avg * np.exp(-1j * phi))
        el = early_late_offset(I, int(EDGE), gate_ms=2.0, sample_rate=SR)
        diff_us = abs(el - ests[0].edge_offset_samples) / SR * 1e6
        # early_late_offset previously carried a deterministic +0.5
        # sample (~5.2 us @ 96 kHz) bias from its gate-boundary
        # convention (fixed: -0.5 correction in the return value; see
        # tools/t6_estimator_sweep.py and task-7-report.md). Now
        # measures ~0.007 us here; 1.0 us leaves comfortable margin
        # while still catching a real regression.
        assert diff_us < 1.0
