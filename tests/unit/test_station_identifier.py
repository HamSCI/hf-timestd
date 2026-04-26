"""
Unit tests for hf_timestd.core.station_identifier

Physics-based station identification with three operational phases:
BOOTSTRAP (frequency/modulation only), REFINEMENT (adds timing validation),
and MEASUREMENT (timing-window discrimination).

Tests cover:
- Module constants (anchor and shared frequency tables)
- StationDelayModel: validation-window scaling with sample count and σ;
  is_timing_consistent boundary
- StationIdentification dataclass defaults
- BOOTSTRAP path: anchor frequency, FSK, WWVH 1200 Hz tone, WWV 1000 Hz tone,
  ambiguous skip, unknown frequency
- REFINEMENT path: passes through bootstrap's unambiguous identifications,
  timing-validation success/rejection, shared-frequency timing discrimination,
  no-match fallback. Note: the identifier currently reads delay models keyed
  by station name only (WWV/WWVH/BPM), while update_delay_model writes
  composite keys "{station}_{frequency_mhz}". Tests that exercise the
  timing-based paths populate the dict directly with the station-name keys
  the identifier looks up, and document the inconsistency.
- MEASUREMENT path: anchor frequency, temporal-window match, outside-windows,
  unknown frequency
- update_delay_model: creates new model, running mean / Welford std,
  composite-key storage; get_delay_model retrieves via the same key
- get_all_delay_models returns a copy (mutating it doesn't affect state)
"""

from unittest.mock import MagicMock

import math

import numpy as np
import pytest

from hf_timestd.core.operational_phase_manager import OperationalPhase
from hf_timestd.core.station_identifier import (
    ANCHOR_FREQUENCIES,
    SHARED_FREQUENCIES,
    StationDelayModel,
    StationIdentification,
    StationIdentifier,
)


# =============================================================================
# Module-level constants
# =============================================================================


class TestModuleConstants:
    def test_anchor_frequencies_disjoint_from_shared(self):
        assert set(ANCHOR_FREQUENCIES).isdisjoint(set(SHARED_FREQUENCIES))

    def test_anchor_frequencies_have_known_stations(self):
        assert ANCHOR_FREQUENCIES[3.33] == 'CHU'
        assert ANCHOR_FREQUENCIES[7.85] == 'CHU'
        assert ANCHOR_FREQUENCIES[14.67] == 'CHU'
        assert ANCHOR_FREQUENCIES[20.0] == 'WWV'
        assert ANCHOR_FREQUENCIES[25.0] == 'WWV'

    def test_shared_frequencies_are_canonical(self):
        # The HF time-and-frequency shared bands
        assert sorted(SHARED_FREQUENCIES) == [2.5, 5.0, 10.0, 15.0]


# =============================================================================
# StationDelayModel
# =============================================================================


class TestStationDelayModel:
    def _model(self, *, mean=10.0, std=0.5, n=20, ts=1700000000.0,
               station='WWV', freq=10.0):
        return StationDelayModel(
            station=station,
            frequency_mhz=freq,
            mean_delay_ms=mean,
            std_delay_ms=std,
            n_measurements=n,
            last_updated=ts,
        )

    def test_validation_window_during_bootstrap(self):
        # Few measurements → wide window
        m = self._model(n=5)
        assert m.get_validation_window_ms() == 5.0

    def test_validation_window_when_confident(self):
        # Many measurements with tight std → narrow window
        m = self._model(n=50, std=0.2)
        assert m.get_validation_window_ms() == 2.0

    def test_validation_window_otherwise(self):
        # Many measurements but loose std → medium window
        m = self._model(n=50, std=2.5)
        assert m.get_validation_window_ms() == 3.0

    def test_timing_consistent_inside_window(self):
        m = self._model(mean=10.0, std=0.2, n=50)  # window = 2.0
        assert m.is_timing_consistent(11.0) is True
        assert m.is_timing_consistent(9.5) is True

    def test_timing_consistent_outside_window(self):
        m = self._model(mean=10.0, std=0.2, n=50)
        assert m.is_timing_consistent(13.0) is False
        assert m.is_timing_consistent(7.0) is False

    def test_timing_consistent_at_boundary_is_strict(self):
        # Boundary is strict less-than: at exactly window distance → False
        m = self._model(mean=10.0, std=0.2, n=50)
        assert m.is_timing_consistent(12.0) is False
        assert m.is_timing_consistent(8.0) is False


# =============================================================================
# StationIdentification dataclass
# =============================================================================


class TestStationIdentification:
    def test_defaults(self):
        r = StationIdentification(
            station='WWV', confidence=1.0,
            method='anchor_frequency', reason='WWV anchor frequency',
        )
        assert r.timing_validated is False
        assert r.timing_error_ms is None


