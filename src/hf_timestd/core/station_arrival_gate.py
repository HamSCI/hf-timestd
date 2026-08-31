"""Decide which time stations are present from WHERE their ticks arrive.

Every candidate is judged INDEPENDENTLY against a window derived from
geometry, so all outcomes are expressible: several stations, one, or
none.  That is the whole point.  The discriminator this replaces could
only ever emit a PAIR -- it assigned "early" and "late" to the two
strongest peaks -- so it manufactured a second station whenever only one
was on the air, and it had no way to say "nothing is here".

Measured on AC0G-B4, 2026-08-31, over nine hours of shared-channel
ensembles:

  * conf=0.50 -- an exact coin flip -- on 60-70% of shared ensembles,
    and never on the WWV-only channels where the question cannot arise
  * label-versus-arrival disagreement of 34-79% per channel
  * on SHARED_5000, every one of 297 WWVH-labelled ensembles sat at the
    WWV delay: WWVH was not there and was reported anyway
  * 1,331 "measured delay diff differs significantly from expected"
    warnings in six hours, each followed by the assignment proceeding
    unchanged, because the geometric check only logged

BPM IS WHY ORDER FAILS.  Its tick tone is 1000 Hz, identical to WWV, so
no tone-based test can separate them.  And in a forced pair the late
peak becomes WWVH by construction, so BPM was labelled WWVH -- giving a
residual of 39.7 - 22.96 = +16.7 ms against SHARED_2500's observed +16.1
ms mode.  By arrival the two are 17 ms apart and the question is easy.

WHY GEOMETRY IS ENOUGH.  From EM38ww every plausible propagation mode
lands each station inside a window under a millisecond wide:

    WWV  (1119 km)   3.73 ms ground .. 4.24 ms 1-hop F2
    WWVH (6600 km)  22.02 ms ground .. 22.82 ms 3-hop F2
    BPM (11504 km)  38.37 ms ground .. 39.66 ms 5-hop F2

Multipath spreads arrivals WITHIN a window and cannot carry one across
the 18 ms that separates WWV from WWVH.  The one genuinely different
path -- long-path WWVH the other way round the world -- arrives about
111 ms late, nowhere near any window.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

__all__ = ["StationWindow", "GateVerdict", "arrival_windows",
           "gate_arrivals", "can_discriminate"]

#: How far BEFORE the modelled delay an arrival may still count.  The
#: model uses an F2 hop; a ground-wave or E-layer path arrives slightly
#: earlier, by about half a millisecond at these ranges.
DEFAULT_EARLY_MS = 1.5

#: ...and how far after, which carries the real uncertainty: extra hops,
#: chordal paths, a raised layer.  Generous, and still far short of the
#: 18 ms that separates the nearest pair.
DEFAULT_LATE_MS = 3.0


@dataclass(frozen=True)
class StationWindow:
    station: str
    min_ms: float
    max_ms: float

    def contains(self, arrival_ms: float) -> bool:
        return self.min_ms <= float(arrival_ms) <= self.max_ms


@dataclass(frozen=True)
class GateVerdict:
    #: Stations judged present, in window order.  May be empty, and an
    #: empty verdict is a real answer rather than a failure to decide.
    present: Tuple[str, ...]
    #: Arrivals that matched each present station.  Carried because the
    #: three candidates lie on three INDEPENDENT great-circle paths from
    #: one receiver -- 1119 km, 6600 km and 11504 km -- observed on the
    #: same frequency at the same instant.  Every instrumental term is
    #: therefore common-mode and cancels between paths, so the per-path
    #: arrival series is a propagation measurement in its own right, not
    #: merely a by-product of deciding who is on the air.
    matched: Dict[str, Tuple[float, ...]]
    #: Arrivals matching no window.  Reported rather than assigned to
    #: whichever station happens to be nearest -- an unexplained arrival
    #: is evidence about the channel, not about a station.
    unmatched: Tuple[float, ...]
    windows: Dict[str, StationWindow]

    @property
    def ambiguous(self) -> bool:
        """True when an arrival could not be explained by any candidate."""
        return bool(self.unmatched)


#: Coverage factor applied to the reference uncertainty.
DEFAULT_K = 3.0


def arrival_windows(
    expected_delays_ms: Dict[str, float],
    early_ms: float = DEFAULT_EARLY_MS,
    late_ms: float = DEFAULT_LATE_MS,
    reference_sigma_ms: float = 0.0,
    k: float = DEFAULT_K,
) -> Dict[str, StationWindow]:
    """Build per-station windows, refusing any set that would overlap.

    Overlapping windows would let one station's arrival satisfy another,
    which is the failure this module exists to prevent.  Better to refuse
    at construction than to answer confidently from a broken partition.
    """
    # An arrival is only as well known as the clock it was measured on.
    # The Offset Judge publishes that uncertainty; carrying it here is
    # what lets the gate stop cleanly when T6 goes away instead of
    # answering confidently on a ruler that cannot resolve 18 ms.
    slack = k * max(0.0, float(reference_sigma_ms))
    windows = {
        s: StationWindow(s, float(d) - early_ms - slack, float(d) + late_ms + slack)
        for s, d in expected_delays_ms.items()
    }
    ordered = sorted(windows.values(), key=lambda w: w.min_ms)
    for a, b in zip(ordered, ordered[1:]):
        if a.max_ms >= b.min_ms:
            raise ValueError(
                f"windows for {a.station} ({a.min_ms:.2f}-{a.max_ms:.2f} ms) "
                f"and {b.station} ({b.min_ms:.2f}-{b.max_ms:.2f} ms) overlap: "
                f"tolerances of -{early_ms}/+{late_ms} ms are too wide for a "
                f"separation of {b.min_ms + early_ms - a.max_ms + late_ms:.2f} ms"
            )
    return windows


def gate_arrivals(
    arrivals_ms: Iterable[float],
    windows: Dict[str, StationWindow],
) -> GateVerdict:
    """Which candidates does this set of arrivals support?

    Each station is tested on its own.  Two arrivals inside one window
    are two hops of one signal, not two stations; an arrival inside none
    names nobody.
    """
    arrivals = [float(a) for a in arrivals_ms]
    ordered = sorted(windows.values(), key=lambda w: w.min_ms)
    matched = {
        w.station: tuple(a for a in arrivals if w.contains(a))
        for w in ordered
    }
    present = tuple(w.station for w in ordered if matched[w.station])
    unmatched = tuple(
        a for a in arrivals
        if not any(w.contains(a) for w in ordered)
    )
    return GateVerdict(
        present=present,
        matched={s: v for s, v in matched.items() if v},
        unmatched=unmatched,
        windows=dict(windows),
    )


def can_discriminate(
    expected_delays_ms: Dict[str, float],
    reference_sigma_ms: float = 0.0,
    early_ms: float = DEFAULT_EARLY_MS,
    late_ms: float = DEFAULT_LATE_MS,
    k: float = DEFAULT_K,
) -> bool:
    """Can this ruler still tell these stations apart?

    Ask before gating, so a degraded timing reference produces an
    abstention rather than a confident wrong answer.
    """
    try:
        arrival_windows(expected_delays_ms, early_ms, late_ms,
                        reference_sigma_ms, k)
        return True
    except ValueError:
        return False
