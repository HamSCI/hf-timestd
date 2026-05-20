"""
Unit tests for hf_timestd.core.tec_geometry

Pure-functional ionospheric geometry helpers:
- great_circle_distance — Haversine between two surface points
- calculate_midpoint — great circle midpoint
- calculate_elevation_angle — RX elevation to a single-hop reflection
- convert_slant_to_vertical — obliquity-corrected vertical TEC
- calculate_geometry_for_station — convenience aggregator
"""

import math

import pytest

from hf_timestd.core.tec_geometry import (
    DEFAULT_IONO_HEIGHT_KM,
    EARTH_RADIUS_KM,
    STATIONS,
    calculate_elevation_angle,
    calculate_geometry_for_station,
    calculate_midpoint,
    convert_slant_to_vertical,
    great_circle_distance,
)


# =============================================================================
# Module constants
# =============================================================================


class TestModuleConstants:
    def test_earth_radius_is_mean_radius(self):
        # 6371 km is the mean Earth radius (within 5 km of all common conventions)
        assert 6300 < EARTH_RADIUS_KM < 6400

    def test_default_iono_height_in_F_layer(self):
        # F-layer reflection altitude
        assert 200 <= DEFAULT_IONO_HEIGHT_KM <= 500

    def test_stations_present(self):
        for code in ['WWV', 'WWVH', 'CHU', 'BPM']:
            assert code in STATIONS
            entry = STATIONS[code]
            assert -90 <= entry['lat'] <= 90
            assert -180 <= entry['lon'] <= 180
            assert 'name' in entry


# =============================================================================
# great_circle_distance
# =============================================================================


class TestGreatCircleDistance:
    def test_zero_when_same_point(self):
        assert great_circle_distance(40.0, -105.0, 40.0, -105.0) == pytest.approx(0.0)

    def test_symmetric(self):
        d1 = great_circle_distance(40.0, -105.0, 21.0, -160.0)
        d2 = great_circle_distance(21.0, -160.0, 40.0, -105.0)
        assert d1 == pytest.approx(d2)

    def test_quarter_great_circle_along_equator(self):
        # 90° of longitude on the equator → quarter circumference
        d = great_circle_distance(0.0, 0.0, 0.0, 90.0)
        expected = math.pi * EARTH_RADIUS_KM / 2  # quarter circumference
        assert d == pytest.approx(expected, rel=1e-6)

    def test_pole_to_pole(self):
        # North pole to south pole → half the circumference
        d = great_circle_distance(90.0, 0.0, -90.0, 0.0)
        expected = math.pi * EARTH_RADIUS_KM
        assert d == pytest.approx(expected, rel=1e-6)

    def test_known_distance_wwv_to_wwvh(self):
        # WWV (Fort Collins) to WWVH (Kauai): ~5400 km
        wwv = STATIONS['WWV']
        wwvh = STATIONS['WWVH']
        d = great_circle_distance(wwv['lat'], wwv['lon'], wwvh['lat'], wwvh['lon'])
        # Conservative bracket
        assert 5000 < d < 5800


# =============================================================================
# calculate_midpoint
# =============================================================================


class TestCalculateMidpoint:
    def test_same_point_is_self(self):
        lat, lon = calculate_midpoint(40.0, -105.0, 40.0, -105.0)
        assert lat == pytest.approx(40.0)
        assert lon == pytest.approx(-105.0)

    def test_along_equator_is_arithmetic_mean(self):
        # Two equatorial points → midpoint also on equator, longitude is mean
        lat, lon = calculate_midpoint(0.0, 0.0, 0.0, 90.0)
        assert lat == pytest.approx(0.0, abs=1e-9)
        assert lon == pytest.approx(45.0, abs=1e-9)

    def test_along_meridian_is_arithmetic_mean(self):
        # Same longitude → midpoint latitude is the arithmetic mean
        lat, lon = calculate_midpoint(30.0, -100.0, 50.0, -100.0)
        assert lat == pytest.approx(40.0, abs=1e-6)
        assert lon == pytest.approx(-100.0, abs=1e-6)

    def test_midpoint_is_equidistant(self):
        # Great-circle midpoint must be equidistant from both endpoints
        lat1, lon1 = 40.0, -105.0
        lat2, lon2 = 30.0, -80.0
        m_lat, m_lon = calculate_midpoint(lat1, lon1, lat2, lon2)
        d1 = great_circle_distance(lat1, lon1, m_lat, m_lon)
        d2 = great_circle_distance(lat2, lon2, m_lat, m_lon)
        assert d1 == pytest.approx(d2, rel=1e-6)


