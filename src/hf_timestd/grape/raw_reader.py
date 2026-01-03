"""
Raw Binary Reader - Read raw station data from hf-timestd archive
"""

import numpy as np
import logging
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple, Generator

logger = logging.getLogger(__name__)

class RawBinaryReader:
    """
    Reader for hf-timestd raw binary archive files.
    
    Reads per-minute complex64 binary files from the data archive.
    Supports .bin (raw), .bin.zst (zstd compressed), and .bin.lz4 (lz4 compressed).
    """
    
    def __init__(self, data_root: Path, channel_name: str):
        """
        Initialize reader.
        
        Args:
            data_root: Root data directory (containing raw_archive/)
            channel_name: Channel name (e.g., "WWV 10 MHz")
        """
        self.data_root = Path(data_root)
        self.channel_name = channel_name
        
        # Resolve channel directory
        # hf-timestd converts "WWV 10 MHz" -> "WWV_10_MHz"
        self.channel_dir_name = channel_name.replace(' ', '_')
        
        # Check raw_archive first (Phase 1 storage)
        self.archive_dir = self.data_root / 'raw_archive' / self.channel_dir_name
        
        # Fallback to test paths or direct channel paths if needed
        if not self.archive_dir.exists():
            # Try raw_buffer (for very fresh data or diff config)
            self.archive_dir = self.data_root / 'raw_buffer' / self.channel_dir_name
            
        logger.debug(f"RawBinaryReader initialized for {channel_name} at {self.archive_dir}")

    def get_available_minutes(self, date_str: str) -> List[int]:
        """
        Get list of available minute timestamps for a date.
        
        Args:
            date_str: Date string (YYYYMMDD or YYYY-MM-DD)
            
        Returns:
            Sorted list of unix timestamps (minute boundaries)
        """
        if '-' in date_str:
            date_str = date_str.replace('-', '')
            
        day_dir = self.archive_dir / date_str
        if not day_dir.exists():
            logger.warning(f"No data directory for {date_str} at {day_dir}")
            return []
            
        minutes = set()
        # Scan for binary files
        for f in day_dir.glob('*.bin*'):
            try:
                # Handle .bin, .bin.zst, .bin.lz4
                name = f.name
                if '.bin' in name:
                    stem = name.split('.bin')[0]
                    # Check if stem is integer timestamp
                    if stem.isdigit():
                        minutes.add(int(stem))
            except Exception:
                continue
                
        return sorted(list(minutes))

    def read_minute(self, minute_timestamp: int) -> Tuple[Optional[np.ndarray], Optional[Dict]]:
        """
        Read IQ samples and metadata for a specific minute.
        
        Args:
            minute_timestamp: Unix timestamp of the minute start
            
        Returns:
            Tuple of (samples, metadata)
            samples: complex64 numpy array or None
            metadata: dict or None
        """
        dt = datetime.fromtimestamp(minute_timestamp, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        day_dir = self.archive_dir / date_str
        base_name = str(minute_timestamp)
        
        # 1. Try to read samples
        samples = None
        
        # Try uncompressed .bin
        bin_path = day_dir / f"{base_name}.bin"
        if bin_path.exists():
            try:
                # Use memmap for efficiency with uncompressed files
                samples = np.memmap(bin_path, dtype=np.complex64, mode='r')
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
                        samples = np.frombuffer(data, dtype=np.complex64)
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
                        samples = np.frombuffer(data, dtype=np.complex64)
                except ImportError:
                    logger.warning("lz4 module not installed - cannot read .lz4 files")
                except Exception as e:
                    logger.error(f"Error reading {lz4_path}: {e}")
        
        # 2. Read metadata
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
        """
        minutes = self.get_available_minutes(date_str)
        if not minutes:
            return 24000
            
        _, meta = self.read_minute(minutes[0])
        if meta and 'sample_rate' in meta:
            return int(meta['sample_rate'])
            
        return 24000 
