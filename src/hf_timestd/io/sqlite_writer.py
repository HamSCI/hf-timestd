"""SQLite data product writer — schema-driven, API-compatible with DataProductWriter.

Phase 1 of the HDF5 → SQLite migration. See
``docs/HDF5-TO-SQLITE-MIGRATION.md`` for the rationale and overall
plan. This module ships the writer alongside the existing
``DataProductWriter`` (which still owns the HDF5 path); a canary
producer can dual-write to both backends for verification before any
read-side switch.

Design decisions:

- **One database file for all of hf-timestd**: default path
  ``/var/lib/timestd/phase2/timestd.db``. One table per data product
  (``L2_timing_measurements``, ``L1_metrology_measurements``, …), with
  a ``channel`` column to distinguish per-channel rows. Mirrors the
  layout the schemas would naturally produce.

- **Long-lived connection per writer**: opened in ``__init__`` and
  held for the writer's lifetime. This is the architectural fix for
  the h5py leak that motivated this migration: no per-cycle
  open/close, so no internal-state accumulation in the database
  library.

- **WAL mode + synchronous=NORMAL**: WAL allows concurrent readers
  while a writer holds the write lock; NORMAL is acceptable for the
  metrology workload because data can be re-derived from raw_buffer
  if a crash truncates the most recent batch.

- **Schema as the source of truth**: ``CREATE TABLE IF NOT EXISTS``
  is generated at construction time from the same JSON schema used by
  the HDF5 writer. Validation goes through identical code
  (``_validate_field``) so dual-write either succeeds in both backends
  or fails the same way in both.

- **NULL preserves None vs NaN**: unlike the HDF5 path (where None →
  ``np.nan`` for float fields, which bit us in the CHU FSK / DUT1
  investigation — commit f7ec934), SQLite stores actual ``NULL``.
  Downstream code that uses ``if x is not None`` works correctly
  without needing a separate NaN guard.

Migration phases beyond Phase 1: see the design doc.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from hf_timestd.schemas import get_schema
from hf_timestd.io.uncertainty import ISOGUMCalculator

logger = logging.getLogger(__name__)


# Default SQLite path. Override via SqliteDataProductWriter(..., db_path=...)
# or via the [storage] sqlite_path config knob (see Phase 1 config wiring).
DEFAULT_DB_PATH = Path("/var/lib/timestd/phase2/timestd.db")


def _sqlite_type_for_field(field: Dict[str, Any]) -> str:
    """Map a JSON-schema field-type to a SQLite column type."""
    t = field.get("type")
    if t == "float":
        return "REAL"
    if t == "integer":
        return "INTEGER"
    if t == "string":
        return "TEXT"
    if t == "boolean":
        return "INTEGER"  # SQLite has no bool; 0/1 in INTEGER
    # Unknown / unsupported — store as TEXT for safety.
    return "TEXT"


def _table_name(product_level: str, product_name: str) -> str:
    """Canonical table name: e.g. 'L2_timing_measurements'.

    SQLite identifier — keep it short and stable; no spaces or
    punctuation. Schema-level fields like ``product_name`` are arbitrary
    strings in the JSON schema, but our naming convention is snake_case.
    """
    return f"{product_level}_{product_name}"


class SqliteDataProductWriter:
    """SQLite-backed data product writer with the same API as
    ``DataProductWriter``.

    Phase 1 of the HDF5 → SQLite migration: producers can opt into
    dual-write (both this and the HDF5 writer) before any read-side
    switch.

    Constructor accepts the same arguments as ``DataProductWriter``
    plus an optional ``db_path`` override; ``output_dir`` is retained
    for API compatibility but is only used to materialise the directory
    holding the default database (sibling to the HDF5 channel dir).
    """

    def __init__(
        self,
        output_dir: Path,
        product_level: str,
        product_name: str,
        channel: str,
        version: str = "v1",
        processing_version: str = "3.2.0",
        station_metadata: Optional[Dict[str, Any]] = None,
        db_path: Optional[Path] = None,
    ):
        """
        Initialize SQLite data product writer.

        Args:
            output_dir: Output directory (kept for API parity; not used
                directly by SQLite — the DB lives at ``db_path``).
            product_level: Data product level (L1, L2, L3).
            product_name: Product name (e.g., ``timing_measurements``).
            channel: Channel name (e.g., ``WWV_10000``).
            version: Schema version (default: ``v1``).
            processing_version: Software version (recorded as a row column).
            station_metadata: Optional station metadata (currently unused
                here — SQLite doesn't carry the per-file metadata that
                HDF5 attached, but the metadata table could be added in
                Phase 2 if needed).
            db_path: Override the default SQLite path
                ``/var/lib/timestd/phase2/timestd.db``. Useful for tests.
        """
        self.output_dir = Path(output_dir)
        # Materialise output_dir for API parity, even though we don't
        # write per-product files there. Producers that pass an
        # output_dir often expect it to exist before they call us.
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.product_level = product_level
        self.product_name = product_name
        self.channel = channel
        self.version = version
        self.processing_version = processing_version
        self.station_metadata = station_metadata or {}

        self.schema = get_schema(product_level, product_name, version)
        self.table = _table_name(product_level, product_name)

        # Default DB path: shared single-file across all of hf-timestd.
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Long-lived connection. isolation_level=None puts us in
        # autocommit mode so each insert is its own transaction unless
        # we explicitly wrap in BEGIN/COMMIT (done in write_measurements_batch).
        # check_same_thread=False permits the calling service to share
        # the writer across threads if it wants to; SQLite's own locking
        # handles serialization.
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            timeout=5.0,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Foreign keys not used yet, but turn them on so future schema
        # additions (e.g. station metadata) can rely on enforcement.
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._measurement_count = 0
        self._ensure_table()
        logger.info(
            f"Initialized {product_level} {product_name} SQLite writer for {channel} "
            f"(schema v{self.schema['schema_version']}, table={self.table}, "
            f"db={self.db_path})"
        )

    # ------------------------------------------------------------------
    # Schema → DDL
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the table for this data product if it doesn't exist.

        Schema is generated from the JSON field list. ``channel`` is
        added as a leading column (the schema doesn't include it
        because in the HDF5 layout the channel is implicit from the
        file path; in SQLite we need it explicit).
        """
        cols: List[str] = ["channel TEXT NOT NULL"]
        for field in self.schema["fields"]:
            name = field["name"]
            sql_type = _sqlite_type_for_field(field)
            required = field.get("required", False)
            null_clause = " NOT NULL" if required else ""
            cols.append(f"{name} {sql_type}{null_clause}")
        # Primary key: (channel, timestamp_utc) when timestamp_utc is in
        # the schema. Fall back to no explicit PK otherwise (rare).
        field_names = {f["name"] for f in self.schema["fields"]}
        if "timestamp_utc" in field_names:
            pk = "PRIMARY KEY (channel, timestamp_utc)"
            cols.append(pk)

        ddl = f"CREATE TABLE IF NOT EXISTS {self.table} (\n    " + ",\n    ".join(cols) + "\n)"
        self._conn.execute(ddl)
        # Index on (channel, timestamp_utc) is implicit via the PK,
        # but add an explicit one if no PK was set so the time-range
        # query is still cheap.
        if "timestamp_utc" in field_names:
            # PK already covers it
            pass
        elif "minute_boundary_utc" in field_names:
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{self.table}_chan_min "
                f"ON {self.table} (channel, minute_boundary_utc)"
            )

    # ------------------------------------------------------------------
    # Validation (identical to DataProductWriter)
    # ------------------------------------------------------------------

    def _validate_field(self, field_schema: Dict[str, Any], value: Any, field_name: str) -> None:
        """Validate a single field against its schema. Identical to the
        HDF5 writer's implementation so dual-write either succeeds in
        both backends or fails the same way in both."""
        if field_schema.get("required", False) and value is None:
            raise ValueError(f"Required field '{field_name}' is missing")
        if value is None:
            return

        field_type = field_schema.get("type")
        if field_type == "float":
            if not isinstance(value, (int, float, np.number)):
                raise ValueError(f"Field '{field_name}' must be numeric, got {type(value)}")
            if not field_schema.get("allow_nan", True):
                ISOGUMCalculator.validate_measurement(float(value), field_name)
        elif field_type == "integer":
            if not isinstance(value, (int, np.integer)):
                raise ValueError(f"Field '{field_name}' must be integer, got {type(value)}")
        elif field_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"Field '{field_name}' must be string, got {type(value)}")
            if "enum" in field_schema:
                if value not in field_schema["enum"]:
                    raise ValueError(
                        f"Field '{field_name}' value '{value}' not in allowed values: "
                        f"{field_schema['enum']}"
                    )
        elif field_type == "boolean":
            if not isinstance(value, (bool, np.bool_)):
                raise ValueError(f"Field '{field_name}' must be boolean, got {type(value)}")

        if "valid_range" in field_schema:
            min_val, max_val = field_schema["valid_range"]
            if not (min_val <= value <= max_val):
                raise ValueError(
                    f"Field '{field_name}' value {value} outside valid range "
                    f"[{min_val}, {max_val}]"
                )

    def validate_measurement(self, measurement: Dict[str, Any]) -> None:
        """Validate a measurement against the schema."""
        field_schemas = {field["name"]: field for field in self.schema["fields"]}
        for field_name, value in measurement.items():
            if field_name not in field_schemas:
                logger.warning(f"Unknown field '{field_name}' (not in schema)")
                continue
            self._validate_field(field_schemas[field_name], value, field_name)
        for field in self.schema["fields"]:
            if field.get("required", False) and field["name"] not in measurement:
                raise ValueError(f"Required field '{field['name']}' missing from measurement")

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def _coerce_for_sqlite(self, field: Dict[str, Any], value: Any) -> Any:
        """Convert a Python value to its SQLite-storable form.

        - Boolean → 0/1
        - numpy scalars → native Python types
        - None passes through as None (SQLite NULL)
        - NaN floats stay as NaN (SQLite stores them faithfully in REAL
          columns; downstream code that needs None semantics should
          write None explicitly upstream)
        """
        if value is None:
            return None
        field_type = field.get("type")
        if field_type == "boolean":
            return 1 if value else 0
        if isinstance(value, np.generic):
            return value.item()
        return value

    def _build_insert(self, measurement: Dict[str, Any]) -> tuple:
        """Build (sql, params) tuple for a single insert.

        Uses INSERT OR REPLACE so duplicate (channel, timestamp_utc)
        primary keys overwrite — matches HDF5's "last write wins"
        semantics within a day.
        """
        cols: List[str] = ["channel"]
        params: List[Any] = [self.channel]
        for field in self.schema["fields"]:
            name = field["name"]
            if name in measurement:
                cols.append(name)
                params.append(self._coerce_for_sqlite(field, measurement[name]))
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT OR REPLACE INTO {self.table} ({', '.join(cols)}) VALUES ({placeholders})"
        return sql, params

    def write_measurement(self, measurement: Dict[str, Any]) -> None:
        """Write a single measurement to SQLite (with validation)."""
        self.validate_measurement(measurement)
        sql, params = self._build_insert(measurement)
        self._conn.execute(sql, params)
        self._measurement_count += 1

    def write_measurements_batch(self, measurements: List[Dict[str, Any]]) -> None:
        """Write multiple measurements in one transaction.

        Mirrors the HDF5 writer's batch API. All rows validated before
        any insert (so the batch is all-or-nothing at validation time);
        the SQL transaction then commits atomically.
        """
        if not measurements:
            return
        for m in measurements:
            self.validate_measurement(m)

        # Build all insert statements eagerly so we can use a single
        # executemany() per distinct column set. In practice the column
        # set varies per row (optional fields may be present or absent),
        # so we group by column-tuple and executemany within each group.
        groups: Dict[tuple, List[List[Any]]] = {}
        for m in measurements:
            cols: List[str] = ["channel"]
            params: List[Any] = [self.channel]
            for field in self.schema["fields"]:
                name = field["name"]
                if name in m:
                    cols.append(name)
                    params.append(self._coerce_for_sqlite(field, m[name]))
            key = tuple(cols)
            groups.setdefault(key, []).append(params)

        # Wrap all groups in a single transaction.
        self._conn.execute("BEGIN")
        try:
            for cols, rows in groups.items():
                placeholders = ", ".join("?" for _ in cols)
                sql = (
                    f"INSERT OR REPLACE INTO {self.table} "
                    f"({', '.join(cols)}) VALUES ({placeholders})"
                )
                self._conn.executemany(sql, rows)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._measurement_count += len(measurements)

    # ------------------------------------------------------------------
    # Lifecycle + smoke testing (mirrors HDF5 writer)
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            logger.info(
                f"SQLite writer closing: {self.channel}/{self.table} "
                f"had {self._measurement_count} measurements"
            )
            try:
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error closing SQLite connection: {e}")
            self._conn = None

    def verify_last_write(self) -> bool:
        """Verify the last write succeeded by reading back the last row."""
        if self._conn is None or self._measurement_count == 0:
            return False
        try:
            # Prefer timestamp_utc as the natural ordering column;
            # fall back to ROWID otherwise.
            field_names = {f["name"] for f in self.schema["fields"]}
            order_col = "timestamp_utc" if "timestamp_utc" in field_names else "ROWID"
            cur = self._conn.execute(
                f"SELECT * FROM {self.table} WHERE channel = ? "
                f"ORDER BY {order_col} DESC LIMIT 1",
                (self.channel,),
            )
            row = cur.fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"Write verification failed: {e}")
            return False

    def write_test_measurement(self) -> bool:
        """Write a minimal test measurement and verify it. Used as a
        startup health check."""
        try:
            test_measurement: Dict[str, Any] = {}
            for field in self.schema["fields"]:
                if not field.get("required", False):
                    continue
                name = field["name"]
                field_type = field.get("type")
                if field_type == "string":
                    if name == "timestamp_utc":
                        test_measurement[name] = (
                            datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                    elif "enum" in field:
                        test_measurement[name] = field["enum"][0]
                    else:
                        test_measurement[name] = "TEST"
                elif field_type == "float":
                    test_measurement[name] = 0.0
                elif field_type == "integer":
                    test_measurement[name] = 0
                elif field_type == "boolean":
                    test_measurement[name] = False
            self.write_measurement(test_measurement)
            if not self.verify_last_write():
                logger.error("SQLite test measurement write verification failed")
                return False
            logger.info(f"✅ SQLite test measurement written and verified for {self.channel}")
            return True
        except Exception as e:
            logger.error(f"SQLite test measurement write failed: {e}", exc_info=True)
            return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
