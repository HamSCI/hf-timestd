"""Learn T6's chain-delay reference per station, then hold it.

See tests/test_t6_reference_resolver.py for the measurement that motivates
this module.  Pure logic only -- no I/O, no clock reads -- so it can be
replayed offline against captured lock records and WWVB ledgers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


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


class T6ReferenceTracker:
    """Learn a per-station chain-delay reference, then hold every lock to it.

    The recorder holds one of these.  It accumulates independently-attested
    chain delays (the T5 / LB-1421 NMEA disambiguation is the intended
    source), latches a reference once they agree, and thereafter gates.
    """

    def __init__(
        self,
        *,
        tolerance_ns: int,
        min_attestations: int,
        config_fingerprint: str = "",
    ) -> None:
        self.tolerance_ns = tolerance_ns
        self.min_attestations = min_attestations
        self._config_fingerprint = config_fingerprint
        self._reference_ns: Optional[int] = None
        self._attested: List[int] = []

    @property
    def reference_ns(self) -> Optional[int]:
        return self._reference_ns

    @property
    def config_fingerprint(self) -> str:
        return self._config_fingerprint

    def set_config_fingerprint(self, fingerprint: str) -> bool:
        """Declare the current channel config; drop the latch if it changed.

        The reference is a property of one installation's signal path, and
        the radiod channel filter is part of that path — its group delay
        reaches ~150 ms at the narrowest widths.  Carrying a reference across
        a filter change would gate every later lock against a stale value and
        silently take T6 off the air, so a change must invalidate, not adapt.

        Returns True if the latch was dropped.
        """
        if fingerprint == self._config_fingerprint:
            return False
        self._config_fingerprint = fingerprint
        self._reference_ns = None
        self._attested.clear()
        return True

    def observe(self, *, attested_ns: int) -> None:
        """Feed one independently-attested chain delay and try to latch.

        Latching needs a majority of the attestations to agree with their own
        robust centre — not merely to exist.  Scattered attestations are the
        phantom population; their mean would be a chain delay no edge ever
        had, so they must not latch anything.
        """
        if self._reference_ns is not None:
            return
        self._attested.append(int(attested_ns))
        if len(self._attested) < self.min_attestations:
            return
        centre = self._modular_centre(self._attested)
        support = sum(
            1 for a in self._attested
            if abs(modular_delta_ns(a, centre)) <= self.tolerance_ns
        )
        if support >= self.min_attestations:
            self._reference_ns = centre

    @staticmethod
    def _modular_centre(values: Sequence[int]) -> int:
        """Median taken in the modular domain, not on raw values.

        A cluster straddling the second boundary (999 ms, 1 ms, 2 ms) has a
        true centre near 0.7 ms, but a naive median returns 2 ms.  Fold every
        value onto the first, take the median of the folded offsets, and add
        it back.
        """
        base = values[0]
        deltas = sorted(modular_delta_ns(v, base) for v in values)
        mid = deltas[len(deltas) // 2]
        return (base + mid) % PPS_PERIOD_NS

    def gate(self, *, candidate_ns: int) -> GateOutcome:
        return gate_candidate(
            candidate_ns=candidate_ns,
            reference_ns=self._reference_ns,
            tolerance_ns=self.tolerance_ns,
        )
