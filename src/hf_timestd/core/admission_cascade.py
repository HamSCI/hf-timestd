"""Which measurements may reach the timing path, and which may not.

A measurement counts when it clears the noise floor, lands inside exactly
one geometric window, and stays consistent with history.  Nothing else
counts.  Abstention is the correct output rather than a degraded
measurement, so exactly one of the seven states carries a value.

The rule this replaces had to emit a station label whether or not evidence
supported one, which is a machine for producing measurements nobody can
stand behind.  See docs/superpowers/specs/2026-09-01-timing-admission-three-keys-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Set

from hf_timestd.core.station_arrival_gate import StationWindow


class AdmissionState(str, Enum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"   # we did not look (BPM schedule only)
    BELOW_FLOOR = "BELOW_FLOOR"     # path delivered nothing detectable
    OFF_MODEL = "OFF_MODEL"         # path delivered; our model missed it
    AMBIGUOUS = "AMBIGUOUS"         # cannot say whose signal it is
    INCONSISTENT = "INCONSISTENT"   # lone outlier against the track
    DEGRADED = "DEGRADED"           # present, unusable
    ADMITTED = "ADMITTED"           # the only state carrying a value


class ChannelState(str, Enum):
    CHANNEL_SILENT = "CHANNEL_SILENT"
    CHANNEL_UNIDENTIFIED = "CHANNEL_UNIDENTIFIED"
    CHANNEL_PARTIAL = "CHANNEL_PARTIAL"


@dataclass(frozen=True)
class ObservedArrival:
    arrival_ms: float
    corr_snr_db: float


@dataclass(frozen=True)
class StationVerdict:
    station: str
    state: AdmissionState
    arrival_ms: Optional[float]
    reason: str


@dataclass(frozen=True)
class ChannelVerdict:
    stations: Dict[str, StationVerdict]
    channel_state: ChannelState
    unclaimed_ms: List[float]

    @property
    def admitted_count(self) -> int:
        return sum(1 for v in self.stations.values()
                   if v.state is AdmissionState.ADMITTED)


HistoryCheck = Callable[[str, float], bool]


def adjudicate_channel(
    *,
    windows: Mapping[str, StationWindow],
    arrivals: Iterable[ObservedArrival],
    eligible: Set[str],
    floor_snr_db: float,
    history_ok: HistoryCheck,
) -> ChannelVerdict:
    """Run the cascade for every station this channel could carry."""
    arrivals = list(arrivals)
    above = [a for a in arrivals if a.corr_snr_db >= floor_snr_db]

    stations: Dict[str, StationVerdict] = {}
    claimed: List[float] = []

    for station, window in windows.items():
        if station not in eligible:
            stations[station] = StationVerdict(
                station, AdmissionState.NOT_ELIGIBLE, None,
                "station not a candidate this minute")
            continue

        inside = [a for a in above if window.contains(a.arrival_ms)]
        if not inside:
            # Nothing in the direct-mode window.  Did anything land in the
            # scatter tail?  That is this station, delayed by sidescatter —
            # real, and not usable as a timing measurement.
            scattered = [a for a in above
                         if window.admits(a.arrival_ms)
                         and not window.contains(a.arrival_ms)]
            if scattered:
                claimed.extend(a.arrival_ms for a in scattered)
                stations[station] = StationVerdict(
                    station, AdmissionState.DEGRADED, None,
                    "arrival lies in the scatter tail, not the direct modes")
                continue
            stations[station] = StationVerdict(
                station, AdmissionState.BELOW_FLOOR, None,
                f"nothing above {floor_snr_db:.1f} dB in window")
            continue

        # More than one window claiming the same arrival means we cannot
        # say whose it is.  Refuse rather than pick.
        contested = [
            a for a in inside
            if sum(1 for w in windows.values() if w.contains(a.arrival_ms)) > 1
        ]
        if contested:
            stations[station] = StationVerdict(
                station, AdmissionState.AMBIGUOUS, None,
                "arrival satisfies more than one station window")
            claimed.extend(a.arrival_ms for a in inside)
            continue

        best = max(inside, key=lambda a: a.corr_snr_db)
        claimed.append(best.arrival_ms)

        if not history_ok(station, best.arrival_ms):
            stations[station] = StationVerdict(
                station, AdmissionState.INCONSISTENT, None,
                "arrival disagrees with the recent track")
            continue

        stations[station] = StationVerdict(
            station, AdmissionState.ADMITTED, best.arrival_ms, "")

    unclaimed = [a.arrival_ms for a in above if a.arrival_ms not in claimed]

    if any(v.state is AdmissionState.ADMITTED for v in stations.values()):
        channel_state = ChannelState.CHANNEL_PARTIAL
    elif above:
        channel_state = ChannelState.CHANNEL_UNIDENTIFIED
    else:
        channel_state = ChannelState.CHANNEL_SILENT

    return ChannelVerdict(stations=stations, channel_state=channel_state,
                          unclaimed_ms=unclaimed)
