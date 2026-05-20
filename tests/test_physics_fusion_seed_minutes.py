"""Regression test for P-H25.

physics_fusion_service kept ``_processed_minutes`` only in memory, so on
every restart it was empty and the wide startup lookback window
reprocessed minutes whose L3 records already existed — producing
duplicate TEC/dTEC/L3 records. ``_seed_processed_minutes_from_l3`` reads
the already-written minute boundaries back from the L3 dtec output.
"""

import h5py
import numpy as np

from hf_timestd.core.physics_fusion_service import PhysicsFusionService


def _bare_service(tmp_path):
    svc = PhysicsFusionService.__new__(PhysicsFusionService)
    svc.data_root = tmp_path
    svc._processed_minutes = set()
    return svc


def _write_l3(tmp_path, minutes):
    dtec_dir = tmp_path / 'phase2' / 'science' / 'dtec'
    dtec_dir.mkdir(parents=True, exist_ok=True)
    with h5py.File(dtec_dir / 'AGGREGATED_dtec_20260519.h5', 'w') as f:
        f.create_dataset('minute_boundary',
                         data=np.array(minutes, dtype=np.float64))


def test_seed_from_l3_dtec(tmp_path):
    minutes = [1_700_000_040, 1_700_000_100, 1_700_000_160]
    _write_l3(tmp_path, minutes)
    svc = _bare_service(tmp_path)
    svc._seed_processed_minutes_from_l3()
    for m in minutes:
        assert m in svc._processed_minutes


def test_seed_normalises_to_minute_boundary(tmp_path):
    # A minute_boundary carrying sub-minute slop must seed the
    # minute-aligned epoch the run-loop's target_minute uses.
    _write_l3(tmp_path, [1_700_000_117.0])
    svc = _bare_service(tmp_path)
    svc._seed_processed_minutes_from_l3()
    assert 1_700_000_100 in svc._processed_minutes


def test_seed_with_no_l3_directory_is_safe(tmp_path):
    svc = _bare_service(tmp_path)
    svc._seed_processed_minutes_from_l3()  # must not raise
    assert svc._processed_minutes == set()
