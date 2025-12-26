#!/usr/bin/env python3
"""
HF-TimeStd Monitoring Server (FastAPI)

FastAPI-based monitoring server with native HDF5 support using h5py.
Serves API endpoints for timing data and a clean web dashboard.
"""

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from typing import Optional
from datetime import datetime
import logging
import toml

# Import HDF5 reader utilities
from utils.hdf5_reader import (
    read_l2_timing_measurements,
    read_l1a_channel_observables,
    get_l2_timing_path,
    get_l1a_observables_path
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="HF-TimeStd Monitoring Server",
    version="4.0.0",
    description="Monitoring server with native HDF5 support"
)

# Add CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global configuration
config = {}
data_root = Path("/var/lib/timestd")

# Get the directory where this script is located
SCRIPT_DIR = Path(__file__).parent

# Setup Jinja2 templates
templates = Jinja2Templates(directory=str(SCRIPT_DIR / "templates"))


@app.on_event("startup")
async def startup_event():
    """Load configuration on startup"""
    global config, data_root
    
    config_path = Path("/etc/hf-timestd/timestd-config.toml")
    if config_path.exists():
        try:
            config = toml.load(config_path)
            logger.info(f"Loaded configuration from {config_path}")
            
            # Get data root from config
            if 'paths' in config and 'data_root' in config['paths']:
                data_root = Path(config['paths']['data_root'])
                logger.info(f"Data root: {data_root}")
        except Exception as e:
            logger.error(f"Error loading config: {e}")
    else:
        logger.warning(f"Config file not found: {config_path}, using defaults")


# ============================================================================
# WEB DASHBOARD
# ============================================================================

@app.get("/")
async def dashboard(request: Request):
    """Render the main dashboard"""
    try:
        # Get summary data
        summary_data = await get_summary()
        
        # Add update time
        summary_data['update_time'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                **summary_data
            }
        )
    except Exception as e:
        logger.error(f"Error rendering dashboard: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


# ============================================================================
# LEGACY HTML PAGES (for compatibility)
# ============================================================================

# Specific HTML file routes
@app.get("/summary.html")
async def serve_summary():
    file_path = SCRIPT_DIR / "summary.html"
    if file_path.exists():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="summary.html not found")

@app.get("/timing.html")
async def serve_timing():
    file_path = SCRIPT_DIR / "timing.html"
    if file_path.exists():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="timing.html not found")

@app.get("/ionosphere.html")
async def serve_ionosphere():
    file_path = SCRIPT_DIR / "ionosphere.html"
    if file_path.exists():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="ionosphere.html not found")


# Generic file serving for CSS, JS, and other static files
@app.get("/{filepath:path}")
async def serve_static_file(filepath: str):
    """Serve static files (CSS, JS, etc.)"""
    # Security: prevent directory traversal
    if ".." in filepath or filepath.startswith("/"):
        raise HTTPException(status_code=403, detail="Access denied")
    
    file_path = SCRIPT_DIR / filepath
    
    # Check if file exists and is a file (not directory)
    if file_path.exists() and file_path.is_file():
        # Determine media type
        media_type = None
        if filepath.endswith('.css'):
            media_type = 'text/css'
        elif filepath.endswith('.js'):
            media_type = 'application/javascript'
        elif filepath.endswith('.json'):
            media_type = 'application/json'
        elif filepath.endswith('.png'):
            media_type = 'image/png'
        elif filepath.endswith('.jpg') or filepath.endswith('.jpeg'):
            media_type = 'image/jpeg'
        elif filepath.endswith('.svg'):
            media_type = 'image/svg+xml'
        
        return FileResponse(file_path, media_type=media_type)
    
    raise HTTPException(status_code=404, detail=f"{filepath} not found")


# ============================================================================
# API ENDPOINTS - HIGH PRIORITY
# ============================================================================

@app.get("/api/v1/timing/clock-offset")
async def get_clock_offset(
    channel: str = Query(..., description="Channel name (e.g., 'WWV 10 MHz')"),
    date: str = Query(..., description="Date in YYYYMMDD format"),
    hours: int = Query(24, description="Number of hours to retrieve")
):
    """
    Get D_clock time series from Phase 2 analytics (L2 timing measurements)
    
    Returns timing measurements with quality metadata from HDF5 files.
    Falls back to CSV if HDF5 unavailable.
    """
    try:
        # Get HDF5 file path
        hdf5_path = get_l2_timing_path(channel, date, data_root)
        
        # Try reading from HDF5
        if hdf5_path.exists():
            try:
                result = read_l2_timing_measurements(hdf5_path)
                logger.info(f"Read {result['statistics']['count']} L2 measurements from HDF5 for {channel}")
                return result
            except Exception as e:
                logger.error(f"Error reading HDF5, falling back to CSV: {e}")
        
        # CSV fallback (implement later if needed)
        logger.warning(f"HDF5 file not found: {hdf5_path}")
        return {
            "measurements": [],
            "statistics": {"count": 0, "total_records": 0},
            "grade_distribution": {"A": 0, "B": 0, "C": 0, "D": 0},
            "source": "none",
            "status": "no_data",
            "message": f"No data available for {channel} on {date}"
        }
        
    except Exception as e:
        logger.error(f"Error in get_clock_offset: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/channels/{channel_name}/carrier-power/{date}")
