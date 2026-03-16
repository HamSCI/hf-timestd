"""
raytrace_engine.py — PHaRLAP/pyLAP interface for hf-timestd

Wraps PHaRLAP 2-D ray tracing (via pyLAP) into a clean interface that
the propagation pipeline can call for offline or advisory mode-identification.

Architecture position
---------------------
This module is **physics overlay** — it is NOT on the real-time chrony-feed
critical path.  It augments PropagationModeSolver with full numerical ray
tracing when pyLAP is available; the rest of the pipeline degrades gracefully
when it is not.

Environment setup (macOS / Linux)
----------------------------------
    export PHARLAP_HOME=/path/to/pharlap_4.7.4
    export DIR_MODELS_REF_DAT=$PHARLAP_HOME/dat
    # macOS: add pylap modules to Python path
    export PYTHONPATH=/path/to/pylap/modules:$PYTHONPATH

Usage
-----
    from hf_timestd.core.raytrace_engine import RaytraceEngine
    engine = RaytraceEngine.build(receiver_lat=40.68, receiver_lon=-105.04)
    if engine.is_available():
        modes = engine.compute_modes('WWV', 10.0, datetime.utcnow())
        for m in modes:
            print(m.n_hops, m.group_delay_ms, m.launch_elev_deg)
"""

from __future__ import annotations

import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional pyLAP import
# ---------------------------------------------------------------------------
_PYLAP_AVAILABLE = False
_PYLAP_ERROR: Optional[str] = None
_pylap_raytrace_2d = None
_pylap_iri2016 = None

def _try_import_pylap() -> bool:
    global _PYLAP_AVAILABLE, _PYLAP_ERROR, _pylap_raytrace_2d, _pylap_iri2016

    pylap_path = os.environ.get('PYLAP_MODULES',
                                os.path.join(os.path.dirname(__file__),
                                             '..', '..', '..', '..', '..',
                                             'pylap', 'modules'))
    pylap_path = os.path.realpath(pylap_path)
    if pylap_path not in sys.path and os.path.isdir(pylap_path):
        sys.path.insert(0, pylap_path)

    try:
        import importlib
        rt2d_mod = importlib.import_module('pylap.raytrace_2d')
        _pylap_raytrace_2d = rt2d_mod.raytrace_2d
        _PYLAP_AVAILABLE   = True
    except Exception as exc:
        _PYLAP_ERROR = str(exc)
        return False

    try:
        iri_mod = importlib.import_module('pylap.iri2016')
        _pylap_iri2016 = iri_mod.iri2016
    except Exception:
        _pylap_iri2016 = None  # raytrace still works; IRI grid falls back

    return True


_try_import_pylap()

# ---------------------------------------------------------------------------
# Station coordinates (transmitter sites)
# ---------------------------------------------------------------------------
try:
    from .wwv_constants import STATION_LOCATIONS as _STATION_LOCS
except Exception:
    _STATION_LOCS = {
        'WWV':   {'lat':  40.6773, 'lon': -105.0421},
        'WWVH':  {'lat':  21.9875, 'lon': -159.7649},
        'CHU':   {'lat':  45.2958, 'lon':  -75.7533},
    }


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass
class RayMode:
    """A single propagation mode found by ray tracing."""
    n_hops: int
    group_delay_ms: float
    launch_elev_deg: float
    ground_range_km: float
    apogee_km: float
    confidence: float = 1.0
    ray_label: int = 1  # 1 = good ray, per PHaRLAP convention

    @property
    def mode_label(self) -> str:
        return f"{self.n_hops}F"


@dataclass
class RaytraceResult:
    station: str
    frequency_mhz: float
    utc_time: datetime
    modes: List[RayMode] = field(default_factory=list)
    source: str = "pharlap"     # "pharlap" | "geometric"
    iri_foF2_mhz: float = 0.0
    iri_hmF2_km:  float = 0.0


# ---------------------------------------------------------------------------
# IRI ionosphere grid helper
# ---------------------------------------------------------------------------
_C_KM_S = 299792.458  # km/s


