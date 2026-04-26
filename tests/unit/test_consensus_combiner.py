"""
Unit tests for hf_timestd.core.consensus_combiner

Combines per-channel D_clock measurements into a single UTC(NIST) estimate.
Tests cover:
- ChannelMeasurement / StationEstimate / ConsensusResult dataclasses
- Quality-grade and SNR weighting
- MAD-based outlier rejection (incl. tied-data short-circuit, < 3 measurements)
- Per-station weighted mean and uncertainty
- _group_by_station behavior (case, unknown stations)
- Status-file reading (success, missing, malformed)
- compute_consensus convergence-state ladder (NO_DATA → SINGLE_SOURCE →
  LOCKED → CONVERGING → DIVERGENT)
- run_and_save atomic JSON output
- create_combiner_from_config wiring
"""

import json
from pathlib import Path

import numpy as np
import pytest

from hf_timestd.core.consensus_combiner import (
    GRADE_WEIGHTS,
    MIN_SNR_DB,
    OUTLIER_THRESHOLD_MAD,
    ChannelMeasurement,
    ConsensusCombiner,
    ConsensusResult,
    StationEstimate,
    create_combiner_from_config,
)


# =============================================================================
# Helpers
# =============================================================================


def make_measurement(
    channel_name: str = "WWV_10000",
    station: str = "WWV",
    d_clock_ms: float = 1.0,
    quality_grade: str = "A",
    snr_db: float = 20.0,
    propagation_delay_ms: float = 5.0,
    n_hops: int = 1,
    confidence: float = 0.9,
    timestamp: float = 0.0,
) -> ChannelMeasurement:
    return ChannelMeasurement(
        channel_name=channel_name,
        station=station,
        d_clock_ms=d_clock_ms,
        quality_grade=quality_grade,
        snr_db=snr_db,
        propagation_delay_ms=propagation_delay_ms,
        n_hops=n_hops,
        confidence=confidence,
        timestamp=timestamp,
    )


def write_status_file(phase2_dir: Path, channel: str, payload: dict):
    """Write a Phase-2 status JSON for a given channel name."""
    status_dir = phase2_dir / channel.replace(' ', '_') / 'status'
    status_dir.mkdir(parents=True, exist_ok=True)
    status_file = status_dir / 'analytics-service-status.json'
    status_file.write_text(json.dumps(payload))
    return status_file


def status_payload(channel: str, **fields) -> dict:
    """Build a default status payload that the reader will accept."""
    base = {
        'station': 'WWV',
        'd_clock_ms': 1.0,
        'quality_grade': 'A',
        'quality_metrics': {'last_snr_db': 20.0},
        'propagation_delay_ms': 5.0,
        'n_hops': 1,
        'time_snap': {'confidence': 0.9},
    }
    base.update(fields)
    return {'channels': {channel: base}}


@pytest.fixture
def phase2(tmp_path):
    p = tmp_path / 'phase2'
    p.mkdir()
    return p


@pytest.fixture
def output_file(tmp_path):
    return tmp_path / 'shared' / 'consensus_timing.json'


# =============================================================================
# Module constants
# =============================================================================


class TestModuleConstants:
    def test_grade_weights_monotonic(self):
        # Higher grade → higher weight
        assert GRADE_WEIGHTS['A'] > GRADE_WEIGHTS['B'] > GRADE_WEIGHTS['C'] > GRADE_WEIGHTS['D']
        # X is exclude-only
        assert GRADE_WEIGHTS['X'] == 0.0

    def test_min_snr_positive(self):
        assert MIN_SNR_DB > 0

    def test_outlier_threshold_sensible(self):
        # 3 MADs ≈ 4.45σ; loose bracket
        assert 2.5 <= OUTLIER_THRESHOLD_MAD <= 5.0


# =============================================================================
# Weight calculation
# =============================================================================


