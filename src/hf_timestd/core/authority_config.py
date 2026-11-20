"""Where the authority manager's settings live in the station config.

The fusion service's runner (``build_authority_runner_from_config``) reads
them, and so does every process that follows the manager's output and
needs to know how often it writes (the T3 admission gate).  One reader
keeps the two from disagreeing.  This module imports nothing heavy, so a
recorder or metrology process can use it.
"""

from __future__ import annotations

from typing import Any, Optional

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
