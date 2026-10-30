"""Has the stand-alone T3 been admitted?  (Michael's ruling B, 2026-10-30)

The 10-22 rulings say T3 claims nothing until a B4 shadow run proves it
(spec 2026-10-30 §8 step 8; step 7 of the 10-22 order "lets it win T3").
Until then T3 runs as a shadow: it acquires, verifies and publishes its
registration, the shadow record and the judge's shadow residuals, and
nothing else.  This switch closes every other path from T3 to the
station's timing: the label-plane tracker, the label-plane anchor, the
ring and native anchors, §18, metrology's labels, the archive sidecar
and the authority snapshot.  On a station without T6 the station falls
through to T2.

::

    [timing.t3]
    admitted = false

Absent, malformed or false, the answer is FALSE.  Only a literal TOML
``true`` admits T3.

Pairing: ``OffsetJudge.SHADOW_ONLY_BENCHES`` keeps ``hf_acquired`` out of
the judge's adoption and out of witness dissent.  Step 7 flips this
switch to true and removes ``hf_acquired`` from that set together; either
one alone leaves T3 half admitted.

Like the anchor closure, the flag resolves once per process and is handed
to the pieces that need it, so the recorder, the metrology processes and
the fusion service cannot disagree about the regime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/hf-timestd/timestd-config.toml")

# ``[timing.t3] admitted``
CONFIG_SECTION = ("timing", "t3")
ADMITTED_KEY = "admitted"

#: What ``authority.json``'s ``t3_unavailable_reason`` says, and what every
#: closed surface names, while T3 waits for admission.
NOT_ADMITTED_REASON = "not admitted (shadow run pending)"

ADMITTED_MESSAGE = (
    "T3 admission: ADMITTED ([timing.t3] admitted = true): the stand-alone "
    "T3 may label data and publish authority"
)
NOT_ADMITTED_MESSAGE = (
    "T3 admission: NOT ADMITTED ([timing.t3] admitted = false): T3 runs as "
    "a shadow only, and reaches no label, anchor or authority"
)

_logged = False


def reset_log_state() -> None:
    """Forget that the regime has been logged (tests only)."""
    global _logged
    _logged = False


def admitted_from_config(config: Optional[dict]) -> bool:
    """The flag out of an already-loaded config dict.  Fails closed on a
    missing section, a scalar where a table belongs, or a non-boolean."""
    node: object = config or {}
    for key in CONFIG_SECTION:
        if not isinstance(node, dict):
            return False
        node = node.get(key) or {}
    if not isinstance(node, dict):
        return False
    return node.get(ADMITTED_KEY) is True


def _load(path: Path) -> dict:
    try:
        if not Path(path).exists():
            return {}
        import toml

        return toml.load(str(path)) or {}
    except Exception as exc:  # noqa: BLE001 — an unreadable config admits nothing
        logger.warning(
            "T3 admission: could not read %s (%s); T3 stays unadmitted", path, exc
        )
        return {}


def t3_admitted(
    config: Optional[dict] = None, *, path: Optional[Path] = None
) -> bool:
    """Resolve the flag, and log the regime once per process.

    Pass the config dict the process already loaded whenever there is one;
    ``path`` (or its default) serves a process that does not carry it.
    """
    if config is None:
        config = _load(path or DEFAULT_CONFIG_PATH)
    admitted = admitted_from_config(config)
    global _logged
    if not _logged:
        _logged = True
        logger.info(ADMITTED_MESSAGE if admitted else NOT_ADMITTED_MESSAGE)
    return admitted
