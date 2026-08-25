"""When to abandon a fine-stage lock that GPS no longer corroborates.

``_t6_last_chain_delay_ns`` is the effective chain delay the matched
filter locked onto.  Step-recovery guards it well: chain_delay is an
RF-path CONSTANT (TS-1 modulator -> coax -> RX-888 -> ADC, microseconds
at most), so a tight cluster implying a *different* one is almost always
the boxcar template's sidelobe at ±0.5 s.  On 2026-05-23 accepting one
walked HPPS out to +216 ms and chrony marked it falseticker.  Refusing
individual candidates is therefore correct and stays correct.

What was missing is the other direction.  Nothing ever re-validated the
LOCK.  On AC0G-B4 2026-08-25 the lock sat at 225,754,278 ns while T5
implied ~149,000,000 ns -- contradicted by 76.8 ms, every second, for
hours: reject -> clear ``_t6_recent_raw`` -> 60 more rejections ->
reject, a livelock with no state that could converge.  Meanwhile the
station fell back to a coarse T5 anchor and ran 26 ms wrong.

Both failures look identical from inside the step-recovery branch --
GPS disagrees with the lock -- so they are separated by MAGNITUDE:

    |Δ| <= BOUND ................ T5 noise.  Nothing to see.
    BOUND < |Δ| < SIDELOBE-GUARD  too big for noise, too small for the
                                  sidelobe => the LOCK is stale.
    |Δ| >= SIDELOBE-GUARD ....... the candidate is a phantom edge and
                                  the lock is fine.  Never escape.

and by DWELL, so that fades and packet-loss bursts cannot trip it.

⚠ This module decides ONLY "has the lock lost its corroboration".  It
does not re-derive anything.  The escape action is the one step-recovery
already performs on accept: drop the lock and the disambiguation so the
next cycle re-references against the timing-tier hierarchy.
"""
from __future__ import annotations

from typing import Optional

# T5's own spread was ~6 ms across seconds on B4, and 5 ms is the
# existing per-candidate sanity threshold
# (``T6_STEP_RECOVERY_T5_SANITY_NS``).  25 ms is COARSE_ANCHOR_SIGMA_NS
# -- what a coarse anchor is worth.  A contradiction bigger than the
# witness's own worth is the first point at which it means anything.
STALE_LOCK_BOUND_NS = 25_000_000.0

# The matched filter's boxcar template holds ±N = ±0.5 s around the
# edge, so its sidelobe sits one half-window away.
SIDELOBE_NS = 500_000_000.0

# Keep well clear of the sidelobe: a phantom that lands a little short
# of ±0.5 s must still be read as a phantom, not as a stale lock.
SIDELOBE_GUARD_NS = 100_000_000.0

# Five times the 60-sample step-recovery window, and comfortably inside
# the judge's 900 s operator alert so the escape acts before the alert
# fires rather than after it.
STALE_LOCK_DWELL_S = 300.0

# Re-acquisition needs room.  Without this, a lock that re-forms just as
# wrong would oscillate at the dwell period.
STALE_LOCK_COOLDOWN_S = STALE_LOCK_DWELL_S


def contradiction_is_escapable(
    contradiction_ns: Optional[float],
    bound_ns: float = STALE_LOCK_BOUND_NS,
    sidelobe_ns: float = SIDELOBE_NS,
    sidelobe_guard_ns: float = SIDELOBE_GUARD_NS,
) -> bool:
    """Whether ``Δ = t5_implied - locked`` can only mean a stale lock.

    Returns False for ``None`` (T5 unavailable is not evidence).
    """
    if contradiction_ns is None:
        return False
    mag = abs(float(contradiction_ns))
    return bound_ns < mag < (float(sidelobe_ns) - float(sidelobe_guard_ns))


class StaleLockWatch:
    """Dwell tracker: escape only after a sustained, escapable Δ.

    Stateless about the lock itself -- the caller supplies Δ and the
    clock, so this is pure and testable without a recorder.
    """

    def __init__(
        self,
        dwell_s: float = STALE_LOCK_DWELL_S,
        cooldown_s: float = STALE_LOCK_COOLDOWN_S,
    ) -> None:
        self.dwell_s = float(dwell_s)
        self.cooldown_s = float(cooldown_s)
        self._since: Optional[float] = None
        self._last_escape: Optional[float] = None

    @property
    def sustained_s(self) -> float:
        """How long the current escapable contradiction has stood."""
        return 0.0 if self._since is None else self._since_for(self._now)

    def _since_for(self, now: Optional[float]) -> float:
        if self._since is None or now is None:
            return 0.0
        return max(0.0, float(now) - self._since)

    _now: Optional[float] = None

    def observe(
        self, contradiction_ns: Optional[float], now_s: float
    ) -> bool:
        """Feed one Δ.  True exactly once when the lock must be dropped."""
        now = float(now_s)
        self._now = now
        if not contradiction_is_escapable(contradiction_ns):
            # Either corroborated, or a phantom.  Both clear the dwell:
            # the contradiction we were timing is no longer the one in
            # front of us.
            self._since = None
            return False
        if self._since is None:
            self._since = now
            return False
        if (now - self._since) < self.dwell_s:
            return False
        if (self._last_escape is not None
                and (now - self._last_escape) < self.cooldown_s):
            return False
        self._last_escape = now
        self._since = None
        return True
