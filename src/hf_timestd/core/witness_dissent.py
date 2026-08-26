"""When the witnesses agree with each other and not with the bench.

hf-timestd#29: *"The binding constraint is not evidence.  It is that no
witness has an actuator.  Adding more witnesses without one produces more
correct, ignored alarms."*

The station has repeatedly been in a state where every available witness
was right and nothing happened:

* 2026-08-15..19 — ~5,000 CRITICAL violations/day while T6 stayed
  AUTHORITATIVE and re-promoted itself after every demotion (#28, #21).
* 2026-08-25 — T6 held authority 3.4 h while **26 ms** wrong.  T4
  -26.153 ms, T3 -26.095 ms, T5 -26.158 ms.  chrony refused it
  independently.  No conflict was raised, because ``cross_bench_conflict``
  gates tier ADVANCEMENT and T6 is already top tier: there is nothing
  above it to refuse.

## The discriminator

A single witness disagreeing proves nothing — the witness may be the
broken one.  What proves the BENCH is wrong is witnesses **agreeing with
each other while disagreeing with it**.  On 2026-08-25 the three agreed
to ~60 us with each other and sat 26 ms from T6; no failure of T4, T3 and
T5 explains that, and one failure of T6 explains it completely.

So dissent requires:

1. a QUORUM (>= 2) — one witness cannot convict;
2. CONCORD — the witnesses' residuals agree within their own combined
   uncertainty, so they are not simply all noisy;
3. MAGNITUDE — their common residual exceeds ``k`` times the combined
   uncertainty of witness and bench.

## Why cheap witnesses suffice (#29's asymmetry)

The bench's job is sub-millisecond; a guardrail's job is catching tens of
milliseconds.  A witness good to ±10 ms catches a 70 ms excursion
trivially — one to two orders of magnitude less accuracy than the thing
it bounds.  That is why intermittent, propagation-limited HF sources are
adequate here while being useless as primary references.

⚠ This module DECIDES; it does not adjudicate.  It never picks a new
tier, never re-ranks, and never edits an anchor.  Its only output is "the
bench is at least this wrong", which the caller turns into published
sigma and, past a bound, withdrawal.  A new adjudication ladder is
exactly what #29 says not to build.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, List, Optional

# Independent witnesses required before dissent can be declared.
MIN_QUORUM = 2

# Multiples of the combined sigma at which a residual stops being noise.
# Matches the judge's existing cross-bench k so one number governs both.
DEFAULT_K = 5.0

# How far apart witnesses may sit and still count as agreeing with each
# other, in multiples of their own combined sigma.
CONCORD_K = 3.0

# Dissent must persist: a fade or a packet-loss burst must not convict.
DEFAULT_DWELL_S = 120.0


@dataclass(frozen=True)
class Witness:
    tier: str
    residual_ns: float      # witness UTC - bench UTC
    sigma_ns: float


@dataclass(frozen=True)
class Dissent:
    """What the witnesses collectively assert about the bench."""

    implied_error_ns: float     # median residual: the bench is >= this wrong
    tiers: tuple                # who concurred
    spread_ns: float            # how tightly they agreed with each other

    @property
    def sigma_floor_ns(self) -> float:
        """The smallest sigma the bench may honestly publish.

        If three independent witnesses put the bench 26 ms away, a 0.8 ms
        error bar is a false statement whatever produced it.
        """
        return abs(self.implied_error_ns)


def evaluate(
    witnesses: List[Witness],
    bench_sigma_ns: float,
    k: float = DEFAULT_K,
    concord_k: float = CONCORD_K,
    min_quorum: int = MIN_QUORUM,
) -> Optional[Dissent]:
    """Do the witnesses jointly convict the bench?  ``None`` if not."""
    usable = [w for w in witnesses
              if w.sigma_ns is not None and w.residual_ns is not None]
    if len(usable) < min_quorum:
        return None

    # (3) magnitude — each witness individually beyond the noise
    convinced = [
        w for w in usable
        if abs(float(w.residual_ns))
        > float(k) * ((float(w.sigma_ns) ** 2 + float(bench_sigma_ns) ** 2)
                      ** 0.5)
    ]
    if len(convinced) < min_quorum:
        return None

    # (2) concord — and they must agree with EACH OTHER, or they are
    # simply all noisy and prove nothing about the bench.
    residuals = [float(w.residual_ns) for w in convinced]
    spread = max(residuals) - min(residuals)
    combined = max(
        (float(w.sigma_ns) for w in convinced), default=0.0
    ) * 2.0
    if spread > float(concord_k) * max(combined, 1.0):
        return None

    return Dissent(
        implied_error_ns=float(median(residuals)),
        tiers=tuple(w.tier for w in convinced),
        spread_ns=float(spread),
    )


class DissentWatch:
    """Dwell tracker: act only on dissent that persists."""

    def __init__(self, dwell_s: float = DEFAULT_DWELL_S) -> None:
        self.dwell_s = float(dwell_s)
        self._since: Optional[float] = None
        self._last: Optional[Dissent] = None

    def observe(
        self, dissent: Optional[Dissent], now_s: float
    ) -> Optional[Dissent]:
        """Feed one evaluation.  Returns the dissent once it has stood
        for the dwell, and keeps returning it while it stands."""
        now = float(now_s)
        if dissent is None:
            self._since = None
            self._last = None
            return None
        if self._since is None:
            self._since = now
        self._last = dissent
        if (now - self._since) < self.dwell_s:
            return None
        return dissent

    @property
    def sustained_s(self) -> Optional[float]:
        return None if self._since is None else self._since


def from_shadow_residuals(
    shadows: Dict[str, Dict], bench_sigma_ns: float, **kw
) -> Optional[Dissent]:
    """Convenience adapter over ``offset_judge``'s shadow_residuals."""
    ws = []
    for tier, d in (shadows or {}).items():
        try:
            ws.append(Witness(tier=str(tier),
                              residual_ns=float(d["shadow_residual_ns"]),
                              sigma_ns=float(d["sigma_ns"])))
        except (KeyError, TypeError, ValueError):
            continue
    return evaluate(ws, bench_sigma_ns, **kw)
