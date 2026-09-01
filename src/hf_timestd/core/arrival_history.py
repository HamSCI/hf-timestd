"""Key 3: does this arrival agree with the recent track?

Propagation delay moves smoothly and mode changes step by known amounts, so
a lone arrival far from the recent track is more likely a sidelobe or a
mis-assignment than a real path change.  T6 already reasons this way — it
estimates from the PPS history rather than each pulse, because the GPSDO
pins the slope.

⛔ The gate must stay falsifiable.  Admit only what matches history and the
tracker can lock onto a wrong track and then refuse the very evidence that
would correct it — the stale-lock failure behind the August T6 -26 ms
excursion.  So a LONE outlier gets rejected, while `reacquire_after`
consecutive arrivals that agree with each other force re-acquisition.
"""

from __future__ import annotations

from collections import deque
from statistics import median
from typing import Deque, Dict, List, Mapping, Union


class ArrivalHistory:
    """Per-(station) recent track with a re-acquisition escape."""

    #: Used when a station has no calibrated tolerance of its own.  Wide
    #: enough not to starve an uncalibrated station, narrow enough to stay
    #: well inside the 18.6 ms WWV-WWVH separation.
    DEFAULT_TOLERANCE_MS = 6.0

    def __init__(self, tolerance_ms: Union[float, Mapping[str, float]],
                 lookback: int, reacquire_after: int) -> None:
        # Calibration gave a different tolerance per station: measured over
        # 3M archived arrivals AFTER keys 1 and 2 had filtered them, the p95
        # minute-to-minute step is 4.86 ms for WWV, 6.03 for WWVH, 8.69 for
        # BPM.  One shared number either starves WWV or lets BPM's real
        # variation read as an outlier — 11,528 km over three hops moves more
        # than 1,122 km over one.  A scalar stays valid for callers that want
        # one value.
        if isinstance(tolerance_ms, Mapping):
            self._tolerance: Dict[str, float] = {
                k: float(v) for k, v in tolerance_ms.items()}
            self.tolerance_ms = None
        else:
            self._tolerance = {}
            self.tolerance_ms = float(tolerance_ms)
        self.lookback = int(lookback)
        self.reacquire_after = int(reacquire_after)
        self._track: Dict[str, Deque[float]] = {}
        self._dissent: Dict[str, List[float]] = {}

    def tolerance_for(self, station: str) -> float:
        """The tolerance this station is judged against."""
        if self.tolerance_ms is not None:
            return self.tolerance_ms
        return self._tolerance.get(station, self.DEFAULT_TOLERANCE_MS)

    def _centre(self, station: str) -> float | None:
        seen = self._track.get(station)
        if not seen:
            return None
        return median(seen)

    def accepts(self, station: str, arrival_ms: float) -> bool:
        centre = self._centre(station)
        if centre is None:
            self.observe(station, arrival_ms)
            return True

        tol = self.tolerance_for(station)
        if abs(arrival_ms - centre) <= tol:
            self._dissent.pop(station, None)
            self.observe(station, arrival_ms)
            return True

        # Disagrees with the track.  Does it agree with the other dissenters?
        run = self._dissent.setdefault(station, [])
        if run and abs(arrival_ms - median(run)) > tol:
            run.clear()
        run.append(arrival_ms)

        if len(run) >= self.reacquire_after:
            self._track[station] = deque(run, maxlen=self.lookback)
            self._dissent.pop(station, None)
            return True
        return False

    def observe(self, station: str, arrival_ms: float) -> None:
        self._track.setdefault(
            station, deque(maxlen=self.lookback)).append(float(arrival_ms))
