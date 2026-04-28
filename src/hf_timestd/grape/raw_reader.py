"""
Raw Binary Reader - Read raw station data from hf-timestd archive
"""

import numpy as np
import logging
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple, Generator

logger = logging.getLogger(__name__)

class RawBinaryReader:
    """
    Reader for hf-timestd raw binary archive files.

    Reads complex64 binary files from the data archive.  Handles both
    legacy 1-minute files and multi-minute chunk files transparently:

    - Legacy: ``{minute_ts}.bin`` contains exactly 60s of samples.
    - Chunk:  ``{chunk_ts}.bin`` contains ``file_duration_sec`` seconds.
              The JSON sidecar includes ``file_duration_sec`` so we know
              how many samples the file spans.  When a per-minute read is
              requested, we locate the containing chunk and extract the
              correct 1-minute slice.

    Supports .bin (raw), .bin.zst (zstd compressed), and .bin.lz4 (lz4 compressed).
    """

    def __init__(self, data_root: Path, channel_name: str,
                 hot_buffer_root: Path = Path('/dev/shm/timestd')):
        """
        Initialize reader.

        Args:
            data_root: Root data directory (containing raw_buffer/)
            channel_name: Channel name (e.g., "SHARED 10000", "CHU 3330", "WWV 20000")
            hot_buffer_root: Hot buffer root (RAM-backed, e.g. /dev/shm/timestd)
        """
        self.data_root = Path(data_root)
        self.channel_name = channel_name

        # Channel directory: spaces become underscores
        # e.g., "SHARED 10000" -> "SHARED_10000"
        self.channel_dir_name = channel_name.replace(' ', '_')

        # Search directories in priority order:
        # 1. Hot buffer (RAM) — most recent minutes, still in /dev/shm
        # 2. Cold buffer (disk) — tiered storage archive destination
        # 3. Legacy raw_archive — older installations wrote here directly
        self._search_dirs: List[Path] = []

        hot_dir = hot_buffer_root / 'raw_buffer' / self.channel_dir_name
        if hot_dir.exists():
            self._search_dirs.append(hot_dir)

        cold_dir = self.data_root / 'raw_buffer' / self.channel_dir_name
        if cold_dir.exists():
            self._search_dirs.append(cold_dir)

        legacy_dir = self.data_root / 'raw_archive' / self.channel_dir_name
        if legacy_dir.exists():
            self._search_dirs.append(legacy_dir)

        # Primary archive_dir for backward compat (first available)
        self.archive_dir = self._search_dirs[0] if self._search_dirs else cold_dir

        # Cache of {day_dir: {minute_ts: (file_stem, offset_seconds_into_chunk)}}.
        # Built lazily by _chunk_index_for() — exactly one directory scan per
        # day_dir per reader lifetime.  The previous implementation guessed
        # chunk durations from a hardcoded list (600/300/900/3600); any
        # other recorder file_duration_sec silently produced "gap" reads.
        # The index is authoritative because it learns the duration from
        # each chunk's own JSON sidecar.
        self._chunk_index_cache: Dict[Path, Dict[int, Tuple[str, int]]] = {}

        logger.debug(f"RawBinaryReader initialized for {channel_name}, "
                     f"search dirs: {[str(d) for d in self._search_dirs]}")


    def get_available_minutes(self, date_str: str) -> List[int]:
        """
        Get list of available minute timestamps for a date.

        Searches all tiered storage locations (hot, cold, legacy) for
        .bin, .bin.zst, or .bin.lz4 files.  For multi-minute chunk files,
        expands each chunk into all the per-minute timestamps it contains
        so that callers can iterate minute-by-minute as before.

        Args:
            date_str: Date string (YYYYMMDD or YYYY-MM-DD)

        Returns:
            Sorted list of unix timestamps (minute boundaries)
        """
        if '-' in date_str:
            date_str = date_str.replace('-', '')

        # We do NOT assume that all minutes for a given UTC day live under a
        # single YYYYMMDD directory.  We scan date-1/date/date+1 and filter.
        try:
            day_start_dt = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f"Invalid date_str: {date_str}")
            return []

        day_start_ts = int(day_start_dt.timestamp())
        day_end_ts = int((day_start_dt + timedelta(days=1)).timestamp())

        candidate_dates = [
            (day_start_dt - timedelta(days=1)).strftime('%Y%m%d'),
            day_start_dt.strftime('%Y%m%d'),
            (day_start_dt + timedelta(days=1)).strftime('%Y%m%d'),
        ]

        minutes = set()
        found_any_dir = False

        for search_dir in self._search_dirs:
            for date_candidate in candidate_dates:
                day_dir = search_dir / date_candidate
                if not day_dir.exists():
                    continue
                found_any_dir = True

                index = self._chunk_index_for(day_dir)
                for ts in index:
                    if day_start_ts <= ts < day_end_ts:
                        minutes.add(ts)

        if not found_any_dir:
            logger.warning(f"No data directory for {date_str} in any search path for {self.channel_name}")

        return sorted(list(minutes))

    def _get_file_duration(self, day_dir: Path, stem: str) -> int:
        """Return file_duration_sec from JSON sidecar, defaulting to 60 (legacy)."""
        json_path = day_dir / f"{stem}.json"
        if json_path.exists():
            try:
                with open(json_path, 'r') as f:
                    meta = json.load(f)
                return int(meta.get('file_duration_sec', 60))
            except Exception:
                pass
        return 60  # legacy 1-minute files have no file_duration_sec field

    def _chunk_index_for(self, day_dir: Path) -> Dict[int, Tuple[str, int]]:
        """Return ``{minute_ts: (file_stem, offset_seconds)}`` for ``day_dir``.

        Built once per directory by globbing ``*.bin*`` and reading each
        sidecar's ``file_duration_sec``; cached on the reader instance.

        This is the authoritative replacement for the old
        "guess from {600, 300, 900, 3600}" heuristic.  Any chunk
        duration the recorder writes — including non-standard values —
        is honored because we learn it from each file's own sidecar.
        Files with a missing or unparseable sidecar fall back to the
        legacy 60-second assumption (one minute per file).

        Returns an empty dict (not None) for empty / nonexistent dirs.
        """
        cached = self._chunk_index_cache.get(day_dir)
        if cached is not None:
            return cached

        index: Dict[int, Tuple[str, int]] = {}
        if not day_dir.exists():
            self._chunk_index_cache[day_dir] = index
            return index

        for f in day_dir.glob('*.bin*'):
            name = f.name
            if '.bin' not in name:
                continue
            stem = name.split('.bin')[0]
            if not stem.isdigit():
                continue
            chunk_ts = int(stem)
            dur = self._get_file_duration(day_dir, stem)
            if dur <= 0 or dur % 60 != 0:
                logger.warning(
                    f"chunk {f} has unusable file_duration_sec={dur}; "
                    f"skipping"
                )
                continue
            for offset in range(0, dur, 60):
                minute_ts = chunk_ts + offset
                if minute_ts in index:
                    existing_stem, _ = index[minute_ts]
                    if existing_stem == stem:
                        continue  # same chunk seen via .bin and .bin.zst
                    logger.warning(
                        f"duplicate chunk for minute {minute_ts}: "
                        f"{existing_stem} vs {stem} in {day_dir} "
                        f"(keeping first)"
                    )
                    continue
                index[minute_ts] = (stem, offset)

        self._chunk_index_cache[day_dir] = index
        return index

    def read_minute(self, minute_timestamp: int) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read IQ samples and metadata for a specific minute.

        Looks up ``minute_timestamp`` in the directory's chunk index
        (built once and cached), then reads the containing chunk and
        extracts the correct 1-minute slice.  Handles legacy 1-minute
        files (each chunk is its own minute) and multi-minute chunks
        of any duration the recorder happens to use, because the
        index learns each chunk's duration from its own JSON sidecar.

        Args:
            minute_timestamp: Unix timestamp of the minute start

        Returns:
            Tuple of (samples, metadata)
            samples: complex64 numpy array (1 minute of samples) or None
            metadata: dict or None when no chunk covers this minute
        """
        dt = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
        candidate_dates = [
            (dt - timedelta(days=1)).strftime('%Y%m%d'),
            dt.strftime('%Y%m%d'),
            (dt + timedelta(days=1)).strftime('%Y%m%d'),
        ]

        for search_dir in self._search_dirs:
            for date_candidate in candidate_dates:
                day_dir = search_dir / date_candidate
                index = self._chunk_index_for(day_dir)
                entry = index.get(minute_timestamp)
                if entry is None:
                    continue

                stem, offset_sec = entry
                chunk_samples, chunk_meta = self._try_read_file(day_dir, stem)
                if chunk_samples is None:
                    # Index pointed at a file we couldn't actually read
                    # (corruption, vanished mid-run, etc.).  Try the next
                    # search dir; gap detection will pick this up.
                    logger.warning(
                        f"chunk index for minute {minute_timestamp} pointed "
                        f"at {day_dir}/{stem}.bin* but the file is unreadable"
                    )
                    continue

                sample_rate = (
                    int(chunk_meta.get('sample_rate', 24000))
                    if chunk_meta else 24000
                )
                samples_per_min = sample_rate * 60
                offset_samples = offset_sec * sample_rate

                if offset_samples + samples_per_min > len(chunk_samples):
                    logger.warning(
                        f"chunk {stem} too short for minute {minute_timestamp}: "
                        f"need {offset_samples}+{samples_per_min} samples, "
                        f"have {len(chunk_samples)}"
                    )
                    del chunk_samples
                    continue

                samples = chunk_samples[
                    offset_samples:offset_samples + samples_per_min
                ].copy()
                del chunk_samples
                return samples, chunk_meta

        # No covering chunk in any search dir.  This is the common
        # "real gap" path; the decimation pipeline treats it as such.
        logger.debug(
            f"no chunk covering minute {minute_timestamp} for {self.channel_name}"
        )
        return None, None

    def _try_read_file(self, day_dir: Path, base_name: str) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """Try to read a binary file and its JSON sidecar from day_dir.

        Tries .bin, .bin.zst, .bin.lz4 in order.  Returns the full file
        contents (not sliced).

        Returns:
            (samples, metadata) or (None, None)
        """
        samples = None

        # Try uncompressed .bin
        bin_path = day_dir / f"{base_name}.bin"
        if bin_path.exists():
            try:
                mm = np.memmap(bin_path, dtype=np.complex64, mode='r')
                samples = np.array(mm, dtype=np.complex64)
                del mm
            except Exception as e:
                logger.error(f"Error reading {bin_path}: {e}")

        # Try zstd compressed .bin.zst
        if samples is None:
            zst_path = day_dir / f"{base_name}.bin.zst"
            if zst_path.exists():
                try:
                    import zstandard as zstd
                    with open(zst_path, 'rb') as f:
                        dctx = zstd.ZstdDecompressor()
                        data = dctx.decompress(f.read())
                        samples = np.frombuffer(data, dtype=np.complex64).copy()
                        del data
                except ImportError:
                    logger.warning("zstandard module not installed - cannot read .zst files")
                except Exception as e:
                    logger.error(f"Error reading {zst_path}: {e}")

        # Try lz4 compressed .bin.lz4
        if samples is None:
            lz4_path = day_dir / f"{base_name}.bin.lz4"
            if lz4_path.exists():
                try:
                    import lz4.frame
                    with open(lz4_path, 'rb') as f:
                        data = lz4.frame.decompress(f.read())
                        samples = np.frombuffer(data, dtype=np.complex64).copy()
                        del data
                except ImportError:
                    logger.warning("lz4 module not installed - cannot read .lz4 files")
                except Exception as e:
                    logger.error(f"Error reading {lz4_path}: {e}")

        # Read metadata sidecar
        metadata = None
        json_path = day_dir / f"{base_name}.json"
        if json_path.exists():
            try:
                with open(json_path, 'r') as f:
                    metadata = json.load(f)
            except Exception as e:
                logger.warning(f"Error reading metadata {json_path}: {e}")

        return samples, metadata

    def read_day(self, date_str: str) -> Generator[Tuple[int, Optional[np.ndarray], Optional[Dict]], None, None]:
        """
        Yield all available minutes for a day.
        
        Args:
            date_str: Date string (YYYYMMDD)
            
        Yields:
            Tuple of (minute_timestamp, samples, metadata)
        """
        minutes = self.get_available_minutes(date_str)
        logger.info(f"Found {len(minutes)} minutes for {date_str} in {self.channel_name}")
        
        for minute_ts in minutes:
            samples, meta = self.read_minute(minute_ts)
            yield minute_ts, samples, meta

    def get_sample_rate(self, date_str: str) -> int:
        """
        Estimate sample rate from the first available file.
        Default to 24000 if cannot determine.
        
        Falls back to reading .json metadata even if no .bin files exist.
        """
        minutes = self.get_available_minutes(date_str)
        if minutes:
            _, meta = self.read_minute(minutes[0])
            if meta and 'sample_rate' in meta:
                return int(meta['sample_rate'])
        
        # Fallback: check JSON metadata in any search directory
        if '-' in date_str:
            date_str = date_str.replace('-', '')
        for search_dir in self._search_dirs:
            day_dir = search_dir / date_str
            if not day_dir.exists():
                continue
            for json_file in day_dir.glob('*.json'):
                try:
                    with open(json_file, 'r') as f:
                        meta = json.load(f)
                    if 'sample_rate' in meta:
                        return int(meta['sample_rate'])
                except Exception:
                    continue
            
        return 24000 
