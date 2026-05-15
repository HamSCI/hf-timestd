"""Unit tests for DualWriter and the make_data_product_writer factory.

Phase 1 of the HDF5 → SQLite migration. See
``docs/HDF5-TO-SQLITE-MIGRATION.md`` and the writer modules under
``src/hf_timestd/io/``.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from hf_timestd.io import (
    DataProductReader,
    DataProductWriter,
    DualWriter,
    SqliteDataProductWriter,
    make_data_product_writer,
)


@pytest.fixture
def temp_dir():
    p = Path(tempfile.mkdtemp())
    yield p
    shutil.rmtree(p, ignore_errors=True)


@pytest.fixture
def sample_l2_measurement():
    return {
        "timestamp_utc": "2026-05-15T17:00:00Z",
        "minute_boundary_utc": 1778857200,
        "rtp_timestamp": 123456789,
        "station": "WWV",
        "frequency_mhz": 10.0,
        "discrimination_method": "TONE",
        "discrimination_confidence": 0.85,
        "tone_detected": True,
        "raw_arrival_time_ms": 5.38,
        "clock_offset_ms": -2.14,
        "uncertainty_ms": 1.2,
        "expanded_uncertainty_ms": 2.4,
        "coverage_factor": 2.0,
        "confidence_level": 0.95,
        "u_rtp_timestamp_ms": 0.05,
        "u_ionospheric_ms": 1.0,
        "u_multipath_ms": 0.5,
        "u_discrimination_ms": 0.3,
        "u_gpsdo_ms": 0.001,
        "u_propagation_model_ms": 0.3,
        "degrees_of_freedom": 1000,
        "quality_grade": "B",
        "confidence": 0.85,
        "quality_flag": "GOOD",
        "propagation_delay_ms": 5.38,
        "propagation_mode": "1E",
        "n_hops": 1,
        "snr_db": 15.3,
        "traceability_chain": "GPSDO → UTC(GPS) → UTC(NIST)",
        "processing_version": "3.2.0",
        "processed_at": "2026-05-15T17:01:00Z",
        "calibration_date": "2026-05-01T00:00:00Z",
        "gpsdo_locked": True,
    }


# ---------------------------------------------------------------------
# Factory tests — config drives which backend(s) are constructed.
# ---------------------------------------------------------------------


class TestFactory:
    def _kwargs(self, temp_dir):
        return dict(
            output_dir=temp_dir,
            product_level="L2",
            product_name="timing_measurements",
            channel="WWV_10000",
        )

    def test_hdf5_only_default(self, temp_dir):
        """No storage_config → today's behaviour (HDF5 only)."""
        writer = make_data_product_writer(**self._kwargs(temp_dir))
        try:
            assert isinstance(writer, DataProductWriter)
        finally:
            writer.close()

    def test_hdf5_only_explicit(self, temp_dir):
        writer = make_data_product_writer(
            **self._kwargs(temp_dir),
            storage_config={"write_hdf5": True, "write_sqlite": False},
        )
        try:
            assert isinstance(writer, DataProductWriter)
        finally:
            writer.close()

    def test_sqlite_only(self, temp_dir):
        db = temp_dir / "timestd.db"
        writer = make_data_product_writer(
            **self._kwargs(temp_dir),
            storage_config={
                "write_hdf5": False,
                "write_sqlite": True,
                "sqlite_path": str(db),
            },
        )
        try:
            assert isinstance(writer, SqliteDataProductWriter)
        finally:
            writer.close()

    def test_dual_writer(self, temp_dir):
        db = temp_dir / "timestd.db"
        writer = make_data_product_writer(
            **self._kwargs(temp_dir),
            storage_config={
                "write_hdf5": True,
                "write_sqlite": True,
                "sqlite_path": str(db),
            },
        )
        try:
            assert isinstance(writer, DualWriter)
        finally:
            writer.close()

    def test_both_disabled_raises(self, temp_dir):
        with pytest.raises(ValueError, match="at least one"):
            make_data_product_writer(
                **self._kwargs(temp_dir),
                storage_config={"write_hdf5": False, "write_sqlite": False},
            )


# ---------------------------------------------------------------------
# DualWriter behaviour — both backends see the same rows.
# ---------------------------------------------------------------------


