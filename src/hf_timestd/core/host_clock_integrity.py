"""One verdict on the host clock, from witnesses that already exist.

Why this module exists.  On 2026-09-04 AC0G-B4's host clock ran 11.6 s slow
for thirteen hours while `chronyc tracking` reported it within 0.1 ms.  The
FUSE refclock carries `trust`, its product follows the host clock, and so
chrony had nothing to disagree with.  Three other measurements on the same
host saw the truth and each one stopped short of saying so:

* the authority manager computed "T6<->T2:11679.507ms" and tagged it
  ":advisory", because a system-clock witness must not demote a
  GPS-disciplined anchor — a correct rule about the ANCHOR that says
  nothing about the CLOCK;
* the LB-1421 probe computed a 12 s host-versus-GPS gap and returned None,
  which downstream read as "no fix";
* gpsdo-monitor's PPS study measured 999.91 ms per true second.

This module takes those measurements and returns a verdict.  It never
touches chrony, tier selection, or the anchor; it exposes.  Pure logic, no
I/O, no clock reads — the manager supplies the numbers and the time, so the
day can be replayed in tests.

Verdicts, worst wins:

    fault        a whole-second-class error: any pair past ``fault_ms``,
                 or the GPS integer second outside its emission window
    suspect      any pair past its own cross-check bound, or the PPS rate
                 past ``rate_suspect_ppm``
    ok           every witness present agrees with the host clock
    unwitnessed  no witness supplied a number this tick
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

#: A pair disagreement past this many ms means a whole-second-class fault,
#: whatever the pair's own bound says.  Same value the asymmetric T3<->T2
#: rule uses in authority_manager.
DEFAULT_FAULT_MS = 1000.0

#: The LB-1421 probe's window for (host_now - fix_age) - pps_utc_sec: an
#: RMC sentence describing the last PPS edge arrives up to ~1.5 s after it.
#: Outside the window the host clock and the GPS second disagree by whole
#: seconds.  Mirrors ``lb1421_t5_probe._read_once``.
DEFAULT_GPS_SECOND_WINDOW_S: Tuple[float, float] = (-0.5, 1.5)

#: PPS period measured by the host's OS clock resolves to roughly 17 ppm
#: over a 60 s window (1 ms / 60 s).  50 ppm leaves margin above that
#: resolution and sits well below the 90-300 ppm seen on 2026-09-04.
DEFAULT_RATE_SUSPECT_PPM = 50.0

_RANK = {"unwitnessed": 0, "ok": 1, "suspect": 2, "fault": 3}


@dataclass(frozen=True)
class HostClockWitness:
    """One witness's number and the bound it was judged against."""

    name: str        # "T2" / "T4" (pair), "lb1421", "pps_rate"
    kind: str        # "pair_ms" | "gps_second_s" | "rate_ppm"
    value: float
    bound: float     # ms for pairs, s (half-width of the window) for GPS, ppm for rate
    exceeded: bool


@dataclass(frozen=True)
class HostClockVerdict:
    verdict: str
    reason: str
    witnesses: Tuple[HostClockWitness, ...]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "witnesses": {
                w.name: {"kind": w.kind, "value": w.value,
                         "bound": w.bound, "exceeded": w.exceeded}
                for w in self.witnesses
            },
        }


