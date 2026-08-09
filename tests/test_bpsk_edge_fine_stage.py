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


def make_bpsk(sr, seconds, edge_offset_samples, edge_width_us=52.0,
              amp=1.0, tilt_per_s=0.0, phase=0.3, noise_rms=0.0,
              start_second=0, seed=1):
    """Synthetic TS-1 baseband: one smooth polarity flip per second at
    edge_offset_samples, base polarity alternating per second (so the
    signal is continuous across second boundaries), constant carrier
    phase, optional linear amplitude tilt across each second and AWGN.

    10–90% transition width edge_width_us (tanh ramp; 10–90% of tanh
    spans 2.197·tau, so tau = width/2.197).
    """
    n = sr * seconds
    t = np.arange(n)
    sec = t // sr + start_second
    pos = t % sr
    base = (1 - 2 * (sec & 1)).astype(np.float64)
    tau = (edge_width_us * 1e-6 * sr) / 2.197
    ramp = np.tanh((pos - edge_offset_samples) / tau)
    env = amp * (1.0 + tilt_per_s * (pos / sr))
    sig = env * base * ramp * np.exp(1j * phase)
    if noise_rms:
        rng = np.random.default_rng(seed)
        sig = sig + noise_rms * (rng.standard_normal(n)
                                 + 1j * rng.standard_normal(n)) / np.sqrt(2)
    return sig.astype(np.complex64)


def run_stage(sr, iq, coarse, fold_seconds, rtp0=123_456, **stage_kw):
    """``coarse`` is given here in the FOLD domain (position within the
    second measured from the start of the fed array, which is what
    ``make_bpsk`` parameterises).  The stage's public API takes the
    **RTP domain** — ``rtp % sample_rate``, the domain
    ``BpskPpsCalibratorMF._chain_delay_samples`` publishes — so
    translate with the registration, which for ``feed_batches`` is
    exactly ``rtp0``.  The default rtp0 is deliberately not a multiple
    of the sample rate (123456 % 96000 = 27456), so every test routed
    through here exercises a real domain difference.  Before the
    RTP-domain handoff fix these tests passed a fold-domain coarse
    straight in; that premise was the bug."""
    stage = BpskEdgeFineStage(sr, fold_seconds=fold_seconds, **stage_kw)
    stage.set_coarse_offset_samples((rtp0 + coarse) % sr)
    ests = feed_batches(stage, iq, rtp0=rtp0)
    return stage, ests


