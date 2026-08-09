"""Fold/continuity accumulation for the T6 fine-stage estimator.

Spec: docs/design/T6_ANCHOR_INVERSION_DESIGN.md §3.
"""
import numpy as np
import pytest

from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage

SR = 96_000


def feed_batches(stage, iq, rtp0, batch=1740, mislabel=None):
    """Feed iq in batches with declared RTP = rtp0 + offset (+ mislabel[i])."""
    i = 0
    b = 0
    out = []
    while i < len(iq):
        chunk = iq[i:i + batch]
        decl = (rtp0 + i) & 0xFFFFFFFF
        if mislabel and b in mislabel:
            decl = (decl + mislabel[b]) & 0xFFFFFFFF
        r = stage.process_samples(chunk, decl)
        if r is not None:
            out.append(r)
        i += batch
        b += 1
    return out


class TestFoldAccumulation:
    def test_continuity_counter_advances_by_samples_received(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=1000)
        assert stage._cont == SR

    def test_fold_sign_alternates_per_second(self):
        # Constant +1 signal folded over 2 s with (-1)^n cancels to ~0;
        # a signal that flips polarity each second folds coherently.
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        const = np.ones(2 * SR, dtype=np.complex64)
        feed_batches(stage, const, rtp0=0)
        avg_const = stage._last_avg_for_test  # captured before reset (see Step 3)
        assert np.max(np.abs(avg_const)) < 1e-6

        stage2 = BpskEdgeFineStage(SR, fold_seconds=2)
        alt = np.ones(2 * SR, dtype=np.complex64)
        alt[SR:] = -1.0
        feed_batches(stage2, alt, rtp0=0)
        avg_alt = stage2._last_avg_for_test
        assert np.min(np.abs(avg_alt)) > 0.99

    def test_batch_spanning_second_boundary_splits_sign_correctly(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        alt = np.ones(2 * SR, dtype=np.complex64)
        alt[SR:] = -1.0
        # 7-sample-offset start so batches straddle the boundary
        feed_batches(stage, alt, rtp0=0, batch=1747)
        assert np.min(np.abs(stage._last_avg_for_test)) > 0.99


class TestRtpRegistration:
    def test_registration_median_ignores_minority_mislabels(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        iq = np.zeros(2 * SR, dtype=np.complex64)
        n_batches = int(np.ceil(len(iq) / 1740))
        # 9% of batches declare RTP 60 samples high (measured B4 signature)
        bad = set(range(0, n_batches, 11))
        feed_batches(stage, iq, rtp0=5_000_000, mislabel={b: +60 for b in bad})
        assert stage._registration_for_test() == 5_000_000

    def test_registration_spread_beyond_limit_discards_block(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=1)
        iq = np.zeros(SR, dtype=np.complex64)
        # A mid-block +500-sample declared jump = real gap signature
        n_batches = int(np.ceil(len(iq) / 1740))
        feed_batches(stage, iq, rtp0=0,
                     mislabel={b: +500 for b in range(n_batches // 2, n_batches)})
        assert stage.blocks_discarded == 1

    def test_two_second_declared_jump_resets_stage(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=4)
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=0)
        cont_before = stage._cont
        assert cont_before == SR
        # stream restart: declared RTP jumps 3 s
        stage.process_samples(np.zeros(1740, dtype=np.complex64),
                              (SR + 3 * SR) & 0xFFFFFFFF)
        assert stage._cont == 1740  # counted from fresh reset

    def test_rtp_wrap_in_declared_timestamps_is_handled(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=1)
        rtp0 = (2**32 - SR // 2) & 0xFFFFFFFF  # wraps mid-block
        iq = np.zeros(SR, dtype=np.complex64)
        feed_batches(stage, iq, rtp0=rtp0)
        assert stage.blocks_discarded == 0
        assert stage._registration_for_test() == rtp0
