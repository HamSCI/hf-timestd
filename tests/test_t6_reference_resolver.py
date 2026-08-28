"""T6's chain-delay reference must be LEARNED per station, then held.

The 2026-08-28 finding (reference_t6_locks_span_whole_second): across 1572
`T6 BPSK PPS LOCKED` records on B4, raw chain_delay landed in 27 distinct
clusters spanning +53 ms .. +993 ms, and lock quality did not discriminate --
clusters at +221/+293/+461/+561/+577/+633/+661/+761 ms all reported
ok_frac=1.00, noise=0.  The estimator has no absolute intra-second reference;
resolving the alias is the *disambiguation's* job, and that job is currently
delegated to T4/T5 (WWV/WWVH/BPM), which are HF-propagated and fail at night
-- exactly when the phantom locks appear.

This module is the pure core of the replacement: pick the alias against an
independent attestation (WWVB, 60 kHz LF), latch it per station, then gate
every later lock on agreement with the latched value.

⛔ The reference is never a shipped constant.  Chain delay is TS-1 -> coax ->
RX-888 for one shack, and moves with cable length, hardware revision and
radiod's configured filter width.  Only the *tolerance* is universal.
"""
import pytest

from hf_timestd.core.t6_reference_resolver import (
    GateOutcome,
    gate_candidate,
)

MS = 1_000_000


def test_gate_refuses_when_no_reference_established():
    """With nothing latched, a lock must NOT be accepted on its own say-so.

    This is the `no usable non-T6 timing authority for disambiguation;
    accepting calibrator value as-is` path that admitted the phantoms.
    Absent a reference the honest answer is "I don't know", not a guess.
    """
    outcome = gate_candidate(
        candidate_ns=197 * MS,
        reference_ns=None,
        tolerance_ns=5 * MS,
    )
    assert outcome.accepted is False
    assert outcome.reason == "no_reference"


def test_gate_rejects_a_candidate_that_disagrees_with_the_reference():
    """The 200 ms phantom must be refused once ~15 ms is latched.

    B4's true chain delay is ~14.5-17.5 ms (test_t6_chain_delay_wrap).  A lock
    reporting 197 ms is the sidelobe cluster the +/-250 ms guard let through.
    """
    outcome = gate_candidate(
        candidate_ns=197 * MS,
        reference_ns=15 * MS,
        tolerance_ns=5 * MS,
    )
    assert outcome.accepted is False
    assert outcome.reason == "disagrees"


def test_gate_accepts_a_candidate_within_tolerance():
    outcome = gate_candidate(
        candidate_ns=17 * MS,
        reference_ns=15 * MS,
        tolerance_ns=5 * MS,
    )
    assert outcome.accepted is True


def test_gate_compares_modularly_across_the_second_boundary():
    """Chain delay is modular in the PPS period: 999 ms is 2 ms from 1 ms.

    Without wrapping, the naive difference is 998 ms and a perfectly good
    lock is thrown away -- the 2026-08-14 B4 lockout in reverse.
    """
    outcome = gate_candidate(
        candidate_ns=999 * MS,
        reference_ns=1 * MS,
        tolerance_ns=5 * MS,
    )
    assert outcome.accepted is True


def test_modular_delta_matches_the_recorder_wrap_helper():
    """Pin our fold to core_recorder_v2's, so the two cannot drift apart.

    Deliberately a characterisation test over existing behaviour: this module
    reimplements the fold to stay import-free, and that is only safe while the
    semantics stay identical.
    """
    from hf_timestd.core.core_recorder_v2 import wrap_chain_delay_ns
    from hf_timestd.core.t6_reference_resolver import modular_delta_ns

    for raw_ms in (0, 1, 15, 197, 499, 500, 501, 750, 843, 993, 999):
        raw = raw_ms * MS
        assert modular_delta_ns(raw, 0) == wrap_chain_delay_ns(raw), raw_ms


def test_selects_the_candidate_the_wwvb_attestations_agree_with():
    """WWVB picks the alias; T6 supplies the precision within it.

    WWVB's on-time mark carries a site-specific path delay (~3 ms/1000 km from
    Fort Collins) so it cannot fix the value -- but the aliases are >=20 ms
    apart, so it does not need to.  Attestations here scatter by a few ms
    around B4's true ~15 ms; the +197 ms and +477 ms phantoms must lose.
    """
    from hf_timestd.core.t6_reference_resolver import select_reference

    result = select_reference(
        candidates_ns=[197 * MS, 15 * MS, 477 * MS],
        attested_ns=[13 * MS, 17 * MS, 14 * MS, 16 * MS, 15 * MS],
        tolerance_ns=5 * MS,
        min_attestations=3,
    )
    assert result.reference_ns == 15 * MS
    assert result.reason == "selected"
    assert result.support == 5


def test_refuses_to_latch_when_two_candidates_are_equally_supported():
    """A tie must refuse, not silently take the first.

    Latching the wrong alias freezes the anchor at a wrong value that no
    amount of MF jitter shakes out (the bee1 2026-05-31 failure).  An
    ambiguous reference is worse than none: none refuses loudly, a wrong one
    looks healthy forever.
    """
    from hf_timestd.core.t6_reference_resolver import select_reference

    result = select_reference(
        candidates_ns=[15 * MS, 500 * MS],
        # Three attestations sit on each candidate -- genuinely ambiguous.
        attested_ns=[14 * MS, 15 * MS, 16 * MS,
                     499 * MS, 500 * MS, 501 * MS],
        tolerance_ns=5 * MS,
        min_attestations=3,
    )
    assert result.reference_ns is None
    assert result.reason == "ambiguous"


def test_refuses_when_too_few_attestations_to_decide():
    from hf_timestd.core.t6_reference_resolver import select_reference

    result = select_reference(
        candidates_ns=[15 * MS],
        attested_ns=[15 * MS],
        tolerance_ns=5 * MS,
        min_attestations=3,
    )
    assert result.reference_ns is None
    assert result.reason == "insufficient_attestations"
