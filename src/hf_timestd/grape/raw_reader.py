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
    
    Reads per-minute complex64 binary files from the data archive.
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
        .bin, .bin.zst, or .bin.lz4 files.
        
        Args:
            date_str: Date string (YYYYMMDD or YYYY-MM-DD)
            
        Returns:
            Sorted list of unix timestamps (minute boundaries)
        """
        if '-' in date_str:
            date_str = date_str.replace('-', '')

        # We do NOT assume that all minutes for a given UTC day live under a
        # single YYYYMMDD directory. In practice, some pipelines can mis-bucket
        # a small number of minutes near day boundaries into the adjacent
        # directory. We defend against this by scanning date-1/date/date+1 and
        # filtering by timestamp.
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
                        # Handle .bin, .bin.zst, .bin.lz4
                        name = f.name
                        if '.bin' in name:
                            stem = name.split('.bin')[0]
                            # Check if stem is integer timestamp
                            if stem.isdigit():
                                ts = int(stem)
                                if day_start_ts <= ts < day_end_ts:
                                    minutes.add(ts)
                    except Exception as e:
                        logger.debug(f"Caught exception: {e}")
                        continue
        
        if not found_any_dir:
            logger.warning(f"No data directory for {date_str} in any search path for {self.channel_name}")
                
        return sorted(list(minutes))

    def read_minute(self, minute_timestamp: int) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read IQ samples and metadata for a specific minute.
        
        Searches all tiered storage locations for the binary file.
        
        Args:
            minute_timestamp: Unix timestamp of the minute start
            
        Returns:
            Tuple of (samples, metadata)
            samples: complex64 numpy array or None
            metadata: dict or None
        """
        dt = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        base_name = str(minute_timestamp)

        # The expected directory is derived from the timestamp, but if a small
        # number of files were mis-bucketed near day boundaries, fall back to
        # adjacent date directories.
        candidate_dates = [
            (dt - timedelta(days=1)).strftime('%Y%m%d'),
            date_str,
            (dt + timedelta(days=1)).strftime('%Y%m%d'),
        ]
        
        # 1. Try to read samples — search all directories
        samples = None
        
        for search_dir in self._search_dirs:
            for date_candidate in candidate_dates:
                day_dir = search_dir / date_candidate
                if not day_dir.exists():
                    continue
                
                # Try uncompressed .bin
                bin_path = day_dir / f"{base_name}.bin"
                if bin_path.exists():
                    try:
                        # Use memmap for efficient reading, but copy to avoid memory accumulation
                        mm = np.memmap(bin_path, dtype=np.complex64, mode='r')
                        samples = np.array(mm, dtype=np.complex64)
                        del mm  # Explicitly release memmap to prevent memory leak
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
                                del data  # Immediately release decompressed buffer
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
                                del data  # Immediately release decompressed buffer
                        except ImportError:
                            logger.warning("lz4 module not installed - cannot read .lz4 files")
                        except Exception as e:
                            logger.error(f"Error reading {lz4_path}: {e}")

                if samples is not None:
                    break

            if samples is not None:
                break  # Found samples, stop searching
        
        # 2. Read metadata — search all directories
        metadata = None
        for search_dir in self._search_dirs:
            for date_candidate in candidate_dates:
                day_dir = search_dir / date_candidate
                json_path = day_dir / f"{base_name}.json"
                if json_path.exists():
                    try:
                        with open(json_path, 'r') as f:
                            metadata = json.load(f)
                        break  # Found metadata, stop searching
                    except Exception as e:
                        logger.warning(f"Error reading metadata {json_path}: {e}")

            if metadata is not None:
                break
        
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
