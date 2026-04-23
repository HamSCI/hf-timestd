"""
CoarseTimeSource — consumes a system-clock-independent UTC estimate so
the authority manager can bootstrap through the circular dependency
where hf-timestd needs the system clock within its tone-detection
window to find HF ticks, but the system clock may be arbitrarily wrong
at boot (per the 2026-04-20 incident: ~107 s drift).

The producer side — extracting wall-clock UTC from WWV/WWVH BCD or CHU
FSK payload decodes that work without a correct system clock — is a
separate integration inside timestd-metrology and is NOT part of this
commit. This module defines the consumer contract (schema v1 JSON at
/run/hf-timestd/coarse_time.json) and the reader used by the bootstrap
coordinator.

Schema v1 (expected):

    {
      "schema": "v1",
      "utc_published": "2026-04-23T14:32:17.123456Z",
      "source": "BCD" | "FSK" | "OTHER",
      "station": "WWV",
      "coarse_utc": "2026-04-23T14:32:00Z",
      "max_error_sec": 1.0,
      "freshness_sec": 60.0
    }

Consumers treat `utc_published` under the same freshness rule as
authority.json (default 60 s). The `coarse_utc` field is the estimated
wall-clock UTC at the moment the observation was made, NOT the
publication time; downstream logic compares it to the current system
clock to decide whether a step is needed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

log = logging.getLogger(__name__)

_SUPPORTED_SCHEMAS = {"v1"}


@dataclass
class CoarseTimeObservation:
    """One system-clock-independent UTC estimate."""
    utc: datetime           # estimated wall-clock UTC at observation time
    source: str             # "BCD", "FSK", "OTHER"
    station: str            # "WWV", "WWVH", "CHU", ...
    max_error_sec: float    # 1σ or worst-case uncertainty bound


class CoarseTimeSource(Protocol):
    def read(self) -> Optional[CoarseTimeObservation]: ...


class CoarseTimeFileSource:
    """Reads coarse_time.json published by the (future) metrology-side
    coarse-time writer.

    Returns None (rather than raising) in every error path — missing
    file, parse error, unsupported schema, stale publication — so the
    bootstrap coordinator can treat "no coarse time available" as a
    distinct state from "file says clock is right."
    """

    def __init__(
        self,
        path: Path = Path("/run/hf-timestd/coarse_time.json"),
        default_freshness_sec: float = 60.0,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.path = Path(path)
        self.default_freshness_sec = float(default_freshness_sec)
        self.now_fn = now_fn

    def read(self) -> Optional[CoarseTimeObservation]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            log.debug("coarse_time.json read error: %s", e)
            return None

        if data.get("schema") not in _SUPPORTED_SCHEMAS:
            log.debug("coarse_time.json unsupported schema: %r", data.get("schema"))
            return None

        try:
            pub = _parse_iso_z(str(data["utc_published"]))
            coarse = _parse_iso_z(str(data["coarse_utc"]))
            source = str(data["source"])
            station = str(data["station"])
            max_err = float(data["max_error_sec"])
        except (KeyError, TypeError, ValueError) as e:
            log.debug("coarse_time.json field error: %s", e)
            return None

        freshness_sec = float(data.get("freshness_sec", self.default_freshness_sec))
        if (self.now_fn() - pub).total_seconds() > freshness_sec:
            return None

        return CoarseTimeObservation(
            utc=coarse,
            source=source,
            station=station,
            max_error_sec=max_err,
        )


def _parse_iso_z(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1]
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