# =============================================================================
# calculate_elevation_angle
# =============================================================================


class TestCalculateElevationAngle:
    def test_zenith_when_zero_distance(self):
        # Coincident TX/RX → straight up → 90°
        elev = calculate_elevation_angle(40.0, -105.0, 40.0, -105.0)
        assert elev == pytest.approx(90.0)

    def test_lower_elevation_at_longer_distance(self):
        # Increasing ground distance → lower elevation angle
        e_short = calculate_elevation_angle(40.0, -105.0, 40.0, -100.0)
        e_long = calculate_elevation_angle(40.0, -105.0, 40.0, -90.0)
        assert e_short > e_long

    def test_higher_iono_height_raises_elevation(self):
        # At the same ground distance, a higher reflection point increases elevation
        rx, tx = (40.0, -105.0), (40.0, -100.0)
        e_low = calculate_elevation_angle(*rx, *tx, h_iono=200.0)
        e_high = calculate_elevation_angle(*rx, *tx, h_iono=500.0)
        assert e_high > e_low

    def test_spherical_elevation_for_known_distance(self):
        # 2000 km path (1000 km half-distance), 350 km reflection height.
        # A flat-Earth triangle would give atan2(350, 1000) ≈ 19.3°; the
        # spherical formula (P-H8) must come out meaningfully lower because
        # the ground curves away from the receiver's local horizontal.
        rx_lat, rx_lon = 0.0, 0.0
        # 2000 km along the equator = 2000 / (2π * 6371 / 360) ≈ 17.985°
        deg_for_2000_km = 2000.0 / (math.pi * EARTH_RADIUS_KM / 180)
        elev = calculate_elevation_angle(rx_lat, rx_lon, 0.0, deg_for_2000_km, h_iono=350.0)

        gamma = 1000.0 / EARTH_RADIUS_KM
        r_p = EARTH_RADIUS_KM + 350.0
        expected = math.degrees(math.atan2(
            r_p * math.cos(gamma) - EARTH_RADIUS_KM, r_p * math.sin(gamma)))
        assert elev == pytest.approx(expected, abs=0.1)

        # Curvature lowers the elevation by several degrees vs flat-Earth.
        flat_earth = math.degrees(math.atan2(350.0, 1000.0))
        assert elev < flat_earth - 2.0

    def test_elevation_in_valid_range(self):
        # Any plausible HF link → 0 < elevation ≤ 90
        rx, tx = STATIONS['WWV'], STATIONS['CHU']
        elev = calculate_elevation_angle(rx['lat'], rx['lon'], tx['lat'], tx['lon'])
        assert 0.0 < elev <= 90.0


# =============================================================================
# convert_slant_to_vertical
# =============================================================================


