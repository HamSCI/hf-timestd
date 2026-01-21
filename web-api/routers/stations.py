"""
Station-specific dashboard Data API.
Aggregates propagation, fusion, and status data for a single station.

Uses the BroadcastRegistry for station-centric data model with computed geometry.
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
from hf_timestd.models.broadcast import (
    BroadcastRegistry, ReceiverLocation, create_registry_from_config
)
from config import config

router = APIRouter(prefix="/stations", tags=["stations"])
logger = logging.getLogger(__name__)

# Initialize services
# Note: In a real app these might be dependency injected, but we'll instantiate for now
# consistent with other routers
_prop_service = None
_fusion_service = None
_broadcast_registry = None

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

def get_broadcast_registry() -> BroadcastRegistry:
    """Get or create the broadcast registry singleton."""
    global _broadcast_registry
    if _broadcast_registry is None:
        # Create receiver from config
        receiver = ReceiverLocation(
            callsign=config.station_metadata.get('callsign', 'UNKNOWN'),
            latitude=config.station_metadata.get('latitude', 0.0),
            longitude=config.station_metadata.get('longitude', 0.0),
            grid_square=config.station_metadata.get('grid_square', ''),
        )
        _broadcast_registry = BroadcastRegistry(receiver)
        logger.info(f"Initialized BroadcastRegistry with {_broadcast_registry.n_broadcasts} broadcasts")
    return _broadcast_registry

# Valid stations (derived from registry)
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

@router.get("/")
async def list_stations():
    """
    List all broadcast stations with summary info.
    
    Returns the 4 stations (WWV, WWVH, CHU, BPM) with their broadcasts
    and computed geometry from the receiver location.
    """
    try:
        registry = get_broadcast_registry()
        
        stations = []
        for station_name in ["WWV", "WWVH", "CHU", "BPM"]:
            station = registry.get_station(station_name)
            if not station:
                continue
            
            broadcasts = registry.get_broadcasts_for_station(station_name)
            if not broadcasts:
                continue
            
            # Use first broadcast for geometry
            b = broadcasts[0]
            
            stations.append({
                "station_id": station_name,
                "location": station.location,
                "coordinates": {"lat": station.latitude, "lon": station.longitude},
                "n_frequencies": len(station.frequencies_hz),
                "frequencies_mhz": station.frequencies_mhz,
                "distance_km": round(b.distance_km, 1),
                "azimuth_deg": round(b.azimuth_deg, 1),
                "min_propagation_ms": round(b.min_propagation_ms, 2),
            })
        
        return {
            "receiver": {
                "callsign": registry.receiver.callsign,
                "latitude": registry.receiver.latitude,
                "longitude": registry.receiver.longitude,
                "grid_square": registry.receiver.grid_square,
            },
            "source_mode": registry.source_mode.value,
            "n_stations": registry.n_stations,
            "n_broadcasts": registry.n_broadcasts,
            "n_channels": registry.n_channels,
            "stations": stations,
        }
    except Exception as e:
        logger.error(f"Error listing stations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/broadcasts")
async def list_broadcasts():
    """
    List all 17 broadcasts with computed geometry.
    
    This is the station-centric view of the data pipeline, showing
    each station+frequency combination as a distinct broadcast.
    """
    try:
        registry = get_broadcast_registry()
        
        broadcasts = []
        for broadcast_id, b in sorted(registry.broadcasts.items()):
            broadcasts.append({
                "broadcast_id": b.broadcast_id,
                "station": b.station,
                "frequency_hz": b.frequency_hz,
                "frequency_mhz": b.frequency_mhz,
                "channel_name": b.channel_name,
                "requires_discrimination": b.requires_discrimination,
                "distance_km": round(b.distance_km, 1),
                "azimuth_deg": round(b.azimuth_deg, 1),
                "min_propagation_ms": round(b.min_propagation_ms, 2),
                "tone_pattern": b.tone_pattern.value,
            })
        
        return {
            "source_mode": registry.source_mode.value,
            "n_broadcasts": len(broadcasts),
            "broadcasts": broadcasts,
        }
    except Exception as e:
        logger.error(f"Error listing broadcasts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels")
async def list_channels():
    """
    List derived channels for recording.
    
    In radiod mode: 9 channels (unique frequencies)
    In phase-engine mode: 17 channels (one per broadcast)
    """
    try:
        registry = get_broadcast_registry()
        
        channels = []
        for ch in registry.channels:
            channels.append({
                "name": ch.name,
                "frequency_hz": ch.frequency_hz,
                "frequency_mhz": ch.frequency_hz / 1e6,
                "stations": ch.stations,
                "requires_discrimination": ch.requires_discrimination,
                "target_station": ch.target_station,
                "beam_azimuth_deg": round(ch.beam_azimuth_deg, 1) if ch.beam_azimuth_deg else None,
            })
        
        return {
            "source_mode": registry.source_mode.value,
            "n_channels": len(channels),
            "channels": channels,
        }
    except Exception as e:
        logger.error(f"Error listing channels: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


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
    """
    Get station info from BroadcastRegistry.
    
    Returns computed geometry (distance, azimuth, min propagation time)
    in addition to static station data.
    """
    registry = get_broadcast_registry()
    station = registry.get_station(station_id)
    
    if not station:
        return {}
    
    # Get broadcasts for this station (with computed geometry)
    broadcasts = registry.get_broadcasts_for_station(station_id)
    
    # Use first broadcast for geometry (all same station have same geometry)
    geometry = {}
    if broadcasts:
        b = broadcasts[0]
        geometry = {
            "distance_km": round(b.distance_km, 1),
            "azimuth_deg": round(b.azimuth_deg, 1),
            "min_propagation_ms": round(b.min_propagation_ms, 2),
        }
    
    # Station descriptions (static, could be moved to config)
    descriptions = {
        "WWV": "NIST standard frequency and time signal station.",
        "WWVH": "NIST Pacific sister station to WWV.",
        "CHU": "NRC Canadian standard time station.",
        "BPM": "NTSC Chinese standard time station.",
    }
    
    return {
        "name": f"Radio Station {station_id}",
        "location": station.location,
        "coordinates": {"lat": station.latitude, "lon": station.longitude},
        "frequencies": station.frequencies_mhz,
        "description": descriptions.get(station_id, "Time signal station."),
        "tone_pattern": station.tone_pattern.value,
        "geometry": geometry,
        "broadcasts": [
            {
                "broadcast_id": b.broadcast_id,
                "frequency_mhz": b.frequency_mhz,
                "channel_name": b.channel_name,
                "requires_discrimination": b.requires_discrimination,
            }
            for b in broadcasts
        ]
    }