def _build_iri_grid(tx_lat: float, tx_lon: float,
                    rx_lat: float, rx_lon: float,
                    utc: datetime,
                    height_start_km: float = 60.0,
                    height_inc_km:   float = 3.0,
                    n_heights: int = 200,
                    range_inc_km: float = 50.0) -> Optional[dict]:
    """
    Build a 2-D ionosphere electron-density grid along the great-circle
    path from tx to rx using IRI-2020 (via pylap.iri2016 → libiri2020).

    pylap.iri2016 signature:
        iri2016(glat, glon, r12_idx, [year,month,day,hour,min],
                height_start, height_step, num_heights, iri_options_dict)
    Returns: (outf_2d, oarr_1d)
        outf_2d[0, :] = electron density profile (m^-3)
        oarr_1d[0]    = foF2 (MHz),  oarr_1d[1] = hmF2 (km)

    Returns a dict ready to pass to pylap.raytrace_2d, or None on failure.
    """
    if not _PYLAP_AVAILABLE or _pylap_iri2016 is None:
        return None

    try:
        # Great-circle distance → number of range columns
        dlat = math.radians(rx_lat - tx_lat)
        dlon = math.radians(rx_lon - tx_lon)
        a    = (math.sin(dlat/2)**2 +
                math.cos(math.radians(tx_lat)) *
                math.cos(math.radians(rx_lat)) *
                math.sin(dlon/2)**2)
        gc_km    = 6371.0 * 2 * math.asin(math.sqrt(a))
        # Grid must cover full multi-hop ray extent, not just the target range.
        # PHaRLAP convention: 10000 km covers all practical HF paths.
        grid_max_km = max(10000.0, gc_km * 2)
        n_ranges = int(grid_max_km / range_inc_km) + 1

        # Sample IRI at path midpoint (horizontally uniform approximation).
        mid_lat = (tx_lat + rx_lat) / 2.0
        mid_lon = (tx_lon + rx_lon) / 2.0
        r12_idx = 100.0  # moderate solar activity

        ut_list = [utc.year, utc.month, utc.day,
                   utc.hour, utc.minute]

        # pylap.iri2016 returns (outf[20, num_heights], oarr[100])
        outf, oarr = _pylap_iri2016(
            float(mid_lat), float(mid_lon), float(r12_idx),
            ut_list,
            float(height_start_km), float(height_inc_km), int(n_heights),
            {}
        )

        ne_profile = np.asarray(outf[0, :], dtype=np.float64)  # m^-3 from IRI
        ne_profile = np.maximum(ne_profile, 0.0)               # guard negatives
        ne_profile = ne_profile / 1e6                          # m^-3 → cm^-3 (raytrace_2d units)
        # oarr[0] = NmF2 (m^-3), oarr[1] = hmF2 (km)
        # foF2 = 8.98 * sqrt(NmF2) Hz → MHz
        nmF2       = max(float(oarr[0]), 0.0)
        foF2_mhz   = 8.98 * math.sqrt(nmF2) / 1e6
        hmF2_km    = float(oarr[1])

        # Broadcast profile across all range columns.
        iono_en_grid   = np.tile(ne_profile.reshape(-1, 1), (1, n_ranges))
        iono_en_grid_5 = np.zeros_like(iono_en_grid)  # Doppler shift not needed
        collision_grid = np.zeros_like(iono_en_grid)
        irreg_grid     = np.zeros((4, n_ranges), dtype=np.float64)

        return dict(
            iono_en_grid=iono_en_grid,
            iono_en_grid_5=iono_en_grid_5,
            collision_grid=collision_grid,
            irreg_grid=irreg_grid,
            height_start=height_start_km,
            height_inc=height_inc_km,
            range_inc=range_inc_km,
            foF2_mhz=foF2_mhz,
            hmF2_km=hmF2_km,
        )
    except Exception as exc:
        logger.warning(f"IRI grid build failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------
class RaytraceEngine:
    """
    PHaRLAP 2-D ray-tracing engine.  Instantiate once per receiver location;
    call compute_modes() for each (station, frequency, time) combination.

    When pyLAP is unavailable the engine returns geometric-fallback results
    so callers don't need to guard every call site.
    """

    def __init__(self, receiver_lat: float, receiver_lon: float):
        self.receiver_lat = receiver_lat
        self.receiver_lon = receiver_lon
        self._available   = _PYLAP_AVAILABLE
        if not self._available:
            logger.warning(f"RaytraceEngine: pyLAP unavailable ({_PYLAP_ERROR}); "
                           "geometric fallback active")

    # ------------------------------------------------------------------
    @classmethod
    def build(cls, receiver_lat: float, receiver_lon: float) -> "RaytraceEngine":
        return cls(receiver_lat, receiver_lon)

    def is_available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    def compute_modes(self,
                      station: str,
                      frequency_mhz: float,
                      utc_time: datetime,
                      max_hops: int = 3,
                      elev_min: float = 2.0,
                      elev_max: float = 60.0,
                      elev_step: float = 0.5) -> RaytraceResult:
        """
        Ray-trace from *station* to the receiver at *utc_time* for one frequency.

        Returns a RaytraceResult with all modes whose ground range closes on
        the receiver within ±200 km.  Falls back to geometric (vacuum) delay
        when pyLAP is unavailable.
        """
        if station not in _STATION_LOCS:
            logger.warning(f"RaytraceEngine: unknown station {station}")
            return self._geometric_fallback(station, frequency_mhz, utc_time)

        if not self._available:
            return self._geometric_fallback(station, frequency_mhz, utc_time)

        tx = _STATION_LOCS[station]
        tx_lat, tx_lon = tx['lat'], tx['lon']

        iono = _build_iri_grid(tx_lat, tx_lon,
                               self.receiver_lat, self.receiver_lon,
                               utc_time)
        if iono is None:
            logger.debug(f"RaytraceEngine: IRI grid unavailable, falling back for {station}")
            return self._geometric_fallback(station, frequency_mhz, utc_time)

        # Great-circle bearing from tx to rx
        dlon_r = math.radians(self.receiver_lon - tx_lon)
        lat1_r = math.radians(tx_lat)
        lat2_r = math.radians(self.receiver_lat)
        bearing_deg = math.degrees(math.atan2(
            math.sin(dlon_r) * math.cos(lat2_r),
            math.cos(lat1_r) * math.sin(lat2_r) -
            math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon_r)
        )) % 360.0

        # Great-circle distance
        dlat = math.radians(self.receiver_lat - tx_lat)
        dlon = math.radians(self.receiver_lon - tx_lon)
        a_gc = (math.sin(dlat/2)**2 +
                math.cos(math.radians(tx_lat)) *
                math.cos(math.radians(self.receiver_lat)) *
                math.sin(dlon/2)**2)
        target_range_km = 6371.0 * 2 * math.asin(math.sqrt(a_gc))
        tolerance_km    = max(300.0, target_range_km * 0.10)

        elevs = np.arange(elev_min, elev_max + elev_step, elev_step, dtype=np.float64)
        freqs = np.full(len(elevs), frequency_mhz, dtype=np.float64)

        result = RaytraceResult(
            station=station,
            frequency_mhz=frequency_mhz,
            utc_time=utc_time,
            iri_foF2_mhz=iono['foF2_mhz'],
            iri_hmF2_km=iono['hmF2_km'],
        )

        # raytrace_2d returns (ray_list, ray_path_list, ray_state_list).
        # ray_list is a Python list with one dict per elevation angle.
        # Each dict has arrays of length nhops_attempted for that ray:
        #   ray_dict['ground_range'][-1]  cumulative ground range (km)
        #   ray_dict['group_range'][-1]   cumulative group range (km)
        #   ray_dict['apogee'][-1]        apogee height (km)
        #   ray_dict['ray_label']         array of labels per hop (1=good)
        tol = [1e-7, 0.01, 10.0]  # [ODE tol, min step km, max step km]

        try:
            # Single call with max_hops avoids Fortran SAVE-variable segfault
            # that occurs when raytrace_2d is invoked multiple times per process.
            ray_list, _rpath, _rstate = _pylap_raytrace_2d(
                tx_lat, tx_lon,
                elevs, bearing_deg, freqs, max_hops,
                tol, 0,   # irreg_flag=0
                iono['iono_en_grid'], iono['iono_en_grid_5'],
                iono['collision_grid'],
                iono['height_start'], iono['height_inc'], iono['range_inc'],
                iono['irreg_grid'],
            )

            for i, ray_dict in enumerate(ray_list):
                labels = np.asarray(ray_dict.get('ray_label', []))
                if len(labels) == 0:
                    continue
                ground_ranges = np.asarray(ray_dict.get('ground_range', []))
                group_ranges  = np.asarray(ray_dict.get('group_range',  []))
                apogees       = np.asarray(ray_dict.get('apogee',       []))
                if len(ground_ranges) == 0:
                    continue

                # hop-0 group/ground ratio is always correctly extracted.
                # Use it to estimate total group path for any hop count.
                gnd0 = float(ground_ranges[0])
                grp0 = float(group_ranges[0]) if len(group_ranges) > 0 else 0.0
                apex0 = float(apogees[0]) if len(apogees) > 0 else 0.0
                if gnd0 > 0 and not math.isnan(grp0) and grp0 > 0:
                    grp_factor = grp0 / gnd0
                else:
                    grp_factor = 1.02  # typical ionospheric slowing

                for k in range(len(ground_ranges)):
                    if int(labels[k]) != 1:
                        continue  # this hop did not complete cleanly
                    total_gnd = float(ground_ranges[k])
                    if math.isnan(total_gnd):
                        continue
                    if abs(total_gnd - target_range_km) > tolerance_km:
                        continue  # ray doesn't land near the receiver
                    # Estimate total group path from hop-0 ratio
                    total_grp = total_gnd * grp_factor
                    delay_ms  = (total_grp / _C_KM_S) * 1000.0
                    result.modes.append(RayMode(
                        n_hops=k + 1,
                        group_delay_ms=delay_ms,
                        launch_elev_deg=float(elevs[i]),
                        ground_range_km=total_gnd,
                        apogee_km=apex0,
                        confidence=1.0,
                        ray_label=1,
                    ))

        except Exception as exc:
            logger.warning(f"RaytraceEngine raytrace_2d failed ({exc}); "
                           "falling back to geometric")
            return self._geometric_fallback(station, frequency_mhz, utc_time)

        if not result.modes:
            logger.debug(f"RaytraceEngine: no rays closed on receiver for "
                         f"{station} {frequency_mhz} MHz; using geometric")
            return self._geometric_fallback(station, frequency_mhz, utc_time)

        # Deduplicate by hop count — keep lowest launch angle per hop count
        by_hop: dict[int, RayMode] = {}
        for mode in result.modes:
            if (mode.n_hops not in by_hop or
                    mode.launch_elev_deg < by_hop[mode.n_hops].launch_elev_deg):
                by_hop[mode.n_hops] = mode
        result.modes = sorted(by_hop.values(), key=lambda m: m.n_hops)

        return result

    # ------------------------------------------------------------------
    def _geometric_fallback(self, station: str, frequency_mhz: float,
                             utc_time: datetime) -> RaytraceResult:
        """Vacuum great-circle delay — 1-hop only, confidence=0."""
        result = RaytraceResult(
            station=station,
            frequency_mhz=frequency_mhz,
            utc_time=utc_time,
            source="geometric",
        )
        if station not in _STATION_LOCS:
            return result
        tx = _STATION_LOCS[station]
        dlat = math.radians(self.receiver_lat - tx['lat'])
        dlon = math.radians(self.receiver_lon - tx['lon'])
        a    = (math.sin(dlat/2)**2 +
                math.cos(math.radians(tx['lat'])) *
                math.cos(math.radians(self.receiver_lat)) *
                math.sin(dlon/2)**2)
        dist_km  = 6371.0 * 2 * math.asin(math.sqrt(a))
        delay_ms = (dist_km / _C_KM_S) * 1000.0
        result.modes.append(RayMode(
            n_hops=1,
            group_delay_ms=delay_ms,
            launch_elev_deg=0.0,
            ground_range_km=dist_km,
            apogee_km=0.0,
            confidence=0.0,
        ))
        return result
