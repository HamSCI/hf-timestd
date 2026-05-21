"""
Ionogram API endpoints — all-arrivals Time-of-Flight vs SNR cluster analysis.

Analogous to Gwyn Griffiths' WSPRDaemon grape_acf_doppler_spread.py plots:
  - ToF time series with S+N level overlay
  - ToF vs S+N scatter with KDE density contours (cluster analysis)

Data source: /var/lib/timestd/phase2/<CHANNEL>/all_arrivals/*.h5
Schema fields used: timing_error_ms, corr_snr_db, peak_rank, minute_boundary_utc,
                    model_expected_ms, utc_second, station, frequency_mhz
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from pathlib import Path
import logging

from config import config
from hf_timestd.io import make_data_product_reader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ionogram", tags=["ionosphere"])

# Channels that have all_arrivals data
ALL_ARRIVALS_CHANNELS = [
    "CHU_3330", "CHU_7850", "CHU_14670",
    "WWV_5000", "WWV_10000", "WWV_15000", "WWV_20000", "WWV_25000",
    "WWVH_5000", "WWVH_10000",
    "BPM_5000", "BPM_10000",
]

# Station colour palette (matches Gwyn Griffiths slide aesthetic)
STATION_COLORS = {
    "CHU": "#A78BFA",   # bright violet (was #7B68EE)
    "WWV": "#2DD4BF",   # bright teal (was #20B2AA)
    "WWVH": "#FB923C",  # bright orange (was #FF8C00)
    "BPM": "#FB7185",   # bright rose (was #DC143C)
}


def _channel_to_station(channel: str) -> str:
    for s in ("CHU", "WWV", "WWVH", "BPM"):
        if channel.startswith(s):
            return s
    return channel


def _compute_solar_overlay(station: str, t0: datetime, t1: datetime) -> dict:
    """Compute solar elevation at the path midpoint for day/night shading."""
    try:
        from hf_timestd.core.solar_zenith_calculator import (
            calculate_midpoint, solar_position,
        )

        rx_lat = config.station_metadata.get('latitude', 0.0)
        rx_lon = config.station_metadata.get('longitude', 0.0)

        # Transmitter coordinates (approximate)
        TX_COORDS = {
            "CHU": (45.2950, -75.7533),   # Ottawa
            "WWV": (40.6776, -105.0461),   # Fort Collins
            "WWVH": (21.9886, -159.7642),  # Kauai
            "BPM": (34.95, 109.51),        # Pucheng
        }
        tx_lat, tx_lon = TX_COORDS.get(station, (40.0, -100.0))
        mid_lat, mid_lon = calculate_midpoint(rx_lat, rx_lon, tx_lat, tx_lon)

        # Generate every 5 minutes over the window
        timestamps = []
        elevations = []
        step = timedelta(minutes=5)
        curr = t0
        while curr <= t1:
            _, el = solar_position(curr, mid_lat, mid_lon)
            timestamps.append(int(curr.timestamp()))
            elevations.append(round(el, 2))
            curr += step

        return {
            "timestamps": timestamps,
            "elevation_deg": elevations,
            "midpoint": {"lat": round(mid_lat, 2), "lon": round(mid_lon, 2)},
            "station": station,
        }
    except Exception as e:
        logger.debug(f"Solar overlay failed: {e}")
        return None


def _load_all_arrivals(
    channel: str,
    ts0: int,
    ts1: int,
    rank_filter: Optional[int],
    min_snr_db: float,
    data_root: Path,
):
    """Load L1_all_arrivals records for one channel in [ts0, ts1]."""
    arr_dir = data_root / "phase2" / channel / "all_arrivals"

    # Per-station transmitter onset correction (fixed calibration constant).
    # CHU: 74ms H3E sideband filter group delay at the transmitter.
    # All others: no correction needed.
    station = _channel_to_station(channel)
    onset_ms = 74.0 if station == "CHU" else 0.0

    rows = {
        "minute_boundary": [],
        "arrival_ms": [],
        "timing_error_ms": [],
        "corr_snr_db": [],
        "peak_rank": [],
        "utc_second": [],
        "carrier_phase_rad": [],
        "detection_method": [],
        "sec_in_minute": [],
    }

    # Read window as ISO; minute_boundary equality filter happens in Python.
    t0_iso = datetime.fromtimestamp(ts0, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
    t1_iso = datetime.fromtimestamp(ts1, tz=timezone.utc).isoformat().replace('+00:00', 'Z')

    try:
        reader = make_data_product_reader(
            data_dir=arr_dir,
            product_level='L1',
            product_name='all_arrivals',
            channel=channel,
            storage_config=getattr(config, 'storage', {}) or {},
        )
    except Exception as e:
        logger.warning(f"L1_all_arrivals reader init failed for {channel}: {e}")
        return None

    try:
        try:
            raw_rows = reader.read_time_range(start=t0_iso, end=t1_iso)
        except Exception as e:
            logger.warning(f"L1_all_arrivals read failed for {channel}: {e}")
            raw_rows = []
    finally:
        close_fn = getattr(reader, 'close', None)
        if close_fn is not None:
            try:
                close_fn()
            except Exception:
                pass

    for row in raw_rows:
        mb = row.get('minute_boundary_utc')
        if mb is None or mb < ts0 or mb > ts1:
            continue
        snr = row.get('corr_snr_db')
        if snr is None or snr < min_snr_db:
            continue
        rank = row.get('peak_rank')
        if rank_filter is not None and rank != rank_filter:
            continue
        rows["minute_boundary"].append(mb)
        rows["arrival_ms"].append(row.get('arrival_ms'))
        rows["timing_error_ms"].append(row.get('timing_error_ms'))
        rows["corr_snr_db"].append(snr)
        rows["peak_rank"].append(rank)
        rows["utc_second"].append(row.get('utc_second'))
        rows["carrier_phase_rad"].append(row.get('carrier_phase_rad') or 0.0)
        rows["detection_method"].append(row.get('detection_method') or 'tone_correlator')
        rows["sec_in_minute"].append(row.get('sec_in_minute') or 0)

    if not rows["minute_boundary"]:
        return None

    # GPS-referenced ToF: no propagation model needed.
    #
    # CHU: minute_boundary_utc is the true minute boundary; utc_second is the
    #   broadcast second (Unix timestamp). sec_in_min = utc_second - mb (1..59).
    #   tof_ms = arrival_ms - sec_in_min*1000 - onset_ms
    #
    # WWV/WWVH/BPM: the writer stores per-second boundaries — minute_boundary_utc
    #   equals utc_second, so sec_in_min = 0. arrival_ms is already the offset
    #   within that second, i.e. the propagation delay directly.
    #   tof_ms = arrival_ms - onset_ms  (onset_ms = 0 for these stations)
    import numpy as np
    arr_arr = np.array(rows["arrival_ms"])
    mb_arr  = np.array(rows["minute_boundary"])
    sec_arr = np.array(rows["utc_second"])
    te_arr  = np.array(rows["timing_error_ms"])
    sec_in_min = sec_arr - mb_arr  # 1..59 for CHU, 0 for WWV/WWVH/BPM
    rows["tof_ms"] = (arr_arr - sec_in_min * 1000.0 - onset_ms).tolist()
    rows["sec_in_min"] = sec_in_min.tolist()
    rows["timing_error_ms"] = te_arr.tolist()
    rows["onset_correction_ms"] = onset_ms

    return rows


def _kde_contours(x, y, n_grid=60, bandwidth=None):
    """
    Compute KDE density on a grid for contour plotting.
    Returns (xi, yi, zi) as flat lists for JSON serialisation.
    """
    import numpy as np
    try:
        from scipy.stats import gaussian_kde
    except ImportError:
        return None

    if len(x) < 10:
        return None

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)

    # Remove NaN/Inf
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    if len(x) < 10:
        return None

    try:
        kde = gaussian_kde(np.vstack([x, y]), bw_method=bandwidth or "scott")
        xi = np.linspace(x.min(), x.max(), n_grid)
        yi = np.linspace(y.min(), y.max(), n_grid)
        Xi, Yi = np.meshgrid(xi, yi)
        Zi = kde(np.vstack([Xi.ravel(), Yi.ravel()])).reshape(Xi.shape)
        # Normalise to [0, 1]
        Zi = (Zi - Zi.min()) / (Zi.max() - Zi.min() + 1e-30)
        return {
            "x": xi.tolist(),
            "y": yi.tolist(),
            "z": Zi.tolist(),   # shape [n_grid, n_grid], row-major (y varies first)
        }
    except Exception as e:
        logger.debug(f"KDE failed: {e}")
        return None


@router.get("/arrivals")
async def get_all_arrivals(
    channel: str = Query("CHU_7850", description="Channel name, e.g. CHU_7850, WWV_10000"),
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-6h')"),
    end: str = Query("now", description="End time"),
    rank: Optional[int] = Query(None, description="Filter by peak_rank (0=dominant, 1=secondary, None=all)"),
    min_snr_db: float = Query(6.0, description="Minimum correlation SNR"),
    include_kde: bool = Query(True, description="Include KDE density grid for contour plot"),
    downsample: int = Query(1, description="Keep every Nth point for scatter (1=all)"),
):
    """
    All-arrivals Time-of-Flight data for one channel.

    Returns scatter data (ToF vs corr_snr_db) and optionally a KDE density
    grid for contour overlay — analogous to Gwyn Griffiths' cluster analysis
    of ToF and S+N level.

    ToF = model_expected_ms + timing_error_ms (absolute propagation delay).
    Rank 0 = dominant (shortest) arrival; rank ≥ 1 = secondary (multi-hop).
    """
    try:
        import numpy as np

        now = datetime.utcnow()

        def _parse(s):
            if s == "now":
                return now
            if s.startswith("-"):
                val = int(s[1:-1])
                u = s[-1]
                if u == "h":
                    return now - timedelta(hours=val)
                if u == "d":
                    return now - timedelta(days=val)
                if u == "m":
                    return now - timedelta(minutes=val)
                raise ValueError(f"Unknown unit '{u}'")
            return datetime.fromisoformat(s.replace("Z", ""))

        t0 = _parse(start)
        t1 = _parse(end)
        ts0 = int(t0.timestamp())
        ts1 = int(t1.timestamp())

        data_root = Path(config.data_root)
        rows = _load_all_arrivals(channel, ts0, ts1, rank, min_snr_db, data_root)

        if rows is None or not rows["minute_boundary"]:
            return {
                "status": "no_data",
                "channel": channel,
                "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
                "n_points": 0,
                "scatter": {},
                "kde": None,
            }

        tof = np.array(rows["tof_ms"])
        snr = np.array(rows["corr_snr_db"])
        rank_arr = np.array(rows["peak_rank"])
        mb = np.array(rows["minute_boundary"])
        sim = np.array(rows["sec_in_min"])
        te_arr = np.array(rows["timing_error_ms"])

        # Filter to physically plausible GPS-referenced ToF.
        # CHU: sec_in_min must be 1-59 (valid broadcast seconds within minute).
        #   The sec_in_min filter already rejects wrong-minute matches.
        # WWV/WWVH/BPM: writer stores per-second boundaries (sec_in_min=0).
        #   arrival_ms = me + te. The correlator frequently locks onto the wrong
        #   5ms tick (te ≈ +60-90ms). Filter |te| < 15ms to keep only real
        #   detections where the peak landed near the model-expected position.
        # SNR cap at 40 dB removes DC/self-interference artefacts.
        SNR_CAP = 40.0
        station = _channel_to_station(channel)
        if station == "CHU":
            tof_max = 30.0
            sim_valid = (sim >= 1) & (sim <= 59)
            te_valid = np.ones(len(te_arr), dtype=bool)  # sec_in_min handles this
        elif station in ("WWVH", "BPM"):
            tof_max = 500.0
            sim_valid = sim == 0
            te_valid = np.abs(te_arr) < 15.0
        else:  # WWV
            tof_max = 200.0
            sim_valid = sim == 0
            te_valid = np.abs(te_arr) < 15.0
        valid = (
            (snr <= SNR_CAP)
            & np.isfinite(tof)
            & (tof >= 0)
            & (tof <= tof_max)
            & sim_valid
            & te_valid
        )
        tof, snr, rank_arr, mb = tof[valid], snr[valid], rank_arr[valid], mb[valid]

        # Downsample for scatter (KDE always uses full set)
        if downsample > 1:
            idx = np.arange(0, len(tof), downsample)
        else:
            idx = np.arange(len(tof))

        scatter = {
            "minute_boundary": mb[idx].tolist(),
            "tof_ms": tof[idx].tolist(),
            "corr_snr_db": snr[idx].tolist(),
            "peak_rank": rank_arr[idx].tolist(),
        }

        # KDE on full (undownsampled, capped) data — x=snr, y=tof (Griffiths convention)
        kde_result = None
        if include_kde and len(tof) >= 10:
            kde_result = _kde_contours(snr, tof, n_grid=60)

        # Summary statistics per rank
        rank_stats = {}
        for r in np.unique(rank_arr):
            m = rank_arr == r
            rank_stats[int(r)] = {
                "n": int(np.sum(m)),
                "tof_mean_ms": float(np.nanmean(tof[m])),
                "tof_std_ms": float(np.nanstd(tof[m])),
                "snr_mean_db": float(np.nanmean(snr[m])),
            }

        station = _channel_to_station(channel)
        freq_khz = channel.split("_")[-1]
        try:
            freq_mhz = int(freq_khz) / 1000.0
        except ValueError:
            freq_mhz = 0.0

        return {
            "status": "ok",
            "channel": channel,
            "station": station,
            "frequency_mhz": freq_mhz,
            "color": STATION_COLORS.get(station, "#888888"),
            "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
            "n_points": len(tof),
            "n_scatter": len(idx),
            "rank_stats": rank_stats,
            "scatter": scatter,
            "kde": kde_result,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in /ionogram/arrivals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/arrivals/timeseries")
async def get_arrivals_timeseries(
    channel: str = Query("CHU_7850", description="Channel name"),
    start: str = Query("-24h", description="Start time"),
    end: str = Query("now", description="End time"),
    min_snr_db: float = Query(6.0, description="Minimum correlation SNR"),
):
    """
    All-arrivals ToF time series (top panel of Griffiths-style plot).

    Returns per-minute ToF for rank-0 (dominant) and rank-1+ (secondary)
    arrivals separately, plus the corr_snr_db time series (bottom panel).

    The implied F2 virtual height can be derived from the rank-0 vs rank-1
    delay difference:
        Δτ = τ(2F2) − τ(1F2)
        h_F2 = sqrt( (c·Δτ/2)² − (d/4)² )   [flat-Earth approx]
    """
    try:
        import numpy as np

        now = datetime.utcnow()

        def _parse(s):
            if s == "now":
                return now
            if s.startswith("-"):
                val = int(s[1:-1])
                u = s[-1]
                if u == "h":
                    return now - timedelta(hours=val)
                if u == "d":
                    return now - timedelta(days=val)
                if u == "m":
                    return now - timedelta(minutes=val)
                raise ValueError(f"Unknown unit '{u}'")
            return datetime.fromisoformat(s.replace("Z", ""))

        t0 = _parse(start)
        t1 = _parse(end)
        ts0 = int(t0.timestamp())
        ts1 = int(t1.timestamp())

        data_root = Path(config.data_root)
        rows = _load_all_arrivals(channel, ts0, ts1, None, min_snr_db, data_root)

        if rows is None or not rows["minute_boundary"]:
            return {
                "status": "no_data",
                "channel": channel,
                "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
                "dominant": {"timestamps": [], "tof_ms": [], "snr_db": []},
                "secondary": {"timestamps": [], "tof_ms": [], "snr_db": []},
            }

        tof = np.array(rows["tof_ms"])
        snr = np.array(rows["corr_snr_db"])
        rank_arr = np.array(rows["peak_rank"])
        mb = np.array(rows["minute_boundary"])
        sim = np.array(rows["sec_in_min"])
        te_arr = np.array(rows["timing_error_ms"])

        # Same GPS-referenced filter as /arrivals endpoint
        station = _channel_to_station(channel)
        if station == "CHU":
            tof_max = 30.0
            sim_valid = (sim >= 1) & (sim <= 59)
            te_valid = np.ones(len(te_arr), dtype=bool)
        elif station in ("WWVH", "BPM"):
            tof_max = 500.0
            sim_valid = sim == 0
            te_valid = np.abs(te_arr) < 15.0
        else:  # WWV
            tof_max = 200.0
            sim_valid = sim == 0
            te_valid = np.abs(te_arr) < 15.0
        valid = (
            np.isfinite(tof) & (tof >= 0) & (tof <= tof_max) & sim_valid & te_valid
        )
        tof, snr, rank_arr, mb = tof[valid], snr[valid], rank_arr[valid], mb[valid]

        dom = rank_arr == 0
        sec = rank_arr > 0

        # Per-minute median for dominant arrivals (cleaner than raw scatter)
        def _bin_by_minute(mb_arr, val_arr, snr_arr):
            unique_mb = np.unique(mb_arr)
            ts_out, val_out, snr_out = [], [], []
            for t in unique_mb:
                m = mb_arr == t
                ts_out.append(int(t))
                val_out.append(float(np.nanmedian(val_arr[m])))
                snr_out.append(float(np.nanmedian(snr_arr[m])))
            return ts_out, val_out, snr_out

        dom_ts, dom_tof, dom_snr = _bin_by_minute(mb[dom], tof[dom], snr[dom])
        sec_ts, sec_tof, sec_snr = _bin_by_minute(mb[sec], tof[sec], snr[sec])

        # Implied F2 height from rank-0 vs rank-1 delay difference.
        # Match dominant and secondary by minute, compute Δτ.
        # Require Δτ ≥ 2ms to reject correlation sidelobes masquerading
        # as multipath (real 1F2→2F2 separation is typically 3-10ms).
        dom_dict = dict(zip(dom_ts, dom_tof))
        dom_snr_dict = dict(zip(dom_ts, dom_snr))
        sec_dict = dict(zip(sec_ts, sec_tof))
        sec_snr_dict = dict(zip(sec_ts, sec_snr))
        common = sorted(set(dom_dict) & set(sec_dict))

        C_KM_S = 299792.458
        MIN_DELTA_TAU_MS = 2.0   # reject sidelobes (< 2ms not physical)
        MIN_PAIR_SNR_DB = 20.0   # both arrivals must be strong

        h_ts, h_vals = [], []
        for t in common:
            delta_tau_ms = sec_dict[t] - dom_dict[t]
            if delta_tau_ms < MIN_DELTA_TAU_MS:
                continue
            # Require both rank-0 and rank-1 to have decent SNR
            if dom_snr_dict.get(t, 0) < MIN_PAIR_SNR_DB:
                continue
            if sec_snr_dict.get(t, 0) < MIN_PAIR_SNR_DB:
                continue
            # Simple vertical-path approximation: h ≈ c·Δτ/2
            h_approx_km = C_KM_S * (delta_tau_ms / 1000.0) / 2.0
            if 100 < h_approx_km < 600:
                h_ts.append(int(t))
                h_vals.append(float(h_approx_km))

        station = _channel_to_station(channel)

        # Solar zenith at path midpoint — for day/night shading overlay
        solar = _compute_solar_overlay(station, t0, t1)

        return {
            "status": "ok",
            "channel": channel,
            "station": station,
            "color": STATION_COLORS.get(station, "#888888"),
            "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
            "dominant": {
                "timestamps": dom_ts,
                "tof_ms": dom_tof,
                "snr_db": dom_snr,
                "n": len(dom_ts),
            },
            "secondary": {
                "timestamps": sec_ts,
                "tof_ms": sec_tof,
                "snr_db": sec_snr,
                "n": len(sec_ts),
            },
            "f2_height": {
                "timestamps": h_ts,
                "height_km": h_vals,
                "n": len(h_ts),
                "note": "Approximate F2 virtual height from rank-0 vs rank-1 delay difference",
            },
            "solar": solar,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in /ionogram/arrivals/timeseries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/doppler-delay")
async def get_doppler_delay(
    channel: str = Query("CHU_7850", description="Channel name"),
    start: str = Query("-6h", description="Start time"),
    end: str = Query("now", description="End time"),
    min_snr_db: float = Query(3.0, description="Minimum tick SNR (lower for edge ticks)"),
    method: Optional[str] = Query(None, description="Filter by detection_method: 'edge_tick', 'tone_correlator', or None for all"),
    include_kde: bool = Query(True, description="Include 2D KDE density grid"),
):
    """
    Doppler-Delay scatter: per-tick timing_error vs carrier_phase.

    Each point is one per-second tick detection from the TickEdgeDetector.
    The carrier_phase_rad progression across seconds within a minute encodes
    Doppler shift; the timing_error_ms is the propagation delay residual.

    Multipath modes (1F2, 2F2, ...) arriving via different ionospheric layers
    have different Doppler shifts (because the layers move at different
    velocities).  Even when delays are too close to resolve temporally,
    different Doppler signatures separate them in the phase domain.

    The 2D KDE highlights clusters — each cluster is a candidate propagation
    mode.  This is the HF time-signal analogue of a Doppler-Delay spread
    function from channel sounding.
    """
    try:
        import numpy as np

        now = datetime.utcnow()

        def _parse(s):
            if s == "now":
                return now
            if s.startswith("-"):
                val = int(s[1:-1])
                u = s[-1]
                if u == "h":
                    return now - timedelta(hours=val)
                if u == "d":
                    return now - timedelta(days=val)
                if u == "m":
                    return now - timedelta(minutes=val)
                raise ValueError(f"Unknown unit '{u}'")
            return datetime.fromisoformat(s.replace("Z", ""))

        t0 = _parse(start)
        t1 = _parse(end)
        ts0 = int(t0.timestamp())
        ts1 = int(t1.timestamp())

        data_root = Path(config.data_root)
        rows = _load_all_arrivals(channel, ts0, ts1, None, min_snr_db, data_root)

        if rows is None or not rows["minute_boundary"]:
            return {
                "status": "no_data",
                "channel": channel,
                "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
                "n_points": 0,
                "scatter": {},
                "kde": None,
            }

        te_arr = np.array(rows["timing_error_ms"])
        phase_arr = np.array(rows["carrier_phase_rad"])
        snr_arr = np.array(rows["corr_snr_db"])
        mb_arr = np.array(rows["minute_boundary"])
        sec_arr = np.array(rows["utc_second"])
        sim_arr = np.array(rows.get("sec_in_minute", [0] * len(te_arr)))
        method_arr = np.array(rows["detection_method"])

        # Filter by detection method if specified
        if method:
            method_mask = np.array([m == method for m in method_arr])
        else:
            method_mask = np.ones(len(te_arr), dtype=bool)

        # Filter out zero-phase records (tone_correlator doesn't have real phase)
        # and require finite values
        valid = (
            method_mask
            & np.isfinite(te_arr)
            & np.isfinite(phase_arr)
            & (snr_arr >= min_snr_db)
        )

        # For edge_tick records, also filter implausible timing errors
        station = _channel_to_station(channel)
        if station == "CHU":
            valid &= (np.abs(te_arr) < 20.0)
        else:
            valid &= (np.abs(te_arr) < 15.0)

        te = te_arr[valid]
        phase = phase_arr[valid]
        snr = snr_arr[valid]
        mb = mb_arr[valid]
        sec = sec_arr[valid]
        sim = sim_arr[valid]
        meth = method_arr[valid]

        # Unwrap phase per minute to show Doppler trend
        # Group by minute_boundary, unwrap within each minute
        phase_unwrapped = np.copy(phase)
        for m in np.unique(mb):
            minute_mask = mb == m
            if np.sum(minute_mask) >= 3:
                phase_unwrapped[minute_mask] = np.unwrap(phase[minute_mask])

        # Compute per-minute Doppler from phase slope
        minute_doppler = {}
        for m in np.unique(mb):
            minute_mask = mb == m
            if np.sum(minute_mask) >= 5:
                t_sec = sim[minute_mask].astype(float)
                p_rad = phase_unwrapped[minute_mask]
                if t_sec[-1] - t_sec[0] > 5.0:
                    try:
                        coeffs = np.polyfit(t_sec, p_rad, 1)
                        doppler_hz = coeffs[0] / (2.0 * np.pi)
                        minute_doppler[int(m)] = float(doppler_hz)
                    except Exception:
                        pass

        scatter = {
            "minute_boundary": mb.tolist(),
            "utc_second": sec.tolist(),
            "sec_in_minute": sim.tolist(),
            "timing_error_ms": te.tolist(),
            "carrier_phase_rad": phase.tolist(),
            "phase_unwrapped_rad": phase_unwrapped.tolist(),
            "corr_snr_db": snr.tolist(),
            "detection_method": [str(m) for m in meth],
        }

        # 2D KDE: timing_error (x) vs carrier_phase (y)
        kde_result = None
        if include_kde and len(te) >= 20:
            kde_result = _kde_contours(te, phase, n_grid=50, bandwidth=None)

        # Summary statistics
        n_edge = int(np.sum(np.array([m == 'edge_tick' for m in meth])))
        n_corr = int(np.sum(np.array([m == 'tone_correlator' for m in meth])))

        return {
            "status": "ok",
            "channel": channel,
            "station": station,
            "color": STATION_COLORS.get(station, "#888888"),
            "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
            "n_points": len(te),
            "n_edge_ticks": n_edge,
            "n_tone_correlator": n_corr,
            "scatter": scatter,
            "kde": kde_result,
            "minute_doppler": minute_doppler,
            "summary": {
                "timing_error_mean_ms": float(np.nanmean(te)) if len(te) > 0 else 0.0,
                "timing_error_std_ms": float(np.nanstd(te)) if len(te) > 0 else 0.0,
                "phase_std_rad": float(np.nanstd(phase)) if len(phase) > 0 else 0.0,
                "mean_snr_db": float(np.nanmean(snr)) if len(snr) > 0 else 0.0,
                "n_minutes_with_doppler": len(minute_doppler),
            },
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in /ionogram/doppler-delay: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/channels")
async def list_ionogram_channels():
    """List channels that have all_arrivals data available."""
    data_root = Path(config.data_root)
    available = []
    for ch in ALL_ARRIVALS_CHANNELS:
        arr_dir = data_root / "phase2" / ch / "all_arrivals"
        if arr_dir.exists() and any(arr_dir.glob("*.h5")):
            files = sorted(arr_dir.glob("*.h5"))
            available.append({
                "channel": ch,
                "station": _channel_to_station(ch),
                "color": STATION_COLORS.get(_channel_to_station(ch), "#888888"),
                "n_files": len(files),
                "latest_date": files[-1].stem.split("_")[-1] if files else None,
            })
    return {"status": "ok", "channels": available, "n_channels": len(available)}
