"""Regression test for TimingValidationService.load_fusion_result after
the Phase-4 HDF5 → SQLite reader conversion.

The method previously opened `fusion_fusion_timing_{date}.h5` via raw
h5py and indexed by `minute_boundary` equality.  It now reads the same
data from `L3_fusion_timing` via `make_data_product_reader`.  The
external contract — return a dict of key columns for a matching minute,
or None for no match — is preserved.
"""

import time
from datetime import datetime, timezone

from hf_timestd.core.timing_validation_service import TimingValidationService
from hf_timestd.io.sqlite_writer import SqliteDataProductWriter


def _write_fusion_row(db_path, fusion_dir, minute_boundary, **overrides):
    writer = SqliteDataProductWriter(
        output_dir=fusion_dir,
        product_level='L3',
        product_name='fusion_timing',
        channel='fusion',
        db_path=db_path,
    )
    try:
        row = {
            'timestamp_utc': datetime.fromtimestamp(
                minute_boundary, tz=timezone.utc
            ).isoformat().replace('+00:00', 'Z'),
            'minute_boundary': minute_boundary,
            'd_clock_fused_ms': 0.5,
            'd_clock_raw_ms': 0.4,
            'uncertainty_ms': 0.1,
            'statistical_uncertainty_ms': 0.05,
            'systematic_uncertainty_ms': 0.05,
            'propagation_uncertainty_ms': 0.01,
            'n_broadcasts': 4,
            'n_stations': 2,
            'stations_used': 'WWV,CHU',
            'wwv_count': 2,
            'wwvh_count': 0,
            'chu_count': 2,
            'bpm_count': 0,
            'consistency_flag': 'OK',
            'global_solve_verified': True,
            'global_solve_n_obs': 4,
            'calibration_applied': True,
            'reference_station': 'WWV',
            'outliers_rejected': 0,
            'quality_grade': 'A',
            'kalman_state': 'LOCKED',
            'quality_flag': 'GOOD',
            'processing_version': 'test',
            'single_station_mode': False,
        }
        row.update(overrides)
        writer.write_measurement(row)
    finally:
        writer.close()


def _service(tmp_path):
    db_path = tmp_path / 'phase2' / 'timestd.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fusion_dir = tmp_path / 'phase2' / 'fusion'
    return TimingValidationService(
        raw_buffer_path=str(tmp_path / 'raw_buffer'),
        hot_buffer_path=str(tmp_path / 'hot_buffer'),
        fusion_output_path=str(fusion_dir),
        storage_config={'read_sqlite': True, 'sqlite_path': str(db_path)},
    ), db_path, fusion_dir


def test_load_fusion_result_returns_matching_row(tmp_path):
    svc, db_path, fusion_dir = _service(tmp_path)
    minute_boundary = int(time.time() // 60) * 60 - 300
    _write_fusion_row(db_path, fusion_dir, minute_boundary)

    result = svc.load_fusion_result(minute_boundary)

    assert result is not None
    assert result['minute_boundary'] == minute_boundary
    assert result['d_clock_fused_ms'] == 0.5
    assert result['quality_grade'] == 'A'
    assert result['kalman_state'] == 'LOCKED'


def test_load_fusion_result_no_match_returns_none(tmp_path):
    svc, db_path, fusion_dir = _service(tmp_path)
    minute_boundary = int(time.time() // 60) * 60 - 300
    _write_fusion_row(db_path, fusion_dir, minute_boundary)

    # Ask for an unwritten minute well outside the ±2 min lookup window.
    assert svc.load_fusion_result(minute_boundary - 600) is None


def test_load_fusion_result_no_db_returns_none(tmp_path):
    svc, _db_path, _fusion_dir = _service(tmp_path)
    minute_boundary = int(time.time() // 60) * 60 - 300

    # No DB file present; loader must not raise.
    assert svc.load_fusion_result(minute_boundary) is None


def test_load_fusion_result_caches(tmp_path):
    svc, db_path, fusion_dir = _service(tmp_path)
    minute_boundary = int(time.time() // 60) * 60 - 300
    _write_fusion_row(db_path, fusion_dir, minute_boundary)

    first = svc.load_fusion_result(minute_boundary)
    assert minute_boundary in svc._fusion_cache
    # Second call hits the cache (identity guarantees the cache served it).
    assert svc.load_fusion_result(minute_boundary) is first