class TestCalculateWeight:
    @pytest.fixture
    def combiner(self, phase2, output_file):
        return ConsensusCombiner(phase2, output_file, channels=[])

    def test_grade_x_returns_zero(self, combiner):
        m = make_measurement(quality_grade='X')
        assert combiner._calculate_weight(m) == 0.0

    def test_below_min_snr_returns_zero(self, combiner):
        m = make_measurement(snr_db=MIN_SNR_DB - 1.0)
        assert combiner._calculate_weight(m) == 0.0

    def test_at_min_snr_returns_zero(self, combiner):
        # Strictly less-than gate at MIN_SNR_DB → equality crosses the floor
        m = make_measurement(snr_db=MIN_SNR_DB)
        # SNR weight at exactly MIN_SNR is clamped to 0.1; full weight is non-zero
        assert combiner._calculate_weight(m) > 0.0

    def test_grade_b_lower_than_grade_a(self, combiner):
        a = make_measurement(quality_grade='A')
        b = make_measurement(quality_grade='B')
        assert combiner._calculate_weight(a) > combiner._calculate_weight(b)

    def test_higher_snr_higher_weight(self, combiner):
        low = make_measurement(snr_db=10.0)
        high = make_measurement(snr_db=25.0)
        assert combiner._calculate_weight(high) > combiner._calculate_weight(low)

    def test_low_confidence_floored_at_0_1(self, combiner):
        # Confidence is floored to 0.1, so even confidence=0 gets some weight
        zero_conf = make_measurement(confidence=0.0)
        full_conf = make_measurement(confidence=1.0)
        assert 0.0 < combiner._calculate_weight(zero_conf) < combiner._calculate_weight(full_conf)


# =============================================================================
# Outlier detection (MAD)
# =============================================================================


class TestDetectOutliers:
    @pytest.fixture
    def combiner(self, phase2, output_file):
        return ConsensusCombiner(phase2, output_file, channels=[])

    def test_returns_unchanged_when_under_three(self, combiner):
        ms = [make_measurement(d_clock_ms=1.0), make_measurement(d_clock_ms=10.0)]
        included, outliers = combiner._detect_outliers(ms)
        assert outliers == []
        assert included == ms

    def test_no_outliers_for_tight_cluster(self, combiner):
        # Spread well below MAD threshold
        ms = [make_measurement(d_clock_ms=v) for v in [1.0, 1.05, 1.1, 0.95, 0.98]]
        included, outliers = combiner._detect_outliers(ms)
        assert outliers == []
        assert len(included) == 5

    def test_identical_data_short_circuits(self, combiner):
        ms = [make_measurement(d_clock_ms=1.0) for _ in range(5)]
        included, outliers = combiner._detect_outliers(ms)
        # MAD ~0 → early return, nothing dropped
        assert outliers == []
        assert len(included) == 5

    def test_extreme_outlier_excluded(self, combiner):
        # Tight cluster around 1.0 + one outlier at 100.0
        ms = [
            make_measurement(channel_name='C1', d_clock_ms=1.0),
            make_measurement(channel_name='C2', d_clock_ms=1.1),
            make_measurement(channel_name='C3', d_clock_ms=0.9),
            make_measurement(channel_name='C4', d_clock_ms=1.05),
            make_measurement(channel_name='OUT', d_clock_ms=100.0),
        ]
        included, outliers = combiner._detect_outliers(ms)
        assert 'OUT' in outliers
        assert all(m.channel_name != 'OUT' for m in included)

    def test_outliers_use_modified_z_score_threshold(self, combiner):
        # Construct values such that one is exactly at the threshold
        median = 1.0
        # mad ≈ 0.5; threshold = 3 * 0.5 * 1.4826 ≈ 2.224
        values = [0.5, 1.0, 1.5, 1.0, 0.5, 1.5, 1.0]  # MAD=0.5
        ms = [make_measurement(channel_name=f'C{i}', d_clock_ms=v) for i, v in enumerate(values)]
        # Add a value just inside (1.0 + 2.0 = 3.0 — outside 2.224 → outlier)
        ms.append(make_measurement(channel_name='AT_THRESHOLD', d_clock_ms=median + 3.0))
        included, outliers = combiner._detect_outliers(ms)
        assert 'AT_THRESHOLD' in outliers


# =============================================================================
# Group by station
# =============================================================================


