"""Learn T6's chain-delay reference per station, then hold it.

See tests/test_t6_reference_resolver.py for the measurement that motivates
this module.  Pure logic only -- no I/O, no clock reads -- so it can be
replayed offline against captured lock records and WWVB ledgers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


PPS_PERIOD_NS = 1_000_000_000


def modular_delta_ns(a_ns: int, b_ns: int, period_ns: int = PPS_PERIOD_NS) -> int:
    """Signed separation of two chain delays, folded into +/- period/2.

    Chain delay is modular in the PPS period, so 999 ms and 1 ms are 2 ms
    apart, not 998.  Same semantics as
    ``core_recorder_v2.wrap_chain_delay_ns`` -- pinned by a test -- but
    reimplemented here so this module stays free of that import.
    """
    half = period_ns // 2
    return (a_ns - b_ns + half) % period_ns - half


@dataclass(frozen=True)
class GateOutcome:
    """Verdict on one candidate chain delay."""

    accepted: bool
    reason: str
    delta_ns: Optional[int] = None


def gate_candidate(
    *,
    candidate_ns: int,
    reference_ns: Optional[int],
    tolerance_ns: int,
) -> GateOutcome:
    """Accept ``candidate_ns`` only if it agrees with the latched reference.

    Refusing with no reference is deliberate: the failure this module exists
    to stop is the recorder accepting a calibrator value "as-is" because no
    authority was available to contradict it.
    """
    if reference_ns is None:
        return GateOutcome(accepted=False, reason="no_reference")
    delta = modular_delta_ns(candidate_ns, reference_ns)
    if abs(delta) > tolerance_ns:
        return GateOutcome(accepted=False, reason="disagrees", delta_ns=delta)
    return GateOutcome(accepted=True, reason="agrees", delta_ns=delta)


@dataclass(frozen=True)
class SelectionResult:
    """Outcome of trying to latch a reference from independent attestations."""

    reference_ns: Optional[int]
    reason: str
    support: int = 0


def select_reference(
    *,
    candidates_ns: Sequence[int],
    attested_ns: Sequence[int],
    tolerance_ns: int,
    min_attestations: int,
) -> SelectionResult:
    """Pick the candidate alias that independent attestations agree with.

    ``attested_ns`` are coarse, independently-derived chain delays -- WWVB's
    on-time mark is the intended source.  They carry a site-specific path
    delay and only need to be good to a few ms, because the aliases they
    choose between are >=20 ms apart.  The precise value then comes from T6's
    own estimator within the chosen alias.
    """
    if len(attested_ns) < min_attestations:
        return SelectionResult(None, "insufficient_attestations", len(attested_ns))
    scored = [
        (
            sum(
                1 for a in attested_ns
                if abs(modular_delta_ns(a, cand)) <= tolerance_ns
            ),
            cand,
        )
        for cand in candidates_ns
    ]
    if not scored:
        return SelectionResult(None, "no_candidate_supported", 0)
    scored.sort(key=lambda sc: sc[0], reverse=True)
    best_support, best = scored[0]
    if best_support < min_attestations:
        return SelectionResult(None, "no_candidate_supported", best_support)
    # A tie means the evidence does not distinguish two aliases.  Latching
    # either one freezes the anchor at a value nothing downstream can shake
    # out, so refuse: no reference is recoverable, a wrong one is not.
    if len(scored) > 1 and scored[1][0] == best_support:
        return SelectionResult(None, "ambiguous", best_support)
    return SelectionResult(best, "selected", best_support)
