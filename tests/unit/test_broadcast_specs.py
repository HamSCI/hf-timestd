"""
Unit tests for hf_timestd.core.broadcast_specs

Authoritative registry of all 17 HF time-standard broadcasts. Tests cover:
- Module-level invariants (frequency partitions, expected counts per station)
- ToneSchedule helpers (get_expected_duration_ms, ticks_per_minute,
  is_special_second, CHU hour-marker special case)
- BroadcastSpec properties (broadcast_id, frequency_mhz, is_unique/shared,
  has_feature, get_expected_duration_ms, to_dict)
- Lookup functions (get_broadcast_spec, get_broadcasts_for_frequency,
  get_broadcasts_for_station, get_channel_broadcasts)
- Frequency utilities (khz_to_mhz, mhz_to_khz, normalize_frequency_khz)
"""

import pytest

from hf_timestd.core.broadcast_specs import (
    ALL_FREQUENCIES_KHZ,
    BPM_FEATURES,
    BPM_FREQUENCIES_KHZ,
    BPM_TONE_SCHEDULE,
    BROADCAST_SPECS,
    CHU_FEATURES,
    CHU_FREQUENCIES_KHZ,
    CHU_TONE_SCHEDULE,
    PROPAGATION_BOUNDS_MS,
    SHARED_FREQUENCIES_KHZ,
    STATION_COORDINATES,
    STATION_TONE_SCHEDULES,
    UNIQUE_FREQUENCIES_KHZ,
    WWVH_FEATURES,
    WWVH_FREQUENCIES_KHZ,
    WWVH_TONE_SCHEDULE,
    WWV_FEATURES,
    WWV_FREQUENCIES_KHZ,
    WWV_TONE_SCHEDULE,
    BroadcastSpec,
    FeatureType,
    Station,
    ToneSchedule,
    get_broadcast_spec,
    get_broadcast_spec_by_id,
    get_broadcasts_for_frequency,
    get_broadcasts_for_station,
    get_channel_broadcasts,
    khz_to_mhz,
    list_all_broadcast_ids,
    list_broadcasts_by_station,
    mhz_to_khz,
    normalize_frequency_khz,
)


# =============================================================================
# Module-level invariants
# =============================================================================


class TestModuleInvariants:
    def test_total_broadcast_count_is_17(self):
        # 6 WWV + 4 WWVH + 3 CHU + 4 BPM = 17
        assert len(BROADCAST_SPECS) == 17

    def test_per_station_counts(self):
        counts = {s.value: 0 for s in Station}
        for spec in BROADCAST_SPECS.values():
            counts[spec.station.value] += 1
        assert counts['WWV'] == 6
        assert counts['WWVH'] == 4
        assert counts['CHU'] == 3
        assert counts['BPM'] == 4

    def test_shared_and_unique_partition(self):
        # No overlap between shared and unique frequencies
        assert set(SHARED_FREQUENCIES_KHZ).isdisjoint(set(UNIQUE_FREQUENCIES_KHZ))
        # Their union covers every advertised frequency
        assert (set(SHARED_FREQUENCIES_KHZ) | set(UNIQUE_FREQUENCIES_KHZ)
                == set(ALL_FREQUENCIES_KHZ))

    def test_all_frequencies_are_sorted_unique(self):
        assert ALL_FREQUENCIES_KHZ == sorted(set(ALL_FREQUENCIES_KHZ))

    def test_propagation_bounds_for_every_station(self):
        for s in Station:
            assert s in PROPAGATION_BOUNDS_MS
            lo, hi = PROPAGATION_BOUNDS_MS[s]
            assert lo < hi

    def test_station_tone_schedules_complete(self):
        for s in Station:
            assert s in STATION_TONE_SCHEDULES

    def test_station_coordinates_complete(self):
        for s in Station:
            assert s in STATION_COORDINATES

    def test_features_have_no_invalid_entries(self):
        for features in (WWV_FEATURES, WWVH_FEATURES, CHU_FEATURES, BPM_FEATURES):
            for f in features:
                assert isinstance(f, FeatureType)


# =============================================================================
# ToneSchedule
# =============================================================================


