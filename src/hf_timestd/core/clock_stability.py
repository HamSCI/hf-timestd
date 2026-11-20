"""Clock stability from T3's own sample-plane phase (task A2).

Each minute T3's summary labels one sample: ``utc_ref`` at ``rtp_ref``.  The
summary's counter map turns ``rtp_ref`` into radiod's input-sample index, and
that index over the input rate gives the GPS-disciplined time since the run
began.  Their difference, the phase, holds still for a perfect clock and a
perfect label.  Within one radiod run the constant parts (GPS-UTC, radiod's
epoch pair) cancel, so only phase differences within a run carry meaning.

The estimator takes the modified Allan deviation of that phase, per run
segment, and expresses it as the time deviation TDEV.  White phase noise in
T3's labels makes TDEV fall as tau^-1/2; wander in the host's reference makes
it rise.  The tau of the minimum marks the crossing, and sets how long a
window the admission policy averages over.

Pure numpy arithmetic: no clock reads, no I/O.  The only time it knows comes
from each point's ``utc_minute`` stamp, which names minutes and never enters
timing arithmetic.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
from hamsci_dsp.counter_map import CounterMap

from .t3_shadow_comparator import T3ShadowComparator

# Builder choice: one shadow row a minute, so the phase arrives every 60 s.
TAU0_S = 60
# Builder choice: one day of phase points.
STABILITY_HISTORY_MIN = 1440
# Builder choice: below this many points, stability reads "unmeasured".
STABILITY_MIN_POINTS = 120
# Builder choice: a p95 needs at least 20 samples to mean anything.
WINDOW_MIN_S = 1800
# Builder choice: a longer window lags dawn and dusk by more than an hour.
WINDOW_MAX_S = 7200
# Builder choice: the shortest hold; hold_s = max(HOLD_MIN_S, window_s // 4).
HOLD_MIN_S = 300

# Every TDEV below this (ms) reads as no measurable noise at all.
_TDEV_FLOOR_MS = 1e-6


@dataclass(frozen=True)
class PhasePoint:
    utc_minute: int      # minute stamp, epoch seconds
    phase_ms: float      # utc_ref - input time, ms; meaningful only within a run
    run_id: float        # the counter map's input_epoch_gps_s


@dataclass(frozen=True)
class ClockStability:
    tau_cross_s: Optional[int]
    window_s: int
    hold_s: int
    drift_ns_per_s: Optional[float]
    n_points: int


def phase_point_from_summary(summary: Optional[dict],
                             utc_minute: int) -> Optional[PhasePoint]:
    """T3's phase this minute, or None when the summary cannot say.

    ``phase_ms = (utc_ref - input_epoch_gps_s - input_index(rtp_ref) /
    input_samprate) * 1e3``.  None for a missing summary, ``utc_ref``,
    ``rtp_ref`` or counter map; a state other than ``ACQUIRED``; a
    ``sample_rate`` its map does not describe (as the shadow comparator
    refuses it); an ``rtp_ref`` beyond the map's reach; or any other failure.
    Never raises.
    """
    try:
        if not isinstance(summary, dict) or summary.get("state") != "ACQUIRED":
            return None
        rtp_ref = int(summary["rtp_ref"])
        utc_ref = float(summary["utc_ref"])
        if not math.isfinite(utc_ref):
            return None
        cmap = CounterMap.from_dict(summary.get("counter_map"))
        if cmap is None or cmap.input_samprate <= 0 or cmap.olen <= 0:
            return None
        # The summary's rtp_ref ticks at its own rate; its map must describe
        # that counter.  Same guard as T3ShadowComparator.t3_minus_t6_ms.
        rate = int(summary["sample_rate"])
        if rate * cmap.ilen != cmap.olen * cmap.input_samprate:
            return None
        idx = T3ShadowComparator._index_near(cmap, rtp_ref)
        if idx is None:
            return None
        epoch = float(cmap.input_epoch_gps_s)
        # utc_ref - epoch first: two large, nearby numbers, so the difference
        # keeps the precision utc_ref itself carries.
        phase_ms = ((utc_ref - epoch) - idx / cmap.input_samprate) * 1e3
        if not math.isfinite(phase_ms):
            return None
        return PhasePoint(utc_minute=int(utc_minute), phase_ms=float(phase_ms),
                          run_id=epoch)
    except Exception:
        return None


def _segments(points: List[PhasePoint]) -> List[Tuple[float, np.ndarray, np.ndarray]]:
    """Split ``points`` into runs.  Each run becomes a regular TAU0_S grid
    from its first minute to its last, NaN where a minute is missing.

    A run ends wherever ``run_id`` changes at all.  The run's phase holds the
    epoch as a constant, so a refined epoch (the probe keeps the least-late
    one) steps the phase as surely as a radiod restart does; splitting there
    keeps either step out of the statistics.  Returns (run_id, grid phase,
    the raw (seconds, phase) pairs for the slope) per segment."""
    out = []
    i = 0
    while i < len(points):
        j = i
        while j + 1 < len(points) and points[j + 1].run_id == points[i].run_id:
            j += 1
        seg = points[i:j + 1]
        t0 = seg[0].utc_minute
        slots = [int(round((p.utc_minute - t0) / TAU0_S)) for p in seg]
        grid = np.full(slots[-1] + 1, np.nan)
        for k, p in zip(slots, seg):
            grid[k] = p.phase_ms
        out.append((seg[0].run_id, grid,
                    np.array([[p.utc_minute - t0, p.phase_ms] for p in seg], float)))
        i = j + 1
    return out


def _mod_terms(x: np.ndarray, m: int) -> Tuple[float, int]:
    """Sum of squared inner sums of second differences at averaging factor
    ``m``, and how many terms it holds.  A term that touches a missing
    minute drops out rather than poison the sum."""
    n = len(x)
    if n < 3 * m + 1:
        return 0.0, 0
    d = x[2 * m:] - 2.0 * x[m:n - m] + x[:n - 2 * m]          # n - 2m values
    bad = ~np.isfinite(d)
    d = np.where(bad, 0.0, d)
    c = np.concatenate(([0.0], np.cumsum(d)))
    cb = np.concatenate(([0], np.cumsum(bad)))
    s = c[m:] - c[:-m]                                         # n - 3m + 1 values
    ok = (cb[m:] - cb[:-m]) == 0
    s = s[ok]
    return float(np.dot(s, s)), int(s.size)


class ClockStabilityEstimator:
    """Holds up to a day of phase points and reads their stability."""

    def __init__(self) -> None:
        self._points: Deque[PhasePoint] = deque(maxlen=STABILITY_HISTORY_MIN)

    def add(self, p: PhasePoint) -> None:
        """Append ``p``.  A minute no later than the last one held (the host
        clock stepped back, or a row repeats) adds nothing, as does a
        non-finite phase."""
        if p is None or not math.isfinite(p.phase_ms):
            return
        if self._points and p.utc_minute <= self._points[-1].utc_minute:
            return
        self._points.append(p)

    def tdev_by_tau(self) -> Dict[int, float]:
        """TDEV (ms) by tau (s), for tau = m * TAU0_S, m = 1, 2, 4, ... while
        m <= n / 4.  Each run segment contributes its terms; segments pool by
        term count.  A tau no segment can reach stays out."""
        return self._tdev(_segments(list(self._points)))

    def _tdev(self, segs) -> Dict[int, float]:
        n = len(self._points)
        out: Dict[int, float] = {}
        m = 1
        while m <= n / 4:
            total, count = 0.0, 0
            for _, grid, _ in segs:
                t, c = _mod_terms(grid, m)
                total += t
                count += c
            if count:
                # TDEV^2 = tau^2 Mod sigma_y^2 / 3, with
                # Mod sigma_y^2 = sum / (2 m^2 tau^2 count): tau cancels.
                out[m * TAU0_S] = math.sqrt(total / (6.0 * m * m * count))
            m *= 2
        return out

    def stability(self) -> ClockStability:
        n = len(self._points)
        unmeasured = ClockStability(tau_cross_s=None, window_s=WINDOW_MIN_S,
                                    hold_s=HOLD_MIN_S, drift_ns_per_s=None,
                                    n_points=n)
        if n < STABILITY_MIN_POINTS:
            return unmeasured
        segs = _segments(list(self._points))
        tdev = self._tdev(segs)
        if not tdev:
            # Too fragmented to evaluate any tau: conservative, unmeasured.
            return unmeasured
        if all(v < _TDEV_FLOOR_MS for v in tdev.values()):
            tau_cross = max(tdev)
        else:
            tau_cross = min(tdev, key=lambda t: (tdev[t], t))
        window = int(min(max(tau_cross, WINDOW_MIN_S), WINDOW_MAX_S))
        hold = int(max(HOLD_MIN_S, window // 4))
        # sigma_y = Mod sigma_y = sqrt(3) TDEV / tau; ms/s -> ns/s is 1e6.
        sigma_y_ns_per_s = math.sqrt(3.0) * tdev[tau_cross] * 1e6 / tau_cross
        drift = None
        _, _, raw = segs[-1]
        # The slope needs two minutes of the current run; with fewer the
        # drift stays unknown (None) rather than guessed.
        if len(raw) >= 2:
            slope_ms_per_s = float(np.polyfit(raw[:, 0], raw[:, 1], 1)[0])
            drift = float(abs(slope_ms_per_s) * 1e6 + sigma_y_ns_per_s)
        return ClockStability(tau_cross_s=int(tau_cross), window_s=window,
                              hold_s=hold, drift_ns_per_s=drift, n_points=n)
