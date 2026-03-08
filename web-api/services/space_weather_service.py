"""
Space Weather Service - Ingest and provide solar/geomagnetic data.

Data sources:
- NOAA SWPC JSON API for X-ray flux, Kp index, proton flux, F10.7
- GFZ Potsdam JSON API for Kp index (fallback when NOAA unavailable)

Architecture:
- A background thread polls all endpoints every POLL_INTERVAL minutes,
  keeping cache files warm regardless of UI activity.
- Public get_*() methods read from cache only — they never block on
  network I/O and therefore never stall the FastAPI event loop.
- requests.Session with urllib3 Retry handles transient 5xx / connection
  errors automatically, with exponential backoff.
- Stale cache (> STALE_WARN_AGE) is served with a WARNING log entry.
  Cache older than STALE_MAX_AGE is treated as absent.
"""

import logging
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from pathlib import Path
import json
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class XrayFlux:
    """X-ray flux measurement from GOES satellites."""
    timestamp: str
    flux_short: float  # 0.05-0.4 nm (W/m²)
    flux_long: float   # 0.1-0.8 nm (W/m²)
    satellite: int
    
    def get_class(self) -> str:
        """Get X-ray class (A/B/C/M/X) from long wavelength flux."""
        if self.flux_long >= 1e-4:
            return f"X{self.flux_long/1e-4:.1f}"
        elif self.flux_long >= 1e-5:
            return f"M{self.flux_long/1e-5:.1f}"
        elif self.flux_long >= 1e-6:
            return f"C{self.flux_long/1e-6:.1f}"
        elif self.flux_long >= 1e-7:
            return f"B{self.flux_long/1e-7:.1f}"
        else:
            return f"A{self.flux_long/1e-8:.1f}"


@dataclass
class KpIndex:
    """Planetary K-index measurement."""
    timestamp: str
    kp: float
    kp_index: int  # 0-9 scale
    observed: str  # 'observed' or 'estimated'


@dataclass
class ProtonFlux:
    """Proton flux measurement from GOES satellites."""
    timestamp: str
    flux: float  # particles/(cm²·s·sr)
    energy: str  # e.g., ">=10 MeV"
    satellite: int


@dataclass
class SolarIndices:
    """Daily solar indices."""
    date: str
    f107: Optional[float] = None  # Solar flux at 10.7 cm (sfu)
    f107_adj: Optional[float] = None  # Adjusted F10.7
    sunspot_number: Optional[int] = None
    ap: Optional[int] = None  # Daily Ap index


