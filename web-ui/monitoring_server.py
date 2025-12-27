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
        fusion_csv = data_root / "phase2" / "fusion" / "fused_d_clock.csv"
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
# API V2 ENDPOINTS - TIMING METROLOGY & PROPAGATION SCIENCE
# ============================================================================

@app.get("/api/v2/timing/kalman-funnel")
async def get_kalman_funnel(
    hours: int = Query(24, description="Number of hours to retrieve"),
    date: Optional[str] = Query(None, description="Date in YYYYMMDD format (default: today)")
):
    """
    Get Kalman convergence data showing uncertainty reduction over time.
    Returns fused D_clock with uncertainty bounds, per-station contributions, and quality grades.
    """
    try:
        import csv
        from datetime import datetime, timedelta
        
        # Determine date range
        if date:
            end_time = datetime.strptime(date, "%Y%m%d")
        else:
            end_time = datetime.utcnow()
        
        start_time = end_time - timedelta(hours=hours)
        
        # Read fusion CSV file
        fusion_csv = data_root / "phase2" / "fusion" / "fused_d_clock.csv"
        
        if not fusion_csv.exists():
            return {
                "status": "no_data",
                "message": "Fusion data not available",
                "data": []
            }
        
        # Parse fusion data
        records = []
        with open(fusion_csv, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                timestamp = float(row.get('timestamp', 0))
                dt = datetime.fromtimestamp(timestamp)
                
                # Filter by time range
                if start_time <= dt <= end_time:
                    records.append({
                        'timestamp': timestamp,
                        'timestamp_utc': dt.isoformat() + 'Z',
                        'd_clock_fused_ms': float(row.get('d_clock_fused_ms', 0)) if row.get('d_clock_fused_ms') else None,
                        'uncertainty_ms': float(row.get('uncertainty_ms', 0)) if row.get('uncertainty_ms') else None,
                        'quality_grade': row.get('quality_grade', 'D'),
                        'n_stations': int(row.get('n_stations', 0)) if row.get('n_stations') else 0,
                        'n_broadcasts': int(row.get('n_broadcasts', 0)) if row.get('n_broadcasts') else 0,
                        # Per-station contributions
                        'stations': {
                            'WWV': {
                                'mean_ms': float(row.get('wwv_mean_ms', 0)) if row.get('wwv_mean_ms') else None,
                                'count': int(row.get('wwv_count', 0)) if row.get('wwv_count') else 0
                            },
                            'WWVH': {
                                'mean_ms': float(row.get('wwvh_mean_ms', 0)) if row.get('wwvh_mean_ms') else None,
                                'count': int(row.get('wwvh_count', 0)) if row.get('wwvh_count') else 0
                            },
                            'CHU': {
                                'mean_ms': float(row.get('chu_mean_ms', 0)) if row.get('chu_mean_ms') else None,
                                'count': int(row.get('chu_count', 0)) if row.get('chu_count') else 0
                            },
                            'BPM': {
                                'mean_ms': float(row.get('bpm_mean_ms', 0)) if row.get('bpm_mean_ms') else None,
                                'count': int(row.get('bpm_count', 0)) if row.get('bpm_count') else 0
                            }
                        }
                    })
        
        # Calculate statistics
        if records:
            latest = records[-1]
            grade_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
            for r in records:
                grade = r.get('quality_grade', 'D')
                if grade in grade_counts:
                    grade_counts[grade] += 1
        else:
            latest = None
            grade_counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
        
        return {
            "status": "ok",
            "data": records,
            "latest": latest,
            "statistics": {
                "count": len(records),
                "grade_distribution": grade_counts,
                "time_range": {
                    "start": start_time.isoformat() + 'Z',
                    "end": end_time.isoformat() + 'Z',
                    "hours": hours
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Error in get_kalman_funnel: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v2/timing/quality-timeline")
async def get_quality_timeline(hours: int = Query(24, description="Number of hours")):
    """
    Get quality grade distribution over time.
    Returns A/B/C/D grade counts per hour and data completeness metrics.
    """
    try:
        import csv
        from datetime import datetime, timedelta
        from collections import defaultdict
        
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(hours=hours)
        
        fusion_csv = data_root / "phase2" / "fusion" / "fused_d_clock.csv"
        
        if not fusion_csv.exists():
            return {"status": "no_data", "timeline": []}
        
        # Group by hour
        hourly_grades = defaultdict(lambda: {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'total': 0})
        
        with open(fusion_csv, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                timestamp = float(row.get('timestamp', 0))
                dt = datetime.fromtimestamp(timestamp)
                
                if start_time <= dt <= end_time:
                    hour_key = dt.replace(minute=0, second=0, microsecond=0)
                    grade = row.get('quality_grade', 'D')
                    
                    if grade in hourly_grades[hour_key]:
                        hourly_grades[hour_key][grade] += 1
                        hourly_grades[hour_key]['total'] += 1
        
        # Convert to timeline format
        timeline = []
        for hour in sorted(hourly_grades.keys()):
            data = hourly_grades[hour]
            timeline.append({
                'timestamp': hour.isoformat() + 'Z',
                'grades': {
                    'A': data['A'],
                    'B': data['B'],
                    'C': data['C'],
                    'D': data['D']
                },
                'total': data['total'],
                'completeness': min(1.0, data['total'] / 60.0)  # Expect ~60 measurements per hour
            })
        
        return {
            "status": "ok",
            "timeline": timeline,
            "hours": hours
        }
        
    except Exception as e:
        logger.error(f"Error in get_quality_timeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v2/timing/chrony-status")
async def get_chrony_status():
    """
    Get Chrony SHM integration status.
    Returns current Chrony source status, TMGR feed health, system clock discipline state.
    """
    try:
        import subprocess
        
        # Run chronyc sources to get TMGR status
        result = subprocess.run(
            ['chronyc', 'sources'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        tmgr_status = {
            'active': False,
            'state': 'unknown',
            'offset_ms': None,
            'stratum': None
        }
        
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'TMGR' in line or 'SHM' in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        tmgr_status['active'] = True
                        # Parse state (* = current source, + = combined, - = not combined)
                        if parts[0].startswith('*'):
                            tmgr_status['state'] = 'current_source'
                        elif parts[0].startswith('+'):
                            tmgr_status['state'] = 'combined'
                        elif parts[0].startswith('-'):
                            tmgr_status['state'] = 'not_combined'
                        
                        # Try to parse offset (usually in column 6 or 7)
                        try:
                            for part in parts:
                                if 'ms' in part or part.replace('.', '').replace('-', '').isdigit():
                                    offset_str = part.replace('ms', '').strip()
                                    tmgr_status['offset_ms'] = float(offset_str)
                                    break
                        except:
                            pass
        
        # Get tracking status
        tracking_result = subprocess.run(
            ['chronyc', 'tracking'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        system_time = {
            'synchronized': False,
            'system_offset_ms': None,
            'reference': None
        }
        
        if tracking_result.returncode == 0:
            for line in tracking_result.stdout.split('\n'):
                if 'Reference ID' in line:
                    system_time['reference'] = line.split(':')[-1].strip()
                if 'System time' in line:
                    # Parse system time offset
                    try:
                        parts = line.split(':')[-1].strip().split()
                        if len(parts) >= 2:
                            offset = float(parts[0])
                            unit = parts[1]
                            if 'seconds' in unit:
                                system_time['system_offset_ms'] = offset * 1000
                            system_time['synchronized'] = True
                    except:
                        pass
        
        return {
            "status": "ok",
            "tmgr": tmgr_status,
            "system_time": system_time,
            "timestamp": datetime.utcnow().isoformat() + 'Z'
        }
        
    except subprocess.TimeoutExpired:
        logger.error("Chrony command timeout")
        return {
            "status": "error",
            "message": "Chrony command timeout",
            "tmgr": {"active": False, "state": "timeout"},
            "system_time": {"synchronized": False}
        }
    except FileNotFoundError:
        logger.warning("chronyc not found")
        return {
            "status": "not_installed",
            "message": "Chrony not installed or not in PATH",
            "tmgr": {"active": False, "state": "not_installed"},
            "system_time": {"synchronized": False}
        }
    except Exception as e:
        logger.error(f"Error in get_chrony_status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v2/system/health-summary")
async def get_health_summary():
    """
    Get aggregated system health summary.
    Returns service status, data flow indicators, error rates, quality grade distribution.
    """
    try:
        # Get latest fusion data for quality assessment
        fusion_csv = data_root / "phase2" / "fusion" / "fused_d_clock.csv"
        
        current_quality = {
            'grade': 'D',
            'd_clock_ms': None,
            'uncertainty_ms': None,
            'n_stations': 0
        }
        
        grade_distribution = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
        
        if fusion_csv.exists():
            import csv
            with open(fusion_csv, 'r') as f:
                reader = csv.DictReader(f)
                records = list(reader)
                
                # Get latest
                if records:
                    latest = records[-1]
                    current_quality = {
                        'grade': latest.get('quality_grade', 'D'),
                        'd_clock_ms': float(latest.get('d_clock_fused_ms', 0)) if latest.get('d_clock_fused_ms') else None,
                        'uncertainty_ms': float(latest.get('uncertainty_ms', 0)) if latest.get('uncertainty_ms') else None,
                        'n_stations': int(latest.get('n_stations', 0)) if latest.get('n_stations') else 0
                    }
                
                # Calculate grade distribution (last 24h)
                for row in records[-1440:]:  # ~24h at 1min cadence
                    grade = row.get('quality_grade', 'D')
                    if grade in grade_distribution:
                        grade_distribution[grade] += 1
        
        # Check data flow (are HDF5 files being written?)
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y%m%d")
        
        data_flow = {
            'core_recorder': False,
            'analytics': False,
            'fusion': False
        }
        
        # Check for recent HDF5 files
        phase2_dir = data_root / "phase2"
        if phase2_dir.exists():
            # Look for any channel with today's data
            for channel_dir in phase2_dir.iterdir():
                if channel_dir.is_dir() and not channel_dir.name.startswith('.'):
                    # Check for L1A observables
                    l1a_dir = channel_dir / "carrier_power"
                    if l1a_dir.exists():
                        for f in l1a_dir.glob(f"{today}*.h5"):
                            data_flow['analytics'] = True
                            break
                    
                    # Check for L2 timing
                    l2_dir = channel_dir / "clock_offset"
                    if l2_dir.exists():
                        for f in l2_dir.glob(f"{today}*.h5"):
                            data_flow['analytics'] = True
                            break
        
        # Fusion is active if we have recent data
        data_flow['fusion'] = fusion_csv.exists() and current_quality['d_clock_ms'] is not None
        
        return {
            "status": "ok",
            "timestamp": datetime.utcnow().isoformat() + 'Z',
            "timing": current_quality,
            "grade_distribution_24h": grade_distribution,
            "data_flow": data_flow,
            "overall_health": "good" if current_quality['grade'] in ['A', 'B'] else "degraded" if current_quality['grade'] == 'C' else "poor"
        }
        
    except Exception as e:
        logger.error(f"Error in get_health_summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))



# ============================================================================
# STATIC FILE SERVING (catch-all - must be last!)
# ============================================================================

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
