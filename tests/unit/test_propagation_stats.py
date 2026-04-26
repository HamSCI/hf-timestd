"""
Unit tests for hf_timestd.core.propagation_stats

PropagationStatsCalculator aggregates per-minute propagation-mode
observations into hourly and daily statistics, including MUF estimates
and quality flags.
"""

from datetime import datetime, timezone

import pytest

from hf_timestd.core.propagation_stats import PropagationStatsCalculator


# =============================================================================
# Helpers
# =============================================================================


def m(station='WWV', freq=10.0, mode='1F', snr=15.0):
    """Build a minimal measurement dict."""
    return {
        'station': station,
        'frequency_mhz': freq,
        'propagation_mode': mode,
        'snr_db': snr,
    }


@pytest.fixture
def calc():
    return PropagationStatsCalculator(processing_version="3.3.0")


@pytest.fixture
def hour():
    start = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)
    return start, end


# =============================================================================
# Module-level invariants
# =============================================================================


class TestModuleConstants:
    def test_valid_modes_includes_all_canonical(self):
        # Sanity-check the canonical set
        for mode in ('1E', '1F', '2F', '3F', 'GW', 'UNKNOWN'):
            assert mode in PropagationStatsCalculator.VALID_MODES

    def test_quality_thresholds_have_three_levels(self):
        thresholds = PropagationStatsCalculator.QUALITY_THRESHOLDS
        assert set(thresholds) == {'GOOD', 'MARGINAL', 'BAD'}
        # GOOD requires more observations than MARGINAL
        assert thresholds['GOOD']['min_obs'] > thresholds['MARGINAL']['min_obs']
        assert thresholds['GOOD']['min_completeness'] > thresholds['MARGINAL']['min_completeness']


# =============================================================================
# _group_measurements
# =============================================================================


class TestGroupMeasurements:
    def test_groups_by_station_and_frequency(self, calc):
        ms = [
            m(station='WWV', freq=10.0),
            m(station='WWV', freq=10.0),
            m(station='WWV', freq=15.0),
            m(station='CHU', freq=7.85),
        ]
        groups = calc._group_measurements(ms)
        assert len(groups[('WWV', 10.0)]) == 2
        assert len(groups[('WWV', 15.0)]) == 1
        assert len(groups[('CHU', 7.85)]) == 1

    def test_unknown_station_default(self, calc):
        ms = [{'frequency_mhz': 10.0, 'propagation_mode': '1F'}]
        groups = calc._group_measurements(ms)
        assert ('UNKNOWN', 10.0) in groups


# =============================================================================
# _calculate_mode_probabilities
# =============================================================================


class TestCalculateModeProbabilities:
    def test_probabilities_sum_to_one(self, calc):
        from collections import Counter
        counts = Counter({'1F': 30, '2F': 20, '1E': 10})
        probs = calc._calculate_mode_probabilities(counts, total_observations=60)
        assert sum(probs.values()) == pytest.approx(1.0)

    def test_zero_count_yields_zero_probability(self, calc):
        from collections import Counter
        counts = Counter()
        probs = calc._calculate_mode_probabilities(counts, total_observations=60)
        assert all(p == 0.0 for p in probs.values())

    def test_zero_total_observations_yields_zero(self, calc):
        from collections import Counter
        # Defensive — total_observations=0 must not divide-by-zero
        counts = Counter({'1F': 0})
        probs = calc._calculate_mode_probabilities(counts, total_observations=0)
        assert all(p == 0.0 for p in probs.values())


# =============================================================================
# _estimate_muf
# =============================================================================


class TestEstimateMUF:
    def test_low_f_layer_probability_returns_none(self, calc):
        probs = {'1E': 0.8, '1F': 0.1, '2F': 0.05, '3F': 0.05, 'GW': 0.0,
                 'UNKNOWN': 0.0}
        muf, conf = calc._estimate_muf(10.0, probs, n_observations=60)
        assert muf is None
        assert conf is None

    def test_strong_f_layer_estimates_higher_muf(self, calc):
        probs = {'1F': 0.6, '2F': 0.3, '3F': 0.0, '1E': 0.05, 'GW': 0.0,
                 'UNKNOWN': 0.05}
        # f_layer_prob = 0.9 → MUF ≈ 1.2 × freq
        muf, conf = calc._estimate_muf(10.0, probs, n_observations=60)
        assert muf == pytest.approx(12.0)
        assert 0.0 < conf <= 1.0

    def test_moderate_f_layer_estimates_near_freq(self, calc):
        probs = {'1F': 0.5, '2F': 0.05, '3F': 0.05, '1E': 0.2, 'GW': 0.0,
                 'UNKNOWN': 0.2}
        muf, conf = calc._estimate_muf(10.0, probs, n_observations=60)
        assert muf == pytest.approx(11.0)

    def test_weak_f_layer_estimates_at_freq(self, calc):
        # f_layer_prob between 0.3 and 0.5 → MUF ≈ freq
        probs = {'1F': 0.3, '2F': 0.05, '3F': 0.0, '1E': 0.4, 'GW': 0.0,
                 'UNKNOWN': 0.25}
        muf, conf = calc._estimate_muf(10.0, probs, n_observations=60)
        assert muf == pytest.approx(10.0)

    def test_confidence_increases_with_observations(self, calc):
        probs = {'1F': 0.7, '2F': 0.2, '3F': 0.0, '1E': 0.1, 'GW': 0.0,
                 'UNKNOWN': 0.0}
        _, low = calc._estimate_muf(10.0, probs, n_observations=10)
        _, high = calc._estimate_muf(10.0, probs, n_observations=120)
        assert high > low


