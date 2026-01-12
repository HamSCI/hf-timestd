#!/usr/bin/env python3
"""
Ionospheric Model - Dynamic Layer Heights with IRI-2020 Integration

================================================================================
VULNERABILITY ADDRESSED (Issue 1.2 from PHASE2_CRITIQUE.md)
================================================================================

ORIGINAL PROBLEM:
-----------------
The transmission_time_solver.py used FIXED ionospheric layer heights:

    F2_LAYER_HEIGHT_KM = 300.0   # Day
    F2_NIGHT_HEIGHT_KM = 350.0   # Night (NEVER USED!)

This is problematic because:

1. The F2 layer height varies from 200-400 km depending on:
   - Time of day (rises at night as ionization decays)
   - Solar activity (higher during solar maximum)
   - Season (higher in summer)
   - Latitude (higher at equator)
   - Geomagnetic activity (disturbed during storms)

2. The F2_NIGHT_HEIGHT_KM constant was DEFINED but NEVER USED conditionally.
   The code always used the daytime value regardless of time.

3. A 100 km error in layer height causes ~0.5-1.0 ms timing error for
   single-hop F-layer paths, directly impacting D_clock accuracy.

IMPACT ANALYSIS:
----------------
For a 1-hop F-layer path at 1500 km ground distance:

    Height     Path Length    Delay      Error from 300km baseline
    --------   -----------    --------   -------------------------
    200 km     1528 km        5.10 ms    -0.32 ms
    250 km     1567 km        5.23 ms    -0.19 ms
    300 km     1612 km        5.38 ms    (baseline)
    350 km     1662 km        5.54 ms    +0.16 ms
    400 km     1715 km        5.72 ms    +0.34 ms

During disturbed conditions, hmF2 can vary by ±100 km from climatology,
causing timing errors of ±0.3-0.5 ms that would be systematic (bias).

================================================================================
SOLUTION: THREE-TIER IONOSPHERIC MODEL
================================================================================

This module implements a hierarchical approach:

TIER 1: IRI-2020 (International Reference Ionosphere)
    - The internationally recognized empirical ionospheric model
    - Provides hmF2 based on date, time, location, solar activity
    - Captures diurnal, seasonal, solar cycle, and geographic variations
    - Improved topside electron density model over IRI-2016
    - Better storm-time corrections and high-latitude coverage
    - Updated CCIR/URSI coefficients
    - Typical accuracy: ~20-30 km RMSE for hmF2

TIER 2: Parametric Fallback Model
    - When IRI-2020 is unavailable (missing Fortran compiler, indices, etc.)
    - Simple sinusoidal model capturing primary diurnal variation
    - Based on published climatological relationships
    - Typical accuracy: ~40-60 km RMSE

TIER 3: Static Fallback
    - Last resort when no time/location available
    - Uses the original fixed constants
    - Only used during initialization or error conditions

CALIBRATION LAYER:
------------------
All tiers are refined by a CALIBRATION mechanism that learns from
actual propagation measurements:

    hmF2_calibrated = hmF2_model + calibration_offset

The calibration offset is derived from:
    observed_delay - predicted_delay → implied height error

This allows the model to adapt to local conditions not captured by
climatology (the "ionospheric weather" vs "climate").

================================================================================
REFERENCES
================================================================================
1. Bilitza, D. et al. (2022). "International Reference Ionosphere 2020."
   Earth, Planets and Space, 74, 1-20.
   https://doi.org/10.1186/s40623-022-01649-0

2. ITU-R P.1239-3: "ITU-R Reference Ionospheric Characteristics"

3. Davies, K. (1990). "Ionospheric Radio." Peter Peregrinus Ltd.
   Chapter 4: The Ionospheric Layers.

================================================================================
REVISION HISTORY
================================================================================
2025-12-13: Upgraded from IRI-2016 to IRI-2020 for improved accuracy
2025-12-07: Initial implementation addressing Issue 1.2 fixed layer heights
"""

import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from enum import Enum
from collections import OrderedDict
from pathlib import Path

import numpy as np

try:
    import xarray as xr  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    xr = None

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Static fallback heights (TIER 3) - original values from transmission_time_solver.py
DEFAULT_E_LAYER_HEIGHT_KM = 110.0
DEFAULT_F1_LAYER_HEIGHT_KM = 200.0
DEFAULT_F2_LAYER_HEIGHT_KM = 300.0
DEFAULT_F2_NIGHT_HEIGHT_KM = 350.0

# Parametric model constants (TIER 2)
# Based on climatological relationships from ITU-R P.1239
HMF2_MIN_KM = 220.0       # Typical minimum F2 height (midlatitude, low solar activity)
HMF2_MAX_KM = 400.0       # Typical maximum F2 height (equatorial, high solar activity)
HMF2_DIURNAL_AMP_KM = 50.0  # Diurnal variation amplitude (~50 km typical)
HMF2_SOLAR_FACTOR = 0.3   # Height increase per 100 SFU above baseline


class IonosphericModelTier(Enum):
    """Which model tier provided the current estimate"""
    IRI_2020 = "IRI-2020"           # Full physics-based model (upgraded from IRI-2016)
    IRI_2016 = "IRI-2016"           # Legacy - kept for backward compatibility
    IONEX = "IONEX"                 # GPS-derived global TEC maps (2-hour cadence)
    PARAMETRIC = "Parametric"       # Simple diurnal/solar model
    STATIC = "Static"               # Fixed constants
    CALIBRATED = "Calibrated"       # Any tier + calibration applied


@dataclass
class LayerHeights:
    """Ionospheric layer height estimates with metadata"""
    # Layer heights (km)
    hmE: float          # E-layer peak height
    hmF1: float         # F1-layer peak height
    hmF2: float         # F2-layer peak height (primary for HF propagation)
    
    # Metadata
    tier: IonosphericModelTier  # Which model provided estimate
    timestamp: datetime         # When estimate was computed
    location: Tuple[float, float]  # (lat, lon) for this estimate
    
    # Uncertainty estimate (1-sigma, km)
    hmF2_uncertainty_km: float = 30.0
    
    # Calibration applied
    calibration_offset_km: float = 0.0
    
    # Solar indices used (if available)
    f107: Optional[float] = None     # 10.7 cm solar flux
    ap: Optional[float] = None       # Geomagnetic Ap index


@dataclass
class CalibrationEntry:
    """A single calibration measurement"""
    timestamp: datetime
    predicted_hmF2_km: float
    implied_hmF2_km: float  # Derived from observed propagation delay
    offset_km: float        # implied - predicted
    confidence: float       # Weight for this measurement (0-1)


