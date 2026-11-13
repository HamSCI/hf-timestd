"""Atomic read of radiod's (gps_time, rtp_timesnap) anchor pair.

ka9q-python's listener thread refreshes ``ChannelInfo.gps_time`` and
``ChannelInfo.rtp_timesnap`` one at a time.  A reader that fetches the
two attributes separately can land between the writes and hold a pair
that radiod never advertised (observed on AC0G-ND 2026-11-12/13: a ~1 s
re-base of the sample-to-UTC mapping).  ``ChannelInfo.get_anchor()``
returns both halves from one attribute, so every paired read goes
through ``read_anchor_pair``.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple


def read_anchor_pair(ci: Any) -> Optional[Tuple[int, int]]:
    """Return ``(gps_time_ns, rtp_timesnap)`` from ``ci``, or None.

    Prefers ``ci.get_anchor()``; accepts only a genuine 2-tuple with both
    entries set, else reads the legacy attributes.  Returns None when
    ``ci`` is None or either half is missing.  Never raises.
    """
    if ci is None:
        return None
    try:
        getter = getattr(ci, 'get_anchor', None)
        if callable(getter):
            pair = getter()
            if (isinstance(pair, tuple) and len(pair) == 2
                    and pair[0] is not None and pair[1] is not None):
                return int(pair[0]), int(pair[1])
        gps = getattr(ci, 'gps_time', None)
        snap = getattr(ci, 'rtp_timesnap', None)
        if gps is None or snap is None:
            return None
        return int(gps), int(snap)
    except Exception:  # noqa: BLE001 - contract: never raises
        return None
