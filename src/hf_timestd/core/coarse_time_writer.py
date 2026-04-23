"""
CoarseTimeWriter — publishes /run/hf-timestd/coarse_time.json for the
authority manager's bootstrap coordinator (METROLOGY.md §4.5 / 2c).

Producer side of the contract consumed by
`hf_timestd.core.coarse_time_source.CoarseTimeFileSource`. When an HF
time-station decode (WWV/WWVH BCD or CHU FSK) yields an absolute UTC
that's independent of the local system clock, this writer publishes
it so the bootstrap coordinator can compare against the system clock
and — if far enough off — invoke `chronyc makestep`.

Precision. The decoders recover wall-clock UTC to minute precision
(day/hour/minute from the broadcast time code). `coarse_utc` is
therefore minute-level; `max_error_sec` reflects that (~60 s for
minute-only decoders). Consumers that need tighter precision must
wait for steady-state Fusion (T3) — this file exists to recover the
*order of magnitude* of the clock error at bootstrap, not sub-second
precision.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = "v1"

DEFAULT_PATH = Path("/run/hf-timestd/coarse_time.json")


class CoarseTimeWriter:
    """Writes coarse_time.json atomically on each successful decode."""

    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        freshness_sec: float = 120.0,
    ):
        self.path = Path(path)
        self.freshness_sec = float(freshness_sec)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # /run/hf-timestd is created by the service unit's
            # RuntimeDirectory=; during tests the parent dir is
            # writable, so this only bites when run without the unit.
            log.debug("CoarseTimeWriter: cannot mkdir %s", self.path.parent)

    def publish(
        self,
        *,
        source: str,
        station: str,
        coarse_utc: datetime,
        max_error_sec: float,
        utc_published: Optional[datetime] = None,
    ) -> None:
        """Write a schema v1 coarse-time record.

        Args:
            source: "BCD" (WWV/WWVH) | "FSK" (CHU) | "OTHER".
            station: Transmitter call (e.g., "WWV", "WWVH", "CHU").
            coarse_utc: UTC recovered from the broadcast time code.
                Typically minute-precision (seconds==0).
            max_error_sec: Uncertainty bound of coarse_utc. Minute-only
                decoders should pass ~60.0; sub-second decoders less.
            utc_published: When the record was written; defaults to now
                (system clock — used by the reader's freshness check,
                NOT for coarse_utc itself).
        """
        now = utc_published if utc_published is not None else datetime.now(timezone.utc)
        payload = {
            "schema": SCHEMA_VERSION,
            "utc_published": _iso_z(now),
            "source": source,
            "station": station,
            "coarse_utc": _iso_z(coarse_utc),
            "max_error_sec": float(max_error_sec),
            "freshness_sec": self.freshness_sec,
        }
        self._atomic_write(payload)

    def _atomic_write(self, payload: dict) -> None:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.path.parent),
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, self.path)
        except OSError as e:
            log.warning("CoarseTimeWriter: failed to write %s: %s", self.path, e)


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
