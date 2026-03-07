"""
Logs API endpoints.
"""

from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
import subprocess
import logging
from datetime import datetime, timedelta

# Initialize logger for this module
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])

# Map service short names to systemd unit names
# Groups: core pipeline, batch jobs, infrastructure
SERVICE_MAP = {
    # --- Core pipeline (always running) ---
    "core": "timestd-core-recorder",
    "metrology": "timestd-metrology",
    "l2-calibration": "timestd-l2-calibration",
    "physics": "timestd-physics",
    "fusion": "timestd-fusion",
    "vtec": "timestd-vtec",
    "web-api": "timestd-web-api",
    "radiod-monitor": "timestd-radiod-monitor",
    # --- Batch / timer-triggered ---
    "chrony-monitor": "timestd-chrony-monitor",
    "iono-reanalysis": "timestd-iono-reanalysis",
    "ionex-download": "timestd-ionex-download",
    "grape-daily": "grape-daily",
    # --- Infrastructure (external dependencies) ---
    "chrony": "chrony",
    "radiod": "radiod@*",
}

# Human-readable labels and groups for the UI
SERVICE_META = {
    "core":             {"label": "Core Recorder",       "group": "Pipeline"},
    "metrology":        {"label": "Metrology (L1)",      "group": "Pipeline"},
    "l2-calibration":   {"label": "L2 Calibration",      "group": "Pipeline"},
    "physics":          {"label": "Physics (L2)",        "group": "Pipeline"},
    "fusion":           {"label": "Fusion",              "group": "Pipeline"},
    "vtec":             {"label": "VTEC",                "group": "Pipeline"},
    "web-api":          {"label": "Web API",             "group": "Pipeline"},
    "radiod-monitor":   {"label": "Radiod Monitor",      "group": "Pipeline"},
    "chrony-monitor":   {"label": "Chrony Monitor",      "group": "Batch"},
    "iono-reanalysis":  {"label": "Iono Reanalysis",     "group": "Batch"},
    "ionex-download":   {"label": "IONEX Download",      "group": "Batch"},
    "grape-daily":      {"label": "GRAPE Daily",         "group": "Batch"},
    "chrony":           {"label": "Chrony (NTP)",        "group": "Infrastructure"},
    "radiod":           {"label": "Radiod (SDR)",        "group": "Infrastructure"},
}

@router.get("/")
async def get_logs(
    service: str = Query(..., description="Service name (web-api, core, metrology, fusion, vtec, physics, l2-calibration, radiod-monitor)"),
    lines: int = Query(100, ge=1, le=1000, description="Number of lines to return"),
    level: str = Query(None, description="Log level filter (INFO, WARNING, ERROR)"),
    since: str = Query("1h", description="Time range (e.g., 1h, 6h, 24h, 7d) or ISO8601 datetime"),
    until: str = Query(None, description="End time (ISO8601 datetime, optional)")
):
    """
    Get logs for a specific service using journalctl.
    """
    if service not in SERVICE_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid service. Available: {', '.join(SERVICE_MAP.keys())}")
    
    unit_name = SERVICE_MAP[service]
    
    # Construct journalctl command
    # journalctl -u supports glob patterns natively (e.g. radiod@*)
    cmd = ["journalctl", "-u", unit_name, "-n", str(lines), "--no-pager", "--output=short-iso"]
    
    # Add time filter — detect ISO datetime vs relative duration
    if since:
        if 'T' in since or (len(since) >= 10 and since[4] == '-'):
            cmd.extend(["--since", since])
        else:
            cmd.extend(["--since", f"-{since}"])
    if until:
        cmd.extend(["--until", until])
        
    # Add priority filter if specified
    # journalctl priority: 0=emerg, 1=alert, 2=crit, 3=err, 4=warning, 5=notice, 6=info, 7=debug
    if level:
        if level.upper() == "ERROR":
            cmd.extend(["-p", "3"])
        elif level.upper() == "WARNING":
            cmd.extend(["-p", "4"])
        elif level.upper() == "INFO":
            cmd.extend(["-p", "6"])
        elif level.upper() == "DEBUG":
            cmd.extend(["-p", "7"])
            
    try:
        # Run journalctl
        # Note: Needs user to be in 'systemd-journal' group or run as root/sudo
        # This setup assumes the user running the web-api has permission
        process = subprocess.run(cmd, capture_output=True, text=True)
        
        if process.returncode != 0:
            logger.warning(f"journalctl failed (rc={process.returncode}): {process.stderr.strip()}")
            # Return empty result with explanation instead of 500
            hint = "journalctl access denied — add the web-api user to the 'systemd-journal' group"
            if "No journal files" in process.stderr or "not seeing messages" in process.stderr:
                hint = "No journal entries visible — user may need 'systemd-journal' group membership"
            return {
                "service": service,
                "unit": unit_name,
                "count": 0,
                "logs": [],
                "error": hint
            }
            
        logs = process.stdout.splitlines()
        
        # Simple parsing to structured format
        parsed_logs = []
        for line in logs:
            try:
                # journalctl short-iso format:
                # 2026-01-04T22:00:00+0000 hostname process[pid]: message
                parts = line.split(" ", 3)
                if len(parts) >= 4:
                    timestamp = parts[0]
                    # host = parts[1] 
                    # process = parts[2]
                    message = parts[3]
                    
                    # Try to strict log level from message if present
                    msg_level = "INFO"
                    if "ERROR" in message or "CRITICAL" in message:
                        msg_level = "ERROR"
                    elif "WARNING" in message:
                        msg_level = "WARNING"
                    elif "DEBUG" in message:
                        msg_level = "DEBUG"
                        
                    parsed_logs.append({
                        "timestamp": timestamp,
                        "level": msg_level,
                        "message": message
                    })
                else:
                    parsed_logs.append({
                        "timestamp": "",
                        "level": "UNKNOWN", 
                        "message": line
                    })
            except Exception:
                parsed_logs.append({"timestamp": "", "level": "UNKNOWN", "message": line})
                
        return {
            "service": service,
            "unit": unit_name,
            "count": len(parsed_logs),
            "logs": parsed_logs
        }
        
    except Exception as e:
        logger.error(f"Error fetching logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/services")
async def list_services():
    """Return available services with labels and groups for the UI."""
    services = []
    for key in SERVICE_MAP:
        meta = SERVICE_META.get(key, {"label": key, "group": "Other"})
        services.append({
            "key": key,
            "label": meta["label"],
            "group": meta["group"],
            "unit": SERVICE_MAP[key],
        })
    return {"services": services}
