"""Drive the cascade across an archived arrival stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Set

from hf_timestd.core.admission_cascade import ChannelVerdict, adjudicate_channel
from hf_timestd.core.arrival_history import ArrivalHistory
from hf_timestd.replay.archive_reader import read_minutes


@dataclass
class MinuteVerdict:
    channel: str
    minute_utc: int
    verdict: ChannelVerdict
    deployed_labels: Set[str]


def replay(db_path, source, *, floor_snr_db: float, tolerance_ms: float,
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
            continue
        verdict = adjudicate_channel(
            windows=windows, arrivals=group.arrivals,
            eligible=source.eligible_for(group.minute_utc, group.frequency_mhz),
            floor_snr_db=floor_snr_db, history_ok=history.accepts)
        yield MinuteVerdict(channel=group.channel, minute_utc=group.minute_utc,
                            verdict=verdict,
                            deployed_labels=group.deployed_labels)
