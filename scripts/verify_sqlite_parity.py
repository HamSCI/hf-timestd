#!/usr/bin/env python3
"""Compare HDF5 vs SQLite output for a data product over a time window.

Phase 1 of the HDF5 → SQLite migration. Once a host has been running
dual-write for some time, this script verifies that the two backends
agree row-by-row — any divergence is a bug in the writers and must be
fixed before Phase 2 (reader migration) can begin.

Usage:
    python verify_sqlite_parity.py \\
        --channel CHU_7850 \\
        --product timing_measurements \\
        --level L2 \\
        --hours 1 \\
        [--hdf5-dir /var/lib/timestd/phase2] \\
        [--sqlite-db /var/lib/timestd/phase2/timestd.db]

Exits 0 if the two backends agree on row count and field values,
1 otherwise. Prints a summary that lists field-level mismatches.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Tolerance for float comparison — empirically, HDF5 and SQLite both
# store IEEE 754 doubles faithfully, so exact equality is reasonable.
# Bump if a real cross-backend rounding difference shows up.
FLOAT_TOL = 0.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", required=True, help="Channel name (e.g. CHU_7850)")
    p.add_argument("--product", required=True, help="Product name (e.g. timing_measurements)")
    p.add_argument("--level", default="L2", help="Product level (L1/L2/L3); default L2")
    p.add_argument("--hours", type=float, default=1.0, help="Look-back window in hours; default 1")
    p.add_argument(
        "--hdf5-dir",
        type=Path,
        default=Path("/var/lib/timestd/phase2"),
        help="Root of HDF5 channel directories",
    )
    p.add_argument(
        "--sqlite-db",
        type=Path,
        default=Path("/var/lib/timestd/phase2/timestd.db"),
        help="Path to the SQLite database",
    )
    p.add_argument("--verbose", action="store_true", help="Print per-row diff details")
    return p.parse_args()


def _time_window(args: argparse.Namespace) -> Tuple[str, str]:
    """Build a single (start_iso, end_iso) window used by BOTH backends.

    Leaves a 60-second flush buffer at the end of the window — HDF5
    writers use SWMR and recently-appended rows may not be visible to
    readers for ~tens of seconds. Without the buffer, the boundary
    produces spurious "sql_only" divergences (rows committed to SQLite
    WAL but not yet flushed for HDF5 readers). The buffer pushes both
    reads back into the steady-state region where SWMR has caught up.
    """
    now = datetime.now(timezone.utc)
    end = now - timedelta(seconds=60)
    start = end - timedelta(hours=args.hours)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


def read_hdf5(args: argparse.Namespace, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    """Use DataProductReader to read N hours of data from the HDF5 archive."""
    from hf_timestd.io import DataProductReader
    from hf_timestd.data_product_registry import DataProductRegistry

    channel_dir = args.hdf5_dir / args.channel
    data_dir = DataProductRegistry.get_data_dir(
        channel_dir=channel_dir,
        product_level=args.level,
        product_name=args.product,
        create=False,
    )
    reader = DataProductReader(
        data_dir=data_dir,
        product_level=args.level,
        product_name=args.product,
        channel=args.channel,
    )
    return reader.read_time_range(start=start_iso, end=end_iso)


def read_sqlite(args: argparse.Namespace, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    """Read the same time range from SQLite via a raw query."""
    table = f"{args.level}_{args.product}"

    conn = sqlite3.connect(f"file:{args.sqlite_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} "
            f"WHERE channel = ? AND timestamp_utc BETWEEN ? AND ? "
            f"ORDER BY timestamp_utc",
            (args.channel, start_iso, end_iso),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def normalise(value: Any) -> Any:
    """Make HDF5/SQLite values comparable.

    - HDF5 bool → numpy bool → coerce to Python bool.
    - SQLite int 0/1 for booleans → leave as int; we'll only compare
      values via float/int conversion.
    - bytes → str.
    - NaN floats are not equal to themselves; map them to None so a
      NULL-on-the-SQLite-side and NaN-on-the-HDF5-side count as the
      same "missing" state.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, float):
        try:
            if value != value:  # NaN check
                return None
        except Exception:
            pass
    # numpy scalars
    if hasattr(value, "item"):
        try:
            v = value.item()
            return normalise(v)
        except Exception:
            pass
    return value


