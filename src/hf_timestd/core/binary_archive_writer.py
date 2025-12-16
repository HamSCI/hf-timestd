#!/usr/bin/env python3
"""
Binary Archive Writer - Simple, robust raw IQ storage

Writes raw complex64 binary files with JSON metadata sidecars.
Designed for maximum reliability - append-only, no HDF5 complexity.

Architecture:
- One binary file per minute per channel
- JSON sidecar with timestamps and metadata
- Memory-mappable for zero-copy Phase 2 reading
- Optional async compression of completed minutes

File structure:
    raw_buffer/{CHANNEL}/YYYYMMDD/
        1765031100.bin      # Raw complex64 samples
        1765031100.json     # Metadata sidecar
        1765031040.bin.zst  # Compressed older minute (optional)
"""

import errno
import json
import logging
import numpy as np
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Constants
SAMPLES_PER_MINUTE = 20000 * 60  # 1,200,000 samples at 20 kHz
BYTES_PER_SAMPLE = 8  # complex64 = 2 x float32


@dataclass
class BinaryArchiveConfig:
    """Configuration for binary archive writer."""
    channel_name: str
    frequency_hz: float
    sample_rate: int = 20000
    output_dir: Path = Path('/tmp/timestd-test/raw_buffer')
    station_config: Dict[str, Any] = field(default_factory=dict)
    compress_completed: bool = False  # Async compression of old minutes
    compression: str = 'none'  # 'none', 'zstd', or 'lz4' - reduces disk I/O by ~2-3x
    compression_level: int = 3  # zstd: 1-22 (3 = good balance), lz4: 1-12
    storage_quota_percent: float = 80.0  # Max disk usage percentage (from config storage_quota)
    use_tiered_storage: bool = False  # Use /dev/shm hot buffer with disk cold storage


@dataclass
class MinuteBuffer:
    """Buffer for accumulating one minute of samples."""
    minute_boundary: int  # Unix timestamp of minute start
    samples: np.ndarray   # Pre-allocated buffer
    write_pos: int = 0    # Current write position
    gap_count: int = 0    # Number of gaps in this minute
    gap_samples: int = 0  # Total gap samples
    start_rtp: Optional[int] = None
    
    @property
    def is_complete(self) -> bool:
        return self.write_pos >= SAMPLES_PER_MINUTE
    
    @property
    def samples_remaining(self) -> int:
        return max(0, SAMPLES_PER_MINUTE - self.write_pos)


