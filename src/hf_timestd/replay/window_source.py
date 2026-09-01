"""Recompute the geometry a past minute SHOULD have had.

The archive's `model_expected_ms` records the prediction that was actually
used, and before 2026-09-01 that prediction was roughly doubled on any band
sitting under foF2 — 10 MHz read WWV 8.49 / WWVH 44.49 against a true
4.24 / 22.85.  Replay therefore recomputes windows from the fixed predictor
rather than reusing what was stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Set

from hf_timestd.core.station_arrival_gate import (
    StationWindow, arrival_windows, eligible_candidates,
)


class WindowSource:
    def __init__(self, receiver_lat: float, receiver_lon: float,
                 reference_sigma_ms: float = 0.7) -> None:
        from hamsci_dsp.propagation.arrival_matrix import ArrivalPatternMatrix

        self.reference_sigma_ms = float(reference_sigma_ms)
        self._matrix = ArrivalPatternMatrix(receiver_lat=receiver_lat,
                                            receiver_lon=receiver_lon)

    def _expected(self, minute_utc: int, frequency_mhz: float) -> Dict[str, float]:
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        matrix = self._matrix.get_expected_arrivals(dt)
        out: Dict[str, float] = {}
        for station in ("WWV", "WWVH", "BPM"):
            arrival = matrix.get_arrival(station, frequency_mhz)
            if arrival is not None:
                out[station] = float(arrival.expected_delay_ms)
        return out

    def eligible_for(self, minute_utc: int, frequency_mhz: float) -> Set[str]:
        dt = datetime.fromtimestamp(minute_utc, tz=timezone.utc)
        candidates = eligible_candidates(
            self._expected(minute_utc, frequency_mhz),
            utc_minute=dt.minute, utc_hour=dt.hour,
            frequency_mhz=frequency_mhz)
        return set(candidates)

    def windows_for(self, minute_utc: int,
                    frequency_mhz: float) -> Dict[str, StationWindow]:
        expected = self._expected(minute_utc, frequency_mhz)
        if not expected:
            return {}
        try:
            return arrival_windows(
                expected, reference_sigma_ms=self.reference_sigma_ms)
        except ValueError:
            # Overlapping windows: the geometry cannot separate these
            # stations at this sigma.  Refusing is the honest answer.
            return {}
