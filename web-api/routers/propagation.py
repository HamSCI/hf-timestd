"""
Propagation and ionospheric analysis API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta, timezone
from typing import Optional, List
import logging

from services.propagation_service import PropagationService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/propagation", tags=["propagation"])

# Initialize service
propagation_service = PropagationService(config.data_root)

# Lazy-init HFPropagationModel for live prediction endpoint
_prop_model = None

def _get_prop_model():
    """Get or create cached HFPropagationModel instance."""
    global _prop_model
    if _prop_model is None:
        try:
            from hf_timestd.core.propagation_model import HFPropagationModel
            lat = config.station.get('latitude', 0.0)
            lon = config.station.get('longitude', 0.0)
            if lat == 0.0 and lon == 0.0:
                logger.warning("No receiver coordinates in config — model predictions will be inaccurate")
            _prop_model = HFPropagationModel(
                receiver_lat=lat,
                receiver_lon=lon,
                enable_realtime=True
            )
        except Exception as e:
            logger.error(f"Failed to initialize HFPropagationModel: {e}")
    return _prop_model


@router.get("/conditions")
@router.get("/current")  # Alias for frontend compatibility
async def get_current_conditions():
    """
    Get current propagation conditions.
    
    Returns summary of propagation modes, MUF estimates, and signal quality
    from the last hour of measurements.
    """
    try:
        conditions = propagation_service.get_current_conditions()
        
        if conditions is None:
            raise HTTPException(
                status_code=404,
                detail="No recent propagation data available"
            )
        
        return conditions
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting propagation conditions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/timeline")
async def get_mode_timeline(
    start: str = Query("-6h", description="Start time (ISO8601 or relative like '-6h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')"),
    station: Optional[str] = Query(None, description="Filter by station (WWV, WWVH, CHU, BPM)")
):
    """
    Get propagation mode timeline.
    
    Returns time series of propagation modes observed across all channels.
    """
    try:
        logger.info(f"Timeline request: start={start}, end={end}, station={station}")
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        logger.info(f"  Fetching timeline from {start_time} to {end_time}")
        timeline = propagation_service.get_mode_timeline(start_time, end_time, station)
        logger.info(f"  Timeline fetch complete. Count: {timeline.get('count') if timeline else 'None'}")
        
        if timeline is None:
            raise HTTPException(
                status_code=404,
                detail="No propagation timeline data available"
            )
        
        return timeline
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting propagation timeline: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/tec")
async def get_tec_summary(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')")
):
    """
    Get TEC (Total Electron Content) summary.
    
    Returns HF-derived TEC measurements and trends.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        tec_data = propagation_service.get_tec_summary(start_time, end_time)
        
        if tec_data is None:
            raise HTTPException(
                status_code=404,
                detail="No TEC data available"
            )
        
        return tec_data
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting TEC summary: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/test-signals")
async def get_test_signals(
    start: str = Query("-24h", description="Start time (ISO8601 or relative like '-24h')"),
    end: str = Query("now", description="End time (ISO8601 or 'now')")
):
    """
    Get WWV/WWVH test signal analysis data.
    
    Returns test signal detections from minutes 8 (WWV) and 44 (WWVH) with
    ionospheric metrics including field strength, delay spread, and scintillation.
    """
    try:
        # Parse time range
        if end == "now":
            end_time = datetime.utcnow()
        else:
            end_time = datetime.fromisoformat(end.replace('Z', ''))
        
        if start.startswith('-'):
            # Relative time
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
        
        test_signal_data = propagation_service.get_test_signal_summary(start_time, end_time)
        
        if test_signal_data is None:
            raise HTTPException(
                status_code=404,
                detail="No test signal data available"
            )
        
        return test_signal_data
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting test signal data: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/model/predict")
async def get_model_prediction(
    station: str = Query(..., description="Station name (WWV, WWVH, CHU, BPM)"),
    frequency_mhz: float = Query(..., description="Broadcast frequency in MHz"),
    time: Optional[str] = Query(None, description="UTC time (ISO8601). Default: now")
):
    """
    Get live HFPropagationModel prediction for a station/frequency.
    
    Returns predicted delays for all propagation modes, ionospheric parameters,
    model data source, and uncertainty estimates. Useful for diagnosing model
    behavior and comparing against observed arrivals.
    """
    model = _get_prop_model()
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="HFPropagationModel not available"
        )
    
    station = station.upper()
    if station not in ('WWV', 'WWVH', 'CHU', 'BPM'):
        raise HTTPException(status_code=400, detail=f"Unknown station: {station}")
    
    if time is not None:
        try:
            utc_time = datetime.fromisoformat(time.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid time format: {time}")
    else:
        utc_time = datetime.now(timezone.utc)
    
    try:
        prediction = model.predict(station, frequency_mhz, utc_time)
        
        # Build response with all mode details
        modes = []
        if hasattr(prediction, 'all_modes') and prediction.all_modes:
            for m in prediction.all_modes:
                modes.append({
                    'mode': m.get('mode', ''),
                    'delay_ms': m.get('delay_ms', 0),
                    'uncertainty_ms': m.get('uncertainty_ms', 0),
                    'elevation_deg': m.get('elevation_deg', 0),
                })
        
        return {
            'station': station,
            'frequency_mhz': frequency_mhz,
            'utc_time': utc_time.isoformat(),
            'distance_km': prediction.distance_km,
            'primary_delay_ms': prediction.primary_delay_ms,
            'primary_mode': prediction.primary_mode,
            'primary_uncertainty_ms': prediction.primary_uncertainty_ms,
            'data_source': prediction.data_source,
            'model_confidence': prediction.model_confidence,
            'iono_params': {
                'hmF2_km': getattr(prediction, 'hmF2_km', None),
                'foF2_MHz': getattr(prediction, 'foF2_MHz', None),
                'TEC_TECU': getattr(prediction, 'TEC_TECU', None),
            },
            'all_modes': modes,
        }
    
    except Exception as e:
        logger.error(f"Model prediction failed: {e}")
        raise HTTPException(status_code=500, detail=f"Prediction failed: {e}")


@router.get("/model/all-stations")
async def get_all_station_predictions(
    time: Optional[str] = Query(None, description="UTC time (ISO8601). Default: now")
):
    """
    Get live model predictions for all station/frequency combinations.
    
    Returns a summary of current propagation predictions across all broadcasts,
    including which data source the model is using and confidence levels.
    """
    model = _get_prop_model()
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="HFPropagationModel not available"
        )
    
    if time is not None:
        try:
            utc_time = datetime.fromisoformat(time.replace('Z', '+00:00'))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid time format: {time}")
    else:
        utc_time = datetime.now(timezone.utc)
    
    broadcasts = {
        'WWV': [2.5, 5.0, 10.0, 15.0, 20.0, 25.0],
        'WWVH': [2.5, 5.0, 10.0, 15.0],
        'CHU': [3.33, 7.85, 14.67],
        'BPM': [2.5, 5.0, 10.0, 15.0],
    }
    
    results = []
    for station, freqs in broadcasts.items():
        for freq in freqs:
            try:
                pred = model.predict(station, freq, utc_time)
                results.append({
                    'station': station,
                    'frequency_mhz': freq,
                    'primary_delay_ms': round(pred.primary_delay_ms, 3),
                    'primary_mode': pred.primary_mode,
                    'uncertainty_ms': round(pred.primary_uncertainty_ms, 3),
                    'data_source': pred.data_source,
                    'model_confidence': round(pred.model_confidence, 3),
                })
            except Exception as e:
                results.append({
                    'station': station,
                    'frequency_mhz': freq,
                    'error': str(e),
                })
    
    return {
        'utc_time': utc_time.isoformat(),
        'receiver_lat': config.station.get('latitude', 0.0),
        'receiver_lon': config.station.get('longitude', 0.0),
        'predictions': results,
    }


@router.get("/model/iono-status")
async def get_iono_service_status():
    """
    Get IonoDataService status — data freshness, sources, and cache state.
    """
    try:
        from hf_timestd.core.iono_data_service import IonoDataService
        service = IonoDataService.get_instance()
        
        status = {
            'running': service._running if hasattr(service, '_running') else False,
            'cache_dir': str(service.cache_dir),
            'enable_wamipe': service.enable_wamipe,
            'enable_giro': service.enable_giro,
            'has_wamipe_grid': service._current_grid is not None if hasattr(service, '_current_grid') else False,
            'giro_station_count': len(service._giro_stations) if hasattr(service, '_giro_stations') else 0,
            'giro_measurement_count': len(service._giro_measurements) if hasattr(service, '_giro_measurements') else 0,
        }
        
        # Add fetch timestamps if available
        if hasattr(service, '_last_wamipe_fetch'):
            status['last_wamipe_fetch'] = service._last_wamipe_fetch.isoformat() if service._last_wamipe_fetch else None
        if hasattr(service, '_last_giro_fetch'):
            status['last_giro_fetch'] = service._last_giro_fetch.isoformat() if service._last_giro_fetch else None
        
        return status
    
    except Exception as e:
        logger.error(f"IonoDataService status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
