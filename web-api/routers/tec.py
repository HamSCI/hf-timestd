"""
TEC (Total Electron Content) API endpoints for v6.5.0.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import logging
import glob
from pathlib import Path

from services.tec_service import TECService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tec", tags=["ionosphere"])

# Initialize service
tec_service = TECService(config.data_root)


def _parse_relative_time(s: str, now: datetime) -> datetime:
    """Parse ISO8601 or relative time string like '-6h', '-1d'."""
    if s == "now":
        return now
    if s.startswith('-'):
        val = int(s[1:-1])
        unit = s[-1]
        if unit == 'h':
            return now - timedelta(hours=val)
        elif unit == 'd':
            return now - timedelta(days=val)
        elif unit == 'm':
            return now - timedelta(minutes=val)
        raise ValueError(f"Unknown unit in '{s}'")
    return datetime.fromisoformat(s.replace('Z', ''))


@router.get("/dtec")
async def get_dtec_timeseries(
    start: str = Query("-6h", description="Start time (ISO8601 or relative like '-6h', '-1d')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    station: Optional[str] = Query(None, description="Filter by station (WWV, WWVH, CHU, BPM)"),
    freq_mhz: Optional[float] = Query(None, description="Filter by frequency in MHz"),
    min_snr_db: float = Query(6.0, description="Minimum mean_snr_db to include"),
    downsample: int = Query(1, description="Keep every Nth point (1=all, 5=every 5th)"),
):
    """
    Multi-station carrier-phase dTEC time series.

    Returns per-station, per-frequency dtec_rate_tecu_per_s and integrated
    dtec_mean_tecu for the requested window.  Suitable for a multi-path
    overlay chart showing correlated TID signatures across all paths.

    Each series is keyed as '<STATION>_<freq>MHz', e.g. 'WWV_10.0MHz'.
    """
    try:
        import h5py
        import numpy as np

        now = datetime.utcnow()
        t0 = _parse_relative_time(start, now)
        t1 = _parse_relative_time(end, now)
        ts0 = int(t0.timestamp())
        ts1 = int(t1.timestamp())

        dtec_dir = Path(config.data_root) / 'phase2' / 'science' / 'dtec'
        if not dtec_dir.exists():
            return {"status": "no_data", "message": "dTEC directory not found", "series": {}}

        # Collect files covering the requested range (one file per day)
        files = sorted(dtec_dir.glob("AGGREGATED_dtec_*.h5"))
        if not files:
            return {"status": "no_data", "message": "No dTEC files found", "series": {}}

        series: dict = {}
        n_total = 0

        for fpath in files:
            try:
                with h5py.File(str(fpath), 'r', locking=False) as h:
                    mb = h['minute_boundary'][:]
                    mask = (mb >= ts0) & (mb <= ts1)
                    if not np.any(mask):
                        continue

                    stations_arr = h['station'][:][mask].astype(str)
                    freqs_arr = h['frequency_mhz'][:][mask]
                    dtec_rate = h['dtec_rate_tecu_per_s'][:][mask]
                    dtec_mean = h['dtec_mean_tecu'][:][mask]
                    snr_arr = h['mean_snr_db'][:][mask]
                    anchored = h['is_anchored'][:][mask]
                    mb_filt = mb[mask]

                    # Apply filters
                    snr_mask = snr_arr >= min_snr_db
                    if station:
                        snr_mask &= (stations_arr == station.upper())
                    if freq_mhz is not None:
                        snr_mask &= (np.abs(freqs_arr - freq_mhz) < 0.01)

                    for s in np.unique(stations_arr[snr_mask]):
                        for f in np.unique(freqs_arr[snr_mask & (stations_arr == s)]):
                            key = f"{s}_{f:.1f}MHz"
                            row_mask = snr_mask & (stations_arr == s) & (np.abs(freqs_arr - f) < 0.01)
                            idx = np.where(row_mask)[0]
                            if downsample > 1:
                                idx = idx[::downsample]
                            if key not in series:
                                series[key] = {
                                    "station": s,
                                    "frequency_mhz": float(f),
                                    "timestamps": [],
                                    "dtec_rate_tecu_per_s": [],
                                    "dtec_mean_tecu": [],
                                    "mean_snr_db": [],
                                    "is_anchored": [],
                                }
                            series[key]["timestamps"].extend(mb_filt[idx].tolist())
                            series[key]["dtec_rate_tecu_per_s"].extend(dtec_rate[idx].tolist())
                            series[key]["dtec_mean_tecu"].extend(dtec_mean[idx].tolist())
                            series[key]["mean_snr_db"].extend(snr_arr[idx].tolist())
                            series[key]["is_anchored"].extend(anchored[idx].tolist())
                            n_total += len(idx)
            except Exception as e:
                logger.warning(f"Error reading dTEC file {fpath}: {e}")
                continue

        # Sort each series by timestamp
        for key in series:
            order = sorted(range(len(series[key]["timestamps"])),
                           key=lambda i: series[key]["timestamps"][i])
            for field in ("timestamps", "dtec_rate_tecu_per_s", "dtec_mean_tecu",
                          "mean_snr_db", "is_anchored"):
                series[key][field] = [series[key][field][i] for i in order]

        return {
            "status": "ok",
            "time_range": {"start": t0.isoformat() + "Z", "end": t1.isoformat() + "Z"},
            "n_series": len(series),
            "n_points_total": n_total,
            "series": series,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in /tec/dtec: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/current")
async def get_current_tec():
    """
    Get the most recent TEC estimates.
    
    Returns current TEC values for all monitored propagation paths.
    """
    try:
        data = tec_service.get_current_tec()
        
        if data is None:
            return {
                "status": "no_data",
                "message": "No TEC data available",
                "timestamp": datetime.utcnow().isoformat() + 'Z'
            }
        
        return {
            "status": "ok",
            "timestamp": data.get('timestamp', ''),
            "paths": data.get('paths', {}),
            "n_paths": len(data.get('paths', {}))
        }
    
    except Exception as e:
        logger.error(f"Error getting current TEC: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/history")
async def get_tec_history(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    station: Optional[str] = Query(None, description="Filter by station (WWV, WWVH, CHU, BPM)")
):
    """
    Get TEC history for a time range.
    
    Returns TEC values over time for all paths or filtered by station.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            duration_str = start[1:]
            if duration_str.endswith('h'):
                hours = int(duration_str[:-1])
                start_time = end_time - timedelta(hours=hours)
            elif duration_str.endswith('d'):
                days = int(duration_str[:-1])
                start_time = end_time - timedelta(days=days)
            else:
                raise ValueError(f"Invalid duration format: {start}")
        else:
            start_time = datetime.fromisoformat(start.replace('Z', ''))
        
        data = tec_service.get_tec_history(start_time, end_time, station=station)
        
        return {
            "status": "ok",
            **data
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting TEC history: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/station/{station}")
async def get_tec_by_station(
    station: str,
    hours: int = Query(24, description="Number of hours of history")
):
    """
    Get TEC data for a specific station.
    
    Returns TEC values for all frequencies from the specified station.
    """
    try:
        station = station.upper()
        if station not in ['WWV', 'WWVH', 'CHU', 'BPM']:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid station: {station}. Must be WWV, WWVH, CHU, or BPM"
            )
        
        data = tec_service.get_tec_by_station(station, hours)
        
        return {
            "status": "ok",
            "station": station,
            **data
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting TEC for station {station}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
