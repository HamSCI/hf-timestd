"""
Tests for `core/hop_geometry` — the consolidated spherical hop geometry
(review item S2).

Pins down: forward geometry, the inverse `height_from_path`, the
flat-Earth limit, hop-count selection, input validation, and the
spherical-vs-flat divergence that S2 exists to eliminate.
"""

from __future__ import annotations

import math

import pytest

from hf_timestd.core.hop_geometry import (
    EARTH_RADIUS_KM,
    C_LIGHT_KM_MS,
    HopGeometry,
    height_from_path,
    hop_geometry,
    max_single_hop_distance_km,
    n_hops_for_distance,
)


# --------------------------------------------------------------------------
# Forward geometry
# --------------------------------------------------------------------------
def test_returns_hopgeometry_with_consistent_fields():
    g = hop_geometry(2000.0, 300.0, n_hops=1)
    assert isinstance(g, HopGeometry)
    assert g.n_hops == 1
    assert g.ground_distance_km == 2000.0
    assert g.height_km == 300.0
    # One up-leg + one down-leg.
    assert g.path_length_km == pytest.approx(2.0 * g.slant_per_leg_km)
    # The slant path is always longer than the ground distance it spans.
    assert g.path_length_km > g.ground_distance_km


def test_geometric_delay_property_matches_path_over_c():
    g = hop_geometry(3000.0, 300.0, n_hops=1)
    assert g.geometric_delay_ms == pytest.approx(g.path_length_km / C_LIGHT_KM_MS)


def test_elevation_falls_as_distance_grows():
    """A longer hop launches at a shallower angle."""
    elevs = [hop_geometry(d, 300.0, 1).elevation_deg for d in (200, 1000, 2000, 3000)]
    assert elevs == sorted(elevs, reverse=True)
    # All physical single hops here launch above the horizon.
    assert all(e > 0 for e in elevs)


def test_elevation_near_vertical_for_tiny_ground_distance():
    g = hop_geometry(1.0, 300.0, 1)
    assert g.elevation_deg > 89.0


def test_multihop_path_equals_n_single_hops():
    """An N-hop path is N independent hops of d/N each."""
    total = 6000.0
    g4 = hop_geometry(total, 300.0, n_hops=4)
    one = hop_geometry(total / 4, 300.0, n_hops=1)
    assert g4.path_length_km == pytest.approx(4.0 * one.path_length_km)
    assert g4.elevation_deg == pytest.approx(one.elevation_deg)


def test_law_of_cosines_self_consistency():
    """slant_per_leg must satisfy the law of cosines for the half-hop."""
    g = hop_geometry(2500.0, 280.0, n_hops=1)
    R = EARTH_RADIUS_KM
    r_p = R + 280.0
    gamma = g.central_angle_rad / 2.0
    expected = math.sqrt(R**2 + r_p**2 - 2 * R * r_p * math.cos(gamma))
    assert g.slant_per_leg_km == pytest.approx(expected)
    # Elevation, slant and the half-hop chord close the same triangle.
    assert g.slant_per_leg_km * math.sin(
        math.radians(g.elevation_deg)
    ) == pytest.approx(r_p * math.cos(gamma) - R)


# --------------------------------------------------------------------------
# Flat-Earth limit — the spherical model must reduce to the triangle
# --------------------------------------------------------------------------
def test_reduces_to_flat_triangle_for_short_paths():
    """At short range curvature is negligible: spherical ≈ flat triangle."""
    d, h = 60.0, 300.0
    flat = 2.0 * math.hypot(d / 2.0, h)
    sph = hop_geometry(d, h, 1).path_length_km
    assert sph == pytest.approx(flat, rel=1e-3)


def test_spherical_exceeds_flat_on_long_paths():
    """The bug S2 fixes: a flat triangle understates a long slant path."""
    d, h = 7000.0, 300.0
    n = 2
    flat = n * 2.0 * math.hypot(d / n / 2.0, h)
    sph = hop_geometry(d, h, n).path_length_km
    assert sph > flat
    # Divergence is real — over a percent on this WWVH-scale path.
    assert (sph - flat) / flat > 0.01


# --------------------------------------------------------------------------
# Inverse — height_from_path
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ground_km", [400.0, 1500.0, 3000.0, 7000.0])
@pytest.mark.parametrize("height_km", [110.0, 250.0, 350.0])
@pytest.mark.parametrize("n_hops", [1, 2, 3])
def test_inverse_round_trips_forward(ground_km, height_km, n_hops):
    """height_from_path is the exact inverse of hop_geometry's path."""
    path = hop_geometry(ground_km, height_km, n_hops).path_length_km
    recovered = height_from_path(path, ground_km, n_hops)
    assert recovered == pytest.approx(height_km, abs=1e-6)


def test_inverse_none_when_path_too_short():
    """A path shorter than the ground distance can't close the triangle."""
    assert height_from_path(10.0, 2000.0, 1) is None


def test_inverse_rejects_bad_arguments():
    assert height_from_path(0.0, 2000.0, 1) is None
    assert height_from_path(-100.0, 2000.0, 1) is None
    assert height_from_path(2500.0, 2000.0, 0) is None
    assert height_from_path(2500.0, -1.0, 1) is None


def test_inverse_height_responds_to_delay():
    """A longer observed path implies a higher reflection layer."""
    ground = 2000.0
    base = hop_geometry(ground, 300.0, 1).path_length_km
    lower = height_from_path(base - 50.0, ground, 1)
    higher = height_from_path(base + 50.0, ground, 1)
    assert lower < 300.0 < higher


# --------------------------------------------------------------------------
# Hop-count helpers
# --------------------------------------------------------------------------
def test_max_single_hop_distance_formula():
    h = 300.0
    assert max_single_hop_distance_km(h) == pytest.approx(
        2.0 * math.sqrt(2.0 * EARTH_RADIUS_KM * h + h**2)
    )


def test_n_hops_for_distance():
    assert n_hops_for_distance(1500.0, 300.0) == 1
    # A path past the single-hop tangent limit needs >= 2 hops.
    far = max_single_hop_distance_km(300.0) * 2.5
    assert n_hops_for_distance(far, 300.0) >= 3
    # Always at least one hop.
    assert n_hops_for_distance(10.0, 300.0) == 1


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------
def test_hop_geometry_rejects_bad_hop_count():
    with pytest.raises(ValueError):
        hop_geometry(2000.0, 300.0, n_hops=0)
    with pytest.raises(ValueError):
        hop_geometry(2000.0, 300.0, n_hops=-1)


def test_hop_geometry_rejects_negative_distances():
    with pytest.raises(ValueError):
        hop_geometry(-1.0, 300.0, 1)
    with pytest.raises(ValueError):
        hop_geometry(2000.0, -1.0, 1)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
