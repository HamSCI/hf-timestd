#!/usr/bin/env python3
"""`estimate_stale` must say WHICH kind of silence it found.

B4 lost T6 for 6h23m on 2026-08-31 (availability 97.2% -> 41.5%), cleared only
by restarting the recorder. Diagnosis by elimination exhausted itself: signal
quality was fine (an independent block-wise estimator located the edge
sample-exactly in 98% of seconds THROUGHOUT), the Costas loop stayed locked,
C/N0 was 57-58 dB-Hz — HIGHER than when it worked — T5 had 120/120 valid fixes,
and integer-second naming was correct. The violation recorded was
`estimate_stale`.

But `_last_estimate_at` advances only when `_check` returns NO violations. So
two entirely different conditions produce the identical message:

  (a) the fine stage stopped producing estimates, or
  (b) the fine stage kept producing them and the authority REFUSED every one.

Under (b) nothing reconciles: the fine stage's own `_failed_blocks` recovery
never fires, because from its side the blocks are succeeding. That is the
shape of a stall that only a process restart clears.

The corrective actions are opposite — fix the fine stage, or fix whatever
invariant is refusing everything — and the current log cannot tell them apart.
"""

import pytest

from hf_timestd.core.t6_anchor_authority import T6AnchorAuthority


def test_a_fresh_authority_reports_no_estimates_seen():
    a = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)
    d = a.stale_diagnosis()
    assert d["estimates_seen"] == 0
    assert d["rejected_since_accept"] == 0
    assert d["rejection_reasons"] == {}


def test_rejections_are_counted_by_reason():
    """The whole point: which invariant is refusing, and how often."""
    a = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)
    a.note_estimate_seen(("edge_period",))
    a.note_estimate_seen(("edge_period",))
    a.note_estimate_seen(("fine_coarse", "edge_period"))

    d = a.stale_diagnosis()
    assert d["estimates_seen"] == 3
    assert d["rejected_since_accept"] == 3
    assert d["rejection_reasons"] == {"edge_period": 3, "fine_coarse": 1}


def test_an_accepted_estimate_clears_the_rejection_run():
    a = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)
    a.note_estimate_seen(("edge_period",))
    a.note_estimate_seen(())          # accepted
    d = a.stale_diagnosis()
    assert d["rejected_since_accept"] == 0
    assert d["rejection_reasons"] == {}
    assert d["estimates_seen"] == 2   # the total is cumulative, not reset


def test_the_diagnosis_separates_the_two_silences():
    """(a) nothing arriving vs (b) everything refused — different faults."""
    a = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)

    # (b) estimates arriving, all refused
    for _ in range(5):
        a.note_estimate_seen(("fine_coarse",))
    d = a.stale_diagnosis()
    assert d["estimates_arriving"] is True
    assert d["verdict"] == "rejected"

    # (a) nothing arriving at all
    b = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)
    d2 = b.stale_diagnosis()
    assert d2["estimates_arriving"] is False
    assert d2["verdict"] == "absent"


def test_seconds_since_last_estimate_seen_is_reported():
    a = T6AnchorAuthority(sample_rate_hz=24000, delay_budget_ns=200)
    a.note_estimate_seen(("edge_period",))
    d = a.stale_diagnosis()
    assert d["since_seen_sec"] is not None
    assert d["since_seen_sec"] >= 0.0
