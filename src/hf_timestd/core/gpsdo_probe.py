"""
GpsdoProbe — reads `/run/gpsdo/<serial>.json` files produced by the
`gpsdo-monitor` daemon to decide whether this host has an A1 timing
reference (a locked Leo Bodnar GPSDO) or falls back to A0.

Conceptually this is the A-level equivalent of
`chrony_tracking_probe.ChronyTrackingProbe` for T-levels: a small,
testable poller that the `AuthorityRunner` can consult each tick to
get a fresh answer instead of a static TOML value.

Note on naming: there is an unrelated `hf_timestd.core.gpsdo_monitor`
module in this package — it's the sample-counter / anchor watchdog, not
a GPSDO health probe. This file's source of truth is the separate
`mijahauan/gpsdo-monitor` daemon, not that module.

The probe treats a file as usable iff:

  - `schema == "v1"`
  - `written_utc` parses and is no older than
    `staleness_factor * probe_interval_sec` (default 3× the published
    probe interval, floored at 30 s for very frequent probes).
  - `a_level_hint` is present and parseable.

`poll()` returns the string `"A1"` if any fresh device (optionally
filtered by serial) reports `a_level_hint == "A1"`, else `"A0"`. The
authority manager's `a_level_provider` signature is exactly
`Callable[[], str]`, so `probe.poll` can be passed directly.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


@dataclass
class GpsdoSample:
    """One device's view for diagnostics — returned by `poll_detail()`."""

    path: Path
    serial: Optional[str]
    a_level_hint: str          # "A1" | "A0" | "unknown" (malformed file)
    a_level_reason: Optional[str]
    age_sec: Optional[float]
    fresh: bool
    used: bool                 # True iff the probe treated this file as valid


class GpsdoProbe:
    """Poll `/run/gpsdo/*.json` and return a host-level A-level."""

    DEFAULT_RUN_DIR = Path("/run/gpsdo")
    MIN_STALENESS_SEC = 30.0       # floor so a very fast probe interval (1 s)
                                   # doesn't reject the file at 3 s
    DEFAULT_STALENESS_FACTOR = 3.0

    def __init__(
        self,
        run_dir: Path = DEFAULT_RUN_DIR,
        *,
        serial: Optional[str] = None,
        staleness_factor: float = DEFAULT_STALENESS_FACTOR,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.serial = serial
        self.staleness_factor = float(staleness_factor)
        self._now = now_fn

    # --- public ---------------------------------------------------------

    def poll(self) -> str:
        """Return `"A1"` if any fresh, usable device reports A1, else `"A0"`.

        A file that is missing / unreadable / malformed / stale is treated
        as not-A1 but does NOT raise — the authority manager polls on a
        loop and we want a single bad read to degrade gracefully."""
        samples = self.poll_detail()
        if any(s.used and s.a_level_hint == "A1" for s in samples):
            return "A1"
        return "A0"

    def poll_detail(self) -> List[GpsdoSample]:
        """Return per-device diagnostics. Useful for the CLI / TUI and
        for logging *why* the probe chose A0."""
        out: List[GpsdoSample] = []
        for path in self._enumerate_files():
            out.append(self._read_one(path))
        return out

    # --- internal -------------------------------------------------------

    def _enumerate_files(self) -> List[Path]:
        if not self.run_dir.is_dir():
            return []
        files = sorted(self.run_dir.glob("*.json"))
        files = [f for f in files if f.name != "index.json"]
        if self.serial is not None:
            want = f"{self.serial}.json"
            files = [f for f in files if f.name == want]
        return files

    def _read_one(self, path: Path) -> GpsdoSample:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            log.debug("gpsdo-monitor file %s unreadable: %s", path, e)
            return GpsdoSample(path=path, serial=None, a_level_hint="unknown",
                               a_level_reason=None, age_sec=None,
                               fresh=False, used=False)

        if not isinstance(data, dict) or data.get("schema") != "v1":
            return GpsdoSample(path=path, serial=None, a_level_hint="unknown",
                               a_level_reason=None, age_sec=None,
                               fresh=False, used=False)

        serial = ((data.get("device") or {}).get("serial")) if isinstance(
            data.get("device"), dict) else None
        a_hint = str(data.get("a_level_hint") or "unknown")
        a_reason = data.get("a_level_reason")

        age_sec = self._age_seconds(data.get("written_utc"))
        max_age = self._max_age_sec(data.get("probe_interval_sec"))
        fresh = age_sec is not None and age_sec <= max_age
        used = fresh and a_hint in ("A1", "A0")
        return GpsdoSample(
            path=path, serial=serial, a_level_hint=a_hint,
            a_level_reason=a_reason, age_sec=age_sec, fresh=fresh, used=used,
        )

    def _max_age_sec(self, probe_interval_sec: object) -> float:
        try:
            interval = float(probe_interval_sec) if probe_interval_sec is not None else 10.0
        except (TypeError, ValueError):
            interval = 10.0
        return max(self.MIN_STALENESS_SEC, self.staleness_factor * interval)

    def _age_seconds(self, written_utc: object) -> Optional[float]:
        if not isinstance(written_utc, str):
            return None
        try:
            # gpsdo-monitor writes ISO-8601 with trailing 'Z'. Python's
            # fromisoformat accepts 'Z' from 3.11 onwards — our min.
            dt = datetime.fromisoformat(written_utc.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = self._now() - dt.timestamp()
        return max(0.0, age)