def assess(
    *,
    pair_disagreements: Mapping[str, Tuple[float, float]],
    gps_second_delta_s: Optional[float],
    rate_ppm: Optional[float],
    fault_ms: float = DEFAULT_FAULT_MS,
    gps_second_window_s: Tuple[float, float] = DEFAULT_GPS_SECOND_WINDOW_S,
    rate_suspect_ppm: float = DEFAULT_RATE_SUSPECT_PPM,
) -> HostClockVerdict:
    """Combine the witnesses into one verdict.

    ``pair_disagreements`` maps a system-clock witness tier to
    ``(|delta_ms|, bound_ms)`` — the numbers the authority manager's pair
    check already computes for a witness against an rtp-frame active tier.
    ``gps_second_delta_s`` is the LB-1421 probe's host-versus-GPS gap.
    ``rate_ppm`` is the host clock's rate against the GPSDO PPS.
    """
    witnesses = []
    verdict = "unwitnessed"
    reasons = []

    def _raise(to: str) -> None:
        nonlocal verdict
        if _RANK[to] > _RANK[verdict]:
            verdict = to

    for name, (delta_ms, bound_ms) in pair_disagreements.items():
        exceeded = abs(delta_ms) > bound_ms
        witnesses.append(HostClockWitness(name, "pair_ms", float(delta_ms),
                                          float(bound_ms), exceeded))
        if abs(delta_ms) > fault_ms:
            _raise("fault")
            reasons.append(f"{name} disagrees by {delta_ms:.1f} ms "
                           f"(> {fault_ms:.0f} ms)")
        elif exceeded:
            _raise("suspect")
            reasons.append(f"{name} disagrees by {delta_ms:.3f} ms "
                           f"(> {bound_ms:.3f} ms)")
        else:
            _raise("ok")

    if gps_second_delta_s is not None:
        lo, hi = gps_second_window_s
        exceeded = not (lo <= gps_second_delta_s <= hi)
        witnesses.append(HostClockWitness("lb1421", "gps_second_s",
                                          float(gps_second_delta_s),
                                          float(hi - lo) / 2.0, exceeded))
        if exceeded:
            _raise("fault")
            reasons.append(f"host minus GPS second {gps_second_delta_s:+.3f} s "
                           f"(outside {lo:+.1f}..{hi:+.1f} s)")
        else:
            _raise("ok")

    if rate_ppm is not None:
        exceeded = abs(rate_ppm) > rate_suspect_ppm
        witnesses.append(HostClockWitness("pps_rate", "rate_ppm",
                                          float(rate_ppm),
                                          float(rate_suspect_ppm), exceeded))
        if exceeded:
            _raise("suspect")
            reasons.append(f"host rate {rate_ppm:+.1f} ppm against PPS "
                           f"(> {rate_suspect_ppm:.0f} ppm)")
        else:
            _raise("ok")

    if verdict == "unwitnessed":
        reason = "no host-clock witness reported this tick"
    elif verdict == "ok":
        reason = "every witness agrees with the host clock"
    else:
        reason = "; ".join(reasons)
    return HostClockVerdict(verdict, reason, tuple(witnesses))


class HostClockAlarm:
    """Decide when a verdict deserves a log line.

    ``update`` returns ``"enter"`` on the first tick of a non-ok verdict and
    on any change between suspect and fault, ``"repeat"`` once per
    ``repeat_sec`` while the condition holds, ``"clear"`` on the tick that
    returns to ok, and None otherwise.  ``since`` holds the time of the
    first non-ok tick of the current episode.  ``unwitnessed`` neither
    alarms nor clears: no news is not good news, and not bad news either.
    """

    def __init__(self, repeat_sec: float = 3600.0) -> None:
        self.repeat_sec = float(repeat_sec)
        self.since: Optional[float] = None
        self._last_verdict: Optional[str] = None
        self._last_spoke: Optional[float] = None

    def update(self, verdict: HostClockVerdict, now: float) -> Optional[str]:
        v = verdict.verdict
        if v == "unwitnessed":
            return None
        if v == "ok":
            if self.since is None:
                return None
            self.since = None
            self._last_verdict = None
            self._last_spoke = None
            return "clear"
        # suspect or fault
        if self.since is None:
            self.since = now
        if v != self._last_verdict:
            self._last_verdict = v
            self._last_spoke = now
            return "enter"
        if self._last_spoke is not None and now - self._last_spoke >= self.repeat_sec:
            self._last_spoke = now
            return "repeat"
        return None
