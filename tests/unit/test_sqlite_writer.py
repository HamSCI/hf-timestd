"""Unit tests for SqliteDataProductWriter.

Phase 1 of the HDF5 → SQLite migration. See
``docs/HDF5-TO-SQLITE-MIGRATION.md`` and
``src/hf_timestd/io/sqlite_writer.py``.

Mirrors the structure of ``test_hdf5_io.py`` so any future
refactoring that touches both writers can keep them in lockstep.
"""

from __future__ import annotations

import sqlite3
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pytest

from hf_timestd.io.sqlite_writer import (
    SqliteDataProductWriter,
    _sqlite_type_for_field,
    _table_name,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    p = Path(tempfile.mkdtemp())
    yield p
    shutil.rmtree(p, ignore_errors=True)


@pytest.fixture
def temp_db(temp_dir):
    return temp_dir / "timestd.db"


@pytest.fixture
def sample_l2_measurement():
    """Same shape as tests/unit/test_hdf5_io.py — keeps the dual-write
    surface easy to compare."""
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


def _make_writer(temp_dir, temp_db, product="timing_measurements", level="L2", channel="WWV_10000"):
    return SqliteDataProductWriter(
        output_dir=temp_dir,
        product_level=level,
        product_name=product,
        channel=channel,
        db_path=temp_db,
    )


# ---------------------------------------------------------------------
# Helper-fn unit tests
# ---------------------------------------------------------------------


class TestHelpers:
    def test_sqlite_type_for_field(self):
        assert _sqlite_type_for_field({"type": "float"}) == "REAL"
        assert _sqlite_type_for_field({"type": "integer"}) == "INTEGER"
        assert _sqlite_type_for_field({"type": "string"}) == "TEXT"
        assert _sqlite_type_for_field({"type": "boolean"}) == "INTEGER"
        assert _sqlite_type_for_field({"type": "unknown"}) == "TEXT"

    def test_table_name(self):
        assert _table_name("L2", "timing_measurements") == "L2_timing_measurements"
        assert _table_name("L1", "metrology_measurements") == "L1_metrology_measurements"


# ---------------------------------------------------------------------
# Writer construction + DDL
# ---------------------------------------------------------------------


class TestConstruction:
    def test_create_writer(self, temp_dir, temp_db):
        writer = _make_writer(temp_dir, temp_db)
        try:
            assert writer.product_level == "L2"
            assert writer.product_name == "timing_measurements"
            assert writer.channel == "WWV_10000"
            assert writer.table == "L2_timing_measurements"
            assert writer.db_path == temp_db
            assert temp_db.exists()  # DB file created on first connection
        finally:
            writer.close()

    def test_table_ddl_includes_channel_column(self, temp_dir, temp_db):
        writer = _make_writer(temp_dir, temp_db)
        try:
            # Open the DB independently and inspect the table schema.
            conn = sqlite3.connect(str(temp_db))
            cur = conn.execute(f"PRAGMA table_info({writer.table})")
            cols = [row[1] for row in cur.fetchall()]
            assert "channel" in cols
            assert "timestamp_utc" in cols
            assert "clock_offset_ms" in cols
            conn.close()
        finally:
            writer.close()

    def test_table_ddl_idempotent(self, temp_dir, temp_db):
        """Creating two writers against the same DB should not error."""
        w1 = _make_writer(temp_dir, temp_db)
        w2 = _make_writer(temp_dir, temp_db, channel="CHU_7850")
        try:
            assert w1.table == w2.table  # Same product → same table
        finally:
            w1.close()
            w2.close()

    def test_wal_mode_enabled(self, temp_dir, temp_db):
        writer = _make_writer(temp_dir, temp_db)
        try:
            cur = writer._conn.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            writer.close()


# ---------------------------------------------------------------------
# write_measurement
# ---------------------------------------------------------------------


class TestWriteMeasurement:
    def test_basic_write(self, temp_dir, temp_db, sample_l2_measurement):
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurement(sample_l2_measurement)
            assert writer._measurement_count == 1
            assert writer.verify_last_write()
        finally:
            writer.close()

    def test_round_trip(self, temp_dir, temp_db, sample_l2_measurement):
        """Write, then read back via raw SQL — values match."""
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurement(sample_l2_measurement)
            conn = sqlite3.connect(str(temp_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()
            conn.close()
            assert row is not None
            assert row["channel"] == "WWV_10000"
            assert row["station"] == "WWV"
            assert row["clock_offset_ms"] == pytest.approx(-2.14)
            assert row["gpsdo_locked"] == 1  # bool → int
        finally:
            writer.close()

    def test_none_stored_as_null(self, temp_dir, temp_db, sample_l2_measurement):
        """NULL preserves None vs NaN — the architectural-win we wanted.

        Uses delay_spread_ms which is optional in the schema (the
        DUT1=NaN poisoning bug in fusion would not have happened if
        SQLite NULL semantics had been preserved end-to-end).
        """
        m = dict(sample_l2_measurement)
        m["delay_spread_ms"] = None
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurement(m)
            conn = sqlite3.connect(str(temp_db))
            row = conn.execute(
                f"SELECT delay_spread_ms FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()
            conn.close()
            assert row[0] is None  # NULL came back as Python None — not NaN
        finally:
            writer.close()

    def test_numpy_scalars_coerced(self, temp_dir, temp_db, sample_l2_measurement):
        """numpy scalars survive the writer (.item() conversion)."""
        m = dict(sample_l2_measurement)
        m["clock_offset_ms"] = np.float64(-2.14)
        m["rtp_timestamp"] = np.int64(123456789)
        m["gpsdo_locked"] = np.bool_(True)
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurement(m)
            conn = sqlite3.connect(str(temp_db))
            row = conn.execute(
                f"SELECT clock_offset_ms, rtp_timestamp, gpsdo_locked "
                f"FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()
            conn.close()
            assert row[0] == pytest.approx(-2.14)
            assert row[1] == 123456789
            assert row[2] == 1
        finally:
            writer.close()

    def test_validation_rejects_bad_value(self, temp_dir, temp_db, sample_l2_measurement):
        """Bad enum should raise the same way as the HDF5 writer."""
        m = dict(sample_l2_measurement)
        m["station"] = "INVALID_STATION"
        writer = _make_writer(temp_dir, temp_db)
        try:
            with pytest.raises(ValueError):
                writer.write_measurement(m)
        finally:
            writer.close()

    def test_duplicate_timestamps_append_both_rows(self, temp_dir, temp_db, sample_l2_measurement):
        """Same (channel, timestamp_utc) → BOTH rows are kept (append-only,
        no upsert). HDF5 is append-only and several products emit multiple
        rows per timestamp_utc (different stations or tones within the
        same processing second). The SQLite writer mirrors that — both
        the original PK approach and a TIMESTAMP-as-UNIQUE constraint
        would silently lose data."""
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurement(sample_l2_measurement)
            second = dict(sample_l2_measurement)
            second["clock_offset_ms"] = 99.0
            writer.write_measurement(second)
            conn = sqlite3.connect(str(temp_db))
            row_count = conn.execute(
                f"SELECT COUNT(*) FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()[0]
            values = conn.execute(
                f"SELECT clock_offset_ms FROM {writer.table} "
                f"WHERE channel = ? ORDER BY ROWID",
                (writer.channel,),
            ).fetchall()
            conn.close()
            assert row_count == 2  # both rows kept
            assert values[0][0] == pytest.approx(-2.14)
            assert values[1][0] == pytest.approx(99.0)
        finally:
            writer.close()


# ---------------------------------------------------------------------
# Batch writes
# ---------------------------------------------------------------------


class TestBatchWrite:
    def _make_batch(self, base, n):
        out = []
        for i in range(n):
            m = dict(base)
            # New timestamp each row so the PK doesn't collide.
            m["timestamp_utc"] = f"2026-05-15T17:{i:02d}:00Z"
            m["minute_boundary_utc"] = 1778857200 + i * 60
            out.append(m)
        return out

    def test_batch_round_trip(self, temp_dir, temp_db, sample_l2_measurement):
        writer = _make_writer(temp_dir, temp_db)
        try:
            batch = self._make_batch(sample_l2_measurement, 10)
            writer.write_measurements_batch(batch)
            conn = sqlite3.connect(str(temp_db))
            n = conn.execute(
                f"SELECT COUNT(*) FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()[0]
            conn.close()
            assert n == 10
            assert writer._measurement_count == 10
        finally:
            writer.close()

    def test_batch_validation_failure_rolls_back(self, temp_dir, temp_db, sample_l2_measurement):
        """A bad row in the batch should leave the table empty (we
        validate everything before any insert)."""
        writer = _make_writer(temp_dir, temp_db)
        try:
            batch = self._make_batch(sample_l2_measurement, 5)
            batch[3]["station"] = "INVALID_STATION"
            with pytest.raises(ValueError):
                writer.write_measurements_batch(batch)
            conn = sqlite3.connect(str(temp_db))
            n = conn.execute(
                f"SELECT COUNT(*) FROM {writer.table} WHERE channel = ?",
                (writer.channel,),
            ).fetchone()[0]
            conn.close()
            assert n == 0
        finally:
            writer.close()

    def test_empty_batch_is_noop(self, temp_dir, temp_db):
        writer = _make_writer(temp_dir, temp_db)
        try:
            writer.write_measurements_batch([])  # no error
            assert writer._measurement_count == 0
        finally:
            writer.close()


# ---------------------------------------------------------------------
# Multi-channel storage in one DB
# ---------------------------------------------------------------------


class TestMultiChannel:
    def test_two_writers_share_one_table(self, temp_dir, temp_db, sample_l2_measurement):
        """Two writers for different channels write to the SAME table;
        the channel column distinguishes them."""
        w_wwv = _make_writer(temp_dir, temp_db, channel="WWV_10000")
        w_chu = _make_writer(temp_dir, temp_db, channel="CHU_7850")
        try:
            wwv_meas = dict(sample_l2_measurement)
            chu_meas = dict(sample_l2_measurement)
            chu_meas["station"] = "CHU"
            chu_meas["frequency_mhz"] = 7.85
            chu_meas["clock_offset_ms"] = 0.5
            w_wwv.write_measurement(wwv_meas)
            w_chu.write_measurement(chu_meas)

            conn = sqlite3.connect(str(temp_db))
            wwv_row = conn.execute(
                f"SELECT clock_offset_ms FROM {w_wwv.table} WHERE channel = 'WWV_10000'"
            ).fetchone()
            chu_row = conn.execute(
                f"SELECT clock_offset_ms FROM {w_chu.table} WHERE channel = 'CHU_7850'"
            ).fetchone()
            conn.close()
            assert wwv_row[0] == pytest.approx(-2.14)
            assert chu_row[0] == pytest.approx(0.5)
        finally:
            w_wwv.close()
            w_chu.close()


# ---------------------------------------------------------------------
# Smoke / health-check
# ---------------------------------------------------------------------


class TestSmoke:
    def test_write_test_measurement_succeeds(self, temp_dir, temp_db):
        writer = _make_writer(temp_dir, temp_db)
        try:
            assert writer.write_test_measurement() is True
            assert writer._measurement_count == 1
        finally:
            writer.close()

    def test_context_manager_closes_connection(self, temp_dir, temp_db):
        with _make_writer(temp_dir, temp_db) as writer:
            assert writer._conn is not None
            writer.write_test_measurement()
        assert writer._conn is None  # close() called on __exit__
