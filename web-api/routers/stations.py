"""
Station-specific dashboard Data API.
Aggregates propagation, fusion, and status data for a single station.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import numpy as np

from fastapi import APIRouter, HTTPException, Query

# Services
from services.propagation_service import PropagationService
from services.fusion_service import FusionService
from hf_timestd.core.solar_zenith_calculator import calculate_midpoint, solar_position
from config import config

router = APIRouter(prefix="/stations", tags=["stations"])
logger = logging.getLogger(__name__)

# Initialize services
# Note: In a real app these might be dependency injected, but we'll instantiate for now
# consistent with other routers
_prop_service = None
_fusion_service = None

def get_prop_service():
    global _prop_service
    if _prop_service is None:
        _prop_service = PropagationService(config.data_root)
    return _prop_service

def get_fusion_service():
    global _fusion_service
    if _fusion_service is None:
        _fusion_service = FusionService(config.data_root / "phase2" / "fusion")
    return _fusion_service

# Valid stations
STATIONS = {"WWV", "WWVH", "CHU", "BPM"}

def sanitize_for_json(obj):
    """Recursively convert numpy types to Python native types."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    elif isinstance(obj, (np.integer, int)):
        return int(obj)
    elif isinstance(obj, (np.floating, float)):
        return float(obj) if not np.isnan(obj) and not np.isinf(obj) else None
    elif isinstance(obj, (np.ndarray,)):
        return sanitize_for_json(obj.tolist())
    elif isinstance(obj, np.bool_):
        return bool(obj)
    return obj

