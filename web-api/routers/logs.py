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
SERVICE_MAP = {
    "web-api": "timestd-web-api",
    "core": "timestd-core-recorder",
    "analytics": "timestd-analytics",
    "fusion": "timestd-fusion",
    "vtec": "timestd-vtec",
    "ionex": "timestd-ionex-download",
    "physics": "timestd-physics"
}

@router.get("/")
async def get_logs(
    service: str = Query(..., description="Service name (web-api, core, analytics, fusion, vtec, ionex, grape)"),
    lines: int = Query(100, ge=1, le=1000, description="Number of lines to return"),
    level: str = Query(None, description="Log level filter (INFO, WARNING, ERROR)"),
    since: str = Query("1h", description="Time range (e.g., 1h, 6h, 24h, 7d)")
):
    """
    Get logs for a specific service using journalctl.
    """
    if service not in SERVICE_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid service. Available: {', '.join(SERVICE_MAP.keys())}")
    
    unit_name = SERVICE_MAP[service]
    
    # Construct journalctl command
    cmd = ["journalctl", "-u", unit_name, "-n", str(lines), "--no-pager", "--output=short-iso"]
    
    # Add time filter
    if since:
        cmd.extend(["--since", f"-{since}"])
        
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
            logger.error(f"journalctl failed: {process.stderr}")
            raise HTTPException(status_code=500, detail="Failed to fetch logs")
            
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
