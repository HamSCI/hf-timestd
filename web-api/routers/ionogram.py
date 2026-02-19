"""
Ionogram API endpoints — all-arrivals Time-of-Flight vs SNR cluster analysis.

Analogous to Gwyn Griffin's WSPRDaemon grape_acf_doppler_spread.py plots:
  - ToF time series with S+N level overlay
  - ToF vs S+N scatter with KDE density contours (cluster analysis)

Data source: /var/lib/timestd/phase2/<CHANNEL>/all_arrivals/*.h5
Schema fields used: timing_error_ms, corr_snr_db, peak_rank, minute_boundary_utc,
                    model_expected_ms, utc_second, station, frequency_mhz
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path
import logging

from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ionogram", tags=["ionosphere"])

# Channels that have all_arrivals data
ALL_ARRIVALS_CHANNELS = [
    "CHU_3330", "CHU_7850", "CHU_14670",
    "WWV_5000", "WWV_10000", "WWV_15000", "WWV_20000",
    "WWVH_5000", "WWVH_10000",
    "BPM_5000", "BPM_10000",
]

# Station colour palette (matches Gwyn Griffin slide aesthetic)
STATION_COLORS = {
    "CHU": "#7B68EE",   # medium slate blue
    "WWV": "#20B2AA",   # light sea green
    "WWVH": "#FF8C00",  # dark orange
    "BPM": "#DC143C",   # crimson
}


def _channel_to_station(channel: str) -> str:
    for s in ("CHU", "WWV", "WWVH", "BPM"):
        if channel.startswith(s):
            return s
    return channel


def _load_all_arrivals(
    channel: str,
    ts0: int,
    ts1: int,
    rank_filter: Optional[int],
    min_snr_db: float,
    data_root: Path,
):
    """Load all_arrivals records for one channel in [ts0, ts1]."""
    import h5py
    import numpy as np

    arr_dir = data_root / "phase2" / channel / "all_arrivals"
    if not arr_dir.exists():
        return None

    files = sorted(arr_dir.glob(f"{channel}_all_arrivals_*.h5"))
    if not files:
        return None

    # Per-station transmitter onset correction (fixed calibration constant).
    # CHU: 74ms H3E sideband filter group delay at the transmitter.
    # All others: no correction needed.
    station = _channel_to_station(channel)
    onset_ms = 74.0 if station == "CHU" else 0.0

    rows = {
        "minute_boundary": [],
        "arrival_ms": [],
        "corr_snr_db": [],
        "peak_rank": [],
        "utc_second": [],
    }

    for fpath in files:
        try:
            with h5py.File(str(fpath), "r", locking=False) as h:
                mb  = h["minute_boundary_utc"][:]
                snr = h["corr_snr_db"][:]
                rank = h["peak_rank"][:]
                arr = h["arrival_ms"][:]
                sec = h["utc_second"][:]

                # HDF5 datasets may have slightly different lengths if the
                # file was written mid-minute; truncate all to the minimum.
                n = min(len(mb), len(snr), len(rank), len(arr), len(sec))
                mb, snr, rank, arr, sec = (
                    mb[:n], snr[:n], rank[:n], arr[:n], sec[:n]
                )

                mask = (mb >= ts0) & (mb <= ts1)
                if not np.any(mask):
                    continue

                snr = snr[mask]
                rank = rank[mask]
                arr = arr[mask]
                sec = sec[mask]
                mb_f = mb[mask]

                # Apply filters
                fmask = snr >= min_snr_db
                if rank_filter is not None:
                    fmask &= (rank == rank_filter)

                rows["minute_boundary"].extend(mb_f[fmask].tolist())
                rows["arrival_ms"].extend(arr[fmask].tolist())
                rows["corr_snr_db"].extend(snr[fmask].tolist())
                rows["peak_rank"].extend(rank[fmask].tolist())
                rows["utc_second"].extend(sec[fmask].tolist())
        except Exception as e:
            logger.warning(f"Error reading {fpath}: {e}")
            continue

    if not rows["minute_boundary"]:
        return None

    # GPS-referenced ToF: no propagation model needed.
    # arrival_ms = time from minute boundary to tone onset (GPS-disciplined).
    # utc_second = Unix timestamp of the broadcast second.
    # sec_in_min = utc_second - minute_boundary_utc  (1..59 for valid CHU seconds).
    # tof_ms = arrival_ms - sec_in_min*1000 - onset_ms
    #
    # onset_ms is the fixed transmitter delay (CHU: 74ms H3E group delay).
    # This gives the true propagation delay with no model dependency.
    import numpy as np
    arr_arr = np.array(rows["arrival_ms"])
    mb_arr  = np.array(rows["minute_boundary"])
    sec_arr = np.array(rows["utc_second"])
    sec_in_min = sec_arr - mb_arr  # seconds since minute boundary
    rows["tof_ms"] = (arr_arr - sec_in_min * 1000.0 - onset_ms).tolist()
    rows["sec_in_min"] = sec_in_min.tolist()
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
    grid for contour overlay — analogous to Gwyn Griffin's cluster analysis
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

        # Filter to physically plausible GPS-referenced ToF.
        # sec_in_min must be a valid broadcast second (1-59).
        # tof_max is the physical upper bound for each station:
        #   CHU: 1521 km path, max ~30ms for any realistic ionospheric mode.
        #   WWV/WWVH/BPM: longer paths, allow more.
        # SNR cap at 40 dB removes DC/self-interference artefacts.
        SNR_CAP = 40.0
        station = _channel_to_station(channel)
        if station == "CHU":
            tof_max = 30.0
        elif station in ("WWVH", "BPM"):
            tof_max = 500.0
        else:  # WWV
            tof_max = 200.0
        valid = (
            (snr <= SNR_CAP)
            & np.isfinite(tof)
            & (tof >= 0)
            & (tof <= tof_max)
            & (sim >= 1)
            & (sim <= 59)
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

        # KDE on full (undownsampled, capped) data — x=snr, y=tof (Griffin convention)
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
    All-arrivals ToF time series (top panel of Griffin-style plot).

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

        # Same GPS-referenced filter as /arrivals endpoint
        station = _channel_to_station(channel)
        tof_max = 30.0 if station == "CHU" else (500.0 if station in ("WWVH", "BPM") else 200.0)
        valid = (
            np.isfinite(tof) & (tof >= 0) & (tof <= tof_max)
            & (sim >= 1) & (sim <= 59)
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

        # Implied F2 height from rank-0 vs rank-1 delay difference
        # Match dominant and secondary by minute, compute Δτ
        dom_dict = dict(zip(dom_ts, dom_tof))
        sec_dict = dict(zip(sec_ts, sec_tof))
        common = sorted(set(dom_dict) & set(sec_dict))

        # Distance for this channel (rough, from wwv_constants)
        DIST_KM = {
            "CHU_3330": 1650, "CHU_7850": 1650, "CHU_14670": 1650,
            "WWV_5000": 1200, "WWV_10000": 1200, "WWV_15000": 1200,
            "WWV_20000": 1200, "WWV_25000": 1200,
            "WWVH_5000": 4400, "WWVH_10000": 4400,
            "BPM_5000": 10960, "BPM_10000": 10960,
        }
        d_km = DIST_KM.get(channel, 1500)
        C_KM_S = 299792.458

        h_ts, h_vals = [], []
        for t in common:
            delta_tau_s = (sec_dict[t] - dom_dict[t]) / 1000.0
            if delta_tau_s <= 0:
                continue
            # 2-hop vs 1-hop: Δτ = 2/c * (sqrt(h²+(d/2)²) - sqrt(h²+(d/4)²))
            # Approximate: use simpler flat-Earth 1F vs 2F formula
            # τ_1F = 2/c * sqrt(h² + (d/2)²)
            # τ_2F = 4/c * sqrt(h² + (d/4)²)
            # Δτ = τ_2F - τ_1F → solve for h numerically
            # For display, use the simpler estimate: h ≈ c*Δτ/2 (vertical path approx)
            h_approx_km = C_KM_S * delta_tau_s / 2.0
            if 100 < h_approx_km < 600:
                h_ts.append(int(t))
                h_vals.append(float(h_approx_km))

        station = _channel_to_station(channel)

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
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in /ionogram/arrivals/timeseries: {e}", exc_info=True)
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
