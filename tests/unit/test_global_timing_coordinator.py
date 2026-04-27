"""
Unit tests for hf_timestd.core.global_timing_coordinator

The coordinator aggregates per-channel detections from a shared directory
and runs the GlobalDifferentialSolver for a verified UTC back-calculation.

Tests stub the solver to keep these unit tests focused on the I/O and
result-management logic.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import hf_timestd.core.global_timing_coordinator as gtc_mod
from hf_timestd.core.global_timing_coordinator import (
    ChannelDetection,
    GlobalTimingCoordinator,
    GlobalTimingResult,
    create_coordinator,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def coordinator(tmp_path):
    # Patch the solver so we don't need a real receiver location lookup
    with patch.object(gtc_mod, 'GlobalDifferentialSolver') as MockSolver:
        c = GlobalTimingCoordinator(
            shared_dir=tmp_path / 'shared',
            grid_square='EM38ww',
            min_channels=2,
            sample_rate=20000,
        )
        c.solver = MockSolver.return_value
    return c


def make_solve_result(
    clock_error_ms=1.0,
    uncertainty_ms=0.5,
    confidence=0.9,
    quality_grade='A',
    verified=True,
    n_observations=4,
    n_pairs=6,
    pair_consistency_ms=0.3,
    mode_assignments=None,
):
    r = MagicMock()
    r.clock_error_ms = clock_error_ms
    r.uncertainty_ms = uncertainty_ms
    r.confidence = confidence
    r.quality_grade = quality_grade
    r.verified = verified
    r.n_observations = n_observations
    r.n_pairs = n_pairs
    r.pair_consistency_ms = pair_consistency_ms
    r.mode_assignments = mode_assignments or []
    return r


# =============================================================================
# Construction
# =============================================================================


class TestConstruction:
    def test_creates_detections_dir(self, coordinator):
        assert coordinator.detections_dir.is_dir()
        assert coordinator.detections_dir.name == 'detections'

    def test_results_file_is_under_shared(self, coordinator, tmp_path):
        assert coordinator.results_file == tmp_path / 'shared' / 'global_timing.json'

    def test_solver_initialized(self, coordinator):
        assert coordinator.solver is not None


# =============================================================================
# write_detection
# =============================================================================


class TestWriteDetection:
    def test_creates_detection_file(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(
            minute_utc=minute, channel='WWV_10000', station='WWV',
            frequency_mhz=10.0, timing_error_ms=2.5, snr_db=15.0,
        )
        f = coordinator.detections_dir / '20260426_1200.json'
        assert f.exists()
        data = json.loads(f.read_text())
        assert data['minute_utc'].startswith('2026-04-26T12:00:00')
        assert len(data['detections']) == 1
        det = data['detections'][0]
        assert det['channel'] == 'WWV_10000'
        assert det['station'] == 'WWV'
        assert det['frequency_mhz'] == 10.0
        assert det['timing_error_ms'] == 2.5
        assert det['snr_db'] == 15.0

    def test_appends_distinct_channels(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        coordinator.write_detection(minute, 'CHU_3330', 'CHU', 3.33, 2.0)
        data = json.loads(
            (coordinator.detections_dir / '20260426_1200.json').read_text()
        )
        channels = {d['channel'] for d in data['detections']}
        assert channels == {'WWV_10000', 'CHU_3330'}

    def test_overwrites_same_channel(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0,
                                     timing_error_ms=1.0)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0,
                                     timing_error_ms=2.5)
        data = json.loads(
            (coordinator.detections_dir / '20260426_1200.json').read_text()
        )
        # Only one entry for that channel; carries the new value
        wwv = [d for d in data['detections'] if d['channel'] == 'WWV_10000']
        assert len(wwv) == 1
        assert wwv[0]['timing_error_ms'] == 2.5

    def test_recovers_from_corrupt_existing_file(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        f = coordinator.detections_dir / '20260426_1200.json'
        f.write_text("not json")
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.5)
        data = json.loads(f.read_text())
        assert len(data['detections']) == 1


# =============================================================================
# solve_minute
# =============================================================================


class TestSolveMinute:
    def test_no_file_returns_none(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        assert coordinator.solve_minute(minute) is None

    def test_below_min_channels_returns_none(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        # Only one detection — below min_channels=2
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        assert coordinator.solve_minute(minute) is None

    def test_low_confidence_solver_returns_none(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        coordinator.write_detection(minute, 'CHU_3330', 'CHU', 3.33, 2.0)
        coordinator.solver.solve_global.return_value = make_solve_result(
            confidence=0.05)
        assert coordinator.solve_minute(minute) is None

    def test_returns_global_timing_result(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        coordinator.write_detection(minute, 'CHU_3330', 'CHU', 3.33, 2.0)
        coordinator.solver.solve_global.return_value = make_solve_result(
            confidence=0.9, clock_error_ms=2.5, n_observations=2)
        result = coordinator.solve_minute(minute)
        assert isinstance(result, GlobalTimingResult)
        assert result.clock_error_ms == 2.5
        assert result.n_channels == 2
        assert result.minute_utc.startswith('2026-04-26T12:00')

    def test_corrupt_detection_file_returns_none(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        f = coordinator.detections_dir / '20260426_1200.json'
        f.write_text("not json")
        assert coordinator.solve_minute(minute) is None


# =============================================================================
# solve_and_save
# =============================================================================


class TestSolveAndSave:
    def test_writes_results_file(self, coordinator):
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        coordinator.write_detection(minute, 'CHU_3330', 'CHU', 3.33, 2.0)
        coordinator.solver.solve_global.return_value = make_solve_result()
        coordinator.solve_and_save(minute)
        assert coordinator.results_file.exists()
        data = json.loads(coordinator.results_file.read_text())
        assert 'latest' in data
        assert 'results' in data
        assert data['latest']['clock_error_ms'] == 1.0

    def test_keeps_last_60_results(self, coordinator):
        # Pre-populate with 65 historical results
        existing = {
            'results': [{'minute_utc': str(i)} for i in range(65)],
            'latest': None,
        }
        coordinator.results_file.parent.mkdir(parents=True, exist_ok=True)
        coordinator.results_file.write_text(json.dumps(existing))

        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        coordinator.write_detection(minute, 'WWV_10000', 'WWV', 10.0, 1.0)
        coordinator.write_detection(minute, 'CHU_3330', 'CHU', 3.33, 2.0)
        coordinator.solver.solve_global.return_value = make_solve_result()
        coordinator.solve_and_save(minute)

        data = json.loads(coordinator.results_file.read_text())
        assert len(data['results']) == 60

    def test_returns_none_when_solve_returns_none(self, coordinator):
        # No detection file → solve_minute returns None → save short-circuits
        minute = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
        assert coordinator.solve_and_save(minute) is None
        # Results file should not be created
        assert not coordinator.results_file.exists()


# =============================================================================
# get_latest_result
# =============================================================================


class TestGetLatestResult:
    def test_no_results_file_returns_none(self, coordinator):
        assert coordinator.get_latest_result() is None

    def test_returns_latest_when_present(self, coordinator):
        latest = {
            'minute_utc': '2026-04-26T12:00:00+00:00',
            'clock_error_ms': 1.0, 'uncertainty_ms': 0.5,
            'confidence': 0.9, 'quality_grade': 'A', 'verified': True,
            'n_channels': 4, 'n_pairs': 6, 'pair_consistency_ms': 0.3,
            'mode_assignments': [],
            'last_updated': '2026-04-26T12:00:00+00:00',
        }
        coordinator.results_file.parent.mkdir(parents=True, exist_ok=True)
        coordinator.results_file.write_text(json.dumps(
            {'results': [latest], 'latest': latest}))
        result = coordinator.get_latest_result()
        assert isinstance(result, GlobalTimingResult)
        assert result.clock_error_ms == 1.0

    def test_corrupt_file_returns_none(self, coordinator):
        coordinator.results_file.parent.mkdir(parents=True, exist_ok=True)
        coordinator.results_file.write_text("not json")
        assert coordinator.get_latest_result() is None

    def test_missing_latest_field_returns_none(self, coordinator):
        coordinator.results_file.parent.mkdir(parents=True, exist_ok=True)
        coordinator.results_file.write_text(json.dumps({'results': []}))
        assert coordinator.get_latest_result() is None


# =============================================================================
# cleanup_old_files
# =============================================================================


class TestCleanupOldFiles:
    def test_removes_only_old_files(self, coordinator):
        import os
        old = coordinator.detections_dir / '20251201_0000.json'
        old.write_text("{}")
        # Set mtime to 30 hours ago
        old_mtime = (datetime.now().timestamp() - 30 * 3600)
        os.utime(old, (old_mtime, old_mtime))

        recent = coordinator.detections_dir / '20260426_1200.json'
        recent.write_text("{}")

        removed = coordinator.cleanup_old_files(max_age_hours=24)
        assert removed == 1
        assert not old.exists()
        assert recent.exists()

    def test_zero_when_nothing_old(self, coordinator):
        # Fresh files only
        (coordinator.detections_dir / 'new.json').write_text("{}")
        assert coordinator.cleanup_old_files(max_age_hours=24) == 0


# =============================================================================
# create_coordinator factory
# =============================================================================


class TestCreateCoordinator:
    def test_factory_uses_shared_subdir(self, tmp_path):
        with patch.object(gtc_mod, 'GlobalDifferentialSolver'):
            c = create_coordinator(tmp_path, 'EM38ww')
        assert c.shared_dir == tmp_path / 'shared'