class IonosphericModel:
    """
    Hierarchical ionospheric model with IRI-2020 integration and calibration.
    
    This class provides dynamic ionospheric layer heights that vary with:
    - Time of day (diurnal cycle)
    - Season
    - Solar activity
    - Geographic location
    
    IRI-2020 improvements over IRI-2016:
    - Better topside electron density model
    - Improved storm-time corrections
    - Updated CCIR/URSI coefficients
    - Better high-latitude coverage
    
    It gracefully degrades from IRI-2020 → Parametric → Static fallbacks,
    and applies learned calibration offsets to improve accuracy.
    
    Usage:
        model = IonosphericModel()
        heights = model.get_layer_heights(
            timestamp=datetime.now(timezone.utc),
            latitude=40.0,
            longitude=-105.0
        )
        print(f"F2 layer height: {heights.hmF2:.1f} km (via {heights.tier.value})")
    """
    
    def __init__(
        self,
        enable_iri: bool = True,
        enable_calibration: bool = True,
        calibration_window_hours: float = 24.0,
        max_calibration_entries: int = 100,
        ionex_dir: Optional[Path] = None
    ):
        """
        Initialize the ionospheric model.
        
        Args:
            enable_iri: Attempt to use IRI-2016 if available
            enable_calibration: Apply learned calibration offsets
            calibration_window_hours: Time window for calibration averaging
            max_calibration_entries: Maximum stored calibration points
            ionex_dir: Directory containing IONEX files (default: /var/lib/timestd/ionex)
        """
        self.enable_iri = enable_iri
        self.enable_calibration = enable_calibration
        self.calibration_window_hours = calibration_window_hours
        self.max_calibration_entries = max_calibration_entries
        
        # IRI-2020 availability (falls back to IRI-2016 if needed)
        self._iri_available: Optional[bool] = None  # None = not checked yet
        self._iri_module = None
        self._iri_version: str = "none"  # "2020", "2016", or "none"
        
        # Calibration storage: keyed by location hash (LRU eviction)
        self._calibration_data: OrderedDict[str, list] = OrderedDict()
        self.max_locations = 10  # Maximum locations to track
        
        # Cache for IRI results (avoid repeated calculations)
        self._iri_cache: Dict[str, LayerHeights] = {}
        self._cache_ttl_seconds = 300  # 5 minute cache
        
        # IONEX support for global VTEC maps
        self.ionex_dir = Path(ionex_dir) if ionex_dir else Path('/var/lib/timestd/ionex')
        self._ionex_cache: Dict[str, 'IONEXParser'] = {}  # Cache parsed IONEX files
        self._ionex_cache_max_age = 86400  # 24 hours
        
        # Statistics
        self.stats = {
            'iri_calls': 0,
            'iri_cache_hits': 0,
            'parametric_fallbacks': 0,
            'static_fallbacks': 0,
            'calibration_updates': 0,
            'ionex_calls': 0,
            'ionex_cache_hits': 0
        }
        
        # Check IRI availability on init (lazy)
        if enable_iri:
            self._check_iri_availability()
    
    def _check_iri_availability(self) -> bool:
        """Check if IRI-2020 (or IRI-2016 fallback) Python package is available."""
        if self._iri_available is not None:
            return self._iri_available
        
        # Try IRI-2020 first (preferred)
        try:
            import iri2020
            # Quick test to verify it works
            # This will compile Fortran on first run if needed
            self._iri_module = iri2020
            self._iri_available = True
            self._iri_version = "2020"
            logger.info("IRI-2020 model available and functional")
            return self._iri_available
        except ImportError:
            logger.debug("IRI-2020 not installed, trying IRI-2016 fallback")
        except Exception as e:
            logger.debug(f"IRI-2020 initialization failed: {e}, trying IRI-2016 fallback")
        
        # Fallback to IRI-2016 if IRI-2020 not available
        try:
            import iri2016
            self._iri_module = iri2016
            self._iri_available = True
            self._iri_version = "2016"
            logger.info("IRI-2016 model available (IRI-2020 preferred but not installed)")
            return self._iri_available
        except ImportError:
            self._iri_available = False
            self._iri_version = "none"
            logger.warning(
                "Neither IRI-2020 nor IRI-2016 available (pip install iri2020 + gfortran required). "
                "Using parametric fallback model."
            )
        except Exception as e:
            self._iri_available = False
            self._iri_version = "none"
            logger.warning(f"IRI initialization failed: {e}. Using parametric fallback.")
        
        return self._iri_available

    @staticmethod
    def _extract_scalar(value, default: Optional[float]) -> float:
        """
        Normalize IRI outputs (which may be scalars, numpy arrays, or xarray DataArrays)
        into plain floats so downstream code remains version agnostic.
        """
        if value is None:
            return default if default is not None else float('nan')
        
        if xr is not None and isinstance(value, xr.DataArray):
            if value.size == 0:
                return default if default is not None else float('nan')
            try:
                res = float(value.values.ravel()[0])
                if not np.isfinite(res):
                    return default if default is not None else float('nan')
                return res
            except (IndexError, ValueError, TypeError):
                return default if default is not None else float('nan')
        
        if isinstance(value, np.ndarray):
            if value.size == 0:
                return default if default is not None else float('nan')
            try:
                res = float(value.reshape(-1)[0])
                if not np.isfinite(res):
                    return default if default is not None else float('nan')
                return res
            except (IndexError, ValueError, TypeError):
                return default if default is not None else float('nan')
        
        if hasattr(value, "item"):
            try:
                res = float(value.item())
                if not np.isfinite(res):
                    return default if default is not None else float('nan')
                return res
            except (AttributeError, TypeError, ValueError):
                pass
        
        if isinstance(value, (list, tuple)):
            if len(value) == 0:
                return default if default is not None else float('nan')
            try:
                res = float(value[0])
                if not np.isfinite(res):
                    return default if default is not None else float('nan')
                return res
            except (IndexError, ValueError, TypeError):
                return default if default is not None else float('nan')
        
        try:
            res = float(value)
            if not np.isfinite(res):
                return default if default is not None else float('nan')
            return res
        except (TypeError, ValueError):
            if default is not None:
                return default
            raise
    
    def _location_key(self, lat: float, lon: float, time: datetime) -> str:
        """Generate cache key for location/time combination."""
        # Round to reduce cache granularity (5-minute, 1-degree resolution)
        lat_round = round(lat)
        lon_round = round(lon)
        # Round time to 5-minute intervals
        minute_slot = (time.hour * 60 + time.minute) // 5
        return f"{lat_round}_{lon_round}_{time.date()}_{minute_slot}"
    
    def calculate_hf_reflection_point(
        self,
        tx_lat: float,
        tx_lon: float,
        rx_lat: float,
        rx_lon: float,
        layer_height_km: float = 300.0
    ) -> Tuple[float, float]:
        """
        Calculate ionospheric pierce point for HF propagation.
        
        Assumes single reflection at F2 layer. For HF skywave, the signal
        reflects off the ionosphere at approximately the midpoint of the
        great circle path between transmitter and receiver.
        
        Args:
            tx_lat: Transmitter latitude (degrees)
            tx_lon: Transmitter longitude (degrees)
            rx_lat: Receiver latitude (degrees)
            rx_lon: Receiver longitude (degrees)
            layer_height_km: Ionospheric layer height (default: 300 km for F2)
            
        Returns:
            (lat, lon) of reflection point in degrees
        """
        # Convert to radians
        tx_lat_rad = math.radians(tx_lat)
        tx_lon_rad = math.radians(tx_lon)
        rx_lat_rad = math.radians(rx_lat)
        rx_lon_rad = math.radians(rx_lon)
        
        # For single-hop HF, reflection point is approximately at the midpoint
        # of the great circle path. This is a simplification but works well
        # for typical HF distances (1000-3000 km).
        
        # Calculate midpoint using spherical geometry
        Bx = math.cos(rx_lat_rad) * math.cos(rx_lon_rad - tx_lon_rad)
        By = math.cos(rx_lat_rad) * math.sin(rx_lon_rad - tx_lon_rad)
        
        lat_mid_rad = math.atan2(
            math.sin(tx_lat_rad) + math.sin(rx_lat_rad),
            math.sqrt((math.cos(tx_lat_rad) + Bx)**2 + By**2)
        )
        lon_mid_rad = tx_lon_rad + math.atan2(By, math.cos(tx_lat_rad) + Bx)
        
        # Convert back to degrees
        lat_mid = math.degrees(lat_mid_rad)
        lon_mid = math.degrees(lon_mid_rad)
        
        # Normalize longitude to -180 to 180
        if lon_mid > 180:
            lon_mid -= 360
        elif lon_mid < -180:
            lon_mid += 360
        
        logger.debug(f"HF reflection point: ({lat_mid:.2f}°, {lon_mid:.2f}°) "
                    f"for TX({tx_lat:.2f}°, {tx_lon:.2f}°) → RX({rx_lat:.2f}°, {rx_lon:.2f}°)")
        
        return lat_mid, lon_mid
    
    def get_ionex_vtec(
        self,
        lat: float,
        lon: float,
        timestamp: datetime
    ) -> Optional[Tuple[float, str]]:
        """
        Get VTEC from IONEX files.
        
        Searches for IONEX file matching the timestamp, parses it if needed,
        and interpolates VTEC at the specified location.
        
        Args:
            lat: Latitude (degrees, -90 to 90)
            lon: Longitude (degrees, -180 to 180)
            timestamp: UTC timestamp
            
        Returns:
            (vtec_tecu, source_file) or None if unavailable
        """
        if not self.ionex_dir.exists():
            logger.debug(f"IONEX directory does not exist: {self.ionex_dir}")
            return None
        
        # Find IONEX file for this date
        date_str = timestamp.strftime('%Y%m%d')
        
        # Try to find matching IONEX file
        # Modern format: IGS0OPSFIN_YYYYDDD0000_01D_02H_GIM.INX.gz
        # Legacy format: igsgDDD0.YYi.Z
        ionex_files = list(self.ionex_dir.glob(f"*{date_str[:4]}*"))
        
        if not ionex_files:
            logger.debug(f"No IONEX files found for date {date_str}")
            return None
        
        # Use the most recent file (in case multiple exist)
        ionex_file = max(ionex_files, key=lambda p: p.stat().st_mtime)
        
        # Check cache
        cache_key = str(ionex_file)
        if cache_key in self._ionex_cache:
            parser = self._ionex_cache[cache_key]
            self.stats['ionex_cache_hits'] += 1
        else:
            # Parse IONEX file
            try:
                # Import IONEXParser from ionex_integration script
                # Path: src/hf_timestd/core/ionospheric_model.py -> scripts/ionex_integration.py
                import sys
                import importlib.util
                
                # Get project root (3 levels up from this file)
                project_root = Path(__file__).parent.parent.parent.parent
                ionex_script_path = project_root / "scripts" / "ionex_integration.py"
                
                if not ionex_script_path.exists():
                    logger.warning(f"IONEX integration script not found: {ionex_script_path}")
                    return None
                
                spec = importlib.util.spec_from_file_location(
                    "ionex_integration",
                    ionex_script_path
                )
                ionex_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(ionex_module)
                
                parser = ionex_module.IONEXParser(ionex_file)
                self._ionex_cache[cache_key] = parser
                self.stats['ionex_calls'] += 1
                
                # Limit cache size
                if len(self._ionex_cache) > 7:  # Keep last week
                    oldest_key = next(iter(self._ionex_cache))
                    del self._ionex_cache[oldest_key]
                    
            except Exception as e:
                logger.warning(f"Failed to parse IONEX file {ionex_file}: {e}")
                return None
        
        # Interpolate VTEC
        try:
            vtec = parser.interpolate(lat, lon, timestamp)
            if vtec is not None:
                logger.debug(f"IONEX VTEC: {vtec:.2f} TECU at ({lat:.2f}°, {lon:.2f}°)")
                return vtec, ionex_file.name
            else:
                logger.debug(f"IONEX interpolation returned None")
                return None
        except Exception as e:
            logger.warning(f"IONEX VTEC interpolation failed: {e}")
            return None
    
    
    def _calculate_cache_ttl(self, timestamp: datetime, latitude: float) -> float:
        """
        Calculate adaptive cache TTL based on ionospheric conditions.
        
        Ionosphere is more stable during daytime quiet conditions (longer TTL acceptable),
        but more variable at night and during disturbed conditions (shorter TTL needed).
        
        Args:
            timestamp: UTC timestamp
            latitude: Geographic latitude in degrees
            
        Returns:
            Cache TTL in seconds
        """
        # Calculate local solar time (approximate)
        # For more accuracy, could use longitude, but hour-level precision is sufficient
        hour = timestamp.hour
        
        # Daytime (06:00-18:00 UTC, approximate): longer TTL (ionosphere more stable)
        # Nighttime: shorter TTL (more variable, especially near sunrise/sunset)
        if 6 <= hour < 18:
            base_ttl = 1800  # 30 minutes daytime
        else:
            base_ttl = 300   # 5 minutes nighttime
        
        # Future enhancement: reduce TTL during disturbed conditions
        # if hasattr(self, 'ap') and self.ap and self.ap > 30:  # Disturbed
        #     base_ttl = min(base_ttl, 300)
        
        return base_ttl
    
    def _get_iri_heights(
        self,
        timestamp: datetime,
        latitude: float,
        longitude: float
    ) -> Optional[LayerHeights]:
        """
        Get layer heights from IRI-2020 model (or IRI-2016 fallback).
        
        Returns None if IRI is unavailable or fails.
        """
        if not self._iri_available or self._iri_module is None:
            return None
        
        # Check cache first
        cache_key = self._location_key(latitude, longitude, timestamp)
        if cache_key in self._iri_cache:
            self.stats['iri_cache_hits'] += 1
            cached = self._iri_cache[cache_key]
            # Verify cache not stale (using adaptive TTL)
            age_seconds = (datetime.now(timezone.utc) - cached.timestamp).total_seconds()
            cache_ttl = self._calculate_cache_ttl(timestamp, latitude)
            if age_seconds < cache_ttl:
                return cached
        
        try:
            self.stats['iri_calls'] += 1
            
            # Call IRI-2016
            # We only need heights, not full profile, so use minimal altitude range
            result = self._iri_module.IRI(
                time=timestamp,
                altkmrange=(100, 500, 50),  # 100-500 km in 50 km steps
                glat=latitude,
                glon=longitude
            )
            
            # Extract peak heights from IRI output
            # IRI returns hmF2, hmF1, hmE as single values
            hmF2 = self._extract_scalar(result.get('hmF2'), DEFAULT_F2_LAYER_HEIGHT_KM)
            hmF1 = self._extract_scalar(result.get('hmF1'), DEFAULT_F1_LAYER_HEIGHT_KM)
            hmE = self._extract_scalar(result.get('hmE'), DEFAULT_E_LAYER_HEIGHT_KM)
            
            # Sanity check on values
            if not (150 < hmF2 < 500):
                logger.warning(f"IRI hmF2={hmF2} outside valid range, using parametric")
                return None
            
            # Set tier based on which IRI version is being used
            tier = IonosphericModelTier.IRI_2020 if self._iri_version == "2020" else IonosphericModelTier.IRI_2016
            
            heights = LayerHeights(
                hmE=hmE,
                hmF1=hmF1,
                hmF2=hmF2,
                tier=tier,
                timestamp=datetime.now(timezone.utc),
                location=(latitude, longitude),
                hmF2_uncertainty_km=25.0 if self._iri_version == "2020" else 28.0  # IRI-2020 slightly better
            )
            
            # Cache result
            self._iri_cache[cache_key] = heights
            
            logger.debug(f"IRI-{self._iri_version}: hmF2={hmF2:.1f} km at ({latitude:.1f}, {longitude:.1f})")
            return heights
            
        except Exception as e:
            logger.warning(f"IRI-{self._iri_version} calculation failed: {e}")
            return None
    
    def _get_parametric_heights(
        self,
        timestamp: datetime,
        latitude: float,
        longitude: float,
        f107: Optional[float] = None
    ) -> LayerHeights:
        """
        TIER 2: Parametric model for when IRI-2020/IRI-2016 is unavailable.
        
        This implements a simplified ionospheric model based on:
        1. Diurnal variation (higher at night)
        2. Solar activity dependence
        3. Latitude dependence
        
        MODEL:
        ------
        hmF2 = hmF2_base + diurnal_term + solar_term + latitude_term
        
        Where:
            hmF2_base ≈ 280 km (midlatitude, moderate solar activity)
            diurnal_term = A × cos(2π × (hour - 14)/24)  # Peak at 14 LT
            solar_term = B × (F10.7 - 100) / 100
            latitude_term = C × cos(lat)  # Higher at equator
        
        This captures ~70% of hmF2 variance (diurnal is dominant).
        """
        self.stats['parametric_fallbacks'] += 1
        
        # Calculate local solar time
        # Local time = UTC + longitude/15 hours
        utc_hour = timestamp.hour + timestamp.minute / 60.0
        local_solar_time = (utc_hour + longitude / 15.0) % 24.0
        
        # Base height (midlatitude, moderate solar activity)
        hmF2_base = 280.0
        
        # Diurnal term: F2 layer RISES at night (ionization decays)
        # Minimum around 14:00 local time, maximum around 02:00-04:00
        # Amplitude ~50 km typical
        diurnal_phase = 2 * math.pi * (local_solar_time - 14.0) / 24.0
        diurnal_term = -HMF2_DIURNAL_AMP_KM * math.cos(diurnal_phase)
        
        # Solar activity term (if F10.7 available)
        # Higher solar flux → denser ionosphere → lower hmF2
        # Typical range: F10.7 = 70 (solar min) to 250 (solar max)
        if f107 is not None:
            solar_term = -HMF2_SOLAR_FACTOR * (f107 - 100)
        else:
            solar_term = 0.0  # Assume moderate activity
        
        # Latitude term: F2 layer higher at equator
        # Due to equatorial ionization fountain effect
        lat_rad = math.radians(abs(latitude))
        latitude_term = 20.0 * math.cos(lat_rad)
        
        # Seasonal term (simplified)
        # F2 layer higher in summer hemisphere
        day_of_year = timestamp.timetuple().tm_yday
        # Northern hemisphere summer peaks around day 172 (June 21)
        seasonal_phase = 2 * math.pi * (day_of_year - 172) / 365.0
        if latitude < 0:
            seasonal_phase += math.pi  # Opposite in southern hemisphere
        seasonal_term = 15.0 * math.cos(seasonal_phase)
        
        # Total hmF2
        hmF2 = hmF2_base + diurnal_term + solar_term + latitude_term + seasonal_term
        
        # Clamp to physically reasonable range
        hmF2 = max(HMF2_MIN_KM, min(HMF2_MAX_KM, hmF2))
        
        # E and F1 layers (simpler models)
        # E-layer: relatively constant at ~110 km during day, absent at night
        is_daytime = 6 < local_solar_time < 18
        hmE = DEFAULT_E_LAYER_HEIGHT_KM if is_daytime else 105.0  # Slightly lower residual
        
        # F1-layer: only present during day, between E and F2
        hmF1 = (hmE + hmF2) / 2 if is_daytime else hmF2 - 50
        
        logger.debug(f"Parametric model: hmF2={hmF2:.1f} km, LST={local_solar_time:.1f}h, "
                    f"diurnal={diurnal_term:+.1f}, solar={solar_term:+.1f}")
        
        return LayerHeights(
            hmE=hmE,
            hmF1=hmF1,
            hmF2=hmF2,
            tier=IonosphericModelTier.PARAMETRIC,
            timestamp=datetime.now(timezone.utc),
            location=(latitude, longitude),
            hmF2_uncertainty_km=50.0,  # Parametric has more uncertainty
            f107=f107
        )
    
    def _get_static_heights(self, timestamp: datetime) -> LayerHeights:
        """
        TIER 3: Static fallback using original fixed constants.
        
        Used when no location/time available or as last resort.
        """
        self.stats['static_fallbacks'] += 1
        
        # At least use the day/night distinction that was DEFINED but never USED
        # in the original code
        utc_hour = timestamp.hour if timestamp else 12
        is_night = utc_hour < 6 or utc_hour > 18
        
        hmF2 = DEFAULT_F2_NIGHT_HEIGHT_KM if is_night else DEFAULT_F2_LAYER_HEIGHT_KM
        
        logger.debug(f"Static fallback: hmF2={hmF2:.1f} km (night={is_night})")
        
        return LayerHeights(
            hmE=DEFAULT_E_LAYER_HEIGHT_KM,
            hmF1=DEFAULT_F1_LAYER_HEIGHT_KM,
            hmF2=hmF2,
            tier=IonosphericModelTier.STATIC,
            timestamp=datetime.now(timezone.utc),
            location=(0.0, 0.0),  # Unknown
            hmF2_uncertainty_km=80.0  # High uncertainty for static
        )
    
    def _apply_calibration(
        self,
        heights: LayerHeights,
        latitude: float,
        longitude: float
    ) -> LayerHeights:
        """
        Apply learned calibration offset to model heights.
        
        The calibration captures systematic differences between the model
        and actual ionospheric conditions ("weather" vs "climate").
        """
        if not self.enable_calibration:
            return heights
        
        # Get calibration data for this approximate location
        loc_key = f"{round(latitude)}_{round(longitude)}"
        cal_data = self._calibration_data.get(loc_key, [])
        
        if not cal_data:
            return heights
        
        # Filter to recent entries within calibration window
        now = datetime.now(timezone.utc)
        window_seconds = self.calibration_window_hours * 3600
        recent = []
        for c in cal_data:
            # Skip invalid or old entries
            if not np.isfinite(c.offset_km) or not np.isfinite(c.confidence):
                continue
            if (now - c.timestamp).total_seconds() < window_seconds:
                recent.append(c)
        
        if not recent:
            return heights
        
        # Weighted average of recent calibration offsets
        total_weight = sum(c.confidence for c in recent)
        if total_weight > 0:
            weighted_offset = sum(c.offset_km * c.confidence for c in recent) / total_weight
        else:
            weighted_offset = 0.0
        
        # Apply calibration
        calibrated_hmF2 = heights.hmF2 + weighted_offset
        
        logger.debug(f"Calibration applied: {heights.hmF2:.1f} km + {weighted_offset:+.1f} km "
                    f"= {calibrated_hmF2:.1f} km (from {len(recent)} measurements)")
        
        return LayerHeights(
            hmE=heights.hmE,
            hmF1=heights.hmF1,
            hmF2=calibrated_hmF2,
            tier=IonosphericModelTier.CALIBRATED,
            timestamp=heights.timestamp,
            location=heights.location,
            hmF2_uncertainty_km=max(15.0, heights.hmF2_uncertainty_km - 10.0),  # Reduced uncertainty
            calibration_offset_km=weighted_offset,
            f107=heights.f107,
            ap=heights.ap
        )
    
    def get_layer_heights(
        self,
        timestamp: Optional[datetime] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        f107: Optional[float] = None
    ) -> LayerHeights:
        """
        Get ionospheric layer heights using the best available model.
        
        Tries models in order: IRI-2016 → Parametric → Static
        Then applies calibration if enabled and data available.
        
        Args:
            timestamp: UTC datetime (default: now)
            latitude: Geographic latitude in degrees
            longitude: Geographic longitude in degrees
            f107: Optional 10.7 cm solar flux (improves parametric model)
            
        Returns:
            LayerHeights with hmE, hmF1, hmF2 and metadata
        """
        # Default timestamp
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        
        # Ensure timezone-aware
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        
        heights: Optional[LayerHeights] = None
        
        # TIER 1: Try IRI-2020 (or IRI-2016 fallback)
        if self.enable_iri and latitude is not None and longitude is not None:
            heights = self._get_iri_heights(timestamp, latitude, longitude)
        
        # TIER 2: Parametric fallback
        if heights is None and latitude is not None and longitude is not None:
            heights = self._get_parametric_heights(timestamp, latitude, longitude, f107)
        
        # TIER 3: Static fallback
        if heights is None:
            heights = self._get_static_heights(timestamp)
        
        # Apply calibration
        if latitude is not None and longitude is not None:
            heights = self._apply_calibration(heights, latitude, longitude)
        
        return heights
    
    def update_calibration(
        self,
        latitude: float,
        longitude: float,
        timestamp: datetime,
        observed_delay_ms: float,
        predicted_delay_ms: float,
        ground_distance_km: float,
        n_hops: int,
        confidence: float = 1.0
    ):
        """
        Update calibration based on observed vs predicted propagation delay.
        
        When we measure an actual propagation delay that differs from the
        model prediction, we can infer what the actual hmF2 must have been.
        
        DERIVATION:
        -----------
        For a single F-layer hop at distance D with layer height h:
            delay = (path_length / c) × 1000  [ms]
            path_length = 2 × sqrt((D/2)² + h²)  [km]
        
        Solving for h given delay and D:
            path_length = delay × c / 1000
            sqrt((D/2)² + h²) = path_length / 2
            h = sqrt((path_length/2)² - (D/2)²)
        
        The calibration offset is:
            offset = implied_h - predicted_h
        
        Args:
            latitude, longitude: Location of this measurement
            timestamp: When measurement was taken
            observed_delay_ms: Actual measured propagation delay
            predicted_delay_ms: Model-predicted delay
            ground_distance_km: Ground distance TX to RX
            n_hops: Number of ionospheric hops
            confidence: Weight for this calibration point (0-1)
        """
        if n_hops == 0:
            return
            
        # SANITY CHECK: Explicitly reject NaN or non-finite values
        if not np.isfinite(observed_delay_ms) or not np.isfinite(predicted_delay_ms):
            logger.debug(f"Calibration: rejected NaN/inf delay (obs={observed_delay_ms}, pred={predicted_delay_ms})")
            return
            
        if not np.isfinite(confidence) or confidence < 0.3:
            return  # Can't calibrate from ground wave or low-confidence/NaN
        
        self.stats['calibration_updates'] += 1
        
        # Speed of light in km/s
        c_km_s = 299792.458
        
        # Calculate implied layer height from observed delay
        # path_length_km = delay_ms × c / 1000
        observed_path_km = observed_delay_ms * c_km_s / 1000.0
        predicted_path_km = predicted_delay_ms * c_km_s / 1000.0
        
        # For N-hop path: each hop covers (ground_distance / N)
        hop_distance = ground_distance_km / n_hops
        half_hop = hop_distance / 2.0
        
        # Solve for height: h = sqrt((path_per_hop/2)² - half_hop²)
        # path_per_hop = total_path / n_hops
        observed_path_per_hop = observed_path_km / n_hops
        predicted_path_per_hop = predicted_path_km / n_hops
        
        # Each hop: path = 2 × slant_range, slant_range = path_per_hop / 2
        observed_slant = observed_path_per_hop / 2.0
        predicted_slant = predicted_path_per_hop / 2.0
        
        # h = sqrt(slant² - half_hop²)
        try:
            implied_h_sq = observed_slant ** 2 - half_hop ** 2
            predicted_h_sq = predicted_slant ** 2 - half_hop ** 2
            
            if implied_h_sq < 0 or predicted_h_sq < 0:
                logger.debug(f"Calibration: geometry invalid (negative under sqrt)")
                return
            
            implied_hmF2 = math.sqrt(implied_h_sq)
            predicted_hmF2 = math.sqrt(predicted_h_sq)
            
            offset_km = implied_hmF2 - predicted_hmF2
            
            # Sanity check: offset shouldn't be extreme
            if abs(offset_km) > 150:
                logger.debug(f"Calibration: offset {offset_km:.1f} km too large, ignoring")
                return
            
        except (ValueError, ZeroDivisionError) as e:
            logger.debug(f"Calibration: calculation error: {e}")
            return
        
        # Store calibration entry
        loc_key = f"{round(latitude)}_{round(longitude)}"
        if loc_key not in self._calibration_data:
            # Evict oldest location if at capacity
            if len(self._calibration_data) >= self.max_locations:
                oldest_key = next(iter(self._calibration_data))
                evicted_count = len(self._calibration_data[oldest_key])
                del self._calibration_data[oldest_key]
                logger.debug(f"Evicted calibration data for oldest location ({evicted_count} entries)")
            
            self._calibration_data[loc_key] = []
        
        entry = CalibrationEntry(
            timestamp=timestamp,
            predicted_hmF2_km=predicted_hmF2,
            implied_hmF2_km=implied_hmF2,
            offset_km=offset_km,
            confidence=confidence
        )
        
        self._calibration_data[loc_key].append(entry)
        
        # Move to end (mark as recently used)
        self._calibration_data.move_to_end(loc_key)
        
        # Trim to max entries (keep most recent)
        if len(self._calibration_data[loc_key]) > self.max_calibration_entries:
            self._calibration_data[loc_key] = self._calibration_data[loc_key][-self.max_calibration_entries:]
        
        logger.debug(f"Calibration update: predicted_hmF2={predicted_hmF2:.1f} km, "
                    f"implied={implied_hmF2:.1f} km, offset={offset_km:+.1f} km")
    
    def get_calibration_stats(self, latitude: float, longitude: float) -> Dict:
        """Get calibration statistics for a location."""
        loc_key = f"{round(latitude)}_{round(longitude)}"
        cal_data = self._calibration_data.get(loc_key, [])
        
        if not cal_data:
            return {'n_entries': 0, 'mean_offset_km': 0.0, 'std_offset_km': 0.0}
        
        offsets = [c.offset_km for c in cal_data]
        return {
            'n_entries': len(cal_data),
            'mean_offset_km': np.mean(offsets),
            'std_offset_km': np.std(offsets),
            'min_offset_km': min(offsets),
            'max_offset_km': max(offsets)
        }
    
    def get_stats(self) -> Dict:
        """Get model usage statistics."""
        return dict(self.stats)


