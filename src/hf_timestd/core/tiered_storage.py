#!/usr/bin/env python3
"""
Tiered Storage Manager - RAM-aware hot/cold buffer management

Automatically configures hot buffer (RAM) and cold buffer (disk) based on
available system memory. Provides zero-disk-I/O for real-time pipeline while
ensuring data is archived to persistent storage.

Architecture:
    /dev/shm/timestd/raw_buffer/{CHANNEL}/    <- Hot buffer (RAM)
    /var/lib/timestd/raw_buffer/{CHANNEL}/    <- Cold buffer (disk)

The hot buffer holds recent minutes in RAM for:
- Zero-latency reads by Phase 2 Analytics
- Web-UI access to current data
- Avoiding disk write/read round-trips

A background thread moves old minutes from hot to cold storage.

RAM Budget Calculation:
    Per channel per minute: ~10 MB (9.6 MB IQ + metadata)
    
    Available RAM    Channels    Hot Minutes    RAM Used
    ─────────────    ────────    ───────────    ────────
    1 GB             9           2              180 MB (18%)
    2 GB             9           5              450 MB (22%)
    4 GB             9           10             900 MB (22%)
    8 GB             9           20             1.8 GB (22%)
    16 GB            9           30             2.7 GB (17%)

Default: Use 20% of available RAM for hot buffer, minimum 2 minutes.
"""

import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Constants
MB = 1024 * 1024
GB = 1024 * MB
BYTES_PER_MINUTE = 10 * MB  # ~10 MB per channel per minute (IQ + metadata)
MIN_HOT_MINUTES = 2  # Always keep at least 2 minutes in RAM
MAX_HOT_MINUTES = 60  # Never keep more than 1 hour in RAM
DEFAULT_RAM_PERCENT = 20  # Use 20% of available RAM for hot buffer


@dataclass
class TieredStorageConfig:
    """Configuration for tiered storage."""
    # Required path - must be provided from config (default to /var/lib/timestd for backwards compatibility)
    cold_buffer_root: Path = Path('/var/lib/timestd')
    
    # Optional paths
    hot_buffer_root: Path = Path('/dev/shm/timestd')
    
    # Auto-configuration
    auto_configure: bool = True  # Auto-detect RAM and set hot_minutes
    ram_percent: float = DEFAULT_RAM_PERCENT  # Percent of available RAM to use
    
    # Manual override (used if auto_configure=False)
    hot_minutes: int = 5  # Minutes to keep in hot buffer
    
    # Behavior
    archive_to_cold: bool = True  # Move old minutes to cold storage
    delete_after_archive: bool = True  # Delete from hot after archiving
    archive_interval_seconds: float = 30.0  # How often to run archiver
    
    # Channel info (set at runtime)
    num_channels: int = 9


def get_available_ram_bytes() -> int:
    """Get available RAM in bytes using /proc/meminfo or fallback."""
    try:
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    # Format: "MemAvailable:    1234567 kB"
                    parts = line.split()
                    kb = int(parts[1])
                    return kb * 1024
    except (FileNotFoundError, ValueError, IndexError):
        pass
    
    # Fallback: use shutil.disk_usage on /dev/shm
    try:
        usage = shutil.disk_usage('/dev/shm')
        return usage.free
    except (FileNotFoundError, OSError):
        pass
    
    # Last resort: assume 1 GB available
    logger.warning("Could not detect available RAM, assuming 1 GB")
    return 1 * GB


def get_shm_size_bytes() -> int:
    """Get size of /dev/shm (tmpfs) in bytes."""
    try:
        usage = shutil.disk_usage('/dev/shm')
        return usage.total
    except (FileNotFoundError, OSError):
        # /dev/shm doesn't exist (non-Linux?)
        return 0


def calculate_hot_minutes(
    num_channels: int,
    ram_percent: float = DEFAULT_RAM_PERCENT,
    available_ram: Optional[int] = None
) -> int:
    """
    Calculate optimal number of hot buffer minutes based on available RAM.
    
    Args:
        num_channels: Number of channels being recorded
        ram_percent: Percentage of available RAM to use (0-100)
        available_ram: Override available RAM detection (for testing)
        
    Returns:
        Number of minutes to keep in hot buffer
    """
    if available_ram is None:
        available_ram = get_available_ram_bytes()
    
    # Also check /dev/shm size - can't use more than that
    shm_size = get_shm_size_bytes()
    if shm_size > 0:
        available_ram = min(available_ram, shm_size)
    
    # Calculate RAM budget
    ram_budget = int(available_ram * (ram_percent / 100.0))
    
    # Calculate minutes that fit in budget
    bytes_per_minute_all_channels = BYTES_PER_MINUTE * num_channels
    hot_minutes = ram_budget // bytes_per_minute_all_channels
    
    # Clamp to valid range
    hot_minutes = max(MIN_HOT_MINUTES, min(MAX_HOT_MINUTES, hot_minutes))
    
    logger.info(
        f"TieredStorage: Available RAM={available_ram/GB:.1f}GB, "
        f"budget={ram_budget/MB:.0f}MB ({ram_percent}%), "
        f"channels={num_channels}, hot_minutes={hot_minutes}"
    )
    
    return hot_minutes


