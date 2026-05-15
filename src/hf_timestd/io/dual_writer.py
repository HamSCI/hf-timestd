"""DualWriter — forwards writes to both HDF5 and SQLite backends.

Phase 1 of the HDF5 → SQLite migration. Producers don't need to know
which backend(s) are configured — they construct a writer via
``make_data_product_writer(...)`` and call the same API regardless.

Three configurations are supported (driven by ``[storage]`` config):

- ``write_hdf5 = true,  write_sqlite = false``  → returns ``DataProductWriter`` (today's behaviour)
- ``write_hdf5 = false, write_sqlite = true``   → returns ``SqliteDataProductWriter`` (Phase 3+)
- ``write_hdf5 = true,  write_sqlite = true``   → returns ``DualWriter`` wrapping both (Phase 1+2 canary)

DualWriter validates ONCE (against the schema), then dispatches the
already-validated row to both backends. This guarantees the two stores
agree on whether a row is acceptable — they never see different inputs.

If either backend raises during the actual insert, the exception
propagates. The other backend may have succeeded — that's the same
"partial write" semantics either backend would have alone if it
encountered a transient I/O error mid-batch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .hdf5_writer import DataProductWriter
from .sqlite_writer import SqliteDataProductWriter

logger = logging.getLogger(__name__)


class DualWriter:
    """Forwards ``DataProductWriter`` API calls to both an HDF5 and a
    SQLite backend. Validation runs once (in the SQLite branch — same
    code as the HDF5 branch) before either insert."""

    def __init__(
        self,
        h5_writer: DataProductWriter,
        sqlite_writer: SqliteDataProductWriter,
    ):
        self.h5 = h5_writer
        self.sql = sqlite_writer
        # Expose the schema for callers that inspect it (mirrors the
        # underlying writers' public attribute).
        self.schema = h5_writer.schema
        self.channel = h5_writer.channel
        self.product_level = h5_writer.product_level
        self.product_name = h5_writer.product_name

    # ------------------------------------------------------------------
    # Validation — single source of truth so dual-write is consistent.
    # ------------------------------------------------------------------

    def validate_measurement(self, measurement: Dict[str, Any]) -> None:
        # Both backends share the same _validate_field implementation,
        # so calling one is enough.
        self.sql.validate_measurement(measurement)

    # ------------------------------------------------------------------
    # Write paths — pre-validate once, then dispatch.
    # ------------------------------------------------------------------

    def write_measurement(self, measurement: Dict[str, Any]) -> None:
        # validate first so a bad row is rejected by both backends
        self.validate_measurement(measurement)
        # write_measurement on each backend will re-run validate; that's
        # cheap and keeps the backends usable standalone.
        self.h5.write_measurement(measurement)
        self.sql.write_measurement(measurement)

    def write_measurements_batch(self, measurements: List[Dict[str, Any]]) -> None:
        if not measurements:
            return
        # Validate all rows once before either backend touches the wire.
        for m in measurements:
            self.validate_measurement(m)
        self.h5.write_measurements_batch(measurements)
        self.sql.write_measurements_batch(measurements)

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    def close(self) -> None:
        # Best-effort close of both, even if one raises.
        errors = []
        try:
            self.h5.close()
        except Exception as e:
            errors.append(("h5", e))
        try:
            self.sql.close()
        except Exception as e:
            errors.append(("sqlite", e))
        if errors:
            for backend, e in errors:
                logger.warning(f"DualWriter.close: {backend} backend close failed: {e}")

    def verify_last_write(self) -> bool:
        # Both must agree they saw a write.
        return self.h5.verify_last_write() and self.sql.verify_last_write()

    def write_test_measurement(self) -> bool:
        # Both backends share the schema, so the synthesized test
        # measurement is the same shape. Use the HDF5 path's
        # implementation as the canonical source — then we explicitly
        # write the same row into SQLite to keep both backends in sync.
        # (Can't just call self.h5.write_test_measurement() and have
        # the SQLite side receive that row — that path lives behind
        # write_measurement.)
        try:
            from datetime import datetime, timezone
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
            return self.verify_last_write()
        except Exception as e:
            logger.error(f"DualWriter test measurement failed: {e}", exc_info=True)
            return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


# ---------------------------------------------------------------------
# Factory — picks the right backend(s) from config.
# ---------------------------------------------------------------------


def make_data_product_writer(
    output_dir: Path,
    product_level: str,
    product_name: str,
    channel: str,
    *,
    version: str = "v1",
    processing_version: str = "3.2.0",
    station_metadata: Optional[Dict[str, Any]] = None,
    storage_config: Optional[Dict[str, Any]] = None,
):
    """Construct a data product writer based on ``[storage]`` config.

    Returns one of: ``DataProductWriter``, ``SqliteDataProductWriter``,
    or ``DualWriter`` wrapping both. Callers don't need to know which
    — the returned object exposes the full writer API in all three
    cases.

    Args:
        output_dir / product_level / product_name / channel / version /
            processing_version / station_metadata: same as
            ``DataProductWriter.__init__``.
        storage_config: a dict typically loaded from the
            ``[storage]`` section of timestd-config.toml. Recognized
            keys:
              - ``write_hdf5`` (bool, default True)
              - ``write_sqlite`` (bool, default False)
              - ``sqlite_path`` (str, default
                ``/var/lib/timestd/phase2/timestd.db``)
            ``None`` is treated as defaults — i.e. HDF5-only,
            preserving today's behaviour.
    """
    cfg = storage_config or {}
    write_hdf5 = bool(cfg.get("write_hdf5", True))
    write_sqlite = bool(cfg.get("write_sqlite", False))
    sqlite_path = cfg.get("sqlite_path")  # SqliteDataProductWriter has its own default

    if not write_hdf5 and not write_sqlite:
        # Defensive: refuse a config that disables both backends. The
        # service can't make progress without somewhere to write.
        raise ValueError(
            "at least one of [storage] write_hdf5 / write_sqlite must be true"
        )

    h5_writer = None
    sql_writer = None
    if write_hdf5:
        h5_writer = DataProductWriter(
            output_dir=output_dir,
            product_level=product_level,
            product_name=product_name,
            channel=channel,
            version=version,
            processing_version=processing_version,
            station_metadata=station_metadata,
        )
    if write_sqlite:
        sqlite_kwargs = dict(
            output_dir=output_dir,
            product_level=product_level,
            product_name=product_name,
            channel=channel,
            version=version,
            processing_version=processing_version,
            station_metadata=station_metadata,
        )
        if sqlite_path is not None:
            sqlite_kwargs["db_path"] = Path(sqlite_path)
        sql_writer = SqliteDataProductWriter(**sqlite_kwargs)

    if h5_writer is not None and sql_writer is not None:
        return DualWriter(h5_writer, sql_writer)
    if h5_writer is not None:
        return h5_writer
    return sql_writer
