"""
Routers package.
"""

from .health import router as health_router
from .metrology import router as metrology_router
from .station import router as station_router
from .stability import router as stability_router
from .propagation import router as propagation_router
from .stations import router as stations_router
from .logs import router as logs_router

__all__ = [
    'health_router',
    'metrology_router',
    'station_router',
    'stability_router',
    'propagation_router',
    'stations_router',
    'logs_router'
]
