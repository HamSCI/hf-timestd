#!/usr/bin/env python3
"""
HF skywave hop geometry — the single source of truth (review item S2).

Skywave hop geometry (slant path length, launch/arrival elevation, the
inverse height-from-path) was reimplemented in at least four modules with
two incompatible conventions: a spherical law-of-cosines model in
``arrival_pattern_matrix`` and ``propagation_model``, and a flat-Earth
triangle in ``propagation_mode_solver``, ``propagation_engine`` and the
``ionospheric_model`` calibration inverse. For a 7000 km path the
flat-vs-spherical divergence is several percent — tens of milliseconds —
so the same path produced different delays depending on which module
asked. This module is the one spherical implementation; every caller
delegates here.

Geometry
--------
One ionospheric hop is the triangle (Earth centre C, ground point G,
reflection point P):

* G sits on the surface at geocentric radius ``R`` (Earth radius).
* P sits at radius ``r_p = R + h`` above the hop midpoint.
* The hop's ground arc subtends a central angle ``theta = d_hop / R`` at
  C; the reflection point is above the midpoint, so each leg (G→P) spans
  the half-hop angle ``gamma = theta / 2``.

Law of cosines for one leg's slant range::

    slant^2 = R^2 + r_p^2 - 2 * R * r_p * cos(gamma)

The total path of an N-hop mode is ``N * 2 * slant`` (each hop is an
up-leg plus a down-leg). The launch/arrival elevation is the angle of the
leg above the local horizontal at G::

    elevation = atan2(r_p * cos(gamma) - R,  r_p * sin(gamma))

Both reduce to the flat-Earth triangle (``slant = hypot(d_hop/2, h)``,
``elevation = atan2(h, d_hop/2)``) as ``gamma -> 0``, i.e. for short
paths or in the ``R -> inf`` limit.

REFERENCE: Davies, K. (1990). "Ionospheric Radio." IEE Electromagnetic
Waves Series 31, §6 (oblique propagation geometry).
"""

import math
from dataclasses import dataclass
from typing import Optional

from .wwv_constants import EARTH_RADIUS_KM

# Speed of light, km per ms — for callers converting a path length (km)
# straight to a geometric (vacuum) delay (ms).
C_LIGHT_KM_MS = 299.792458


@dataclass(frozen=True)
class HopGeometry:
    """Spherical-Earth geometry of an N-hop HF skywave path."""

    n_hops: int  # number of ionospheric reflections (>= 1)
    ground_distance_km: float  # total great-circle ground distance
    height_km: float  # reflection-layer height
    path_length_km: float  # total slant path, all hops, up + down
    elevation_deg: float  # launch / arrival elevation angle
    slant_per_leg_km: float  # one up-or-down leg of a single hop
    central_angle_rad: float  # full per-hop ground-arc central angle

    @property
    def geometric_delay_ms(self) -> float:
        """Vacuum (free-space) propagation delay over the slant path."""
        return self.path_length_km / C_LIGHT_KM_MS


def max_single_hop_distance_km(
    height_km: float,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> float:
    """
    Largest ground distance reachable in one hop (tangent-ray limit).

    Twice the tangent-ray slant range ``sqrt(2*R*h + h^2)``; used as a
    feasibility threshold by callers, which apply their own margin. This
    preserves the convention already used by ``arrival_pattern_matrix``
    and ``propagation_model``.
    """
    return 2.0 * math.sqrt(2.0 * earth_radius_km * height_km + height_km**2)


def n_hops_for_distance(
    ground_distance_km: float,
    height_km: float,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> int:
    """
    Minimum number of hops needed to span ``ground_distance_km``.

    Always returns >= 1; the ground-wave (<~500 km, no hop) case is left
    to the caller, which knows its own short-range threshold.
    """
    max_1hop = max_single_hop_distance_km(height_km, earth_radius_km)
    if ground_distance_km <= max_1hop:
        return 1
    return max(2, int(math.ceil(ground_distance_km / max_1hop)))


def hop_geometry(
    ground_distance_km: float,
    height_km: float,
    n_hops: int = 1,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> HopGeometry:
    """
    Spherical-Earth geometry of an N-hop skywave path.

    Args:
        ground_distance_km: Total great-circle ground distance, TX to RX.
        height_km: Reflection-layer height above the surface.
        n_hops: Number of equal hops the ground distance is split into.
        earth_radius_km: Earth radius (defaults to the project constant).

    Returns:
        A :class:`HopGeometry` with the total slant path, the launch
        elevation, and the intermediate quantities callers reuse.

    Raises:
        ValueError: if ``n_hops < 1`` or either distance is negative.
    """
    if n_hops < 1:
        raise ValueError(f"n_hops must be >= 1, got {n_hops}")
    if ground_distance_km < 0 or height_km < 0:
        raise ValueError(
            f"distances must be non-negative "
            f"(ground={ground_distance_km}, height={height_km})"
        )

    R = earth_radius_km
    r_p = R + height_km

    hop_ground_km = ground_distance_km / n_hops
    theta = hop_ground_km / R  # per-hop ground-arc central angle
    gamma = theta / 2.0  # half-hop: ground point -> reflection

    # Law of cosines for one leg (ground point -> reflection point).
    slant_sq = R**2 + r_p**2 - 2.0 * R * r_p * math.cos(gamma)
    slant = math.sqrt(max(0.0, slant_sq))

    path_length_km = n_hops * 2.0 * slant

    # Elevation above the local horizontal at the ground point.
    elev_rad = math.atan2(
        r_p * math.cos(gamma) - R,
        r_p * math.sin(gamma),
    )

    return HopGeometry(
        n_hops=n_hops,
        ground_distance_km=ground_distance_km,
        height_km=height_km,
        path_length_km=path_length_km,
        elevation_deg=math.degrees(elev_rad),
        slant_per_leg_km=slant,
        central_angle_rad=theta,
    )


def height_from_path(
    path_length_km: float,
    ground_distance_km: float,
    n_hops: int,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> Optional[float]:
    """
    Inverse of :func:`hop_geometry`: infer reflection height from a path.

    Given an observed total slant path (e.g. from a measured propagation
    delay), the ground distance and the hop count, solve the law-of-cosines
    leg equation for the reflection-layer height. This is the inverse the
    ``ionospheric_model`` height calibration needs (P-M12) — using the same
    spherical geometry as the forward predictors so the calibration offset
    is a pure height error, not a flat-vs-spherical geometry artefact.

    Solving ``slant^2 = R^2 + r_p^2 - 2*R*r_p*cos(gamma)`` for ``r_p``::

        r_p = R*cos(gamma) + sqrt(slant^2 - (R*sin(gamma))^2)
        h   = r_p - R

    (The ``+`` root is the physical one — the ``-`` root puts the
    reflection point below the surface.)

    Returns:
        The reflection height (km), or ``None`` when the path is too short
        to be geometrically realisable for this ground distance and hop
        count — the slant leg cannot close the triangle (discriminant < 0)
        or the implied height is negative.
    """
    if n_hops < 1 or path_length_km <= 0 or ground_distance_km < 0:
        return None

    R = earth_radius_km
    slant = path_length_km / (2.0 * n_hops)
    gamma = ground_distance_km / (2.0 * R * n_hops)

    discriminant = slant**2 - (R * math.sin(gamma)) ** 2
    if discriminant < 0:
        return None

    r_p = R * math.cos(gamma) + math.sqrt(discriminant)
    height_km = r_p - R
    if height_km < 0:
        return None
    return height_km