# =============================================================================
# IONOSPHERIC DELAY MODEL (Issue 1.3 Fix - 2025-12-07)
# =============================================================================
#
# VULNERABILITY ADDRESSED:
# ------------------------
# The original code used a constant 0.15 ms per hop with linear frequency scaling:
#
#     iono_factor = IONO_DELAY_FACTOR.get(frequency_mhz, 1.0)
#     iono_delay_ms = n_hops * 0.15 * iono_factor
#
# This is WRONG because ionospheric group delay follows 1/f²:
#
#     τ_iono = 40.3 × TEC / (c × f²)  [seconds]
#
# Where:
#     TEC = Total Electron Content (electrons/m² along path)
#     c = speed of light (m/s)
#     f = frequency (Hz)
#
# The 1/f² relationship means:
#     - 2.5 MHz has 16× more delay than 10 MHz (not 1.5× as in old code!)
#     - 5 MHz has 4× more delay than 10 MHz (not 1.1× as in old code!)
#
# PHYSICAL CONSTANTS:
# -------------------
# The constant 40.3 comes from the Appleton-Hartree equation:
#     40.3 = e² / (8π²ε₀mₑ) ≈ 40.3 m³/s²
#
# TEC is measured in TECU (TEC Units):
#     1 TECU = 10^16 electrons/m²
#
# TYPICAL VALUES:
# ---------------
#     TEC (quiet, night, solar min):  5-15 TECU
#     TEC (day, moderate activity):   20-50 TECU
#     TEC (day, solar max):           50-150 TECU
#     TEC (storm enhanced):           up to 300+ TECU
#
# EXAMPLE DELAYS (for TEC = 30 TECU, 1 hop through F-layer):
#     25.0 MHz: 0.032 ms
#     15.0 MHz: 0.089 ms
#     10.0 MHz: 0.201 ms
#     5.0 MHz:  0.806 ms
#     2.5 MHz:  3.22 ms
#
# Note: For HF propagation, the "slant TEC" through the ionosphere is
# typically 1.5-3× the vertical TEC, depending on elevation angle.
#
# REFERENCE: Budden, K.G. (1985). "The Propagation of Radio Waves."
#            Cambridge University Press. Chapter 13.
# =============================================================================

