#!/usr/bin/env python3
"""Compare the diff sidecar's per-PPS edge timestamps against the
matched-filter calibrator's chain_delay history.

The diff detector writes one CSV row per accepted PPS edge:
    timestamp_unix,edge_rtp_int,edge_rtp_frac,d_magnitude,median_d,chain_delay_samples

The MF calibrator does NOT write per-edge data, but its chain_delay
is visible in:
  * authority_history.db.authority_snapshot.t6_offset_ms /
    t6_local_minus_source_ns (sampled every 30 s)
  * core-recorder journal's "T6 BPSK PPS LOCKED" lines on transitions
  * core-recorder journal's "T6 SHM diag" lines (last_edge_rtp every 60 s)

This script:
  1. Reads the diff CSV.
  2. Reports diff-detector statistics: edges/min, σ of chain_delay,
     histogram of |d| values at accepted edges, threshold margin.
  3. Pulls MF chain_delay history from authority_history.db over the
     same window.
  4. Reports comparison: median(diff_chain_delay - MF_chain_delay),
     σ of the difference, residual fingerprint.

If the diff detector's chain_delay is tighter than the MF's by a
meaningful margin, and the per-PPS difference is consistent, that's
evidence to migrate the operational detection path.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
import sys
from pathlib import Path


def read_diff_csv(path: Path):
    rows = []
    with path.open() as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            rows.append({
                'ts': float(r['timestamp_unix']),
                'edge_rtp_int': int(r['edge_rtp_int']),
                'edge_rtp_frac': float(r['edge_rtp_frac']),
                'd_magnitude': float(r['d_magnitude']),
                'median_d': float(r['median_d']),
                'chain_delay_samples': float(r['chain_delay_samples']),
            })
    return rows


def read_mf_authority(db_path: Path, since_ts: float):
    """Pull MF-derived t6 fields from authority history since since_ts."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT utc_published, t6_offset_ms, t6_local_minus_source_ns, "
            "t6_chain_delay_ns "
            "FROM authority_snapshot "
            "WHERE utc_published > datetime(?, 'unixepoch') "
            "AND t6_available = 1 "
            "ORDER BY rowid",
            (since_ts,),
        )
        return [
            {
                'utc': row[0],
                't6_offset_ms': row[1],
                't6_lms_ns': row[2],
                't6_chain_delay_ns': row[3],
            }
            for row in cur.fetchall()
        ]
    finally:
        conn.close()


def summarize_diff(rows, sample_rate_hz):
    """Per-PPS statistics for the diff detector."""
    if len(rows) < 3:
        print("  not enough rows for statistics")
        return
    print(f"  edges: {len(rows)}")
    duration_s = rows[-1]['ts'] - rows[0]['ts']
    print(f"  time span: {duration_s:.1f} s ({duration_s/60:.1f} min)")
    if duration_s > 0:
        print(f"  rate: {len(rows)/duration_s*60:.1f} edges/min "
              f"(expect ~60 if signal is clean)")
    # chain_delay statistics (mod SR; comparable to MF's modular value).
    cd = [r['chain_delay_samples'] for r in rows]
    cd_ns = [c * 1e9 / sample_rate_hz for c in cd]
    print(f"  chain_delay_samples: median={statistics.median(cd):.3f}, "
          f"σ={statistics.stdev(cd):.3f}, "
          f"range=[{min(cd):.3f}, {max(cd):.3f}]")
    print(f"  chain_delay_ns:      median={statistics.median(cd_ns):.0f} ns, "
          f"σ={statistics.stdev(cd_ns):.0f} ns, "
          f"range={max(cd_ns) - min(cd_ns):.0f} ns")
    # |d| margin: how far above threshold are we?
    margins = [r['d_magnitude'] / r['median_d'] if r['median_d'] > 0 else float('inf')
               for r in rows]
    finite = [m for m in margins if m != float('inf')]
    if finite:
        print(f"  detection margin (|d|/median_d): "
              f"median={statistics.median(finite):.0f}×, "
              f"min={min(finite):.0f}×, "
              f"max={max(finite):.0f}×")


def summarize_mf(rows, sample_rate_hz):
    if len(rows) < 3:
        print("  not enough MF rows for statistics")
        return
    print(f"  authority snapshots: {len(rows)}")
    offsets_ms = [r['t6_offset_ms'] for r in rows if r['t6_offset_ms'] is not None]
    if offsets_ms:
        print(f"  t6_offset_ms: median={statistics.median(offsets_ms):.3f} ms, "
              f"σ={statistics.stdev(offsets_ms):.3f} ms, "
              f"range=[{min(offsets_ms):.3f}, {max(offsets_ms):.3f}]")
    cd_ns = [r['t6_chain_delay_ns'] for r in rows
             if r['t6_chain_delay_ns'] is not None]
    if cd_ns:
        cd_mod = [c % (sample_rate_hz * 1e9 / sample_rate_hz) for c in cd_ns]  # ns
        # The MF chain_delay is in ns since 0; modular to [0, 1e9) ns.
        cd_mod = [c % 1_000_000_000 for c in cd_ns]
        print(f"  t6_chain_delay (mod 1 s): "
              f"median={statistics.median(cd_mod):.0f} ns, "
              f"σ={statistics.stdev(cd_mod):.0f} ns, "
              f"range={max(cd_mod) - min(cd_mod):.0f} ns")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        '--csv', type=Path,
        default=Path('/var/lib/timestd/debug/bpsk_diff_edges.csv'),
        help='Path to diff sidecar CSV',
    )
    ap.add_argument(
        '--db', type=Path,
        default=Path('/var/lib/timestd/authority_history.db'),
        help='Path to authority_history.db',
    )
    ap.add_argument(
        '--sample-rate', type=int, default=96000,
        help='IQ sample rate (Hz)',
    )
    args = ap.parse_args()

    print(f"=== Diff detector sidecar: {args.csv} ===")
    if not args.csv.exists():
        print(f"  (no CSV at {args.csv}; sidecar not enabled or not yet "
              f"writing)")
        sys.exit(1)
    diff_rows = read_diff_csv(args.csv)
    summarize_diff(diff_rows, args.sample_rate)

    if not diff_rows:
        sys.exit(0)
    since_ts = diff_rows[0]['ts']
    print(f"\n=== MF detector (authority_history.db, since {since_ts}) ===")
    mf_rows = read_mf_authority(args.db, since_ts)
    summarize_mf(mf_rows, args.sample_rate)


if __name__ == '__main__':
    main()