class TestLocalisation:
    EDGE = 43_181.4  # arbitrary fractional position within the second

    def test_clean_signal_localises_to_subsample(self):
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.05)
        _, ests = run_stage(SR, iq, coarse=self.EDGE + 30, fold_seconds=10)
        assert len(ests) == 1
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 1.0

    def test_edge_rtp_and_subsample_name_the_true_edge(self):
        rtp0 = 7_654_321
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.0)
        _, ests = run_stage(SR, iq, coarse=self.EDGE, fold_seconds=10,
                            rtp0=rtp0)
        est = ests[0]
        # true edge RTP positions are rtp0 + k*SR + EDGE; est must name one
        total = (est.edge_rtp - rtp0) % SR + est.edge_subsample
        err_us = abs(total - self.EDGE) / SR * 1e6
        assert err_us < 1.0
        assert -0.5 <= est.edge_subsample < 0.5

    def test_amplitude_tilt_moves_argmax_but_not_zero_crossing(self):
        # 5%/s amplitude tilt: the ±0.5 s boxcar argmax walks by ms
        # (the flat-apex defect); the zero crossing must not.
        iq = make_bpsk(SR, 10, self.EDGE, tilt_per_s=0.05, noise_rms=0.02)
        _, ests = run_stage(SR, iq, coarse=self.EDGE, fold_seconds=10)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

        # Demonstrate the defect the fine stage fixes: boxcar argmax on
        # the same folded data shifts by >100 µs.
        one_sec = np.real(
            make_bpsk(SR, 1, self.EDGE, tilt_per_s=0.05)
            * np.exp(-1j * 0.3))
        N = SR // 2
        c = np.cumsum(np.concatenate([one_sec, one_sec]))  # periodic ext.
        y = np.abs((c[N:N + SR] - c[:SR]) - (c[SR:2 * SR] - c[N:N + SR]))
        argmax_err_us = abs(
            ((np.argmax(y) - self.EDGE + SR / 2) % SR - SR / 2)) / SR * 1e6
        assert argmax_err_us > 100.0

    def test_s16_quantisation_at_34_counts_rms_still_localises(self):
        # B4 measured T6 at ~34 counts RMS of ±32767 under s16.
        iq = make_bpsk(SR, 30, self.EDGE, amp=34.0, noise_rms=1.0)
        q = (np.round(iq.real) + 1j * np.round(iq.imag)).astype(np.complex64)
        _, ests = run_stage(SR, q, coarse=self.EDGE, fold_seconds=30)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

    def test_parity_of_start_second_does_not_move_the_edge(self):
        a = make_bpsk(SR, 10, self.EDGE, start_second=0, noise_rms=0.02)
        b = make_bpsk(SR, 10, self.EDGE, start_second=1, noise_rms=0.02,
                      seed=2)
        _, ea = run_stage(SR, a, coarse=self.EDGE, fold_seconds=10)
        _, eb = run_stage(SR, b, coarse=self.EDGE, fold_seconds=10)
        diff_us = abs(ea[0].edge_offset_samples
                      - eb[0].edge_offset_samples) / SR * 1e6
        assert diff_us < 2.0

    def test_mislabelled_batches_do_not_smear_the_edge(self):
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.02)
        stage = BpskEdgeFineStage(SR, fold_seconds=10)
        # RTP-domain coarse (registration = rtp0 = 999); see run_stage.
        stage.set_coarse_offset_samples((999 + self.EDGE) % SR)
        n_batches = int(np.ceil(len(iq) / 1740))
        bad = {b: +60 for b in range(0, n_batches, 11)}
        ests = feed_batches(stage, iq, rtp0=999, mislabel=bad)
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 2.0

    def test_no_coarse_offset_returns_none(self):
        iq = make_bpsk(SR, 2, self.EDGE)
        stage = BpskEdgeFineStage(SR, fold_seconds=2)
        ests = feed_batches(stage, iq, rtp0=0)
        assert ests == []

    def test_edge_near_fold_boundary_wraps_cleanly(self):
        edge = 20.0  # 20 samples after the fold wrap point
        iq = make_bpsk(SR, 10, edge, noise_rms=0.02)
        _, ests = run_stage(SR, iq, coarse=edge, fold_seconds=10)
        err_us = abs(((ests[0].edge_offset_samples - edge + SR / 2) % SR
                      - SR / 2)) / SR * 1e6
        assert err_us < 1.0


