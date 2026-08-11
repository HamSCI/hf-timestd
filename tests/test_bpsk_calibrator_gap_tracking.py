"""Task 0 of the gap-aware tracking plan: REPRODUCE the field defect.

Field record (docs/T6-BLOCK-SLIP-ROOT-CAUSE-2026-08-10.md): sample gaps
make the recovered edge position slip by exactly the gap size, so each
re-lock's origin lands integer blocks away; the recorder's cancelling
±60-sample label wobble contributes a two-state ±1-packet phase.

These tests feed the MF calibrator a synthetic stream with (a) the
cancelling wobble, (b) a genuine sustained gap (samples removed, declared
RTP jumping truthfully), (c) both.  The invariant under test is the
stage-1 origin invariant: the edge position IN DECLARED RTP must be
unchanged across the event (< 1 sample, modular).

The gap cases are xfail(strict=True) until the gap-aware fix lands: they
document the defect, and they will LOUDLY flip when it is fixed.
"""
import numpy as np
import pytest

from hf_timestd.core.bpsk_pps_calibrator_mf import BpskPpsCalibratorMF

from tests.test_bpsk_pps_calibrator_mf import (  # reuse the proven helpers
    SR,
    _make_bpsk_signal,
    _modular_distance,
)


def _make_cal():
    return BpskPpsCalibratorMF(
        sample_rate=SR, consecutive_required=5, edge_tolerance_samples=20,
    )


def _feed(cal, signal, *, rtp_start=0, batch_size=480,
          wobble=False, gap_at_sample=None, gap_samples=0):
    """Feed with truthful-but-hostile labels.

    wobble: every 8th batch declares its start +60 early, the following
      batch returns to the true count (net-zero transient — the recorder
      repackaging pattern).
    gap_at_sample/gap_samples: REMOVE gap_samples of signal at that
      stream position; declared RTP jumps by the removed amount (a real
      loss, truthfully labelled).
    Returns (last_result_before_gap, last_result_after).
    """
    before = after = None
    rtp = rtp_start
    i = 0
    past_gap = False
    nbatch = 0
    while i < len(signal):
        if (gap_at_sample is not None and not past_gap
                and i >= gap_at_sample):
            i += gap_samples          # samples lost from the stream
            rtp = (rtp + gap_samples) & 0xFFFFFFFF   # labels jump truthfully
            past_gap = True
            continue
        batch = signal[i:i + batch_size]
        declared = rtp
        if wobble and nbatch % 8 == 7:
            declared = (rtp + 60) & 0xFFFFFFFF   # transient +60 mislabel
        r = cal.process_samples(batch, declared)
        if r is not None:
            if past_gap:
                after = r
            else:
                before = r
        rtp = (rtp + len(batch)) & 0xFFFFFFFF
        i += len(batch)
        nbatch += 1
    return before, after


INJECTED = 12.3   # true edge position (samples past the second)


def test_baseline_contiguous():
    """Sanity: contiguous truthful labels recover the injected offset."""
    cal = _make_cal()
    sig = _make_bpsk_signal(duration_s=12.0, edge_offset_samples=INJECTED)
    before, _ = _feed(cal, sig)
    assert before is not None and before.locked
    assert _modular_distance(before.chain_delay_samples, INJECTED, SR) < 0.1


def test_cancelling_wobble_does_not_move_edge():
    """The recorder's net-zero ±60 label wobble must not shift the
    recovered edge (field two-state ±1-packet signature)."""
    cal = _make_cal()
    sig = _make_bpsk_signal(duration_s=20.0, edge_offset_samples=INJECTED)
    before, _ = _feed(cal, sig, wobble=True)
    assert before is not None and before.locked
    err = _modular_distance(before.chain_delay_samples, INJECTED, SR)
    assert err < 1.0, f"wobble moved the edge by {err:.2f} samples"


