"""DualWriter (legacy) and ``make_data_product_writer`` factory.

Phases 1–3 of the HDF5 → SQLite migration used three backend
configurations (HDF5-only, SQLite-only, both-dual-write).  Phase 3b
(2026-05-20) flipped to SQLite-only writes on bee1 and Phase 4 then
collapsed the factory to always return ``SqliteDataProductWriter``.

``DualWriter`` is retained in this module solely so any out-of-tree
test fixture that imported the class continues to import; it is no
longer reachable from the factory.  Phase 5 removes it together with
the HDF5 backend modules.
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
    """Construct an SQLite data product writer.

    Post-Phase-4 the factory always returns ``SqliteDataProductWriter``.
    ``storage_config`` is kept in the signature so existing call sites
    don't need to change; only ``sqlite_path`` is honoured (the
    ``write_hdf5`` / ``write_sqlite`` knobs are no-ops — SQLite is the
    sole backend).
    """
    cfg = storage_config or {}
    sqlite_path = cfg.get("sqlite_path")  # SqliteDataProductWriter has its own default

    kwargs: Dict[str, Any] = dict(
        output_dir=output_dir,
        product_level=product_level,
        product_name=product_name,
        channel=channel,
        version=version,
        processing_version=processing_version,
        station_metadata=station_metadata,
    )
    if sqlite_path is not None:
        kwargs["db_path"] = Path(sqlite_path)
    return SqliteDataProductWriter(**kwargs)
