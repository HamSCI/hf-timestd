"""Durable T3 shadow record: small JSONL rows, one file per stream per UTC day.

The file name comes from the row's own ``utc_minute`` (Unix seconds), never
from a clock read.  A failed write logs one ERROR per failure streak and
never raises: the record is diagnostic and must not disturb the timing path.
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
        self._last_day: Optional[str] = None
        self._name_re = re.compile(
            rf"^{re.escape(stream)}-(\d{{8}})\.jsonl$")

    def append(self, row: dict) -> None:
        """Append one compact JSON line to the day file of the row's minute."""
        try:
            day = datetime.fromtimestamp(
                int(row["utc_minute"]), tz=timezone.utc)
            day_key = day.strftime("%Y%m%d")
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{self.stream}-{day_key}.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            if day_key != self._last_day:
                self._last_day = day_key
                self._prune(day)
        except Exception as e:  # noqa: BLE001
            if not self._failing:
                logger.error(
                    f"T3ShadowRecord '{self.stream}' cannot append to "
                    f"{self.directory} ({e}); the shadow record is "
                    f"incomplete")
            self._failing = True
            return
        self._failing = False

    def _prune(self, today: datetime) -> None:
        """Remove this stream's day files older than the retention."""
        cutoff = (today - timedelta(days=self.retention_days)
                  ).strftime("%Y%m%d")
        try:
            for p in self.directory.iterdir():
                m = self._name_re.match(p.name)
                if m and m.group(1) < cutoff:
                    p.unlink()
        except OSError as e:
            logger.debug(f"T3ShadowRecord prune skipped: {e}")
