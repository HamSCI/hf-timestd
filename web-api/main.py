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

from routers import health_router, metrology_router, station_router, stability_router, propagation_router
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
app.include_router(health_router, prefix="/api")
app.include_router(metrology_router, prefix="/api")
app.include_router(stability_router, prefix="/api")
app.include_router(propagation_router, prefix="/api")

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


@app.on_event("startup")
async def startup_event():
    """Log startup information."""
    logger.info("=" * 60)
    logger.info("hf-timestd Web UI Starting")
    logger.info("=" * 60)
    logger.info(f"Station: {config.station_metadata['callsign']}")
    logger.info(f"Data Root: {config.data_root}")
    logger.info(f"Channels: {len(config.channels)}")
    logger.info(f"Mode: {config.station_metadata['mode']}")
    logger.info("=" * 60)


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