def _make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """Build a requests.Session with retry/backoff on 5xx and connection errors."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class SpaceWeatherService:
    """Service for fetching and caching space weather data.

    A background daemon thread (started on first instantiation) refreshes
    all cache files on POLL_INTERVAL.  Public get_*() methods read only
    from disk cache — no network I/O on the calling thread.
    """

    NOAA_BASE_URL = "https://services.swpc.noaa.gov/json"
    GFZ_KP_URL = "https://kp.gfz.de/app/json/?starttime={start}&endtime={end}&index=Kp"
    CACHE_DIR = Path("/var/lib/timestd/space_weather_cache")

    POLL_INTERVAL = timedelta(minutes=10)  # Background refresh cadence
    STALE_WARN_AGE = timedelta(hours=2)    # Log WARNING when cache this old
    STALE_MAX_AGE = timedelta(hours=6)     # Treat cache as absent beyond this

    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize space weather service and start background poller."""
        self.cache_dir = cache_dir or self.CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = _make_session()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="SpaceWeatherPoller",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Space Weather Service initialized, cache: {self.cache_dir}, "
            f"polling every {self.POLL_INTERVAL}"
        )

    def stop(self):
        """Signal the background poller to stop (for clean shutdown)."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """Background thread: refresh all cache files periodically."""
        # Immediate first fetch so cache is warm before the first UI request
        self._refresh_all()
        while not self._stop_event.wait(timeout=self.POLL_INTERVAL.total_seconds()):
            self._refresh_all()

    def _refresh_all(self):
        """Fetch all endpoints and update cache files."""
        jobs = [
            ("xray_6hour",   self._fetch_xray, {"endpoint": "xrays-6-hour.json"}),
            ("xray_1day",    self._fetch_xray, {"endpoint": "xrays-1-day.json"}),
            ("xray_3day",    self._fetch_xray, {"endpoint": "xrays-3-day.json"}),
            ("xray_7day",    self._fetch_xray, {"endpoint": "xrays-7-day.json"}),
            ("kp_index",     self._fetch_kp,   {}),
            ("proton_flux",  self._fetch_proton, {}),
            ("solar_indices", self._fetch_solar_indices, {}),
        ]
        for cache_key, fetch_fn, kwargs in jobs:
            try:
                data = fetch_fn(**kwargs)
                if data is not None:
                    self._write_cache(cache_key, data)
                    logger.debug(f"Poller: refreshed {cache_key} ({len(data) if isinstance(data, list) else 'dict'} items)")
            except Exception as e:
                logger.warning(f"Poller: failed to refresh {cache_key}: {e}")

    # ------------------------------------------------------------------
    # Low-level fetch helpers (network I/O only, no caching logic)
    # ------------------------------------------------------------------

    def _fetch_xray(self, endpoint: str) -> Optional[list]:
        url = f"{self.NOAA_BASE_URL}/goes/primary/{endpoint}"
        resp = self._session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"GOES X-ray fetch failed: HTTP {resp.status_code} ({url})")
        return None

    def _fetch_kp(self) -> Optional[list]:
        """Fetch Kp from NOAA; fall back to GFZ Potsdam on failure."""
        url = f"{self.NOAA_BASE_URL}/planetary_k_index_1m.json"
        resp = self._session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"NOAA Kp fetch failed (HTTP {resp.status_code}), trying GFZ fallback")
        return self._fetch_kp_gfz()

    def _fetch_kp_gfz(self) -> Optional[list]:
        """Fetch Kp from GFZ Potsdam and normalise to NOAA-compatible schema."""
        now = datetime.utcnow()
        start = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = self.GFZ_KP_URL.format(start=start, end=end)
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"GFZ Kp fetch failed: HTTP {resp.status_code}")
                return None
            raw = resp.json()
            # GFZ schema: {"datetime": [...], "Kp": [...], "status": [...]}
            datetimes = raw.get("datetime", [])
            kp_vals = raw.get("Kp", [])
            statuses = raw.get("status", [])
            result = []
            for i, ts_str in enumerate(datetimes):
                kp_val = kp_vals[i] if i < len(kp_vals) else 0.0
                status = statuses[i] if i < len(statuses) else 1
                # Normalise to NOAA-like dict so _parse_kp_data works unchanged
                result.append({
                    "time_tag": ts_str.replace(" ", "T").rstrip("Z") ,
                    "estimated_kp": float(kp_val),
                    "kp_index": int(round(float(kp_val))),
                    "kp": "Z" if status == 1 else "M",  # Z=observed, M=model
                    "_source": "gfz",
                })
            logger.info(f"GFZ Kp fallback: retrieved {len(result)} entries")
            return result
        except Exception as e:
            logger.error(f"GFZ Kp fallback error: {e}")
            return None

    def _fetch_proton(self) -> Optional[list]:
        url = f"{self.NOAA_BASE_URL}/goes/primary/integral-protons-plot-6-hour.json"
        resp = self._session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Proton flux fetch failed: HTTP {resp.status_code}")
        return None

    def _fetch_solar_indices(self) -> Optional[list]:
        """Fetch daily solar indices: F10.7 from 10cm-flux, Ap from noaa-planetary-k-index."""
        # Current F10.7 from 10cm-flux summary
        f107 = None
        f107_url = "https://services.swpc.noaa.gov/products/summary/10cm-flux.json"
        try:
            resp = self._session.get(f107_url, timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                f107 = float(raw.get("Flux", 0)) or None
        except Exception as e:
            logger.warning(f"F10.7 fetch error: {e}")

        # 3-hourly Kp/Ap from noaa-planetary-k-index (covers ~7 days)
        kp_url = f"{self.NOAA_BASE_URL.replace('/json', '')}/products/noaa-planetary-k-index.json"
        try:
            resp = self._session.get(kp_url, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"noaa-planetary-k-index fetch failed: HTTP {resp.status_code}")
                kp_rows = []
            else:
                kp_rows = resp.json()
        except Exception as e:
            logger.warning(f"noaa-planetary-k-index fetch error: {e}")
            kp_rows = []

        # Aggregate 3-hourly Ap values into daily records
        daily: dict = {}
        for row in kp_rows:
            try:
                ts_str = row[0] if isinstance(row, list) else row.get("time_tag", "")
                date_str = ts_str[:10]  # YYYY-MM-DD
                ap_val = int(row[2]) if isinstance(row, list) else int(row.get("a_running", 0))
                if date_str not in daily:
                    daily[date_str] = {"ap_sum": 0, "ap_count": 0}
                daily[date_str]["ap_sum"] += ap_val
                daily[date_str]["ap_count"] += 1
            except (ValueError, IndexError, TypeError):
                continue

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today not in daily:
            daily[today] = {"ap_sum": 0, "ap_count": 0}

        result = []
        for date_str, vals in sorted(daily.items()):
            ap_mean = round(vals["ap_sum"] / vals["ap_count"]) if vals["ap_count"] else None
            result.append({
                "time_tag": date_str,
                "f10_7": f107 if date_str == today else None,
                "f10_7_index": None,
                "sunspot_number": None,
                "ap": ap_mean,
            })
        return result if result else None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _write_cache(self, cache_key: str, data: Any):
        cache_file = self.cache_dir / f"{cache_key}.json"
        tmp = cache_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(cache_file)  # atomic rename

    def _read_cache(self, cache_key: str) -> Optional[Any]:
        """Read cache file, respecting stale-age policy."""
        cache_file = self.cache_dir / f"{cache_key}.json"
        if not cache_file.exists():
            return None
        age = datetime.utcnow() - datetime.fromtimestamp(cache_file.stat().st_mtime)
        if age > self.STALE_MAX_AGE:
            logger.warning(
                f"Cache too stale ({age}) for {cache_key} — treating as absent. "
                f"Check network connectivity."
            )
            return None
        if age > self.STALE_WARN_AGE:
            logger.warning(f"Serving stale cache ({age}) for {cache_key}")
        try:
            with open(cache_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Cache read error for {cache_key}: {e}")
            return None

    # ------------------------------------------------------------------
    # Public API — reads cache only, never blocks on network
    # ------------------------------------------------------------------

    def get_xray_flux(self, hours: int = 24) -> List[XrayFlux]:
        """
        Get X-ray flux data from GOES satellites.
        
        Args:
            hours: Number of hours of history (max 7 days from NOAA)
        
        Returns:
            List of XrayFlux measurements (one per timestamp, both bands merged)
        """
        # Select the smallest NOAA endpoint that covers the requested window
        if hours <= 6:
            cache_key = "xray_6hour"
        elif hours <= 24:
            cache_key = "xray_1day"
        elif hours <= 72:
            cache_key = "xray_3day"
        else:
            cache_key = "xray_7day"

        data = self._read_cache(cache_key)
        if not data:
            return []
        
        # Parse and filter by time range
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        # NOAA returns two rows per timestamp (short 0.05-0.4nm and long
        # 0.1-0.8nm bands).  Merge them into one XrayFlux per timestamp.
        merged: Dict[str, Dict] = {}  # time_tag -> {flux_short, flux_long, satellite}

        for item in data:
            try:
                ts_str = item['time_tag']
                ts = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%SZ')
                if ts < cutoff:
                    continue
                
                energy = item.get('energy', '')
                flux_val = float(item.get('flux', 0))
                satellite = int(item.get('satellite', 0))

                if ts_str not in merged:
                    merged[ts_str] = {'flux_short': 0.0, 'flux_long': 0.0, 'satellite': satellite}

                if '0.05-0.4' in energy:
                    merged[ts_str]['flux_short'] = flux_val
                elif '0.1-0.8' in energy:
                    merged[ts_str]['flux_long'] = flux_val
                else:
                    # Unknown band — treat as long
                    merged[ts_str]['flux_long'] = flux_val
            except (ValueError, KeyError) as e:
                logger.debug(f"Skipping invalid X-ray entry: {e}")
                continue

        results = [
            XrayFlux(
                timestamp=ts_str,
                flux_short=vals['flux_short'],
                flux_long=vals['flux_long'],
                satellite=vals['satellite'],
            )
            for ts_str, vals in sorted(merged.items())
            if vals['flux_long'] > 0  # Drop entries with no long-band data
        ]
        
        logger.debug(f"Retrieved {len(results)} X-ray flux measurements ({cache_key})")
        return results
    
    def get_kp_index(self, hours: int = 24) -> List[KpIndex]:
        """
        Get planetary Kp index data.
        
        Args:
            hours: Number of hours of history
        
        Returns:
            List of KpIndex measurements
        """
        data = self._read_cache("kp_index")
        if not data:
            return []
        
        # Parse and filter by time range
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        results = []
        
        for item in data:
            try:
                ts_str = item['time_tag']
                # NOAA uses ISO format (2026-03-06T18:45:00) or space-separated
                try:
                    ts = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    try:
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
                    except ValueError:
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                if ts < cutoff:
                    continue
                
                # kp field is a string like '3M' or '0Z'; use estimated_kp for numeric value
                kp_val = float(item.get('estimated_kp', 0))
                kp_obj = KpIndex(
                    timestamp=ts.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    kp=kp_val,
                    kp_index=int(item.get('kp_index', 0)),
                    observed='observed' if str(item.get('kp', '')).endswith('Z') else 'estimated'
                )
                results.append(kp_obj)
            except (ValueError, KeyError) as e:
                logger.debug(f"Skipping invalid Kp entry: {e}")
                continue
        
        logger.info(f"Retrieved {len(results)} Kp index measurements")
        return results
    
    def get_proton_flux(self, hours: int = 24) -> List[ProtonFlux]:
        """
        Get proton flux data from GOES satellites.
        
        Args:
            hours: Number of hours of history
        
        Returns:
            List of ProtonFlux measurements
        """
        data = self._read_cache("proton_flux")
        if not data:
            return []
        
        # Parse and filter by time range
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        results = []
        
        for item in data:
            try:
                ts = datetime.strptime(item['time_tag'], '%Y-%m-%dT%H:%M:%SZ')
                if ts < cutoff:
                    continue
                
                # NOAA provides multiple energy channels
                proton = ProtonFlux(
                    timestamp=item['time_tag'],
                    flux=float(item.get('flux', 0)),
                    energy=item.get('energy', '>=10 MeV'),
                    satellite=int(item.get('satellite', 0))
                )
                results.append(proton)
            except (ValueError, KeyError) as e:
                logger.debug(f"Skipping invalid proton entry: {e}")
                continue
        
        logger.info(f"Retrieved {len(results)} proton flux measurements")
        return results
    
    def get_current_conditions(self) -> Dict[str, Any]:
        """
        Get current space weather conditions summary.
        
        Returns:
            Dictionary with current X-ray class, Kp, proton flux, etc.
        """
        # Get latest data
        xray_data = self.get_xray_flux(hours=1)
        kp_data = self.get_kp_index(hours=3)
        proton_data = self.get_proton_flux(hours=1)
        
        result = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'xray': None,
            'kp': None,
            'proton_flux': None,
            'alerts': []
        }
        
        # Latest X-ray
        if xray_data:
            latest_xray = xray_data[-1]
            xray_class = latest_xray.get_class()
            result['xray'] = {
                'timestamp': latest_xray.timestamp,
                'flux': latest_xray.flux_long,
                'class': xray_class,
                'satellite': latest_xray.satellite
            }
            
            # Alert for M/X class flares
            if latest_xray.flux_long >= 1e-5:
                result['alerts'].append({
                    'type': 'XRAY_FLARE',
                    'severity': 'HIGH' if latest_xray.flux_long >= 1e-4 else 'MEDIUM',
                    'message': f"{xray_class} X-ray flare detected"
                })
        
        # Latest Kp
        if kp_data:
            latest_kp = kp_data[-1]
            result['kp'] = {
                'timestamp': latest_kp.timestamp,
                'kp': latest_kp.kp,
                'kp_index': latest_kp.kp_index,
                'observed': latest_kp.observed
            }
            
            # Alert for geomagnetic storms
            if latest_kp.kp_index >= 5:
                severity = 'HIGH' if latest_kp.kp_index >= 7 else 'MEDIUM'
                result['alerts'].append({
                    'type': 'GEOMAGNETIC_STORM',
                    'severity': severity,
                    'message': f"Geomagnetic storm (Kp={latest_kp.kp_index})"
                })
        
        # Latest proton flux (>10 MeV)
        if proton_data:
            # Find >=10 MeV channel
            proton_10mev = [p for p in proton_data if '>=10' in p.energy]
            if proton_10mev:
                latest_proton = proton_10mev[-1]
                result['proton_flux'] = {
                    'timestamp': latest_proton.timestamp,
                    'flux': latest_proton.flux,
                    'energy': latest_proton.energy,
                    'satellite': latest_proton.satellite
                }
                
                # Alert for polar cap absorption
                if latest_proton.flux >= 10:
                    result['alerts'].append({
                        'type': 'POLAR_CAP_ABSORPTION',
                        'severity': 'HIGH' if latest_proton.flux >= 100 else 'MEDIUM',
                        'message': f"Elevated proton flux ({latest_proton.flux:.1f} pfu)"
                    })
        
        return result
    
    def get_solar_indices(self, days: int = 30) -> List[SolarIndices]:
        """
        Get daily solar indices (F10.7, Ap, sunspot number) from NOAA SWPC.
        
        Args:
            days: Number of days of history
        
        Returns:
            List of SolarIndices sorted by date ascending
        """
        data = self._read_cache("solar_indices")
        if not data:
            return []

        cutoff_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        results = []
        for item in data:
            try:
                date_str = item.get("time_tag", item.get("date", ""))[:10]  # YYYY-MM-DD
                if date_str < cutoff_date:
                    continue
                results.append(SolarIndices(
                    date=date_str,
                    f107=float(item["f10_7"]) if item.get("f10_7") not in (None, "") else None,
                    f107_adj=float(item["f10_7_index"]) if item.get("f10_7_index") not in (None, "") else None,
                    sunspot_number=int(float(item["sunspot_number"])) if item.get("sunspot_number") not in (None, "") else None,
                    ap=int(float(item["ap"])) if item.get("ap") not in (None, "") else None,
                ))
            except (ValueError, KeyError, TypeError) as e:
                logger.debug(f"Skipping invalid solar indices entry: {e}")
                continue

        results.sort(key=lambda x: x.date)
        logger.debug(f"Retrieved {len(results)} solar indices records")
        return results
    
    def detect_sid_events(self, hours: int = 6) -> List[Dict[str, Any]]:
        """
        Detect Sudden Ionospheric Disturbance (SID) events from X-ray data.
        
        Args:
            hours: Hours of history to analyze
        
        Returns:
            List of detected SID events
        """
        xray_data = self.get_xray_flux(hours=hours)
        if len(xray_data) < 10:
            return []
        
        events = []
        threshold = 1e-6  # C-class threshold
        
        # Simple peak detection
        for i in range(5, len(xray_data) - 5):
            current = xray_data[i]
            
            # Check if this is a peak above threshold
            if current.flux_long < threshold:
                continue
            
            # Check if it's higher than neighbors
            prev_avg = sum(x.flux_long for x in xray_data[i-5:i]) / 5
            next_avg = sum(x.flux_long for x in xray_data[i+1:i+6]) / 5
            
            if current.flux_long > prev_avg * 1.5 and current.flux_long > next_avg:
                events.append({
                    'timestamp': current.timestamp,
                    'peak_flux': current.flux_long,
                    'xray_class': current.get_class(),
                    'type': 'SID',
                    'confidence': 0.8 if current.flux_long >= 1e-5 else 0.6
                })
        
        logger.info(f"Detected {len(events)} potential SID events")
        return events
