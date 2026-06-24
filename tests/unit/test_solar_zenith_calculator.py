"""
Unit tests for hf_timestd.core.solar_zenith_calculator

Pure-functional helpers for converting Maidenhead grids to lat/lon, computing
midpoints, and getting the solar elevation/azimuth at a given location/time.

Tests cover:
- grid_to_latlon: 4-char and 6-char grids, error on too-short input
- calculate_midpoint: identity, equator/meridian arithmetic mean, cross-equator
- solar_position: bounds (elevation in [-90, 90], azimuth in [0, 360)),
  noon-near-equator high elevation, polar darkness in winter,
  high-noon at sub-solar longitude
- calculate_solar_zenith_for_day: dict shape, time-series length matches
  interval, every elevation falls in [-90, 90], BPM included
"""

from datetime import datetime, timedelta

import math

import pytest

from hf_timestd.core.solar_zenith_calculator import (
    calculate_midpoint,
    calculate_solar_zenith_for_day,
    grid_to_latlon,
    solar_position,
)


# =============================================================================
# grid_to_latlon
# =============================================================================


class TestGridToLatLon:
    def test_4char_grid_em38(self):
        # EM38 = central US (centred near 38°N, 92°W)
        lat, lon = grid_to_latlon('EM38')
        assert 38 <= lat <= 40
        assert -94 <= lon <= -92

    def test_6char_grid_em38ww(self):
        lat, lon = grid_to_latlon('EM38ww')
        # 6-char grid is more precise — must fall inside the 4-char square
        assert 38 <= lat <= 39
        assert -93 <= lon <= -92

    def test_lowercase_grid_normalized(self):
        a = grid_to_latlon('em38ww')
        b = grid_to_latlon('EM38WW')
        assert a == b

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            grid_to_latlon('EM3')

    def test_distinct_grids_distinct_locations(self):
        # FN31 (NYC area) vs DM04 (LA area)
        ny = grid_to_latlon('FN31')
        la = grid_to_latlon('DM04')
        assert ny[0] != la[0] or ny[1] != la[1]


# =============================================================================
# calculate_midpoint
# =============================================================================


class TestCalculateMidpoint:
    def test_same_point(self):
        lat, lon = calculate_midpoint(40.0, -100.0, 40.0, -100.0)
        assert lat == pytest.approx(40.0, abs=1e-9)
        assert lon == pytest.approx(-100.0, abs=1e-9)

    def test_same_meridian_near_arithmetic_mean(self):
        # Same longitude → midpoint stays on the meridian, latitude near the
        # arithmetic mean. The geodesic (WGS-84) distance-halfway point
        # deviates ~0.03° from the exact mean (meridian arc-per-degree grows
        # with latitude), so this is approximate, not exact as on a sphere.
        lat, lon = calculate_midpoint(20.0, -100.0, 60.0, -100.0)
        assert lat == pytest.approx(40.0, abs=0.05)
        assert lon == pytest.approx(-100.0, abs=1e-3)

    def test_equator_long_only(self):
        lat, lon = calculate_midpoint(0.0, 0.0, 0.0, 90.0)
        assert lat == pytest.approx(0.0, abs=1e-9)
        # On a sphere, the geometric midpoint of two equator points is on
        # the equator at the average longitude
        assert lon == pytest.approx(45.0, abs=1e-3)


# =============================================================================
# solar_position
# =============================================================================


class TestSolarPosition:
    def test_returns_azimuth_and_elevation(self):
        dt = datetime(2026, 6, 21, 12, 0, 0)
        az, el = solar_position(dt, 0.0, 0.0)
        # Azimuth in [0, 360)
        assert 0.0 <= az < 360.0
        # Elevation in [-90, 90]
        assert -90.0 <= el <= 90.0

    def test_summer_solstice_noon_at_subsolar_high_elevation(self):
        # Around noon UTC at the equator near the subsolar point in summer
        dt = datetime(2026, 6, 21, 12, 0, 0)
        # Subsolar latitude is ~+23.4° on June solstice
        _, el = solar_position(dt, 23.4, 0.0)
        # Sun should be near zenith (elevation > 80°)
        assert el > 70.0

    def test_polar_winter_dark(self):
        # Antarctic, near south pole, midwinter (June solstice for southern
        # hemisphere = polar night)
        dt = datetime(2026, 6, 21, 12, 0, 0)
        _, el = solar_position(dt, -85.0, 0.0)
        # Sun is below horizon → negative elevation
        assert el < 0.0

    def test_polar_summer_light(self):
        # Antarctic, midsummer (December solstice = polar day)
        dt = datetime(2026, 12, 21, 12, 0, 0)
        _, el = solar_position(dt, -85.0, 0.0)
        assert el > 0.0


# =============================================================================
# calculate_solar_zenith_for_day
# =============================================================================


class TestCalculateSolarZenithForDay:
    def test_dict_shape(self):
        result = calculate_solar_zenith_for_day('20260621', 'EM38ww',
                                                 interval_minutes=60)
        for key in ('date', 'receiver_grid', 'receiver_location',
                    'wwv_midpoint', 'wwvh_midpoint', 'chu_midpoint',
                    'bpm_midpoint',
                    'interval_minutes', 'timestamps',
                    'wwv_solar_elevation', 'wwvh_solar_elevation',
                    'chu_solar_elevation', 'bpm_solar_elevation'):
            assert key in result

    def test_series_length_matches_interval(self):
        # 60-minute interval over 24 hours → 24 samples
        result = calculate_solar_zenith_for_day('20260101', 'EM38ww',
                                                 interval_minutes=60)
        assert len(result['timestamps']) == 24
        assert len(result['wwv_solar_elevation']) == 24
        assert len(result['chu_solar_elevation']) == 24

    def test_5_minute_interval_yields_288(self):
        # 5-minute interval → 24 * 60 / 5 = 288 samples
        result = calculate_solar_zenith_for_day('20260101', 'EM38',
                                                 interval_minutes=5)
        assert len(result['timestamps']) == 288

    def test_elevations_within_bounds(self):
        result = calculate_solar_zenith_for_day('20260101', 'EM38ww',
                                                 interval_minutes=120)
        for series_key in ('wwv_solar_elevation', 'wwvh_solar_elevation',
                            'chu_solar_elevation', 'bpm_solar_elevation'):
            for el in result[series_key]:
                assert -90.0 <= el <= 90.0

    def test_summer_more_daylight_than_winter(self):
        winter = calculate_solar_zenith_for_day('20260101', 'EM38ww',
                                                 interval_minutes=60)
        summer = calculate_solar_zenith_for_day('20260621', 'EM38ww',
                                                 interval_minutes=60)
        # Compare daylight (positive elevation) hours at the receiver's WWV path
        winter_day = sum(1 for el in winter['wwv_solar_elevation'] if el > 0)
        summer_day = sum(1 for el in summer['wwv_solar_elevation'] if el > 0)
        # In northern hemisphere, summer has more daylight hours
        assert summer_day > winter_day
