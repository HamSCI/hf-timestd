"""Least-delayed-arrival filter for the T6 stream hand-off.

The NativeAnchor labels any RTP sample with sub-us UTC by pure counter
arithmetic.  Turning that into "what is UTC *now*" needs one more step:
the label of an arrived sample is behind true UTC by however long the
sample took to reach us.  That transport latency is what the T6 bench
must not swallow.

It is not noise.  radiod emits a whole blocktime as a burst, and
MultiStream delivers on a packet count that does not divide the burst,
so the labelled sample's age sweeps a full blocktime (measured on B4:
uniform 0..19.2 ms, stdev 5.90 ms over 6,599 deliveries).  Averaging
that would centre the bench half a blocktime early.

The least-delayed arrival in a rolling window is the honest estimate:
latency is bounded below by the physical path and every sample above
that floor is queueing we can discard.  Offsets are (label_utc - mono),
so the SMALLEST latency is the LARGEST offset -- a running maximum.

This is the classic NTP/PTP delay filter.  It deliberately knows
nothing about radiod's blocktime or packet geometry: those are
configuration, and a filter that hardcoded them would silently stop
working when either changed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


@dataclass(frozen=True)
class FloorEstimate:
    """The least-delayed arrival observed in the window.

    ``offset_s`` maps monotonic to UTC: utc(m) = m + offset_s.
    """

    offset_s: float
    sigma_ns: float
    n: int
    span_s: float


class ArrivalFloorTracker:
    """Rolling maximum of (label_utc - arrival_mono) over a time window.

    Fed from the stream's delivery callback -- NOT from the judge's
    poll.  The judge ticks every 10 s; at that rate a window holds a
    single arrival and filters nothing.
    """

    # How many past floor estimates the sigma is measured over.  At the
    # judge's 10 s tick this is a ~5 minute horizon.
    HISTORY_LEN = 30

    # Lower bound on claimable precision.  The hand-off's CONSTANT delay
    # is invisible from inside the stream -- every arrival carries it
    # equally -- so a steady floor would otherwise report sigma 0.  This
    # is a bound on what we refuse to claim, not a measurement of the
    # delay itself; the cross-bench gate against T4/T5 is what actually
    # catches the residual, published as shadow_residuals.
    MIN_SIGMA_NS = 100_000.0

    # Stand-in until the floor's own scatter is observable (needs at
    # least two estimates -- e.g. right after an anchor recapture).
    # Deliberately the same conservative transport bound the bench uses
    # with no floor at all: honest, far wider than the promotion gate so
    # it blocks adoption on its own, and -- unlike inf -- publishable as
    # JSON into shadow_residuals.
    UNMEASURED_SIGMA_NS = 25_000_000.0

    def __init__(self, window_s: float = 2.0):
        self.window_s = float(window_s)
        self._window: Deque[Tuple[float, float]] = deque()
        self._history: Deque[float] = deque(maxlen=self.HISTORY_LEN)

    def reset(self, cause: str = "") -> None:
        """Drop both the window and the measured sigma.

        Called whenever the anchor is (re)captured or restored: offsets
        are expressed relative to it, so the frame moves with it.
        """
        self._window.clear()
        self._history.clear()

    def note(self, offset_s: float, mono: float) -> None:
        """Record one arrival's (label_utc - arrival_mono) offset."""
        self._window.append((float(mono), float(offset_s)))

    def estimate(
        self, mono_now: float, record: bool = True
    ) -> Optional[FloorEstimate]:
        """The least-delayed arrival still inside the window.

        ``record=False`` returns the same estimate WITHOUT folding it
        into the sigma history.  The history is a fixed-length window
        (``HISTORY_LEN``), so its horizon is set by how often it is
        appended to: at the judge's 10 s tick it spans ~5 minutes.  A
        second consumer sampling faster — the HPPS SHM push runs once
        per PPS edge, 10x the judge's rate — would silently shorten that
        horizon to ~30 s and change the sigma the BENCH publishes.
        Readers that are not the judge pass ``record=False`` so sigma
        keeps measuring what it says it measures.
        """
        while self._window and mono_now - self._window[0][0] > self.window_s:
            self._window.popleft()
        if not self._window:
            return None
        offsets = [o for _m, o in self._window]
        floor = max(offsets)
        if record:
            self._history.append(floor)
        return FloorEstimate(
            offset_s=floor,
            sigma_ns=self._sigma_ns(),
            n=len(offsets),
            span_s=self._window[-1][0] - self._window[0][0],
        )

    def _sigma_ns(self) -> float:
        """1-sigma from how much the floor estimate itself has moved.

        Measured, never asserted: the estimator's own scatter across
        recent windows is exactly its uncertainty.
        """
        hist = list(self._history)
        if len(hist) < 2:
            return self.UNMEASURED_SIGMA_NS
        med = _median(hist)
        mad = _median([abs(x - med) for x in hist])
        return max(self.MIN_SIGMA_NS, 1.4826 * mad * 1e9)


def _median(values) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])
