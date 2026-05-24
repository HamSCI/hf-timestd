"""Append-only SQLite store for AuthorityManager cycle snapshots.

Layer 4 of the Pattern B V1 fix (docs/TIMING-PIPELINE-WIRING.md
§10.3): each authority tick already publishes a snapshot of the
timing chain to ``/run/hf-timestd/authority.json``.  That file is
overwritten every cycle, so the per-cycle history is otherwise
lost.  This store appends one row per cycle to a local SQLite
database so the time-series is queryable hours / days / weeks
later:

  - **GPSDO drift vs UTC over time** — slow walk in ``rtp_to_utc_offset_ns``.
  - **BPSK injector / modulator behaviour** — sudden steps or periodic
    structure in ``t6_local_minus_source_ns``.
  - **Chrony discipline quality** — variance of ``t4_offset_ms``.
  - **Anchor health** — ``t6_recapture_count`` / reason histogram,
    correlation between recaptures and chrony events.
  - **T-level transition history** — ``last_transition_utc`` vs
    actual transitions; cross-correlate with operational incidents.

The store is **optional**: when no path is configured the
AuthorityManager just doesn't construct one and the cycle proceeds
exactly as before.  No fallback path is required for operators who
don't want long-term history.

Design choices:

* **Separate DB file** at ``/var/lib/timestd/authority_history.db``,
  not co-located with the Phase 1 HDF5→SQLite migration target
  (``/var/lib/timestd/phase2/timestd.db``).  Keeps the verification
  window for Phase 1 clean — adding writers to that file during
  verification would muddy the parity story.

* **WAL + synchronous=NORMAL**, matching ``sqlite_writer.py``'s
  pattern.  Allows concurrent readers (e.g. an ad-hoc ``sqlite3``
  CLI invocation by an operator) without blocking the cycle writer.

* **Append-only with INSERT OR IGNORE** on ``utc_published`` PK so
  a duplicate cycle (clock skew, manual replay) silently no-ops
  rather than aborting the rest of the row's metadata.

* **Schema drift is tolerant**: ``insert()`` accepts an arbitrary
  dict and only emits the known columns; extra keys are silently
  dropped, missing keys land as NULL.  New columns can be added
  in a follow-up commit by appending to ``COLUMNS`` and the
  CREATE TABLE — old rows just carry NULL for the new field.

Retention is the operator's concern.  sigmond's ``smd storage
trim`` machinery already has timers for the upload sinks (wspr
24h / hfdl 24h / timestd 30d); adding a similar timer for
authority_history.db is a follow-up step, not part of this
commit.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Ordered column list.  Tracks the schema below.  Used by insert()
# to extract only the known fields from the caller's snapshot dict.
COLUMNS = (
    # --- AuthorityState headline ---
    "utc_published",
    "schema_version",
    "a_level",
    "t_level_active",
    "t_level_available",        # JSON-encoded list
    "t_level_witnesses",        # JSON-encoded list
    "rtp_to_utc_offset_ns",
    "sigma_ns",
    "stations_contributing",    # JSON-encoded list
    "last_transition_utc",
    "disagreement_flags",       # JSON-encoded list
    "governor_radiod",
    "bootstrap_complete",       # 0/1 or NULL
    "bootstrap_reason",
    "bootstrap_delta_sec",
    # --- T6 (BpskPpsProbe) headline ---
    "t6_available",             # 0/1
    "t6_reason",                # ProbeResult.reason when unavailable
    "t6_offset_ms",             # ProbeResult.offset_ms (== Δ in ms)
    "t6_sigma_ms",
    "t6_local_minus_source_ns",
    "t6_pps_ok",
    "t6_pps_noise",
    "t6_pps_consecutive",
    "t6_chain_delay_ns",
    # --- T6 Layer 2 drift monitor ---
    "t6_anchor_discontinuity",  # 0/1
    "t6_sustained_breach",      # 0/1
    "t6_anchor_residual_samples",
    "t6_breach_duration_sec",
    # --- T6 Layer 3 re-capture ---
    "t6_recapture_count",
    "t6_last_recapture_reason",
    "t6_last_recapture_age_sec",
    # --- T5 (LbeT5DirectProbe — LBE-1421 USB-NMEA, substrate-grounded) ---
    "t5_available",
    "t5_offset_ms",
    "t5_sigma_ms",
    "t5_valid_fix",             # 0/1, NULL when no probe attached
    "t5_pps_utc_sec",
    "t5_nmea_age_sec",
    # --- T4 (ChronyTrackingProbe) ---
    "t4_available",
    "t4_offset_ms",
    "t4_sigma_ms",
    # --- T3 (FusionStatusProbe) ---
    "t3_available",
    "t3_offset_ms",
    "t3_sigma_ms",
    "t3_kalman_state",
)


# Mapping from column name to type / NULL allowance.  The exact SQL
# is generated below; this comment is the source of truth.  Columns
# not in this mapping default to TEXT NULL.
_INT_COLUMNS = frozenset({
    "schema_version",
    "rtp_to_utc_offset_ns",
    "sigma_ns",
    "bootstrap_complete",
    "bootstrap_delta_sec",
    "t6_available",
    "t6_local_minus_source_ns",
    "t6_pps_ok",
    "t6_pps_noise",
    "t6_pps_consecutive",
    "t6_chain_delay_ns",
    "t6_anchor_discontinuity",
    "t6_sustained_breach",
    "t6_anchor_residual_samples",
    "t6_recapture_count",
    "t5_available",
    "t5_valid_fix",
    "t5_pps_utc_sec",
    "t4_available",
    "t3_available",
})
_REAL_COLUMNS = frozenset({
    "t6_offset_ms",
    "t6_sigma_ms",
    "t6_breach_duration_sec",
    "t6_last_recapture_age_sec",
    "t5_offset_ms",
    "t5_sigma_ms",
    "t5_nmea_age_sec",
    "t4_offset_ms",
    "t4_sigma_ms",
    "t3_offset_ms",
    "t3_sigma_ms",
})


def _column_sql(name: str) -> str:
    if name == "utc_published":
        return f"{name} TEXT NOT NULL PRIMARY KEY"
    if name in _INT_COLUMNS:
        return f"{name} INTEGER"
    if name in _REAL_COLUMNS:
        return f"{name} REAL"
    return f"{name} TEXT"


_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS authority_snapshot (\n    "
    + ",\n    ".join(_column_sql(c) for c in COLUMNS)
    + "\n)"
)
_CREATE_INDEX_T_LEVEL = (
    "CREATE INDEX IF NOT EXISTS idx_authority_t_level "
    "ON authority_snapshot(t_level_active)"
)


class AuthoritySnapshotStore:
    """Long-lived SQLite connection that appends one row per
    AuthorityManager cycle.

    The store opens its own connection and lives for the duration
    of the runner process.  Failures are non-fatal: ``insert()``
    logs the exception and returns — the cycle's primary deliverable
    (``authority.json``) must not be blocked by a DB hiccup.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` because the runner thread isn't
        # the constructor thread in production (the store is built in
        # the main thread and passed to the runner's tick loop).
        # We never share the cursor across threads — only the
        # connection object — and the inserts are serialized by the
        # runner's single tick loop, so this is safe.
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=5.0,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_CREATE_TABLE + ";\n" + _CREATE_INDEX_T_LEVEL + ";")
        self._migrate_missing_columns()
        self._conn.commit()
        logger.info(
            "AuthoritySnapshotStore initialised at %s", self.db_path,
        )

    def _migrate_missing_columns(self) -> None:
        """Forward-only schema migration.

        ``CREATE TABLE IF NOT EXISTS`` only creates the table when it's
        absent — it never alters an existing table.  Adding a column to
        ``COLUMNS`` without an explicit ALTER would silently fail every
        INSERT on existing DBs, the cycle's snapshot would be lost, and
        the operator would only notice via journal warnings (or absent
        rows in queries — the symptom that bit Phase 2A T5 rollout).
        This helper bridges the gap: it diffs the live schema against
        ``COLUMNS`` and emits ``ALTER TABLE ADD COLUMN`` for each
        missing field.

        Forward-only: removed or renamed columns are NOT dropped — a
        downgrade-then-upgrade cycle must work, so unknown old columns
        are left alone.  Renames are not supported (treat them as
        add-new + leave-old).

        Idempotent — re-running on an up-to-date schema is a no-op.
        """
        cur = self._conn.execute(
            "PRAGMA table_info(authority_snapshot)"
        )
        existing = {row[1] for row in cur.fetchall()}  # row[1] = column name
        for column in COLUMNS:
            if column in existing:
                continue
            # _column_sql returns "name TYPE [PRIMARY KEY]" — we strip
            # any constraints SQLite forbids in ALTER ADD COLUMN
            # (PRIMARY KEY, UNIQUE, NOT NULL without default).  Our only
            # constraint today is PRIMARY KEY on utc_published, which
            # cannot be added by ALTER — but it's also impossible to
            # reach this branch for utc_published, since CREATE TABLE
            # IF NOT EXISTS would have put the table there on first
            # init.  Defensive guard anyway.
            if column == "utc_published":
                continue
            type_sql = (
                "INTEGER" if column in _INT_COLUMNS
                else "REAL" if column in _REAL_COLUMNS
                else "TEXT"
            )
            self._conn.execute(
                f"ALTER TABLE authority_snapshot ADD COLUMN {column} {type_sql}"
            )
            logger.info(
                "AuthoritySnapshotStore: added missing column %s %s",
                column, type_sql,
            )

    def insert(self, snapshot: dict) -> None:
        """Append a snapshot row.  Unknown keys in ``snapshot`` are
        silently dropped (schema drift tolerance); missing keys land
        as NULL.  Duplicate ``utc_published`` rows are ignored.

        List-valued fields (e.g. ``t_level_available``,
        ``disagreement_flags``) are JSON-encoded before storage so
        operators can round-trip them with ``json.loads`` at query
        time.
        """
        if self._conn is None:
            return
        values = []
        for column in COLUMNS:
            v = snapshot.get(column)
            # JSON-encode list / dict values so the SQLite column
            # stays TEXT but the structure round-trips.
            if isinstance(v, (list, tuple, dict)):
                try:
                    v = json.dumps(v, separators=(",", ":"))
                except (TypeError, ValueError):
                    v = str(v)
            values.append(v)
        placeholders = ",".join("?" for _ in COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO authority_snapshot "
            f"({','.join(COLUMNS)}) VALUES ({placeholders})"
        )
        try:
            self._conn.execute(sql, values)
            self._conn.commit()
        except sqlite3.Error as exc:
            # Don't propagate.  authority.json was already written;
            # the cycle's primary deliverable succeeded.  A DB hiccup
            # is an observability gap, not a service failure.
            logger.warning(
                "AuthoritySnapshotStore: insert failed (%s): %s",
                snapshot.get("utc_published"), exc,
            )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            finally:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # Context-manager support for tests + ad-hoc scripts.
    def __enter__(self) -> "AuthoritySnapshotStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
