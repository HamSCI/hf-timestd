"""Drive the cascade across an archived arrival stream."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional, Protocol, Set

from hf_timestd.core.admission_cascade import ChannelVerdict, adjudicate_channel
from hf_timestd.core.arrival_history import ArrivalHistory
from hf_timestd.replay.archive_reader import read_minutes


class WindowSource(Protocol):
    """Duck-typed geometry provider for ionospheric windows."""

    def windows_for(self, minute_utc: int, frequency_mhz: float) -> Dict:
        """Return a dict of station names to StationWindow objects, or {} if refused."""
        ...

    def eligible_for(self, minute_utc: int, frequency_mhz: float) -> Set[str]:
        """Return a set of eligible station names for this minute and frequency."""
        ...


@dataclass
class MinuteVerdict:
    channel: str
    minute_utc: int
    verdict: Optional[ChannelVerdict]
    deployed_labels: Set[str]
    geometry_refused: bool = False
    skipped_null_snr: int = 0


def replay(db_path: str | Path, source: WindowSource, *, floor_snr_db: float, tolerance_ms: float,
           lookback: int, reacquire_after: int, **filters
           ) -> Iterator[MinuteVerdict]:
    histories = {}
    for group in read_minutes(db_path, **filters):
        history = histories.setdefault(
            group.channel,
            ArrivalHistory(tolerance_ms=tolerance_ms, lookback=lookback,
                           reacquire_after=reacquire_after))
        windows = source.windows_for(group.minute_utc, group.frequency_mhz)
        if not windows:
            yield MinuteVerdict(channel=group.channel, minute_utc=group.minute_utc,
                                verdict=None, deployed_labels=group.deployed_labels,
                                geometry_refused=True,
                                skipped_null_snr=group.skipped_null_snr)
            continue
        verdict = adjudicate_channel(
            windows=windows, arrivals=group.arrivals,
            eligible=source.eligible_for(group.minute_utc, group.frequency_mhz),
            floor_snr_db=floor_snr_db, history_ok=history.accepts)
        yield MinuteVerdict(channel=group.channel, minute_utc=group.minute_utc,
                            verdict=verdict, deployed_labels=group.deployed_labels,
                            skipped_null_snr=group.skipped_null_snr)
