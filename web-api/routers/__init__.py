"""
Routers package.
"""

from .health import router as health_router
from .metrology import router as metrology_router
from .station import router as station_router
from .stability import router as stability_router
from .propagation import router as propagation_router
from .logs import router as logs_router
from .stations import router as stations_router
from .space_weather import router as space_weather_router
from .correlations import router as correlations_router
from .physics import router as physics_router
from .docs import router as docs_router
from .tec import router as tec_router
from .tid import router as tid_router

__all__ = [
    'health_router',
    'metrology_router',
    'station_router',
    'stability_router',
    'propagation_router',
    'stations_router',
    'logs_router',
    'space_weather_router',
    'correlations_router',
    'physics_router',
    'docs_router',
    'tec_router',
    'tid_router'
]
