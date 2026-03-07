#!/usr/bin/env python3
"""
Compact HDF5 files: convert variable-length strings to fixed-length byte strings.

Reads each .h5 file under phase2/, rewrites it with fixed-length S dtype,
gzip-4 compression, and automatic chunking.  Verifies row counts match
before replacing the original.

Expected savings: ~100–200x on files with variable-length string columns.
  299 GB phase2/ → ~3 GB after compaction.

Usage:
    python3 compact_hdf5.py [--data-root /var/lib/timestd] [--dry-run]
"""

import argparse
import h5py
import numpy as np
import os
import sys
import tempfile
import shutil
import time
from pathlib import Path

# Import the canonical length table from the writer
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
from hf_timestd.io.hdf5_writer import _DEFAULT_STRING_LENGTHS, _DEFAULT_STRING_MAX


def get_fixed_dtype(dataset_name: str, orig_dtype) -> np.dtype:
    """Determine the compact dtype for a dataset."""
    if orig_dtype == object or str(orig_dtype).startswith('<U') or orig_dtype.kind == 'O':
        max_len = _DEFAULT_STRING_LENGTHS.get(dataset_name, _DEFAULT_STRING_MAX)
        return np.dtype(f'S{max_len}')
    return orig_dtype


def compact_file(src_path: Path, dry_run: bool = False) -> dict:
    """
    Compact a single HDF5 file.

    Returns dict with stats: {original_bytes, compact_bytes, rows, skipped_reason}.
    """
    result = {
        'original_bytes': src_path.stat().st_size,
        'compact_bytes': 0,
        'rows': 0,
        'skipped_reason': None,
    }

    try:
        with h5py.File(src_path, 'r', locking=False) as f_in:
            datasets = list(f_in.keys())
            if not datasets:
                result['skipped_reason'] = 'no datasets'
                return result

            # Check if any dataset uses variable-length strings
            has_vlen = False
            for name in datasets:
                ds = f_in[name]
                if ds.dtype == object or ds.dtype.kind == 'O':
                    has_vlen = True
                    break

            if not has_vlen:
                result['skipped_reason'] = 'already compact'
                result['compact_bytes'] = result['original_bytes']
                return result

            # Get row count from first dataset
            first_ds = f_in[datasets[0]]
            n_rows = first_ds.shape[0]
            result['rows'] = n_rows

            if n_rows == 0:
                result['skipped_reason'] = 'empty file'
                return result

            if dry_run:
                result['compact_bytes'] = result['original_bytes'] // 100  # rough estimate
                return result

            # Write to temp file alongside original
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix='.h5.compact',
                dir=src_path.parent
            )
            os.close(tmp_fd)

            try:
                with h5py.File(tmp_path, 'w', libver='latest') as f_out:
                    # Copy file-level attributes
                    for attr_name, attr_val in f_in.attrs.items():
                        f_out.attrs[attr_name] = attr_val

                    # Process each dataset in chunks to limit memory
                    CHUNK = 100_000
                    for name in datasets:
                        ds_in = f_in[name]
                        new_dtype = get_fixed_dtype(name, ds_in.dtype)
                        n = ds_in.shape[0]

                        # Create output dataset
                        ds_out = f_out.create_dataset(
                            name,
                            shape=(n,),
                            dtype=new_dtype,
                            chunks=True,
                            compression='gzip',
                            compression_opts=4,
                        )

                        # Copy dataset attributes
                        for attr_name, attr_val in ds_in.attrs.items():
                            ds_out.attrs[attr_name] = attr_val

                        # Copy data in chunks
                        for start in range(0, n, CHUNK):
                            end = min(start + CHUNK, n)
                            data = ds_in[start:end]

                            # Convert variable-length strings to fixed-length
                            if ds_in.dtype == object or ds_in.dtype.kind == 'O':
                                max_len = int(new_dtype.itemsize)
                                converted = []
                                for val in data:
                                    if isinstance(val, bytes):
                                        converted.append(val[:max_len])
                                    elif isinstance(val, str):
                                        converted.append(val.encode('utf-8')[:max_len])
                                    else:
                                        converted.append(str(val).encode('utf-8')[:max_len])
                                data = np.array(converted, dtype=new_dtype)

                            ds_out[start:end] = data

                    # Verify row counts
                    for name in datasets:
                        assert f_out[name].shape[0] == f_in[name].shape[0], \
                            f"Row count mismatch for {name}: {f_out[name].shape[0]} vs {f_in[name].shape[0]}"

                # Get compact size
                compact_size = os.path.getsize(tmp_path)
                result['compact_bytes'] = compact_size

                # Replace original with compact version
                os.replace(tmp_path, src_path)

            except Exception:
                # Clean up temp file on error
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    except Exception as e:
        result['skipped_reason'] = str(e)

    return result


def main():
    parser = argparse.ArgumentParser(description='Compact HDF5 phase2 files')
    parser.add_argument('--data-root', type=Path, default=Path('/var/lib/timestd'))
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done')
    parser.add_argument('--subdir', default='phase2', help='Subdirectory to process (default: phase2)')
    args = parser.parse_args()

    target_dir = args.data_root / args.subdir
    if not target_dir.exists():
        print(f"Directory not found: {target_dir}")
        sys.exit(1)

    from datetime import datetime, timezone
    today_str = datetime.now(timezone.utc).strftime('%Y%m%d')

    all_h5 = sorted(target_dir.rglob('*.h5'))
    # Skip today's files — they're actively being written by live services
    h5_files = [f for f in all_h5 if today_str not in f.name]
    skipped_today = len(all_h5) - len(h5_files)
    print(f"Found {len(all_h5)} HDF5 files in {target_dir}")
    if skipped_today:
        print(f"  Skipping {skipped_today} files for today ({today_str}) — actively written")
    print(f"  Processing {len(h5_files)} files")
    if args.dry_run:
        print("[DRY RUN]")

    total_orig = 0
    total_compact = 0
    total_skipped = 0
    total_compacted = 0
    start_time = time.time()

    for i, fpath in enumerate(h5_files):
        rel = fpath.relative_to(target_dir)
        size_mb = fpath.stat().st_size / 1e6

        result = compact_file(fpath, dry_run=args.dry_run)
        total_orig += result['original_bytes']

        if result['skipped_reason']:
            if result['skipped_reason'] != 'already compact':
                print(f"  [{i+1}/{len(h5_files)}] SKIP {rel} ({result['skipped_reason']})")
            else:
                total_compact += result['compact_bytes']
            total_skipped += 1
        else:
            ratio = result['original_bytes'] / max(result['compact_bytes'], 1)
            saved_mb = (result['original_bytes'] - result['compact_bytes']) / 1e6
            total_compact += result['compact_bytes']
            total_compacted += 1
            print(
                f"  [{i+1}/{len(h5_files)}] {rel}: "
                f"{size_mb:.0f} MB → {result['compact_bytes']/1e6:.1f} MB "
                f"({ratio:.0f}x, saved {saved_mb:.0f} MB, {result['rows']:,} rows)"
            )

    elapsed = time.time() - start_time
    saved_gb = (total_orig - total_compact) / 1e9
    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Summary:")
    print(f"  Files processed: {len(h5_files)}")
    print(f"  Compacted: {total_compacted}")
    print(f"  Skipped: {total_skipped}")
    print(f"  Original total: {total_orig/1e9:.1f} GB")
    print(f"  Compact total:  {total_compact/1e9:.1f} GB")
    print(f"  Space saved:    {saved_gb:.1f} GB")
    print(f"  Elapsed:        {elapsed:.0f}s")


if __name__ == '__main__':
    main()
