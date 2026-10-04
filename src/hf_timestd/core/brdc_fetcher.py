"""Keep the day's multi-GNSS broadcast navigation file fresh on disk.

BKG publishes ``BRDC00WRD_S`` from live IGS streams and refreshes it every 15
minutes, so a station gets ephemerides valid for *now*, not only yesterday's.
No login is needed (unlike NASA CDDIS).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from hf_timestd.core.broadcast_ephemeris import BroadcastEphemeris

logger = logging.getLogger(__name__)

# ─── ephemeris fetcher ─────────────────────────────────────────────────────

DEFAULT_BRDC_URL = ('https://igs.bkg.bund.de/root_ftp/IGS/BRDC/{year}/{doy}/'
                    'BRDC00WRD_S_{year}{doy}0000_01D_MN.rnx.gz')


def brdc_url(day: datetime, template: str = DEFAULT_BRDC_URL) -> str:
    return template.format(year=f'{day.year:04d}', doy=f'{day.timetuple().tm_yday:03d}')


class BrdcFetcher:
    """Keep today's (and, near midnight, yesterday's) BRDC file fresh on disk
    and loaded into a :class:`BroadcastEphemeris`.  BKG refreshes the file
    every 15 minutes; so does this.  A failed fetch keeps what is loaded and
    says so; it never raises into the data loop."""

    def __init__(self, cache_dir: Path, ephem: BroadcastEphemeris,
                 url_template: str = DEFAULT_BRDC_URL, refresh_s: float = 900.0,
                 opener=urllib.request.urlopen):
        self.cache_dir = Path(cache_dir)
        self.ephem = ephem
        self.url_template = url_template
        self.refresh_s = refresh_s
        self._open = opener
        self._stop = threading.Event()

    def _days(self, now: datetime) -> List[datetime]:
        days = [now]
        if now.hour < 4:          # yesterday's late ephemerides still apply
            days.insert(0, now - timedelta(days=1))
        return days

    def refresh_once(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        loaded = 0
        for day in self._days(now):
            url = brdc_url(day, self.url_template)
            dest = self.cache_dir / url.rsplit('/', 1)[-1]
            tmp = dest.with_suffix(dest.suffix + '.part')
            try:
                with self._open(url, timeout=60) as resp, open(tmp, 'wb') as out:
                    shutil.copyfileobj(resp, out)
                os.replace(tmp, dest)
            except Exception as exc:   # noqa: BLE001 — network trouble is expected
                logger.warning("BRDC fetch failed (%s): %s — keeping what is loaded", url, exc)
                tmp.unlink(missing_ok=True)
            if dest.exists():
                try:
                    loaded += self.ephem.load_file(dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("BRDC parse failed (%s): %s", dest, exc)
        logger.info("BRDC: %d healthy GPS ephemerides in memory", len(self.ephem))
        return loaded

    def start(self) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                self.refresh_once()
                self._stop.wait(self.refresh_s)
        t = threading.Thread(target=loop, name='brdc-fetcher', daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