class TestGroupByStation:
    @pytest.fixture
    def combiner(self, phase2, output_file):
        return ConsensusCombiner(phase2, output_file, channels=[])

    def test_empty_input(self, combiner):
        groups = combiner._group_by_station([])
        assert set(groups) == {'WWV', 'WWVH', 'CHU'}
        assert all(g == [] for g in groups.values())

    def test_assigns_to_correct_station(self, combiner):
        ms = [
            make_measurement(channel_name='WWV_10000', station='WWV'),
            make_measurement(channel_name='CHU_3330', station='CHU'),
            make_measurement(channel_name='WWVH_15000', station='WWVH'),
        ]
        groups = combiner._group_by_station(ms)
        assert len(groups['WWV']) == 1
        assert len(groups['CHU']) == 1
        assert len(groups['WWVH']) == 1

    def test_unknown_station_logged_and_dropped(self, combiner, caplog):
        ms = [make_measurement(station='BPM')]  # not in WWV/WWVH/CHU
        groups = combiner._group_by_station(ms)
        assert all(g == [] for g in groups.values())
        assert any('Unknown station' in r.message for r in caplog.records)

    def test_lowercase_station_normalized_to_upper(self, combiner):
        ms = [make_measurement(station='wwv')]
        groups = combiner._group_by_station(ms)
        assert len(groups['WWV']) == 1


# =============================================================================
# Per-station estimate
# =============================================================================


class TestEstimateStation:
    @pytest.fixture
    def combiner(self, phase2, output_file):
        return ConsensusCombiner(phase2, output_file, channels=[])

    def test_empty_returns_none(self, combiner):
        assert combiner._estimate_station('WWV', []) is None

    def test_all_zero_weight_returns_none(self, combiner):
        # All 'X' grade → weights all zero → returns None
        ms = [make_measurement(quality_grade='X') for _ in range(3)]
        assert combiner._estimate_station('WWV', ms) is None

    def test_single_measurement_uses_default_uncertainty(self, combiner):
        m = make_measurement(d_clock_ms=2.5)
        est = combiner._estimate_station('WWV', [m])
        assert est is not None
        assert est.d_clock_ms == pytest.approx(2.5)
        # Single-measurement default uncertainty per source code
        assert est.uncertainty_ms == pytest.approx(2.0)
        assert est.n_channels == 1

    def test_weighted_mean_favors_high_grade(self, combiner):
        # Grade A measurement at 1.0 vs Grade D at 100.0
        # Weighted mean must be much closer to 1.0 (A weight=1.0 vs D weight=0.15)
        ms = [
            make_measurement(quality_grade='A', d_clock_ms=1.0, snr_db=25.0),
            make_measurement(quality_grade='D', d_clock_ms=100.0, snr_db=25.0),
        ]
        est = combiner._estimate_station('WWV', ms)
        assert est.d_clock_ms < 50.0  # weighted toward 1.0

    def test_best_quality_reflects_min_grade_index(self, combiner):
        ms = [
            make_measurement(quality_grade='C', channel_name='C1'),
            make_measurement(quality_grade='A', channel_name='C2'),
            make_measurement(quality_grade='B', channel_name='C3'),
        ]
        est = combiner._estimate_station('WWV', ms)
        assert est.best_quality == 'A'

    def test_channel_list_preserved(self, combiner):
        ms = [
            make_measurement(channel_name='WWV_5000'),
            make_measurement(channel_name='WWV_10000'),
        ]
        est = combiner._estimate_station('WWV', ms)
        assert sorted(est.channels) == ['WWV_10000', 'WWV_5000']


# =============================================================================
# Status file reading
# =============================================================================


