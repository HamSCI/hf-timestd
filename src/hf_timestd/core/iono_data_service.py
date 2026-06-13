#!/usr/bin/env python3
"""
Ionospheric Data Service - Real-Time WAM-IPE and GIRO Data Ingestion

================================================================================
PURPOSE
================================================================================

This service provides real-time ionospheric data for the propagation model by:

1. Fetching GIRO ionosonde data for real-time hmF2/foF2 measurements
   - Source: Lowell GIRO Data Center DIDBase (DIDBGetValues web service)
   - Provides ground-truth ionosonde measurements near the path midpoint

2. Falling back to the IRI-2020 climatological model (via IonosphericModel,
   driven by the weekly-refreshed F10.7/Ap/sunspot indices) where no nearby
   ionosonde measurement is available, and to a crude internal parametric
   model only if IRI itself is unavailable.

3. Caching data locally to minimize network requests
   - Cache directory: /var/lib/timestd/iono_cache/
   - GIRO measurements cached for 15 minutes; station list cached on disk

4. Providing interpolated ionospheric parameters at arbitrary lat/lon/time:
   - hmF2 (F2 layer peak height)
   - NmF2 (F2 layer peak electron density)
   - TEC (Total Electron Content)
   - foF2 (F2 layer critical frequency)

================================================================================
DATA SOURCES
================================================================================

GIRO (primary real-time):
    - Station list: https://lgdc.uml.edu/common/DIDBFastStationList (HTML;
      parsed for URSI code + lat/lon, with a bundled fallback table at
      hf_timestd/data/giro_stations.tsv when the page is down).
    - Measurements: https://lgdc.uml.edu/common/DIDBGetValues
      (foF2,hmF2 with CS autoscaling confidence; dates as YYYY/MM/DD HH:MM:SS).
    - The DIDBase server is intermittently overloaded (HTTP 503/404 flapping),
      so all fetches go through net_fetch's retry/backoff session.

IRI-2020 (fallback base):
    - Delegated to IonosphericModel; GIRO measurements are blended on top.

WAM-IPE: DISABLED (enable_wamipe=False by default).
    The operational Whole-atmosphere Forecast System feeds — both the public
    S3 PDS (s3://noaa-nws-wam-ipe-pds/, prefix v1.2/wfs.*) and NOMADS
    (nomads.ncep.noaa.gov/.../wfs/prod) — publish only the WAM *neutral*
    atmosphere (variable `den`, kg m^-3, on a fixed-height grid). They carry
    NO IPE ionosphere product: no electron density, NmF2, hmF2 or TEC. Neutral
    density is not usable for HF ray-tracing, so this branch cannot supply the
    ionospheric parameters it was written for. The fetch/parse code is retained
    (guarded by enable_wamipe) for the day an actual IPE 2D product is
    published, but it is inert by default and will never fabricate a grid from
    defaults (see _parse_wamipe_netcdf). Audited 2026-06-13.

================================================================================
ARCHITECTURE
================================================================================

    IonoDataService (singleton, thread-safe)
        ├── _fetch_giro()        → fetch latest GIRO ionosonde measurements
        ├── _fetch_wamipe()      → (disabled) WAM-IPE NetCDF, see note above
        ├── _base_point()        → IRI-2020 base (else parametric climatology)
        ├── get_iono_params()    → GIRO-corrected params at (lat, lon, time)
        ├── get_electron_density_profile() → Ne(h) for ray-tracing
        └── start() / stop()     → background update thread
"""

import logging
import math
import os
import re
import time
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple, List, Callable, NamedTuple
from dataclasses import dataclass, field

import numpy as np

from .tec_geometry import great_circle_distance

try:
    import requests as _requests
except ImportError:  # pragma: no cover - optional dependency
    _requests = None

try:
    from . import net_fetch as _net_fetch
except ImportError:  # pragma: no cover - defensive
    _net_fetch = None

logger = logging.getLogger(__name__)

if _requests is None:
    logger.warning("requests library not installed — IonoDataService network fetching disabled")

# =============================================================================
# CONSTANTS
# =============================================================================

# WAM-IPE S3 bucket (public, no credentials needed)
WAMIPE_S3_BUCKET = "noaa-nws-wam-ipe-pds"
WAMIPE_S3_BASE_URL = f"https://{WAMIPE_S3_BUCKET}.s3.amazonaws.com"

# NOMADS fallback for WAM-IPE
WAMIPE_NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/wfs/prod"

# GIRO DIDBase (Lowell GIRO Data Center)
#   - station list: an HTML page listing URSI code + name + lat/lon
#   - DIDBGetValues: the machine-readable scaled-characteristics web service
#     (dates formatted YYYY/MM/DD HH:MM:SS; '#'-commented header + columns)
GIRO_STATION_LIST_URL = "https://lgdc.uml.edu/common/DIDBFastStationList"
GIRO_GETVALUES_URL = "https://lgdc.uml.edu/common/DIDBGetValues"

# Bundled station fallback table (URSI<TAB>name<TAB>lat<TAB>lon), shipped as
# package data and used when the live station-list page is unreachable.
BUNDLED_STATIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "giro_stations.tsv"

# How many nearest ionosondes to poll each cycle. Polling the whole network
# (~130 stations) every 5 min hammers a frequently-overloaded server and most
# stations are too far to contribute; the nearest handful covers the operator's
# region (full GIRO weight is within ~555 km, zero beyond ~3330 km).
GIRO_MAX_STATIONS_POLLED = 12

# GIRO correction weighting by great-circle distance from the query point
# (P-M16). Full weight within ~5° (555 km), zero beyond ~30° (3330 km) —
# the km equivalents of the original degree thresholds, now dateline-safe.
GIRO_FULL_WEIGHT_KM = 555.0
GIRO_ZERO_WEIGHT_KM = 3330.0

# Cache settings
DEFAULT_CACHE_DIR = "/var/lib/timestd/iono_cache"
WAMIPE_CACHE_MAX_AGE_S = 3600    # 1 hour
GIRO_CACHE_MAX_AGE_S = 900       # 15 minutes
FETCH_INTERVAL_S = 300            # Check for new data every 5 minutes

# WAM-IPE grid parameters (from documentation)
# ipe05 files: 2D ionosphere outputs at 5-min cadence
# Grid: geographic lat/lon
WAMIPE_NLAT = 181   # -90 to 90, 1-degree
WAMIPE_NLON = 361   # -180 to 180, 1-degree


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class IonoGridPoint:
    """Ionospheric parameters at a single grid point."""
    latitude: float
    longitude: float
    timestamp: datetime
    hmF2_km: float = 300.0       # F2 layer peak height
    NmF2_m3: float = 1e12        # F2 peak electron density (m^-3)
    foF2_MHz: float = 8.0        # F2 critical frequency
    TEC_TECU: float = 20.0       # Total Electron Content
    hmE_km: float = 110.0        # E layer peak height
    source: str = "default"      # Data source identifier


