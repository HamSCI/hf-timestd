#!/usr/bin/env python3
"""
Space-Weather Service — near-real-time solar & geomagnetic indices.

hf-timestd's ionospheric models are driven by three indices:

* **F10.7** — 10.7 cm solar radio flux (solar EUV proxy; sets daytime
  ionisation / foF2 / NmF2).
* **Ap / Kp** — planetary geomagnetic activity (storm-time hmF2 and TEC
  departures).

PHaRLAP/IRI-2020 reads these from the ``apf107.dat`` / ``ig_rz.dat`` files
refreshed weekly by ``update-iri-indices.sh``. That cadence is fine for
the smoothed climatology but lags for the *current* day — exactly when a
geomagnetic storm or a flare-driven F10.7 jump matters most. This service
fetches the latest observed values directly so the parametric ionosphere
path (and any consumer that asks) can use today's real conditions.

Sources (in priority order):

* NOAA SWPC (https://services.swpc.noaa.gov) — primary, no auth:
    - F10.7:  /products/summary/10cm-flux.json   (latest observed)
              /text/daily-solar-indices.txt        (daily observed, history)
    - Kp/Ap:  /products/noaa-planetary-k-index.json (3-hourly Kp + a_running)
* GFZ Potsdam (https://kp.gfz.de/app/json/) — Kp fallback when SWPC is down.

All fetches go through net_fetch's retry/backoff session, results are
cached to disk, and every value is range-validated before use.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    from . import net_fetch as _net_fetch
except ImportError:  # pragma: no cover - defensive
    _net_fetch = None

logger = logging.getLogger(__name__)

# --- endpoints -------------------------------------------------------------
SWPC_F107_SUMMARY = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
SWPC_DSD = "https://services.swpc.noaa.gov/text/daily-solar-indices.txt"
SWPC_KP = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
GFZ_KP = "https://kp.gfz.de/app/json/"

# --- defaults --------------------------------------------------------------
DEFAULT_CACHE_DIR = "/var/lib/timestd/iono_cache"
CACHE_FILE = "space_weather.json"
FETCH_INTERVAL_S = 1800          # refresh every 30 min
# A cached value older than this is reported stale (but still returned so
# consumers degrade gracefully rather than losing the index entirely).
F107_MAX_AGE_S = 36 * 3600       # F10.7 is observed ~3x/day
KP_MAX_AGE_S = 9 * 3600          # Kp is 3-hourly

# Physical plausibility bounds (reject corrupt/garbage values).
F107_RANGE = (60.0, 400.0)       # solar-cycle min ~64, extreme flares ~300+
KP_RANGE = (0.0, 9.0)
AP_RANGE = (0.0, 400.0)

# Climatological last-resort defaults if nothing is available at all.
F107_DEFAULT = 100.0
KP_DEFAULT = 2.0
AP_DEFAULT = 7.0


@dataclass
class SpaceWeather:
    """A snapshot of the latest solar/geomagnetic indices."""
    f107: Optional[float] = None
    f107_time: Optional[str] = None
    f107_source: Optional[str] = None
    kp: Optional[float] = None
    kp_time: Optional[str] = None
    kp_source: Optional[str] = None
    ap: Optional[float] = None
    ap_time: Optional[str] = None
    ap_source: Optional[str] = None
    fetched_at: Optional[str] = None


def _in_range(v: Optional[float], lo: float, hi: float) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


class SpaceWeatherService:
    """Thread-safe singleton fetching/caching near-real-time space weather."""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, cache_dir: str = DEFAULT_CACHE_DIR) -> "SpaceWeatherService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(cache_dir=cache_dir)
            return cls._instance

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self._lock = threading.RLock()
        self._sw = SpaceWeather()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._event = threading.Event()
        self._stats = {"fetches": 0, "failures": 0, "last_update": None}

        if _net_fetch is not None:
            self._session = _net_fetch.build_session()
        else:  # pragma: no cover
            self._session = None

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            import tempfile
            self.cache_dir = Path(tempfile.gettempdir()) / "timestd_iono_cache"
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Seed from disk so getters work immediately on restart.
        self._load_cache()

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="space-weather-service", daemon=True
        )
        self._thread.start()
        self._event.set()  # trigger an immediate first fetch
        logger.info("SpaceWeatherService started")

    def stop(self):
        self._running = False
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self):
        backoff = 60
        while self._running:
            self._event.wait(timeout=FETCH_INTERVAL_S)
            self._event.clear()
            if not self._running:
                break
            try:
                self.refresh()
                backoff = 60
            except Exception as e:  # pragma: no cover - defensive
                backoff = min(backoff * 2, FETCH_INTERVAL_S)
                logger.error("SpaceWeather refresh error (backoff %ss): %s",
                             backoff, e, exc_info=True)
                time.sleep(backoff)

    def force_update(self):
        self._event.set()

    # -- fetching -----------------------------------------------------------
    def _get_json(self, url: str) -> Optional[Any]:
        if self._session is None:
            return None
        try:
            r = self._session.get(
                url, timeout=getattr(_net_fetch, "DEFAULT_TIMEOUT", (10, 30))
            )
            if r.status_code != 200:
                logger.debug("space-weather %s HTTP %s", url, r.status_code)
                return None
            return r.json()
        except Exception as e:
            logger.debug("space-weather fetch failed %s: %s", url, e)
            return None

    def _get_text(self, url: str) -> Optional[str]:
        if self._session is None:
            return None
        try:
            r = self._session.get(
                url, timeout=getattr(_net_fetch, "DEFAULT_TIMEOUT", (10, 30))
            )
            if r.status_code != 200:
                return None
            return r.text
        except Exception as e:
            logger.debug("space-weather fetch failed %s: %s", url, e)
            return None

    def refresh(self) -> SpaceWeather:
        """Fetch all indices from the network and update the snapshot/cache."""
        new = SpaceWeather(fetched_at=datetime.now(timezone.utc).isoformat())
        ok = False

        # --- F10.7: SWPC summary, then SWPC DSD text ---
        f107 = self._fetch_f107_swpc_summary()
        if f107 is None:
            f107 = self._fetch_f107_swpc_dsd()
        if f107 is not None:
            new.f107, new.f107_time, new.f107_source = f107
            ok = True

        # --- Kp/Ap: SWPC planetary K-index, then GFZ (Kp only) ---
        kpap = self._fetch_kp_ap_swpc()
        if kpap is not None:
            new.kp, new.ap, new.kp_time, new.kp_source = kpap
            new.ap_time, new.ap_source = new.kp_time, new.kp_source
            ok = True
        else:
            gfz = self._fetch_kp_gfz()
            if gfz is not None:
                new.kp, new.kp_time, new.kp_source = gfz
                ok = True

        with self._lock:
            # Preserve any field we failed to refresh this cycle.
            prev = self._sw
            if new.f107 is None:
                new.f107, new.f107_time, new.f107_source = (
                    prev.f107, prev.f107_time, prev.f107_source)
            if new.kp is None:
                new.kp, new.kp_time, new.kp_source = (
                    prev.kp, prev.kp_time, prev.kp_source)
            if new.ap is None:
                new.ap, new.ap_time, new.ap_source = (
                    prev.ap, prev.ap_time, prev.ap_source)
            self._sw = new
            if ok:
                self._stats["fetches"] += 1
                self._stats["last_update"] = new.fetched_at
            else:
                self._stats["failures"] += 1
            self._save_cache()

        logger.info("SpaceWeather: F10.7=%s (%s) Kp=%s Ap=%s (%s)",
                    new.f107, new.f107_source, new.kp, new.ap, new.kp_source)
        return new

    def _fetch_f107_swpc_summary(self):
        d = self._get_json(SWPC_F107_SUMMARY)
        # [{"flux":128,"time_tag":"2026-06-12T20:00:00"}]
        if isinstance(d, list) and d:
            flux = _in_range(d[0].get("flux"), *F107_RANGE)
            if flux is not None:
                return flux, d[0].get("time_tag"), "swpc:summary"
        return None

    def _fetch_f107_swpc_dsd(self):
        txt = self._get_text(SWPC_DSD)
        if not txt:
            return None
        # Columns: YYYY MM DD  <10.7cm flux> <SESC ssn> ...; data rows start
        # with a 4-digit year. Take the last data row (most recent day).
        last = None
        for line in txt.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith(":"):
                continue
            parts = s.split()
            if len(parts) >= 4 and parts[0].isdigit() and len(parts[0]) == 4:
                last = parts
        if last is None:
            return None
        flux = _in_range(last[3], *F107_RANGE)
        if flux is None:
            return None
        day = f"{last[0]}-{last[1]}-{last[2]}T00:00:00"
        return flux, day, "swpc:dsd"

    def _fetch_kp_ap_swpc(self):
        d = self._get_json(SWPC_KP)
        # [{"time_tag":..,"Kp":3.0,"a_running":15,"station_count":7}, ...]
        if not (isinstance(d, list) and d):
            return None
        # newest = max time_tag
        try:
            latest = max(d, key=lambda e: e.get("time_tag", ""))
        except (TypeError, ValueError):
            return None
        kp = _in_range(latest.get("Kp"), *KP_RANGE)
        ap = _in_range(latest.get("a_running"), *AP_RANGE)
        if kp is None and ap is None:
            return None
        return kp, ap, latest.get("time_tag"), "swpc:planetary-k"

    def _fetch_kp_gfz(self):
        d = self._get_json(GFZ_KP)
        # {"Kp":[..],"datetime":["..Z",..]}
        if not isinstance(d, dict):
            return None
        kps = d.get("Kp") or []
        times = d.get("datetime") or []
        for i in range(len(kps) - 1, -1, -1):
            kp = _in_range(kps[i], *KP_RANGE)
            if kp is not None:
                t = times[i] if i < len(times) else None
                return kp, t, "gfz"
        return None

    # -- persistence --------------------------------------------------------
    def _save_cache(self):
        try:
            tmp = self.cache_dir / (CACHE_FILE + ".tmp")
            tmp.write_text(json.dumps(asdict(self._sw)))
            tmp.replace(self.cache_dir / CACHE_FILE)
        except OSError as e:
            logger.debug("space-weather cache write failed: %s", e)

    def _load_cache(self):
        path = self.cache_dir / CACHE_FILE
        try:
            if path.is_file():
                data = json.loads(path.read_text())
                self._sw = SpaceWeather(**{
                    k: data.get(k) for k in SpaceWeather().__dict__
                })
                logger.info("SpaceWeather: seeded from cache (F10.7=%s Kp=%s)",
                            self._sw.f107, self._sw.kp)
        except (OSError, ValueError, TypeError) as e:
            logger.debug("space-weather cache load failed: %s", e)

    # -- public getters -----------------------------------------------------
    @staticmethod
    def _age_s(iso: Optional[str]) -> Optional[float]:
        if not iso:
            return None
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - t).total_seconds()
        except ValueError:
            return None

    def get_f107(self, default: float = F107_DEFAULT) -> float:
        """Latest F10.7 (sfu). Returns ``default`` if none available."""
        with self._lock:
            return self._sw.f107 if self._sw.f107 is not None else default

    def get_kp(self, default: float = KP_DEFAULT) -> float:
        with self._lock:
            return self._sw.kp if self._sw.kp is not None else default

    def get_ap(self, default: float = AP_DEFAULT) -> float:
        with self._lock:
            return self._sw.ap if self._sw.ap is not None else default

    def get_snapshot(self) -> SpaceWeather:
        with self._lock:
            return SpaceWeather(**asdict(self._sw))

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            sw = self._sw
            return {
                **self._stats,
                "f107": sw.f107,
                "f107_source": sw.f107_source,
                "f107_age_s": self._age_s(sw.f107_time),
                "f107_stale": (self._age_s(sw.f107_time) or 1e9) > F107_MAX_AGE_S,
                "kp": sw.kp,
                "kp_source": sw.kp_source,
                "kp_age_s": self._age_s(sw.kp_time),
                "kp_stale": (self._age_s(sw.kp_time) or 1e9) > KP_MAX_AGE_S,
                "ap": sw.ap,
                "ap_source": sw.ap_source,
            }