class TieredStorageManager:
    """
    Manages hot (RAM) and cold (disk) storage tiers.
    
    Usage:
        manager = TieredStorageManager(config)
        manager.start()  # Start background archiver
        
        # Writers use hot buffer path
        hot_path = manager.get_hot_buffer_path(channel_name)
        
        # Readers check hot first, then cold
        data_path = manager.find_minute_file(channel_name, minute_boundary)
        
        manager.stop()
    """
    
    def __init__(self, config: Optional[TieredStorageConfig] = None):
        self.config = config or TieredStorageConfig()
        
        # Auto-configure hot_minutes based on RAM
        if self.config.auto_configure:
            self.hot_minutes = calculate_hot_minutes(
                num_channels=self.config.num_channels,
                ram_percent=self.config.ram_percent
            )
        else:
            self.hot_minutes = self.config.hot_minutes
        
        # Ensure directories exist
        self.hot_root = self.config.hot_buffer_root / 'raw_buffer'
        self.cold_root = self.config.cold_buffer_root / 'raw_buffer'
        
        self.hot_root.mkdir(parents=True, exist_ok=True)
        self.cold_root.mkdir(parents=True, exist_ok=True)
        
        # Background archiver
        self._running = False
        self._archiver_thread: Optional[threading.Thread] = None
        self._archive_lock = threading.Lock()
        
        logger.info(
            f"TieredStorageManager initialized: "
            f"hot={self.hot_root}, cold={self.cold_root}, "
            f"hot_minutes={self.hot_minutes}"
        )
    
    def get_hot_buffer_path(self, channel_name: str) -> Path:
        """Get hot buffer directory for a channel (for writers)."""
        from ..paths import channel_name_to_dir
        safe_name = channel_name_to_dir(channel_name)
        path = self.hot_root / safe_name
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def get_cold_buffer_path(self, channel_name: str) -> Path:
        """Get cold buffer directory for a channel."""
        from ..paths import channel_name_to_dir
        safe_name = channel_name_to_dir(channel_name)
        path = self.cold_root / safe_name
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def find_minute_file(
        self,
        channel_name: str,
        minute_boundary: int,
        date_str: Optional[str] = None
    ) -> Optional[Path]:
        """
        Find a minute file, checking hot buffer first, then cold.
        
        Args:
            channel_name: Channel name
            minute_boundary: Unix timestamp of minute
            date_str: Optional YYYYMMDD string (computed if not provided)
            
        Returns:
            Path to .bin file (may be .bin.zst), or None if not found
        """
        if date_str is None:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(minute_boundary, tz=timezone.utc)
            date_str = dt.strftime('%Y%m%d')
        
        # Check hot buffer first
        hot_path = self.get_hot_buffer_path(channel_name) / date_str
        for ext in ['.bin', '.bin.zst', '.bin.lz4']:
            candidate = hot_path / f"{minute_boundary}{ext}"
            if candidate.exists():
                return candidate
        
        # Check cold buffer
        cold_path = self.get_cold_buffer_path(channel_name) / date_str
        for ext in ['.bin', '.bin.zst', '.bin.lz4']:
            candidate = cold_path / f"{minute_boundary}{ext}"
            if candidate.exists():
                return candidate
        
        return None
    
    def start(self):
        """Start background archiver thread."""
        if self._running:
            return
        
        self._running = True
        self._archiver_thread = threading.Thread(
            target=self._archiver_loop,
            name="TieredStorageArchiver",
            daemon=True
        )
        self._archiver_thread.start()
        logger.info("TieredStorageManager archiver started")
    
    def stop(self):
        """Stop background archiver thread."""
        self._running = False
        if self._archiver_thread:
            self._archiver_thread.join(timeout=5.0)
            self._archiver_thread = None
        logger.info("TieredStorageManager archiver stopped")
    
    def _archiver_loop(self):
        """Background loop that moves old minutes from hot to cold."""
        while self._running:
            try:
                self._archive_old_minutes()
            except Exception as e:
                logger.error(f"Archiver error: {e}", exc_info=True)
            
            # Sleep in small increments to allow quick shutdown
            for _ in range(int(self.config.archive_interval_seconds)):
                if not self._running:
                    break
                time.sleep(1.0)
    
    def _archive_old_minutes(self):
        """Move minutes older than hot_minutes from hot to cold storage."""
        if not self.config.archive_to_cold:
            return
        
        now = int(time.time())
        cutoff = now - (self.hot_minutes * 60)
        
        with self._archive_lock:
            # Iterate through all channel directories in hot buffer
            for channel_dir in self.hot_root.iterdir():
                if not channel_dir.is_dir():
                    continue
                
                # Iterate through date directories
                for date_dir in channel_dir.iterdir():
                    if not date_dir.is_dir():
                        continue
                    
                    # Find old .bin files
                    for bin_file in date_dir.glob('*.bin*'):
                        try:
                            # Extract minute boundary from filename
                            stem = bin_file.stem
                            if stem.endswith('.bin'):
                                stem = stem[:-4]  # Remove .bin from .bin.zst
                            minute = int(stem)
                            
                            if minute < cutoff:
                                self._move_to_cold(bin_file, channel_dir.name, date_dir.name)
                        except ValueError:
                            continue  # Not a minute file
    
    def _move_to_cold(self, hot_file: Path, channel_name: str, date_str: str):
        """Move a file from hot to cold storage."""
        cold_dir = self.cold_root / channel_name / date_str
        cold_dir.mkdir(parents=True, exist_ok=True)
        
        cold_file = cold_dir / hot_file.name
        
        # Also move the JSON sidecar if it exists
        json_name = hot_file.name.split('.')[0] + '.json'
        hot_json = hot_file.parent / json_name
        cold_json = cold_dir / json_name
        
        try:
            # Move binary file
            shutil.move(str(hot_file), str(cold_file))
            
            # Move JSON sidecar
            if hot_json.exists():
                shutil.move(str(hot_json), str(cold_json))
            
            logger.debug(f"Archived {hot_file.name} to cold storage")
            
        except Exception as e:
            logger.error(f"Failed to archive {hot_file}: {e}")
    
    def get_stats(self) -> Dict:
        """Get storage statistics."""
        hot_files = 0
        hot_bytes = 0
        cold_files = 0
        cold_bytes = 0
        
        for f in self.hot_root.rglob('*.bin*'):
            hot_files += 1
            hot_bytes += f.stat().st_size
        
        for f in self.cold_root.rglob('*.bin*'):
            cold_files += 1
            cold_bytes += f.stat().st_size
        
        return {
            'hot_minutes': self.hot_minutes,
            'hot_files': hot_files,
            'hot_bytes': hot_bytes,
            'hot_mb': hot_bytes / MB,
            'cold_files': cold_files,
            'cold_bytes': cold_bytes,
            'cold_mb': cold_bytes / MB,
        }


