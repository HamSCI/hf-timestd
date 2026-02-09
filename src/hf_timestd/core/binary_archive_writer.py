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
BYTES_PER_SAMPLE = 8  # complex64 = 2 x float32


@dataclass
class TimingSnapshot:
    """
    A GPS_TIME/RTP_TIMESNAP pair from radiod status packets.
    
    These snapshots enable post-hoc RTP-to-UTC conversion using radiod's
    authoritative timing (when GPS+PPS disciplined, L4/L5 accuracy).
    
    Capture frequency: ~2 Hz (radiod's default status update rate)
    Metrological justification:
    - In L4/L5: Documents stable GPS-disciplined mapping for verification
    - In L3/L2/L1: Captures NTP slew/step events for post-hoc correction
    
    Attributes:
        gps_time_ns: radiod's GPS_TIME (ns since GPS epoch, from CLOCK_REALTIME)
        rtp_timesnap: RTP timestamp at the moment GPS_TIME was sampled
        local_receipt_time: When hf-timestd received this status packet (Unix time)
    """
    gps_time_ns: int
    rtp_timesnap: int  
    local_receipt_time: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'gps_time_ns': self.gps_time_ns,
            'rtp_timesnap': self.rtp_timesnap,
            'local_receipt_time': self.local_receipt_time
        }


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
    radiod_snr_db: Optional[float] = None  # SNR from radiod (updated periodically)
    
    # Pre-roll: Start buffer before minute boundary to capture full minute markers.
    # The minute marker tone starts at second 0, so we need samples BEFORE the
    # minute boundary to capture the full tone onset. NTP is used as a hint for
    # where to look, not as ground truth - the bootstrap establishes timing from
    # the tones themselves.
    pre_roll_seconds: float = 2.0  # Start buffer 2s before minute boundary