class TestCoarseOffsetDomain:
    """Final-review Finding 1: the coarse offset handed over by
    ``BpskPpsCalibratorMF`` is ``edge_rtp_full % sample_rate`` — the RTP
    domain — while the fold buffer indexes by samples-since-stage-start.
    The two differ by ``registration % sample_rate``, an arbitrary value
    fixed by wherever the stage happened to start.  Feeding the RTP
    value into a fold-domain search window mis-places the ±6 ms window
    by that amount (286 ms in the reviewer's repro), so the stage
    produced no estimates at all in production, while near the fold seam
    it could localise the seam instead of the edge.
    """

    EDGE = 43_181.4          # fold-domain edge position
    RTP0 = 123_456           # % 96000 == 27456: domains differ by 286 ms

    def test_production_style_rtp_domain_coarse_localises(self):
        # Exactly what the recorder does: hand over the MF's
        # `_chain_delay_samples`, i.e. (rtp of the edge) % SR.
        coarse_rtp = (self.RTP0 + self.EDGE) % SR
        assert abs(coarse_rtp - self.EDGE) > 0.1 * SR  # domains really differ
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.05)
        stage = BpskEdgeFineStage(SR, fold_seconds=10)
        stage.set_coarse_offset_samples(coarse_rtp)
        ests = feed_batches(stage, iq, rtp0=self.RTP0)
        assert len(ests) == 1
        err_us = abs(ests[0].edge_offset_samples - self.EDGE) / SR * 1e6
        assert err_us < 1.0

    def test_edge_rtp_phase_matches_the_rtp_domain_coarse(self):
        # The authority compares (edge_rtp + subsample) % SR against the
        # same coarse — both must land on the same RTP phase.
        coarse_rtp = (self.RTP0 + self.EDGE) % SR
        iq = make_bpsk(SR, 10, self.EDGE, noise_rms=0.02)
        stage = BpskEdgeFineStage(SR, fold_seconds=10)
        stage.set_coarse_offset_samples(coarse_rtp)
        est = feed_batches(stage, iq, rtp0=self.RTP0)[0]
        phase = (est.edge_rtp + est.edge_subsample) % SR
        err_us = abs((phase - coarse_rtp + SR / 2) % SR - SR / 2) / SR * 1e6
        assert err_us < 1.0

    def test_translation_helper_inverts_the_registration(self):
        stage = BpskEdgeFineStage(SR, fold_seconds=4)
        stage.set_coarse_offset_samples((self.RTP0 + self.EDGE) % SR)
        assert stage.coarse_offset_fold_domain(self.RTP0) == pytest.approx(
            self.EDGE % SR)
        # A stage that started one sample later shifts the fold position
        # by exactly one sample — the arbitrariness the bug ignored.
        assert stage.coarse_offset_fold_domain(self.RTP0 + 1) == pytest.approx(
            (self.EDGE - 1) % SR)

    def test_seam_adjacent_edge_is_not_confused_with_the_fold_seam(self):
        # Edge 20 samples past the true second boundary, with a
        # registration that puts the fold seam ~half a second away from
        # the RTP-domain coarse: the old mixed-domain seeding searched
        # around the wrong place entirely.
        edge = 20.0
        rtp0 = 48_000 + 7  # seam sits mid-second in the RTP domain
        iq = make_bpsk(SR, 10, edge, noise_rms=0.02)
        stage = BpskEdgeFineStage(SR, fold_seconds=10)
        stage.set_coarse_offset_samples((rtp0 + edge) % SR)
        ests = feed_batches(stage, iq, rtp0=rtp0)
        assert len(ests) == 1
        err_us = abs((ests[0].edge_offset_samples - edge + SR / 2) % SR
                     - SR / 2) / SR * 1e6
        assert err_us < 1.0


class TestFoldAcrossNonDividingBatches:
    """Regression for the flush-boundary overshoot defect found by the
    T6 Task 7 offline acceptance gate on t6-rawiq.bin.

    1740 does not evenly divide fold_seconds*SR (8*96000=768000;
    768000 % 1740 == 660): a flush used to consume the *whole*
    triggering batch even when that overshot the boundary, so `_cont`
    was never exactly 0 at the start of any block after the first.
    Since sign-alternation is `(_cont + arange(n)) // sample_rate`
    (relative to the last reset, not an absolute-second reference),
    every subsequent block started phase-shifted from the true
    per-second boundary and its coherent average destructively
    cancelled -- silently: `_compute_estimate` just returned None (no
    zero crossing found), with no counter or log distinguishing that
    from "still accumulating". Fixed by splitting any batch that
    crosses the boundary in `process_samples`, so `_cont` is always
    exactly 0 at a block start.
    """

    def test_batch_1740_does_not_divide_fold_survives_three_consecutive_blocks(self):
        edge = 43_181.4
        iq = make_bpsk(SR, 24, edge, noise_rms=0.05)
        stage = BpskEdgeFineStage(SR, fold_seconds=8)
        # RTP-domain coarse (registration = rtp0 = 555_555); see run_stage.
        stage.set_coarse_offset_samples((555_555 + edge + 40) % SR)
        ests = feed_batches(stage, iq, rtp0=555_555, batch=1740)
        assert len(ests) == 3
        assert stage.blocks_discarded == 0
        offs = np.array([e.edge_offset_samples for e in ests])
        spread_us = (offs.max() - offs.min()) / SR * 1e6
        assert spread_us < 1.0
