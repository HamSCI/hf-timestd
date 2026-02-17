"""
FastAPI Web UI for hf-timestd

Main application entry point providing:
- RESTful API for data access
- Static file serving for frontend
- WebSocket support for real-time updates
"""

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import logging
import asyncio

# Systemd watchdog support
try:
    from systemd import daemon as systemd_daemon
    SYSTEMD_AVAILABLE = True
except ImportError:
    SYSTEMD_AVAILABLE = False

from routers import health_router, metrology_router, station_router, stability_router, propagation_router, logs_router, stations_router, space_weather_router, correlations_router, physics_router, docs_router, tec_router, tid_router, dashboard_router, phase_router, grape_router
from routers.timing_validation import router as timing_validation_router
from routers.decoder_comparison import router as decoder_comparison_router
from config import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="hf-timestd Web UI",
    description="Web interface for HF Time Standard monitoring and analysis",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc"
)

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(station_router, prefix="/api")
app.include_router(stations_router, prefix="/api")
app.include_router(health_router, prefix="/api")
app.include_router(metrology_router, prefix="/api")
app.include_router(stability_router, prefix="/api")
app.include_router(propagation_router, prefix="/api")
app.include_router(logs_router, prefix="/api")
app.include_router(space_weather_router, prefix="/api")
app.include_router(correlations_router, prefix="/api")
app.include_router(physics_router, prefix="/api")
app.include_router(tec_router, prefix="/api")
app.include_router(tid_router, prefix="/api")
app.include_router(docs_router)  # No prefix - router has its own /api/docs prefix
app.include_router(timing_validation_router)  # No prefix - router has its own /api/timing-validation prefix
app.include_router(decoder_comparison_router)  # No prefix - router has its own /api/decoder-comparison prefix
app.include_router(dashboard_router, prefix="/api")  # 24-hour dashboard endpoints
app.include_router(phase_router, prefix="/api")  # Phase/Doppler analysis endpoints
app.include_router(grape_router, prefix="/api")  # GRAPE spectrograms and upload status

# Static files directory
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve index page."""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    else:
        return HTMLResponse(
            content="""
            <html>
                <head><title>hf-timestd</title></head>
                <body>
                    <h1>hf-timestd Web UI</h1>
                    <p>API documentation: <a href="/api/docs">/api/docs</a></p>
                    <p>Station metadata: <a href="/api/station/metadata">/api/station/metadata</a></p>
                    <p>System health: <a href="/api/health/system">/api/health/system</a></p>
                    <p>Latest fusion: <a href="/api/metrology/fusion/latest">/api/metrology/fusion/latest</a></p>
                </body>
            </html>
            """,
            status_code=200
        )


@app.get("/dashboard-24h", response_class=HTMLResponse)
async def dashboard_24h_page():
    """Serve 24-hour dashboard."""
    dashboard_path = static_dir / "dashboard-24h.html"
    if dashboard_path.exists():
        return FileResponse(dashboard_path)
    else:
        return HTMLResponse(
            content="<html><body><h1>24-Hour Dashboard not found</h1></body></html>",
            status_code=404
        )


@app.get("/phase", response_class=HTMLResponse)
async def phase_page():
    """Serve phase/Doppler analysis dashboard."""
    phase_path = static_dir / "phase.html"
    if phase_path.exists():
        return FileResponse(phase_path)
    else:
        return HTMLResponse(
            content="<html><body><h1>Phase/Doppler Dashboard not found</h1></body></html>",
            status_code=404
        )


@app.get("/grape", response_class=HTMLResponse)
async def grape_page():
    """Serve GRAPE spectrograms and upload dashboard."""
    grape_path = static_dir / "grape.html"
    if grape_path.exists():
        return FileResponse(grape_path)
    else:
        return HTMLResponse(
            content="<html><body><h1>GRAPE Dashboard not found</h1></body></html>",
            status_code=404
        )


@app.get("/timing-validation", response_class=HTMLResponse)
async def timing_validation_page():
    """Serve timing validation dashboard."""
    validation_path = static_dir / "timing-validation.html"
    if validation_path.exists():
        return FileResponse(validation_path)
    else:
        return HTMLResponse(
            content="<html><body><h1>Timing Validation Dashboard not found</h1></body></html>",
            status_code=404
        )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "hf-timestd-web-ui",
        "version": "1.0.0",
        "data_root": str(config.data_root),
        "station": config.station_metadata['callsign']
    }


async def watchdog_task():
    """Background task to send systemd watchdog notifications."""
    while True:
        if SYSTEMD_AVAILABLE:
            systemd_daemon.notify('WATCHDOG=1')
        await asyncio.sleep(10)  # Send heartbeat every 10 seconds


@app.on_event("startup")
async def startup_event():
    """Log startup information and start watchdog."""
    logger.info("=" * 60)
    logger.info("hf-timestd Web UI Starting")
    logger.info("=" * 60)
    logger.info(f"Station: {config.station_metadata['callsign']}")
    logger.info(f"Data Root: {config.data_root}")
    logger.info(f"Channels: {len(config.channels)}")
    logger.info(f"Mode: {config.station_metadata['mode']}")
    logger.info("=" * 60)
    
    # Notify systemd we're ready and start watchdog
    if SYSTEMD_AVAILABLE:
        systemd_daemon.notify('READY=1')
        logger.info("Systemd watchdog enabled")
        asyncio.create_task(watchdog_task())


if __name__ == "__main__":
    import uvicorn
    
    # Run with uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
