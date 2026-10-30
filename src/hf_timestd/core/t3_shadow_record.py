"""Durable T3 shadow record: small JSONL rows, one file per stream per UTC day.

The file name comes from the row's own ``utc_minute`` (Unix seconds), never
from a clock read.  A failed write logs one ERROR per failure streak and
never raises: the record is diagnostic and must not disturb the timing path.
A prune that fails logs one WARNING per streak and retries on the next row.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class T3ShadowRecord:
    DEFAULT_DIR = Path("/var/lib/timestd/t3_shadow")

    def __init__(self, stream: str, directory: Path = DEFAULT_DIR,
                 retention_days: int = 14):
        self.stream = stream
        self.directory = Path(directory)
        self.retention_days = retention_days
        self._failing = False
        self._prune_failing = False
        self._latest_day: Optional[str] = None   # newest day seen
        self._pruned_day: Optional[str] = None   # last day pruned OK
        self._name_re = re.compile(
            rf"^{re.escape(stream)}-(\d{{8}})\.jsonl$")

    def append(self, row: dict) -> None:
        """Append one compact JSON line to the day file of the row's minute."""
        try:
            try:
                day = datetime.fromtimestamp(
                    int(row["utc_minute"]), tz=timezone.utc)
                # allow_nan=False: NaN must fail here, not become a corrupt line.
                line = json.dumps(row, separators=(",", ":"),
                                  allow_nan=False) + "\n"
            except Exception as e:  # noqa: BLE001
                self._note_failure(
                    f"T3ShadowRecord '{self.stream}' BAD ROW dropped ({e!r}); "
                    f"the shadow record is incomplete")
                return
            day_key = day.strftime("%Y%m%d")
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"{self.stream}-{day_key}.jsonl"
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception as e:  # noqa: BLE001
                self._note_failure(
                    f"T3ShadowRecord '{self.stream}' cannot append to "
                    f"{self.directory} ({e}); the shadow record is "
                    f"incomplete")
                return
            self._failing = False
            if self._latest_day is None or day_key > self._latest_day:
                self._latest_day = day_key
                latest_dt = day
            else:
                latest_dt = None
            if self._latest_day != self._pruned_day:
                if latest_dt is None:
                    latest_dt = datetime.strptime(
                        self._latest_day, "%Y%m%d").replace(
                        tzinfo=timezone.utc)
                if self._prune(latest_dt):
                    self._pruned_day = self._latest_day
        except Exception as e:  # noqa: BLE001  never propagate
            logger.debug(f"T3ShadowRecord unexpected failure: {e!r}")

    def _note_failure(self, message: str) -> None:
        if not self._failing:
            logger.error(message)
        self._failing = True

    def _prune(self, today: datetime) -> bool:
        """Remove this stream's day files older than the retention.
        Return True when every stale file went; one WARNING per streak."""
        cutoff = (today - timedelta(days=self.retention_days)
                  ).strftime("%Y%m%d")
        errors = []
        try:
            names = list(self.directory.iterdir())
        except OSError as e:
            names, errors = [], [e]
        for p in names:
            m = self._name_re.match(p.name)
            if m and m.group(1) < cutoff:
                try:
                    p.unlink()
                except OSError as e:
                    errors.append(e)
        if errors:
            if not self._prune_failing:
                logger.warning(
                    f"T3ShadowRecord '{self.stream}' cannot prune "
                    f"{self.directory} ({errors[0]}); files past "
                    f"{self.retention_days} days remain")
            self._prune_failing = True
            return False
        self._prune_failing = False
        return True