class TestReadChannelStatus:
    def test_missing_file_returns_none(self, phase2, output_file):
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        assert c._read_channel_status('WWV_10000') is None

    def test_malformed_json_returns_none_and_logs(self, phase2, output_file, caplog):
        status_dir = phase2 / 'WWV_10000' / 'status'
        status_dir.mkdir(parents=True)
        (status_dir / 'analytics-service-status.json').write_text("{not json")
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        assert c._read_channel_status('WWV_10000') is None
        assert any('Failed to read status' in r.message for r in caplog.records)

    def test_missing_d_clock_returns_none(self, phase2, output_file):
        write_status_file(phase2, 'WWV_10000',
                          status_payload('WWV_10000', d_clock_ms=None))
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        assert c._read_channel_status('WWV_10000') is None

    def test_well_formed_status_parsed(self, phase2, output_file):
        write_status_file(phase2, 'WWV_10000', status_payload(
            'WWV_10000',
            d_clock_ms=2.34,
            quality_grade='B',
            quality_metrics={'last_snr_db': 18.0},
            propagation_delay_ms=4.5,
            n_hops=1,
            time_snap={'confidence': 0.75},
        ))
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        m = c._read_channel_status('WWV_10000')
        assert m is not None
        assert m.d_clock_ms == 2.34
        assert m.quality_grade == 'B'
        assert m.snr_db == 18.0
        assert m.propagation_delay_ms == 4.5
        assert m.n_hops == 1
        assert m.confidence == 0.75
        assert m.station == 'WWV'

    def test_falls_back_to_first_channel_in_dict(self, phase2, output_file):
        # The status file holds a different key than the requested channel name
        status_dir = phase2 / 'WWV_10000' / 'status'
        status_dir.mkdir(parents=True)
        (status_dir / 'analytics-service-status.json').write_text(json.dumps({
            'channels': {
                'OTHER_KEY': {
                    'station': 'WWV',
                    'd_clock_ms': 0.5,
                    'quality_grade': 'A',
                    'quality_metrics': {'last_snr_db': 22.0},
                    'time_snap': {'confidence': 0.9},
                }
            }
        }))
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        m = c._read_channel_status('WWV_10000')
        # First-channel fallback fires
        assert m is not None
        assert m.d_clock_ms == 0.5


# =============================================================================
# compute_consensus end-to-end
# =============================================================================


class TestComputeConsensus:
    def test_no_channels_yields_no_data(self, phase2, output_file):
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000', 'CHU_3330'])
        result = c.compute_consensus()
        assert result.convergence_state == 'NO_DATA'
        assert result.included_channels == 0
        assert result.total_channels == 2
        assert result.uncertainty_ms == 100.0  # sentinel

    def test_single_station_marked_single_source(self, phase2, output_file):
        # One channel from WWV only
        write_status_file(phase2, 'WWV_10000', status_payload('WWV_10000',
                                                              station='WWV'))
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        result = c.compute_consensus()
        assert result.convergence_state == 'SINGLE_SOURCE'
        assert set(result.station_estimates) == {'WWV'}
        assert result.included_channels == 1

    def test_locked_when_stations_within_one_ms(self, phase2, output_file):
        # WWV at 1.0, CHU at 1.5 → spread 0.5 → LOCKED
        write_status_file(phase2, 'WWV_10000', status_payload(
            'WWV_10000', station='WWV', d_clock_ms=1.0))
        write_status_file(phase2, 'CHU_3330', status_payload(
            'CHU_3330', station='CHU', d_clock_ms=1.5))
        c = ConsensusCombiner(phase2, output_file,
                              channels=['WWV_10000', 'CHU_3330'])
        result = c.compute_consensus()
        assert result.convergence_state == 'LOCKED'
        assert result.station_agreement_ms == pytest.approx(0.5, abs=1e-9)

    def test_converging_when_stations_within_three_ms(self, phase2, output_file):
        # WWV at 0.0, CHU at 2.0 → spread 2.0 → CONVERGING
        write_status_file(phase2, 'WWV_10000', status_payload(
            'WWV_10000', station='WWV', d_clock_ms=0.0))
        write_status_file(phase2, 'CHU_3330', status_payload(
            'CHU_3330', station='CHU', d_clock_ms=2.0))
        c = ConsensusCombiner(phase2, output_file,
                              channels=['WWV_10000', 'CHU_3330'])
        result = c.compute_consensus()
        assert result.convergence_state == 'CONVERGING'

    def test_divergent_when_stations_far_apart(self, phase2, output_file):
        # WWV at 0, CHU at 10 → spread 10 → DIVERGENT
        write_status_file(phase2, 'WWV_10000', status_payload(
            'WWV_10000', station='WWV', d_clock_ms=0.0))
        write_status_file(phase2, 'CHU_3330', status_payload(
            'CHU_3330', station='CHU', d_clock_ms=10.0))
        c = ConsensusCombiner(phase2, output_file,
                              channels=['WWV_10000', 'CHU_3330'])
        result = c.compute_consensus()
        assert result.convergence_state == 'DIVERGENT'

    def test_outlier_excluded_via_mad(self, phase2, output_file):
        # 4 tight + 1 outlier → outlier dropped
        for i, val in enumerate([1.0, 1.05, 0.95, 1.1]):
            write_status_file(phase2, f'WWV_{i:05d}', status_payload(
                f'WWV_{i:05d}', station='WWV', d_clock_ms=val))
        write_status_file(phase2, 'WWV_OUT', status_payload(
            'WWV_OUT', station='WWV', d_clock_ms=50.0))
        c = ConsensusCombiner(phase2, output_file,
                              channels=['WWV_00000', 'WWV_00001', 'WWV_00002',
                                        'WWV_00003', 'WWV_OUT'])
        result = c.compute_consensus()
        assert 'WWV_OUT' in result.outlier_channels
        assert result.included_channels == 4

    def test_d_clock_consensus_close_to_data(self, phase2, output_file):
        # All stations near 2.0 → consensus near 2.0
        for chan in ['WWV_10000', 'CHU_3330', 'WWVH_15000']:
            station = 'WWVH' if chan.startswith('WWVH') else chan.split('_')[0]
            write_status_file(phase2, chan, status_payload(
                chan, station=station, d_clock_ms=2.0))
        c = ConsensusCombiner(phase2, output_file,
                              channels=['WWV_10000', 'CHU_3330', 'WWVH_15000'])
        result = c.compute_consensus()
        assert result.d_clock_ms == pytest.approx(2.0, abs=0.01)