# Module-level singleton for easy access
_manager: Optional[TieredStorageManager] = None


def get_tiered_storage_manager(
    config: Optional[TieredStorageConfig] = None
) -> TieredStorageManager:
    """Get or create the global TieredStorageManager singleton."""
    global _manager
    if _manager is None:
        _manager = TieredStorageManager(config)
    return _manager


def init_tiered_storage(
    cold_buffer_root: str,  # Required - must be provided from config
    num_channels: int = 9,
    hot_buffer_root: str = '/dev/shm/timestd',
    ram_percent: float = DEFAULT_RAM_PERCENT,
    auto_start: bool = True
) -> TieredStorageManager:
    """
    Initialize tiered storage with auto-configuration.
    
    Args:
        num_channels: Number of channels being recorded
        hot_buffer_root: Path for RAM-based hot buffer
        cold_buffer_root: Path for disk-based cold buffer
        ram_percent: Percentage of available RAM to use for hot buffer
        auto_start: Start background archiver immediately
        
    Returns:
        Configured TieredStorageManager
    """
    global _manager
    
    config = TieredStorageConfig(
        hot_buffer_root=Path(hot_buffer_root),
        cold_buffer_root=Path(cold_buffer_root),
        auto_configure=True,
        ram_percent=ram_percent,
        num_channels=num_channels,
    )
    
    _manager = TieredStorageManager(config)
    
    if auto_start:
        _manager.start()
    
    return _manager