class BinaryArchiveWriter:
    """
    Simple binary archive writer for Phase 1 raw IQ data.
    
    Key features:
    - Append-only binary files (cannot fail like HDF5)
    - One file per minute (easy for Phase 2 to read)
    - Memory-mappable output
    - No complex library dependencies
    """
    
    def __init__(self, config: BinaryArchiveConfig):
        self.config = config
        
        # Use tiered storage if enabled (writes to /dev/shm, archives to disk)
        if config.use_tiered_storage:
            from .tiered_storage import get_tiered_storage_manager
            self._tiered_manager = get_tiered_storage_manager()
            self.archive_dir = self._tiered_manager.get_hot_buffer_path(config.channel_name)
        else:
            self._tiered_manager = None
            self.archive_dir = config.output_dir / self._sanitize_channel_name()
        
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        
        # Current minute buffer
        self.current_buffer: Optional[MinuteBuffer] = None
        self._lock = threading.Lock()
        
        # Statistics
        self.minutes_written = 0
        self.samples_written = 0
        self.total_gaps = 0
        self.write_errors = 0
        
        # Time reference - RTP is primary after initialization
        # We establish a one-time mapping from RTP timestamp to Unix time at startup,
        # then derive all minute boundaries from RTP timestamps to avoid wall clock jitter
        self.rtp_to_unix_offset: Optional[float] = None  # unix_time = rtp_timestamp / sample_rate + offset
        self.last_rtp_timestamp: Optional[int] = None
        self.cumulative_samples: int = 0  # Total samples processed
        
        logger.info(f"BinaryArchiveWriter initialized for {config.channel_name}")
        logger.info(f"  Output: {self.archive_dir}")
        logger.info(f"  Format: raw complex64 binary + JSON metadata")
    
    def _sanitize_channel_name(self) -> str:
        """Convert channel name to filesystem-safe format.
        
        Preserves dots in frequency (e.g., CHU_7.85_MHz) for consistency
        with analytics scripts and web UI.
        """
        return self.config.channel_name.replace(' ', '_')
    
    def _get_minute_dir(self, minute_boundary: int) -> Path:
        """Get directory for a specific minute."""
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        day_dir = self.archive_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir
    
    def _start_new_minute(self, rtp_derived_time: float, rtp_timestamp: int) -> MinuteBuffer:
        """Start a new minute buffer.
        
        Args:
            rtp_derived_time: Unix time derived from RTP timestamp (GPSDO-disciplined)
            rtp_timestamp: Raw RTP timestamp for metadata
        """
        minute_boundary = (int(rtp_derived_time) // 60) * 60
        
        buffer = MinuteBuffer(
            minute_boundary=minute_boundary,
            samples=np.zeros(SAMPLES_PER_MINUTE, dtype=np.complex64),
            write_pos=0,
            start_rtp=rtp_timestamp
        )
        
        logger.debug(f"Started new minute buffer: {minute_boundary}")
        return buffer
    
    def _check_disk_space(self, path: Path, required_bytes: int) -> bool:
        """Check if sufficient disk space is available based on storage quota.
        
        Uses the configured storage_quota_percent to determine if we're over quota.
        Also checks for absolute minimum free space (100MB headroom).
        """
        try:
            stat = shutil.disk_usage(path)
            
            # Check storage quota percentage
            current_usage_percent = (stat.used / stat.total) * 100
            if current_usage_percent >= self.config.storage_quota_percent:
                logger.error(
                    f"Storage quota exceeded: {current_usage_percent:.1f}% used, "
                    f"quota is {self.config.storage_quota_percent:.1f}%. "
                    "Run quota manager to free space."
                )
                return False
            
            # Also check absolute minimum free space (100MB headroom)
            min_free = required_bytes + 100 * 1024 * 1024
            if stat.free < min_free:
                logger.error(
                    f"Insufficient disk space: {stat.free / 1024 / 1024:.1f}MB free, "
                    f"need {min_free / 1024 / 1024:.1f}MB"
                )
                return False
            
            return True
        except OSError as e:
            logger.warning(f"Could not check disk space: {e}")
            return True  # Proceed anyway, let write fail if needed
    
    def _cleanup_partial_write(self, *paths: Path) -> None:
        """Clean up partial files after a failed write."""
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
                    logger.debug(f"Cleaned up partial file: {path}")
            except OSError as e:
                logger.warning(f"Failed to clean up {path}: {e}")
    
    def _flush_minute(self, buffer: MinuteBuffer) -> bool:
        """Write completed minute buffer to disk with disk full handling."""
        bin_path = None
        json_path = None
        temp_json = None
        
        try:
            minute_dir = self._get_minute_dir(buffer.minute_boundary)
            
            # Binary file path - extension depends on compression
            compression = self.config.compression.lower()
            if compression == 'zstd':
                bin_path = minute_dir / f"{buffer.minute_boundary}.bin.zst"
            elif compression == 'lz4':
                bin_path = minute_dir / f"{buffer.minute_boundary}.bin.lz4"
            else:
                bin_path = minute_dir / f"{buffer.minute_boundary}.bin"
            json_path = minute_dir / f"{buffer.minute_boundary}.json"
            
            # Write binary data (just the filled portion)
            actual_samples = min(buffer.write_pos, SAMPLES_PER_MINUTE)
            raw_data = buffer.samples[:actual_samples].tobytes()
            
            # Check disk space before writing (raw size + some overhead)
            if not self._check_disk_space(minute_dir, len(raw_data) + 10000):
                self.write_errors += 1
                return False
            
            # Apply compression if configured
            if compression == 'zstd':
                try:
                    import zstandard as zstd
                    # Use multi-threaded compression (threads=-1 = auto-detect cores)
                    # This releases the GIL and distributes work across cores
                    cctx = zstd.ZstdCompressor(level=self.config.compression_level, threads=-1)
                    compressed_data = cctx.compress(raw_data)
                    with open(bin_path, 'wb') as f:
                        f.write(compressed_data)
                    compression_ratio = len(raw_data) / len(compressed_data)
                    logger.debug(f"zstd compression: {len(raw_data)} -> {len(compressed_data)} ({compression_ratio:.1f}x)")
                except ImportError:
                    logger.warning("zstandard not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{buffer.minute_boundary}.bin"
                    buffer.samples[:actual_samples].tofile(bin_path)
            elif compression == 'lz4':
                try:
                    import lz4.frame
                    compressed_data = lz4.frame.compress(raw_data, compression_level=self.config.compression_level)
                    with open(bin_path, 'wb') as f:
                        f.write(compressed_data)
                    compression_ratio = len(raw_data) / len(compressed_data)
                    logger.debug(f"lz4 compression: {len(raw_data)} -> {len(compressed_data)} ({compression_ratio:.1f}x)")
                except ImportError:
                    logger.warning("lz4 not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{buffer.minute_boundary}.bin"
                    buffer.samples[:actual_samples].tofile(bin_path)
            else:
                # No compression - direct write
                buffer.samples[:actual_samples].tofile(bin_path)
            
            # Write metadata sidecar
            metadata = {
                'minute_boundary': buffer.minute_boundary,
                'channel_name': self.config.channel_name,
                'frequency_hz': self.config.frequency_hz,
                'sample_rate': self.config.sample_rate,
                'samples_written': actual_samples,
                'samples_expected': SAMPLES_PER_MINUTE,
                'completeness_pct': 100.0 * actual_samples / SAMPLES_PER_MINUTE,
                'gap_count': buffer.gap_count,
                'gap_samples': buffer.gap_samples,
                'start_rtp_timestamp': buffer.start_rtp,
                'dtype': 'complex64',
                'byte_order': 'little',
                'compression': compression if compression != 'none' else None,
                'written_at': datetime.now(timezone.utc).isoformat(),
                'station': self.config.station_config
            }
            
            # Atomic write: write to temp file, fsync, then rename
            temp_json = json_path.with_suffix('.tmp')
            with open(temp_json, 'w') as f:
                json.dump(metadata, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            temp_json.replace(json_path)
            
            self.minutes_written += 1
            logger.info(
                f"📁 Wrote minute {buffer.minute_boundary}: "
                f"{actual_samples}/{SAMPLES_PER_MINUTE} samples "
                f"({metadata['completeness_pct']:.1f}%) "
                f"[{bin_path.name}]"
            )
            
            return True
            
        except OSError as e:
            # Handle disk full specifically
            if e.errno == errno.ENOSPC:
                logger.error(
                    f"DISK FULL: Failed to write minute {buffer.minute_boundary}. "
                    "Consider freeing disk space or enabling compression."
                )
            else:
                logger.error(f"OS error writing minute {buffer.minute_boundary}: {e}")
            # Clean up any partial files
            self._cleanup_partial_write(bin_path, json_path, temp_json)
            self.write_errors += 1
            return False
        except Exception as e:
            logger.error(f"Failed to write minute {buffer.minute_boundary}: {e}", exc_info=True)
            # Clean up any partial files
            self._cleanup_partial_write(bin_path, json_path, temp_json)
            self.write_errors += 1
            return False
    
    def _rtp_to_unix_time(self, rtp_timestamp: int) -> float:
        """
        Convert RTP timestamp to Unix time using established reference.
        
        After initialization, this derives time from RTP (GPSDO-disciplined)
        rather than wall clock, avoiding NTP-induced jitter.
        """
        if self.rtp_to_unix_offset is None:
            # Not initialized yet - return 0 (will use system_time fallback)
            return 0.0
        return rtp_timestamp / self.config.sample_rate + self.rtp_to_unix_offset
    
    def write_samples(
        self,
        samples: np.ndarray,
        rtp_timestamp: int,
        system_time: Optional[float] = None,
        gap_samples: int = 0
    ) -> int:
        """
        Write IQ samples to the archive.
        
        Args:
            samples: Complex64 IQ samples
            rtp_timestamp: RTP timestamp of first sample
            system_time: System wall clock time (only used for initial sync)
            gap_samples: Number of gap samples (for statistics)
            
        Returns:
            Number of samples written
        """
        with self._lock:
            # Establish RTP-to-Unix reference on first call
            # This is the ONLY time we use system_time - thereafter RTP is primary
            if self.rtp_to_unix_offset is None:
                if system_time is None:
                    system_time = time.time()
                # offset = unix_time - (rtp_timestamp / sample_rate)
                self.rtp_to_unix_offset = system_time - (rtp_timestamp / self.config.sample_rate)
                logger.info(f"RTP-to-Unix reference established: offset={self.rtp_to_unix_offset:.3f}s")
            
            # Ensure complex64
            if samples.dtype != np.complex64:
                samples = samples.astype(np.complex64)
            
            # Determine which minute this belongs to FROM RTP TIMESTAMP (GPSDO-disciplined)
            # This avoids wall clock jitter from NTP/chrony adjustments
            sample_unix_time = self._rtp_to_unix_time(rtp_timestamp)
            sample_minute = (int(sample_unix_time) // 60) * 60
            
            # Start new buffer if needed
            if self.current_buffer is None:
                self.current_buffer = self._start_new_minute(sample_unix_time, rtp_timestamp)
            
            # Check if we've crossed into a new minute
            if sample_minute > self.current_buffer.minute_boundary:
                # Flush current minute
                self._flush_minute(self.current_buffer)
                # Start new minute
                self.current_buffer = self._start_new_minute(sample_unix_time, rtp_timestamp)
            
            # Write to buffer
            buffer = self.current_buffer
            samples_to_write = min(len(samples), buffer.samples_remaining)
            
            if samples_to_write > 0:
                buffer.samples[buffer.write_pos:buffer.write_pos + samples_to_write] = samples[:samples_to_write]
                buffer.write_pos += samples_to_write
                self.samples_written += samples_to_write
            
            # Track gaps
            if gap_samples > 0:
                buffer.gap_count += 1
                buffer.gap_samples += gap_samples
                self.total_gaps += 1
            
            # Update time reference
            self.last_rtp_timestamp = rtp_timestamp
            
            # Check if minute is complete
            if buffer.is_complete:
                self._flush_minute(buffer)
                self.current_buffer = None
            
            return samples_to_write
    
    def flush(self):
        """Flush any pending data to disk."""
        with self._lock:
            if self.current_buffer and self.current_buffer.write_pos > 0:
                self._flush_minute(self.current_buffer)
                self.current_buffer = None
    
    def close(self):
        """Close the writer, flushing any pending data."""
        self.flush()
        logger.info(
            f"BinaryArchiveWriter closed: {self.minutes_written} minutes, "
            f"{self.samples_written} samples, {self.write_errors} errors"
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get writer statistics."""
        return {
            'channel_name': self.config.channel_name,
            'minutes_written': self.minutes_written,
            'samples_written': self.samples_written,
            'total_gaps': self.total_gaps,
            'write_errors': self.write_errors,
            'current_buffer_pos': self.current_buffer.write_pos if self.current_buffer else 0
        }


class BinaryArchiveReader:
    """
    Reader for binary archive files.
    
    Provides memory-mapped access for zero-copy reading by Phase 2.
    """
    
    def __init__(self, archive_dir: Path, channel_name: str):
        # Use channel_name_to_dir for consistent path format (preserves dots)
        from ..paths import channel_name_to_dir
        self.archive_dir = archive_dir / channel_name_to_dir(channel_name)
        self.channel_name = channel_name
        self.sample_rate = 20000
    
    def get_available_minutes(self, date_str: Optional[str] = None) -> List[int]:
        """Get list of available minute boundaries."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
        
        day_dir = self.archive_dir / date_str
        if not day_dir.exists():
            return []
        
        minutes = []
        # Match both uncompressed and compressed files
        for bin_file in day_dir.glob('*.bin*'):
            try:
                # Handle .bin, .bin.zst, .bin.lz4
                stem = bin_file.stem
                if stem.endswith('.bin'):
                    stem = stem[:-4]  # Remove .bin from .bin.zst
                minute = int(stem)
                if minute not in minutes:
                    minutes.append(minute)
            except ValueError:
                pass
        
        return sorted(minutes)
    
    def read_minute(self, minute_boundary: int) -> Optional[np.ndarray]:
        """
        Read samples for a specific minute.
        
        Handles both compressed and uncompressed files.
        Returns numpy array (memory-mapped for uncompressed, loaded for compressed).
        """
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        base_path = self.archive_dir / date_str / f"{minute_boundary}"
        
        # Try uncompressed first (fastest - memory-mappable)
        bin_path = Path(f"{base_path}.bin")
        if bin_path.exists():
            return np.memmap(bin_path, dtype=np.complex64, mode='r')
        
        # Try zstd compressed
        zst_path = Path(f"{base_path}.bin.zst")
        if zst_path.exists():
            try:
                import zstandard as zstd
                with open(zst_path, 'rb') as f:
                    dctx = zstd.ZstdDecompressor()
                    decompressed = dctx.decompress(f.read())
                return np.frombuffer(decompressed, dtype=np.complex64)
            except ImportError:
                logger.warning("zstandard not installed, cannot read .bin.zst files")
                return None
        
        # Try lz4 compressed
        lz4_path = Path(f"{base_path}.bin.lz4")
        if lz4_path.exists():
            try:
                import lz4.frame
                with open(lz4_path, 'rb') as f:
                    decompressed = lz4.frame.decompress(f.read())
                return np.frombuffer(decompressed, dtype=np.complex64)
            except ImportError:
                logger.warning("lz4 not installed, cannot read .bin.lz4 files")
                return None
        
        return None
    
    def read_metadata(self, minute_boundary: int) -> Optional[Dict]:
        """Read metadata for a specific minute."""
        dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
        date_str = dt.strftime('%Y%m%d')
        json_path = self.archive_dir / date_str / f"{minute_boundary}.json"
        
        if not json_path.exists():
            return None
        
        try:
            with open(json_path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Corrupted metadata file {json_path}: {e}")
            return None
    
    def get_latest_complete_minute(self) -> Optional[int]:
        """Get the most recent complete minute boundary."""
        # Current minute is still being written, so go back 1
        current_minute = (int(time.time()) // 60) * 60
        target = current_minute - 60
        
        # Check if it exists
        if self.read_minute(target) is not None:
            return target
        
        # Fall back to scanning
        minutes = self.get_available_minutes()
        if minutes:
            # Return second-to-last (last might be incomplete)
            return minutes[-2] if len(minutes) > 1 else minutes[-1]
        
        return None