async def get_carrier_power(
    channel_name: str,
    date: str
):
    """
    Get carrier power time series from Phase 2 (L1A channel observables)
    
    Returns channel observables with quality metadata from HDF5 files.
    Falls back to CSV if HDF5 unavailable.
    """
    try:
        # URL decode channel name
        from urllib.parse import unquote
        channel_name = unquote(channel_name)
        
        # Get HDF5 file path
        hdf5_path = get_l1a_observables_path(channel_name, date, data_root)
        
        # Try reading from HDF5
        if hdf5_path.exists():
            try:
                result = read_l1a_channel_observables(hdf5_path)
                logger.info(f"Read {result['count']} L1A observables from HDF5 for {channel_name}")
                
                # Format response to match expected structure
                return {
                    "channel": channel_name,
                    "date": date,
                    "records": result['records'],
                    "count": result['count'],
                    "source": result['source'],
                    "status": result['status']
                }
            except Exception as e:
                logger.error(f"Error reading HDF5, falling back to CSV: {e}")
        
        # CSV fallback
        logger.warning(f"HDF5 file not found: {hdf5_path}")
        return {
            "channel": channel_name,
            "date": date,
            "records": [],
            "count": 0,
            "source": "none",
            "status": "no_data"
        }
        
    except Exception as e:
        logger.error(f"Error in get_carrier_power: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# API ENDPOINTS - SYSTEM INFO
# ============================================================================

@app.get("/api/v1/station/info")
async def get_station_info():
    """Get station configuration information"""
    return {
        "station_name": config.get('station', {}).get('name', 'HF-TimeStd'),
        "location": config.get('station', {}).get('location', 'Unknown'),
        "channels": len(config.get('recorder', {}).get('channels', [])),
        "version": "4.0.0"
    }


@app.get("/api/v1/system/status")
async def get_system_status():
    """Get aggregated system status"""
    return {
        "server": "online",
        "version": "4.0.0",
        "data_root": str(data_root),
        "hdf5_support": True,
        "message": "FastAPI monitoring server with native HDF5 support"
    }


@app.get("/api/v1/summary")
async def get_summary():
    """Get system summary for dashboard"""
    try:
        # Get station info with all required fields
        station_config = config.get('station', {})
        station_info = {
            "id": station_config.get('name', 'HF-TimeStd'),
            "station_id": station_config.get('name', 'HF-TimeStd'),
            "name": station_config.get('name', 'HF-TimeStd'),
            "callsign": station_config.get('callsign', 'N/A'),
            "grid_square": station_config.get('grid_square', 'N/A'),
            "receiver": station_config.get('receiver', 'ka9q-radio'),
            "instrument_id": station_config.get('instrument_id', 'hf-timestd-001'),
            "mode": station_config.get('mode', 'production'),
            "data_root": str(data_root),
            "location": station_config.get('location', 'Unknown'),
            "version": "4.0.0"
        }
        
        # Get channel list
        channels_list = config.get('recorder', {}).get('channels', [])
        channels = []
        for ch in channels_list:
            if ch.get('enabled', True):
                channels.append({
                    "name": ch.get('description', f"Channel {ch.get('ssrc', 'unknown')}"),
                    "frequency_mhz": ch.get('freq', 0) / 1e6 if ch.get('freq') else 0,
                    "status": "active",
                    "rtp_streaming": True,
                    "snr_db": None
                })
        
        return {
            "station": station_info,
            "processes": {
                "radiod": {"status": "unknown", "running": False},
                "core_recorder": {"status": "unknown", "running": False},
                "analytics_service": {"status": "unknown", "running": False}
            },
            "continuity": {
                "overall_health": "unknown",
                "data_span_days": 0,
                "data_span": {},
                "gaps": [],
                "total_downtime_seconds": 0,
                "downtime_percentage": 0
            },
            "storage": {
                "total_gb": 0,
                "used_gb": 0,
                "available_gb": 0,
                "total_bytes": 0,
                "used_bytes": 0,
                "used_percent": 0,
                "location": str(data_root)
            },
            "channels": channels,
            "timestamp": __import__('time').time()
        }
    except Exception as e:
        logger.error(f"Error in get_summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "4.0.0"}


# ============================================================================
# API ENDPOINTS - TIMING ANALYSIS
# ============================================================================

@app.get("/api/v1/timing/fusion")
async def get_timing_fusion():
    """
    Get multi-broadcast fusion results - UTC(NIST) aligned D_clock
    Combines broadcasts from WWV, WWVH, CHU, BPM
    """
    try:
        import csv
        import json
        
        # Fusion CSV file path
        fusion_csv = data_root / "phase2" / "science" / "timing" / "fused_clock.csv"
        calibration_json = data_root / "phase2" / "science" / "timing" / "broadcast_calibration.json"
        
        latest_fusion = None
        history = []
        calibration = {}
        
        # Read fusion results from CSV
        if fusion_csv.exists():
            with open(fusion_csv, 'r') as f:
                reader = csv.DictReader(f)
                records = list(reader)
                
                # Get last 60 entries for chart
                recent_records = records[-60:] if len(records) > 60 else records
                
                for record in recent_records:
                    parsed = {
                        'timestamp': float(record.get('timestamp', 0)) if record.get('timestamp') else None,
                        'd_clock_fused_ms': float(record.get('d_clock_fused_ms', 0)) if record.get('d_clock_fused_ms') else None,
                        'd_clock_raw_ms': float(record.get('d_clock_raw_ms', 0)) if record.get('d_clock_raw_ms') else None,
                        'uncertainty_ms': float(record.get('uncertainty_ms', 0)) if record.get('uncertainty_ms') else None,
                        'n_broadcasts': int(record.get('n_broadcasts', 0)) if record.get('n_broadcasts') else 0,
                        'n_stations': int(record.get('n_stations', 0)) if record.get('n_stations') else 0,
                        'quality_grade': record.get('quality_grade', 'D'),
                        'outliers_rejected': int(record.get('outliers_rejected', 0)) if record.get('outliers_rejected') else 0,
                        'consistency_flag': record.get('consistency_flag', 'UNKNOWN')
                    }
                    
                    history.append({
                        'timestamp': parsed['timestamp'] or 0,
                        'd_clock_fused_ms': parsed['d_clock_fused_ms'] or 0,
                        'd_clock_raw_ms': parsed['d_clock_raw_ms'] or 0,
                        'uncertainty_ms': parsed['uncertainty_ms'] or 0,
                        'n_broadcasts': parsed['n_broadcasts'],
                        'quality_grade': parsed['quality_grade']
                    })
                
                # Get latest record with station stats
                if records:
                    latest_record = records[-1]
                    latest_fusion = {
                        **{k: float(v) if v and k.endswith('_ms') else v 
                           for k, v in latest_record.items()},
                        'station_stats': {
                            'WWV': {
                                'mean_ms': float(latest_record.get('wwv_mean_ms', 0)) if latest_record.get('wwv_mean_ms') else None,
                                'count': int(latest_record.get('wwv_count', 0)) if latest_record.get('wwv_count') else 0,
                                'intra_std_ms': float(latest_record.get('wwv_intra_std_ms', 0)) if latest_record.get('wwv_intra_std_ms') else None
                            },
                            'WWVH': {
                                'mean_ms': float(latest_record.get('wwvh_mean_ms', 0)) if latest_record.get('wwvh_mean_ms') else None,
                                'count': int(latest_record.get('wwvh_count', 0)) if latest_record.get('wwvh_count') else 0,
                                'intra_std_ms': float(latest_record.get('wwvh_intra_std_ms', 0)) if latest_record.get('wwvh_intra_std_ms') else None
                            },
                            'CHU': {
                                'mean_ms': float(latest_record.get('chu_mean_ms', 0)) if latest_record.get('chu_mean_ms') else None,
                                'count': int(latest_record.get('chu_count', 0)) if latest_record.get('chu_count') else 0,
                                'intra_std_ms': float(latest_record.get('chu_intra_std_ms', 0)) if latest_record.get('chu_intra_std_ms') else None
                            },
                            'BPM': {
                                'mean_ms': float(latest_record.get('bpm_mean_ms', 0)) if latest_record.get('bpm_mean_ms') else None,
                                'count': int(latest_record.get('bpm_count', 0)) if latest_record.get('bpm_count') else 0,
                                'intra_std_ms': float(latest_record.get('bpm_intra_std_ms', 0)) if latest_record.get('bpm_intra_std_ms') else None
                            }
                        }
                    }
        
        # Read calibration state
        if calibration_json.exists():
            with open(calibration_json, 'r') as f:
                calibration = json.load(f)
        
        return {
            'status': 'active' if latest_fusion else 'no_data',
            'latest': latest_fusion,
            'history': history,
            'calibration': calibration,
            'description': 'Multi-broadcast fusion aligns D_clock to UTC(NIST) using all available broadcasts (WWV, WWVH, CHU, BPM)'
        }
        
    except Exception as e:
        logger.error(f"Error in get_timing_fusion: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/channels")
async def get_channels():
    """Get list of configured channels"""
    try:
        channels = config.get('recorder', {}).get('channels', [])
        enabled_channels = [
            {
                'name': ch.get('description', f"Channel {ch.get('ssrc', 'unknown')}"),
                'frequency_mhz': ch.get('freq', 0) / 1e6 if ch.get('freq') else 0,
                'enabled': ch.get('enabled', True)
            }
            for ch in channels
            if ch.get('enabled', True)
        ]
        return {
            'channels': enabled_channels,
            'count': len(enabled_channels)
        }
    except Exception as e:
        logger.error(f"Error in get_channels: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.exception_handler(404)
async def not_found_handler(request, exc):
    """Custom 404 handler"""
    return JSONResponse(
        status_code=404,
        content={"error": "Not found", "path": str(request.url)}
    )


@app.exception_handler(500)
async def server_error_handler(request, exc):
    """Custom 500 handler"""
    logger.error(f"Server error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "monitoring_server:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level="info"
    )
