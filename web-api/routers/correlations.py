"""
Correlation Analysis API endpoints.

Provides correlation analysis between space weather and HF propagation metrics.
"""

from fastapi import APIRouter, Query, HTTPException
from datetime import datetime, timedelta
from typing import Optional
import logging

from services.correlation_service import CorrelationService
from config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/correlations", tags=["correlations"])

# Initialize service
correlation_service = CorrelationService(config.data_root)

# Station coordinates (from stations router)
STATION_COORDS = {
    "WWV": (40.6776, -105.0405),
    "WWVH": (21.983, -159.765),
    "CHU": (45.2978, -75.7663),
    "BPM": (34.9479, 109.5447)
}


@router.get("/snr-solar")
async def get_snr_solar_correlation(
    station: str = Query(..., description="Station ID (WWV, WWVH, CHU, BPM)"),
    frequency: float = Query(..., description="Frequency in MHz"),
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Analyze correlation between SNR and solar zenith angle.
    
    Expected: Strong positive correlation (r > 0.5) for F-layer propagation.
    SNR should increase when sun is higher (lower zenith angle).
    
    Returns:
        Correlation statistics, scatter plot data, and interpretation
    """
    try:
        station = station.upper()
        if station not in STATION_COORDS:
            raise HTTPException(status_code=400, detail=f"Invalid station: {station}")
        
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        
        # Get receiver coordinates from config
        rx_coords = (config.station_metadata['latitude'], config.station_metadata['longitude'])
        tx_coords = STATION_COORDS[station]
        
        result = correlation_service.analyze_snr_solar_zenith(
            station=station,
            frequency=frequency,
            start=start,
            end=end,
            station_coords=tx_coords,
            rx_coords=rx_coords
        )
        
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing SNR-solar correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sid-detection")
async def get_sid_correlation(
    hours: int = Query(24, ge=1, le=168, description="Hours of history")
):
    """
    Detect Sudden Ionospheric Disturbance (SID) events.
    
    Correlates X-ray flares with SNR drops across all frequencies.
    SID events cause increased D-layer absorption, particularly on lower frequencies.
    
    Returns:
        List of SID events with correlated SNR drops on affected channels
    """
    try:
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        
        result = correlation_service.detect_sid_correlation(start, end)
        
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error detecting SID correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/tec-f107")
async def get_tec_f107_correlation(
    station: str = Query(..., description="Station ID (WWV, WWVH, CHU, BPM)"),
    days: int = Query(30, ge=7, le=90, description="Days of history")
):
    """
    Analyze correlation between TEC and F10.7 solar flux.
    
    Expected: Positive correlation (r > 0.6) between daily F10.7 and daytime TEC.
    Higher solar flux → more EUV radiation → higher F-layer ionization → higher TEC.
    
    Returns:
        TEC-F10.7 correlation statistics and trend analysis
    """
    try:
        station = station.upper()
        if station not in STATION_COORDS:
            raise HTTPException(status_code=400, detail=f"Invalid station: {station}")
        
        result = correlation_service.analyze_tec_f107_correlation(
            station=station,
            days=days
        )
        
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing TEC-F10.7 correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/propagation-kp")
async def get_propagation_kp_correlation(
    hours: int = Query(72, ge=24, le=168, description="Hours of history")
):
    """
    Analyze relationship between propagation modes and Kp index.
    
    Expected effects:
    - High Kp (>5): Auroral absorption on high-latitude paths (CHU)
    - High Kp: Enhanced E-layer, F-layer irregularities
    - High Kp: Increased scintillation and fading
    
    Returns:
        Propagation statistics binned by Kp level (quiet/unsettled/storm)
    """
    try:
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        
        result = correlation_service.analyze_propagation_mode_kp(start, end)
        
        if 'error' in result:
            raise HTTPException(status_code=404, detail=result['error'])
        
        return result
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error analyzing propagation-Kp correlation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/summary")
async def get_correlation_summary(
    station: Optional[str] = Query(None, description="Station ID (optional)"),
    hours: int = Query(24, ge=6, le=168, description="Hours of history")
):
    """
    Get comprehensive correlation summary.
    
    Combines multiple correlation analyses into a single dashboard view:
    - SID event detection
    - Propagation-Kp relationship
    - Current space weather impact assessment
    
    Returns:
        Multi-faceted correlation summary for dashboard display
    """
    try:
        end = datetime.utcnow()
        start = end - timedelta(hours=hours)
        
        # Get SID correlation
        sid_result = correlation_service.detect_sid_correlation(start, end)
        
        # Get propagation-Kp correlation
        kp_result = correlation_service.analyze_propagation_mode_kp(start, end)
        
        summary = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'period': {
                'start': start.isoformat() + 'Z',
                'end': end.isoformat() + 'Z',
                'hours': hours
            },
            'sid_events': {
                'detected': sid_result.get('sid_events_detected', 0),
                'correlated': sid_result.get('correlated_events', 0),
                'recent_events': sid_result.get('events', [])[:5]  # Last 5
            },
            'geomagnetic_effects': {
                'kp_bins': kp_result.get('kp_bins', {}),
                'interpretation': kp_result.get('interpretation', '')
            },
            'recommendations': []
        }
        
        # Add recommendations based on findings
        if sid_result.get('correlated_events', 0) > 0:
            summary['recommendations'].append({
                'type': 'SID_DETECTED',
                'message': 'Recent solar flares detected with propagation impacts',
                'action': 'Monitor lower frequencies for absorption'
            })
        
        if kp_result.get('kp_bins', {}).get('storm', {}).get('channels'):
            summary['recommendations'].append({
                'type': 'GEOMAGNETIC_STORM',
                'message': 'Geomagnetic storm conditions affecting propagation',
                'action': 'Expect degraded high-latitude paths (CHU)'
            })
        
        return summary
    
    except Exception as e:
        logger.error(f"Error generating correlation summary: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