def _is_hdf5_default_fill(x: Any) -> bool:
    """Recognise the type-default values HDF5's writer substitutes for
    Python None in `hdf5_writer.py:_append_measurement`:
      - integer → 0
      - string  → ""
      - boolean → False
      - float   → NaN (already mapped to None by normalise())

    SQLite, by contrast, stores actual NULL for missing values. When
    one side is None and the other is the corresponding HDF5 fill, the
    two backends are semantically equivalent ("missing") even though
    they look different at the raw-value level. The dual-write parity
    check should not flag these as divergences — they're an artefact
    of HDF5's inability to represent NULL.

    Side effect: a real 0 / empty string / False that should differ
    from a real NULL will be silently treated as equivalent here. That
    is acceptable for Phase 1 verification — the cases we actually
    care about (different non-fill values across backends) are still
    caught.
    """
    if isinstance(x, str) and x == "":
        return True
    if isinstance(x, bool) and x is False:
        return True
    # `isinstance(x, int)` is True for booleans too — exclude those.
    if isinstance(x, int) and not isinstance(x, bool) and x == 0:
        return True
    return False


def values_equal(a: Any, b: Any) -> bool:
    a, b = normalise(a), normalise(b)
    if a is None and b is None:
        return True
    # HDF5 default-fill ↔ SQLite NULL — semantically equivalent.
    if a is None and _is_hdf5_default_fill(b):
        return True
    if b is None and _is_hdf5_default_fill(a):
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= FLOAT_TOL
        except Exception:
            return False
    # Bool/int cross-comparison: HDF5 may store True/False, SQLite 1/0.
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return a == b


def diff_rows(
    h5_rows: List[Dict[str, Any]],
    sql_rows: List[Dict[str, Any]],
) -> Tuple[int, List[Tuple[str, str, str, Any, Any]]]:
    """Join by timestamp_utc and report value-level diffs.

    Returns (total_diffs, list of (timestamp, field, side, h5_val, sql_val)).
    """
    h5_by_ts = {normalise(r.get("timestamp_utc")): r for r in h5_rows}
    sql_by_ts = {normalise(r.get("timestamp_utc")): r for r in sql_rows}
    diffs: List[Tuple[str, str, str, Any, Any]] = []

    h5_only = set(h5_by_ts) - set(sql_by_ts)
    sql_only = set(sql_by_ts) - set(h5_by_ts)
    for ts in sorted(h5_only):
        diffs.append((str(ts), "*", "h5_only", h5_by_ts[ts], None))
    for ts in sorted(sql_only):
        diffs.append((str(ts), "*", "sql_only", None, sql_by_ts[ts]))

    for ts in sorted(set(h5_by_ts) & set(sql_by_ts)):
        h5_row = h5_by_ts[ts]
        sql_row = sql_by_ts[ts]
        # Compare fields present in HDF5 (the canonical schema).
        for k, h5_v in h5_row.items():
            if k == "channel":
                # SQLite has a channel column; HDF5 doesn't.
                continue
            sql_v = sql_row.get(k)
            if not values_equal(h5_v, sql_v):
                diffs.append((str(ts), k, "value_diff", h5_v, sql_v))

    return len(diffs), diffs


def main() -> int:
    args = parse_args()
    # Compute the time window ONCE so HDF5 and SQLite reads use
    # identical bounds — otherwise a few-microsecond drift between
    # two now() calls can leak a boundary row into one but not the
    # other.
    start_iso, end_iso = _time_window(args)
    try:
        h5_rows = read_hdf5(args, start_iso, end_iso)
    except Exception as e:
        print(f"ERROR reading HDF5: {e}", file=sys.stderr)
        return 2
    try:
        sql_rows = read_sqlite(args, start_iso, end_iso)
    except Exception as e:
        print(f"ERROR reading SQLite: {e}", file=sys.stderr)
        return 2

    print(f"Channel:    {args.channel}")
    print(f"Product:    {args.level}_{args.product}")
    print(f"Window:     last {args.hours} h")
    print(f"HDF5 rows:  {len(h5_rows)}")
    print(f"SQLite rows: {len(sql_rows)}")

    n_diff, diffs = diff_rows(h5_rows, sql_rows)
    if n_diff == 0:
        print("OK — no divergence between backends.")
        return 0

    print(f"\n{n_diff} divergence(s) found:")
    if args.verbose:
        for ts, field, kind, h5_v, sql_v in diffs:
            print(f"  [{kind}] ts={ts} field={field}: h5={h5_v!r} sql={sql_v!r}")
    else:
        # Summary by kind / field
        from collections import Counter
        ctr = Counter((d[1], d[2]) for d in diffs)
        for (field, kind), count in ctr.most_common(20):
            print(f"  field={field} kind={kind}: {count} occurrences")
        if len(ctr) > 20:
            print(f"  ... and {len(ctr) - 20} more (re-run with --verbose for detail)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
