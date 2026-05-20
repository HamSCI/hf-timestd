"""SQLite data product reader — schema-driven, API-compatible with DataProductReader.

Phase 2 of the HDF5 → SQLite migration (see
``docs/HDF5-TO-SQLITE-MIGRATION.md``). This is the read-side
counterpart to ``SqliteDataProductWriter``: a reader whose
``read_time_range`` returns the same list-of-dicts shape that the HDF5
``DataProductReader`` returns, so consumers can switch backends without
code changes — typically via ``make_data_product_reader`` (below),
selected by the ``[storage] read_sqlite`` config knob.

**Long-lived connection.** The migration was motivated by the h5py
per-cycle reader leak: Fusion builds a fresh ``DataProductReader`` every
~8 s, each of which opens/closes an HDF5 file and accumulates internal
h5py state. ``SqliteDataProductReader`` holds one read-only connection
for its lifetime. Opening/closing a SQLite connection is also cheap and
leak-free — so a per-cycle usage pattern is safe too — but a consumer
that keeps the reader alive across cycles gets the architectural fix
for free.

**API scope.** Only ``read_time_range`` is implemented: it is the sole
method any consumer of ``DataProductReader`` actually calls. The HDF5
reader's ``read_file_metadata`` / ``get_quality_summary`` /
``list_available_dates`` have no callers in the codebase and are not
mirrored here — add them if a need appears.

**Two intentional semantic differences from the HDF5 reader:**

- *NULL vs fill.* SQLite returns Python ``None`` for a missing optional
  field. The HDF5 writer substitutes a type-default fill (``0`` / ``""``
  / ``False`` / ``NaN``) because HDF5 cannot store NULL — that fill is
  exactly what poisoned the CHU FSK / DUT1 path (commit f7ec934). The
  SQLite reader preserves ``None`` so ``if x is not None`` works
  correctly. A consumer switching backends should expect ``None`` where
  it used to see ``NaN`` / ``0`` / ``""``.
- *Ordering.* Rows are returned ordered by ``timestamp_utc`` (then
  ROWID for rows sharing a timestamp). The HDF5 reader returns
  storage/append order, which is also chronological in practice — so
  this is parity, made explicit.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from hf_timestd.schemas import get_schema
from hf_timestd.io.hdf5_reader import DataProductReader
from hf_timestd.io.sqlite_writer import DEFAULT_DB_PATH, _table_name

logger = logging.getLogger(__name__)


# Quality-grade ordering — identical to DataProductReader.read_time_range
# so the grade filter behaves the same across backends.
_GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}

# The SQL pre-filter widens the requested window by this many seconds on
# each side. The widened BETWEEN exists only to narrow the table scan
# via the (channel, timestamp_utc) index — the exact bounds are then
# enforced in Python with parsed-datetime comparison. The slack absorbs
# any lexical-vs-chronological mismatch from inconsistent ISO-8601
# suffixes ('Z' vs '+00:00'): a suffix difference only perturbs the sort
# within the same microsecond, far below 2 s.
_SQL_WINDOW_SLACK_SEC = 2.0


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'.

    Python 3.10's ``datetime.fromisoformat`` does not accept 'Z'; the
    HDF5 reader works around it the same way.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class SqliteDataProductReader:
    """SQLite-backed data product reader with the same ``read_time_range``
    API as ``DataProductReader``.

    Constructor accepts the same arguments as ``DataProductReader`` plus
    an optional ``db_path`` override. ``data_dir`` and ``use_registry``
    are retained for drop-in API parity but are not used — SQLite has no
    per-product directory layout; all products live in one database,
    one table each, distinguished by a ``channel`` column.

    A missing database file or a missing table is tolerated: reads
    return an empty list (mirroring the HDF5 reader, which skips absent
    files). This is the expected state for a product that is not being
    dual-written yet.
    """

    def __init__(
        self,
        data_dir: Path,
        product_level: str,
        product_name: str,
        channel: str,
        version: str = "v1",
        use_registry: bool = True,
        db_path: Optional[Path] = None,
    ):
        """
        Initialize the SQLite data product reader.

        Args:
            data_dir: Accepted for API parity with ``DataProductReader``;
                not used to locate data (the DB lives at ``db_path``).
            product_level: Data product level (L1, L2, L3).
            product_name: Product name (e.g. ``timing_measurements``).
            channel: Channel name (e.g. ``WWV_10000``) — matched against
                the ``channel`` column.
            version: Schema version (default: ``v1``).
            use_registry: Accepted for API parity; not used.
            db_path: Override the default SQLite path
                ``/var/lib/timestd/phase2/timestd.db``. Useful for tests.
        """
        self.data_dir = Path(data_dir)
        self.product_level = product_level
        self.product_name = product_name
        self.channel = channel
        self.version = version

        self.schema = get_schema(product_level, product_name, version)
        self.table = _table_name(product_level, product_name)
        self._field_types = {f["name"]: f.get("type") for f in self.schema["fields"]}

        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

        self._conn: Optional[sqlite3.Connection] = None
        self._table_present = False
        if self.db_path.exists():
            # Read-only URI connection: never creates the file, never
            # writes. WAL mode (set by the writers) lets us read
            # concurrently while a writer holds the write lock.
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=5.0,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._table_present = self._check_table()
        else:
            logger.debug(
                f"SQLite DB {self.db_path} does not exist yet — "
                f"{self.table} reads will return no rows"
            )

        logger.info(
            f"Initialized {product_level} {product_name} SQLite reader for "
            f"{channel} (schema v{self.schema['schema_version']}, "
            f"table={self.table}, present={self._table_present}, "
            f"db={self.db_path})"
        )

    def _check_table(self) -> bool:
        """Return True if this product's table exists in the database."""
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (self.table,),
        ).fetchone()
        if row is None:
            logger.debug(
                f"Table {self.table} not present in {self.db_path} — reads "
                f"will return no rows (product not dual-written yet)"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Row → measurement-dict conversion
    # ------------------------------------------------------------------

    def _row_to_measurement(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a ``SELECT *`` row to a measurement dict.

        - Drops the ``channel`` column: the HDF5 reader's dicts never
          carry it (channel is implicit in the file path), so omitting
          it keeps the two backends drop-in compatible.
        - Coerces ``boolean``-typed columns back from SQLite's 0/1
          INTEGER storage to Python ``bool``, matching HDF5.
        - Leaves SQLite NULL as Python ``None`` (see module docstring).
        """
        m: Dict[str, Any] = {}
        for key in row.keys():
            if key == "channel":
                continue
            value = row[key]
            if value is not None and self._field_types.get(key) == "boolean":
                value = bool(value)
            m[key] = value
        return m

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def read_time_range(
        self,
        start: str,
        end: str,
        min_quality_grade: Optional[str] = None,
        quality_flags: Optional[List[str]] = None,
        min_confidence: Optional[float] = None,
        station: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Read measurements within ``[start, end]`` with quality filtering.

        Signature and filter semantics mirror
        ``DataProductReader.read_time_range``. ``start`` / ``end`` are
        ISO-8601 strings (a trailing 'Z' is accepted). Returns a list of
        measurement dicts ordered by timestamp; both bounds inclusive.
        """
        if self._conn is None or not self._table_present:
            return []
        if "timestamp_utc" not in self._field_types:
            logger.warning(
                f"{self.table} schema has no timestamp_utc field — "
                f"read_time_range returns no rows"
            )
            return []

        start_dt = _parse_iso(start)
        end_dt = _parse_iso(end)

        # Widened SQL bounds — index-narrowing only; the exact cut is the
        # parsed-datetime comparison below. isoformat() here matches the
        # dominant stored format (a '+00:00' suffix).
        sql_lo = (start_dt - timedelta(seconds=_SQL_WINDOW_SLACK_SEC)).isoformat()
        sql_hi = (end_dt + timedelta(seconds=_SQL_WINDOW_SLACK_SEC)).isoformat()

        try:
            rows = self._conn.execute(
                f"SELECT * FROM {self.table} "
                f"WHERE channel = ? AND timestamp_utc BETWEEN ? AND ? "
                f"ORDER BY timestamp_utc, ROWID",
                (self.channel, sql_lo, sql_hi),
            ).fetchall()
        except sqlite3.Error as e:
            logger.error(f"SQLite read of {self.table} failed: {e}")
            return []

        min_grade_value = (
            _GRADE_ORDER.get(min_quality_grade, 3) if min_quality_grade else 3
        )

        # Collect (ts_dt, measurement) so the final result can be sorted
        # by true parsed time — robust to any suffix inconsistency.
        kept: List[tuple] = []
        for row in rows:
            m = self._row_to_measurement(row)

            timestamp = m.get("timestamp_utc")
            if not timestamp:
                continue
            try:
                ts_dt = _parse_iso(timestamp)
            except (ValueError, TypeError):
                continue
            if ts_dt < start_dt or ts_dt > end_dt:
                continue

            # Quality-grade filter (missing grade treated as 'D').
            if min_quality_grade:
                grade = m.get("quality_grade", "D")
                if _GRADE_ORDER.get(grade, 3) > min_grade_value:
                    continue
            # Quality-flag filter (missing flag treated as 'BAD').
            if quality_flags:
                if m.get("quality_flag", "BAD") not in quality_flags:
                    continue
            # Confidence filter (NULL/missing treated as 0.0).
            if min_confidence is not None:
                if (m.get("confidence") or 0.0) < min_confidence:
                    continue
            # Station filter.
            if station:
                if m.get("station", "") != station:
                    continue

            kept.append((ts_dt, m))

        kept.sort(key=lambda pair: pair[0])
        result = [m for _, m in kept]
        logger.info(
            f"Read {len(result)} measurements from {start} to {end} "
            f"(quality_grade >= {min_quality_grade}, flags={quality_flags})"
        )
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the read-only SQLite connection."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as e:
                logger.warning(f"Error closing SQLite reader connection: {e}")
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


# ---------------------------------------------------------------------
# Factory — picks the read backend from config (mirrors make_data_product_writer)
# ---------------------------------------------------------------------


def make_data_product_reader(
    data_dir: Path,
    product_level: str,
    product_name: str,
    channel: str,
    *,
    version: str = "v1",
    use_registry: bool = True,
    storage_config: Optional[Dict[str, Any]] = None,
):
    """Construct a data product reader based on ``[storage]`` config.

    Returns a ``DataProductReader`` (HDF5) by default, or a
    ``SqliteDataProductReader`` when ``[storage] read_sqlite = true``.
    Both expose the same ``read_time_range`` API, so the caller does not
    need to know which backend it received.

    This is the read-side mirror of ``make_data_product_writer``.
    Keeping the read knob independent of the write knob
    (``read_sqlite`` vs ``write_sqlite``) is what lets the migration
    verify SQLite *writes* for days before any reader trusts them — see
    ``docs/HDF5-TO-SQLITE-MIGRATION.md``, Phase 2.

    Args:
        data_dir / product_level / product_name / channel / version /
            use_registry: same as ``DataProductReader.__init__``.
        storage_config: a dict typically loaded from the ``[storage]``
            section of timestd-config.toml. Recognized keys:
              - ``read_sqlite`` (bool, default False)
              - ``sqlite_path`` (str, default
                ``/var/lib/timestd/phase2/timestd.db``)
            ``None`` is treated as defaults — i.e. HDF5 reads,
            preserving today's behaviour.
    """
    cfg = storage_config or {}
    read_sqlite = bool(cfg.get("read_sqlite", False))

    if read_sqlite:
        sqlite_path = cfg.get("sqlite_path")  # SqliteDataProductReader has its own default
        kwargs: Dict[str, Any] = dict(
            data_dir=data_dir,
            product_level=product_level,
            product_name=product_name,
            channel=channel,
            version=version,
            use_registry=use_registry,
        )
        if sqlite_path is not None:
            kwargs["db_path"] = Path(sqlite_path)
        return SqliteDataProductReader(**kwargs)

    return DataProductReader(
        data_dir=data_dir,
        product_level=product_level,
        product_name=product_name,
        channel=channel,
        version=version,
        use_registry=use_registry,
    )
