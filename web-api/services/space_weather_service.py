"""
Space Weather Service - Ingest and provide solar/geomagnetic data.

Data sources:
- NOAA SWPC JSON API for X-ray flux, Kp index, proton flux
- Space Weather Canada for F10.7 solar flux
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from pathlib import Path
import json
import time
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


class SpaceWeatherService:
    """Service for fetching and caching space weather data."""
    
    NOAA_BASE_URL = "https://services.swpc.noaa.gov/json"
    CACHE_DIR = Path("/var/lib/timestd/space_weather_cache")
    CACHE_DURATION = timedelta(minutes=15)  # Cache API responses
    
    def __init__(self, cache_dir: Optional[Path] = None):
        """Initialize space weather service."""
        self.cache_dir = cache_dir or self.CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Space Weather Service initialized, cache: {self.cache_dir}")
    
    def _get_cached_or_fetch(self, cache_key: str, fetch_func, max_age: timedelta = None) -> Optional[Any]:
        """Get data from cache or fetch fresh data."""
        max_age = max_age or self.CACHE_DURATION
        cache_file = self.cache_dir / f"{cache_key}.json"
        
        # Check cache
        if cache_file.exists():
            cache_age = datetime.utcnow() - datetime.fromtimestamp(cache_file.stat().st_mtime)
            if cache_age < max_age:
                try:
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    logger.debug(f"Cache hit: {cache_key} (age: {cache_age})")
                    return data
                except Exception as e:
                    logger.warning(f"Cache read error for {cache_key}: {e}")
        
        # Fetch fresh data
        try:
            data = fetch_func()
            if data is not None:
                # Save to cache
                with open(cache_file, 'w') as f:
                    json.dump(data, f)
                logger.debug(f"Cache updated: {cache_key}")
            return data
        except Exception as e:
            logger.error(f"Fetch error for {cache_key}: {e}")
            # Try to return stale cache if available
            if cache_file.exists():
                try:
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    logger.warning(f"Using stale cache for {cache_key}")
                    return data
                except:
                    pass
            return None
    
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
            endpoint = "xrays-6-hour.json"
            cache_key = "xray_6hour"
        elif hours <= 24:
            endpoint = "xrays-1-day.json"
            cache_key = "xray_1day"
        elif hours <= 72:
            endpoint = "xrays-3-day.json"
            cache_key = "xray_3day"
        else:
            endpoint = "xrays-7-day.json"
            cache_key = "xray_7day"

        def fetch():
            url = f"{self.NOAA_BASE_URL}/goes/primary/{endpoint}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        
        data = self._get_cached_or_fetch(cache_key, fetch)
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
        
        logger.info(f"Retrieved {len(results)} X-ray flux measurements ({endpoint})")
        return results
    
    def get_kp_index(self, hours: int = 24) -> List[KpIndex]:
        """
        Get planetary Kp index data.
        
        Args:
            hours: Number of hours of history
        
        Returns:
            List of KpIndex measurements
        """
        def fetch():
            url = f"{self.NOAA_BASE_URL}/planetary_k_index_1m.json"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        
        data = self._get_cached_or_fetch("kp_index", fetch)
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
        def fetch():
            # 6-hour integral protons
            url = f"{self.NOAA_BASE_URL}/goes/primary/integral-protons-plot-6-hour.json"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        
        data = self._get_cached_or_fetch("proton_flux", fetch)
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
        Get daily solar indices (F10.7, Ap, sunspot number).
        
        Note: This is a placeholder. Full implementation would fetch from
        Space Weather Canada or NOAA archives.
        
        Args:
            days: Number of days of history
        
        Returns:
            List of SolarIndices
        """
        # TODO: Implement F10.7 fetching from Space Weather Canada
        # For now, return empty list
        logger.warning("Solar indices (F10.7) not yet implemented")
        return []
    
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
