"""Unit tests for SqliteDataProductReader and make_data_product_reader.

Phase 2 of the HDF5 → SQLite migration. See
``docs/HDF5-TO-SQLITE-MIGRATION.md`` and
``src/hf_timestd/io/sqlite_reader.py``.

Mirrors the structure of ``test_sqlite_writer.py`` so the read/write
pair can be kept in lockstep. Rows are produced with the real
``SqliteDataProductWriter`` (and, for the cross-backend parity test,
the real ``DualWriter``) so the reader is always exercised against
genuine writer output.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from hf_timestd.io.hdf5_reader import DataProductReader
from hf_timestd.io.sqlite_writer import SqliteDataProductWriter
from hf_timestd.io.sqlite_reader import (
    SqliteDataProductReader,
    make_data_product_reader,
    _parse_iso,
)
from hf_timestd.io import make_data_product_writer


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


# A known-valid L2 timing_measurements row — same shape as
# test_sqlite_writer.py's sample so the two suites compare cleanly.
BASE_MEASUREMENT = {
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
    "traceability_chain": "GPSDO -> UTC(GPS) -> UTC(NIST)",
    "processing_version": "3.2.0",
    "processed_at": "2026-05-15T17:01:00Z",
    "calibration_date": "2026-05-01T00:00:00Z",
    "gpsdo_locked": True,
}

# Required fields — always populated, never NULL on either backend, so
# they are safe to compare row-by-row across HDF5 and SQLite.
REQUIRED_FIELDS = [
    "timestamp_utc", "minute_boundary_utc", "rtp_timestamp", "station",
    "frequency_mhz", "discrimination_method", "discrimination_confidence",
    "tone_detected", "raw_arrival_time_ms", "clock_offset_ms",
    "quality_grade", "confidence", "quality_flag", "gpsdo_locked",
]

LEVEL = "L2"
PRODUCT = "timing_measurements"
CHANNEL = "WWV_10000"


def _row(ts, **overrides):
    """Build a measurement at timestamp ``ts`` with field overrides."""
    m = dict(BASE_MEASUREMENT)
    m["timestamp_utc"] = ts
    m.update(overrides)
    return m


def _write_rows(temp_dir, temp_db, rows, channel=CHANNEL):
    """Write ``rows`` to SQLite with the real writer, then close it."""
    writer = SqliteDataProductWriter(
        output_dir=temp_dir,
        product_level=LEVEL,
        product_name=PRODUCT,
        channel=channel,
        db_path=temp_db,
    )
    try:
        writer.write_measurements_batch(rows)
    finally:
        writer.close()


def _make_reader(temp_dir, temp_db, channel=CHANNEL):
    return SqliteDataProductReader(
        data_dir=temp_dir,
        product_level=LEVEL,
        product_name=PRODUCT,
        channel=channel,
        db_path=temp_db,
    )


# A window comfortably bracketing the BASE_MEASUREMENT timestamp.
WIN_START = "2026-05-15T00:00:00Z"
WIN_END = "2026-05-15T23:59:59Z"


# ---------------------------------------------------------------------
# Helper-fn unit tests
# ---------------------------------------------------------------------


class TestHelpers:
    def test_parse_iso_accepts_z_suffix(self):
        a = _parse_iso("2026-05-15T17:00:00Z")
        b = _parse_iso("2026-05-15T17:00:00+00:00")
        assert a == b

    def test_parse_iso_keeps_microseconds(self):
        dt = _parse_iso("2026-05-15T17:00:00.141045+00:00")
        assert dt.microsecond == 141045


# ---------------------------------------------------------------------
# Construction + tolerance of missing DB / table
# ---------------------------------------------------------------------


class TestConstruction:
    def test_create_reader(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [_row("2026-05-15T17:00:00Z")])
        reader = _make_reader(temp_dir, temp_db)
        try:
            assert reader.product_level == LEVEL
            assert reader.product_name == PRODUCT
            assert reader.channel == CHANNEL
            assert reader.table == "L2_timing_measurements"
            assert reader.db_path == temp_db
            assert reader._table_present is True
        finally:
            reader.close()

    def test_missing_db_returns_empty(self, temp_dir, temp_db):
        """No DB file on disk — reads return [] instead of raising."""
        assert not temp_db.exists()
        reader = _make_reader(temp_dir, temp_db)
        try:
            assert reader._conn is None
            assert reader.read_time_range(WIN_START, WIN_END) == []
        finally:
            reader.close()

    def test_missing_table_returns_empty(self, temp_dir, temp_db):
        """DB exists but this product's table was never written."""
        sqlite3.connect(str(temp_db)).close()  # create an empty DB file
        reader = _make_reader(temp_dir, temp_db)
        try:
            assert reader._conn is not None
            assert reader._table_present is False
            assert reader.read_time_range(WIN_START, WIN_END) == []
        finally:
            reader.close()


