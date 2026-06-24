"""WWVB (60 kHz LF) propagation-delay model for the Fusion source pool.

WWVB is groundwave-dominant by day and skywave-assisted at night.  At the
>1000 km paths typical for a continental-US receiver, the single-hop skywave
delay and the groundwave delay both converge to within a few hundred
microseconds of the great-circle light-travel time: an ~85 km night-time
reflection height adds negligibly to a path already many hundreds of km long
(for a 1250 km path the one-hop slant exceeds the ground path by <0.05 ms).
We therefore model the expected delay as the great-circle light-travel time
plus a small groundwave secondary-phase excess, and carry an honest 1-sigma
uncertainty that covers what we are NOT modelling: the groundwave-vs-skywave
divergence, ground-conductivity secondary-phase delay, and the diurnal
reflection-height variation.

This is the LF analogue of ``metrology_engine._vacuum_hop_fallback_delay``.
The HF 40.3/f^2 ionospheric group-delay term is deliberately NOT used here: at
60 kHz it diverges (f^2 = 0.0036 MHz^2) and does not describe an LF
groundwave/skywave path.

GPS-learned override hook
-------------------------
Once a deployment has run a GPS-disciplined learning pass (see
docs/WWVB-INTEGRATION.md, propagation-delay calibration), a calibrated
per-site delay and a tighter sigma can be supplied via ``learned_delay_ms`` /
``learned_sigma_ms``; these take precedence over the nominal physical estimate.
Until then the nominal model keeps WWVB an honest, appropriately down-weighted
Fusion source rather than a falsely precise one.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

from hamsci_dsp.geometry import great_circle_km

from .wwv_constants import (
    EARTH_RADIUS_KM,
    SPEED_OF_LIGHT_KM_S,
    WWVB_LAT,
    WWVB_LON,
)

# Rough mixed-path groundwave secondary-phase slowing at LF.  This is an
# order-of-magnitude placeholder, NOT a calibrated coefficient -- it is
# superseded by the GPS-learned override the moment one is available, and the
# real unmodelled physics is carried in WWVB_NOMINAL_SIGMA_MS, not here.  Kept
# small on purpose so the nominal delay stays within 1 sigma of the bare
# light-travel time.
WWVB_GROUNDWAVE_SECONDARY_US_PER_KM = 0.4

# Honest nominal 1-sigma (ms): groundwave/skywave delay divergence, diurnal
# reflection-height change, and secondary-phase model error.  Deliberately
# loose so the Fusion combiner weights an uncalibrated WWVB source modestly.
WWVB_NOMINAL_SIGMA_MS = 2.0


class WwvbDelay(NamedTuple):
    """Expected propagation delay for a WWVB path to one receiver."""

    delay_ms: float
    """Expected transmitter->receiver propagation delay (ms)."""

    sigma_ms: float
    """1-sigma uncertainty on ``delay_ms``."""

    distance_km: float
    """Great-circle distance receiver->Fort Collins (km)."""

    calibrated: bool
    """True if ``delay_ms`` came from a GPS-learned override, else nominal."""


def _great_circle_km(
    lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float
) -> float:
    """Delegates to hamsci_dsp.geometry.great_circle_km (geodesic WGS-84)."""
    return great_circle_km(lat1_deg, lon1_deg, lat2_deg, lon2_deg)


def wwvb_expected_delay(
    rx_lat: float,
    rx_lon: float,
    *,
    learned_delay_ms: Optional[float] = None,
    learned_sigma_ms: Optional[float] = None,
) -> WwvbDelay:
    """Expected WWVB propagation delay from Fort Collins to ``(rx_lat, rx_lon)``.

    Args:
        rx_lat, rx_lon: receiver geodetic latitude / longitude (degrees).
        learned_delay_ms: optional GPS-learned delay (ms) that overrides the
            nominal physical estimate.  When provided, it is returned verbatim
            with ``calibrated=True``.
        learned_sigma_ms: optional 1-sigma to pair with ``learned_delay_ms``;
            defaults to ``WWVB_NOMINAL_SIGMA_MS`` if the learned delay is given
            without an explicit sigma.

    Returns:
        A ``WwvbDelay`` with delay, 1-sigma, great-circle distance, and a flag
        for whether the delay is calibrated.
    """
    dist_km = _great_circle_km(rx_lat, rx_lon, WWVB_LAT, WWVB_LON)

    if learned_delay_ms is not None:
        sigma = (
            learned_sigma_ms
            if learned_sigma_ms is not None
            else WWVB_NOMINAL_SIGMA_MS
        )
        return WwvbDelay(
            delay_ms=float(learned_delay_ms),
            sigma_ms=float(sigma),
            distance_km=dist_km,
            calibrated=True,
        )

    light_time_ms = dist_km / SPEED_OF_LIGHT_KM_S * 1000.0
    secondary_phase_ms = dist_km * WWVB_GROUNDWAVE_SECONDARY_US_PER_KM / 1000.0
    delay_ms = light_time_ms + secondary_phase_ms

    return WwvbDelay(
        delay_ms=delay_ms,
        sigma_ms=WWVB_NOMINAL_SIGMA_MS,
        distance_km=dist_km,
        calibrated=False,
    )


__all__ = [
    "WwvbDelay",
    "wwvb_expected_delay",
    "WWVB_NOMINAL_SIGMA_MS",
    "WWVB_GROUNDWAVE_SECONDARY_US_PER_KM",
]
