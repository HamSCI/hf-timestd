#!/usr/bin/env python3
"""
Phase 1: Raw Archive Writer - Immutable 20 kHz IQ Archive

The raw data from the radiod RTP feed must be stored as the definitive,
uncorrected source of truth. This module implements the Phase 1 storage
layer of the three-phase robust time-aligned data pipeline.

Design Principles:
==================
1. CONTAMINATION POLICY: Raw 20 kHz IQ data is NEVER modified or resampled
   based on any subsequent analysis (tone detection, discrimination, etc.)

2. TIME TAGGING: The only temporal reference is the monotonic System Time
   (t_system) provided by the radiod wall clock or derived from the initial
   sample index. NO UTC corrections are applied at this stage.

3. FILE SPLITTING: Files are split based on FIXED DURATION (1 hour) or
   FIXED SIZE (1 GB), NOT based on signal events (tone detection).

4. FORMAT: Digital RF (DRF) - treats the entire dataset as one continuous
   time series, optimized for RF data with HDF5 backend.

5. COMPRESSION: Lossless compression using Shuffle filter + ZSTD/LZ4/Deflate
   to preserve storage space while guaranteeing data integrity.

This phase produces the immutable archive that Phase 2 (Analytical Engine)
reads to generate the Clock Offset Series (D_clock).

Usage:
------
    writer = RawArchiveWriter(
        output_dir=Path('/data/raw_archive'),
        channel_name='WWV_10MHz',
        frequency_hz=10e6,
        sample_rate=20000,
        station_config={'callsign': 'W3PM', 'grid_square': 'EM38ww'}
    )
    
    writer.write_samples(iq_samples, rtp_timestamp, system_time)
    writer.flush()
"""

import numpy as np
import logging
import time
import uuid
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass, field
from collections import deque
import json
import threading
import os

logger = logging.getLogger(__name__)

# Data quality constants
# For 32-bit float IQ from radiod, typical values are -1.0 to +1.0
# Values above 100 are suspicious, above 1e6 almost certainly bad
MAX_SAMPLE_VALUE = 100.0  # Flag samples exceeding this as suspicious
MAX_SAMPLE_VALUE_CRITICAL = 1e6  # Samples above this are likely decode errors
MAX_GAP_SAMPLES_WARNING = 20000  # Warn if gap exceeds 1 second at 20kHz
NTP_SYNC_CHECK_INTERVAL = 60  # Check NTP status every 60 seconds

# Resilience constants
FORCED_FLUSH_INTERVAL_SEC = 60  # Force flush at least every 60 seconds
WRITE_WATCHDOG_TIMEOUT_SEC = 120  # Recreate writer if no writes for 2 minutes
MAX_GAP_BEFORE_WRITER_RESET = 400000  # Reset writer if gap > 20 seconds at 20kHz

# Storage quota constants
DEFAULT_QUOTA_GB = 100  # Default storage quota per channel
QUOTA_CHECK_INTERVAL = 300  # Check quota every 5 minutes
QUOTA_HEADROOM_RATIO = 0.05  # Remove extra 5% when cleaning to avoid thrashing

# Try to import Digital RF
try:
    import digital_rf as drf
    DRF_AVAILABLE = True
except ImportError:
    DRF_AVAILABLE = False
    logger.warning("digital_rf not available - raw archive writing disabled")

# Compression options for HDF5
COMPRESSION_OPTIONS = {
    'zstd': {'compression': 'zstd', 'compression_opts': 3},  # Fast, good ratio
    'lz4': {'compression': 'lz4'},  # Very fast, moderate ratio
    'gzip': {'compression': 'gzip', 'compression_opts': 6},  # Compatible, good ratio
    'none': {}
}


@dataclass
class RawArchiveConfig:
    """
    Configuration for raw archive writer.
    
    Attributes:
        output_dir: Base directory for raw archive
        channel_name: Channel identifier (e.g., 'WWV_10MHz')
        frequency_hz: Center frequency in Hz
        sample_rate: Sample rate in Hz (default 20000)
        station_config: Station metadata (callsign, grid, etc.)
        file_duration_sec: Fixed file duration in seconds (default 3600 = 1 hour)
        max_file_size_bytes: Maximum file size (default 1GB)
        compression: Compression algorithm ('zstd', 'lz4', 'gzip', 'none')
        use_shuffle: Use HDF5 shuffle filter (improves compression)
    """
    output_dir: Path
    channel_name: str
    frequency_hz: float
    sample_rate: int = 20000
    station_config: Dict[str, Any] = field(default_factory=dict)
    
    # File splitting policy - FIXED DURATION, NOT EVENT-BASED
    file_duration_sec: int = 60  # 60 second files for real-time Phase 2 access
    max_file_size_bytes: int = 1_073_741_824  # 1 GB
    
    # Compression settings
    compression: str = 'gzip'  # 'zstd', 'lz4', 'gzip', 'none'
    use_shuffle: bool = True  # Shuffle filter improves compression
    
    # DRF-specific
    subdir_cadence_secs: int = 86400  # Daily subdirectories
    file_cadence_millisecs: int = 60000  # 60 second file cadence for real-time access
    # Note: Storage quota is managed at top-level by StorageQuotaManager
    
    def __post_init__(self):
        self.output_dir = Path(self.output_dir)


@dataclass
class NTPStatus:
    """NTP synchronization status for provenance."""
    synced: bool = False
    offset_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    stratum: Optional[int] = None
    reference: Optional[str] = None
    check_time: Optional[float] = None
    
    def to_dict(self) -> Dict:
        return {
            'synced': self.synced,
            'offset_ms': self.offset_ms,
            'jitter_ms': self.jitter_ms,
            'stratum': self.stratum,
            'reference': self.reference,
            'check_time': self.check_time
        }