# =============================================================================
# Identifier helpers
# =============================================================================


def make_identifier(phase: OperationalPhase) -> StationIdentifier:
    """Build a StationIdentifier with a phase manager fixed to `phase`."""
    pm = MagicMock()
    pm.get_phase.return_value = phase
    return StationIdentifier(operational_phase_manager=pm)


# =============================================================================
# BOOTSTRAP phase
# =============================================================================


class TestBootstrapIdentification:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.BOOTSTRAP)

    def test_anchor_frequency_returns_full_confidence(self, ident):
        r = ident.identify(
            frequency_mhz=20.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station == 'WWV'
        assert r.confidence == 1.0
        assert r.method == 'anchor_frequency'

    def test_chu_anchor_frequencies(self, ident):
        for freq in (3.33, 7.85, 14.67):
            r = ident.identify(
                frequency_mhz=freq,
                has_1000hz_tone=False, has_1200hz_tone=False,
                measured_delay_ms=0.0,
            )
            assert r.station == 'CHU'
            assert r.method == 'anchor_frequency'

    def test_fsk_modulation_identifies_chu(self, ident):
        # Even on a non-anchor frequency, FSK → CHU
        r = ident.identify(
            frequency_mhz=10.0,  # shared, would be ambiguous otherwise
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
            has_fsk=True,
        )
        assert r.station == 'CHU'
        assert r.confidence == 1.0
        assert r.method == 'fsk_modulation'

    def test_wwvh_1200hz_tone_unambiguous(self, ident):
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=True,
            measured_delay_ms=0.0,
        )
        assert r.station == 'WWVH'
        assert r.confidence == 1.0
        assert r.method == 'unique_tone'

    def test_wwv_1000hz_tone_likely(self, ident):
        # 1000 Hz with no 1200 Hz → likely WWV but BPM possible (so 0.9)
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=True, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station == 'WWV'
        assert r.confidence == 0.9
        assert r.method == 'likely_wwv'

    def test_ambiguous_shared_skipped(self, ident):
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station is None
        assert r.confidence == 0.0
        assert r.method == 'bootstrap_skip'

    def test_unknown_frequency_skipped(self, ident):
        r = ident.identify(
            frequency_mhz=11.111,  # not anchor, not shared
            has_1000hz_tone=True, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station is None
        assert r.method == 'unknown_frequency'


# =============================================================================
# REFINEMENT phase
# =============================================================================


class TestRefinementIdentification:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.REFINEMENT)

    def test_anchor_passes_through_when_no_model(self, ident):
        # Bootstrap returns full confidence, no timing model exists yet,
        # so the bootstrap result is returned unchanged.
        r = ident.identify(
            frequency_mhz=20.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station == 'WWV'
        assert r.method == 'anchor_frequency'

    def test_anchor_validated_by_timing(self, ident):
        # NOTE: identifier looks up models keyed by *station name* (not
        # composite "{station}_{frequency_mhz}"). Populate directly to
        # exercise the timing-validated branch.
        ident.station_delay_models['WWV'] = StationDelayModel(
            station='WWV', frequency_mhz=20.0,
            mean_delay_ms=10.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        r = ident.identify(
            frequency_mhz=20.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=10.5,
        )
        assert r.station == 'WWV'
        assert r.method == 'frequency_timing_validated'
        assert r.timing_validated is True
        assert r.timing_error_ms == pytest.approx(0.5)

    def test_anchor_rejected_by_timing(self, ident):
        ident.station_delay_models['WWV'] = StationDelayModel(
            station='WWV', frequency_mhz=20.0,
            mean_delay_ms=10.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        r = ident.identify(
            frequency_mhz=20.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=20.0,  # way outside window
        )
        assert r.station is None
        assert r.method == 'timing_rejection'
        assert r.timing_validated is False
        assert r.timing_error_ms == pytest.approx(10.0)

    def test_shared_frequency_timing_discrimination(self, ident):
        # WWVH at ~25 ms, WWV at ~5 ms — measurement closer to WWV
        ident.station_delay_models['WWV'] = StationDelayModel(
            station='WWV', frequency_mhz=10.0,
            mean_delay_ms=5.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        ident.station_delay_models['WWVH'] = StationDelayModel(
            station='WWVH', frequency_mhz=10.0,
            mean_delay_ms=25.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=5.3,
        )
        assert r.station == 'WWV'
        assert r.method == 'timing_discrimination'
        assert r.confidence == pytest.approx(0.95)
        assert r.timing_validated is True

    def test_shared_frequency_no_match(self, ident):
        # No timing model fits — we get 'timing_no_match'
        ident.station_delay_models['WWV'] = StationDelayModel(
            station='WWV', frequency_mhz=10.0,
            mean_delay_ms=5.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=100.0,
        )
        assert r.station is None
        assert r.method == 'timing_no_match'

    def test_non_shared_unknown_returns_refinement_skip(self, ident):
        r = ident.identify(
            frequency_mhz=11.111,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station is None
        assert r.method == 'refinement_skip'


# =============================================================================
# MEASUREMENT phase
# =============================================================================


class TestMeasurementIdentification:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.MEASUREMENT)

    def test_anchor_frequency_in_measurement(self, ident):
        r = ident.identify(
            frequency_mhz=25.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station == 'WWV'
        assert r.method == 'anchor_frequency'

    def test_temporal_window_match(self, ident):
        ident.station_delay_models['WWVH'] = StationDelayModel(
            station='WWVH', frequency_mhz=10.0,
            mean_delay_ms=20.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        # Measurement-phase window is ±1ms regardless of model std
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=20.5,
        )
        assert r.station == 'WWVH'
        assert r.method == 'temporal_window'
        assert r.timing_validated is True
        assert r.timing_error_ms == pytest.approx(0.5)

    def test_outside_temporal_windows(self, ident):
        ident.station_delay_models['WWV'] = StationDelayModel(
            station='WWV', frequency_mhz=10.0,
            mean_delay_ms=5.0, std_delay_ms=0.2,
            n_measurements=50, last_updated=0.0,
        )
        r = ident.identify(
            frequency_mhz=10.0,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=50.0,
        )
        assert r.station is None
        assert r.method == 'outside_windows'

    def test_unknown_frequency_in_measurement(self, ident):
        r = ident.identify(
            frequency_mhz=11.111,
            has_1000hz_tone=False, has_1200hz_tone=False,
            measured_delay_ms=0.0,
        )
        assert r.station is None
        assert r.method == 'measurement_unknown'


# =============================================================================
# Delay-model maintenance
# =============================================================================


class TestUpdateDelayModel:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.BOOTSTRAP)

    def test_first_measurement_creates_model(self, ident):
        ident.update_delay_model('WWV', 10.0, 5.0, timestamp=1.0)
        # Composite-key storage: "{station}_{freq}"
        key = 'WWV_10.0'
        assert key in ident.station_delay_models
        m = ident.station_delay_models[key]
        assert m.mean_delay_ms == 5.0
        assert m.std_delay_ms == 5.0  # initial uncertainty
        assert m.n_measurements == 1
        assert m.last_updated == 1.0

    def test_two_measurements_average(self, ident):
        ident.update_delay_model('WWV', 10.0, 4.0, timestamp=1.0)
        ident.update_delay_model('WWV', 10.0, 6.0, timestamp=2.0)
        m = ident.station_delay_models['WWV_10.0']
        # n=2 path: mean updates but std stays at initial sentinel
        assert m.mean_delay_ms == pytest.approx(5.0)
        assert m.n_measurements == 2

    def test_running_mean_converges(self, ident):
        # Stream of identical samples → mean stays at the sample value
        for _ in range(20):
            ident.update_delay_model('WWV', 10.0, 7.0, timestamp=0.0)
        m = ident.station_delay_models['WWV_10.0']
        assert m.mean_delay_ms == pytest.approx(7.0)
        assert m.n_measurements == 20

    def test_welford_std_after_three_samples(self, ident):
        # Standard-deviation update path runs only when n > 1
        # Feed 1, 2, 3 → mean=2, σ_pop=2/3, σ_sample=1
        for v in (1.0, 2.0, 3.0):
            ident.update_delay_model('WWV', 10.0, v, timestamp=0.0)
        m = ident.station_delay_models['WWV_10.0']
        assert m.mean_delay_ms == pytest.approx(2.0)
        # Welford produces a finite, positive std
        assert math.isfinite(m.std_delay_ms)
        assert m.std_delay_ms > 0


class TestGetDelayModel:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.BOOTSTRAP)

    def test_returns_none_when_absent(self, ident):
        assert ident.get_delay_model('WWV', 10.0) is None

    def test_returns_model_after_update(self, ident):
        ident.update_delay_model('WWV', 10.0, 5.0, timestamp=0.0)
        m = ident.get_delay_model('WWV', 10.0)
        assert m is not None
        assert m.mean_delay_ms == 5.0


class TestGetAllDelayModels:
    @pytest.fixture
    def ident(self):
        return make_identifier(OperationalPhase.BOOTSTRAP)

    def test_returns_a_copy(self, ident):
        ident.update_delay_model('WWV', 10.0, 5.0, timestamp=0.0)
        snapshot = ident.get_all_delay_models()
        # Mutating the returned dict does not affect internal state
        snapshot.clear()
        assert ident.get_delay_model('WWV', 10.0) is not None