class TestToneSchedule:
    def test_wwv_default_durations(self):
        # Marker at 0 → 800ms, regular tick → 5ms, skip seconds → None
        assert WWV_TONE_SCHEDULE.get_expected_duration_ms(0) == 800.0
        assert WWV_TONE_SCHEDULE.get_expected_duration_ms(15) == 5.0
        assert WWV_TONE_SCHEDULE.get_expected_duration_ms(29) is None
        assert WWV_TONE_SCHEDULE.get_expected_duration_ms(59) is None

    def test_chu_second_zero_is_always_silent(self):
        # CHU's second 0 is in skip_seconds. The skip check fires before the
        # hour-marker special case, so second 0 always returns None — even
        # at the top of the hour. (The hour marker is actually transmitted
        # at second 59.5 of the previous minute; the docstring captures this.)
        assert CHU_TONE_SCHEDULE.get_expected_duration_ms(0, minute=0) is None
        assert CHU_TONE_SCHEDULE.get_expected_duration_ms(0, minute=30) is None

    def test_chu_skip_seconds(self):
        # CHU has second 0 and second 29 silent
        assert CHU_TONE_SCHEDULE.get_expected_duration_ms(0) is None
        assert CHU_TONE_SCHEDULE.get_expected_duration_ms(29) is None

    def test_chu_fsk_seconds_return_10ms(self):
        for sec in range(31, 40):
            assert CHU_TONE_SCHEDULE.get_expected_duration_ms(sec) == 10.0

    def test_chu_voice_seconds_return_10ms(self):
        for sec in range(50, 60):
            assert CHU_TONE_SCHEDULE.get_expected_duration_ms(sec) == 10.0

    def test_bpm_no_skip_seconds(self):
        assert BPM_TONE_SCHEDULE.skip_seconds == frozenset()
        # Second 15 is a regular tick (10 ms); 25-29 are UT1 ticks (100 ms)
        assert BPM_TONE_SCHEDULE.get_expected_duration_ms(15) == 10.0
        assert BPM_TONE_SCHEDULE.get_expected_duration_ms(29) == 100.0

    def test_bpm_ut1_seconds_return_100ms(self):
        for sec in (25, 26, 27, 28, 29, 55, 56, 57, 58, 59):
            assert BPM_TONE_SCHEDULE.get_expected_duration_ms(sec) == 100.0

    def test_get_ticks_per_minute(self):
        # WWV/WWVH: skip 2 seconds → 58 ticks
        assert WWV_TONE_SCHEDULE.get_ticks_per_minute() == 58
        # CHU: skip 2 seconds (0, 29) → 58 ticks
        assert CHU_TONE_SCHEDULE.get_ticks_per_minute() == 58
        # BPM: skip none → 60 ticks
        assert BPM_TONE_SCHEDULE.get_ticks_per_minute() == 60

    def test_is_special_second(self):
        # CHU FSK and voice seconds are "special"
        assert CHU_TONE_SCHEDULE.is_special_second(35) is True
        assert CHU_TONE_SCHEDULE.is_special_second(55) is True
        assert CHU_TONE_SCHEDULE.is_special_second(15) is False
        # BPM UT1 ticks are "special"
        assert BPM_TONE_SCHEDULE.is_special_second(27) is True
        # WWV/WWVH have no special seconds
        assert WWV_TONE_SCHEDULE.is_special_second(15) is False


# =============================================================================
# BroadcastSpec properties
# =============================================================================


class TestBroadcastSpecProperties:
    @pytest.fixture
    def wwv_10mhz(self):
        return BROADCAST_SPECS['WWV_10000']

    @pytest.fixture
    def chu_7p85mhz(self):
        return BROADCAST_SPECS['CHU_7850']

    def test_broadcast_id(self, wwv_10mhz):
        assert wwv_10mhz.broadcast_id == 'WWV_10000'

    def test_frequency_mhz_conversion(self, wwv_10mhz, chu_7p85mhz):
        assert wwv_10mhz.frequency_mhz == 10.0
        assert chu_7p85mhz.frequency_mhz == 7.85

    def test_is_shared_frequency(self, wwv_10mhz):
        assert wwv_10mhz.is_shared_frequency is True
        assert wwv_10mhz.is_unique_frequency is False

    def test_is_unique_frequency_for_chu(self, chu_7p85mhz):
        assert chu_7p85mhz.is_unique_frequency is True
        assert chu_7p85mhz.is_shared_frequency is False

    def test_tone_freq_hz_proxied(self, wwv_10mhz, chu_7p85mhz):
        assert wwv_10mhz.tone_freq_hz == 1000
        assert chu_7p85mhz.tone_freq_hz == 1000  # CHU uses 1000 Hz

    def test_minute_marker_duration_ms_proxied(self, wwv_10mhz, chu_7p85mhz):
        assert wwv_10mhz.minute_marker_duration_ms == 800.0
        assert chu_7p85mhz.minute_marker_duration_ms == 500.0

    def test_ticks_per_minute_proxied(self, wwv_10mhz):
        assert wwv_10mhz.ticks_per_minute == 58

    def test_has_feature(self, wwv_10mhz):
        assert wwv_10mhz.has_feature(FeatureType.BCD_TIMECODE) is True
        assert wwv_10mhz.has_feature(FeatureType.FSK_TIMECODE) is False

    def test_get_expected_duration_ms(self, wwv_10mhz):
        assert wwv_10mhz.get_expected_duration_ms(0) == 800.0
        assert wwv_10mhz.get_expected_duration_ms(29) is None

    def test_to_dict_contains_expected_keys(self, wwv_10mhz):
        d = wwv_10mhz.to_dict()
        for key in ('broadcast_id', 'station', 'frequency_khz', 'frequency_mhz',
                    'tone_freq_hz', 'minute_marker_duration_ms',
                    'ticks_per_minute', 'features', 'lat', 'lon',
                    'propagation_bounds_ms', 'is_unique_frequency',
                    'test_signal_minute'):
            assert key in d
        # Features are serialised as list of strings
        assert isinstance(d['features'], list)
        assert all(isinstance(f, str) for f in d['features'])


