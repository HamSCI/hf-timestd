"""Read an archived arrival stream, grouped into minutes.

Opens the database READ-ONLY.  This harness exists to ask what the cascade
WOULD have decided; it must never be able to alter the record it is asking
about.

`arrival_ms` in L1_all_arrivals counts milliseconds into the MINUTE, so a
tick at second 59 plus 4.24 ms reads 59004.24.  The cascade reasons within
a second, so the reader reduces it modulo 1000.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Set

from hf_timestd.core.admission_cascade import ObservedArrival


@dataclass
class MinuteGroup:
    channel: str
    minute_utc: int
    frequency_mhz: float
    arrivals: List[ObservedArrival] = field(default_factory=list)
    deployed_labels: Set[str] = field(default_factory=set)


def read_minutes(
    db_path,
    *,
    channel: Optional[str] = None,
    start_utc: Optional[int] = None,
    end_utc: Optional[int] = None,
) -> Iterator[MinuteGroup]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql = ("SELECT channel, minute_boundary_utc, frequency_mhz, station,"
               " arrival_ms, corr_snr_db FROM L1_all_arrivals")
        clauses, params = [], []
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if start_utc is not None:
            clauses.append("minute_boundary_utc >= ?")
            params.append(int(start_utc))
        if end_utc is not None:
            clauses.append("minute_boundary_utc < ?")
            params.append(int(end_utc))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY channel, minute_boundary_utc"

        current: Optional[MinuteGroup] = None
        for ch, minute, freq, station, arrival_ms, snr in con.execute(sql, params):
            if current is None or (ch, minute) != (current.channel,
                                                   current.minute_utc):
                if current is not None:
                    yield current
                current = MinuteGroup(channel=ch, minute_utc=int(minute),
                                      frequency_mhz=float(freq))
            current.arrivals.append(ObservedArrival(
                arrival_ms=float(arrival_ms) % 1000.0,
                corr_snr_db=float(snr if snr is not None else 0.0)))
            if station:
                current.deployed_labels.add(str(station))
        if current is not None:
            yield current
    finally:
        con.close()
