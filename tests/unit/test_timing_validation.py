"""
Unit tests for hf_timestd.core.timing_validation

The TimingValidator compares fusion-derived clock offsets against radiod's
GPS_TIME/RTP_TIMESNAP ground-truth pairs. Tests cover:
- TimingComparison dataclass and to_dict serialization
- Trigger logic: comparison fires only when both fusion + radiod inputs are
  recorded, and re-fires on each subsequent record
- Discrepancy math: fusion - radiod, GPS-to-Unix conversion (incl. leap seconds)
- Threshold-based alerting and RTP-authority error escalation
- Statistics: empty/initial/post-comparison shapes; mean/std/max
- Recent-comparison history bounded to the requested count
- Latest-discrepancy accessor
"""

from unittest.mock import patch

import pytest

from hf_timestd.core.timing_validation import (
    BILLION,
    GPS_EPOCH_UNIX,
    GPS_LEAP_SECONDS,
    TimingComparison,
    TimingValidator,
)
from hf_timestd.interfaces.data_models import TimingAuthority, TimingConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def fusion_config():
    return TimingConfig(authority=TimingAuthority.FUSION,
                        validation_threshold_ms=5.0)


@pytest.fixture
def rtp_config():
    return TimingConfig(authority=TimingAuthority.RTP,
                        validation_threshold_ms=5.0)


@pytest.fixture
def validator(fusion_config):
    return TimingValidator(fusion_config)


def gps_time_ns_for(unix_sec: float) -> int:
    """Build a GPS_TIME (ns since GPS epoch) corresponding to the given Unix
    second, accounting for the GPS↔Unix epoch offset and accumulated leap
    seconds."""
    return int(BILLION * (unix_sec - GPS_EPOCH_UNIX + GPS_LEAP_SECONDS))


# =============================================================================
# TimingComparison
# =============================================================================


class TestTimingComparison:
    def test_to_dict_round_trips_all_fields(self):
        c = TimingComparison(
            timestamp=1000.0,
            fusion_offset_ms=2.5,
            radiod_offset_ms=2.0,
            discrepancy_ms=0.5,
            fusion_uncertainty_ms=0.2,
        )
        d = c.to_dict()
        assert d == {
            'timestamp': 1000.0,
            'fusion_offset_ms': 2.5,
            'radiod_offset_ms': 2.0,
            'discrepancy_ms': 0.5,
            'fusion_uncertainty_ms': 0.2,
        }


# =============================================================================
# Initialization
# =============================================================================


class TestInitialization:
    def test_starts_with_no_data(self, validator):
        assert validator.get_discrepancy_ms() is None
        assert validator.comparisons_made == 0
        assert validator.alerts_raised == 0

    def test_history_bounded(self, validator):
        # The deque is sized to ~10 minutes at 2 Hz
        assert validator._comparison_history.maxlen == 1200

    def test_stores_authority_and_threshold(self, fusion_config):
        v = TimingValidator(fusion_config)
        assert v.timing_config.authority == TimingAuthority.FUSION
        assert v.timing_config.validation_threshold_ms == 5.0


# =============================================================================
# Trigger logic
# =============================================================================


class TestTriggerLogic:
    def test_fusion_alone_does_not_compare(self, validator):
        validator.record_fusion_offset(1.0)
        assert validator.comparisons_made == 0
        assert validator.get_discrepancy_ms() is None

    def test_radiod_alone_does_not_compare(self, validator):
        validator.record_radiod_snapshot(gps_time_ns=gps_time_ns_for(1000.0),
                                         rtp_timesnap=42)
        assert validator.comparisons_made == 0

    def test_comparison_fires_after_both_recorded(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=42)
            validator.record_fusion_offset(2.5)

        assert validator.comparisons_made == 1
        assert validator.get_discrepancy_ms() is not None

    def test_each_subsequent_record_fires_a_new_comparison(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=42)
            validator.record_fusion_offset(2.5)
            validator.record_fusion_offset(3.0)
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=43)
        # 1 (after first fusion), 1 (next fusion), 1 (next radiod) = 3
        assert validator.comparisons_made == 3


# =============================================================================
# Discrepancy math
# =============================================================================


