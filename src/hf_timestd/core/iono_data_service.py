"""Shim: canonical home is hamsci_dsp.ionosphere.data_service (split §5.2).

The library takes its cache directory and IRI-fallback IONEX directory
by injection; hf-timestd's operational defaults live here so every
existing consumer (`IonoDataService.get_instance()`, propagation model,
CLI) behaves exactly as before.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from hamsci_dsp.ionosphere.data_service import *  # noqa: F401,F403
from hamsci_dsp.ionosphere.data_service import (  # noqa: F401
    IonoDataService as _LibIonoDataService,
)

DEFAULT_CACHE_DIR = "/var/lib/timestd/iono_cache"
DEFAULT_IRI_IONEX_DIR = Path("/var/lib/timestd/ionex")


class IonoDataService(_LibIonoDataService):
    """hf-timestd-flavored service: timestd cache + IONEX dirs by default."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls, cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
                     **kwargs) -> "IonoDataService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(cache_dir=cache_dir, **kwargs)
            return cls._instance

    def __init__(self, cache_dir: Optional[str] = DEFAULT_CACHE_DIR,
                 **kwargs):
        kwargs.setdefault("iri_ionex_dir", DEFAULT_IRI_IONEX_DIR)
        super().__init__(cache_dir=cache_dir, **kwargs)
