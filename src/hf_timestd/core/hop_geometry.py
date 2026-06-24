#!/usr/bin/env python3
"""
HF skywave hop geometry — now a thin shim over :mod:`hamsci_dsp.geometry`.

The spherical law-of-cosines hop model (slant path length, launch/arrival
elevation, the inverse height-from-path) was the hf-timestd "single source of
truth" (review item S2); it has been promoted into hamsci_dsp.geometry as the
canonical shared implementation, and this module now delegates to it so the
math lives in exactly one place.

The functions keep hf-timestd's ``EARTH_RADIUS_KM`` (6371.0) as the default
Earth radius, so results are byte-identical to the previous local
implementation — the delegation is a pure de-duplication, not a numeric change.

Geometry (for reference; full derivation in hamsci_dsp.geometry):
one hop is the triangle (Earth centre C, ground point G at radius ``R``,
reflection point P at ``r_p = R + h`` above the hop midpoint). The half-hop
central angle is ``gamma = (d_hop / R) / 2`` and the per-leg slant follows the
law of cosines ``slant^2 = R^2 + r_p^2 - 2*R*r_p*cos(gamma)``; the total N-hop
path is ``N * 2 * slant``. Both reduce to the flat-Earth triangle as
``gamma -> 0``.

REFERENCE: Davies, K. (1990). "Ionospheric Radio." IEE Electromagnetic
Waves Series 31, §6 (oblique propagation geometry).
"""

from typing import Optional

from hamsci_dsp.geometry import (
    HopGeometry,
    hop_geometry as _hop_geometry,
    height_from_path as _height_from_path,
    max_single_hop_distance_km as _max_single_hop_distance_km,
    n_hops_for_distance as _n_hops_for_distance,
)

from .wwv_constants import EARTH_RADIUS_KM

# Speed of light, km per ms — kept for back-compat for callers that imported it
# from here. Identical to hamsci_dsp.constants.C_KM_MS, which backs
# HopGeometry.geometric_delay_ms.
C_LIGHT_KM_MS = 299.792458

__all__ = [
    "EARTH_RADIUS_KM",
    "C_LIGHT_KM_MS",
    "HopGeometry",
    "hop_geometry",
    "height_from_path",
    "max_single_hop_distance_km",
    "n_hops_for_distance",
]


def max_single_hop_distance_km(
    height_km: float,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> float:
    """Largest ground distance reachable in one hop (tangent-ray limit).

    Delegates to :func:`hamsci_dsp.geometry.max_single_hop_distance_km`.
    """
    return _max_single_hop_distance_km(height_km, earth_radius_km)


def n_hops_for_distance(
    ground_distance_km: float,
    height_km: float,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> int:
    """Minimum number of hops needed to span ``ground_distance_km`` (>= 1).

    Delegates to :func:`hamsci_dsp.geometry.n_hops_for_distance`.
    """
    return _n_hops_for_distance(ground_distance_km, height_km, earth_radius_km)


def hop_geometry(
    ground_distance_km: float,
    height_km: float,
    n_hops: int = 1,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> HopGeometry:
    """Spherical-Earth geometry of an N-hop skywave path.

    Delegates to :func:`hamsci_dsp.geometry.hop_geometry`.

    Raises:
        ValueError: if ``n_hops < 1`` or either distance is negative.
    """
    return _hop_geometry(ground_distance_km, height_km, n_hops, earth_radius_km)


def height_from_path(
    path_length_km: float,
    ground_distance_km: float,
    n_hops: int,
    earth_radius_km: float = EARTH_RADIUS_KM,
) -> Optional[float]:
    """Inverse of :func:`hop_geometry`: infer reflection height from a path.

    Delegates to :func:`hamsci_dsp.geometry.height_from_path`. Returns ``None``
    when the path is too short to be geometrically realisable.
    """
    return _height_from_path(
        path_length_km, ground_distance_km, n_hops, earth_radius_km
    )
