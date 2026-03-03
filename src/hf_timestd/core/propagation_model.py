#!/usr/bin/env python3
"""
Propagation Model - Physics-Based HF Group Delay Prediction

================================================================================
PURPOSE
================================================================================

This module replaces the static vacuum speed-of-light propagation model with
a real-time ionospheric data-driven model. It computes HF group delay by
numerically integrating through a 1D electron density profile along the
signal path.

Key improvements over the previous model:
1. Frequency-dependent group delay (1/f² ionospheric term)
2. Time-varying predictions (tracks diurnal ionospheric changes)
3. Multi-hop arrival predictions (1F, 2F, 3F modes)
4. Adaptive uncertainty based on model confidence
5. Self-consistency checks via multi-frequency differential delay

================================================================================
PHYSICS
================================================================================

HF Group Delay:
    The group delay through the ionosphere is:
    
        τ_group = ∫ (n_g / c) ds
    
    where n_g is the group refractive index:
    
        n_g = 1 / sqrt(1 - (f_p/f)²)
    
    and f_p is the plasma frequency:
    
        f_p = sqrt(Ne * e² / (4π² * ε₀ * mₑ)) ≈ 8.98 * sqrt(Ne) Hz
    
    For f >> f_p (valid for HF above the MUF):
    
        n_g ≈ 1 + 0.5 * (f_p/f)²
    
    The excess group delay (beyond vacuum) is:
    
        Δτ = (40.3 / c) * ∫ Ne ds / f²  =  40.3 * sTEC / (c * f²)
    
    where sTEC is the slant Total Electron Content along the path.

Multi-Hop Propagation:
    For an N-hop path, the signal bounces N times off the ionosphere.
    Each hop has:
    - A geometric path length (spherical Earth + ionospheric layer)
    - An ionospheric group delay contribution
    
    Total delay = Σ (geometric_delay + iono_delay) for each hop

Propagation Modes:
    1F: Single F-layer hop (most common for < ~3000 km)
    2F: Two F-layer hops (3000-6000 km)
    3F: Three F-layer hops (> 6000 km)
    1E: Single E-layer hop (daytime, lower frequencies)

================================================================================
INTEGRATION WITH EXISTING CODE
================================================================================

This module is designed to be called by:
- ArrivalPatternMatrix.compute_matrix() — replaces _compute_propagation_delay_ms()
- MetrologyEngine._predict_geometric_delay() — replaces simple vacuum calculation

It depends on:
- IonoDataService — provides real-time Ne profiles and TEC
- wwv_constants — station locations and physical constants
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple, NamedTuple
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

# =============================================================================
# PHYSICAL CONSTANTS
# =============================================================================

C_LIGHT_M_S = 299792458.0        # Speed of light (m/s)
C_LIGHT_KM_S = 299792.458        # Speed of light (km/s)
C_LIGHT_KM_MS = 299.792458       # Speed of light (km/ms)
EARTH_RADIUS_KM = 6371.0         # Mean Earth radius (km)

# Ionospheric constants
E_CHARGE = 1.602176634e-19       # Elementary charge (C)
E_MASS = 9.1093837015e-31        # Electron mass (kg)
EPSILON_0 = 8.8541878128e-12     # Vacuum permittivity (F/m)
K_GROUP_DELAY = 40.3             # Group delay constant (m³/s²)
# K = e² / (8π²ε₀mₑ) = 40.3 m³/s²
# Group delay excess = K * sTEC / (c * f²)  [seconds]
# where sTEC in el/m², f in Hz


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class PropagationMode(NamedTuple):
    """A specific propagation mode (e.g., 1F, 2F, 1E)."""
    n_hops: int
    layer: str       # 'F' or 'E'
    label: str       # e.g., '1F', '2F', '1E'


# Standard propagation modes to evaluate
PROPAGATION_MODES = [
    PropagationMode(1, 'F', '1F'),
    PropagationMode(2, 'F', '2F'),
    PropagationMode(3, 'F', '3F'),
    PropagationMode(1, 'E', '1E'),
]


@dataclass
class ModeArrival:
    """Predicted arrival for a specific propagation mode."""
    mode: PropagationMode
    delay_ms: float                # Total group delay (geometric + ionospheric)
    geometric_delay_ms: float      # Vacuum path delay
    iono_delay_ms: float           # Excess ionospheric group delay
    path_length_km: float          # Total path length
    reflection_height_km: float    # Ionospheric reflection height
    elevation_angle_deg: float     # Launch elevation angle
    is_feasible: bool              # Whether this mode is physically possible
    uncertainty_ms: float          # 1-sigma uncertainty estimate
    
    # Diagnostics
    slant_tec_tecu: float = 0.0   # Slant TEC along path
    foF2_MHz: float = 0.0         # F2 critical frequency (for MUF check)
    muf_MHz: float = 0.0          # Maximum usable frequency for this mode


@dataclass
class PropagationPrediction:
    """Complete propagation prediction for a station/frequency pair."""
    station: str
    frequency_mhz: float
    timestamp: datetime
    distance_km: float
    
    # All feasible mode arrivals, sorted by delay
    arrivals: List[ModeArrival] = field(default_factory=list)
    
    # Best (most likely) arrival
    primary_delay_ms: float = 0.0
    primary_mode: str = ""
    primary_uncertainty_ms: float = 15.0
    
    # Model metadata
    data_source: str = "fallback"  # "wamipe", "wamipe+giro", "iri", "fallback"
    model_confidence: float = 0.0  # 0-1
    
    def get_feasible_arrivals(self) -> List[ModeArrival]:
        """Get all feasible arrivals sorted by delay."""
        return sorted(
            [a for a in self.arrivals if a.is_feasible],
            key=lambda a: a.delay_ms
        )
    
    def get_primary_arrival(self) -> Optional[ModeArrival]:
        """Get the primary (most likely) arrival."""
        feasible = self.get_feasible_arrivals()
        return feasible[0] if feasible else None


# =============================================================================
# PROPAGATION MODEL
# =============================================================================

class HFPropagationModel:
    """
    Physics-based HF propagation delay model.
    
    Computes group delay predictions using real-time ionospheric data
    from IonoDataService, with fallback to IRI-2020 or parametric models.
    
    Usage:
        model = HFPropagationModel(receiver_lat=38.92, receiver_lon=-92.13)
        
        prediction = model.predict(
            station='WWV',
            frequency_mhz=10.0,
            utc_time=datetime.now(timezone.utc)
        )
        
        print(f"Primary: {prediction.primary_delay_ms:.2f} ms ({prediction.primary_mode})")
        for arrival in prediction.get_feasible_arrivals():
            print(f"  {arrival.mode.label}: {arrival.delay_ms:.2f} ms")
    """
    
    def __init__(
        self,
        receiver_lat: float,
        receiver_lon: float,
        enable_realtime: bool = True
    ):
        """
        Initialize the propagation model.
        
        Args:
            receiver_lat: Receiver latitude (degrees)
            receiver_lon: Receiver longitude (degrees)
            enable_realtime: Whether to use real-time ionospheric data
        """
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self.enable_realtime = enable_realtime
        
        # Station locations (canonical source: wwv_constants)
        from .wwv_constants import STATION_LOCATIONS as _SL
        self.station_locations = {
            k: (v['lat'], v['lon']) for k, v in _SL.items()
        }
        
        # Pre-compute great circle distances
        self.distances: Dict[str, float] = {}
        for station, (lat, lon) in self.station_locations.items():
            self.distances[station] = self._haversine_km(
                receiver_lat, receiver_lon, lat, lon
            )
        
        # IonoDataService reference (lazy init)
        self._iono_service = None
        
        # IRI model reference (lazy init)
        self._iri_model = None
        
        # Cache of recent predictions
        self._cache: Dict[Tuple[str, float, int], PropagationPrediction] = {}
        self._cache_ttl_s = 60  # Cache predictions for 1 minute
        
        # DUT1 (UT1-UTC) correction from CHU FSK Frame B decode.
        # Used to compute UT1 for correct solar zenith angle in the parametric
        # ionospheric fallback model. UT1 = UTC + DUT1 gives the correct Earth
        # rotation angle. Typical magnitude: ±0.9s, updated via set_dut1().
        self._dut1_seconds: float = 0.0
        
        logger.info(f"HFPropagationModel initialized at ({receiver_lat:.4f}, {receiver_lon:.4f})")
    
    def _get_iono_service(self):
        """Lazy-initialize the IonoDataService."""
        if self._iono_service is None and self.enable_realtime:
            try:
                from .iono_data_service import IonoDataService
                self._iono_service = IonoDataService.get_instance()
                # Don't start the service here - let the caller manage lifecycle
            except ImportError:
                logger.warning("IonoDataService not available")
        return self._iono_service
    
    def _get_iri_model(self):
        """Lazy-initialize the IRI ionospheric model."""
        if self._iri_model is None:
            try:
                from .ionospheric_model import IonosphericModel
                self._iri_model = IonosphericModel(enable_iri=True)
            except (ImportError, Exception) as e:
                logger.debug(f"IRI model not available: {e}")
        return self._iri_model
    
    def predict(
        self,
        station: str,
        frequency_mhz: float,
        utc_time: Optional[datetime] = None,
        modes: Optional[List[PropagationMode]] = None
    ) -> PropagationPrediction:
        """
        Predict propagation delay for a station/frequency pair.
        
        Evaluates all feasible propagation modes and returns predictions
        for each, with the primary (most likely) mode identified.
        
        Args:
            station: Station name (WWV, WWVH, CHU, BPM)
            frequency_mhz: Frequency in MHz
            utc_time: UTC time (default: now)
            modes: Propagation modes to evaluate (default: all standard modes)
            
        Returns:
            PropagationPrediction with all feasible arrivals
        """
        if utc_time is None:
            utc_time = datetime.now(timezone.utc)
        
        if modes is None:
            modes = PROPAGATION_MODES
        
        # Check cache
        cache_key = (station, frequency_mhz, int(utc_time.timestamp()) // self._cache_ttl_s)
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        distance_km = self.distances.get(station, 0.0)
        if distance_km == 0.0:
            logger.warning(f"Unknown station: {station}")
            return PropagationPrediction(
                station=station, frequency_mhz=frequency_mhz,
                timestamp=utc_time, distance_km=0.0
            )
        
        # Get station coordinates
        station_lat, station_lon = self.station_locations[station]
        
        # Evaluate each propagation mode with per-mode iono params
        # sampled at the actual reflection points along the great circle
        arrivals = []
        for mode in modes:
            iono_params = self._get_mode_iono_params(
                n_hops=mode.n_hops,
                station_lat=station_lat,
                station_lon=station_lon,
                utc_time=utc_time
            )
            arrival = self._evaluate_mode(
                mode=mode,
                distance_km=distance_km,
                frequency_mhz=frequency_mhz,
                iono_params=iono_params,
                station_lat=station_lat,
                station_lon=station_lon,
                utc_time=utc_time
            )
            arrivals.append(arrival)
        
        # Build prediction
        prediction = PropagationPrediction(
            station=station,
            frequency_mhz=frequency_mhz,
            timestamp=utc_time,
            distance_km=distance_km,
            arrivals=arrivals,
            data_source=iono_params.get('source', 'fallback'),
            model_confidence=iono_params.get('confidence', 0.0)
        )
        
        # Set primary arrival (first feasible, lowest delay)
        feasible = prediction.get_feasible_arrivals()
        if feasible:
            primary = feasible[0]
            prediction.primary_delay_ms = primary.delay_ms
            prediction.primary_mode = primary.mode.label
            prediction.primary_uncertainty_ms = primary.uncertainty_ms
        else:
            # No feasible mode — use vacuum fallback
            vacuum_delay = distance_km / C_LIGHT_KM_MS
            prediction.primary_delay_ms = vacuum_delay * 1.15  # 15% overhead
            prediction.primary_mode = "vacuum_fallback"
            prediction.primary_uncertainty_ms = 15.0
        
        # Cache
        self._cache[cache_key] = prediction
        # Evict old entries
        if len(self._cache) > 1000:
            oldest = sorted(self._cache.keys(), key=lambda k: k[2])[:500]
            for k in oldest:
                del self._cache[k]
        
        return prediction
    
    def predict_all_modes(
        self,
        station: str,
        frequency_mhz: float,
        utc_time: Optional[datetime] = None
    ) -> List[ModeArrival]:
        """
        Get all feasible mode arrivals for a station/frequency.
        
        Convenience method for the arrival pattern matrix.
        
        Returns:
            List of feasible ModeArrival objects sorted by delay
        """
        prediction = self.predict(station, frequency_mhz, utc_time)
        return prediction.get_feasible_arrivals()
    
    def _get_iono_params(
        self,
        lat: float,
        lon: float,
        utc_time: datetime
    ) -> Dict:
        """
        Get ionospheric parameters from the best available source.
        
        Priority:
        1. IonoDataService (WAM-IPE + GIRO)
        2. IRI-2020
        3. Parametric fallback
        """
        # Try IonoDataService first
        service = self._get_iono_service()
        if service is not None:
            try:
                point = service.get_iono_params(lat, lon, utc_time)
                if point.source != "climatological_fallback":
                    return {
                        'hmF2_km': point.hmF2_km,
                        'hmE_km': point.hmE_km,
                        'NmF2_m3': point.NmF2_m3,
                        'foF2_MHz': point.foF2_MHz,
                        'TEC_TECU': point.TEC_TECU,
                        'source': point.source,
                        'confidence': 0.8 if 'giro' in point.source else 0.6,
                    }
            except Exception as e:
                logger.debug(f"IonoDataService failed: {e}")
        
        # Try IRI-2020
        iri = self._get_iri_model()
        if iri is not None:
            try:
                heights = iri.get_layer_heights(
                    timestamp=utc_time,
                    latitude=lat,
                    longitude=lon
                )
                if heights is not None:
                    return {
                        'hmF2_km': heights.hmF2,
                        'hmE_km': heights.hmE,
                        'NmF2_m3': 1.24e10 * (heights.foF2 ** 2) if hasattr(heights, 'foF2') and heights.foF2 else 1e12,
                        'foF2_MHz': heights.foF2 if hasattr(heights, 'foF2') and heights.foF2 else 8.0,
                        'TEC_TECU': 20.0,  # IRI doesn't directly give TEC here
                        'source': 'iri',
                        'confidence': 0.5,
                    }
            except Exception as e:
                logger.debug(f"IRI model failed: {e}")
        
        # Parametric fallback
        return self._parametric_iono(lat, lon, utc_time)
    
    def set_dut1(self, dut1_seconds: float) -> None:
        """Set DUT1 (UT1-UTC) from CHU FSK Frame B decode.
        
        DUT1 corrects UTC to UT1 (Earth rotation angle) for accurate solar
        zenith computation in the parametric ionospheric model. The effect is
        small (±0.9s → ±0.004° solar angle) but represents a real correction
        from a national time lab broadcast.
        
        Args:
            dut1_seconds: DUT1 in seconds (typically -0.9 to +0.9)
        """
        if abs(dut1_seconds) > 1.0:
            logger.warning(f"DUT1={dut1_seconds}s out of expected range ±0.9s")
            return
        if dut1_seconds != self._dut1_seconds:
            logger.info(f"Propagation model DUT1 updated: {self._dut1_seconds:+.1f}s → {dut1_seconds:+.1f}s")
            self._dut1_seconds = dut1_seconds

    def _parametric_iono(
        self,
        lat: float,
        lon: float,
        utc_time: datetime
    ) -> Dict:
        """Parametric ionospheric model fallback.
        
        Delegates to IonoDataService._climatological_fallback() for the canonical
        parametric model (includes seasonal, equatorial anomaly, and latitude terms).
        Falls back to a minimal inline model only if IonoDataService is unavailable.
        """
        try:
            from .iono_data_service import IonoDataService
            point = IonoDataService._climatological_fallback(lat, lon, utc_time)
            return {
                'hmF2_km': point.hmF2_km,
                'hmE_km': point.hmE_km,
                'NmF2_m3': point.NmF2_m3,
                'foF2_MHz': point.foF2_MHz,
                'TEC_TECU': point.TEC_TECU,
                'source': 'parametric',
                'confidence': 0.2,
            }
        except Exception as e:
            logger.debug(f"Caught exception: {e}")
            # Minimal inline fallback if IonoDataService import fails
            # Apply DUT1 correction: UT1 = UTC + DUT1 for correct solar geometry
            ut1_hour = utc_time.hour + utc_time.minute / 60.0 + self._dut1_seconds / 3600.0
            lst = ut1_hour + lon / 15.0
            lst = lst % 24.0
            diurnal_phase = (lst - 14.0) / 24.0 * 2 * math.pi
            hmF2 = 300.0 - 50.0 * math.cos(diurnal_phase)
            foF2 = 5.5 + 2.5 * math.cos(diurnal_phase)
            NmF2 = 1.24e10 * foF2 ** 2
            TEC = 5.0 + 35.0 * (1 + math.cos(diurnal_phase)) / 2.0
            return {
                'hmF2_km': hmF2,
                'hmE_km': 110.0,
                'NmF2_m3': NmF2,
                'foF2_MHz': foF2,
                'TEC_TECU': TEC,
                'source': 'parametric',
                'confidence': 0.2,
            }
    
    def _evaluate_mode(
        self,
        mode: PropagationMode,
        distance_km: float,
        frequency_mhz: float,
        iono_params: Dict,
        station_lat: float,
        station_lon: float,
        utc_time: datetime
    ) -> ModeArrival:
        """
        Evaluate a specific propagation mode.
        
        Checks feasibility (MUF, geometry) and computes delay if feasible.
        """
        n_hops = mode.n_hops
        
        # Select reflection height based on layer
        if mode.layer == 'E':
            reflection_height = iono_params.get('hmE_km', 110.0)
        else:
            reflection_height = iono_params.get('hmF2_km', 300.0)
        
        foF2 = iono_params.get('foF2_MHz', 8.0)
        
        # Check geometric feasibility
        # Maximum single-hop ground distance
        R = EARTH_RADIUS_KM
        h = reflection_height
        max_1hop_km = 2 * math.sqrt(2 * R * h + h ** 2)
        
        hop_distance = distance_km / n_hops
        
        # Is this mode geometrically possible?
        if hop_distance > max_1hop_km * 1.1:  # 10% margin
            return ModeArrival(
                mode=mode,
                delay_ms=0.0,
                geometric_delay_ms=0.0,
                iono_delay_ms=0.0,
                path_length_km=0.0,
                reflection_height_km=reflection_height,
                elevation_angle_deg=0.0,
                is_feasible=False,
                uncertainty_ms=0.0,
            )
        
        # Compute elevation angle
        # From spherical geometry: sin(elev) = (cos(θ/2) * (R+h) - R) / slant
        theta = hop_distance / R  # Central angle for one hop (radians)
        half_theta = theta / 2
        
        slant_sq = R**2 + (R + h)**2 - 2 * R * (R + h) * math.cos(half_theta)
        slant = math.sqrt(max(0, slant_sq))
        
        if slant > 0:
            sin_elev = ((R + h) * math.cos(half_theta) - R) / slant
            elevation_deg = math.degrees(math.asin(max(-1, min(1, sin_elev))))
        else:
            elevation_deg = 90.0
        
        # Check MUF (Maximum Usable Frequency)
        # MUF = foF2 * sec(i) where i is the angle of incidence at the layer
        # For oblique incidence: sec(i) ≈ distance factor
        # Simplified: MUF ≈ foF2 / sin(elevation)
        if elevation_deg > 0:
            muf = foF2 / math.sin(math.radians(elevation_deg))
        else:
            muf = foF2 * 3.0  # Rough estimate for very low angles
        
        # E-layer MUF is lower
        if mode.layer == 'E':
            foE = iono_params.get('foE_MHz', 3.0)
            if elevation_deg > 0:
                muf = foE / math.sin(math.radians(elevation_deg))
            else:
                muf = foE * 3.0
        
        # Is the frequency below the MUF? (with margin)
        is_feasible = frequency_mhz <= muf * 1.1  # 10% margin for model uncertainty
        
        # Also check minimum elevation (below ~3° is unreliable)
        if elevation_deg < 3.0 and n_hops == 1:
            is_feasible = False
        
        if not is_feasible:
            return ModeArrival(
                mode=mode,
                delay_ms=0.0,
                geometric_delay_ms=0.0,
                iono_delay_ms=0.0,
                path_length_km=0.0,
                reflection_height_km=reflection_height,
                elevation_angle_deg=elevation_deg,
                is_feasible=False,
                uncertainty_ms=0.0,
                muf_MHz=muf,
                foF2_MHz=foF2,
            )
        
        # Compute geometric path length (spherical Earth)
        path_length = n_hops * 2 * slant  # Up and down for each hop
        
        # Geometric (vacuum) delay
        geometric_delay_ms = path_length / C_LIGHT_KM_MS
        
        # Ionospheric group delay
        # Method: numerical integration through electron density profile if available,
        # otherwise use TEC-based approximation
        # Use the first reflection point for Ne profile lookup
        first_reflect_frac = 1.0 / (2.0 * n_hops)
        reflect_lat, reflect_lon = self._intermediate_point(
            self.receiver_lat, self.receiver_lon,
            station_lat, station_lon, first_reflect_frac
        )
        iono_delay_ms = self._compute_iono_delay(
            frequency_mhz=frequency_mhz,
            n_hops=n_hops,
            iono_params=iono_params,
            midpoint_lat=reflect_lat,
            midpoint_lon=reflect_lon,
            utc_time=utc_time,
            elevation_deg=elevation_deg
        )
        
        total_delay_ms = geometric_delay_ms + iono_delay_ms
        
        # Uncertainty estimate
        uncertainty_ms = self._estimate_uncertainty(
            mode=mode,
            iono_params=iono_params,
            frequency_mhz=frequency_mhz,
            elevation_deg=elevation_deg
        )
        
        return ModeArrival(
            mode=mode,
            delay_ms=total_delay_ms,
            geometric_delay_ms=geometric_delay_ms,
            iono_delay_ms=iono_delay_ms,
            path_length_km=path_length,
            reflection_height_km=reflection_height,
            elevation_angle_deg=elevation_deg,
            is_feasible=True,
            uncertainty_ms=uncertainty_ms,
            slant_tec_tecu=iono_params.get('TEC_TECU', 0.0),
            foF2_MHz=foF2,
            muf_MHz=muf,
        )
    
    def _compute_iono_delay(
        self,
        frequency_mhz: float,
        n_hops: int,
        iono_params: Dict,
        midpoint_lat: float,
        midpoint_lon: float,
        utc_time: datetime,
        elevation_deg: float
    ) -> float:
        """
        Compute ionospheric group delay.
        
        Two methods:
        1. Numerical integration through Ne(h) profile (if available)
        2. TEC-based approximation (fallback)
        
        Returns:
            Ionospheric excess group delay in milliseconds
        """
        # Try numerical integration through electron density profile
        service = self._get_iono_service()
        if service is not None:
            try:
                altitudes, Ne = service.get_electron_density_profile(
                    midpoint_lat, midpoint_lon, utc_time
                )
                if altitudes is not None and len(altitudes) > 0:
                    return self._integrate_group_delay(
                        altitudes_km=altitudes,
                        Ne_m3=Ne,
                        frequency_mhz=frequency_mhz,
                        n_hops=n_hops,
                        elevation_deg=elevation_deg
                    )
            except Exception as e:
                logger.debug(f"Ne profile integration failed: {e}")
        
        # Fallback: TEC-based approximation
        return self._tec_group_delay(
            tec_tecu=iono_params.get('TEC_TECU', 20.0),
            frequency_mhz=frequency_mhz,
            n_hops=n_hops,
            elevation_deg=elevation_deg
        )
    
    def _integrate_group_delay(
        self,
        altitudes_km: np.ndarray,
        Ne_m3: np.ndarray,
        frequency_mhz: float,
        n_hops: int,
        elevation_deg: float
    ) -> float:
        """
        Numerically integrate group delay through electron density profile.
        
        Computes:
            Δτ = (1/c) ∫ (n_g - 1) ds
        
        where n_g = 1 / sqrt(1 - f_p²/f²) ≈ 1 + 0.5 * f_p²/f²
        
        For oblique propagation, the path through each altitude layer is
        longer by a factor of 1/sin(elevation) (secant law).
        
        Args:
            altitudes_km: Altitude grid (km)
            Ne_m3: Electron density at each altitude (m^-3)
            frequency_mhz: Signal frequency (MHz)
            n_hops: Number of ionospheric hops
            elevation_deg: Elevation angle (degrees)
            
        Returns:
            Excess group delay in milliseconds
        """
        freq_hz = frequency_mhz * 1e6
        freq_sq = freq_hz ** 2
        
        # Integrate (n_g - 1) * ds through the profile
        # n_g - 1 ≈ 0.5 * f_p² / f² = 0.5 * Ne * e² / (4π²ε₀mₑ * f²)
        #         = 40.3 * Ne / f²  (with Ne in m^-3, f in Hz)
        
        elev_rad = math.radians(max(1.0, elevation_deg))
        cos_elev = math.cos(elev_rad)
        R = EARTH_RADIUS_KM
        
        delay_s = 0.0
        for i in range(len(altitudes_km) - 1):
            dh_km = altitudes_km[i + 1] - altitudes_km[i]
            dh_m = dh_km * 1000.0
            Ne_avg = (Ne_m3[i] + Ne_m3[i + 1]) / 2.0
            
            if Ne_avg <= 0:
                continue
            
            # Check if frequency is above local plasma frequency
            fp_sq = Ne_avg * E_CHARGE**2 / (4 * math.pi**2 * EPSILON_0 * E_MASS)
            if fp_sq >= freq_sq:
                # Signal is reflected — this is the reflection point
                # For group delay calculation, we integrate up to here
                break
            
            # Group delay excess per unit path length
            # Δn_g = 0.5 * fp² / f² (first-order approximation)
            # More accurate: 1/sqrt(1 - fp²/f²) - 1
            ratio = fp_sq / freq_sq
            if ratio < 0.5:
                # First-order approximation (good for f >> fp)
                dn_g = 0.5 * ratio
            else:
                # Full expression (near reflection)
                dn_g = 1.0 / math.sqrt(1.0 - ratio) - 1.0
            
            # Altitude-dependent obliquity (thin-shell mapping function)
            # M(h) = 1 / sqrt(1 - (R*cos(e)/(R+h))²)
            # More accurate than constant 1/sin(e), especially at low elevations
            h_mid = (altitudes_km[i] + altitudes_km[i + 1]) / 2.0
            sin_sq = 1.0 - (R * cos_elev / (R + h_mid)) ** 2
            obliquity = 1.0 / math.sqrt(max(0.01, sin_sq))
            
            # Path through this layer (oblique)
            ds_m = dh_m * obliquity
            
            delay_s += dn_g * ds_m / C_LIGHT_M_S
        
        # Multiply by number of hops (each hop traverses the ionosphere twice:
        # up and down, but the profile integration already covers one pass)
        # For a single hop: signal goes up through ionosphere, reflects, comes back down
        # So we need 2x the one-way integration
        delay_ms = delay_s * 2.0 * n_hops * 1000.0
        
        return delay_ms
    
    def _tec_group_delay(
        self,
        tec_tecu: float,
        frequency_mhz: float,
        n_hops: int,
        elevation_deg: float
    ) -> float:
        """
        Compute ionospheric group delay from TEC (fallback method).
        
        Group delay = 40.3 * sTEC / (c * f²) [seconds]
        
        where:
            sTEC = VTEC / sin(elevation)  [slant TEC]
            VTEC in TECU (10^16 el/m²)
            f in Hz
        
        Args:
            tec_tecu: Vertical TEC in TECU
            frequency_mhz: Frequency in MHz
            n_hops: Number of hops
            elevation_deg: Elevation angle
            
        Returns:
            Group delay in milliseconds
        """
        freq_hz = frequency_mhz * 1e6
        
        # Convert VTEC to slant TEC
        if elevation_deg > 5.0:
            obliquity = 1.0 / math.sin(math.radians(elevation_deg))
        else:
            obliquity = 5.0  # Cap
        
        stec = tec_tecu * 1e16 * obliquity  # Convert TECU to el/m²
        
        # Group delay = 40.3 * sTEC / (c * f²)
        delay_s = K_GROUP_DELAY * stec / (C_LIGHT_M_S * freq_hz ** 2)
        
        # Each hop traverses the ionosphere twice (up and down)
        delay_ms = delay_s * 2.0 * n_hops * 1000.0
        
        return delay_ms
    
    def _estimate_uncertainty(
        self,
        mode: PropagationMode,
        iono_params: Dict,
        frequency_mhz: float,
        elevation_deg: float
    ) -> float:
        """
        Estimate 1-sigma uncertainty for a mode prediction.
        
        Uncertainty sources:
        1. Ionospheric model error (hmF2 uncertainty → geometric delay error)
        2. TEC uncertainty → ionospheric delay error
        3. Mode identification uncertainty (is this really 1F or 2F?)
        4. Data source quality
        
        Returns:
            1-sigma uncertainty in milliseconds
        """
        source = iono_params.get('source', 'fallback')
        confidence = iono_params.get('confidence', 0.0)
        
        # Base uncertainty from data source
        if 'wamipe' in source and 'giro' in source:
            base_ms = 0.5   # WAM-IPE + GIRO correction
        elif 'wamipe' in source:
            base_ms = 1.0   # WAM-IPE alone
        elif 'iri' in source:
            base_ms = 1.5   # IRI-2020
        elif 'parametric' in source:
            base_ms = 3.0   # Parametric fallback
        else:
            base_ms = 5.0   # No model
        
        # hmF2 uncertainty contribution
        # ~30 km hmF2 error → ~0.1-0.3 ms delay error per hop
        hmF2_uncertainty_km = 30.0 if 'wamipe' in source else 50.0
        # Approximate: Δτ ≈ Δh / c * 2 * n_hops (simplified)
        hmF2_delay_error_ms = hmF2_uncertainty_km / C_LIGHT_KM_MS * 2 * mode.n_hops * 0.3
        
        # TEC uncertainty contribution
        # ~5 TECU error → frequency-dependent delay error
        tec_uncertainty_tecu = 5.0
        tec_delay_error_ms = self._tec_group_delay(
            tec_uncertainty_tecu, frequency_mhz, mode.n_hops, 
            max(elevation_deg, 10.0)
        )
        
        # Multi-hop penalty (more hops = more uncertainty)
        hop_factor = math.sqrt(mode.n_hops)
        
        # Low elevation penalty
        elev_factor = 1.0
        if elevation_deg < 10.0:
            elev_factor = 2.0
        elif elevation_deg < 20.0:
            elev_factor = 1.5
        
        # Combine (RSS)
        total_ms = math.sqrt(
            base_ms ** 2 +
            hmF2_delay_error_ms ** 2 +
            tec_delay_error_ms ** 2
        ) * hop_factor * elev_factor
        
        return total_ms
    
    def compute_differential_delay(
        self,
        station: str,
        freq1_mhz: float,
        freq2_mhz: float,
        utc_time: Optional[datetime] = None
    ) -> Tuple[float, float]:
        """
        Compute differential group delay between two frequencies.
        
        This is a key observable for TEC estimation:
            Δτ = τ(f1) - τ(f2) = 40.3 * sTEC * (1/f1² - 1/f2²) / c
        
        Args:
            station: Station name
            freq1_mhz: First frequency (MHz)
            freq2_mhz: Second frequency (MHz)
            utc_time: UTC time
            
        Returns:
            Tuple of (differential_delay_ms, implied_tec_tecu)
        """
        pred1 = self.predict(station, freq1_mhz, utc_time)
        pred2 = self.predict(station, freq2_mhz, utc_time)
        
        diff_ms = pred1.primary_delay_ms - pred2.primary_delay_ms
        
        # Implied TEC from differential delay
        # Δτ = K * sTEC * (1/f1² - 1/f2²) / c
        # sTEC = Δτ * c / (K * (1/f1² - 1/f2²))
        f1_hz = freq1_mhz * 1e6
        f2_hz = freq2_mhz * 1e6
        freq_factor = 1.0 / f1_hz**2 - 1.0 / f2_hz**2
        
        if abs(freq_factor) > 0:
            stec_el_m2 = (diff_ms / 1000.0) * C_LIGHT_M_S / (K_GROUP_DELAY * freq_factor)
            implied_tec_tecu = stec_el_m2 / 1e16
        else:
            implied_tec_tecu = 0.0
        
        return diff_ms, implied_tec_tecu
    
    def self_consistency_check(
        self,
        station: str,
        observed_delays: Dict[float, float],
        utc_time: Optional[datetime] = None
    ) -> Dict:
        """
        Check self-consistency between model and multi-frequency observations.
        
        Compares the observed differential delays between frequencies with
        the model's predicted differential delays. Large discrepancies indicate
        either a model error or a mode misidentification.
        
        Args:
            station: Station name
            observed_delays: Dict mapping frequency_mhz → observed_delay_ms
            utc_time: UTC time
            
        Returns:
            Dict with consistency metrics
        """
        if len(observed_delays) < 2:
            return {'consistent': True, 'reason': 'insufficient_frequencies'}
        
        freqs = sorted(observed_delays.keys())
        
        residuals = []
        for i in range(len(freqs)):
            for j in range(i + 1, len(freqs)):
                f1, f2 = freqs[i], freqs[j]
                
                # Observed differential delay
                obs_diff = observed_delays[f1] - observed_delays[f2]
                
                # Predicted differential delay
                pred_diff, implied_tec = self.compute_differential_delay(
                    station, f1, f2, utc_time
                )
                
                residual = obs_diff - pred_diff
                residuals.append({
                    'freq_pair': (f1, f2),
                    'obs_diff_ms': obs_diff,
                    'pred_diff_ms': pred_diff,
                    'residual_ms': residual,
                    'implied_tec_tecu': implied_tec,
                })
        
        # Compute consistency metric
        residual_values = [r['residual_ms'] for r in residuals]
        rms_residual = math.sqrt(sum(r**2 for r in residual_values) / len(residual_values))
        
        # Consistent if RMS residual < 1 ms
        consistent = rms_residual < 1.0
        
        return {
            'consistent': consistent,
            'rms_residual_ms': rms_residual,
            'pairs': residuals,
            'n_frequencies': len(freqs),
        }
    
    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great circle distance using Haversine formula."""
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        c = 2 * math.asin(math.sqrt(a))
        
        return EARTH_RADIUS_KM * c

    @staticmethod
    def _intermediate_point(
        lat1: float, lon1: float, lat2: float, lon2: float, fraction: float
    ) -> Tuple[float, float]:
        """
        Compute an intermediate point along the great circle at a given fraction.

        Uses the spherical interpolation formula so that multi-hop reflection
        points are placed correctly on the great circle rather than using
        simple linear lat/lon averaging.

        Args:
            lat1, lon1: Start point (degrees)
            lat2, lon2: End point (degrees)
            fraction: 0.0 = start, 1.0 = end

        Returns:
            (lat, lon) in degrees
        """
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

        a = math.sin((1 - fraction) * d) / math.sin(d)
        b = math.sin(fraction * d) / math.sin(d)

        x = a * math.cos(lat1_r) * math.cos(lon1_r) + b * math.cos(lat2_r) * math.cos(lon2_r)
        y = a * math.cos(lat1_r) * math.sin(lon1_r) + b * math.cos(lat2_r) * math.sin(lon2_r)
        z = a * math.sin(lat1_r) + b * math.sin(lat2_r)

        lat = math.degrees(math.atan2(z, math.sqrt(x ** 2 + y ** 2)))
        lon = math.degrees(math.atan2(y, x))
        return lat, lon

    def _get_mode_iono_params(
        self,
        n_hops: int,
        station_lat: float,
        station_lon: float,
        utc_time: datetime
    ) -> Dict:
        """
        Get ionospheric parameters averaged over the actual reflection points
        for a given number of hops.

        For 1-hop: single midpoint (fraction 0.5).
        For N-hop: reflection points at fractions 1/(2N), 3/(2N), ..., (2N-1)/(2N).

        This avoids the error of using the path midpoint for multi-hop modes
        where the ionospheric pierce points are distributed along the path.
        """
        if n_hops <= 1:
            # Single hop — midpoint is correct
            mid_lat, mid_lon = self._intermediate_point(
                self.receiver_lat, self.receiver_lon,
                station_lat, station_lon, 0.5
            )
            return self._get_iono_params(mid_lat, mid_lon, utc_time)

        # Multi-hop: sample at each reflection point and average
        params_list = []
        for i in range(n_hops):
            frac = (2 * i + 1) / (2 * n_hops)
            pt_lat, pt_lon = self._intermediate_point(
                self.receiver_lat, self.receiver_lon,
                station_lat, station_lon, frac
            )
            params_list.append(self._get_iono_params(pt_lat, pt_lon, utc_time))

        # Average the numeric parameters, keep metadata from first point
        avg = dict(params_list[0])
        for key in ('hmF2_km', 'hmE_km', 'NmF2_m3', 'foF2_MHz', 'TEC_TECU'):
            vals = [p.get(key, 0.0) for p in params_list if key in p]
            if vals:
                avg[key] = sum(vals) / len(vals)

        # Confidence is the minimum across all points (weakest link)
        confidences = [p.get('confidence', 0.0) for p in params_list]
        avg['confidence'] = min(confidences) if confidences else 0.0

        return avg
