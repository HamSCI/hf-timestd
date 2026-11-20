"""Does the stand-alone T3 hold admission?  The ``[timing.t3] admission`` setting.

Three modes::

    [timing.t3]
    admission = "auto"      # or "off" or "on"

``auto`` (the default) lets the station decide.  The fusion service
compares T3 with T2 over a rolling window and admits T3 while it wins
and its sigmas stay honest.  It withdraws T3 at once on a jump.  The
fusion service publishes each decision in ``authority.json``; every
other process follows it through one ``T3AdmissionGate``
(``t3_admission_gate``).

``off`` keeps T3 a shadow.  It acquires, verifies and publishes its
registration and shadow record, and reaches no label, anchor or
authority.

``on`` admits T3 with no judging.  It serves testing only.

The old ``admitted`` key maps: true reads as ``on``, false reads as
``auto``.  The new key wins when both appear.  A malformed value logs a
WARNING and reads as ``auto``.

The mode resolves once per process and goes to the pieces that need it,
so the recorder, the metrology processes and the fusion service cannot
disagree about it.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/hf-timestd/timestd-config.toml")

# ``[timing.t3] admission`` (``admitted`` stays for the old-key mapping)
CONFIG_SECTION = ("timing", "t3")
ADMISSION_KEY = "admission"
ADMITTED_KEY = "admitted"


class AdmissionMode(str, Enum):
    AUTO = "auto"
    OFF = "off"
    ON = "on"


#: What ``authority.json``'s ``t3_unavailable_reason`` says, and what every
#: closed surface names, while T3 waits for admission.
NOT_ADMITTED_REASON = "not admitted (shadow run pending)"

_logged = False


def reset_log_state() -> None:
    """Forget that the mode has been logged (tests only)."""
    global _logged
    _logged = False


def admission_mode_from_config(config: Optional[dict]) -> AdmissionMode:
    """The mode out of an already-loaded config dict.

    A missing section reads as AUTO silently.  A malformed value (a scalar
    where a table belongs, an unknown string, a non-boolean ``admitted``)
    logs a WARNING and reads as AUTO.
    """
    node: object = config or {}
    for key in CONFIG_SECTION:
        if not isinstance(node, dict):
            logger.warning("T3 admission: [timing.t3] is malformed; using auto")
            return AdmissionMode.AUTO
        node = node.get(key) or {}
    if not isinstance(node, dict):
        logger.warning("T3 admission: [timing.t3] is malformed; using auto")
        return AdmissionMode.AUTO
    if ADMISSION_KEY in node:
        value = node[ADMISSION_KEY]
        try:
            return AdmissionMode(value)
        except ValueError:
            logger.warning(
                "T3 admission: [timing.t3] admission = %r is not auto, off or "
                "on; using auto", value)
            return AdmissionMode.AUTO
    if ADMITTED_KEY in node:
        value = node[ADMITTED_KEY]
        if value is True:
            return AdmissionMode.ON
        if value is False:
            return AdmissionMode.AUTO
        logger.warning(
            "T3 admission: [timing.t3] admitted = %r is not a boolean; "
            "using auto", value)
    return AdmissionMode.AUTO


def _load(path: Path) -> dict:
    try:
        if not Path(path).exists():
            return {}
        import toml

        return toml.load(str(path)) or {}
    except Exception as exc:  # noqa: BLE001 — an unreadable config falls back to auto
        logger.warning(
            "T3 admission: could not read %s (%s); using auto", path, exc
        )
        return {}


def t3_admission_mode(
    config: Optional[dict] = None, *, path: Optional[Path] = None
) -> AdmissionMode:
    """Resolve the mode, and log it once per process.

    Pass the config dict the process already loaded whenever there is one;
    ``path`` (or its default) serves a process that does not carry it.
    """
    if config is None:
        config = _load(path or DEFAULT_CONFIG_PATH)
    mode = admission_mode_from_config(config)
    global _logged
    if not _logged:
        _logged = True
        logger.info("T3 admission: [timing.t3] admission = %s", mode.value)
    return mode

