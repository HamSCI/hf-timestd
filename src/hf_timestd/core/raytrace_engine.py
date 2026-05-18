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

Deployment status (P-H14)
-------------------------
As of the 2026-05 metrology/physics review this engine is **complete but not
yet wired into any caller** — neither HFPropagationModel nor
PropagationModeSolver constructs a RaytraceEngine.  This is a deliberate
deferral, not an oversight:

  * pyLAP/PHaRLAP is an optional dependency requiring a manual native install
    and three environment variables (see "Environment setup" below).  It is
    absent on the standard deployment, so a wired-in call would resolve to the
    geometric fallback anyway.
  * A single 2-D ray trace is orders of magnitude more expensive than the
    analytic tier, and PHaRLAP's Fortran ODE solver can enter runaway loops —
    every call is already isolated in a worker process with a timeout
    (``_raytrace_with_timeout``).  That cost rules it out of the real-time
    chrony-feed path.

The intended wiring, when scheduled, is **reanalysis-only and advisory**:
HFPropagationModel / PropagationModeSolver would, when ``is_available()`` is
true, call ``compute_modes()`` asynchronously as a Tier-0 cross-check whose
result is logged and compared against the analytic prediction but never
blocks or replaces the delivered delay.  Until then this module is retained,
tested for graceful degradation, and intentionally unreferenced.

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
import multiprocessing as mp
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
# Raytrace timeout (subprocess guard)
# ---------------------------------------------------------------------------
RAYTRACE_TIMEOUT_S = 120  # Max wall-clock seconds per raytrace_2d call


def _raytrace_with_timeout(raytrace_func, args, timeout_s=RAYTRACE_TIMEOUT_S):
    """Run raytrace_2d in a forked subprocess with hard timeout.

    PHaRLAP's Fortran ODE solver can enter runaway loops for certain
    frequency/ionosphere combinations (e.g. above-MUF at night).  A hung
    C-extension call cannot be interrupted by Python signals, so we fork
    a child process and kill it if it exceeds the deadline.
    """
    ctx = mp.get_context('fork')
    q = ctx.Queue(maxsize=1)

    def _worker(queue, fn, fn_args):
        try:
            result = fn(*fn_args)
            queue.put(('ok', result))
        except Exception as exc:
            queue.put(('err', str(exc)))

    proc = ctx.Process(target=_worker, args=(q, raytrace_func, args),
                       daemon=True)
    proc.start()
    proc.join(timeout=timeout_s)

    if proc.is_alive():
        logger.warning("raytrace_2d timed out after %ds — killing subprocess",
                       timeout_s)
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2)
        raise TimeoutError(f"raytrace_2d exceeded {timeout_s}s timeout")

    if q.empty():
        raise RuntimeError("raytrace_2d subprocess exited without result")

    tag, payload = q.get_nowait()
    if tag == 'err':
        raise RuntimeError(f"raytrace_2d failed: {payload}")
    return payload


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


