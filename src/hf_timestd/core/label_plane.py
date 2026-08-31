"""Measured (label − host) reference-plane offset.

Under the content-time labelling convention (``docs/design/
CONTENT_TIME_LABELING_CONVENTION.md``), a T6 label answers *when did this
energy reach the antenna*.  Everything downstream of the antenna — USB
transfer, the 3.24 M-point FFT, filtering, scheduling — happens after
that instant, so a label-plane reading of "now" is one pipeline latency
EARLIER than a host-plane reading of "now".  On AC0G-B4 that gap is
≈16 ms; §3 of the proposal decomposes it into an analytic 2.5 ms DSP term
plus a load-dependent compute latency Λ ≈ 14 ms.

The judge must know that expected difference to compare a label-plane
bench (T6) against a host-plane one (T4/T3) without reading the plane gap
as disagreement.  Asserting it as a constant is precisely the mistake the
convention proposal exists to correct — a 16.618 ms constant, calibrated
once, that silently absorbed a varying quantity — so it is measured here
instead.

Why this is a measurement and not a circularity
-----------------------------------------------
The arrival floor gives ``label − arrival_mono = E − pipeline_min`` where
E is the true monotonic→UTC epoch.  Recovering the transport therefore
needs *some* clock to supply E, and the only one available is the host's:

    transport ≈ pipeline_min + host_clock_error

Two guards keep that honest rather than circular:

1. **Discipline gate.**  Observations are refused unless the host clock
   is independently disciplined to better than ``max_host_sigma_ns``.
   With chrony on FUSE/T5 the host holds ~20 µs — three orders of
   magnitude below the ~16 ms term — and every estimate carries that
   sigma, so the term can never claim to be tighter than the clock it was
   measured against.
2. **Slowness.**  The estimate is a long-window median of a physical
   latency that drifts, while the judge's cross-bench gate is
   instantaneous.  This matters more than it looks: a term that tracked
   the instantaneous T6−T4 delta would cancel exactly the disagreement
   the gate exists to detect, quietly making the gate vacuous.  Because
   the term is slow, a T6 epoch STEP still reaches the gate at full
   amplitude (``test_a_t6_step_is_not_absorbed_immediately``).

With no usable estimate this returns ``None`` and the caller falls back
to its configured value.  Refusing to answer is a supported outcome; a
made-up plane offset is not.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

__all__ = ["LabelPlaneEstimate", "LabelPlaneTracker"]


@dataclass(frozen=True)
class LabelPlaneEstimate:
    """``offset_ns`` is (label − host); negative = label reads earlier."""

    offset_ns: float
    sigma_ns: float
    n: int
    span_s: float
    #: How much the term WANDERS (population spread), as distinct from how
    #: well its centre is known (``sigma_ns``).  The judge subtracts the
    #: centre and widens its violation tolerance by the spread: a term can
    #: be centred precisely and still move, and both facts change what a
    #: comparison should conclude.
    spread_ns: float = 0.0
    #: Independent-sample count actually credited (span / correlation time).
    n_eff: float = 0.0


class LabelPlaneTracker:
    """Rolling median of the (label − host) plane offset."""

    #: Default observation window.  Long enough that the term is a
    #: property of the pipeline rather than of any one tick, short enough
    #: to follow a genuine latency change (a CPU-load shift) within a few
    #: minutes.
    DEFAULT_WINDOW_S = 600.0

    #: Below this many observations the median is not meaningful.
    DEFAULT_MIN_N = 8

    #: Consecutive observations against ONE chrony-disciplined host clock
    #: are not independent draws: the host's error is correlated over
    #: tens of seconds, so polling faster manufactures confidence rather
    #: than information.  Independent samples are credited from the span,
    #: not the count.
    DEFAULT_CORRELATION_TIME_S = 60.0

    #: The averaging-down argument applies to host clock NOISE and never
    #: to host clock BIAS.  A caller that can defend a figure for the
    #: irreducible part declares it as ``host_bias_floor_ns``; otherwise
    #: the whole reported host sigma stands as the floor, which keeps the
    #: honest half of the original guard -- the term may never claim to be
    #: tighter than the systematic error of the clock it was measured
    #: against.

    #: Host clocks worse than this are not a usable reference for the
    #: measurement.  1 ms is already ~6% of the term; anything looser and
    #: the estimate says more about the host than about the pipeline.
    DEFAULT_MAX_HOST_SIGMA_NS = 1_000_000.0

    def __init__(
        self,
        window_s: float = DEFAULT_WINDOW_S,
        min_n: int = DEFAULT_MIN_N,
        max_host_sigma_ns: float = DEFAULT_MAX_HOST_SIGMA_NS,
        correlation_time_s: float = DEFAULT_CORRELATION_TIME_S,
        host_bias_floor_ns: Optional[float] = None,
    ):
        self.window_s = float(window_s)
        self.min_n = int(min_n)
        self.max_host_sigma_ns = float(max_host_sigma_ns)
        self.correlation_time_s = max(1e-9, float(correlation_time_s))
        self.host_bias_floor_ns = (None if host_bias_floor_ns is None
                                   else max(0.0, float(host_bias_floor_ns)))
        # (mono, offset_ns, host_sigma_ns)
        self._obs: Deque[Tuple[float, float, float]] = deque()

    # ── ingest ──────────────────────────────────────────────────────────

    def observe(
        self,
        *,
        label_utc: float,
        host_utc: float,
        host_sigma_ns: float,
        mono: float,
    ) -> None:
        """Record one paired label-plane / host-plane reading.

        Both readings must describe the same monotonic instant; the caller
        (the judge) extrapolates them there before calling.  Observations
        from an undisciplined host are dropped, not stored.
        """
        try:
            host_sigma_ns = float(host_sigma_ns)
        except (TypeError, ValueError):
            return
        if not (host_sigma_ns >= 0.0) or host_sigma_ns > self.max_host_sigma_ns:
            return

        offset_ns = (float(label_utc) - float(host_utc)) * 1e9
        self._obs.append((float(mono), offset_ns, host_sigma_ns))
        self._expire(float(mono))

    def _expire(self, now_mono: float) -> None:
        cutoff = now_mono - self.window_s
        while self._obs and self._obs[0][0] < cutoff:
            self._obs.popleft()

    # ── readout ─────────────────────────────────────────────────────────

    def estimate(self) -> Optional[LabelPlaneEstimate]:
        """The current plane offset, or ``None`` when not yet knowable."""
        if len(self._obs) < self.min_n:
            return None

        offsets = [o for _, o, _ in self._obs]
        median = statistics.median(offsets)

        # How far the term WANDERS.  Λ is a pipeline latency that moves
        # with load, so a large spread is a true statement about the
        # quantity — not evidence that the measurement is poor.
        try:
            spread = statistics.pstdev(offsets)
        except statistics.StatisticsError:  # pragma: no cover - len>=min_n
            spread = 0.0

        span = self._obs[-1][0] - self._obs[0][0]

        # How well the CENTRE is known.  This is the standard error of the
        # median (1.2533 * s / sqrt(n_eff)), and the two quantities differ
        # by an order of magnitude in practice.  The former code returned
        # max(host_sigma, spread) for both, which shrinks with neither n
        # nor span -- so a measured term could never beat the bound the
        # consumer applies, and the configured constant won by default
        # rather than on merit (AC0G-B4 published exactly that refusal for
        # the whole content-time era).
        #
        # n_eff comes from the SPAN and a declared correlation time, never
        # from the raw count: consecutive reads of one disciplined clock
        # carry one clock's error, and polling faster does not divide it.
        n_eff = max(1.0, span / self.correlation_time_s)
        sem = 1.2533 * spread / math.sqrt(n_eff)

        # The host clock's systematic error is the floor: noise averages
        # down, bias does not.  Take the worst host sigma offered as an
        # upper bound on that bias unless the caller declares a tighter
        # one it can defend.
        host_sigma = max(s for _, _, s in self._obs)
        floor = (host_sigma if self.host_bias_floor_ns is None
                 else self.host_bias_floor_ns)
        sigma = math.sqrt(sem * sem + floor * floor)

        return LabelPlaneEstimate(
            offset_ns=median,
            sigma_ns=float(sigma),
            n=len(self._obs),
            span_s=float(span),
            spread_ns=float(spread),
            n_eff=float(n_eff),
        )
