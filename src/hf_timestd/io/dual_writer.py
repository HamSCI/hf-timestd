"""``make_data_product_writer`` factory — SQLite-only post-Phase-4.

Phases 1–3 of the HDF5 → SQLite migration used three backend
configurations (HDF5-only, SQLite-only, both-dual-write); Phase 3b
flipped bee1 to SQLite-only writes (2026-05-20) and Phase 4 deleted
the HDF5 backend.  The factory now unconditionally returns a
``SqliteDataProductWriter``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .sqlite_writer import SqliteDataProductWriter

logger = logging.getLogger(__name__)


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
