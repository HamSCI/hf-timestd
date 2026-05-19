"""
Unit tests for hf_timestd.core.tec_validator

The validator compares HF-derived TEC against GPS VTEC pulled from IONEX
files via IonosphericModel.get_ionex_vtec(). All tests stub the
IonosphericModel to avoid touching IRI / on-disk IONEX data.

Covers:
- Validation flag transitions: VALIDATED, UNVALIDATED, VTEC_UNAVAILABLE,
  VALIDATION_FAILED
- Confidence gating (MIN_CONFIDENCE_FOR_VALIDATION)
- Bias bound (MAX_TEC_DIFFERENCE_TECU)
- Timestamp parsing (ISO8601 string, Unix epoch, datetime)
- IPP location (great-circle midpoint, P-H9)
- Slant->vertical obliquity mapping before comparison (P-H9)
- Non-physical elevation gating
- Station coordinate lookup (case-insensitive, unknown returns None)
- validate_batch wiring per-station IPP geometry into validate_tec_measurement
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hf_timestd.core.tec_validator import TECValidator
from hf_timestd.core.tec_geometry import calculate_midpoint, convert_slant_to_vertical


# A nominal mid-sky elevation for direct validate_tec_measurement() calls
# whose assertions are about flag transitions, not the bias magnitude.
NOMINAL_ELEV = 45.0


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def validator():
    """Validator with a stub IonosphericModel — no real IONEX I/O."""
    with patch('hf_timestd.core.ionospheric_model.IonosphericModel') as MockModel:
        mock_iono = MagicMock()
        MockModel.return_value = mock_iono
        v = TECValidator()
        # Replace the real instance so tests can drive get_ionex_vtec directly.
        v.iono_model = mock_iono
    return v


def _measurement(**overrides):
    base = {
        'timestamp_utc': '2026-04-26T12:00:00Z',
        'tec_tecu': 25.0,
        'confidence': 0.9,
        'station': 'WWV',
    }
    base.update(overrides)
    return base


# =============================================================================
# Initialization
# =============================================================================


class TestInitialization:
    def test_default_ionex_dir(self):
        from pathlib import Path
        with patch('hf_timestd.core.ionospheric_model.IonosphericModel'):
            v = TECValidator()
        assert v.ionex_dir == Path('/var/lib/timestd/ionex')

    def test_custom_ionex_dir(self, tmp_path):
        with patch('hf_timestd.core.ionospheric_model.IonosphericModel'):
            v = TECValidator(ionex_dir=tmp_path)
        assert v.ionex_dir == tmp_path

    def test_iono_model_failure_logged_and_set_none(self, caplog):
        with patch('hf_timestd.core.ionospheric_model.IonosphericModel',
                   side_effect=RuntimeError("boom")):
            v = TECValidator()
        assert v.iono_model is None
        assert any('Failed to initialize ionospheric model' in r.message
                   for r in caplog.records)


# =============================================================================
# Station lookup
# =============================================================================


class TestStationLookup:
    @pytest.mark.parametrize("station", ['WWV', 'WWVH', 'CHU', 'BPM'])
    def test_known_stations(self, validator, station):
        coords = validator.get_station_location(station)
        assert coords is not None
        lat, lon = coords
        assert -90 <= lat <= 90
        assert -180 <= lon <= 180

    def test_unknown_station_returns_none(self, validator):
        assert validator.get_station_location('KZN') is None

    def test_lookup_is_case_insensitive(self, validator):
        upper = validator.get_station_location('WWV')
        lower = validator.get_station_location('wwv')
        mixed = validator.get_station_location('WwV')
        assert upper == lower == mixed


# =============================================================================
# IPP location — great-circle midpoint (P-H9)
# =============================================================================


class TestIPPLocation:
    def test_midpoint_of_collinear_points(self, validator):
        # Two points on the same meridian: the meridian is a great circle, so
        # the great-circle midpoint is the plain latitude average.
        ipp_lat, ipp_lon = validator.calculate_ipp_location(
            tx_lat=40.0, tx_lon=-100.0,
            rx_lat=20.0, rx_lon=-100.0,
        )
        assert ipp_lat == pytest.approx(30.0)
        assert ipp_lon == pytest.approx(-100.0)

    def test_midpoint_matches_great_circle_helper(self, validator):
        ipp_lat, ipp_lon = validator.calculate_ipp_location(
            tx_lat=40.0, tx_lon=-100.0,
            rx_lat=30.0, rx_lon=-90.0,
        )
        gc_lat, gc_lon = calculate_midpoint(40.0, -100.0, 30.0, -90.0)
        assert ipp_lat == pytest.approx(gc_lat)
        assert ipp_lon == pytest.approx(gc_lon)

    def test_ipp_is_great_circle_not_cartesian(self, validator):
        # Two 60°N points 120° of longitude apart: the great-circle path bows
        # poleward, so the IPP latitude exceeds 60° — the Cartesian lat/lon
        # mean (the pre-P-H9 behaviour) would wrongly give exactly 60°N.
        ipp_lat, ipp_lon = validator.calculate_ipp_location(
            tx_lat=60.0, tx_lon=-150.0,
            rx_lat=60.0, rx_lon=-30.0,
        )
        assert ipp_lat > 60.5
        assert ipp_lon == pytest.approx(-90.0)


# =============================================================================
# validate_tec_measurement — flag transitions
# =============================================================================


class TestValidateTECMeasurement:
    def test_no_iono_model_returns_vtec_unavailable(self):
        with patch('hf_timestd.core.ionospheric_model.IonosphericModel',
                   side_effect=RuntimeError("boom")):
            v = TECValidator()
        result = v.validate_tec_measurement(_measurement(), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE
        assert result['vtec_tecu'] is None
        assert result['tec_bias_tecu'] is None

    def test_low_confidence_skips_validation(self, validator):
        result = validator.validate_tec_measurement(
            _measurement(confidence=0.4), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_UNVALIDATED
        # Iono model must not have been consulted
        validator.iono_model.get_ionex_vtec.assert_not_called()

    def test_threshold_confidence_passes_gate(self, validator):
        # Exactly the threshold should be allowed (0.5 is not < 0.5)
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/some/file')
        result = validator.validate_tec_measurement(
            _measurement(confidence=0.5), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_iso8601_timestamp_with_z(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc='2026-04-26T12:00:00Z'),
            40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        # The model was called with a parsed datetime
        _, kwargs = validator.iono_model.get_ionex_vtec.call_args
        assert isinstance(kwargs['timestamp'], datetime)

    def test_unix_epoch_timestamp(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        ts = 1745667600  # arbitrary epoch
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc=ts), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_datetime_passthrough(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        dt = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc=dt), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_unparseable_timestamp_returns_failed(self, validator):
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc='not-a-date'), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    @pytest.mark.parametrize("bad_value", [None, float('nan'), float('inf')])
    def test_invalid_hf_tec_returns_failed(self, validator, bad_value):
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=bad_value), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    @pytest.mark.parametrize("bad_elev", [0.0, -5.0, float('nan'), float('inf')])
    def test_nonphysical_elevation_returns_failed(self, validator, bad_elev):
        # No single-hop obliquity mapping exists for a non-positive or
        # non-finite elevation; the measurement cannot be validated.
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, bad_elev)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED
        validator.iono_model.get_ionex_vtec.assert_not_called()

    def test_ionex_returns_none_marks_vtec_unavailable(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = None
        result = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE
        assert result['vtec_tecu'] is None

    @pytest.mark.parametrize("bad_vtec", [0.05, 500.0, 1000.0])
    def test_out_of_range_vtec_marks_failed(self, validator, bad_vtec):
        # Out of range: below the 0.1 TECU floor, or at/above the 500 cap.
        validator.iono_model.get_ionex_vtec.return_value = (bad_vtec, '/f')
        result = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    @pytest.mark.parametrize("low_vtec", [0.1, 0.5])
    def test_low_night_vtec_within_floor_is_accepted(self, validator, low_vtec):
        # P-M7: deep-night VTEC below 1 TECU is physically valid — the 0.1
        # floor must not reject it (the old 1.0 floor did). Reaching a
        # non-FAILED flag proves the range check passed.
        validator.iono_model.get_ionex_vtec.return_value = (low_vtec, '/f')
        result = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_excessive_bias_marks_failed_but_records_values(self, validator):
        # HF slant=25 -> vertical ~18.6; GPS=200 -> bias well beyond 50 TECU.
        validator.iono_model.get_ionex_vtec.return_value = (200.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=25.0), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED
        # Even on failure-by-bias, the vertical-vs-vertical comparison is
        # preserved so downstream telemetry can flag the disagreement.
        hf_vtec, _ = convert_slant_to_vertical(25.0, NOMINAL_ELEV)
        assert result['vtec_tecu'] == pytest.approx(200.0)
        assert result['tec_bias_tecu'] == pytest.approx(hf_vtec - 200.0)

    def test_ionex_io_error_marks_vtec_unavailable(self, validator):
        # P-M7: a missing/corrupt IONEX file (OSError/ValueError) means the
        # GPS VTEC is genuinely unavailable — flag VTEC_UNAVAILABLE, not
        # VALIDATION_FAILED (validation could not run; it did not fail).
        validator.iono_model.get_ionex_vtec.side_effect = OSError("ionex missing")
        result = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE

    def test_unexpected_ionex_error_propagates(self, validator):
        # P-M7: only IO/parse errors are caught. An unexpected error is a
        # real bug and must surface, not be masked as a failed validation.
        validator.iono_model.get_ionex_vtec.side_effect = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            validator.validate_tec_measurement(
                _measurement(), 40.0, -100.0, NOMINAL_ELEV)

    def test_successful_validation_records_bias(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=25.0), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        hf_vtec, _ = convert_slant_to_vertical(25.0, NOMINAL_ELEV)
        assert result['vtec_tecu'] == pytest.approx(22.0)
        assert result['hf_vtec_tecu'] == pytest.approx(hf_vtec)
        assert result['tec_bias_tecu'] == pytest.approx(hf_vtec - 22.0)

    def test_negative_bias_when_hf_below_gps(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (40.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=20.0), 40.0, -100.0, NOMINAL_ELEV)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        hf_vtec, _ = convert_slant_to_vertical(20.0, NOMINAL_ELEV)
        assert result['tec_bias_tecu'] == pytest.approx(hf_vtec - 40.0)
        assert result['tec_bias_tecu'] < 0.0


# =============================================================================
# Obliquity correction — slant -> vertical before comparison (P-H9)
# =============================================================================


class TestObliquityCorrection:
    def test_hf_slant_is_mapped_to_vertical_before_comparison(self, validator):
        # At low elevation a slant TEC sits well above the vertical GPS VTEC
        # purely from path geometry. Pre-P-H9 (raw slant minus vertical) this
        # looked like a large bias; the obliquity mapping removes it.
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        elevation = 15.0  # low elevation -> large obliquity factor
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=50.0), 40.0, -100.0, elevation)

        hf_vtec, M = convert_slant_to_vertical(50.0, elevation)
        assert M > 1.0
        assert result['obliquity_factor'] == pytest.approx(M)
        assert result['hf_vtec_tecu'] == pytest.approx(hf_vtec)
        # Vertical TEC is smaller than the slant TEC it was mapped from.
        assert result['hf_vtec_tecu'] < 50.0
        # The reported bias is vertical-vs-vertical, far smaller than the
        # raw slant-vs-vertical difference (50 - 20 = 30 TECU).
        assert result['tec_bias_tecu'] == pytest.approx(hf_vtec - 20.0)
        assert abs(result['tec_bias_tecu']) < 30.0

    def test_obliquity_factor_grows_as_elevation_drops(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        high = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, 80.0)
        low = validator.validate_tec_measurement(
            _measurement(), 40.0, -100.0, 10.0)
        assert low['obliquity_factor'] > high['obliquity_factor'] > 1.0


# =============================================================================
# validate_batch
# =============================================================================


class TestValidateBatch:
    def test_unknown_station_short_circuits_to_failed(self, validator):
        measurements = [_measurement(station='UNKNOWN')]
        result = validator.validate_batch(measurements,
                                          receiver_lat=40.0, receiver_lon=-100.0)
        assert len(result) == 1
        assert result[0]['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED
        # No iono lookup for unknown stations
        validator.iono_model.get_ionex_vtec.assert_not_called()

    def test_known_station_uses_great_circle_ipp(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        rx_lat, rx_lon = 30.0, -85.0
        measurements = [_measurement(station='WWV')]
        validator.validate_batch(measurements, rx_lat, rx_lon)

        # The IPP passed to get_ionex_vtec is the great-circle midpoint of
        # the WWV->RX path, using the validator's own canonical coordinates.
        wwv_lat, wwv_lon = validator.get_station_location('WWV')
        expected_lat, expected_lon = calculate_midpoint(
            wwv_lat, wwv_lon, rx_lat, rx_lon)

        _, kwargs = validator.iono_model.get_ionex_vtec.call_args
        assert kwargs['lat'] == pytest.approx(expected_lat)
        assert kwargs['lon'] == pytest.approx(expected_lon)

    def test_batch_preserves_original_fields(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        m = _measurement(station='WWV', extra_field='preserved')
        result = validator.validate_batch([m], 40.0, -100.0)
        # Original fields survive
        assert result[0]['extra_field'] == 'preserved'
        assert result[0]['tec_tecu'] == m['tec_tecu']
        # Validation fields appended
        assert 'validation_flag' in result[0]
        assert 'tec_bias_tecu' in result[0]
        assert 'vtec_tecu' in result[0]
        assert 'hf_vtec_tecu' in result[0]
        assert 'obliquity_factor' in result[0]

    def test_batch_length_matches_input(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        measurements = [
            _measurement(station='WWV'),
            _measurement(station='CHU'),
            _measurement(station='UNKNOWN'),
        ]
        result = validator.validate_batch(measurements, 40.0, -100.0)
        assert len(result) == 3

    def test_mixed_outcomes_per_measurement(self, validator):
        # First call returns valid, second returns None → second is unavailable
        validator.iono_model.get_ionex_vtec.side_effect = [
            (22.0, '/f'),  # first measurement
            None,          # second measurement
        ]
        result = validator.validate_batch([
            _measurement(station='WWV'),
            _measurement(station='CHU'),
        ], 40.0, -100.0)

        assert result[0]['validation_flag'] == TECValidator.FLAG_VALIDATED
        assert result[1]['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE
