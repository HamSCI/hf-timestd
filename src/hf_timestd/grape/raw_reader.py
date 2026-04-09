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

                # Scan for binary files
                for f in day_dir.glob('*.bin*'):
                    try:
                        name = f.name
                        if '.bin' in name:
                            stem = name.split('.bin')[0]
                            if stem.isdigit():
                                file_ts = int(stem)
                                # Determine file duration from JSON sidecar
                                dur = self._get_file_duration(day_dir, stem)
                                # Expand chunk into per-minute timestamps
                                for offset in range(0, dur, 60):
                                    ts = file_ts + offset
                                    if day_start_ts <= ts < day_end_ts:
                                        minutes.add(ts)
                    except Exception as e:
                        logger.debug(f"Caught exception: {e}")
                        continue

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

    def read_minute(self, minute_timestamp: int) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read IQ samples and metadata for a specific minute.

        Handles both legacy 1-minute files (named by minute timestamp) and
        multi-minute chunk files (named by chunk boundary timestamp).  For
        chunk files the correct 1-minute slice is extracted.

        Args:
            minute_timestamp: Unix timestamp of the minute start

        Returns:
            Tuple of (samples, metadata)
            samples: complex64 numpy array (1 minute of samples) or None
            metadata: dict or None
        """
        dt = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        base_name = str(minute_timestamp)

        candidate_dates = [
            (dt - timedelta(days=1)).strftime('%Y%m%d'),
            date_str,
            (dt + timedelta(days=1)).strftime('%Y%m%d'),
        ]

        # 1. Try to read samples — search all directories
        samples = None
        metadata = None

        for search_dir in self._search_dirs:
            for date_candidate in candidate_dates:
                day_dir = search_dir / date_candidate
                if not day_dir.exists():
                    continue

                # --- Try exact-match file (legacy 1-minute or this IS the chunk start) ---
                samples, metadata = self._try_read_file(day_dir, base_name)

                if samples is not None:
                    # Check if this is a chunk file and we need a slice
                    file_dur = 60
                    if metadata and 'file_duration_sec' in metadata:
                        file_dur = int(metadata['file_duration_sec'])

                    if file_dur > 60:
                        # This minute IS the chunk start — extract first minute
                        sample_rate = int(metadata.get('sample_rate', 24000))
                        samples_per_min = sample_rate * 60
                        samples = samples[:samples_per_min].copy()
                    break

                # --- Try containing chunk file (minute is inside a larger chunk) ---
                # Search for chunk files that could contain this minute.
                # Try common durations: 600 (10 min), 300 (5 min), 900 (15 min), 3600 (1 hr)
                for dur in (600, 300, 900, 3600):
                    chunk_boundary = (minute_timestamp // dur) * dur
                    if chunk_boundary == minute_timestamp:
                        continue  # Already tried exact match above
                    chunk_name = str(chunk_boundary)
                    chunk_samples, chunk_meta = self._try_read_file(day_dir, chunk_name)
                    if chunk_samples is not None:
                        # Verify file_duration_sec covers our minute
                        chunk_dur = 60
                        if chunk_meta and 'file_duration_sec' in chunk_meta:
                            chunk_dur = int(chunk_meta['file_duration_sec'])
                        if chunk_dur < dur:
                            continue  # Chunk doesn't actually span this minute

                        sample_rate = int(chunk_meta.get('sample_rate', 24000)) if chunk_meta else 24000
                        samples_per_min = sample_rate * 60
                        offset_sec = minute_timestamp - chunk_boundary
                        offset_samples = offset_sec * sample_rate

                        if offset_samples + samples_per_min <= len(chunk_samples):
                            samples = chunk_samples[offset_samples:offset_samples + samples_per_min].copy()
                            metadata = chunk_meta
                        del chunk_samples
                        if samples is not None:
                            break

                if samples is not None:
                    break
            if samples is not None:
                break

        return samples, metadata

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