def check_ntp_status() -> NTPStatus:
    """Check system NTP synchronization status."""
    status = NTPStatus(check_time=time.time())
    try:
        # Try chronyc first (common on modern systems)
        result = subprocess.run(
            ['chronyc', 'tracking'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Reference ID' in line:
                    status.reference = line.split(':')[1].strip().split()[0]
                elif 'Stratum' in line:
                    status.stratum = int(line.split(':')[1].strip())
                elif 'System time' in line:
                    # Parse offset like "0.000001234 seconds fast"
                    parts = line.split(':')[1].strip().split()
                    if len(parts) >= 2:
                        status.offset_ms = float(parts[0]) * 1000
                        if 'slow' in line:
                            status.offset_ms = -status.offset_ms
                elif 'Root delay' in line:
                    pass  # Could extract jitter from here
            status.synced = status.stratum is not None and status.stratum > 0
            return status
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    try:
        # Fall back to ntpq
        result = subprocess.run(
            ['ntpq', '-c', 'rv'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            status.synced = 'sync_ntp' in result.stdout or 'sync_pps' in result.stdout
            return status
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    
    return status


@dataclass
class SystemTimeReference:
    """
    System time reference for raw archive - NO UTC CORRECTIONS.
    
    This is the ONLY time reference stored in Phase 1.
    The relationship to UTC is calculated in Phase 2.
    
    Attributes:
        rtp_timestamp: RTP timestamp (sample counter from radiod)
        system_time: Local system wall clock time (seconds since epoch)
        ntp_offset_ms: NTP offset at time of recording (if known)
        sample_rate: Sample rate for RTP→time conversion
        ntp_status: Full NTP status at time of recording
    """
    rtp_timestamp: int
    system_time: float
    ntp_offset_ms: Optional[float] = None
    sample_rate: int = 20000
    ntp_status: Optional[NTPStatus] = None
    
    def calculate_time_at_sample(self, sample_rtp: int) -> float:
        """Calculate system time at a given RTP sample index."""
        # Handle 32-bit RTP wraparound
        rtp_diff = sample_rtp - self.rtp_timestamp
        if rtp_diff > 0x80000000:
            rtp_diff -= 0x100000000
        elif rtp_diff < -0x80000000:
            rtp_diff += 0x100000000
        
        elapsed_seconds = rtp_diff / self.sample_rate
        return self.system_time + elapsed_seconds
    
    def to_dict(self) -> Dict:
        return {
            'rtp_timestamp': self.rtp_timestamp,
            'system_time': self.system_time,
            'ntp_offset_ms': self.ntp_offset_ms,
            'sample_rate': self.sample_rate
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> 'SystemTimeReference':
        return cls(
            rtp_timestamp=d['rtp_timestamp'],
            system_time=d['system_time'],
            ntp_offset_ms=d.get('ntp_offset_ms'),
            sample_rate=d.get('sample_rate', 20000)
        )


@dataclass
class ArchiveSegmentInfo:
    """Information about an archive segment (for provenance)."""
    segment_id: int
    start_system_time: float
    start_rtp_timestamp: int
    sample_count: int
    gap_count: int = 0
    gap_samples: int = 0
    file_path: Optional[Path] = None


def parse_quota_string(quota_str: str, base_path: Path) -> Optional[int]:
    """
    Parse a quota string into bytes.
    
    Supported formats:
        - "500GB", "500gb", "500 GB" - fixed size in gigabytes
        - "1TB", "1.5TB" - fixed size in terabytes  
        - "80%", "50%" - percentage of disk containing base_path
        - "unlimited", "", None - no limit
    
    Args:
        quota_str: Quota specification string
        base_path: Path to determine disk for percentage calculation
        
    Returns:
        Quota in bytes, or None for unlimited
    """
    if not quota_str or quota_str.lower() in ('unlimited', 'none', '0'):
        return None
    
    quota_str = quota_str.strip().upper()
    
    # Percentage of disk
    if quota_str.endswith('%'):
        try:
            percentage = float(quota_str[:-1])
            if not 0 < percentage <= 100:
                raise ValueError(f"Percentage must be 0-100, got {percentage}")
            
            # Get disk stats
            import shutil
            disk_usage = shutil.disk_usage(base_path)
            quota_bytes = int(disk_usage.total * percentage / 100)
            
            logger.info(f"Quota {quota_str}: {quota_bytes / 1024**3:.1f} GB "
                       f"of {disk_usage.total / 1024**3:.1f} GB disk")
            return quota_bytes
            
        except ValueError as e:
            logger.error(f"Invalid percentage quota '{quota_str}': {e}")
            return None
    
    # Fixed size: extract number and unit
    import re
    match = re.match(r'^([\d.]+)\s*(TB|GB|MB)?$', quota_str)
    if not match:
        logger.error(f"Invalid quota format '{quota_str}'. "
                    f"Use: '500GB', '1TB', '80%', or 'unlimited'")
        return None
    
    value = float(match.group(1))
    unit = match.group(2) or 'GB'
    
    multipliers = {
        'MB': 1024**2,
        'GB': 1024**3,
        'TB': 1024**4,
    }
    
    quota_bytes = int(value * multipliers[unit])
    logger.info(f"Quota: {value} {unit} = {quota_bytes / 1024**3:.1f} GB")
    return quota_bytes


class StorageQuotaManager:
    """
    Manages storage quota for raw archive data across ALL channels.
    
    Automatically removes oldest data when quota is exceeded,
    using a FIFO (first-in-first-out) policy based on date directories.
    
    The manager looks at ALL channel directories under the archive root
    and removes the oldest date directories across all channels.
    
    Usage:
        manager = StorageQuotaManager(archive_root, quota="80%")
        manager = StorageQuotaManager(archive_root, quota="500GB")
        manager.enforce_quota()  # Called periodically
    """
    
    def __init__(
        self,
        archive_root: Path,
        quota: Optional[str] = None,
        headroom_ratio: float = QUOTA_HEADROOM_RATIO
    ):
        """
        Initialize storage quota manager.
        
        Args:
            archive_root: Root directory containing all channel archives
            quota: Quota specification ("500GB", "80%", "unlimited")
            headroom_ratio: Extra space to free when cleaning (default 5%)
        """
        self.archive_root = Path(archive_root)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        
        self.quota_bytes = parse_quota_string(quota, self.archive_root) if quota else None
        self.headroom_ratio = headroom_ratio
        
        # Statistics
        self.bytes_removed: int = 0
        self.dirs_removed: int = 0
        self.last_check_time: float = 0
        self.removal_log: List[Dict] = []
        
        if self.quota_bytes:
            logger.info(f"StorageQuotaManager: {self.quota_bytes / 1024**3:.1f} GB quota for {archive_root}")
        else:
            logger.info(f"StorageQuotaManager: unlimited storage for {archive_root}")
    
    def get_storage_usage(self) -> Tuple[int, List[Tuple[Path, str, int]]]:
        """
        Calculate current storage usage across ALL channels.
        
        Returns:
            Tuple of (total_bytes, list of (dir_path, date_str, size_bytes) sorted oldest first)
        """
        if not self.archive_root.exists():
            return 0, []
        
        date_dirs = []  # List of (full_path, date_str, size_bytes)
        total_bytes = 0
        
        # Scan all channel directories under archive_root
        # Structure: archive_root/raw_archive/CHANNEL_NAME/YYYYMMDD/
        raw_archive = self.archive_root / 'raw_archive'
        if not raw_archive.exists():
            return 0, []
        
        for channel_dir in raw_archive.iterdir():
            if not channel_dir.is_dir():
                continue
            
            # Find date directories in each channel
            for item in channel_dir.iterdir():
                if item.is_dir() and len(item.name) == 8 and item.name.isdigit():
                    dir_size = self._get_dir_size(item)
                    date_dirs.append((item, item.name, dir_size))
                    total_bytes += dir_size
            
            # Include metadata directory in total
            metadata_dir = channel_dir / 'metadata'
            if metadata_dir.exists():
                total_bytes += self._get_dir_size(metadata_dir)
        
        # Sort by date (oldest first for FIFO removal)
        date_dirs.sort(key=lambda x: x[1])
        
        return total_bytes, date_dirs
    
    def _get_dir_size(self, path: Path) -> int:
        """Calculate total size of directory recursively."""
        total = 0
        try:
            for item in path.rglob('*'):
                if item.is_file():
                    total += item.stat().st_size
        except (PermissionError, OSError) as e:
            logger.warning(f"Error calculating size of {path}: {e}")
        return total
    
    def enforce_quota(self, force: bool = False) -> Dict[str, Any]:
        """
        Enforce storage quota by removing oldest data if needed.
        
        Removes oldest date directories across ALL channels until
        storage is below quota (with headroom).
        
        Args:
            force: If True, check even if recently checked
            
        Returns:
            Dict with enforcement results
        """
        if self.quota_bytes is None:
            return {'action': 'none', 'reason': 'unlimited_quota'}
        
        current_time = time.time()
        self.last_check_time = current_time
        
        total_bytes, date_dirs = self.get_storage_usage()
        
        result = {
            'total_bytes': total_bytes,
            'quota_bytes': self.quota_bytes,
            'usage_ratio': total_bytes / self.quota_bytes if self.quota_bytes else 0,
            'removed_dirs': [],
            'bytes_freed': 0,
        }
        
        if total_bytes <= self.quota_bytes:
            result['action'] = 'none'
            result['reason'] = 'within_quota'
            return result
        
        # Calculate target size (with headroom)
        target_bytes = int(self.quota_bytes * (1 - self.headroom_ratio))
        bytes_to_free = total_bytes - target_bytes
        
        logger.warning(f"⚠️ Storage quota exceeded: {total_bytes / 1024**3:.2f} GB / "
                      f"{self.quota_bytes / 1024**3:.2f} GB")
        logger.info(f"   Freeing {bytes_to_free / 1024**3:.2f} GB to reach "
                   f"{target_bytes / 1024**3:.2f} GB target")
        
        bytes_freed = 0
        dirs_removed = []
        
        # Remove oldest directories first (FIFO) - across all channels
        today_str = datetime.now().strftime('%Y%m%d')
        
        for dir_path, date_str, dir_size in date_dirs:
            if bytes_freed >= bytes_to_free:
                break
            
            # Safety check: never remove today's directory
            if date_str >= today_str:
                logger.warning(f"   Skipping {dir_path.name} (current/future date)")
                continue
            
            # Remove the directory
            try:
                channel_name = dir_path.parent.name
                self._remove_directory(dir_path)
                bytes_freed += dir_size
                dirs_removed.append(f"{channel_name}/{date_str}")
                
                # Log removal for audit trail
                removal_record = {
                    'channel': channel_name,
                    'date': date_str,
                    'path': str(dir_path),
                    'size_bytes': dir_size,
                    'removed_at': datetime.now(tz=timezone.utc).isoformat(),
                    'reason': 'quota_enforcement'
                }
                self.removal_log.append(removal_record)
                
                logger.info(f"   ✓ Removed {channel_name}/{date_str} ({dir_size / 1024**2:.1f} MB)")
                
            except Exception as e:
                logger.error(f"   ✗ Failed to remove {dir_path}: {e}")
        
        # Update statistics
        self.bytes_removed += bytes_freed
        self.dirs_removed += len(dirs_removed)
        
        result['action'] = 'cleaned'
        result['removed_dirs'] = dirs_removed
        result['bytes_freed'] = bytes_freed
        
        # Log summary
        new_total = total_bytes - bytes_freed
        logger.info(f"   Freed {bytes_freed / 1024**3:.2f} GB, "
                   f"new usage: {new_total / 1024**3:.2f} GB "
                   f"({new_total / self.quota_bytes * 100:.1f}%)")
        
        return result
    
    def _remove_directory(self, path: Path):
        """Safely remove a directory and all its contents."""
        import shutil
        
        if not path.exists():
            return
        
        # Safety checks
        if not path.is_dir():
            raise ValueError(f"Not a directory: {path}")
        
        # Ensure we're removing from within archive_root
        try:
            path.relative_to(self.archive_root)
        except ValueError:
            raise ValueError(f"Path {path} is not within archive root")
        
        # Remove
        shutil.rmtree(path)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get quota manager statistics."""
        total_bytes, date_dirs = self.get_storage_usage()
        
        # Get unique dates across all channels
        unique_dates = sorted(set(d[1] for d in date_dirs))
        
        return {
            'quota_gb': self.quota_bytes / 1024**3 if self.quota_bytes else None,
            'used_gb': total_bytes / 1024**3,
            'usage_ratio': total_bytes / self.quota_bytes if self.quota_bytes else 0,
            'date_dirs_count': len(date_dirs),
            'unique_dates': len(unique_dates),
            'oldest_date': unique_dates[0] if unique_dates else None,
            'newest_date': unique_dates[-1] if unique_dates else None,
            'bytes_removed_total': self.bytes_removed,
            'dirs_removed_total': self.dirs_removed,
        }
    
    def write_removal_log(self, log_file: Optional[Path] = None):
        """Write removal log to file for audit purposes."""
        if not self.removal_log:
            return
        
        if log_file is None:
            log_file = self.archive_root / 'quota_removal_log.json'
        
        log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Append to existing log
        existing_log = []
        if log_file.exists():
            try:
                with open(log_file) as f:
                    existing_log = json.load(f)
            except:
                pass
        
        existing_log.extend(self.removal_log)
        
        with open(log_file, 'w') as f:
            json.dump(existing_log, f, indent=2)
        
        self.removal_log.clear()


class RawArchiveWriter:
    """
    Phase 1: Immutable Raw Archive Writer
    
    Writes 20 kHz IQ samples to Digital RF format with:
    - System time tagging ONLY (no UTC corrections)
    - Fixed-duration file splitting (NOT event-based)
    - Lossless compression (Shuffle + ZSTD/LZ4/gzip)
    - Complete provenance metadata
    
    The output is the definitive, uncorrected source of truth for
    all subsequent analysis (Phase 2) and product generation (Phase 3).
    """
    
    def __init__(self, config: RawArchiveConfig):
        """
        Initialize raw archive writer.
        
        Args:
            config: RawArchiveConfig with all settings
        """
        if not DRF_AVAILABLE:
            raise ImportError(
                "digital_rf package required for raw archive writing. "
                "Install with: pip install digital_rf"
            )
        
        self.config = config
        self._lock = threading.Lock()
        
        # Create output directory structure
        # output_dir is expected to be the raw_archive root (e.g., /data/raw_archive/)
        # We add only the channel name subdirectory
        self.archive_dir = config.output_dir / config.channel_name.replace(' ', '_')
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        
        # Metadata directory
        self.metadata_dir = self.archive_dir / 'metadata'
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        
        # DRF writer state
        self.drf_writer: Optional[drf.DigitalRFWriter] = None
        self.metadata_writer: Optional[drf.DigitalMetadataWriter] = None
        self.dataset_uuid = uuid.uuid4().hex
        
        # Time reference (system time ONLY)
        self.system_time_ref: Optional[SystemTimeReference] = None
        self.current_day: Optional[datetime] = None
        
        # Monotonic sample index for DRF
        self.next_sample_index: Optional[int] = None
        
        # Statistics
        self.samples_written: int = 0
        self.files_written: int = 0
        self.total_gap_samples: int = 0
        self.session_start_time: float = time.time()
        
        # Data quality tracking
        self.data_quality = {
            'samples_validated': 0,
            'samples_with_nan': 0,
            'samples_with_inf': 0,
            'samples_clipped': 0,
            'max_sample_seen': 0.0,
            'min_sample_seen': 0.0,
            'gaps_detected': 0,
            'largest_gap_samples': 0,
            'late_packets_dropped': 0,
            'write_errors': 0,
            'dtype_conversions': 0,
            'forced_flushes': 0,
            'watchdog_resets': 0,
        }
        
        # Resilience tracking
        self.last_flush_time: float = time.time()
        self.last_write_time: float = time.time()
        self.last_heartbeat_log: float = time.time()
        self._recovery_start_time: Optional[float] = None  # Set after write errors
        self._consecutive_errors: int = 0  # Track errors before full reset
        self._max_errors_before_reset: int = 5  # Only reset after multiple failures
        
        # RTP sequence tracking for gap detection
        self.last_rtp_timestamp: Optional[int] = None
        self.expected_samples_per_packet: int = 31  # F32 at 20kHz
        
        # NTP status tracking
        self.ntp_status: Optional[NTPStatus] = None
        self.last_ntp_check: float = 0
        self._check_ntp_on_init()
        
        # Segment tracking
        self.current_segment: Optional[ArchiveSegmentInfo] = None
        self.segment_counter: int = 0
        
        # Sliding window monitor (10-second real-time quality tracking)
        # DISABLED: All analytics should be post-processing in Phase 2
        # Real-time analysis was causing 244% CPU usage for 1.44 MB/sec data
        self.sliding_monitor = None
        self.sliding_monitor_enabled = False  # Disabled for performance
        self.window_sample_buffer: List[np.ndarray] = []  # Accumulate for 10s windows
        self.window_sample_count = 0
        self.window_gap_count = 0
        self.window_gap_samples = 0
        self.samples_per_window = config.sample_rate * 10  # 10 seconds
        self._init_sliding_monitor()
        
        logger.info(f"RawArchiveWriter initialized for {config.channel_name}")
        logger.info(f"  Output: {self.archive_dir}")
        logger.info(f"  Sample rate: {config.sample_rate} Hz")
        logger.info(f"  File duration: {config.file_duration_sec}s")
        logger.info(f"  Compression: {config.compression} (shuffle={config.use_shuffle})")
    
    def _init_sliding_monitor(self):
        """Initialize the 10-second sliding window monitor."""
        if not self.sliding_monitor_enabled:
            return
        
        try:
            from .sliding_window_monitor import SlidingWindowMonitor
            
            # Output to status directory alongside raw archive
            status_dir = self.config.output_dir / 'status'
            status_dir.mkdir(parents=True, exist_ok=True)
            
            self.sliding_monitor = SlidingWindowMonitor(
                channel_name=self.config.channel_name,
                sample_rate=self.config.sample_rate,
                output_dir=status_dir,
                enabled=True
            )
            logger.info(f"  Sliding window monitor: ENABLED (10s windows)")
        except ImportError as e:
            logger.warning(f"  Sliding window monitor: DISABLED ({e})")
            self.sliding_monitor = None
        except Exception as e:
            logger.warning(f"  Sliding window monitor initialization failed: {e}")
            self.sliding_monitor = None
    
    def _process_sliding_window(self, system_time: float):
        """
        Process accumulated samples through the 10-second sliding window monitor.
        
        Called when we've accumulated enough samples for a complete window.
        """
        if self.sliding_monitor is None or not self.sliding_monitor_enabled:
            return
        
        if self.window_sample_count < self.samples_per_window:
            return
        
        try:
            # Concatenate buffered samples
            if self.window_sample_buffer:
                window_samples = np.concatenate(self.window_sample_buffer)
                
                # Trim to exact window size
                window_samples = window_samples[:self.samples_per_window]
                
                # Calculate timestamp for window start (10 seconds ago)
                window_timestamp = system_time - 10.0
                
                # Process through monitor
                gap_info = {
                    'gap_count': self.window_gap_count,
                    'gap_samples': self.window_gap_samples
                }
                
                metrics = self.sliding_monitor.process_chunk(
                    samples=window_samples,
                    timestamp=window_timestamp,
                    gap_info=gap_info
                )
                
                if metrics and metrics.signal_present:
                    logger.debug(
                        f"{self.config.channel_name} 10s monitor: "
                        f"SNR={metrics.dominant_snr_db:.1f}dB, "
                        f"quality={metrics.quality.value}"
                    )
            
            # Reset window buffer, keeping overflow for next window
            overflow_samples = self.window_sample_count - self.samples_per_window
            if overflow_samples > 0 and self.window_sample_buffer:
                # Keep the overflow portion
                all_samples = np.concatenate(self.window_sample_buffer)
                self.window_sample_buffer = [all_samples[self.samples_per_window:]]
                self.window_sample_count = overflow_samples
            else:
                self.window_sample_buffer = []
                self.window_sample_count = 0
            
            self.window_gap_count = 0
            self.window_gap_samples = 0
            
        except Exception as e:
            logger.warning(f"Sliding window processing error: {e}")
            # Reset on error
            self.window_sample_buffer = []
            self.window_sample_count = 0
    
    def _check_ntp_on_init(self):
        """Check NTP status at initialization and log warnings."""
        self.ntp_status = check_ntp_status()
        self.last_ntp_check = time.time()
        
        if self.ntp_status.synced:
            logger.info(f"  NTP: SYNCED (stratum={self.ntp_status.stratum}, "
                       f"offset={self.ntp_status.offset_ms:.3f}ms)")
        else:
            logger.warning("⚠️ NTP NOT SYNCED - system time may be inaccurate!")
            logger.warning("   Phase 2 D_clock calculations depend on accurate time")
    
    def _validate_samples(self, samples: np.ndarray) -> Tuple[np.ndarray, Dict[str, int]]:
        """
        Validate and clean IQ samples before writing.
        
        Returns:
            Tuple of (cleaned_samples, quality_metrics)
        """
        metrics = {
            'nan_count': 0,
            'inf_count': 0,
            'clipped_count': 0,
        }
        
        # Check for NaN values
        nan_mask = np.isnan(samples)
        if np.any(nan_mask):
            metrics['nan_count'] = int(np.sum(nan_mask))
            # Replace NaN with zero (preserves sample count)
            samples = np.where(nan_mask, 0+0j, samples)
            logger.warning(f"⚠️ {metrics['nan_count']} NaN samples replaced with zero")
        
        # Check for Inf values
        inf_mask = np.isinf(samples)
        if np.any(inf_mask):
            metrics['inf_count'] = int(np.sum(inf_mask))
            # Replace Inf with zero
            samples = np.where(inf_mask, 0+0j, samples)
            logger.warning(f"⚠️ {metrics['inf_count']} Inf samples replaced with zero")
        
        # Check for clipped/suspicious values
        abs_samples = np.abs(samples)
        max_val = np.max(abs_samples) if len(abs_samples) > 0 else 0
        if max_val > MAX_SAMPLE_VALUE:
            metrics['clipped_count'] = int(np.sum(abs_samples > MAX_SAMPLE_VALUE))
            logger.warning(f"⚠️ {metrics['clipped_count']} samples exceed {MAX_SAMPLE_VALUE}")
        
        # Track min/max for provenance
        if len(abs_samples) > 0:
            self.data_quality['max_sample_seen'] = max(
                self.data_quality['max_sample_seen'], float(max_val)
            )
            min_val = np.min(abs_samples)
            if self.data_quality['min_sample_seen'] == 0:
                self.data_quality['min_sample_seen'] = float(min_val)
            else:
                self.data_quality['min_sample_seen'] = min(
                    self.data_quality['min_sample_seen'], float(min_val)
                )
        
        return samples, metrics
    
    def _detect_gap(self, rtp_timestamp: int, num_samples: int) -> int:
        """
        Detect gaps in RTP stream based on timestamp discontinuity.
        
        Returns:
            Number of gap samples (0 if no gap)
        """
        if self.last_rtp_timestamp is None:
            self.last_rtp_timestamp = rtp_timestamp
            return 0
        
        # Calculate expected vs actual RTP timestamp
        expected_rtp = self.last_rtp_timestamp + num_samples
        
        # Handle 32-bit wraparound
        rtp_diff = rtp_timestamp - expected_rtp
        if rtp_diff > 0x80000000:
            rtp_diff -= 0x100000000
        elif rtp_diff < -0x80000000:
            rtp_diff += 0x100000000
        
        # Update last timestamp
        self.last_rtp_timestamp = rtp_timestamp + num_samples
        
        if rtp_diff > 0:
            # Gap detected - missing samples
            gap_samples = rtp_diff
            self.data_quality['gaps_detected'] += 1
            self.data_quality['largest_gap_samples'] = max(
                self.data_quality['largest_gap_samples'], gap_samples
            )
            
            if gap_samples > MAX_GAP_SAMPLES_WARNING:
                gap_duration_ms = (gap_samples / self.config.sample_rate) * 1000
                logger.warning(f"⚠️ Large gap detected: {gap_samples} samples ({gap_duration_ms:.1f}ms)")
            
            return gap_samples
        elif rtp_diff < -num_samples:
            # Significant backwards jump - late/duplicate packet
            self.data_quality['late_packets_dropped'] += 1
            return -1  # Signal to skip this packet
        
        return 0
    
    def _create_drf_writer(self, system_time: float, rtp_timestamp: int):
        """
        Create Digital RF writer for current day.
        
        File splitting is based on FIXED DURATION, not signal events.
        
        Args:
            system_time: System wall clock time
            rtp_timestamp: RTP timestamp for sample index calculation
        """
        dt = datetime.fromtimestamp(system_time, tz=timezone.utc)
        day_date = dt.date()
        
        # Close existing writer if day changed
        if self.current_day and self.current_day != day_date:
            logger.info(f"Day boundary: closing previous DRF writer")
            self._close_writer()
        
        if self.drf_writer is not None:
            return  # Already have writer for this day
        
        # Build directory structure: raw_archive/CHANNEL/YYYYMMDD/
        date_str = day_date.strftime('%Y%m%d')
        drf_dir = self.archive_dir / date_str
        drf_dir.mkdir(parents=True, exist_ok=True)
        
        # Calculate start_global_index from system time
        # This is samples since Unix epoch at sample_rate
        start_global_index = int(system_time * self.config.sample_rate)
        
        # === RESILIENCE: Use recovery time if set (after write error) ===
        if hasattr(self, '_recovery_start_time') and self._recovery_start_time is not None:
            recovery_index = int(self._recovery_start_time * self.config.sample_rate)
            if recovery_index > start_global_index:
                logger.info(f"Using recovery start time: skipping from {start_global_index} to {recovery_index}")
                start_global_index = recovery_index
            self._recovery_start_time = None  # Clear after use
        
        # === RESILIENCE: Check for existing data and skip ahead if needed ===
        # On restart, there may be files from a previous run at the same timestamp
        # DRF doesn't allow overwriting, so we must start after existing data
        try:
            existing_end = self._find_existing_data_end(drf_dir)
            if existing_end is not None and existing_end >= start_global_index:
                # Existing data covers our start point - skip to NEXT minute boundary
                # Files are created at 60-second boundaries, so align to next minute
                existing_end_time = existing_end / self.config.sample_rate
                next_minute = ((int(existing_end_time) // 60) + 1) * 60 + 1  # +1 sec safety
                new_start = int(next_minute * self.config.sample_rate)
                logger.warning(
                    f"⚠️ Existing DRF data found up to index {existing_end}. "
                    f"Skipping to next minute boundary: {new_start} (time={next_minute})"
                )
                start_global_index = new_start
        except Exception as e:
            logger.debug(f"Could not check existing data: {e}")
        
        # Initialize next_sample_index if first write
        if self.next_sample_index is None:
            self.next_sample_index = start_global_index
        
        logger.info(f"Creating DRF writer for {day_date}")
        logger.info(f"  Directory: {drf_dir}")
        logger.info(f"  start_global_index: {start_global_index}")
        
        # Get compression level
        compression_level = 6 if self.config.compression == 'gzip' else 0
        
        # Create Digital RF writer
        # CRITICAL: dtype must match input data (complex64 for IQ)
        self.drf_writer = drf.DigitalRFWriter(
            str(drf_dir),
            dtype=np.complex64,
            subdir_cadence_secs=self.config.subdir_cadence_secs,
            file_cadence_millisecs=self.config.file_cadence_millisecs,
            start_global_index=start_global_index,
            sample_rate_numerator=self.config.sample_rate,
            sample_rate_denominator=1,
            uuid_str=self.dataset_uuid,
            compression_level=compression_level,
            checksum=False,
            is_complex=True,
            num_subchannels=1,
            is_continuous=True,  # We fill gaps with zeros
            marching_periods=False
        )
        
        # Create metadata writer for provenance
        metadata_subdir = drf_dir / 'metadata'
        metadata_subdir.mkdir(parents=True, exist_ok=True)
        
        self.metadata_writer = drf.DigitalMetadataWriter(
            str(metadata_subdir),
            subdir_cadence_secs=self.config.subdir_cadence_secs,
            file_cadence_secs=3600,  # Hourly metadata files
            sample_rate_numerator=self.config.sample_rate,
            sample_rate_denominator=1,
            file_name='raw_archive_metadata'
        )
        
        # Write initial metadata
        station_config = self.config.station_config
        metadata = {
            'archive_type': 'raw_20khz_iq',
            'phase': 'phase1_immutable',
            'channel_name': self.config.channel_name,
            'frequency_hz': self.config.frequency_hz,
            'sample_rate': float(self.config.sample_rate),
            'callsign': station_config.get('callsign', 'UNKNOWN'),
            'grid_square': station_config.get('grid_square', 'UNKNOWN'),
            'uuid_str': self.dataset_uuid,
            'date': day_date.isoformat(),
            'compression': self.config.compression,
            'created_at': datetime.now(tz=timezone.utc).isoformat(),
            # CRITICAL: This is system time only, NOT UTC-corrected
            'time_reference': 'system_time_only',
            'utc_correction_applied': False
        }
        self.metadata_writer.write(start_global_index, metadata)
        
        self.current_day = day_date
        self.files_written += 1
        
        logger.info(f"✅ DRF writer ready (system time reference only)")
    
    def _close_writer(self):
        """Close current Digital RF writer and flush data."""
        if self.drf_writer:
            try:
                self.drf_writer.close()
            except Exception as e:
                logger.warning(f"Error closing DRF writer: {e}")
            self.drf_writer = None
            self.metadata_writer = None
            logger.info("DRF writer closed")
    
    def _cleanup_conflicting_files(self):
        """
        Remove temp files and recent HDF5 files that may conflict with recovery.
        
        Called after write errors to allow fresh start.
        """
        try:
            from datetime import datetime, timezone
            day_date = datetime.now(tz=timezone.utc).date()
            date_str = day_date.strftime('%Y%m%d')
            drf_dir = self.archive_dir / date_str
            
            if not drf_dir.exists():
                return
            
            # Find and remove temp files
            for subdir in drf_dir.iterdir():
                if subdir.is_dir():
                    for f in subdir.glob('tmp.*.h5'):
                        try:
                            f.unlink()
                            logger.info(f"Removed temp file: {f.name}")
                        except Exception:
                            pass
                    
                    # Remove files in current minute boundary and future
                    # DRF uses 60-second file cadence, so delete current + next minute
                    current_time = time.time()
                    current_minute = (int(current_time) // 60) * 60
                    for f in subdir.glob('rf@*.h5'):
                        try:
                            # Extract timestamp from filename: rf@TIMESTAMP.000.h5
                            ts_str = f.stem.split('@')[1].split('.')[0]
                            file_ts = float(ts_str)
                            # Remove if file is for current minute or later
                            if file_ts >= current_minute:
                                f.unlink()
                                logger.warning(f"🗑️ Removed conflicting file: {f.name}")
                        except Exception:
                            pass
                            
        except Exception as e:
            logger.debug(f"Cleanup failed: {e}")
    
    def _find_existing_data_end(self, drf_dir: Path) -> Optional[int]:
        """
        Find the end sample index of existing DRF data in directory.
        
        Used on restart to avoid overwriting existing files.
        
        Returns:
            End sample index if data exists, None otherwise
        """
        try:
            # Use DRF reader to find bounds of existing data
            parent_dir = drf_dir.parent  # Go up to channel dir (contains date subdirs)
            reader = drf.DigitalRFReader(str(parent_dir))
            channels = reader.get_channels()
            
            if not channels:
                return None
            
            # Find the latest end point across all channels (date directories)
            latest_end = None
            for ch in channels:
                bounds = reader.get_bounds(ch)
                if bounds[1] is not None:
                    if latest_end is None or bounds[1] > latest_end:
                        latest_end = bounds[1]
            
            return latest_end
            
        except Exception as e:
            # No existing data or error reading - that's fine
            logger.debug(f"No existing DRF data found: {e}")
            return None
    
    def set_time_reference(
        self,
        rtp_timestamp: int,
        system_time: float,
        ntp_offset_ms: Optional[float] = None
    ):
        """
        Set the system time reference for this recording session.
        
        This establishes the mapping between RTP timestamps and system time.
        NO UTC corrections are applied - that's Phase 2's job.
        
        Args:
            rtp_timestamp: RTP timestamp from first packet
            system_time: System wall clock time at first packet
            ntp_offset_ms: NTP offset if known (stored as metadata only)
        """
        self.system_time_ref = SystemTimeReference(
            rtp_timestamp=rtp_timestamp,
            system_time=system_time,
            ntp_offset_ms=ntp_offset_ms,
            sample_rate=self.config.sample_rate
        )
        
        logger.info(f"System time reference set:")
        logger.info(f"  RTP timestamp: {rtp_timestamp}")
        logger.info(f"  System time: {system_time}")
        logger.info(f"  NTP offset: {ntp_offset_ms} ms")
    
    def write_samples(
        self,
        samples: np.ndarray,
        rtp_timestamp: int,
        system_time: Optional[float] = None,
        gap_samples: int = 0
    ) -> int:
        """
        Write IQ samples to the raw archive.
        
        CRITICAL: This method stores data WITHOUT any UTC corrections.
        The time reference is system time only.
        
        Args:
            samples: Complex64 IQ samples (20 kHz)
            rtp_timestamp: RTP timestamp of first sample
            system_time: System wall clock time (uses current time if None)
            gap_samples: Number of zero-filled gap samples (for provenance)
            
        Returns:
            Number of samples written
        """
        with self._lock:
            if system_time is None:
                system_time = time.time()
            
            # Periodic NTP status check
            if time.time() - self.last_ntp_check > NTP_SYNC_CHECK_INTERVAL:
                self.ntp_status = check_ntp_status()
                self.last_ntp_check = time.time()
                if not self.ntp_status.synced:
                    logger.warning("⚠️ NTP sync lost during recording")
            
            # === RESILIENCE: Periodic heartbeat log ===
            current_time = time.time()
            if current_time - self.last_heartbeat_log >= 60:
                logger.info(
                    f"📡 {self.config.channel_name} heartbeat: "
                    f"{self.samples_written:,} samples, "
                    f"{self.data_quality['gaps_detected']} gaps, "
                    f"{self.data_quality['write_errors']} errors"
                )
                self.last_heartbeat_log = current_time
            
            # === RESILIENCE: Watchdog - reset writer if no writes for too long ===
            if self.drf_writer is not None and current_time - self.last_write_time > WRITE_WATCHDOG_TIMEOUT_SEC:
                logger.warning(
                    f"⚠️ Watchdog: No writes for {WRITE_WATCHDOG_TIMEOUT_SEC}s - resetting DRF writer"
                )
                self._close_writer()
                self.data_quality['watchdog_resets'] += 1
            
            # === RESILIENCE: Forced periodic flush ===
            if self.drf_writer is not None and current_time - self.last_flush_time > FORCED_FLUSH_INTERVAL_SEC:
                try:
                    # Close and reopen to force flush
                    self._close_writer()
                    self.data_quality['forced_flushes'] += 1
                    self.last_flush_time = current_time
                    logger.debug(f"Forced flush completed for {self.config.channel_name}")
                except Exception as e:
                    logger.error(f"Forced flush failed: {e}")
            
            # Note: Storage quota is managed at top-level, not per-channel
            
            # Set time reference if not established
            if self.system_time_ref is None:
                self.set_time_reference(rtp_timestamp, system_time)
            
            # Ensure writer exists
            self._create_drf_writer(system_time, rtp_timestamp)
            
            if self.drf_writer is None:
                logger.error("Failed to create DRF writer")
                self.data_quality['write_errors'] += 1
                return 0
            
            # Ensure samples are complex64
            if samples.dtype != np.complex64:
                samples = samples.astype(np.complex64)
                self.data_quality['dtype_conversions'] += 1
            
            # VALIDATE SAMPLES - catch bad data before writing
            samples, validation_metrics = self._validate_samples(samples)
            self.data_quality['samples_validated'] += len(samples)
            self.data_quality['samples_with_nan'] += validation_metrics['nan_count']
            self.data_quality['samples_with_inf'] += validation_metrics['inf_count']
            self.data_quality['samples_clipped'] += validation_metrics['clipped_count']
            
            # DETECT GAPS using RTP sequence
            detected_gap = self._detect_gap(rtp_timestamp, len(samples))
            if detected_gap < 0:
                # Late packet - skip it
                return 0
            if detected_gap > 0:
                gap_samples = max(gap_samples, detected_gap)
                
                # === RESILIENCE: Reset writer on extremely large gaps ===
                if detected_gap > MAX_GAP_BEFORE_WRITER_RESET:
                    logger.warning(
                        f"⚠️ Massive gap ({detected_gap} samples = {detected_gap/self.config.sample_rate:.1f}s) - "
                        f"resetting DRF writer to resync"
                    )
                    self._close_writer()
                    self.system_time_ref = None  # Reset time reference too
                    self.last_rtp_timestamp = None
                    # Recreate writer with fresh state
                    self._create_drf_writer(system_time, rtp_timestamp)
                    self.set_time_reference(rtp_timestamp, system_time)
            
            # Use RTP timestamp for sample ordering (not wall-clock time)
            # This ensures monotonic writes even with timing jitter
            if self.system_time_ref is not None:
                # Calculate sample index relative to RTP reference
                rtp_diff = rtp_timestamp - self.system_time_ref.rtp_timestamp
                # Handle 32-bit RTP wraparound
                if rtp_diff > 0x80000000:
                    rtp_diff -= 0x100000000
                elif rtp_diff < -0x80000000:
                    rtp_diff += 0x100000000
                
                # Check for backwards RTP (out-of-order packets should be handled upstream)
                if self.next_sample_index is not None:
                    expected_rtp_index = self.next_sample_index - int(self.system_time_ref.system_time * self.config.sample_rate)
                    if rtp_diff < expected_rtp_index - len(samples):
                        # Significant backwards jump - likely late packet, skip it
                        self.data_quality['late_packets_dropped'] += 1
                        return 0
            
            try:
                # Write samples to DRF
                # Note: DRF auto-advances from start_global_index
                self.drf_writer.rf_write(samples)
                
                # === RESILIENCE: Update write timestamp for watchdog ===
                self.last_write_time = time.time()
                self._consecutive_errors = 0  # Reset error counter on success
                
                # Update state
                self.samples_written += len(samples)
                self.total_gap_samples += gap_samples
                # Track next expected sample based on what we just wrote
                if self.next_sample_index is None:
                    self.next_sample_index = int(system_time * self.config.sample_rate) + len(samples)
                else:
                    self.next_sample_index += len(samples)
                
                # Update segment tracking
                if self.current_segment is None:
                    self.current_segment = ArchiveSegmentInfo(
                        segment_id=self.segment_counter,
                        start_system_time=system_time,
                        start_rtp_timestamp=rtp_timestamp,
                        sample_count=len(samples),
                        gap_count=1 if gap_samples > 0 else 0,
                        gap_samples=gap_samples
                    )
                else:
                    self.current_segment.sample_count += len(samples)
                    if gap_samples > 0:
                        self.current_segment.gap_count += 1
                        self.current_segment.gap_samples += gap_samples
                
                # Accumulate samples for 10-second sliding window monitor
                if self.sliding_monitor_enabled and self.sliding_monitor is not None:
                    self.window_sample_buffer.append(samples.copy())
                    self.window_sample_count += len(samples)
                    if gap_samples > 0:
                        self.window_gap_count += 1
                        self.window_gap_samples += gap_samples
                    
                    # Process when we have a complete 10-second window
                    if self.window_sample_count >= self.samples_per_window:
                        self._process_sliding_window(system_time)
                
                return len(samples)
                
            except Exception as e:
                self._consecutive_errors += 1
                self.data_quality['write_errors'] += 1
                
                # For first few errors, just skip packet (writer may still be OK)
                if self._consecutive_errors < self._max_errors_before_reset:
                    logger.warning(f"DRF write error ({self._consecutive_errors}/{self._max_errors_before_reset}): {e}")
                    return 0
                
                # === RESILIENCE: Full reset after multiple errors ===
                logger.error(f"DRF write error (resetting after {self._consecutive_errors} errors): {e}")
                logger.warning("⚠️ Resetting DRF writer after repeated write errors")
                
                # Clean up conflicting files FIRST
                self._cleanup_conflicting_files()
                
                try:
                    self._close_writer()
                except Exception:
                    pass  # Ignore errors during cleanup
                
                # Reset time reference to resync on next write
                self.system_time_ref = None
                self.last_rtp_timestamp = None
                self.next_sample_index = None
                self._consecutive_errors = 0  # Reset counter
                
                # Set recovery timestamp to NEXT minute boundary
                # Files are created at 60-second boundaries, so we must skip to next minute
                current_time = time.time()
                next_minute = ((int(current_time) // 60) + 1) * 60 + 1  # Next minute + 1 sec
                self._recovery_start_time = next_minute
                
                return 0
    
    def write_gap_metadata(
        self,
        gap_start_rtp: int,
        gap_samples: int,
        system_time: float
    ):
        """
        Write metadata about a gap in the data stream.
        
        This records gap provenance without modifying the raw data.
        
        Args:
            gap_start_rtp: RTP timestamp where gap started
            gap_samples: Number of samples in the gap
            system_time: System time when gap was detected
        """
        if self.metadata_writer is None:
            return
        
        gap_metadata = {
            'event_type': 'gap',
            'gap_start_rtp': gap_start_rtp,
            'gap_samples': gap_samples,
            'gap_duration_ms': (gap_samples / self.config.sample_rate) * 1000,
            'system_time': system_time,
            'detected_at': datetime.now(tz=timezone.utc).isoformat()
        }
        
        sample_index = int(system_time * self.config.sample_rate)
        try:
            self.metadata_writer.write(sample_index, gap_metadata)
            logger.debug(f"Gap metadata written: {gap_samples} samples at RTP {gap_start_rtp}")
        except Exception as e:
            logger.warning(f"Failed to write gap metadata: {e}")
    
    def flush(self):
        """Flush all buffered data to disk."""
        with self._lock:
            if self.drf_writer:
                try:
                    self.drf_writer.close()
                    # Recreate writer for subsequent writes
                    self.drf_writer = None
                    self.last_flush_time = time.time()
                    logger.info(f"Flushed raw archive: {self.samples_written} total samples")
                except Exception as e:
                    logger.error(f"Error flushing DRF writer: {e}")
    
    def close(self):
        """Close the writer and finalize the archive."""
        with self._lock:
            self._close_writer()
            
            # Write final session metadata
            self._write_session_summary()
            
            logger.info(f"RawArchiveWriter closed:")
            logger.info(f"  Total samples: {self.samples_written}")
            logger.info(f"  Total gaps: {self.total_gap_samples}")
            logger.info(f"  Files written: {self.files_written}")
    
    def set_stream_health(self, metrics: Dict[str, Any]):
        """
        Set RTP stream health metrics to be included in session summary.
        
        Args:
            metrics: Dict with keys like packets_received, packets_dropped,
                    packets_out_of_order, sequence_errors, timestamp_jumps
        """
        self.stream_health_metrics = metrics
    
    def _write_session_summary(self):
        """Write session summary metadata file with complete provenance."""
        
        # Calculate data integrity metrics
        duration_sec = time.time() - self.session_start_time
        expected_samples = int(duration_sec * self.config.sample_rate)
        sample_integrity = self.samples_written / max(1, expected_samples)
        
        summary = {
            'archive_type': 'raw_20khz_iq',
            'phase': 'phase1_immutable',
            'version': '2.0',  # Hardened version
            'channel_name': self.config.channel_name,
            'frequency_hz': self.config.frequency_hz,
            'sample_rate': self.config.sample_rate,
            'compression': self.config.compression,
            'total_samples': self.samples_written,
            'total_gap_samples': self.total_gap_samples,
            'files_written': self.files_written,
            'session_start': datetime.fromtimestamp(
                self.session_start_time, tz=timezone.utc
            ).isoformat(),
            'session_end': datetime.now(tz=timezone.utc).isoformat(),
            'duration_seconds': duration_sec,
            'system_time_ref': self.system_time_ref.to_dict() if self.system_time_ref else None,
            'station_config': self.config.station_config,
            'uuid': self.dataset_uuid,
            # CRITICAL: Mark this as uncorrected data
            'utc_correction_applied': False,
            'time_reference': 'system_time_only',
            'reprocessable': True,
            # RTP stream health metrics
            'stream_health': getattr(self, 'stream_health_metrics', None),
            # NTP synchronization status
            'ntp_status': self.ntp_status.to_dict() if self.ntp_status else None,
            # Data quality metrics
            'data_quality': {
                **self.data_quality,
                'sample_integrity_ratio': sample_integrity,
                'expected_samples': expected_samples,
            },
            # Note: Storage quota is managed at top-level, see quota_removal_log.json
        }
        
        summary_file = self.metadata_dir / 'session_summary.json'
        try:
            with open(summary_file, 'w') as f:
                json.dump(summary, f, indent=2)
            logger.info(f"Session summary written to {summary_file}")
            
            # Log data quality summary
            dq = self.data_quality
            logger.info(f"Data quality summary:")
            logger.info(f"  Samples validated: {dq['samples_validated']:,}")
            logger.info(f"  Sample integrity: {sample_integrity:.4f} ({sample_integrity*100:.2f}%)")
            logger.info(f"  Gaps detected: {dq['gaps_detected']}, largest: {dq['largest_gap_samples']} samples")
            if dq['samples_with_nan'] > 0 or dq['samples_with_inf'] > 0:
                logger.warning(f"  ⚠️ Bad samples: {dq['samples_with_nan']} NaN, {dq['samples_with_inf']} Inf")
            if dq['late_packets_dropped'] > 0:
                logger.info(f"  Late packets dropped: {dq['late_packets_dropped']}")
            if dq['write_errors'] > 0:
                logger.warning(f"  ⚠️ Write errors: {dq['write_errors']}")
                
        except Exception as e:
            logger.error(f"Failed to write session summary: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current writer statistics."""
        with self._lock:
            return {
                'samples_written': self.samples_written,
                'gap_samples': self.total_gap_samples,
                'files_written': self.files_written,
                'current_day': self.current_day.isoformat() if self.current_day else None,
                'uptime_seconds': time.time() - self.session_start_time,
                'sample_rate': self.config.sample_rate,
                'archive_dir': str(self.archive_dir)
            }


class RawArchiveReader:
    """
    Read raw archive data from Phase 1 for analysis (Phase 2) or
    product generation (Phase 3).
    
    This provides read-only access to the immutable raw archive.
    """
    
    def __init__(self, archive_dir: Path, channel_name: str):
        """
        Initialize raw archive reader.
        
        Args:
            archive_dir: Base directory containing raw archive
            channel_name: Channel name to read
        """
        if not DRF_AVAILABLE:
            raise ImportError("digital_rf required for reading raw archives")
        
        # archive_dir is expected to be the raw_archive root (e.g., /data/raw_archive/)
        self.archive_dir = Path(archive_dir) / channel_name.replace(' ', '_')
        self.channel_name = channel_name
        
        # Create DRF reader
        self.drf_reader: Optional[drf.DigitalRFReader] = None
        self._init_reader()
    
    def _init_reader(self):
        """Initialize Digital RF reader."""
        if not self.archive_dir.exists():
            logger.warning(f"Archive directory not found: {self.archive_dir}")
            return
        
        try:
            self.drf_reader = drf.DigitalRFReader(str(self.archive_dir))
            channels = self.drf_reader.get_channels()
            logger.info(f"RawArchiveReader initialized, channels: {channels}")
        except Exception as e:
            logger.debug(f"DRF reader init (expected on first run): {e}")
            self.drf_reader = None
    
    def read_samples(
        self,
        start_index: int,
        num_samples: int
    ) -> Optional[Tuple[np.ndarray, int]]:
        """
        Read samples from the raw archive.
        
        Args:
            start_index: Global sample index to start reading
            num_samples: Number of samples to read
            
        Returns:
            Tuple of (samples, actual_start_index) or None if not available
        """
        if self.drf_reader is None:
            return None
        
        try:
            # Get available channels
            channels = self.drf_reader.get_channels()
            if not channels:
                return None
            
            channel = channels[0]  # Use first channel
            
            # Read data
            data = self.drf_reader.read(
                start_index,
                num_samples,
                channel
            )
            
            if data is None or len(data) == 0:
                return None
            
            return data, start_index
            
        except Exception as e:
            logger.error(f"Error reading from archive: {e}")
            return None
    
    def get_bounds(self) -> Optional[Tuple[int, int]]:
        """
        Get the sample index bounds of available data.
        
        Returns:
            Tuple of (first_index, last_index) or None
        """
        if self.drf_reader is None:
            return None
        
        try:
            channels = self.drf_reader.get_channels()
            if not channels:
                return None
            
            channel = channels[0]
            bounds = self.drf_reader.get_bounds(channel)
            return bounds
        except Exception as e:
            logger.error(f"Error getting archive bounds: {e}")
            return None
    
    def get_metadata(self) -> Optional[Dict]:
        """Get archive metadata."""
        metadata_file = self.archive_dir / 'metadata' / 'session_summary.json'
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error reading metadata: {e}")
        return None


# Convenience factory function
def create_raw_archive_writer(
    output_dir: Path,
    channel_name: str,
    frequency_hz: float,
    sample_rate: int = 20000,
    station_config: Optional[Dict] = None,
    compression: str = 'gzip'
) -> RawArchiveWriter:
    """
    Create a raw archive writer with default settings.
    
    Args:
        output_dir: Base output directory
        channel_name: Channel identifier
        frequency_hz: Center frequency
        sample_rate: Sample rate (default 20000)
        station_config: Station metadata
        compression: Compression algorithm
        
    Returns:
        Configured RawArchiveWriter
    """
    config = RawArchiveConfig(
        output_dir=output_dir,
        channel_name=channel_name,
        frequency_hz=frequency_hz,
        sample_rate=sample_rate,
        station_config=station_config or {},
        compression=compression
    )
    return RawArchiveWriter(config)
