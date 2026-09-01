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
from typing import Deque, Dict, List


class ArrivalHistory:
    """Per-(station) recent track with a re-acquisition escape."""

    def __init__(self, tolerance_ms: float, lookback: int,
                 reacquire_after: int) -> None:
        self.tolerance_ms = float(tolerance_ms)
        self.lookback = int(lookback)
        self.reacquire_after = int(reacquire_after)
        self._track: Dict[str, Deque[float]] = {}
        self._dissent: Dict[str, List[float]] = {}

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

        if abs(arrival_ms - centre) <= self.tolerance_ms:
            self._dissent.pop(station, None)
            self.observe(station, arrival_ms)
            return True

        # Disagrees with the track.  Does it agree with the other dissenters?
        run = self._dissent.setdefault(station, [])
        if run and abs(arrival_ms - median(run)) > self.tolerance_ms:
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