class TestDualWriter:
    def _make_dual(self, temp_dir, channel="WWV_10000"):
        db = temp_dir / "timestd.db"
        return make_data_product_writer(
            output_dir=temp_dir,
            product_level="L2",
            product_name="timing_measurements",
            channel=channel,
            storage_config={
                "write_hdf5": True,
                "write_sqlite": True,
                "sqlite_path": str(db),
            },
        )

    def test_single_write_lands_in_both_backends(self, temp_dir, sample_l2_measurement):
        writer = self._make_dual(temp_dir)
        try:
            writer.write_measurement(sample_l2_measurement)
            # HDF5 file should exist with one row
            h5_path = temp_dir / "WWV_10000_timing_measurements_20260515.h5"
            assert h5_path.exists()
            with h5py.File(h5_path, "r", swmr=True) as f:
                assert f["timestamp_utc"].shape[0] == 1
                assert f["clock_offset_ms"][-1] == pytest.approx(-2.14)
            # SQLite DB should also have one row
            db = temp_dir / "timestd.db"
            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT clock_offset_ms FROM L2_timing_measurements WHERE channel = ?",
                ("WWV_10000",),
            ).fetchone()
            conn.close()
            assert row is not None
            assert row[0] == pytest.approx(-2.14)
        finally:
            writer.close()

    def test_batch_write_lands_in_both_backends(self, temp_dir, sample_l2_measurement):
        writer = self._make_dual(temp_dir)
        try:
            batch = []
            for i in range(5):
                m = dict(sample_l2_measurement)
                m["timestamp_utc"] = f"2026-05-15T17:{i:02d}:00Z"
                m["minute_boundary_utc"] = 1778857200 + i * 60
                batch.append(m)
            writer.write_measurements_batch(batch)
            # HDF5 should have 5 rows
            h5_path = temp_dir / "WWV_10000_timing_measurements_20260515.h5"
            with h5py.File(h5_path, "r", swmr=True) as f:
                assert f["timestamp_utc"].shape[0] == 5
            # SQLite should have 5 rows
            db = temp_dir / "timestd.db"
            conn = sqlite3.connect(str(db))
            n = conn.execute(
                "SELECT COUNT(*) FROM L2_timing_measurements WHERE channel = ?",
                ("WWV_10000",),
            ).fetchone()[0]
            conn.close()
            assert n == 5
        finally:
            writer.close()

    def test_validation_failure_blocks_both(self, temp_dir, sample_l2_measurement):
        """A bad row should be rejected before EITHER backend sees it."""
        writer = self._make_dual(temp_dir)
        try:
            bad = dict(sample_l2_measurement)
            bad["station"] = "INVALID_STATION"
            with pytest.raises(ValueError):
                writer.write_measurement(bad)
            # Neither backend should have a row.
            h5_path = temp_dir / "WWV_10000_timing_measurements_20260515.h5"
            if h5_path.exists():
                with h5py.File(h5_path, "r", swmr=True) as f:
                    # File may exist but be empty
                    assert f["timestamp_utc"].shape[0] == 0
            db = temp_dir / "timestd.db"
            if db.exists():
                conn = sqlite3.connect(str(db))
                n = conn.execute(
                    "SELECT COUNT(*) FROM L2_timing_measurements WHERE channel = ?",
                    ("WWV_10000",),
                ).fetchone()[0]
                conn.close()
                assert n == 0
        finally:
            writer.close()

    def test_close_closes_both(self, temp_dir):
        writer = self._make_dual(temp_dir)
        # Smoke: close() must not raise even if one backend has nothing to flush
        writer.close()
        # Doubled close is idempotent on both backends
        writer.close()

    def test_context_manager(self, temp_dir, sample_l2_measurement):
        db_path = temp_dir / "timestd.db"
        with make_data_product_writer(
            output_dir=temp_dir,
            product_level="L2",
            product_name="timing_measurements",
            channel="WWV_10000",
            storage_config={
                "write_hdf5": True,
                "write_sqlite": True,
                "sqlite_path": str(db_path),
            },
        ) as writer:
            assert isinstance(writer, DualWriter)
            writer.write_measurement(sample_l2_measurement)

        # After exit, both backends should have closed their handles.
        # We verify by re-opening SQLite and reading the row independently.
        conn = sqlite3.connect(str(db_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM L2_timing_measurements"
        ).fetchone()[0]
        conn.close()
        assert n == 1

    def test_test_measurement_writes_both(self, temp_dir):
        writer = self._make_dual(temp_dir)
        try:
            assert writer.write_test_measurement() is True
        finally:
            writer.close()