# =============================================================================
# Lookups
# =============================================================================


class TestLookups:
    def test_get_broadcast_spec_known(self):
        spec = get_broadcast_spec('WWV', 10000)
        assert spec is not None
        assert spec.broadcast_id == 'WWV_10000'

    def test_get_broadcast_spec_unknown(self):
        assert get_broadcast_spec('XYZ', 9999) is None

    def test_get_broadcast_spec_by_id(self):
        spec = get_broadcast_spec_by_id('CHU_3330')
        assert spec is not None
        assert spec.station == Station.CHU

    def test_get_broadcast_spec_by_id_unknown(self):
        assert get_broadcast_spec_by_id('FOO_1234') is None

    def test_get_broadcasts_for_frequency_shared(self):
        # 10 MHz is shared by WWV, WWVH, BPM
        broadcasts = get_broadcasts_for_frequency(10000)
        stations = {b.station.value for b in broadcasts}
        assert {'WWV', 'WWVH', 'BPM'} <= stations

    def test_get_broadcasts_for_frequency_unique(self):
        # 20 MHz is WWV-only
        broadcasts = get_broadcasts_for_frequency(20000)
        assert len(broadcasts) == 1
        assert broadcasts[0].station == Station.WWV

    def test_get_broadcasts_for_station(self):
        wwv = get_broadcasts_for_station('WWV')
        assert len(wwv) == 6
        chu = get_broadcasts_for_station('CHU')
        assert len(chu) == 3

    def test_get_channel_broadcasts_shared(self):
        broadcasts = get_channel_broadcasts('SHARED_10000')
        stations = {b.station.value for b in broadcasts}
        assert stations == {'WWV', 'WWVH', 'BPM'}

    def test_get_channel_broadcasts_station_specific(self):
        # WWV_10000 channel filters to WWV only
        broadcasts = get_channel_broadcasts('WWV_10000')
        assert len(broadcasts) == 1
        assert broadcasts[0].station == Station.WWV

    def test_get_channel_broadcasts_chu_unique(self):
        broadcasts = get_channel_broadcasts('CHU_7850')
        assert len(broadcasts) == 1
        assert broadcasts[0].station == Station.CHU

    def test_get_channel_broadcasts_with_spaces(self):
        # Spaces in channel name are normalized to underscores
        broadcasts = get_channel_broadcasts('SHARED 10000')
        stations = {b.station.value for b in broadcasts}
        assert 'WWV' in stations

    def test_get_channel_broadcasts_invalid_returns_empty(self):
        # No frequency suffix → empty result
        assert get_channel_broadcasts('garbage') == []
        # Non-numeric suffix → empty result
        assert get_channel_broadcasts('WWV_HIGH') == []

    def test_list_all_broadcast_ids_sorted(self):
        ids = list_all_broadcast_ids()
        assert len(ids) == 17
        assert ids == sorted(ids)

    def test_list_broadcasts_by_station(self):
        layout = list_broadcasts_by_station()
        assert set(layout) == {'WWV', 'WWVH', 'CHU', 'BPM'}
        assert len(layout['WWV']) == 6
        assert len(layout['CHU']) == 3


# =============================================================================
# Frequency conversion utilities
# =============================================================================


class TestFrequencyUtils:
    def test_khz_to_mhz(self):
        assert khz_to_mhz(7850) == 7.85
        assert khz_to_mhz(10000) == 10.0

    def test_mhz_to_khz(self):
        assert mhz_to_khz(7.85) == 7850
        assert mhz_to_khz(10.0) == 10000

    def test_mhz_to_khz_rounding(self):
        # Near-integer MHz values round to the nearest kHz
        assert mhz_to_khz(7.8501) == 7850

    def test_normalize_frequency_assumes_mhz_below_100(self):
        assert normalize_frequency_khz(7.85) == 7850
        assert normalize_frequency_khz(10) == 10000

    def test_normalize_frequency_assumes_khz_above_100(self):
        assert normalize_frequency_khz(7850) == 7850
        assert normalize_frequency_khz(10000) == 10000