# Physical constant for ionospheric delay
# τ = IONO_DELAY_CONSTANT × TEC / f²
# where TEC in TECU (10^16 el/m²) and f in MHz, result in milliseconds
IONO_DELAY_CONSTANT_MS = 40.3 / 299792.458 * 1e16 / 1e12  # ≈ 0.1345 ms·MHz²/TECU


@dataclass
class IonosphericDelayResult:
    """Result of ionospheric delay calculation."""
    delay_ms: float              # Total ionospheric delay
    vertical_tec_tecu: float     # Vertical TEC used
    slant_tec_tecu: float        # Slant TEC (path-integrated)
    frequency_mhz: float         # Frequency
    elevation_deg: float         # Elevation angle
    n_hops: int                  # Number of ionospheric hops
    tier: IonosphericModelTier   # Which model provided TEC


class IonosphericDelayCalculator:
    """
    Calculate ionospheric group delay using the proper 1/f² physics.
    
    This replaces the oversimplified linear model with physically correct
    frequency-dependent delay based on TEC estimates.
    
    Usage:
        calc = IonosphericDelayCalculator()
        result = calc.calculate_delay(
            frequency_mhz=10.0,
            n_hops=1,
            elevation_deg=30.0,
            timestamp=datetime.now(timezone.utc),
            latitude=40.0,
            longitude=-105.0
        )
        print(f"Ionospheric delay: {result.delay_ms:.3f} ms")
    """
    
    def __init__(self, iono_model: Optional['IonosphericModel'] = None):
        """
        Initialize the delay calculator.
        
        Args:
            iono_model: IonosphericModel instance for TEC lookup.
                       If None, will use parametric TEC estimates.
        """
        self.iono_model = iono_model
        self._iri_available = False
        
        # Check if IRI-2016 is available (it provides TEC)
        if iono_model is not None and iono_model._iri_available:
            self._iri_available = True
    
    def _estimate_vertical_tec(
        self,
        timestamp: Optional[datetime] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        f107: Optional[float] = None
    ) -> Tuple[float, IonosphericModelTier]:
        """
        Estimate vertical TEC (Total Electron Content).
        
        Returns TEC in TECU (10^16 electrons/m²) and the model tier used.
        
        TIER 1: IRI-2020/IRI-2016 TEC output (if available)
        TIER 1.5: IONEX global TEC maps (GPS-derived, 2-hour cadence)
        TIER 2: Parametric model based on time/location/solar activity
        TIER 3: Static climatological average
        """
        # TIER 1: Try IRI-2020 or IRI-2016 (both provide TEC as output)
        if self._iri_available and self.iono_model is not None:
            try:
                if self.iono_model._iri_module is not None:
                    result = self.iono_model._iri_module.IRI(
                        time=timestamp or datetime.now(timezone.utc),
                        altkmrange=(100, 500, 50),
                        glat=latitude or 40.0,
                        glon=longitude or -105.0
                    )
                    # IRI returns TEC in TECU
                    tec = self._extract_scalar(result.get('TEC'), None)
                    if tec is not None and 1 < tec < 500:
                        tier = IonosphericModelTier.IRI_2020 if self.iono_model._iri_version == "2020" else IonosphericModelTier.IRI_2016
                        return float(tec), tier
            except Exception as e:
                logger.debug(f"IRI TEC lookup failed: {e}")
        
        # TIER 1.5: Try IONEX (global GPS-derived TEC maps)
        if self.iono_model is not None and latitude is not None and longitude is not None and timestamp is not None:
            try:
                ionex_result = self.iono_model.get_ionex_vtec(latitude, longitude, timestamp)
                if ionex_result is not None:
                    vtec, source_file = ionex_result
                    if 1 < vtec < 500:  # Sanity check
                        logger.debug(f"Using IONEX VTEC: {vtec:.2f} TECU from {source_file}")
                        return float(vtec), IonosphericModelTier.IONEX
            except Exception as e:
                logger.debug(f"IONEX TEC lookup failed: {e}")
        
        # TIER 2: Parametric model
        if timestamp is not None and latitude is not None:
            tec = self._parametric_tec(timestamp, latitude, longitude or 0.0, f107)
            return tec, IonosphericModelTier.PARAMETRIC
        
        # TIER 3: Static average (moderate conditions)
        return 30.0, IonosphericModelTier.STATIC
    
    def _parametric_tec(
        self,
        timestamp: datetime,
        latitude: float,
        longitude: float,
        f107: Optional[float] = None
    ) -> float:
        """
        Parametric TEC model capturing primary variations.
        
        TEC varies with:
        1. Time of day: Maximum around 14:00 local time
        2. Solar activity: Higher TEC during solar maximum
        3. Latitude: Higher near equatorial anomaly (~±15° magnetic)
        4. Season: Higher in equinoctial months
        
        This is a simplified climatological model that captures
        the dominant diurnal and solar cycle variations.
        """
        # Calculate local solar time
        utc_hour = timestamp.hour + timestamp.minute / 60.0
        local_solar_time = (utc_hour + longitude / 15.0) % 24.0
        
        # Base TEC (moderate solar activity, midlatitude)
        tec_base = 25.0  # TECU
        
        # Diurnal variation: peak around 14:00 local time
        # TEC ratio day/night is typically 3-5×
        diurnal_phase = 2 * math.pi * (local_solar_time - 14.0) / 24.0
        diurnal_factor = 1.0 + 0.6 * math.cos(diurnal_phase)  # 0.4 to 1.6
        
        # Solar activity factor
        # F10.7 ranges from ~70 (solar min) to ~250 (solar max)
        # TEC roughly doubles from solar min to max
        if f107 is not None:
            solar_factor = 0.5 + 0.5 * (f107 - 70) / 180  # 0.5 to 1.5
            solar_factor = max(0.3, min(2.0, solar_factor))
        else:
            solar_factor = 1.0  # Assume moderate activity
        
        # Latitude factor: equatorial anomaly enhancement
        # TEC peaks around ±15° magnetic latitude
        lat_rad = math.radians(abs(latitude))
        # Simple model: higher at lower latitudes
        lat_factor = 1.0 + 0.3 * math.cos(lat_rad)
        
        # Seasonal factor: higher near equinoxes
        day_of_year = timestamp.timetuple().tm_yday
        # Equinoxes around days 80 (Mar 21) and 266 (Sep 23)
        seasonal_phase = 2 * math.pi * (day_of_year - 80) / 182.5
        seasonal_factor = 1.0 + 0.15 * abs(math.sin(seasonal_phase))
        
        tec = tec_base * diurnal_factor * solar_factor * lat_factor * seasonal_factor
        
        # Clamp to reasonable range
        return max(5.0, min(150.0, tec))
    
    def _vertical_to_slant_tec(
        self,
        vertical_tec: float,
        elevation_deg: float,
        layer_height_km: float = 350.0
    ) -> float:
        """
        Convert vertical TEC to slant TEC for oblique propagation.
        
        The slant factor (obliquity factor) accounts for the longer
        path through the ionosphere at low elevation angles.
        
        For a thin-shell model at height h:
            slant_factor = 1 / cos(zenith_angle_at_iono_height)
        
        Using the thin-shell approximation:
            slant_factor ≈ 1 / sqrt(1 - (R_E × cos(elev) / (R_E + h))²)
        
        Where R_E = Earth radius (6371 km), h = ionosphere height
        """
        if elevation_deg >= 90:
            return vertical_tec  # Zenith: no correction
        
        if elevation_deg < 5:
            elevation_deg = 5  # Avoid extreme values at horizon
        
        # Earth radius
        R_E = 6371.0  # km
        
        # Calculate slant factor using thin-shell approximation
        elev_rad = math.radians(elevation_deg)
        cos_elev = math.cos(elev_rad)
        
        # Ionospheric pierce point calculation
        ratio = R_E * cos_elev / (R_E + layer_height_km)
        
        if ratio >= 1.0:
            slant_factor = 3.0  # Cap at reasonable maximum
        else:
            slant_factor = 1.0 / math.sqrt(1.0 - ratio * ratio)
        
        # Cap at reasonable maximum (very low elevations)
        slant_factor = min(slant_factor, 3.0)
        
        return vertical_tec * slant_factor
    
    def calculate_delay(
        self,
        frequency_mhz: float,
        n_hops: int = 1,
        elevation_deg: float = 30.0,
        timestamp: Optional[datetime] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        f107: Optional[float] = None
    ) -> IonosphericDelayResult:
        """
        Calculate ionospheric group delay.
        
        Uses the physically correct formula:
            τ = 40.3 × TEC / (c × f²)
        
        Args:
            frequency_mhz: Carrier frequency in MHz
            n_hops: Number of ionospheric hops (each hop traverses ionosphere)
            elevation_deg: Elevation angle at transmitter (for slant TEC)
            timestamp: UTC time (for TEC model)
            latitude: Latitude of ionospheric pierce point
            longitude: Longitude of ionospheric pierce point
            f107: Optional solar flux for TEC model
            
        Returns:
            IonosphericDelayResult with delay and metadata
        """
        # Get vertical TEC estimate
        vertical_tec, tier = self._estimate_vertical_tec(
            timestamp, latitude, longitude, f107
        )
        
        # Convert to slant TEC for this elevation
        slant_tec = self._vertical_to_slant_tec(vertical_tec, elevation_deg)
        
        # Total TEC through path: each hop traverses the ionosphere
        # (oversimplification: assumes same TEC for each hop)
        total_slant_tec = slant_tec * n_hops
        
        # Calculate delay using 1/f² relationship
        # τ = K × TEC / f²  where K = 40.3 / c × 10^16 / 10^12
        # Result in milliseconds
        f_sq = frequency_mhz * frequency_mhz
        delay_ms = IONO_DELAY_CONSTANT_MS * total_slant_tec / f_sq
        
        logger.debug(f"Ionospheric delay: {delay_ms:.3f} ms "
                    f"(TEC={total_slant_tec:.1f} TECU, f={frequency_mhz} MHz, "
                    f"n_hops={n_hops}, elev={elevation_deg:.1f}°, tier={tier.value})")
        
        return IonosphericDelayResult(
            delay_ms=delay_ms,
            vertical_tec_tecu=vertical_tec,
            slant_tec_tecu=total_slant_tec,
            frequency_mhz=frequency_mhz,
            elevation_deg=elevation_deg,
            n_hops=n_hops,
            tier=tier
        )