def _gc_point(lat1_deg: float, lon1_deg: float,
              bearing_deg: float, dist_km: float) -> tuple[float, float]:
    """Return (lat, lon) in degrees at *dist_km* along a great circle
    from (lat1, lon1) at initial bearing *bearing_deg*."""
    R = 6371.0
    d = dist_km / R
    lat1 = math.radians(lat1_deg)
    lon1 = math.radians(lon1_deg)
    brg  = math.radians(bearing_deg)
    lat2 = math.asin(math.sin(lat1) * math.cos(d) +
                     math.cos(lat1) * math.sin(d) * math.cos(brg))
    lon2 = lon1 + math.atan2(math.sin(brg) * math.sin(d) * math.cos(lat1),
                              math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def _gc_bearing(lat1: float, lon1: float,
                lat2: float, lon2: float) -> float:
    """Initial bearing in degrees from (lat1, lon1) to (lat2, lon2)."""
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    return math.degrees(math.atan2(
        math.sin(dlon) * math.cos(lat2r),
        math.cos(lat1r) * math.sin(lat2r) -
        math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    )) % 360.0


def _build_iri_grid(tx_lat: float, tx_lon: float,
                    rx_lat: float, rx_lon: float,
                    utc: datetime,
                    height_start_km: float = 60.0,
                    height_inc_km:   float = 3.0,
                    n_heights: int = 200,
                    range_inc_km: float = 50.0,
                    n_iri_samples: int = 0) -> Optional[dict]:
    """
    Build a 2-D ionosphere electron-density grid along the great-circle
    path from tx to rx using IRI-2020 (via pylap.iri2016 → libiri2020).

    When *n_iri_samples* > 1 the IRI is evaluated at that many equally-spaced
    points along the great-circle path (TX → grid edge) and the Ne(h) profiles
    are linearly interpolated across all range columns.  When *n_iri_samples*
    is 0 (default) the sample count is chosen automatically: paths ≤ 2 000 km
    get 5 samples, longer paths get one sample per 500 km (capped at 25).
    Setting *n_iri_samples* = 1 reproduces the legacy midpoint-only behaviour.

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

        brg = _gc_bearing(tx_lat, tx_lon, rx_lat, rx_lon)
        r12_idx = 100.0  # moderate solar activity
        ut_list = [utc.year, utc.month, utc.day,
                   utc.hour, utc.minute]

        # Determine number of IRI sample points along the path.
        if n_iri_samples <= 0:
            n_iri_samples = max(5, min(25, int(gc_km / 500.0) + 1))
        if n_iri_samples == 1:
            # Legacy single-midpoint behaviour.
            sample_dists = [gc_km / 2.0]
        else:
            # Sample from TX to grid edge so rays beyond the receiver are
            # still covered.  The first sample is at the TX, the last at
            # grid_max_km.  Profiles beyond the last sample are held
            # constant (extrapolated from the last IRI call).
            sample_dists = np.linspace(0.0, grid_max_km, n_iri_samples).tolist()

        # Evaluate IRI at each sample point.
        sample_profiles: list[np.ndarray] = []
        sample_foF2: list[float] = []
        sample_hmF2: list[float] = []
        for d in sample_dists:
            slat, slon = _gc_point(tx_lat, tx_lon, brg, d)
            outf, oarr = _pylap_iri2016(
                float(slat), float(slon), float(r12_idx),
                ut_list,
                float(height_start_km), float(height_inc_km), int(n_heights),
                {}
            )
            ne = np.asarray(outf[0, :], dtype=np.float64)
            ne = np.maximum(ne, 0.0) / 1e6  # m^-3 → cm^-3
            sample_profiles.append(ne)
            nmF2 = max(float(oarr[0]), 0.0)
            sample_foF2.append(8.98 * math.sqrt(nmF2) / 1e6)
            sample_hmF2.append(float(oarr[1]))

        n_samp = len(sample_dists)
        logger.debug("IRI grid: %d samples along %.0f km path (%.0f km grid), "
                     "foF2 %.2f–%.2f MHz, hmF2 %.0f–%.0f km",
                     n_samp, gc_km, grid_max_km,
                     min(sample_foF2), max(sample_foF2),
                     min(sample_hmF2), max(sample_hmF2))

        # Interpolate Ne profiles onto every range column.
        if n_samp == 1:
            iono_en_grid = np.tile(sample_profiles[0].reshape(-1, 1),
                                  (1, n_ranges))
        else:
            range_km = np.arange(n_ranges) * range_inc_km
            profiles_arr = np.column_stack(sample_profiles)  # (n_heights, n_samp)
            sample_km = np.array(sample_dists)
            # np.interp operates per-height row; vectorise with apply_along_axis
            iono_en_grid = np.zeros((n_heights, n_ranges), dtype=np.float64)
            for h in range(n_heights):
                iono_en_grid[h, :] = np.interp(range_km, sample_km,
                                               profiles_arr[h, :])

        iono_en_grid_5 = np.zeros_like(iono_en_grid)  # Doppler shift not needed
        collision_grid = np.zeros_like(iono_en_grid)
        irreg_grid     = np.zeros((4, n_ranges), dtype=np.float64)

        # Report midpoint foF2/hmF2 for callers that log IRI parameters.
        mid_idx = n_samp // 2
        foF2_mhz = sample_foF2[mid_idx]
        hmF2_km  = sample_hmF2[mid_idx]

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
            # Wrapped in subprocess timeout to kill runaway ODE solver loops.
            ray_list, _rpath, _rstate = _raytrace_with_timeout(
                _pylap_raytrace_2d,
                (tx_lat, tx_lon,
                 elevs, bearing_deg, freqs, max_hops,
                 tol, 0,   # irreg_flag=0
                 iono['iono_en_grid'], iono['iono_en_grid_5'],
                 iono['collision_grid'],
                 iono['height_start'], iono['height_inc'], iono['range_inc'],
                 iono['irreg_grid']),
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

        except TimeoutError:
            logger.warning("RaytraceEngine: raytrace_2d timed out for "
                           "%s %.1f MHz; using geometric",
                           station, frequency_mhz)
            return self._geometric_fallback(station, frequency_mhz, utc_time)
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