def test_sustained_gap_origin_stable():
    # REGRESSION GUARD (2026-08-11): XPASSed on first run — the coarse MF
    # is gap-safe under honest labels.  The field defect is the LABEL
    # SOURCE (quality.last_rtp_timestamp), not this component.
    """A genuine 4-block (7680-sample) loss with truthful labels: the
    edge's declared-RTP position must be UNCHANGED across the gap."""
    gap = 4 * 1920
    cal = _make_cal()
    sig = _make_bpsk_signal(duration_s=30.0, edge_offset_samples=INJECTED)
    before, after = _feed(cal, sig, gap_at_sample=12 * SR + 480 * 3,
                          gap_samples=gap)
    assert before is not None and before.locked
    assert after is not None, "calibrator never recovered after the gap"
    err = _modular_distance(after.chain_delay_samples, INJECTED, SR)
    assert err < 1.0, (
        f"gap slipped the edge: recovered={after.chain_delay_samples:.2f}, "
        f"injected={INJECTED}, err={err:.1f} samples (gap was {gap})")


def test_gap_plus_wobble_origin_stable():
    # REGRESSION GUARD — see above.
    gap = 1920
    cal = _make_cal()
    sig = _make_bpsk_signal(duration_s=30.0, edge_offset_samples=INJECTED)
    before, after = _feed(cal, sig, wobble=True,
                          gap_at_sample=12 * SR + 480 * 5, gap_samples=gap)
    assert before is not None and before.locked
    assert after is not None
    err = _modular_distance(after.chain_delay_samples, INJECTED, SR)
    assert err < 1.0, f"err={err:.1f} samples"


# ---------------------------------------------------------------------------
# Fine stage (the actual anchor source) — reproduction target #2.
# The coarse MF held through gaps (XPASS above, 2026-08-11); the fine
# stage's continuity counter advances by SAMPLES RECEIVED while
# registration maps it to declared RTP — a sub-reset-threshold gap
# desynchronizes the two.
# ---------------------------------------------------------------------------
from hf_timestd.core.bpsk_edge_fine_stage import BpskEdgeFineStage


def _feed_fine(stage, sig, *, rtp0=0, batch=1740, gap_at=None, gap=0):
    """Feed with TRUTHFUL labels: every sample's declared RTP is its true
    position (rtp0 + index in the original signal).  A gap is simply a
    range of the signal that is never fed — the declared RTP of the next
    batch jumps by the gap size, exactly like real loss."""
    est = []
    i = 0
    past = False
    while i < len(sig):
        if gap_at is not None and not past and i >= gap_at:
            i += gap                # these samples never arrive
            past = True
            continue
        chunk = sig[i:i + batch]
        r = stage.process_samples(chunk, (rtp0 + i) & 0xFFFFFFFF)
        if r is not None:
            est.append((past, r))
        i += batch
    return est


def test_fine_stage_gap_origin_stable():
    # REGRESSION GUARD (2026-08-11): also XPASSed — the fine stage
    # discards the gap-spanning fold block and re-registers correctly
    # under honest labels.  Exonerated with the MF above.
    stage = BpskEdgeFineStage(SR, fold_seconds=4)
    stage.set_coarse_offset_samples(INJECTED)
    sig = _make_bpsk_signal(duration_s=26.0, edge_offset_samples=INJECTED)
    gap = 4 * 1920
    est = _feed_fine(stage, sig, gap_at=13 * SR + 1740 * 2, gap=gap)
    pre = [e for p, e in est if not p]
    post = [e for p, e in est if p]
    assert pre, "no estimate before the gap"
    assert post, "no estimate after the gap (liveness also broken)"
    for e in post:
        pos = (e.edge_rtp % SR) + e.edge_subsample
        err = _modular_distance(pos, INJECTED, SR)
        assert err < 1.0, (
            f"post-gap edge at {pos:.2f} (mod SR), injected {INJECTED}: "
            f"slipped {err:.0f} samples (gap was {gap})")
