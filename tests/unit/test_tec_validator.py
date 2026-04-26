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
- IPP location calculation (midpoint heuristic)
- Station coordinate lookup (case-insensitive, unknown returns None)
- validate_batch wiring per-station IPP geometry into validate_tec_measurement
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hf_timestd.core.tec_validator import TECValidator


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
# IPP location
# =============================================================================


class TestIPPLocation:
    def test_midpoint_of_collinear_points(self, validator):
        ipp_lat, ipp_lon = validator.calculate_ipp_location(
            tx_lat=40.0, tx_lon=-100.0,
            rx_lat=20.0, rx_lon=-100.0,
        )
        # Simple arithmetic mean (the implementation is the simplified midpoint)
        assert ipp_lat == pytest.approx(30.0)
        assert ipp_lon == pytest.approx(-100.0)

    def test_midpoint_in_both_dimensions(self, validator):
        ipp_lat, ipp_lon = validator.calculate_ipp_location(
            tx_lat=40.0, tx_lon=-100.0,
            rx_lat=30.0, rx_lon=-90.0,
        )
        assert ipp_lat == pytest.approx(35.0)
        assert ipp_lon == pytest.approx(-95.0)


# =============================================================================
# validate_tec_measurement — flag transitions
# =============================================================================


class TestValidateTECMeasurement:
    def test_no_iono_model_returns_vtec_unavailable(self):
        with patch('hf_timestd.core.ionospheric_model.IonosphericModel',
                   side_effect=RuntimeError("boom")):
            v = TECValidator()
        result = v.validate_tec_measurement(_measurement(), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE
        assert result['vtec_tecu'] is None
        assert result['tec_bias_tecu'] is None

    def test_low_confidence_skips_validation(self, validator):
        result = validator.validate_tec_measurement(
            _measurement(confidence=0.4), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_UNVALIDATED
        # Iono model must not have been consulted
        validator.iono_model.get_ionex_vtec.assert_not_called()

    def test_threshold_confidence_passes_gate(self, validator):
        # Exactly the threshold should be allowed (0.5 is not < 0.5)
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/some/file')
        result = validator.validate_tec_measurement(
            _measurement(confidence=0.5), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_iso8601_timestamp_with_z(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc='2026-04-26T12:00:00Z'),
            40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        # The model was called with a parsed datetime
        _, kwargs = validator.iono_model.get_ionex_vtec.call_args
        assert isinstance(kwargs['timestamp'], datetime)

    def test_unix_epoch_timestamp(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        ts = 1745667600  # arbitrary epoch
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc=ts), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_datetime_passthrough(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        dt = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc=dt), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED

    def test_unparseable_timestamp_returns_failed(self, validator):
        result = validator.validate_tec_measurement(
            _measurement(timestamp_utc='not-a-date'), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    @pytest.mark.parametrize("bad_value", [None, float('nan'), float('inf')])
    def test_invalid_hf_tec_returns_failed(self, validator, bad_value):
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=bad_value), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    def test_ionex_returns_none_marks_vtec_unavailable(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = None
        result = validator.validate_tec_measurement(_measurement(), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VTEC_UNAVAILABLE
        assert result['vtec_tecu'] is None

    @pytest.mark.parametrize("bad_vtec", [0.5, 1.0, 500.0, 1000.0])
    def test_out_of_range_vtec_marks_failed(self, validator, bad_vtec):
        validator.iono_model.get_ionex_vtec.return_value = (bad_vtec, '/f')
        result = validator.validate_tec_measurement(_measurement(), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    def test_excessive_bias_marks_failed_but_records_values(self, validator):
        # HF=25, GPS=200 → bias=-175 TECU, beyond the 50 TECU bound
        validator.iono_model.get_ionex_vtec.return_value = (200.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=25.0), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED
        # Even on failure-by-bias, the raw HF/GPS comparison is preserved so
        # downstream telemetry can flag the magnitude of the disagreement.
        assert result['vtec_tecu'] == pytest.approx(200.0)
        assert result['tec_bias_tecu'] == pytest.approx(-175.0)

    def test_ionex_exception_marks_failed(self, validator):
        validator.iono_model.get_ionex_vtec.side_effect = RuntimeError("net error")
        result = validator.validate_tec_measurement(_measurement(), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATION_FAILED

    def test_successful_validation_records_bias(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (22.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=25.0), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        assert result['vtec_tecu'] == pytest.approx(22.0)
        assert result['tec_bias_tecu'] == pytest.approx(3.0)

    def test_negative_bias_when_hf_below_gps(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (40.0, '/f')
        result = validator.validate_tec_measurement(
            _measurement(tec_tecu=20.0), 40.0, -100.0)
        assert result['validation_flag'] == TECValidator.FLAG_VALIDATED
        assert result['tec_bias_tecu'] == pytest.approx(-20.0)


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

    def test_known_station_uses_midpoint_ipp(self, validator):
        validator.iono_model.get_ionex_vtec.return_value = (20.0, '/f')
        # WWV is in Fort Collins (40.678, -105.038)
        rx_lat, rx_lon = 30.0, -85.0
        measurements = [_measurement(station='WWV')]
        validator.validate_batch(measurements, rx_lat, rx_lon)

        # The IPP passed to get_ionex_vtec should equal the midpoint
        wwv_lat, wwv_lon = 40.678, -105.038
        expected_lat = (wwv_lat + rx_lat) / 2.0
        expected_lon = (wwv_lon + rx_lon) / 2.0

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
