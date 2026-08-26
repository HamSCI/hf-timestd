"""Catch a whole-second slip in the integer-second naming.

The fine stage names an edge's integer second from the NMEA reading::

    named = pps_utc_sec + round(edge_utc - pps_utc_sec)
    if abs(edge_utc - named) > 0.4: reject

⚠ That guard cannot detect an off-by-one-second, because when ``round()``
tips the wrong way ``named`` moves with it — so ``abs(edge_utc - named)``
is small again and the check passes.  It validates the answer against
itself.

MEASURED on AC0G-B4: across 2,176 consecutive T6 anchor pairs there are
**three** whole-second excursions, each +1 s followed by −1 s one anchor
later (2026-08-25 08:50:05Z, 15:05:02Z, 21:40:03Z).  Two of the three
occurred under the legacy labelling convention, so this is endemic and
not an artifact of any convention change.  Each lasts one anchor —
about 30 s — and self-corrects, which is why it went unnoticed: it is
invisible unless something samples inside the window.  One did, on
2026-08-25, and a working convention change was rolled back because of
it.

The tell is phase: the three bad anchors were logged at .11, .25 and .72
of the second against a usual cadence of .63–.64, i.e. the fold completed
at an unusual phase relative to the NMEA reading.

## The check that does not move with the answer

The RTP counter is GPSDO-disciplined and continuous, so the previous
anchor carried forward by ``ΔRTP / f_s`` predicts where the next edge
must fall.  A naming that disagrees with that by half a second or more is
the NAMING's error, not the counter's — the counter cannot gain or lose a
second, and its rate is known to 0.0004 ppm (1.44 µs/hour, so ~12 ps over
a 30 s anchor interval).

This is the same principle the rest of the system rests on: intervals per
unit time are physical; the absolute value is a claim.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_TWO32 = 1 << 32

# Half a second is the decision boundary of the rounding that fails, so
# anything at or beyond it is an integer-second slip rather than jitter.
SLIP_THRESHOLD_S = 0.5

# Beyond this the previous anchor is too old to predict against: the
# counter is still good, but an anchor this stale may itself be wrong.
MAX_PREDICT_AGE_S = 300.0


def predicted_edge_utc(
    edge_rtp: int, anchor, sample_rate_hz: float
) -> Optional[float]:
    """Where the previous anchor says this edge must fall, in UTC seconds."""
    if anchor is None or not sample_rate_hz:
        return None
    try:
        d = (int(edge_rtp) - int(anchor.anchor_rtp)) & 0xFFFFFFFF
        if d >= (_TWO32 >> 1):
            d -= _TWO32
        return float(anchor.anchor_utc_ns) / 1e9 + d / float(sample_rate_hz)
    except (TypeError, ValueError, AttributeError):
        return None


def reconcile_named_second(
    named: int,
    edge_rtp: int,
    anchor,
    sample_rate_hz: float,
    slip_threshold_s: float = SLIP_THRESHOLD_S,
    max_age_s: float = MAX_PREDICT_AGE_S,
) -> tuple:
    """Return ``(named, slip_seconds)`` with any whole-second slip removed.

    ``slip_seconds`` is 0 when the naming already agrees with the counter.
    Only INTEGER-second corrections are applied: a sub-second disagreement
    is left alone, because that is the fine stage's own business and this
    check has no standing to touch it.

    Returns the naming unchanged when there is no usable previous anchor —
    a first acquisition has nothing to be continuous with.
    """
    pred = predicted_edge_utc(edge_rtp, anchor, sample_rate_hz)
    if pred is None:
        return int(named), 0
    if abs(pred - float(named)) > float(max_age_s):
        return int(named), 0
    slip = int(round(float(named) - pred))
    if slip == 0 or abs(float(named) - pred) < float(slip_threshold_s):
        return int(named), 0
    return int(named) - slip, slip