@dataclass
class MinuteBuffer:
    """Buffer for accumulating one minute of samples."""
    minute_boundary: int  # Unix timestamp of minute start
    samples: np.ndarray   # Pre-allocated buffer
    write_pos: int = 0    # Current write position
    gap_count: int = 0    # Number of gaps in this minute
    gap_samples: int = 0  # Total gap samples
    start_rtp: Optional[int] = None
    start_system_time: Optional[float] = None
    timing_snapshots: List[TimingSnapshot] = field(default_factory=list)  # Snapshots for this minute
    
    @property
    def is_complete(self) -> bool:
        return self.write_pos >= len(self.samples)
    
    @property
    def samples_remaining(self) -> int:
        return max(0, len(self.samples) - self.write_pos)


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
        
        if config.use_tiered_storage:
            from .tiered_storage import get_tiered_storage_manager
            self._tiered_manager = get_tiered_storage_manager()
            self.archive_dir = self._tiered_manager.get_hot_buffer_path(config.channel_name)
        else:
            from ..paths import channel_name_to_dir
            self._tiered_manager = None
            self.archive_dir = config.output_dir / channel_name_to_dir(config.channel_name)
        
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        
        # Buffer sizing
        self.samples_per_minute = int(config.sample_rate * 60)
        
        # Current minute buffer
        self.current_buffer: Optional[MinuteBuffer] = None
        self._lock = threading.Lock()
        
        # Statistics
        self.minutes_written = 0
        self.samples_written = 0
        self.total_gaps = 0
        self.write_errors = 0
        
        # Time reference - GPS_TIME/RTP_TIMESNAP from radiod
        # In RTP mode, the GPSDO-disciplined RTP clock IS the timing authority.
        # GPS_TIME/RTP_TIMESNAP gives us UTC directly:
        #   UTC = gps_time_unix + (rtp - rtp_timesnap) / sample_rate
        # There is no timing offset to discover — we already have it.
        #
        # Counter-space mismatch fix: radiod's RTP_TIMESNAP is based on
        # frontend->samples (ADC input counter) / decimation, but packet RTP
        # timestamps come from filter.out.sample_index / decimation (filter
        # output counter). These differ by the filter pipeline depth, which is
        # constant for a given session. We reconcile the two counter spaces once
        # at startup using the chrony-disciplined wall clock as a cross-check.
        self._gps_time_unix: Optional[float] = None  # GPS_TIME converted to Unix time
        self._rtp_timesnap: Optional[int] = None     # RTP timestamp at GPS_TIME (corrected)
        self._timing_locked: bool = False
        self._pipeline_offset_samples: Optional[int] = None  # Counter-space correction (samples)
        
        # RTP clock drift detection (after offset is locked)
        # If RTP-derived time diverges from wall clock by more than this threshold,
        # we reset the offset calibration to recover from frozen/drifted RTP clocks
        self._rtp_drift_threshold_sec: float = 300.0  # 5 minutes
        self._rtp_drift_check_interval: int = 100  # Check every N chunks
        self._chunks_since_drift_check: int = 0
        
        self.last_rtp_timestamp: Optional[int] = None
        self.cumulative_samples: int = 0  # Total samples processed
        
        # Timing snapshot tracking for radiod GPS_TIME/RTP_TIMESNAP pairs
        # Deduplicated by rtp_timesnap to avoid storing duplicates
        self._last_rtp_timesnap: Optional[int] = None
        self._pending_snapshots: List[TimingSnapshot] = []  # Snapshots waiting for minute assignment
        
        logger.info(f"BinaryArchiveWriter initialized for {config.channel_name}")
        logger.info(f"  Output: {self.archive_dir}")
        logger.info(f"  Format: raw complex64 binary + JSON metadata")
    
    def add_timing_snapshot(self, gps_time_ns: int, rtp_timesnap: int) -> bool:
        """
        Record a GPS_TIME/RTP_TIMESNAP pair from radiod status.
        
        Called at ~2 Hz (radiod's status update rate). Deduplicated by rtp_timesnap
        to avoid storing duplicate snapshots when status hasn't changed.
        
        CRITICAL: This is the AUTHORITATIVE time reference in RTP mode.
        GPS_TIME comes from radiod's GPS+PPS and is the ground truth for UTC.
        We use this to establish the RTP-to-UTC mapping, NOT local system time.
        
        Args:
            gps_time_ns: radiod's GPS_TIME (ns since GPS epoch)
            rtp_timesnap: RTP timestamp at the moment GPS_TIME was sampled
            
        Returns:
            True if snapshot was stored (new), False if deduplicated
        """
        with self._lock:
            # Deduplicate: only store if rtp_timesnap has changed
            if rtp_timesnap == self._last_rtp_timesnap:
                return False
            
            self._last_rtp_timesnap = rtp_timesnap
            
            # Convert GPS_TIME to Unix time
            # GPS epoch is Jan 6, 1980. GPS_TIME is ns since GPS epoch.
            GPS_EPOCH_UNIX = 315964800  # Unix timestamp of GPS epoch
            GPS_LEAP_SECONDS = 18  # Current leap seconds (GPS - UTC)
            BILLION = 1_000_000_000
            
            gps_unix_ns = gps_time_ns + BILLION * (GPS_EPOCH_UNIX - GPS_LEAP_SECONDS)
            gps_unix_sec = gps_unix_ns / BILLION
            
            # Store GPS_TIME/RTP_TIMESNAP mapping.
            # Apply counter-space correction if calibrated (input vs output counter).
            self._gps_time_unix = gps_unix_sec
            if self._pipeline_offset_samples is not None:
                self._rtp_timesnap = (rtp_timesnap + self._pipeline_offset_samples) & 0xFFFFFFFF
            else:
                self._rtp_timesnap = rtp_timesnap
            if not self._timing_locked:
                self._timing_locked = True
                logger.info(f"{self.config.channel_name}: RTP timing LOCKED - GPS_TIME={gps_unix_sec:.6f}, RTP_TIMESNAP={rtp_timesnap}")
            
            snapshot = TimingSnapshot(
                gps_time_ns=gps_time_ns,
                rtp_timesnap=rtp_timesnap,
                local_receipt_time=time.time()  # For diagnostics only, not used for timing
            )
            
            # Add to current buffer if available, otherwise to pending list
            if self.current_buffer is not None:
                self.current_buffer.timing_snapshots.append(snapshot)
            else:
                self._pending_snapshots.append(snapshot)
            
            return True
    
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
            rtp_timestamp: RTP timestamp of the packet that triggered this new minute
            
        The RTP stream tells us the exact time. When a packet's RTP-derived UTC
        crosses a minute boundary, we start a new buffer.
        
        CRITICAL: We calculate the RTP timestamp that corresponds to the exact
        minute boundary using the GPS_TIME/RTP_TIMESNAP mapping. This ensures
        sample position 0 = minute boundary, regardless of when the first packet
        actually arrives.
        """
        minute_boundary = (int(rtp_derived_time) // 60) * 60
        
        # Calculate RTP timestamp at the exact minute boundary using the mapping:
        #   UTC = GPS_TIME + (rtp - RTP_TIMESNAP) / sample_rate
        #   rtp = RTP_TIMESNAP + (UTC - GPS_TIME) * sample_rate
        # Note: _rtp_timesnap is already in packet counter space
        # (see counter-space reconciliation in write_samples).
        time_delta = minute_boundary - self._gps_time_unix
        rtp_delta = int(time_delta * self.config.sample_rate)
        minute_boundary_rtp = (self._rtp_timesnap + rtp_delta) & 0xFFFFFFFF
        
        buffer = MinuteBuffer(
            minute_boundary=minute_boundary,
            samples=np.zeros(self.samples_per_minute, dtype=np.complex64),
            write_pos=0,
            start_rtp=minute_boundary_rtp,  # RTP at actual minute boundary
            start_system_time=float(minute_boundary),  # Exactly on minute boundary
            timing_snapshots=[]
        )
        
        # Transfer any pending timing snapshots to this buffer
        if self._pending_snapshots:
            buffer.timing_snapshots.extend(self._pending_snapshots)
            logger.debug(f"Transferred {len(self._pending_snapshots)} pending timing snapshots to new minute")
            self._pending_snapshots = []
        
        logger.debug(f"Started new minute buffer: {minute_boundary}")
        return buffer
    
    def _check_disk_space(self, path: Path, required_bytes: int) -> bool:
        """Check if sufficient disk space is available based on storage quota.
        
        Uses the configured storage_quota_percent to determine if we're over quota.
        If over quota, automatically removes oldest files to make room.
        Also checks for absolute minimum free space (100MB headroom).
        """
        try:
            stat = shutil.disk_usage(path)
            
            # Check storage quota percentage
            current_usage_percent = (stat.used / stat.total) * 100
            if current_usage_percent >= self.config.storage_quota_percent:
                # Auto-remove oldest files to make room
                freed = self._remove_oldest_files(path, required_bytes)
                if freed > 0:
                    logger.info(
                        f"Storage quota reached ({current_usage_percent:.1f}%), "
                        f"removed oldest files to free {freed / 1024 / 1024:.1f}MB"
                    )
                    # Re-check after cleanup
                    stat = shutil.disk_usage(path)
                    current_usage_percent = (stat.used / stat.total) * 100
                    if current_usage_percent >= self.config.storage_quota_percent:
                        logger.warning(
                            f"Still over quota after cleanup: {current_usage_percent:.1f}%"
                        )
                        # Continue anyway - we tried our best
            
            # Also check absolute minimum free space (100MB headroom)
            min_free = required_bytes + 100 * 1024 * 1024
            if stat.free < min_free:
                # Try to free more space
                freed = self._remove_oldest_files(path, min_free - stat.free)
                if freed > 0:
                    logger.info(f"Freed {freed / 1024 / 1024:.1f}MB for minimum headroom")
                else:
                    logger.error(
                        f"Insufficient disk space: {stat.free / 1024 / 1024:.1f}MB free, "
                        f"need {min_free / 1024 / 1024:.1f}MB"
                    )
                    return False
            
            return True
        except OSError as e:
            logger.warning(f"Could not check disk space: {e}")
            return True  # Proceed anyway, let write fail if needed
    
    def _remove_oldest_files(self, path: Path, bytes_needed: int) -> int:
        """Remove oldest files from the archive to free space.
        
        Args:
            path: Base path to search for files
            bytes_needed: Minimum bytes to free
            
        Returns:
            Total bytes freed
        """
        try:
            # Find all .bin and .bin.zst/.bin.lz4 files in the archive
            archive_root = path.parent if path.name.isdigit() else path
            
            # Collect all minute files with their timestamps
            files_with_time = []
            for pattern in ['**/*.bin', '**/*.bin.zst', '**/*.bin.lz4']:
                for f in archive_root.glob(pattern):
                    try:
                        # Use file modification time for sorting
                        mtime = f.stat().st_mtime
                        size = f.stat().st_size
                        files_with_time.append((mtime, size, f))
                    except OSError:
                        continue
            
            if not files_with_time:
                return 0
            
            # Sort by modification time (oldest first)
            files_with_time.sort(key=lambda x: x[0])
            
            # Remove oldest files until we've freed enough space
            bytes_freed = 0
            files_removed = 0
            for mtime, size, filepath in files_with_time:
                if bytes_freed >= bytes_needed:
                    break
                
                try:
                    # Also remove the corresponding .json sidecar
                    json_path = filepath.with_suffix('.json') if filepath.suffix == '.bin' else \
                                filepath.with_name(filepath.name.replace('.bin.zst', '.json').replace('.bin.lz4', '.json'))
                    
                    filepath.unlink()
                    bytes_freed += size
                    files_removed += 1
                    
                    if json_path.exists():
                        json_size = json_path.stat().st_size
                        json_path.unlink()
                        bytes_freed += json_size
                    
                    logger.debug(f"Removed old file: {filepath.name}")
                except OSError as e:
                    logger.debug(f"Could not remove {filepath}: {e}")
                    continue
            
            if files_removed > 0:
                logger.info(f"Quota cleanup: removed {files_removed} oldest files")
            
            return bytes_freed
            
        except Exception as e:
            logger.warning(f"Error during quota cleanup: {e}")
            return 0
    
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
            actual_samples = min(buffer.write_pos, self.samples_per_minute)
            raw_data = buffer.samples[:actual_samples].tobytes()
            
            # Check disk space before writing (raw size + some overhead)
            if not self._check_disk_space(minute_dir, len(raw_data) + 10000):
                self.write_errors += 1
                return False
            
            # Atomic write: write to temp file first
            bin_path_tmp = bin_path.with_suffix(bin_path.suffix + '.tmp')
            
            # Apply compression if configured
            if compression == 'zstd':
                try:
                    import zstandard as zstd
                    # CRITICAL FIX (2026-01-12): Use threads=1 to avoid resource contention/hangs.
                    # Multi-threaded compression across 9 channels simultaneously was causing 
                    # the recorder service to stall/hang. Single-threaded is safer on low-core systems.
                    cctx = zstd.ZstdCompressor(level=self.config.compression_level, threads=1)
                    compressed_data = cctx.compress(raw_data)
                    with open(bin_path_tmp, 'wb') as f:
                        f.write(compressed_data)
                        f.flush()
                        os.fsync(f.fileno())
                    compression_ratio = len(raw_data) / len(compressed_data)
                    logger.debug(f"zstd compression: {len(raw_data)} -> {len(compressed_data)} ({compression_ratio:.1f}x)")
                except ImportError:
                    logger.warning("zstandard not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{buffer.minute_boundary}.bin"
                    bin_path_tmp = bin_path.with_suffix('.bin.tmp')
                    buffer.samples[:actual_samples].tofile(bin_path_tmp)
            elif compression == 'lz4':
                try:
                    import lz4.frame
                    compressed_data = lz4.frame.compress(raw_data, compression_level=self.config.compression_level)
                    with open(bin_path_tmp, 'wb') as f:
                        f.write(compressed_data)
                        f.flush()
                        os.fsync(f.fileno())
                    compression_ratio = len(raw_data) / len(compressed_data)
                    logger.debug(f"lz4 compression: {len(raw_data)} -> {len(compressed_data)} ({compression_ratio:.1f}x)")
                except ImportError:
                    logger.warning("lz4 not installed, falling back to uncompressed")
                    bin_path = minute_dir / f"{buffer.minute_boundary}.bin"
                    bin_path_tmp = bin_path.with_suffix('.bin.tmp')
                    buffer.samples[:actual_samples].tofile(bin_path_tmp)
            else:
                # No compression - direct write
                buffer.samples[:actual_samples].tofile(bin_path_tmp)
            
            # Rename atomic
            if bin_path_tmp.exists():
                bin_path_tmp.replace(bin_path)
            
            # Write metadata sidecar
            metadata = {
                'minute_boundary': buffer.minute_boundary,
                'channel_name': self.config.channel_name,
                'frequency_hz': self.config.frequency_hz,
                'sample_rate': self.config.sample_rate,
                'samples_written': actual_samples,
                'samples_expected': self.samples_per_minute,
                'completeness_pct': 100.0 * actual_samples / self.samples_per_minute,
                'gap_count': buffer.gap_count,
                'gap_samples': buffer.gap_samples,
                'start_rtp_timestamp': buffer.start_rtp,
                'start_system_time': buffer.start_system_time,
                'dtype': 'complex64',
                'byte_order': 'little',
                'compression': compression if compression != 'none' else None,
                'radiod_snr_db': self.config.radiod_snr_db,  # SNR from radiod
                'written_at': datetime.now(timezone.utc).isoformat(),
                'station': self.config.station_config,
                # Counter-space correction: timing_snapshots[].rtp_timesnap is in
                # radiod status counter space, but start_rtp_timestamp is in packet
                # counter space. They differ by the filter pipeline depth (constant
                # per session). Add this offset to rtp_timesnap before comparing
                # with start_rtp_timestamp.
                'pipeline_offset_samples': self._pipeline_offset_samples or 0,
                # Timing snapshots: GPS_TIME/RTP_TIMESNAP pairs from radiod (~2 Hz)
                # Enables post-hoc RTP-to-UTC conversion and timing validation
                'timing_snapshots': [s.to_dict() for s in buffer.timing_snapshots]
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
                f"{actual_samples}/{self.samples_per_minute} samples "
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
        Convert RTP timestamp to Unix time. In RTP mode the GPSDO provides UTC
        directly via GPS_TIME/RTP_TIMESNAP — no offset discovery needed.
        
        RTP_TIMESNAP has been corrected to the packet counter space (see
        counter-space reconciliation in write_samples).
        
        Formula: UTC = GPS_TIME + (rtp - RTP_TIMESNAP) / sample_rate
        """
        if self._gps_time_unix is None or self._rtp_timesnap is None:
            # Not initialized yet - return 0 (will use system_time fallback)
            return 0.0
        
        # Handle 32-bit RTP wrap-around
        rtp_delta = int((rtp_timestamp - self._rtp_timesnap) & 0xFFFFFFFF)
        if rtp_delta > 0x7FFFFFFF:
            rtp_delta -= 0x100000000
        
        return self._gps_time_unix + rtp_delta / self.config.sample_rate
    
    def _interpolate_gaps(self, samples: np.ndarray) -> np.ndarray:
        """
        Replace zero-filled gaps with phase-continuous interpolation.
        
        ka9q-python fills gaps with zeros which breaks phase continuity.
        This method detects zero runs and replaces them with samples that
        maintain phase continuity from the surrounding valid samples.
        
        Args:
            samples: Complex64 samples potentially containing zero-filled gaps
            
        Returns:
            Samples with gaps interpolated to preserve phase continuity
        """
        # Find zero samples (gap fills from ka9q-python)
        zero_mask = np.abs(samples) < 1e-10
        
        if not np.any(zero_mask):
            return samples  # No gaps to interpolate
        
        # Make a copy to modify
        result = samples.copy()
        
        # Find runs of zeros
        # Pad with False to detect edges at boundaries
        padded = np.concatenate([[False], zero_mask, [False]])
        diff = np.diff(padded.astype(int))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]
        
        for start, end in zip(starts, ends):
            gap_len = end - start
            
            # Get samples before and after gap
            before_idx = start - 1 if start > 0 else None
            after_idx = end if end < len(samples) else None
            
            if before_idx is not None and after_idx is not None:
                # Interpolate phase between before and after
                before_sample = samples[before_idx]
                after_sample = samples[after_idx]
                
                if np.abs(before_sample) > 1e-10 and np.abs(after_sample) > 1e-10:
                    # Linear interpolation of phase and amplitude
                    before_phase = np.angle(before_sample)
                    after_phase = np.angle(after_sample)
                    before_amp = np.abs(before_sample)
                    after_amp = np.abs(after_sample)
                    
                    # Unwrap phase difference to avoid jumps
                    phase_diff = after_phase - before_phase
                    if phase_diff > np.pi:
                        phase_diff -= 2 * np.pi
                    elif phase_diff < -np.pi:
                        phase_diff += 2 * np.pi
                    
                    # Interpolate
                    for i in range(gap_len):
                        t = (i + 1) / (gap_len + 1)
                        interp_phase = before_phase + t * phase_diff
                        interp_amp = before_amp + t * (after_amp - before_amp)
                        result[start + i] = interp_amp * np.exp(1j * interp_phase)
                        
            elif before_idx is not None:
                # Gap at end - extrapolate from before
                before_sample = samples[before_idx]
                if np.abs(before_sample) > 1e-10:
                    # Hold phase and amplitude constant
                    for i in range(gap_len):
                        result[start + i] = before_sample
                        
            elif after_idx is not None:
                # Gap at start - extrapolate from after
                after_sample = samples[after_idx]
                if np.abs(after_sample) > 1e-10:
                    # Hold phase and amplitude constant
                    for i in range(gap_len):
                        result[start + i] = after_sample
        
        return result
    
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
            # GPS_TIME/RTP_TIMESNAP must be established before we can write
            if self._gps_time_unix is None or self._rtp_timesnap is None:
                # Log once per second to avoid spam
                if not hasattr(self, '_last_waiting_log') or time.time() - self._last_waiting_log > 1.0:
                    logger.debug("Waiting for GPS_TIME from radiod...")
                    self._last_waiting_log = time.time()
                return 0  # Cannot write until we have authoritative timing
            
            # Reconcile radiod's two counter spaces on first packet.
            # RTP_TIMESNAP uses the ADC input counter; packet RTP timestamps use
            # the filter output counter. The difference is the filter pipeline
            # depth — a fixed constant for the session, not a timing offset to
            # discover. We cross-check against time.time() (chrony-disciplined,
            # NOT system_time which comes from the same RTP mapping).
            if self._pipeline_offset_samples is None:
                rtp_delta_raw = int((rtp_timestamp - self._rtp_timesnap) & 0xFFFFFFFF)
                if rtp_delta_raw > 0x7FFFFFFF:
                    rtp_delta_raw -= 0x100000000
                raw_rtp_time = self._gps_time_unix + rtp_delta_raw / self.config.sample_rate
                wall_clock = time.time()
                offset_sec = raw_rtp_time - wall_clock
                if abs(offset_sec) > 0.1 and abs(offset_sec) < 30.0:
                    self._pipeline_offset_samples = int(offset_sec * self.config.sample_rate)
                    self._rtp_timesnap = (self._rtp_timesnap + self._pipeline_offset_samples) & 0xFFFFFFFF
                    logger.info(
                        f"{self.config.channel_name}: Counter-space correction: "
                        f"{offset_sec:.3f}s ({self._pipeline_offset_samples} samples)"
                    )
                elif abs(offset_sec) <= 0.1:
                    self._pipeline_offset_samples = 0
                    logger.info(
                        f"{self.config.channel_name}: Counter-space offset negligible: {offset_sec:.3f}s"
                    )
                else:
                    logger.warning(
                        f"{self.config.channel_name}: Counter-space offset {offset_sec:.3f}s outside "
                        f"expected range (0.1-30s), will retry next packet"
                    )
            
            # Ensure complex64
            if samples.dtype != np.complex64:
                samples = samples.astype(np.complex64)
            
            # Phase-preserving gap interpolation
            # ka9q-python fills gaps with zeros which breaks phase continuity
            # Replace zeros with phase-continuous interpolation from surrounding samples
            # Always check for zeros, not just when gap_samples is explicitly set
            samples = self._interpolate_gaps(samples)
            
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
            
            # Write to buffer at correct position based on RTP timestamp
            # In RTP mode, samples are positioned by their RTP offset from minute boundary
            buffer = self.current_buffer
            
            # Calculate position in buffer DIRECTLY from RTP timestamp
            # This is authoritative - RTP is GPSDO-disciplined
            # Handle 32-bit RTP wrap-around
            rtp_delta = int((rtp_timestamp - buffer.start_rtp) & 0xFFFFFFFF)
            if rtp_delta > 0x7FFFFFFF:
                rtp_delta -= 0x100000000
            sample_position = rtp_delta
            
            # Clamp to valid range
            if sample_position < 0:
                # Samples before minute boundary - skip them
                skip_count = -sample_position
                if skip_count >= len(samples):
                    return 0  # All samples are before the minute
                samples = samples[skip_count:]
                sample_position = 0
            
            samples_to_write = min(len(samples), self.samples_per_minute - sample_position)
            
            if samples_to_write > 0 and sample_position < self.samples_per_minute:
                buffer.samples[sample_position:sample_position + samples_to_write] = samples[:samples_to_write]
                # Update write_pos to track highest written position
                buffer.write_pos = max(buffer.write_pos, sample_position + samples_to_write)
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
        # Scan available minutes and return second-to-last (last might be incomplete)
        # This avoids using wall clock time
        minutes = self.get_available_minutes()
        if minutes:
            return minutes[-2] if len(minutes) > 1 else minutes[-1]
        return None