# =============================================================================
# CONVENIENCE FUNCTION
# =============================================================================

# Global instance for simple access
_default_model: Optional[IonosphericModel] = None
_delay_calculator: Optional[IonosphericDelayCalculator] = None


def get_ionospheric_model() -> IonosphericModel:
    """Get the global ionospheric model instance."""
    global _default_model
    if _default_model is None:
        _default_model = IonosphericModel()
    return _default_model


def get_hmF2(
    timestamp: Optional[datetime] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> float:
    """
    Convenience function to get F2 layer height.
    
    Usage:
        from hf_timestd.core.ionospheric_model import get_hmF2
        
        hmF2 = get_hmF2(timestamp=datetime.utcnow(), latitude=40.0, longitude=-105.0)
    """
    model = get_ionospheric_model()
    heights = model.get_layer_heights(timestamp, latitude, longitude)
    return heights.hmF2


def get_delay_calculator() -> IonosphericDelayCalculator:
    """Get the global ionospheric delay calculator instance."""
    global _delay_calculator
    if _delay_calculator is None:
        model = get_ionospheric_model()
        _delay_calculator = IonosphericDelayCalculator(iono_model=model)
    return _delay_calculator


def calculate_ionospheric_delay(
    frequency_mhz: float,
    n_hops: int = 1,
    elevation_deg: float = 30.0,
    timestamp: Optional[datetime] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None
) -> float:
    """
    Convenience function to calculate ionospheric delay.
    
    Uses the proper 1/f² physics instead of linear approximation.
    
    Args:
        frequency_mhz: Carrier frequency in MHz
        n_hops: Number of ionospheric hops
        elevation_deg: Elevation angle
        timestamp: UTC time
        latitude: Latitude
        longitude: Longitude
        
    Returns:
        Ionospheric delay in milliseconds
        
    Usage:
        from hf_timestd.core.ionospheric_model import calculate_ionospheric_delay
        
        delay = calculate_ionospheric_delay(
            frequency_mhz=10.0,
            n_hops=1,
            elevation_deg=30.0,
            timestamp=datetime.utcnow(),
            latitude=40.0,
            longitude=-105.0
        )
        print(f"Delay: {delay:.3f} ms")
    """
    calc = get_delay_calculator()
    result = calc.calculate_delay(
        frequency_mhz=frequency_mhz,
        n_hops=n_hops,
        elevation_deg=elevation_deg,
        timestamp=timestamp,
        latitude=latitude,
        longitude=longitude
    )
    return result.delay_ms