@router.get("/{station_id}")
async def get_station_dashboard(station_id: str):
    """
    Get comprehensive dashboard data for a specific station.
    """
    station_id = station_id.upper()
    if station_id not in STATIONS:
        raise HTTPException(status_code=404, detail=f"Station {station_id} not found")

    try:
        prop = get_prop_service()
        fusion = get_fusion_service()
        
        # 1. Get current propagation conditions
        # We need to filter the global conditions for this station
        conditions = prop.get_current_conditions()
        current_status = None
        if conditions and 'broadcasts' in conditions:
            # Filter for this station
            station_broadcasts = [
                b for b in conditions['broadcasts'] 
                if b['station'] == station_id
            ]
            
            # Calculate aggregate stats
            if station_broadcasts:
                avg_snr = sum(b['avg_snr_db'] for b in station_broadcasts if b['avg_snr_db'] is not None)
                count_snr = sum(1 for b in station_broadcasts if b['avg_snr_db'] is not None)
                
                current_status = {
                    'active_frequencies': [b['frequency_mhz'] for b in station_broadcasts],
                    'avg_snr_db': avg_snr / count_snr if count_snr > 0 else None,
                    'dominant_mode': station_broadcasts[0]['dominant_mode'] if station_broadcasts else 'UNKNOWN',
                    'broadcasts': station_broadcasts
                }

        # 2. Get Fusion Data (System-level view of this station)
        fusion_latest = fusion.get_latest()
        fusion_stats = None
        if fusion_latest:
            # Map fusion keys to station
            # e.g. wwv_mean_ms, wwv_count
            key_prefix = station_id.lower()
            fusion_stats = {
                'offset_ms': fusion_latest.get(f'{key_prefix}_mean_ms'),
                'count': fusion_latest.get(f'{key_prefix}_count'),
                'std_dev_ms': fusion_latest.get(f'{key_prefix}_intra_std_ms'),
                'used_in_fusion': station_id in (fusion_latest.get('stations_used') or []),
                'global_d_clock': fusion_latest.get('d_clock_ms'),
                'global_uncertainty': fusion_latest.get('uncertainty_ms')
            }

        # 3. Get TEC Data (Ionosphere)
        # Last 24 hours
        end = datetime.utcnow()
        start = end - timedelta(hours=24)
        tec_summary = prop.get_tec_summary(start, end)
        tec_stats = None
        if tec_summary and 'paths' in tec_summary and station_id in tec_summary['paths']:
            path_data = tec_summary['paths'][station_id]
            if path_data.get('mean_tec'):
                tec_stats = {
                    'current_tec': path_data['tec_tecu'][-1] if path_data['tec_tecu'] else None,
                    'mean_24h': path_data['mean_tec'],
                    'min_24h': path_data['min_tec'],
                    'max_24h': path_data['max_tec']
                }

        # 4. Static Station Info (from config/metadata if available, or hardcoded defaults)
        # We'll use a simple lookup for now
        info = get_station_info(station_id)

        response = {
            "station_id": station_id,
            "info": info,
            "status": current_status,
            "fusion": fusion_stats,
            "ionosphere": tec_stats,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        return sanitize_for_json(response)

    except Exception as e:
        logger.error(f"Error serving station dashboard for {station_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{station_id}/history")
async def get_station_history(
    station_id: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Get historical data for charts.
    """
    station_id = station_id.upper()
    if station_id not in STATIONS:
        raise HTTPException(status_code=404, detail=f"Station {station_id} not found")

    try:
        prop = get_prop_service()
        
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)

        # 1. Mode/SNR Timeline
        timeline = prop.get_mode_timeline(start, end, station=station_id)
        
        # 2. TEC History
        tec_history = None
        tec_summary = prop.get_tec_summary(start, end)
        if tec_summary and 'paths' in tec_summary and station_id in tec_summary['paths']:
            tec_history = tec_summary['paths'][station_id]

        # 3. Solar Zenith at Path Midpoint
        solar_data = None
        try:
            info = get_station_info(station_id)
            # Receiver location (from config)
            rx_lat = config.station_metadata['latitude']
            rx_lon = config.station_metadata['longitude']
            
            # Transmitter location (from info)
            if info and 'coordinates' in info:
                tx_lat = info['coordinates']['lat']
                tx_lon = info['coordinates']['lon']
                
                # Calculate midpoint
                mid_lat, mid_lon = calculate_midpoint(rx_lat, rx_lon, tx_lat, tx_lon)
                
                # Generate time series matching the requested range
                # We'll calculate every 15 minutes to save compute, client can interpolate
                solar_timestamps = []
                solar_elevations = []
                
                curr = start
                while curr <= end:
                    az, el = solar_position(curr, mid_lat, mid_lon)
                    solar_timestamps.append(curr.isoformat() + 'Z')
                    solar_elevations.append(el)
                    curr += timedelta(minutes=15)
                    
                solar_data = {
                    'timestamps': solar_timestamps,
                    'elevation_deg': solar_elevations,
                    'midpoint': {'lat': mid_lat, 'lon': mid_lon}
                }
        except Exception as e:
            logger.warning(f"Failed to calculate solar zenith: {e}")

        response = {
            "station_id": station_id,
            "range": {"start": start, "end": end},
            "timeline": timeline,
            "tec": tec_history,
            "solar": solar_data
        }
        return sanitize_for_json(response)

    except Exception as e:
        logger.error(f"Error serving station history for {station_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def get_station_info(station_id: str) -> Dict[str, Any]:
    """Helper to get static info."""
    # This could come from a database or config file
    info_map = {
        "WWV": {
            "name": "NIST Radio Station WWV",
            "location": "Fort Collins, Colorado, USA",
            "coordinates": {"lat": 40.6776, "lon": -105.0405},
            "frequencies": [2.5, 5, 10, 15, 20, 25],
            "description": "Standard frequency and time signal station."
        },
        "WWVH": {
            "name": "NIST Radio Station WWVH",
            "location": "Kekaha, Kauai, Hawaii, USA",
            "coordinates": {"lat": 21.983, "lon": -159.765},
            "frequencies": [2.5, 5, 10, 15],
            "description": "Pacific sister station to WWV."
        },
        "CHU": {
            "name": "NRC Radio Station CHU",
            "location": "Ottawa, Ontario, Canada",
            "coordinates": {"lat": 45.2978, "lon": -75.7663},
            "frequencies": [3.33, 7.85, 14.67],
            "description": "Canadian standard time station."
        },
        "BPM": {
            "name": "NTSC Radio Station BPM",
            "location": "Pucheng, Shaanxi, China",
            "coordinates": {"lat": 34.9479, "lon": 109.5447},
            "frequencies": [2.5, 5, 10, 15],
            "description": "Chinese standard time station."
        }
    }
    return info_map.get(station_id, {})