# =============================================================================
# _determine_quality_flag
# =============================================================================


class TestQualityFlag:
    def test_good_when_above_thresholds(self, calc):
        assert calc._determine_quality_flag(60, 1.0) == 'GOOD'
        # Boundary: 40 obs and 0.8 completeness
        assert calc._determine_quality_flag(40, 0.8) == 'GOOD'

    def test_marginal_when_between_thresholds(self, calc):
        assert calc._determine_quality_flag(30, 0.6) == 'MARGINAL'
        # Boundary: 20 obs / 0.5 completeness
        assert calc._determine_quality_flag(20, 0.5) == 'MARGINAL'

    def test_bad_when_below_marginal(self, calc):
        assert calc._determine_quality_flag(0, 0.0) == 'BAD'
        assert calc._determine_quality_flag(10, 0.2) == 'BAD'


# =============================================================================
# _calculate_stats
# =============================================================================


class TestCalculateStats:
    def test_empty_measurements_returns_none(self, calc, hour):
        s = calc._calculate_stats(
            measurements=[], station='WWV', frequency_mhz=10.0,
            period_start=hour[0], period_end=hour[1],
            aggregation_period='HOURLY', expected_observations=60,
        )
        assert s is None

    def test_normalizes_unknown_modes(self, calc, hour):
        ms = [m(mode='1F') for _ in range(10)]
        ms.append(m(mode='unknown_garbage'))
        s = calc._calculate_stats(ms, 'WWV', 10.0, hour[0], hour[1],
                                   'HOURLY', 60)
        assert s is not None
        # Garbage mode is rolled into the UNKNOWN bucket
        total = (s['mode_1e_probability'] + s['mode_1f_probability']
                 + s['mode_2f_probability'] + s['mode_3f_probability']
                 + s['mode_gw_probability'] + s['mode_unknown_probability'])
        assert total == pytest.approx(1.0)

    def test_data_completeness_capped_at_one(self, calc, hour):
        # Many more observations than expected → completeness clipped to 1.0
        ms = [m() for _ in range(100)]
        s = calc._calculate_stats(ms, 'WWV', 10.0, hour[0], hour[1],
                                   'HOURLY', 60)
        assert s['data_completeness'] == 1.0

    def test_mean_snr_excludes_sentinel_values(self, calc, hour):
        ms = [m(snr=20.0), m(snr=10.0), m(snr=-9999)]
        s = calc._calculate_stats(ms, 'WWV', 10.0, hour[0], hour[1],
                                   'HOURLY', 60)
        # Sentinel -9999 dropped; mean of {20, 10}
        assert s['mean_snr_db'] == pytest.approx(15.0)

    def test_no_snr_yields_none_mean(self, calc, hour):
        ms = [{'station': 'WWV', 'frequency_mhz': 10.0,
               'propagation_mode': '1F'}]
        s = calc._calculate_stats(ms, 'WWV', 10.0, hour[0], hour[1],
                                   'HOURLY', 60)
        assert s['mean_snr_db'] is None

    def test_stats_dict_shape(self, calc, hour):
        ms = [m() for _ in range(60)]
        s = calc._calculate_stats(ms, 'WWV', 10.0, hour[0], hour[1],
                                   'HOURLY', 60)
        for key in (
            'timestamp_utc', 'period_start', 'aggregation_period',
            'station', 'frequency_mhz',
            'mode_1e_probability', 'mode_1f_probability',
            'mode_2f_probability', 'mode_3f_probability',
            'mode_gw_probability', 'mode_unknown_probability',
            'estimated_muf_mhz', 'muf_confidence', 'mean_snr_db',
            'n_observations', 'data_completeness', 'quality_flag',
            'processing_version',
        ):
            assert key in s
        assert s['aggregation_period'] == 'HOURLY'
        assert s['processing_version'] == calc.processing_version


# =============================================================================
# Top-level entry points
# =============================================================================


class TestCalculateHourlyStats:
    def test_groups_by_station_and_frequency(self, calc, hour):
        ms = [m(station='WWV', freq=10.0) for _ in range(40)]
        ms += [m(station='WWV', freq=15.0) for _ in range(35)]
        ms += [m(station='CHU', freq=7.85) for _ in range(45)]
        out = calc.calculate_hourly_stats(ms, hour[0], hour[1])
        assert len(out) == 3
        keys = sorted((s['station'], s['frequency_mhz']) for s in out)
        assert keys == [('CHU', 7.85), ('WWV', 10.0), ('WWV', 15.0)]
        # Aggregation period propagates
        assert all(s['aggregation_period'] == 'HOURLY' for s in out)


class TestCalculateDailyStats:
    def test_aggregation_period_is_daily(self, calc):
        start = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc)
        ms = [m() for _ in range(100)]
        out = calc.calculate_daily_stats(ms, start, end)
        assert len(out) == 1
        assert out[0]['aggregation_period'] == 'DAILY'

    def test_daily_completeness_uses_1440_expected(self, calc):
        start = datetime(2026, 4, 26, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc)
        ms = [m() for _ in range(720)]  # half a day at 1/min
        out = calc.calculate_daily_stats(ms, start, end)
        assert out[0]['data_completeness'] == pytest.approx(0.5)
