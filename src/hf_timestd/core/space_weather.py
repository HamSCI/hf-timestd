"""Shim: the space-weather service lives in hamsci_dsp.ionosphere.

Split design §5.2 — parsing/caching moved to hamsci-dsp with the HTTP
session and cache directory injected.  This shim keeps the hf-timestd
operational surface identical: the /var/lib/timestd cache path and the
net_fetch retry/backoff session are supplied here, so every existing
consumer (`SpaceWeatherService.get_instance()`, the spaceweather
healthcheck CLI, ionospheric_model) behaves exactly as before.
"""
from __future__ import annotations

import threading
from typing import Optional

# Re-export the whole public surface (constants, dataclass, ranges).
from hamsci_dsp.ionosphere.space_weather import *  # noqa: F401,F403
from hamsci_dsp.ionosphere.space_weather import (  # noqa: F401
    SpaceWeather,
    SpaceWeatherService as _LibSpaceWeatherService,
)

DEFAULT_CACHE_DIR = "/var/lib/timestd/iono_cache"


def _default_session():
    try:
        from . import net_fetch
        return net_fetch.build_session()
    except ImportError:  # pragma: no cover - defensive
        return None


class SpaceWeatherService(_LibSpaceWeatherService):
    """hf-timestd-flavored service: timestd cache dir + net_fetch session."""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, cache_dir: str = DEFAULT_CACHE_DIR,
                     session=None) -> "SpaceWeatherService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(cache_dir=cache_dir, session=session)
            return cls._instance

    def __init__(self, cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
                 session=None):
        super().__init__(
            cache_dir=cache_dir,
            session=session if session is not None else _default_session(),
        )