@dataclass
class IonoGrid:
    """2D ionospheric grid from WAM-IPE or similar."""
    timestamp: datetime
    source: str                   # "wamipe", "giro", "iri", "fallback"
    
    # Grid coordinates
    lats: np.ndarray = field(default_factory=lambda: np.array([]))   # 1D lat array
    lons: np.ndarray = field(default_factory=lambda: np.array([]))   # 1D lon array
    
    # 2D fields (nlat x nlon)
    hmF2: np.ndarray = field(default_factory=lambda: np.array([]))   # km
    NmF2: np.ndarray = field(default_factory=lambda: np.array([]))   # m^-3
    TEC: np.ndarray = field(default_factory=lambda: np.array([]))    # TECU
    
    # Optional 3D electron density profile (nlat x nlon x nalt)
    altitudes: Optional[np.ndarray] = None   # km
    Ne_3d: Optional[np.ndarray] = None       # m^-3
    
    # Metadata
    forecast_hour: int = 0
    model_cycle: str = ""
    
    def is_valid(self) -> bool:
        """Check if grid has valid data."""
        return (self.lats.size > 0 and self.lons.size > 0 and 
                self.hmF2.size > 0)
    
    def interpolate(self, lat: float, lon: float) -> IonoGridPoint:
        """Bilinear interpolation at arbitrary lat/lon."""
        if not self.is_valid():
            return IonoGridPoint(
                latitude=lat, longitude=lon, timestamp=self.timestamp,
                source="fallback"
            )
        
        # Normalize longitude to grid range
        lon_norm = lon
        if self.lons[0] >= 0 and lon < 0:
            lon_norm = lon + 360.0
        elif self.lons[0] < 0 and lon > 180:
            lon_norm = lon - 360.0
        
        # Find bounding indices
        lat_idx = np.searchsorted(self.lats, lat) - 1
        lon_idx = np.searchsorted(self.lons, lon_norm) - 1
        
        # Clamp to valid range
        lat_idx = max(0, min(lat_idx, len(self.lats) - 2))
        lon_idx = max(0, min(lon_idx, len(self.lons) - 2))
        
        # Bilinear weights
        lat0, lat1 = self.lats[lat_idx], self.lats[lat_idx + 1]
        lon0, lon1 = self.lons[lon_idx], self.lons[lon_idx + 1]
        
        dlat = lat1 - lat0
        dlon = lon1 - lon0
        
        if dlat == 0:
            wlat = 0.0
        else:
            wlat = (lat - lat0) / dlat
        
        if dlon == 0:
            wlon = 0.0
        else:
            wlon = (lon_norm - lon0) / dlon
        
        wlat = max(0.0, min(1.0, wlat))
        wlon = max(0.0, min(1.0, wlon))
        
        def _interp2d(grid):
            if grid.size == 0:
                return 0.0
            v00 = grid[lat_idx, lon_idx]
            v01 = grid[lat_idx, lon_idx + 1]
            v10 = grid[lat_idx + 1, lon_idx]
            v11 = grid[lat_idx + 1, lon_idx + 1]
            return (v00 * (1 - wlat) * (1 - wlon) +
                    v01 * (1 - wlat) * wlon +
                    v10 * wlat * (1 - wlon) +
                    v11 * wlat * wlon)
        
        hmF2 = _interp2d(self.hmF2) if self.hmF2.size > 0 else 300.0
        NmF2 = _interp2d(self.NmF2) if self.NmF2.size > 0 else 1e12
        TEC = _interp2d(self.TEC) if self.TEC.size > 0 else 20.0
        
        # foF2 from NmF2: foF2 = sqrt(NmF2 / 1.24e10) in MHz
        foF2 = np.sqrt(max(0, NmF2) / 1.24e10)
        
        return IonoGridPoint(
            latitude=lat,
            longitude=lon,
            timestamp=self.timestamp,
            hmF2_km=float(hmF2),
            NmF2_m3=float(NmF2),
            foF2_MHz=float(foF2),
            TEC_TECU=float(TEC),
            source=self.source
        )
    
    def get_electron_density_profile(
        self, lat: float, lon: float
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """
        Get electron density profile Ne(h) at a location.
        
        Returns:
            Tuple of (altitudes_km, Ne_m3) arrays, or None if no 3D data
        """
        if self.altitudes is None or self.Ne_3d is None:
            return None
        
        # Find nearest grid point for 3D data (no interpolation for speed)
        lat_idx = np.argmin(np.abs(self.lats - lat))
        
        lon_norm = lon
        if self.lons[0] >= 0 and lon < 0:
            lon_norm = lon + 360.0
        lon_idx = np.argmin(np.abs(self.lons - lon_norm))
        
        return self.altitudes, self.Ne_3d[lat_idx, lon_idx, :]


@dataclass
class GiroStation:
    """GIRO ionosonde station metadata."""
    code: str
    name: str
    latitude: float
    longitude: float
    distance_km: float = 0.0  # Distance from query point


@dataclass
class GiroMeasurement:
    """Real-time ionosonde measurement from GIRO."""
    station_code: str
    timestamp: datetime
    foF2_MHz: float
    hmF2_km: float
    foE_MHz: Optional[float] = None
    hmE_km: Optional[float] = None
    confidence: float = 0.0  # 0-1, based on autoscaling confidence


# =============================================================================
# IONOSPHERIC DATA SERVICE
# =============================================================================

class IonoDataService:
    """
    Background service for fetching and caching ionospheric data.
    
    Thread-safe singleton that provides interpolated ionospheric parameters
    from WAM-IPE model output and GIRO ionosonde measurements.
    
    Usage:
        service = IonoDataService.get_instance()
        service.start()
        
        # Get ionospheric parameters at a point
        point = service.get_iono_params(lat=39.0, lon=-92.0, utc_time=datetime.now(tz=utc))
        print(f"hmF2 = {point.hmF2_km:.1f} km, TEC = {point.TEC_TECU:.1f} TECU")
        
        service.stop()
    """
    
    _instance = None
    _lock = threading.Lock()
    
    @classmethod
    def get_instance(
        cls,
        cache_dir: str = DEFAULT_CACHE_DIR,
        enable_wamipe: bool = False,
        enable_giro: bool = True,
        home_lat: Optional[float] = None,
        home_lon: Optional[float] = None,
    ) -> 'IonoDataService':
        """Get or create the singleton instance.

        Warning: The first caller's parameters win. Subsequent calls with
        different parameters will log a warning but return the existing instance.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    cache_dir=cache_dir,
                    enable_wamipe=enable_wamipe,
                    enable_giro=enable_giro,
                    home_lat=home_lat,
                    home_lon=home_lon,
                )
            else:
                # Warn if parameters differ from existing instance
                inst = cls._instance
                if (str(inst.cache_dir) != str(Path(cache_dir)) or
                        inst.enable_wamipe != enable_wamipe or
                        inst.enable_giro != enable_giro):
                    logger.warning(
                        f"IonoDataService.get_instance() called with different params "
                        f"(wamipe={enable_wamipe}, giro={enable_giro}) than existing "
                        f"instance (wamipe={inst.enable_wamipe}, giro={inst.enable_giro}). "
                        f"Returning existing instance."
                    )
            return cls._instance
    
    def __init__(
        self,
        cache_dir: str = DEFAULT_CACHE_DIR,
        enable_wamipe: bool = False,
        enable_giro: bool = True,
        home_lat: Optional[float] = None,
        home_lon: Optional[float] = None,
        enable_iri_fallback: bool = True,
    ):
        self.cache_dir = Path(cache_dir)
        self.enable_wamipe = enable_wamipe
        self.enable_giro = enable_giro
        # Operator location: used to poll only the nearest ionosondes.
        self.home_lat = home_lat
        self.home_lon = home_lon
        # Use IRI-2020 (via IonosphericModel) as the no-grid base, with GIRO
        # blended on top. Falls back to the crude internal climatology when
        # IRI/PHaRLAP is unavailable.
        self.enable_iri_fallback = enable_iri_fallback
        self._iri_model = None          # lazily constructed IonosphericModel
        self._iri_unavailable = False   # latch: stop retrying a missing IRI

        # Shared retry/backoff HTTP session for GIRO (the DIDBase server
        # flaps between 200/503/404); falls back to the bare requests module
        # if net_fetch could not be imported.
        if _net_fetch is not None:
            self._session = _net_fetch.build_session()
        else:  # pragma: no cover - defensive
            self._session = _requests.Session() if _requests is not None else None

        # Current grids (thread-safe via lock)
        self._grid_lock = threading.RLock()
        self._current_grid: Optional[IonoGrid] = None
        self._previous_grid: Optional[IonoGrid] = None  # For temporal interpolation

        # GIRO measurements cache
        self._giro_lock = threading.RLock()
        self._giro_stations: List[GiroStation] = []
        self._giro_measurements: Dict[str, GiroMeasurement] = {}  # station_code -> latest
        
        # Background thread
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._fetch_event = threading.Event()
        
        # Statistics
        self._stats = {
            'wamipe_fetches': 0,
            'wamipe_failures': 0,
            'giro_fetches': 0,
            'giro_failures': 0,
            'last_wamipe_update': None,
            'last_giro_update': None,
            'grid_source': 'none',
        }
        
        # Ensure cache directory exists
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            import tempfile
            fallback = Path(tempfile.gettempdir()) / "timestd_iono_cache"
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(
                f"Cannot create cache dir {self.cache_dir} (permission denied), "
                f"falling back to {fallback}"
            )
            self.cache_dir = fallback
        
        logger.info(f"IonoDataService initialized (cache={self.cache_dir}, "
                    f"wamipe={enable_wamipe}, giro={enable_giro})")
    
    def start(self):
        """Start the background data fetching thread."""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._background_loop,
            name="iono-data-service",
            daemon=True
        )
        self._thread.start()
        logger.info("IonoDataService background thread started")
        
        # Trigger immediate first fetch
        self._fetch_event.set()
    
    def stop(self):
        """Stop the background data fetching thread."""
        self._running = False
        self._fetch_event.set()  # Wake up thread so it can exit
        if self._thread is not None:
            self._thread.join(timeout=10)
        logger.info("IonoDataService stopped")
    
    def _background_loop(self):
        """Background loop that periodically fetches new data."""
        while self._running:
            try:
                self._fetch_event.wait(timeout=FETCH_INTERVAL_S)
                self._fetch_event.clear()
                
                if not self._running:
                    break
                
                if self.enable_wamipe:
                    self._fetch_wamipe()

                if self.enable_giro:
                    self._fetch_giro()

                # Reset exponential backoff counter after a successful iteration
                self._bg_error_backoff = 60
                    
            except Exception as e:
                _backoff = min(
                    getattr(self, '_bg_error_backoff', 60) * 2,
                    FETCH_INTERVAL_S
                )
                self._bg_error_backoff = _backoff
                logger.error(
                    f"IonoDataService background error (backing off {_backoff:.0f}s): {e}",
                    exc_info=True
                )
                time.sleep(_backoff)
    
    # =========================================================================
    # WAM-IPE DATA FETCHING
    # =========================================================================
    
    def _fetch_wamipe(self):
        """
        Fetch latest WAM-IPE 2D ionosphere products from NOAA.
        
        Strategy:
        1. Try NOMADS first (most current, direct HTTP)
        2. Fall back to S3 bucket
        3. Fall back to cached data
        """
        if _requests is None:
            return
        requests = _requests
        
        now = datetime.now(timezone.utc)
        
        # Determine the latest available model cycle
        # WAM-IPE runs at 00, 06, 12, 18 UTC
        cycle_hour = (now.hour // 6) * 6
        cycle_date = now.strftime("%Y%m%d")
        cycle_str = f"{cycle_hour:02d}"
        
        # Try to find the most recent ipe05 file
        # Filename pattern: wfs.YYYYMMDD/CC/ipe05_YYYYMMDD_CCFF00.nc
        # where CC=cycle, FF=forecast hour
        
        # First try: current cycle, most recent forecast hour
        forecast_minutes = (now.hour - cycle_hour) * 60 + now.minute
        # Round down to nearest 5 minutes
        forecast_5min = (forecast_minutes // 5) * 5
        forecast_hour = forecast_5min // 60
        forecast_min = forecast_5min % 60
        
        urls_to_try = []
        
        # NOMADS URL
        nomads_dir = f"{WAMIPE_NOMADS_BASE}/wfs.{cycle_date}/{cycle_str}"
        # The filename format varies; try common patterns
        for fh in range(forecast_hour, -1, -1):
            for fm in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]:
                fname = f"ipe05_{cycle_date}_{cycle_str}{fh:02d}{fm:02d}00.nc"
                urls_to_try.append(f"{nomads_dir}/{fname}")
                if len(urls_to_try) >= 6:
                    break
            if len(urls_to_try) >= 6:
                break
        
        # S3 fallback — list the prefix and append the latest ipe05 .nc.
        # The bucket is virtual-hosted, so a bucket *listing* needs the
        # ?list-type=2&prefix= query; the original code appended a path
        # ending in '/', which S3 serves as a (missing) object whose XML
        # body was then fed to the NetCDF parser (P-H19).
        s3_nc_url = self._resolve_s3_latest_nc(f"v1.2/wfs.{cycle_date}")
        if s3_nc_url is not None:
            urls_to_try.append(s3_nc_url)
        
        grid = None
        for url in urls_to_try:
            try:
                grid = self._download_and_parse_wamipe(url)
                if grid is not None and grid.is_valid():
                    break
            except Exception as e:
                logger.debug(f"WAM-IPE fetch failed for {url}: {e}")
                continue
        
        if grid is not None and grid.is_valid():
            with self._grid_lock:
                self._previous_grid = self._current_grid
                self._current_grid = grid
            
            self._stats['wamipe_fetches'] += 1
            self._stats['last_wamipe_update'] = now.isoformat()
            self._stats['grid_source'] = 'wamipe'
            logger.info(f"WAM-IPE grid updated: {grid.timestamp.isoformat()}, "
                       f"cycle={grid.model_cycle}")
        else:
            self._stats['wamipe_failures'] += 1
            # Try loading from cache
            cached = self._load_cached_grid()
            if cached is not None:
                with self._grid_lock:
                    if self._current_grid is None:
                        self._current_grid = cached
                        self._stats['grid_source'] = 'cached'
                logger.info("Using cached WAM-IPE grid")
            else:
                logger.warning("No WAM-IPE data available (network and cache both failed)")
    
    def _resolve_s3_latest_nc(self, s3_prefix: str) -> Optional[str]:
        """Resolve the WAM-IPE S3 fallback prefix to the URL of the most
        recent ipe05 NetCDF object.

        The S3 bucket is virtual-hosted; a bucket *listing* requires the
        ``?list-type=2&prefix=`` query and returns a ``ListBucketResult``
        XML document.  Requesting ``{prefix}/`` directly (the original
        bug, P-H19) makes S3 serve a missing-object error whose XML body
        was then handed to the NetCDF parser.  Returns ``None`` when the
        listing is unavailable or contains no ``.nc`` object.
        """
        if _requests is None:
            return None
        requests = _requests
        list_url = (f"{WAMIPE_S3_BASE_URL}/"
                    f"?list-type=2&prefix={s3_prefix}/")
        try:
            resp = requests.get(list_url, timeout=15)
            if resp.status_code != 200:
                return None
            root = ET.fromstring(resp.content)
        except (requests.RequestException, ET.ParseError) as e:
            logger.debug(f"WAM-IPE S3 prefix listing failed "
                         f"({s3_prefix}): {e}")
            return None
        # <Contents><Key>…</Key>; the ListBucketResult document is
        # namespaced, so match the element by local name.
        nc_keys = sorted(
            el.text for el in root.iter()
            if el.tag.rsplit('}', 1)[-1] == 'Key'
            and el.text and el.text.endswith('.nc')
        )
        if not nc_keys:
            return None
        # ipe05 object keys embed the cycle timestamp, so the
        # lexically-greatest key is the most recent grid.
        return f"{WAMIPE_S3_BASE_URL}/{nc_keys[-1]}"

    def _download_and_parse_wamipe(self, url: str) -> Optional[IonoGrid]:
        """Download a WAM-IPE NetCDF file and parse into IonoGrid."""
        if _requests is None:
            return None
        requests = _requests

        try:
            resp = requests.get(url, timeout=30, stream=True)
            if resp.status_code != 200:
                return None

            # Skip HTML directory listings and XML (an S3 listing or
            # error document) — neither is ever NetCDF (P-H19).
            content_type = resp.headers.get('content-type', '').lower()
            if 'html' in content_type or 'xml' in content_type:
                return None
            
            data = resp.content
            if len(data) < 1000:
                return None
            
            # Save to cache
            cache_file = self.cache_dir / "latest_wamipe.nc"
            cache_file.write_bytes(data)
            
            return self._parse_wamipe_netcdf(cache_file)
            
        except requests.RequestException as e:
            logger.debug(f"WAM-IPE download failed: {e}")
            return None
    
    def _parse_wamipe_netcdf(self, filepath: Path) -> Optional[IonoGrid]:
        """
        Parse a WAM-IPE ipe05 NetCDF file into an IonoGrid.
        
        WAM-IPE ipe05 files contain:
        - lat: latitude array
        - lon: longitude array  
        - TEC: Total Electron Content (TECU)
        - NmF2: F2 peak electron density (m^-3)
        - HmF2: F2 peak height (km)
        """
        try:
            import xarray as xr
            
            ds = xr.open_dataset(filepath, engine='scipy')
            
            # Extract coordinate arrays
            # WAM-IPE uses various coordinate names
            lat_names = ['lat', 'latitude', 'Latitude', 'geographic_latitude']
            lon_names = ['lon', 'longitude', 'Longitude', 'geographic_longitude']
            
            lats = None
            lons = None
            for name in lat_names:
                if name in ds.coords or name in ds.dims:
                    lats = ds[name].values
                    break
            for name in lon_names:
                if name in ds.coords or name in ds.dims:
                    lons = ds[name].values
                    break
            
            if lats is None or lons is None:
                # Try dimension names
                for dim in ds.dims:
                    if 'lat' in dim.lower() and lats is None:
                        lats = ds[dim].values if dim in ds.coords else np.arange(ds.dims[dim])
                    if 'lon' in dim.lower() and lons is None:
                        lons = ds[dim].values if dim in ds.coords else np.arange(ds.dims[dim])
            
            if lats is None or lons is None:
                logger.warning(f"Could not find lat/lon in WAM-IPE file. Dims: {list(ds.dims)}")
                ds.close()
                return None
            
            # Extract ionospheric fields
            hmF2 = self._extract_var(ds, ['HmF2', 'hmF2', 'hmf2', 'HMTF'])
            NmF2 = self._extract_var(ds, ['NmF2', 'nmF2', 'nmf2', 'NMTF'])
            TEC = self._extract_var(ds, ['TEC', 'tec', 'VTEC', 'vtec'])

            # Never fabricate a grid from constant defaults. The operational
            # WFS feeds publish only the WAM *neutral* atmosphere (no
            # ionosphere variables), so an ipe05-style file with none of
            # hmF2/NmF2/TEC is not the product we need. Filling defaults here
            # would have served a uniform fake grid tagged source="wamipe" at
            # full confidence — reject instead (see module docstring).
            if hmF2 is None and NmF2 is None and TEC is None:
                logger.warning(
                    "WAM-IPE file %s has no ionosphere variables "
                    "(hmF2/NmF2/TEC) — not an IPE product; discarding",
                    filepath,
                )
                ds.close()
                return None
            
            # Extract timestamp from attributes or filename
            timestamp = datetime.now(timezone.utc)
            for attr in ['time', 'valid_time', 'forecast_time']:
                if attr in ds.attrs:
                    try:
                        timestamp = datetime.fromisoformat(str(ds.attrs[attr]))
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass
            
            # Check for time dimension and take latest
            if 'time' in ds.dims:
                time_vals = ds['time'].values
                if len(time_vals) > 0:
                    # Take the last time step
                    t_idx = -1
                    if hmF2 is not None and hmF2.ndim > 2:
                        hmF2 = hmF2[t_idx]
                    if NmF2 is not None and NmF2.ndim > 2:
                        NmF2 = NmF2[t_idx]
                    if TEC is not None and TEC.ndim > 2:
                        TEC = TEC[t_idx]
            
            model_cycle = ds.attrs.get('cycle', ds.attrs.get('model_cycle', ''))
            
            ds.close()
            
            # Build grid
            grid = IonoGrid(
                timestamp=timestamp,
                source="wamipe",
                lats=np.asarray(lats, dtype=np.float64),
                lons=np.asarray(lons, dtype=np.float64),
                hmF2=np.asarray(hmF2, dtype=np.float64) if hmF2 is not None else np.full((len(lats), len(lons)), 300.0),
                NmF2=np.asarray(NmF2, dtype=np.float64) if NmF2 is not None else np.full((len(lats), len(lons)), 1e12),
                TEC=np.asarray(TEC, dtype=np.float64) if TEC is not None else np.full((len(lats), len(lons)), 20.0),
                model_cycle=str(model_cycle),
            )
            
            # Validate/normalise before use: ascending coordinates and
            # finite physical field values (P-M16).
            grid = self._validate_grid(grid)
            if grid is None:
                logger.warning("WAM-IPE grid failed validation — discarding")
                return None

            logger.debug(f"Parsed WAM-IPE grid: {len(grid.lats)}x{len(grid.lons)}, "
                        f"hmF2 range [{grid.hmF2.min():.0f}, {grid.hmF2.max():.0f}] km")

            return grid

        except ImportError:
            logger.warning("xarray not available for WAM-IPE parsing")
            return None
        except Exception as e:
            logger.warning(f"Failed to parse WAM-IPE NetCDF: {e}")
            return None
    
    @staticmethod
    def _extract_var(ds, names: List[str]) -> Optional[np.ndarray]:
        """Extract a variable from xarray dataset, trying multiple names."""
        for name in names:
            if name in ds.data_vars:
                return ds[name].values
        return None

    @staticmethod
    def _validate_grid(grid: 'IonoGrid') -> Optional['IonoGrid']:
        """
        Validate and normalise a parsed WAM-IPE grid (P-M16).

        Two properties the bilinear interpolator silently depends on:

        * Coordinates strictly ascending — ``IonoGrid.interpolate`` uses
          ``np.searchsorted``, which needs monotonic-ascending lats/lons.
          NetCDF latitude is commonly stored north-to-south (descending),
          which would have produced wrong bracketing indices. Each axis
          is sorted ascending and the field arrays reordered to match;
          duplicate coordinates leave the grid unusable.
        * Finite, physical field values — NetCDF fill values (NaN, ±Inf,
          sentinels such as 9.99e36) poison the bilinear average. Bad
          cells are replaced with the median of the good cells; a field
          with no valid cell at all rejects the grid.

        Returns the normalised grid, or None if it cannot be used.
        """
        lats = np.asarray(grid.lats, dtype=np.float64)
        lons = np.asarray(grid.lons, dtype=np.float64)
        if lats.ndim != 1 or lons.ndim != 1 or lats.size < 2 or lons.size < 2:
            logger.warning("WAM-IPE grid rejected: coordinate arrays not 1-D / too small")
            return None

        fields = {'hmF2': grid.hmF2, 'NmF2': grid.NmF2, 'TEC': grid.TEC}
        expected = (lats.size, lons.size)
        for name, arr in fields.items():
            if arr.size and arr.shape != expected:
                logger.warning(
                    f"WAM-IPE grid rejected: {name} shape {arr.shape} != {expected}"
                )
                return None

        # Sort each coordinate axis ascending, reordering the field arrays.
        lat_order = np.argsort(lats, kind='stable')
        lon_order = np.argsort(lons, kind='stable')
        lats = lats[lat_order]
        lons = lons[lon_order]
        if np.any(np.diff(lats) <= 0) or np.any(np.diff(lons) <= 0):
            logger.warning("WAM-IPE grid rejected: duplicate / non-monotonic coordinates")
            return None
        for name in list(fields):
            arr = fields[name]
            if arr.size:
                fields[name] = arr[np.ix_(lat_order, lon_order)]
        ne_3d = grid.Ne_3d
        if ne_3d is not None and ne_3d.ndim >= 2:
            ne_3d = ne_3d[lat_order][:, lon_order]

        # Replace fill values / non-physical cells with the field median.
        bounds = {'hmF2': (100.0, 600.0), 'NmF2': (1e9, 1e14), 'TEC': (0.1, 400.0)}
        for name, (lo, hi) in bounds.items():
            arr = fields[name]
            if not arr.size:
                continue
            bad = ~np.isfinite(arr) | (arr < lo) | (arr > hi)
            if bad.all():
                logger.warning(f"WAM-IPE grid rejected: {name} has no valid cells")
                return None
            if bad.any():
                median = float(np.median(arr[~bad]))
                fields[name] = np.where(bad, median, arr)
                logger.debug(
                    f"WAM-IPE grid: replaced {int(bad.sum())} fill/out-of-range "
                    f"{name} cell(s) with median {median:.3g}"
                )

        return IonoGrid(
            timestamp=grid.timestamp,
            source=grid.source,
            lats=lats,
            lons=lons,
            hmF2=fields['hmF2'],
            NmF2=fields['NmF2'],
            TEC=fields['TEC'],
            altitudes=grid.altitudes,
            Ne_3d=ne_3d,
            forecast_hour=grid.forecast_hour,
            model_cycle=grid.model_cycle,
        )
    
    def _load_cached_grid(self) -> Optional[IonoGrid]:
        """Load the most recent cached WAM-IPE grid."""
        cache_file = self.cache_dir / "latest_wamipe.nc"
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < WAMIPE_CACHE_MAX_AGE_S:
                return self._parse_wamipe_netcdf(cache_file)
        return None
    
    # §4.4 Low (2026-05-20): `_save_grid_cache(self, grid)` lived here.
    # It was never called -- the NetCDF file is persisted by the
    # download path itself, and the bare "grid_meta.txt" sidecar this
    # method wrote had no readers anywhere in the tree.  Removed; if a
    # metadata-on-disk format is needed in the future, build it
    # alongside the data product (HDF5 sidecar / JSON next to the .nc)
    # rather than a free-floating text file in the cache directory.

    # =========================================================================
    # GIRO DATA FETCHING
    # =========================================================================
    
    def _fetch_giro(self):
        """
        Fetch latest ionosonde measurements from GIRO DIDBase.
        
        GIRO provides real-time autoscaled ionosonde data that can correct
        WAM-IPE systematic biases, especially for hmF2.
        """
        if _requests is None:
            return
        requests = _requests
        
        now = datetime.now(timezone.utc)
        
        try:
            # Fetch station list if not cached, or refresh hourly
            if not self._giro_stations or (
                hasattr(self, '_giro_stations_fetched') and
                (now - self._giro_stations_fetched).total_seconds() > 3600
            ):
                self._fetch_giro_stations()
                self._giro_stations_fetched = now
            
            # Fetch latest measurements from the nearest ionosondes to the
            # operator. Polling the whole network every cycle hammers a
            # frequently-overloaded server and distant stations carry zero
            # GIRO weight anyway.
            updated = 0
            for station in self._stations_to_poll():
                try:
                    meas = self._fetch_giro_station_data(station.code)
                    if meas is not None:
                        with self._giro_lock:
                            self._giro_measurements[station.code] = meas
                        updated += 1
                except Exception as e:
                    logger.debug(f"GIRO fetch failed for {station.code}: {e}")
            
            if updated > 0:
                self._stats['giro_fetches'] += 1
                self._stats['last_giro_update'] = now.isoformat()
                logger.info(f"GIRO updated: {updated} stations")
            else:
                self._stats['giro_failures'] += 1
                
        except Exception as e:
            self._stats['giro_failures'] += 1
            logger.warning(f"GIRO fetch failed: {e}")
    
    # DIDBFastStationList is an HTML table; each station row carries the URSI
    # code in a DIDBYearListForStation anchor followed by three <big> cells:
    # name, latitude, longitude (degrees; longitude in 0..360).
    _STATION_ROW_RE = re.compile(
        r'ursiCode=(?P<code>[A-Za-z0-9]+)"[^>]*>[A-Za-z0-9]+</a>\s*</big>\s*</td>'
        r'\s*<td[^>]*>\s*<big>\s*(?P<name>[^<]*?)\s*</big>\s*</td>'
        r'\s*<td[^>]*>\s*<big>\s*(?P<lat>-?\d+(?:\.\d+)?)\s*</big>\s*</td>'
        r'\s*<td[^>]*>\s*<big>\s*(?P<lon>-?\d+(?:\.\d+)?)\s*</big>\s*</td>',
        re.IGNORECASE,
    )

    @classmethod
    def _parse_didbase_station_html(cls, html: str) -> List[GiroStation]:
        """Parse the DIDBFastStationList HTML page into GiroStation records.

        Longitudes on the page run 0..360; they are normalised to
        [-180, 180) to match great_circle_distance and the rest of the
        service. Returns an empty list if the page format is unrecognised
        (e.g. an error/maintenance page), so the caller can fall back.
        """
        stations: List[GiroStation] = []
        for m in cls._STATION_ROW_RE.finditer(html):
            try:
                lat = float(m['lat'])
                lon = ((float(m['lon']) + 180.0) % 360.0) - 180.0
            except (TypeError, ValueError):
                continue
            stations.append(GiroStation(
                code=m['code'].upper(),
                name=m['name'].strip(),
                latitude=lat,
                longitude=lon,
            ))
        return stations

    def _load_bundled_stations(self) -> List[GiroStation]:
        """Load the bundled station fallback table (package data / disk cache)."""
        # Prefer the on-disk cache written from a previous live fetch, then
        # the package-bundled snapshot.
        for path in (self.cache_dir / "giro_stations.tsv", BUNDLED_STATIONS_FILE):
            try:
                if not path.is_file():
                    continue
                stations: List[GiroStation] = []
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('\t')
                    if len(parts) < 4:
                        continue
                    try:
                        stations.append(GiroStation(
                            code=parts[0].upper(),
                            name=parts[1],
                            latitude=float(parts[2]),
                            longitude=float(parts[3]),
                        ))
                    except ValueError:
                        continue
                if stations:
                    logger.info("GIRO: loaded %d stations from %s",
                                len(stations), path)
                    return stations
            except OSError as e:
                logger.debug("GIRO bundled station load failed (%s): %s", path, e)
        return []

    def _save_station_cache(self, stations: List[GiroStation]):
        """Persist the parsed station list so restarts don't depend on the
        flaky DIDBase page."""
        try:
            tmp = self.cache_dir / "giro_stations.tsv.tmp"
            lines = ["# GIRO station list cache (URSI<TAB>name<TAB>lat<TAB>lon)"]
            lines += [f"{s.code}\t{s.name}\t{s.latitude:.2f}\t{s.longitude:.2f}"
                      for s in stations]
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(self.cache_dir / "giro_stations.tsv")
        except OSError as e:
            logger.debug("GIRO station cache write failed: %s", e)

    def _fetch_giro_stations(self):
        """Refresh the GIRO ionosonde station list.

        Parses the DIDBFastStationList HTML page. On any failure (network,
        503/404 flap, or an unrecognised page) keeps any existing list and,
        if we have none yet, loads the bundled/disk fallback so GIRO can
        still operate.
        """
        stations: List[GiroStation] = []
        if self._session is not None:
            try:
                resp = self._session.get(
                    GIRO_STATION_LIST_URL,
                    timeout=getattr(_net_fetch, "DEFAULT_TIMEOUT", (10, 30)),
                )
                if resp.status_code == 200:
                    stations = self._parse_didbase_station_html(resp.text)
                    if not stations:
                        logger.warning(
                            "GIRO station list returned %d bytes but no parseable "
                            "rows (page format changed?) — using fallback",
                            len(resp.text),
                        )
                else:
                    logger.debug("GIRO station list HTTP %s", resp.status_code)
            except Exception as e:
                logger.debug("GIRO station list fetch failed: %s", e)

        if stations:
            self._giro_stations = stations
            self._save_station_cache(stations)
            logger.info("GIRO: loaded %d ionosonde stations (live)", len(stations))
        elif not self._giro_stations:
            self._giro_stations = self._load_bundled_stations()
    
    def _stations_to_poll(self) -> List[GiroStation]:
        """Select which ionosondes to query this cycle.

        With a known operator location, return the GIRO_MAX_STATIONS_POLLED
        nearest stations (by great-circle distance); otherwise fall back to
        the first N of the list.
        """
        stations = self._giro_stations
        if not stations:
            return []
        if self.home_lat is not None and self.home_lon is not None:
            stations = sorted(
                stations,
                key=lambda s: great_circle_distance(
                    self.home_lat, self.home_lon, s.latitude, s.longitude
                ),
            )
        return stations[:GIRO_MAX_STATIONS_POLLED]

    def _fetch_giro_station_data(self, station_code: str) -> Optional[GiroMeasurement]:
        """Fetch latest ionosonde measurement for a station via DIDBGetValues."""
        if self._session is None:
            return None

        now = datetime.now(timezone.utc)
        # Request the last 60 minutes — digisondes autoscale roughly every
        # 5-15 min, and a 30-min window occasionally missed the latest sounding.
        start = now - timedelta(minutes=60)

        # DIDBGetValues expects dates as 'YYYY/MM/DD HH:MM:SS'. DMUF=3000 asks
        # for the standard 3000 km MUF factor column set; CS is the autoscaling
        # confidence score we map to GiroMeasurement.confidence.
        params = {
            'ursiCode': station_code,
            'charName': 'foF2,hmF2,CS',
            'DMUF': 3000,
            'fromDate': start.strftime('%Y/%m/%d %H:%M:%S'),
            'toDate': now.strftime('%Y/%m/%d %H:%M:%S'),
        }

        try:
            resp = self._session.get(
                GIRO_GETVALUES_URL, params=params,
                timeout=getattr(_net_fetch, "DEFAULT_TIMEOUT", (10, 30)),
            )
            if resp.status_code != 200:
                return None

            parsed = self._parse_didb_characteristics(resp.text)
            if parsed is None:
                return None
            foF2, hmF2, conf01 = parsed
            return GiroMeasurement(
                station_code=station_code,
                timestamp=now,
                foF2_MHz=foF2,
                hmF2_km=hmF2,
                confidence=conf01,
            )

        except Exception as e:
            logger.debug(f"Caught exception: {e}")
            return None

    @staticmethod
    def _parse_didb_characteristics(
        text: str,
    ) -> Optional[Tuple[float, float, float]]:
        """Parse a DIDBGetValues response into (foF2_MHz, hmF2_km, conf01).

        DIDBGetValues returns '#'-prefixed metadata/comment lines — one of
        which is the column header naming the requested characteristics
        (e.g. ``#Time CS foF2 QD hmF2 QD``) — followed by whitespace-
        delimited data rows. We parse foF2/hmF2/CS by *column name* from
        that header: a swapped or inserted column would otherwise silently
        corrupt hmF2, which is blended into the propagation geometry at
        weight up to 1.0 (P-H20). The positional layout (foF2, hmF2, conf
        as the last columns) is used only when no header is present.

        The result is range-validated against ionospheric physics
        (foF2 0.5–30 MHz, hmF2 100–600 km); anything outside is treated as
        a parse error and rejected. ``conf01`` is the CS autoscaling score
        normalised to 0–1. Returns None if no usable row is found.
        """
        header_cols = None
        data_lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith('#'):
                toks = s.lstrip('#').split()
                if 'foF2' in toks and 'hmF2' in toks:
                    header_cols = toks
            else:
                data_lines.append(s)
        if not data_lines:
            return None

        # Most recent measurement = last data row.
        parts = data_lines[-1].split()
        try:
            if header_cols is not None:
                foF2 = float(parts[header_cols.index('foF2')])
                hmF2 = float(parts[header_cols.index('hmF2')])
                conf = 0.0
                for cs_name in ('CS', 'Confidence', 'confidence'):
                    if cs_name in header_cols:
                        conf = float(parts[header_cols.index(cs_name)])
                        break
            else:
                # No header — documented positional layout
                # (timestamp, foF2, hmF2, confidence).
                foF2 = float(parts[-3])
                hmF2 = float(parts[-2])
                conf = float(parts[-1])
        except (ValueError, IndexError):
            return None

        # Range-validate: a corrupted column (a QD letter, a confidence
        # score, a timestamp field) will not fall inside both physical
        # ranges — foF2 0.5–30 MHz, hmF2 100–600 km.
        if not (0.5 <= foF2 <= 30.0 and 100.0 <= hmF2 <= 600.0):
            logger.debug(
                "GIRO: parsed foF2=%s MHz / hmF2=%s km out of physical "
                "range — rejected", foF2, hmF2,
            )
            return None

        return foF2, hmF2, min(1.0, max(0.0, conf) / 100.0)
    
    # =========================================================================
    # PUBLIC API
    # =========================================================================
    
    @staticmethod
    def _blend_points(
        p_prev: IonoGridPoint,
        p_curr: IonoGridPoint,
        frac: float,
        utc_time: datetime,
    ) -> IonoGridPoint:
        """Linearly interpolate two grid points in time (frac=0 → p_prev,
        frac=1 → p_curr)."""
        f = max(0.0, min(1.0, frac))
        g = 1.0 - f
        nmf2 = g * p_prev.NmF2_m3 + f * p_curr.NmF2_m3
        return IonoGridPoint(
            latitude=p_curr.latitude,
            longitude=p_curr.longitude,
            timestamp=utc_time,
            hmF2_km=g * p_prev.hmF2_km + f * p_curr.hmF2_km,
            NmF2_m3=nmf2,
            # foF2 ∝ √NmF2; recompute from the blended density so the two
            # stay mutually consistent.
            foF2_MHz=float(np.sqrt(max(0.0, nmf2) / 1.24e10)),
            TEC_TECU=g * p_prev.TEC_TECU + f * p_curr.TEC_TECU,
            hmE_km=g * p_prev.hmE_km + f * p_curr.hmE_km,
            source=p_curr.source,
        )

    def _apply_temporal_interpolation(
        self,
        curr_point: IonoGridPoint,
        prev_grid: Optional[IonoGrid],
        curr_grid: IonoGrid,
        lat: float,
        lon: float,
        utc_time: datetime,
    ) -> IonoGridPoint:
        """
        Blend the previous and current WAM-IPE grids in time (P-M16).

        WAM-IPE grids arrive on a ~5-minute cadence; serving only the
        current grid makes get_iono_params step discontinuously every
        time a new grid replaces the old one. When the query time lies
        between the two grids' valid times, the previous and current grid
        points are linearly interpolated; otherwise (query outside the
        pair's window, or no usable previous grid) the current grid's
        point is returned unchanged.
        """
        if prev_grid is None or not prev_grid.is_valid():
            return curr_point
        if prev_grid.timestamp is None or curr_grid.timestamp is None:
            return curr_point

        def _utc(t: datetime) -> datetime:
            return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)

        t_prev = _utc(prev_grid.timestamp)
        t_curr = _utc(curr_grid.timestamp)
        q = _utc(utc_time)
        span = (t_curr - t_prev).total_seconds()
        if span <= 0:
            return curr_point  # grids identical / out of order — no blend
        frac = (q - t_prev).total_seconds() / span
        if frac <= 0.0 or frac >= 1.0:
            return curr_point  # query outside the [previous, current] window

        prev_point = prev_grid.interpolate(lat, lon)
        return self._blend_points(prev_point, curr_point, frac, q)

    def get_iono_params(
        self,
        lat: float,
        lon: float,
        utc_time: Optional[datetime] = None
    ) -> IonoGridPoint:
        """
        Get interpolated ionospheric parameters at a location and time.
        
        This is the primary API for the propagation model. Returns the best
        available ionospheric parameters by combining WAM-IPE grid data
        with GIRO corrections.
        
        Args:
            lat: Latitude in degrees
            lon: Longitude in degrees
            utc_time: UTC time (default: now)
            
        Returns:
            IonoGridPoint with hmF2, NmF2, TEC, foF2
        """
        if utc_time is None:
            utc_time = datetime.now(timezone.utc)
        
        # Start with WAM-IPE grid interpolation
        with self._grid_lock:
            grid = self._current_grid
            prev_grid = self._previous_grid

        # A grid older than WAMIPE_CACHE_MAX_AGE_S must not be served as
        # current data: if the background fetch stalls, an hours-old grid
        # was otherwise returned tagged source="wamipe" at full
        # confidence (P-H22).  Treat a stale grid as unusable and fall
        # back to the climatological model.
        grid_age_s = None
        if grid is not None and grid.timestamp is not None:
            gts = grid.timestamp
            if gts.tzinfo is None:
                gts = gts.replace(tzinfo=timezone.utc)
            ref = utc_time if utc_time.tzinfo is not None \
                else utc_time.replace(tzinfo=timezone.utc)
            grid_age_s = (ref - gts).total_seconds()

        grid_stale = (
            grid_age_s is not None and grid_age_s > WAMIPE_CACHE_MAX_AGE_S
        )
        if grid is not None and grid.is_valid() and not grid_stale:
            point = grid.interpolate(lat, lon)
            # Temporal interpolation across the previous→current grid pair
            # so the result does not step each time a new grid is fetched.
            point = self._apply_temporal_interpolation(
                point, prev_grid, grid, lat, lon, utc_time
            )
        else:
            if grid_stale:
                logger.debug(
                    f"WAM-IPE grid stale ({grid_age_s / 3600.0:.1f} h old, "
                    f"max {WAMIPE_CACHE_MAX_AGE_S / 3600.0:.1f} h) — "
                    f"using climatological fallback"
                )
            # Fallback base: IRI-2020 if available, else crude climatology.
            point = self._base_point(lat, lon, utc_time)
        
        # Apply GIRO corrections if available
        giro_correction = self._get_giro_correction(lat, lon, utc_time)
        if giro_correction is not None:
            # Blend WAM-IPE with GIRO: weight by distance and confidence
            w_giro = giro_correction[2]  # weight
            point.hmF2_km = (1 - w_giro) * point.hmF2_km + w_giro * giro_correction[0]
            point.foF2_MHz = (1 - w_giro) * point.foF2_MHz + w_giro * giro_correction[1]
            # Recompute NmF2 from corrected foF2
            point.NmF2_m3 = 1.24e10 * point.foF2_MHz ** 2
            point.source = f"{point.source}+giro"
        
        return point
    
    def get_electron_density_profile(
        self,
        lat: float,
        lon: float,
        utc_time: Optional[datetime] = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Get electron density profile Ne(h) for ray-tracing.
        
        If WAM-IPE 3D data is available, uses that. Otherwise constructs
        a Chapman layer profile from the 2D parameters.
        
        Args:
            lat: Latitude in degrees
            lon: Longitude in degrees
            utc_time: UTC time (default: now)
            
        Returns:
            Tuple of (altitudes_km, Ne_m3) arrays
        """
        # Try WAM-IPE 3D data first
        with self._grid_lock:
            grid = self._current_grid
        
        if grid is not None:
            profile = grid.get_electron_density_profile(lat, lon)
            if profile is not None:
                return profile
        
        # Construct Chapman layer profile from 2D parameters
        point = self.get_iono_params(lat, lon, utc_time)
        return self._chapman_profile(point)
    
    def get_tec_along_path(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        utc_time: Optional[datetime] = None,
        n_points: int = 10
    ) -> float:
        """
        Estimate slant TEC along a ground path by integrating VTEC samples.
        
        This is a simplified approach: samples VTEC at n_points along the
        great circle path and averages. For more accurate slant TEC, use
        the ray-tracing in propagation_model.py.
        
        Args:
            lat1, lon1: Start point (transmitter)
            lat2, lon2: End point (receiver)
            utc_time: UTC time
            n_points: Number of sample points along path
            
        Returns:
            Estimated path TEC in TECU
        """
        if utc_time is None:
            utc_time = datetime.now(timezone.utc)
        
        tec_sum = 0.0
        for i in range(n_points):
            frac = (i + 0.5) / n_points
            lat, lon = self._gc_intermediate(lat1, lon1, lat2, lon2, frac)
            point = self.get_iono_params(lat, lon, utc_time)
            tec_sum += point.TEC_TECU
        
        return tec_sum / n_points
    
    @staticmethod
    def _gc_intermediate(
        lat1: float, lon1: float, lat2: float, lon2: float, frac: float
    ) -> Tuple[float, float]:
        """Intermediate point on the great circle at given fraction (0-1)."""
        lat1_r = math.radians(lat1)
        lon1_r = math.radians(lon1)
        lat2_r = math.radians(lat2)
        lon2_r = math.radians(lon2)

        d = 2 * math.asin(math.sqrt(
            math.sin((lat2_r - lat1_r) / 2) ** 2 +
            math.cos(lat1_r) * math.cos(lat2_r) *
            math.sin((lon2_r - lon1_r) / 2) ** 2
        ))
        if d < 1e-12:
            return lat1, lon1

        a = math.sin((1 - frac) * d) / math.sin(d)
        b = math.sin(frac * d) / math.sin(d)
        x = a * math.cos(lat1_r) * math.cos(lon1_r) + b * math.cos(lat2_r) * math.cos(lon2_r)
        y = a * math.cos(lat1_r) * math.sin(lon1_r) + b * math.cos(lat2_r) * math.sin(lon2_r)
        z = a * math.sin(lat1_r) + b * math.sin(lat2_r)
        return math.degrees(math.atan2(z, math.sqrt(x**2 + y**2))), math.degrees(math.atan2(y, x))
    
    def _get_giro_correction(
        self,
        lat: float,
        lon: float,
        utc_time: datetime
    ) -> Optional[Tuple[float, float, float]]:
        """
        Get GIRO-based correction for hmF2 and foF2.
        
        Finds the nearest GIRO station with recent data and returns
        a correction weighted by distance.
        
        Returns:
            Tuple of (hmF2_km, foF2_MHz, weight) or None
        """
        with self._giro_lock:
            if not self._giro_measurements:
                return None
            
            # Find nearest station with recent data
            best_station = None
            best_distance = float('inf')
            
            for code, meas in self._giro_measurements.items():
                # Check freshness (max 30 minutes)
                age = (utc_time - meas.timestamp).total_seconds()
                if abs(age) > 1800:
                    continue
                
                # Find station coordinates
                station = None
                for s in self._giro_stations:
                    if s.code == code:
                        station = s
                        break
                
                if station is None:
                    continue
                
                # Great-circle distance in km (P-M16). The old Euclidean
                # distance in degrees overstated east-west spans away from
                # the equator and was grossly wrong across the ±180°
                # dateline (e.g. lon 179° vs −179° read as 358° apart).
                dist_km = great_circle_distance(
                    lat, lon, station.latitude, station.longitude
                )

                if dist_km < best_distance:
                    best_distance = dist_km
                    best_station = (meas, dist_km)

            if best_station is None:
                return None

            meas, dist_km = best_station

            # Weight decreases with distance: full weight within
            # GIRO_FULL_WEIGHT_KM, zero beyond GIRO_ZERO_WEIGHT_KM.
            if dist_km > GIRO_ZERO_WEIGHT_KM:
                return None

            weight = max(0.0, min(1.0,
                         (GIRO_ZERO_WEIGHT_KM - dist_km)
                         / (GIRO_ZERO_WEIGHT_KM - GIRO_FULL_WEIGHT_KM)))
            weight *= meas.confidence  # Scale by measurement confidence
            
            if weight < 0.05:
                return None
            
            return (meas.hmF2_km, meas.foF2_MHz, weight)
    
    def _get_iri_model(self):
        """Lazily construct the IonosphericModel (IRI-2020) used as the
        no-grid base. Latches off if it cannot be built so we don't retry
        an expensive import/initialisation on every call."""
        if not self.enable_iri_fallback or self._iri_unavailable:
            return None
        if self._iri_model is None:
            try:
                from .ionospheric_model import IonosphericModel
                self._iri_model = IonosphericModel(enable_iri=True)
            except Exception as e:
                logger.info(
                    "IRI-2020 base model unavailable (%s) — using parametric "
                    "climatology fallback", e
                )
                self._iri_unavailable = True
                return None
        return self._iri_model

    def _base_point(
        self, lat: float, lon: float, utc_time: datetime
    ) -> IonoGridPoint:
        """Best available base ionospheric point when no real-time grid is
        usable: IRI-2020 (source="iri") if available, else the crude internal
        parametric climatology (source="climatological_fallback").

        GIRO measurements are blended on top of whatever base this returns,
        so a nearby ionosonde refines IRI rather than crude climatology.
        """
        iri = self._get_iri_model()
        if iri is not None:
            try:
                heights = iri.get_layer_heights(
                    timestamp=utc_time, latitude=lat, longitude=lon
                )
                if heights is not None:
                    foF2 = getattr(heights, 'foF2', None)
                    if foF2:
                        nmF2 = 1.24e10 * foF2 ** 2
                    else:
                        nmF2 = 1e12
                        foF2 = float(np.sqrt(nmF2 / 1.24e10))
                    tec = getattr(heights, 'tec_tecu', None)
                    if tec is None:
                        # IRI build without TEC: borrow the climatological TEC
                        # so downstream slant-TEC estimates stay reasonable.
                        tec = self._climatological_fallback(
                            lat, lon, utc_time
                        ).TEC_TECU
                    # Label by the tier that actually produced the estimate:
                    # IonosphericModel transparently falls back to its own
                    # parametric model when the IRI binary is unavailable, and
                    # tagging that "iri" would overstate the data quality.
                    tier = getattr(heights, 'tier', None)
                    tv = getattr(tier, 'value', '') or ''
                    if 'IRI' in tv:
                        src = 'iri'
                    elif tv.lower().startswith('param'):
                        src = 'parametric'
                    elif tv.lower().startswith('static'):
                        src = 'static'
                    else:
                        src = tv.lower() or 'iri'
                    return IonoGridPoint(
                        latitude=lat,
                        longitude=lon,
                        timestamp=utc_time,
                        hmF2_km=float(heights.hmF2),
                        NmF2_m3=float(nmF2),
                        foF2_MHz=float(foF2),
                        TEC_TECU=float(tec),
                        hmE_km=float(getattr(heights, 'hmE', 110.0) or 110.0),
                        source=src,
                    )
            except Exception as e:
                logger.debug("IRI base point failed, using climatology: %s", e)
        return self._climatological_fallback(lat, lon, utc_time)

    @staticmethod
    def _climatological_fallback(
        lat: float,
        lon: float,
        utc_time: datetime
    ) -> IonoGridPoint:
        """
        Climatological fallback when no real-time data is available.
        
        Uses simple parametric models based on:
        - Local time (diurnal variation)
        - Latitude (equatorial anomaly)
        - Season (annual variation)
        """
        import math
        
        # Local solar time
        lst = utc_time.hour + utc_time.minute / 60.0 + lon / 15.0
        lst = lst % 24.0
        
        # Day of year for seasonal variation
        doy = utc_time.timetuple().tm_yday
        
        # hmF2: diurnal + seasonal + latitudinal
        # Base: ~250 km day, ~350 km night
        diurnal_phase = (lst - 14.0) / 24.0 * 2 * math.pi
        hmF2_base = 300.0 - 50.0 * math.cos(diurnal_phase)
        
        # Seasonal: higher in summer
        seasonal_phase = (doy - 172) / 365.25 * 2 * math.pi  # Peak at summer solstice
        if lat < 0:
            seasonal_phase += math.pi  # Southern hemisphere
        hmF2_seasonal = 20.0 * math.cos(seasonal_phase)
        
        hmF2 = hmF2_base + hmF2_seasonal
        
        # foF2: diurnal variation
        # ~3 MHz night, ~8 MHz day
        foF2_base = 5.5 + 2.5 * math.cos(diurnal_phase)
        
        # Equatorial anomaly: higher foF2 near ±15° magnetic latitude
        mag_lat = lat  # Simplified (should use magnetic coordinates)
        equatorial_factor = 1.0 + 0.3 * math.exp(-((abs(mag_lat) - 15) / 10) ** 2)
        foF2 = foF2_base * equatorial_factor
        
        # NmF2 from foF2
        NmF2 = 1.24e10 * foF2 ** 2
        
        # TEC: roughly proportional to NmF2 * scale height
        # Typical: 10-80 TECU
        TEC = 5.0 + 35.0 * (1 + math.cos(diurnal_phase)) / 2.0
        
        return IonoGridPoint(
            latitude=lat,
            longitude=lon,
            timestamp=utc_time,
            hmF2_km=hmF2,
            NmF2_m3=NmF2,
            foF2_MHz=foF2,
            TEC_TECU=TEC,
            hmE_km=110.0,
            source="climatological_fallback"
        )
    
    @staticmethod
    def _chapman_profile(
        point: IonoGridPoint,
        alt_min: float = 80.0,
        alt_max: float = 1000.0,
        alt_step: float = 5.0
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Construct a Chapman layer electron density profile from 2D parameters.
        
        The Chapman function:
            Ne(h) = NmF2 * exp(0.5 * (1 - z - exp(-z)))
            where z = (h - hmF2) / H
            H = scale height (~50-80 km for F2 layer)
        
        Also adds an E-layer contribution.
        
        Args:
            point: IonoGridPoint with hmF2, NmF2, hmE
            
        Returns:
            Tuple of (altitudes_km, Ne_m3) arrays
        """
        altitudes = np.arange(alt_min, alt_max + alt_step, alt_step)
        Ne = np.zeros_like(altitudes)
        
        # F2 layer (Chapman)
        hmF2 = point.hmF2_km
        NmF2 = point.NmF2_m3
        # Dynamic scale height: empirically H ≈ 0.22 * hmF2 (40-90 km range)
        # Clamped to physically reasonable bounds
        H_F2 = max(40.0, min(90.0, 0.22 * hmF2))
        
        z_F2 = (altitudes - hmF2) / H_F2
        # Clip to avoid overflow
        z_F2 = np.clip(z_F2, -10, 10)
        Ne_F2 = NmF2 * np.exp(0.5 * (1 - z_F2 - np.exp(-z_F2)))
        
        # E layer (Chapman, smaller)
        # E-layer is solar-produced and essentially disappears at night.
        # Use timestamp to estimate solar zenith angle (simplified).
        hmE = point.hmE_km
        H_E = 10.0  # Scale height for E layer
        
        # Estimate local solar time to scale E-layer density
        lst = point.timestamp.hour + point.timestamp.minute / 60.0 + point.longitude / 15.0
        lst = lst % 24.0
        # Smooth day/night transition: cos² taper, peak at local noon (12h)
        solar_phase = math.pi * (lst - 12.0) / 12.0
        e_layer_factor = max(0.0, math.cos(solar_phase)) ** 2  # 0 at night, 1 at noon
        NmE = NmF2 * 0.1 * e_layer_factor
        
        z_E = (altitudes - hmE) / H_E
        z_E = np.clip(z_E, -10, 10)
        Ne_E = NmE * np.exp(0.5 * (1 - z_E - np.exp(-z_E)))
        
        Ne = Ne_F2 + Ne_E
        
        return altitudes, Ne
    
    def get_stats(self) -> Dict:
        """Get service statistics."""
        with self._grid_lock:
            has_grid = self._current_grid is not None
            grid_age = None
            if has_grid:
                grid_age = (datetime.now(timezone.utc) - self._current_grid.timestamp).total_seconds()
        
        with self._giro_lock:
            n_giro = len(self._giro_measurements)
        
        return {
            **self._stats,
            'has_grid': has_grid,
            'grid_age_s': grid_age,
            'giro_stations_with_data': n_giro,
        }
    
    def force_update(self):
        """Force an immediate data update."""
        self._fetch_event.set()
