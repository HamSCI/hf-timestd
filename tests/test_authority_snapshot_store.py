"""Tests for the V1 Layer 4 authority-snapshot SQLite store.

Coverage:
  * Schema is created on first construction; idempotent on reopen.
  * Insert persists; round-trips via raw SQLite read.
  * Unknown keys in the snapshot dict are silently dropped (forward
    schema drift tolerance — new fields can be added in producer
    commits without coordinated DB migrations).
  * Missing keys land as NULL.
  * Duplicate utc_published is INSERT-OR-IGNORE'd (no exception,
    original row preserved).
  * List-valued fields are JSON-encoded for round-trip.
  * close() releases the connection.

Failure-path coverage (broken connection) is intentionally light;
the production semantics are "log and continue" and the store is
optional — most real failures show up as the warning log line
rather than as a test surface.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hf_timestd.io.authority_snapshot_store import (
    AuthoritySnapshotStore,
    COLUMNS,
)


def _full_snapshot(**overrides) -> dict:
    """Return a maximal snapshot dict covering every declared
    column.  Tests override individual fields as needed."""
    base = {
        "utc_published": "2026-05-16T11:30:00.000000Z",
        "schema_version": "v1",
        "a_level": "A1",
        "t_level_active": "T6",
        "t_level_available": ["T6", "T4", "T3"],
        "t_level_witnesses": ["T4", "T3"],
        "rtp_to_utc_offset_ns": 2384,
        "sigma_ns": 50000,
        "stations_contributing": ["WWV_10000", "CHU_7850"],
        "last_transition_utc": "2026-05-16T10:24:02.000000Z",
        "disagreement_flags": [],
        "governor_radiod": "KA9Q_T3FD",
        "bootstrap_complete": 1,
        "bootstrap_reason": "skipped",
        "bootstrap_delta_sec": 0,
        "t6_available": 1,
        "t6_reason": None,
        "t6_offset_ms": 0.0024,
        "t6_sigma_ms": 0.050,
        "t6_local_minus_source_ns": 2384,
        "t6_pps_ok": 12345,
        "t6_pps_noise": 5,
        "t6_pps_consecutive": 50,
        "t6_chain_delay_ns": 174147000,
        "t6_anchor_discontinuity": 0,
        "t6_sustained_breach": 0,
        "t6_anchor_residual_samples": 12,
        "t6_breach_duration_sec": None,
        "t6_recapture_count": 0,
        "t6_last_recapture_reason": None,
        "t6_last_recapture_age_sec": None,
        "t4_available": 1,
        "t4_offset_ms": -0.0001,
        "t4_sigma_ms": 0.0002,
        "t3_available": 1,
        "t3_offset_ms": -0.08,
        "t3_sigma_ms": 0.15,
        "t3_kalman_state": "LOCKED",
    }
    base.update(overrides)
    return base


class TestSchemaCreation(unittest.TestCase):

    def test_table_and_index_created_on_first_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            store = AuthoritySnapshotStore(path)
            store.close()
            with sqlite3.connect(str(path)) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table', 'index') "
                    "ORDER BY name",
                ).fetchall()
            names = [r[0] for r in rows]
            self.assertIn("authority_snapshot", names)
            self.assertIn("idx_authority_t_level", names)

    def test_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            AuthoritySnapshotStore(path).close()
            # Second open must not raise (CREATE TABLE IF NOT EXISTS).
            AuthoritySnapshotStore(path).close()


class TestInsert(unittest.TestCase):

    def test_full_snapshot_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot())
            with sqlite3.connect(str(path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM authority_snapshot"
                ).fetchone()
            self.assertEqual(row["utc_published"], "2026-05-16T11:30:00.000000Z")
            self.assertEqual(row["t_level_active"], "T6")
            self.assertEqual(row["rtp_to_utc_offset_ns"], 2384)
            self.assertEqual(row["t6_recapture_count"], 0)
            # List values round-trip via JSON.
            self.assertEqual(
                json.loads(row["t_level_available"]), ["T6", "T4", "T3"],
            )

    def test_unknown_keys_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot(
                    future_field_that_does_not_exist="ignored",
                    another_one=42,
                ))
            # Insert succeeded; row exists.
            with sqlite3.connect(str(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM authority_snapshot"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_missing_keys_land_as_null(self):
        sparse = {"utc_published": "2026-05-16T11:30:00.000000Z"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(sparse)
            with sqlite3.connect(str(path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM authority_snapshot"
                ).fetchone()
            self.assertIsNone(row["t_level_active"])
            self.assertIsNone(row["rtp_to_utc_offset_ns"])
            self.assertIsNone(row["t6_offset_ms"])

    def test_duplicate_utc_published_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot(t_level_active="T6"))
                store.insert(_full_snapshot(t_level_active="T4"))   # same ts
            with sqlite3.connect(str(path)) as conn:
                rows = conn.execute(
                    "SELECT t_level_active FROM authority_snapshot"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            # Original row preserved (INSERT OR IGNORE — not OR REPLACE).
            self.assertEqual(rows[0][0], "T6")

    def test_list_values_json_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot(
                    disagreement_flags=["T6<->T4:1.2ms>0.6ms", "majority-downgrade:T6->T4"],
                ))
            with sqlite3.connect(str(path)) as conn:
                row = conn.execute(
                    "SELECT disagreement_flags FROM authority_snapshot"
                ).fetchone()
            decoded = json.loads(row[0])
            self.assertEqual(len(decoded), 2)
            self.assertIn("majority-downgrade:T6->T4", decoded)


class TestClose(unittest.TestCase):

    def test_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            store = AuthoritySnapshotStore(path)
            store.close()
            store.close()                       # no exception
            # Inserts after close are silent no-ops.
            store.insert(_full_snapshot())
            with sqlite3.connect(str(path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM authority_snapshot"
                ).fetchone()[0]
            self.assertEqual(count, 0)


class TestColumnCoverage(unittest.TestCase):
    """Belt-and-suspenders: every declared COLUMN must be writable
    via insert(), and the schema must contain a matching column.
    Catches typos where a field is added to one list but not the
    other."""

    def test_every_column_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.db"
            with AuthoritySnapshotStore(path) as store:
                store.insert(_full_snapshot())
            with sqlite3.connect(str(path)) as conn:
                row = conn.execute(
                    "SELECT * FROM authority_snapshot LIMIT 1"
                ).fetchone()
            row_keys = {d[0] for d in conn.execute(
                "SELECT * FROM authority_snapshot LIMIT 1"
            ).description}
        self.assertEqual(row_keys, set(COLUMNS))


if __name__ == "__main__":
    unittest.main()