# =============================================================================
# run_and_save
# =============================================================================


class TestRunAndSave:
    def test_writes_json_atomically_and_returns_result(self, phase2, output_file):
        write_status_file(phase2, 'WWV_10000', status_payload('WWV_10000',
                                                              station='WWV'))
        c = ConsensusCombiner(phase2, output_file, channels=['WWV_10000'])
        result = c.run_and_save()

        assert isinstance(result, ConsensusResult)
        assert output_file.exists()
        # tmp file should not linger after atomic rename
        assert not output_file.with_suffix('.tmp').exists()

        payload = json.loads(output_file.read_text())
        assert payload['service'] == 'consensus_combiner'
        assert 'consensus' in payload
        assert payload['consensus']['d_clock_ms'] == pytest.approx(result.d_clock_ms)
        assert 'stations' in payload
        assert 'diagnostics' in payload

    def test_creates_parent_directories(self, phase2, tmp_path):
        # Force a deeply-nested output path
        out = tmp_path / 'a' / 'b' / 'c' / 'consensus.json'
        c = ConsensusCombiner(phase2, out, channels=[])
        c.run_and_save()
        assert out.exists()


# =============================================================================
# create_combiner_from_config
# =============================================================================


class TestCreateCombinerFromConfig:
    def test_uses_enabled_channel_descriptions(self, tmp_path):
        config = {
            'recorder': {
                'channels': [
                    {'description': 'WWV_10000', 'enabled': True, 'ssrc': 1},
                    {'description': 'CHU_3330', 'enabled': True, 'ssrc': 2},
                    {'description': 'BPM_5000', 'enabled': False, 'ssrc': 3},
                ]
            }
        }
        c = create_combiner_from_config(config, tmp_path)
        assert c.channels == ['WWV_10000', 'CHU_3330']
        assert c.phase2_dir == tmp_path / 'phase2'
        assert c.output_file == tmp_path / 'shared' / 'consensus_timing.json'

    def test_defaults_enabled_to_true(self, tmp_path):
        # 'enabled' field absent → channel is included
        config = {
            'recorder': {
                'channels': [{'description': 'WWV_10000', 'ssrc': 1}],
            }
        }
        c = create_combiner_from_config(config, tmp_path)
        assert c.channels == ['WWV_10000']

    def test_falls_back_to_ssrc_label_when_no_description(self, tmp_path):
        config = {
            'recorder': {
                'channels': [{'enabled': True, 'ssrc': 4242}],
            }
        }
        c = create_combiner_from_config(config, tmp_path)
        assert c.channels == ['Channel 4242']

    def test_empty_config_yields_empty_channels(self, tmp_path):
        c = create_combiner_from_config({}, tmp_path)
        assert c.channels == []