# ---------------------------------------------------------------------
# read_time_range — round trip and value fidelity
# ---------------------------------------------------------------------


class TestReadTimeRange:
    def test_round_trip(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [_row("2026-05-15T17:00:00Z")])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(WIN_START, WIN_END)
            assert len(rows) == 1
            assert rows[0]["timestamp_utc"] == "2026-05-15T17:00:00Z"
            assert rows[0]["station"] == "WWV"
            assert rows[0]["clock_offset_ms"] == pytest.approx(-2.14)
        finally:
            reader.close()

    def test_channel_column_excluded(self, temp_dir, temp_db):
        """The HDF5 reader's dicts never carry 'channel'; neither does
        this one, so consumers see the same keys."""
        _write_rows(temp_dir, temp_db, [_row("2026-05-15T17:00:00Z")])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(WIN_START, WIN_END)
            assert "channel" not in rows[0]
        finally:
            reader.close()

    def test_boolean_coercion(self, temp_dir, temp_db):
        """SQLite stores booleans as 0/1 INTEGER; the reader coerces
        them back to Python bool to match HDF5."""
        _write_rows(
            temp_dir, temp_db,
            [_row("2026-05-15T17:00:00Z", gpsdo_locked=False, tone_detected=True)],
        )
        reader = _make_reader(temp_dir, temp_db)
        try:
            row = reader.read_time_range(WIN_START, WIN_END)[0]
            assert row["gpsdo_locked"] is False
            assert row["tone_detected"] is True
        finally:
            reader.close()

    def test_null_preserved_as_none(self, temp_dir, temp_db):
        """An optional field written as None reads back as None — not
        the NaN fill the HDF5 path would have substituted."""
        _write_rows(
            temp_dir, temp_db,
            [_row("2026-05-15T17:00:00Z", delay_spread_ms=None)],
        )
        reader = _make_reader(temp_dir, temp_db)
        try:
            row = reader.read_time_range(WIN_START, WIN_END)[0]
            assert row["delay_spread_ms"] is None
        finally:
            reader.close()

    def test_time_window_excludes_outside(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z", clock_offset_ms=1.0),
            _row("2026-05-15T17:00:00Z", clock_offset_ms=2.0),
            _row("2026-05-15T18:00:00Z", clock_offset_ms=3.0),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(
                "2026-05-15T16:30:00Z", "2026-05-15T17:30:00Z"
            )
            assert len(rows) == 1
            assert rows[0]["clock_offset_ms"] == pytest.approx(2.0)
        finally:
            reader.close()

    def test_window_bounds_inclusive(self, temp_dir, temp_db):
        """A row exactly on each bound is included."""
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z"),
            _row("2026-05-15T18:00:00Z"),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(
                "2026-05-15T16:00:00Z", "2026-05-15T18:00:00Z"
            )
            assert len(rows) == 2
        finally:
            reader.close()

    def test_ordering_chronological(self, temp_dir, temp_db):
        """Rows written out of order come back ascending by timestamp."""
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T18:00:00Z"),
            _row("2026-05-15T16:00:00Z"),
            _row("2026-05-15T17:00:00Z"),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(WIN_START, WIN_END)
            ts = [r["timestamp_utc"] for r in rows]
            assert ts == sorted(ts)
        finally:
            reader.close()


# ---------------------------------------------------------------------
# read_time_range — quality filtering (parity with DataProductReader)
# ---------------------------------------------------------------------


class TestQualityFilters:
    def test_min_quality_grade(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z", quality_grade="A"),
            _row("2026-05-15T17:00:00Z", quality_grade="B"),
            _row("2026-05-15T18:00:00Z", quality_grade="C"),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(
                WIN_START, WIN_END, min_quality_grade="B"
            )
            assert {r["quality_grade"] for r in rows} == {"A", "B"}
        finally:
            reader.close()

    def test_quality_flags(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z", quality_flag="GOOD"),
            _row("2026-05-15T17:00:00Z", quality_flag="MARGINAL"),
            _row("2026-05-15T18:00:00Z", quality_flag="BAD"),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(
                WIN_START, WIN_END, quality_flags=["GOOD", "MARGINAL"]
            )
            assert {r["quality_flag"] for r in rows} == {"GOOD", "MARGINAL"}
        finally:
            reader.close()

    def test_min_confidence(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z", confidence=0.3),
            _row("2026-05-15T17:00:00Z", confidence=0.9),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(
                WIN_START, WIN_END, min_confidence=0.5
            )
            assert len(rows) == 1
            assert rows[0]["confidence"] == pytest.approx(0.9)
        finally:
            reader.close()

    def test_station_filter(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [
            _row("2026-05-15T16:00:00Z", station="WWV"),
            _row("2026-05-15T17:00:00Z", station="CHU"),
        ])
        reader = _make_reader(temp_dir, temp_db)
        try:
            rows = reader.read_time_range(WIN_START, WIN_END, station="WWV")
            assert len(rows) == 1
            assert rows[0]["station"] == "WWV"
        finally:
            reader.close()


# ---------------------------------------------------------------------
# Multi-channel isolation
# ---------------------------------------------------------------------


class TestMultiChannel:
    def test_reader_isolates_channel(self, temp_dir, temp_db):
        """Two channels share one table; a reader sees only its own."""
        _write_rows(
            temp_dir, temp_db,
            [_row("2026-05-15T17:00:00Z", clock_offset_ms=-2.14)],
            channel="WWV_10000",
        )
        _write_rows(
            temp_dir, temp_db,
            [_row("2026-05-15T17:00:00Z", station="CHU", clock_offset_ms=0.5)],
            channel="CHU_7850",
        )
        wwv = _make_reader(temp_dir, temp_db, channel="WWV_10000")
        chu = _make_reader(temp_dir, temp_db, channel="CHU_7850")
        try:
            wwv_rows = wwv.read_time_range(WIN_START, WIN_END)
            chu_rows = chu.read_time_range(WIN_START, WIN_END)
            assert len(wwv_rows) == 1 and len(chu_rows) == 1
            assert wwv_rows[0]["clock_offset_ms"] == pytest.approx(-2.14)
            assert chu_rows[0]["clock_offset_ms"] == pytest.approx(0.5)
        finally:
            wwv.close()
            chu.close()


# ---------------------------------------------------------------------
# make_data_product_reader factory
# ---------------------------------------------------------------------


class TestFactory:
    def _kwargs(self, temp_dir):
        return dict(
            data_dir=temp_dir,
            product_level=LEVEL,
            product_name=PRODUCT,
            channel=CHANNEL,
        )

    def test_defaults_to_hdf5(self, temp_dir):
        """No config, empty config, and explicit read_sqlite=false all
        yield the HDF5 reader — today's behaviour is preserved."""
        for cfg in (None, {}, {"read_sqlite": False}):
            reader = make_data_product_reader(
                **self._kwargs(temp_dir), storage_config=cfg
            )
            assert isinstance(reader, DataProductReader)
            assert not isinstance(reader, SqliteDataProductReader)

    def test_read_sqlite_selects_sqlite_backend(self, temp_dir, temp_db):
        reader = make_data_product_reader(
            **self._kwargs(temp_dir),
            storage_config={"read_sqlite": True, "sqlite_path": str(temp_db)},
        )
        try:
            assert isinstance(reader, SqliteDataProductReader)
            assert reader.db_path == temp_db
        finally:
            reader.close()


# ---------------------------------------------------------------------
# Cross-backend parity — DualWriter output read by both readers
# ---------------------------------------------------------------------


class TestCrossBackendParity:
    def test_dualwriter_rows_read_identically(self, temp_dir, temp_db):
        """Write the same rows to HDF5 and SQLite via DualWriter, then
        confirm the HDF5 and SQLite readers return the same required-
        field values row-for-row. This is the unit-test analogue of the
        live verify_sqlite_parity.py check."""
        rows = [
            _row(
                f"2026-05-15T17:0{i}:00Z",
                clock_offset_ms=-2.0 + i * 0.5,
                confidence=0.7 + i * 0.02,
                minute_boundary_utc=1778857200 + i * 60,
            )
            for i in range(5)
        ]
        storage = {
            "write_hdf5": True,
            "write_sqlite": True,
            "sqlite_path": str(temp_db),
        }
        writer = make_data_product_writer(
            output_dir=temp_dir,
            product_level=LEVEL,
            product_name=PRODUCT,
            channel=CHANNEL,
            storage_config=storage,
        )
        try:
            writer.write_measurements_batch(rows)
        finally:
            writer.close()

        hdf5_reader = make_data_product_reader(
            data_dir=temp_dir, product_level=LEVEL, product_name=PRODUCT,
            channel=CHANNEL, storage_config={"read_sqlite": False},
        )
        sqlite_reader = make_data_product_reader(
            data_dir=temp_dir, product_level=LEVEL, product_name=PRODUCT,
            channel=CHANNEL,
            storage_config={"read_sqlite": True, "sqlite_path": str(temp_db)},
        )
        try:
            h5_rows = hdf5_reader.read_time_range(WIN_START, WIN_END)
            sql_rows = sqlite_reader.read_time_range(WIN_START, WIN_END)
        finally:
            if hasattr(hdf5_reader, "close"):
                pass  # HDF5 reader has no persistent handle
            sqlite_reader.close()

        assert len(h5_rows) == len(sql_rows) == 5

        h5_by_ts = {r["timestamp_utc"]: r for r in h5_rows}
        sql_by_ts = {r["timestamp_utc"]: r for r in sql_rows}
        assert set(h5_by_ts) == set(sql_by_ts)

        for ts, h5_row in h5_by_ts.items():
            sql_row = sql_by_ts[ts]
            for field in REQUIRED_FIELDS:
                h5_v, sql_v = h5_row.get(field), sql_row.get(field)
                if isinstance(h5_v, float) or isinstance(sql_v, float):
                    assert float(h5_v) == pytest.approx(float(sql_v)), (
                        f"{field} @ {ts}: h5={h5_v!r} sql={sql_v!r}"
                    )
                else:
                    assert h5_v == sql_v, (
                        f"{field} @ {ts}: h5={h5_v!r} sql={sql_v!r}"
                    )


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


class TestLifecycle:
    def test_context_manager_closes_connection(self, temp_dir, temp_db):
        _write_rows(temp_dir, temp_db, [_row("2026-05-15T17:00:00Z")])
        with _make_reader(temp_dir, temp_db) as reader:
            assert reader._conn is not None
            assert len(reader.read_time_range(WIN_START, WIN_END)) == 1
        assert reader._conn is None  # close() called on __exit__