class TestConvertSlantToVertical:
    def test_zenith_obliquity_is_unity(self):
        # Straight up → no path lengthening → M = 1.0, VTEC = TEC_slant
        vtec, m = convert_slant_to_vertical(30.0, elevation_angle_deg=90.0)
        assert m == pytest.approx(1.0, abs=1e-9)
        assert vtec == pytest.approx(30.0, abs=1e-9)

    def test_obliquity_grows_at_low_elevation(self):
        # Lower elevation → longer path through ionosphere → larger M, smaller VTEC
        vtec_high, m_high = convert_slant_to_vertical(30.0, elevation_angle_deg=80.0)
        vtec_low, m_low = convert_slant_to_vertical(30.0, elevation_angle_deg=10.0)
        assert m_low > m_high > 1.0
        assert vtec_low < vtec_high

    def test_obliquity_within_thin_shell_bound(self):
        # Single-layer model: M = 1/cos(asin(Re*cos(θ)/(Re+h)))
        # At θ=0 (grazing), M is bounded above by (Re+h)/sqrt(2*Re*h + h²)
        vtec, m = convert_slant_to_vertical(30.0, elevation_angle_deg=0.0)
        # Loose upper bound for h=350 km: M < ~3.5
        assert 1.0 < m < 4.0
        # vtec should be tec_slant / m
        assert vtec == pytest.approx(30.0 / m, rel=1e-6)

    def test_explicit_formula_matches(self):
        # Spot-check the formula at 45° with h=350 km
        theta = math.radians(45.0)
        sin_term = (EARTH_RADIUS_KM * math.cos(theta)) / (EARTH_RADIUS_KM + 350.0)
        expected_M = 1.0 / math.cos(math.asin(sin_term))

        _, m = convert_slant_to_vertical(20.0, 45.0, h_iono=350.0)
        assert m == pytest.approx(expected_M, rel=1e-9)

    def test_zero_slant_yields_zero_vertical(self):
        vtec, _ = convert_slant_to_vertical(0.0, elevation_angle_deg=30.0)
        assert vtec == pytest.approx(0.0)


# =============================================================================
# calculate_geometry_for_station
# =============================================================================


class TestCalculateGeometryForStation:
    @pytest.fixture
    def receiver(self):
        # AC0G location used in the module's __main__ example
        return (38.918461, -92.127974)

    def test_unknown_station_raises(self, receiver):
        with pytest.raises(ValueError, match="Unknown station"):
            calculate_geometry_for_station('KZN', *receiver)

    @pytest.mark.parametrize("station", ['WWV', 'WWVH', 'CHU', 'BPM'])
    def test_returns_full_geometry_dict(self, receiver, station):
        geom = calculate_geometry_for_station(station, *receiver)
        for key in ('midpoint_lat', 'midpoint_lon', 'elevation_deg',
                    'distance_km', 'tx_lat', 'tx_lon'):
            assert key in geom

    def test_tx_coords_match_station_table(self, receiver):
        geom = calculate_geometry_for_station('WWV', *receiver)
        assert geom['tx_lat'] == STATIONS['WWV']['lat']
        assert geom['tx_lon'] == STATIONS['WWV']['lon']

    def test_distance_matches_helper(self, receiver):
        geom = calculate_geometry_for_station('WWV', *receiver)
        rx_lat, rx_lon = receiver
        wwv = STATIONS['WWV']
        expected = great_circle_distance(rx_lat, rx_lon, wwv['lat'], wwv['lon'])
        assert geom['distance_km'] == pytest.approx(expected)

    def test_midpoint_matches_helper(self, receiver):
        geom = calculate_geometry_for_station('WWV', *receiver)
        rx_lat, rx_lon = receiver
        wwv = STATIONS['WWV']
        expected_lat, expected_lon = calculate_midpoint(rx_lat, rx_lon,
                                                        wwv['lat'], wwv['lon'])
        assert geom['midpoint_lat'] == pytest.approx(expected_lat)
        assert geom['midpoint_lon'] == pytest.approx(expected_lon)

    def test_iono_height_propagates_to_elevation(self, receiver):
        geom_low = calculate_geometry_for_station('WWV', *receiver, h_iono=200.0)
        geom_high = calculate_geometry_for_station('WWV', *receiver, h_iono=500.0)
        # Higher ionosphere → higher elevation angle for the same path
        assert geom_high['elevation_deg'] > geom_low['elevation_deg']
