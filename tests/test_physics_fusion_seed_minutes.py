"""Regression test for P-H25.

physics_fusion_service kept ``_processed_minutes`` only in memory, so on
every restart it was empty and the wide startup lookback window
reprocessed minutes whose L3 records already existed — producing
duplicate TEC/dTEC/L3 records. ``_seed_processed_minutes_from_l3`` reads
the already-written minute boundaries back from the L3 dtec table.

Post-Phase-4 (HDF5 → SQLite): the seed reads SQLite via
``make_data_product_reader``; the P-H25 contract is unchanged.
"""

import time
from datetime import datetime, timezone

from hf_timestd.core.physics_fusion_service import PhysicsFusionService
from hf_timestd.io.sqlite_writer import SqliteDataProductWriter


def _bare_service(tmp_path, db_path):
    svc = PhysicsFusionService.__new__(PhysicsFusionService)
    svc.data_root = tmp_path
    svc._processed_minutes = set()
    svc._storage_config = {
        "read_sqlite": True,
        "sqlite_path": str(db_path),
    }
    return svc


def _write_l3(tmp_path, db_path, minutes):
    """Insert one L3_dtec row per minute_boundary into the test DB."""
    dtec_dir = tmp_path / 'phase2' / 'science' / 'dtec'
    writer = SqliteDataProductWriter(
        output_dir=dtec_dir,
        product_level='L3',
        product_name='dtec',
        channel='AGGREGATED',
        db_path=db_path,
    )
    try:
        for mb in minutes:
            mb_int = int(mb)
            ts_iso = datetime.fromtimestamp(
                mb_int, tz=timezone.utc
            ).isoformat().replace('+00:00', 'Z')
            writer.write_measurement({
                'timestamp_utc': ts_iso,
                'minute_boundary': mb_int,
                'station': 'TEST',
                'channel': 'AGGREGATED',
                'frequency_mhz': 10.0,
                'n_ticks': 0,
                'dtec_mean_tecu': 0.0,
                'dtec_std_tecu': 0.0,
                'dtec_rate_tecu_per_s': 0.0,
                'is_anchored': False,
                'quality_flag': 'GOOD',
                'processing_version': 'test',
            })
    finally:
        writer.close()


def _recent_minute() -> int:
    """A minute-aligned epoch near 'now' so the seed's 3-day window picks it up."""
    return int(time.time() // 60) * 60


def test_seed_from_l3_dtec(tmp_path):
    db_path = tmp_path / 'phase2' / 'timestd.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    base = _recent_minute() - 600  # 10 minutes ago
    minutes = [base, base + 60, base + 120]
    _write_l3(tmp_path, db_path, minutes)
    svc = _bare_service(tmp_path, db_path)
    svc._seed_processed_minutes_from_l3()
    for m in minutes:
        assert m in svc._processed_minutes


def test_seed_normalises_to_minute_boundary(tmp_path):
    """A minute_boundary carrying sub-minute slop must seed the
    minute-aligned epoch the run-loop's target_minute uses."""
    db_path = tmp_path / 'phase2' / 'timestd.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = _recent_minute() - 600 + 17  # 17 seconds past a minute boundary
    expected = raw - (raw % 60)
    _write_l3(tmp_path, db_path, [raw])
    svc = _bare_service(tmp_path, db_path)
    svc._seed_processed_minutes_from_l3()
    assert expected in svc._processed_minutes


def test_seed_with_no_db_is_safe(tmp_path):
    """No SQLite DB file ⇒ no prior data ⇒ silent no-op."""
    db_path = tmp_path / 'phase2' / 'timestd.db'
    svc = _bare_service(tmp_path, db_path)
    svc._seed_processed_minutes_from_l3()  # must not raise
    assert svc._processed_minutes == set()