class TestDiscrepancyMath:
    def test_aligned_clocks_yield_radiod_offset_zero(self, validator):
        # If receipt time == GPS time → radiod_offset_ms = 0
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1_700_000_000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1_700_000_000.0), rtp_timesnap=0)
            validator.record_fusion_offset(0.0)
        c = validator._comparison_history[-1]
        assert c.radiod_offset_ms == pytest.approx(0.0, abs=1e-6)
        assert c.discrepancy_ms == pytest.approx(0.0, abs=1e-6)

    def test_local_clock_ahead_of_gps_yields_positive_radiod_offset(self, validator):
        # local clock 100 ms ahead of GPS time
        gps_unix = 1_700_000_000.0
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=gps_unix + 0.100):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(gps_unix), rtp_timesnap=0)
            validator.record_fusion_offset(0.0)
        c = validator._comparison_history[-1]
        assert c.radiod_offset_ms == pytest.approx(100.0, abs=1e-3)

    def test_discrepancy_is_fusion_minus_radiod(self, validator):
        # Fusion claims +5 ms; GPS shows local clock is +2 ms ahead
        gps_unix = 1_700_000_000.0
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=gps_unix + 0.002):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(gps_unix), rtp_timesnap=0)
            validator.record_fusion_offset(5.0)
        c = validator._comparison_history[-1]
        assert c.radiod_offset_ms == pytest.approx(2.0, abs=1e-3)
        assert c.discrepancy_ms == pytest.approx(3.0, abs=1e-3)

    def test_uncertainty_propagated(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(1.0, uncertainty_ms=0.42)
        c = validator._comparison_history[-1]
        assert c.fusion_uncertainty_ms == pytest.approx(0.42)


# =============================================================================
# Threshold and alerting
# =============================================================================


class TestThresholdAlerting:
    def test_within_threshold_does_not_alert(self, validator, caplog):
        # discrepancy ≈ 1 ms, threshold = 5 ms
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(1.0)
        assert validator.alerts_raised == 0
        assert not any('exceeds threshold' in r.message for r in caplog.records)

    def test_above_threshold_logs_warning(self, validator, caplog):
        # discrepancy ≈ 10 ms, threshold = 5 ms
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(10.0)
        assert validator.alerts_raised == 1
        assert any('exceeds threshold' in r.message for r in caplog.records)

    def test_negative_discrepancy_triggers_alert(self, validator):
        # Fusion claims -10 ms, threshold = 5 ms → |discrepancy| > threshold
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(-10.0)
        assert validator.alerts_raised == 1

    def test_rtp_authority_escalates_to_error(self, rtp_config, caplog):
        # In RTP authority mode, a large discrepancy is a config error
        v = TimingValidator(rtp_config)
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            v.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            v.record_fusion_offset(50.0)
        assert v.alerts_raised == 1
        # The ALERT-level error log fires only in RTP mode
        assert any('ALERT' in r.message for r in caplog.records
                   if r.levelname == 'ERROR')


# =============================================================================
# Statistics
# =============================================================================


class TestStatistics:
    def test_empty_stats_have_none_values(self, validator):
        s = validator.get_statistics()
        assert s['comparisons_made'] == 0
        assert s['alerts_raised'] == 0
        assert s['mean_discrepancy_ms'] is None
        assert s['std_discrepancy_ms'] is None
        assert s['max_discrepancy_ms'] is None

    def test_single_comparison_stats(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(2.0)
        s = validator.get_statistics()
        assert s['comparisons_made'] == 1
        assert s['mean_discrepancy_ms'] == pytest.approx(2.0, abs=1e-3)
        # Single sample → std defaults to 0.0 (not None)
        assert s['std_discrepancy_ms'] == 0.0
        assert s['max_discrepancy_ms'] == pytest.approx(2.0, abs=1e-3)
        assert s['latest_discrepancy_ms'] == pytest.approx(2.0, abs=1e-3)
        assert s['history_length'] == 1

    def test_multi_comparison_stats(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            for offset in [1.0, 2.0, 3.0, 4.0, 5.0]:
                validator.record_fusion_offset(offset)
        s = validator.get_statistics()
        assert s['history_length'] == 5
        assert s['mean_discrepancy_ms'] == pytest.approx(3.0, abs=1e-3)
        # statistics.stdev of [1,2,3,4,5] = sqrt(2.5) ≈ 1.581
        assert s['std_discrepancy_ms'] == pytest.approx(1.5811, abs=1e-3)
        assert s['max_discrepancy_ms'] == pytest.approx(5.0, abs=1e-3)
        assert s['latest_discrepancy_ms'] == pytest.approx(5.0, abs=1e-3)

    def test_max_uses_absolute_value(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            for offset in [-7.0, 1.0, 2.0]:
                validator.record_fusion_offset(offset)
        s = validator.get_statistics()
        assert s['max_discrepancy_ms'] == pytest.approx(7.0, abs=1e-3)


# =============================================================================
# Recent comparisons
# =============================================================================


class TestRecentComparisons:
    def test_empty_returns_empty_list(self, validator):
        assert validator.get_recent_comparisons() == []

    def test_returns_list_of_dicts(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(1.0)
        recent = validator.get_recent_comparisons(count=10)
        assert len(recent) == 1
        # to_dict shape
        assert set(recent[0]) >= {
            'timestamp', 'fusion_offset_ms', 'radiod_offset_ms',
            'discrepancy_ms', 'fusion_uncertainty_ms',
        }

    def test_count_caps_returned_history(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            for offset in range(20):
                validator.record_fusion_offset(float(offset))
        recent = validator.get_recent_comparisons(count=5)
        assert len(recent) == 5
        # Most-recent block (offsets 15..19)
        assert recent[-1]['fusion_offset_ms'] == 19.0
        assert recent[0]['fusion_offset_ms'] == 15.0

    def test_count_larger_than_history_returns_all(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(1.0)
            validator.record_fusion_offset(2.0)
        recent = validator.get_recent_comparisons(count=100)
        assert len(recent) == 2


# =============================================================================
# Latest discrepancy accessor
# =============================================================================


class TestGetDiscrepancyMs:
    def test_none_before_comparison(self, validator):
        assert validator.get_discrepancy_ms() is None

    def test_returns_latest_after_multiple_comparisons(self, validator):
        with patch('hf_timestd.core.timing_validation.time.time',
                   return_value=1000.0):
            validator.record_radiod_snapshot(
                gps_time_ns=gps_time_ns_for(1000.0), rtp_timesnap=0)
            validator.record_fusion_offset(1.0)
            validator.record_fusion_offset(2.0)
            validator.record_fusion_offset(3.0)
        # Last comparison has fusion=3.0, radiod≈0 → discrepancy ≈ 3.0
        assert validator.get_discrepancy_ms() == pytest.approx(3.0, abs=1e-3)
