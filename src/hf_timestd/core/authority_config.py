"""Where the authority manager's settings live in the station config.

The authority runner (``build_authority_runner_from_config``) reads them,
and so does every process that follows the manager's output and needs to
know how often it writes (the T3 admission gate).  One reader keeps the
two from disagreeing.  ``load_station_config`` reads the TOML itself, for
both the fusion service and the authority service.  This module imports
nothing heavy, so a recorder, metrology or authority process can use it
without loading fusion.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Where the station config lives in production.
DEFAULT_CONFIG_PATH = Path('/etc/hf-timestd/timestd-config.toml')


def load_station_config(path) -> dict:
    """The TOML at ``path``, or {} when it is missing or unreadable.

    {} fails closed: no anchor closure, and T3 not admitted."""
    try:
        path = Path(path)
        if not path.exists():
            return {}
        import toml
        return toml.load(str(path)) or {}
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not read config {path}: {e}")
        return {}


#: The manager's tick, when ``[timing.authority_manager] interval_sec``
#: does not say.
DEFAULT_INTERVAL_SEC = 30.0


def authority_manager_section(config: Optional[dict]) -> dict:
    """``[timing.authority_manager]``, else the legacy ``[timing.authority]``
    sub-table when it is a dict, else ``{}``.  Never raises.

    ``[timing] authority`` as a scalar belongs to sigmond (see the runner),
    and reads here as no section."""
    timing: Any = (config or {}).get("timing", {}) if isinstance(config, dict) else {}
    if not isinstance(timing, dict):
        return {}
    section = timing.get("authority_manager", None)
    if isinstance(section, dict):
        return section
    legacy = timing.get("authority", None)
    return legacy if isinstance(legacy, dict) else {}


def authority_interval_sec(config: Optional[dict]) -> float:
    """``interval_sec`` as the runner reads it: ``float()`` of the value,
    default ``DEFAULT_INTERVAL_SEC``.  A value ``float()`` rejects raises,
    as it always has for the runner."""
    return float(authority_manager_section(config).get(
        "interval_sec", DEFAULT_INTERVAL_SEC))
