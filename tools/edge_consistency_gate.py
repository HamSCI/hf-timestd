"""Refuse an edge that jumps away from where the last ones sat.

⛔ The defect this closes, measured on AC0G-B4 over 2026-09-22.  Across 1,254
folded blocks the magnitude-difference estimator placed the TS-1 edge
correctly 1,249 times with 0.444 us scatter, and five times landed +43.4,
+83.4, +113.1, -184.3 and -282.3 ms away.  Not degraded precision -- gross
errors, on a pilot whose true position CANNOT move because it is injected
ahead of the RX888.

⚠ And the estimator looked MORE confident when it was wrong: the five bad
blocks carried a mean peak/median of 6.90 against 4.35 for the good ones.  So
no threshold on peak strength can separate them, and an acceptance battery
gated on signal quality passes them straight through.

⚠⚠ **This belongs to the BENCH, not to the shipped estimator.**  The five
failures above came from a standalone analysis script that had no gate at all.
hf-timestd's own path already gates harder: ``T6AnchorAuthority`` computes the
period deviation between successive edges and refuses anything past
``edge_period_tolerance_ns = 5_000`` — 5 us, which at 96 kHz is **0.48
samples**, ten times TIGHTER than Scott's five.  Every one of those jumps
would have been refused there by a factor of ten thousand.  So this module
exists to give ad-hoc analysis tooling the protection production already has,
and must not be mistaken for a fix to a product defect.

Scott Newell's wd-record has always carried the answer.  Its per-sample
detector keeps the previous edge and rejects any pulse landing more than five
samples from it, modulo the sample rate:

    uint32_t p = (ts - (uint32_t)sp->last_edge) % sp->samprate;
    uint32_t sample_error = (p > sp->samprate / 2) ? (sp->samprate - p) : p;
    if (sample_error > 5)
      noisy = true;

Position consistency catches what confidence cannot.  This module applies the
same idea to our sub-sample estimator.

⛔⛔ But a gate that never releases is worse than no gate.  On 2026-09-04 B4's
T6 locked onto a phantom 20 ms lattice and stayed there; a hard gate would
have defended that wrong answer indefinitely.  So this one re-acquires: when
several CONSECUTIVE rejected edges agree with EACH OTHER, the world has moved
and the gate moves with it.  Rejecting an outlier and refusing a re-base are
different things, and only the first is wanted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Scott's figure, and ours for the same reason: five samples is far wider
# than any real edge motion (our whole-night scatter was 0.043 samples) and
# far narrower than the smallest jump we have seen (4,167 samples).
DEFAULT_TOLERANCE_SAMPLES = 5.0

# How many consecutive rejects that agree with each other before the gate
# concludes the edge genuinely moved.  Three at one block per 30 s means a
# real re-base costs 90 s of refusals, while a lone outlier -- which never
# repeats -- never reaches it.
DEFAULT_REACQUIRE_AFTER = 3


@dataclass(frozen=True)
class GateVerdict:
    accepted: bool
    edge_offset_samples: float
    # Distance from the running reference, wrapped, in samples.  None for the
    # first edge, which has nothing to compare against.
    distance_samples: Optional[float]
    reason: str
    # True when this verdict ADOPTED a new reference after sustained
    # disagreement, rather than merely accepting a consistent edge.
    reacquired: bool = False


def wrapped_distance(a: float, b: float, period: float) -> float:
    """Shortest distance between two positions on a circular second.

    ⚠ The edge sits modulo the sample rate, so 95,999 and 1 lie TWO samples
    apart at 96 kHz, not 95,998.  A gate that subtracted naively would reject
    every edge that happened to straddle the second boundary -- which is
    exactly where a pulse-per-second edge likes to sit.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    d = abs(a - b) % period
    return min(d, period - d)


class EdgeConsistencyGate:
    """Stateful position gate over successive edge estimates."""

    def __init__(self, samples_per_second: int,
                 tolerance_samples: float = DEFAULT_TOLERANCE_SAMPLES,
                 reacquire_after: int = DEFAULT_REACQUIRE_AFTER) -> None:
        if samples_per_second <= 0:
            raise ValueError("samples_per_second must be positive")
        if tolerance_samples <= 0:
            raise ValueError("tolerance_samples must be positive")
        if reacquire_after < 1:
            raise ValueError("reacquire_after must be at least 1")
        self.samples_per_second = int(samples_per_second)
        self.tolerance_samples = float(tolerance_samples)
        self.reacquire_after = int(reacquire_after)
        self._reference: Optional[float] = None
        self._pending: list[float] = []
        self.accepted_count = 0
        self.rejected_count = 0
        self.reacquire_count = 0

    @property
    def reference(self) -> Optional[float]:
        """The position the gate currently believes, or None before the first."""
        return self._reference

    def check(self, edge_offset_samples: float) -> GateVerdict:
        pos = float(edge_offset_samples) % self.samples_per_second

        if self._reference is None:
            self._reference = pos
            self.accepted_count += 1
            return GateVerdict(True, pos, None, "first edge — nothing to check against")

        d = wrapped_distance(pos, self._reference, self.samples_per_second)

        if d <= self.tolerance_samples:
            # Track slowly: follow the edge, do not let one reading define it.
            self._reference = self._reference + (pos - self._reference) * 0.25 \
                if abs(pos - self._reference) < self.samples_per_second / 2 else pos
            self._reference %= self.samples_per_second
            self._pending.clear()
            self.accepted_count += 1
            return GateVerdict(True, pos, d, "consistent with the running edge")

        # Outside tolerance.  Hold it, and see whether the next ones agree.
        self._pending.append(pos)
        if len(self._pending) >= self.reacquire_after:
            recent = self._pending[-self.reacquire_after:]
            spread = max(wrapped_distance(x, recent[0], self.samples_per_second)
                         for x in recent)
            if spread <= self.tolerance_samples:
                self._reference = recent[-1]
                self._pending.clear()
                self.accepted_count += 1
                self.reacquire_count += 1
                return GateVerdict(
                    True, pos, d,
                    f"re-acquired: {self.reacquire_after} consecutive edges agree "
                    f"{d:.1f} samples away from the old reference",
                    reacquired=True)
            # They disagree with each other, so this is scattered noise, not a
            # re-base.  The quorum itself is already safe — `recent` only ever
            # looks at the last `reacquire_after` entries — so this line is
            # about MEMORY: without it the list grows one entry per rejected
            # edge and never shrinks, which on a station that runs for months
            # is a slow leak in the timing path.
            self._pending = [pos]

        self.rejected_count += 1
        return GateVerdict(
            False, pos, d,
            f"rejected: {d:.1f} samples from the running edge, "
            f"tolerance {self.tolerance_samples:.1f}")
