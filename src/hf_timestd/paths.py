"""
TimeStd Path Specification

This module provides the canonical path structure for all TimeStd data.
ALL producers and consumers MUST use these functions to avoid path mismatches.

SYNC VERSION: 2025-12-08-v3-discovery-fix
Must stay synchronized with web-ui/timestd-paths.js

Phase 1 storage uses per-minute binary complex64 files in raw_buffer/.
Phase 2 writes analytical outputs in phase2/.

CRITICAL: Use channel_name_to_key() for consistent channel naming.
"""

from pathlib import Path
from typing import Optional
import toml


def channel_name_to_key(channel_name: str) -> str:
    """Convert channel name to consistent key format.
    
    Args:
        channel_name: Canonical format "STATION_KILOHERTZ" (e.g., "SHARED_10000", "CHU_3330")
    
    Returns:
        Key format: "shared10000", "chu3330", etc.
    
    Examples:
        >>> channel_name_to_key("SHARED_10000")
        'shared10000'
        >>> channel_name_to_key("CHU_3330")
        'chu3330'
    """
    # Handle canonical STATION_KILOHERTZ format
    if '_' in channel_name and channel_name.split('_')[0] in ('SHARED', 'WWV', 'CHU'):
        parts = channel_name.split('_')
        return f"{parts[0].lower()}{parts[1]}"
    
    # Fallback: underscored lowercase
    return channel_name.replace(' ', '_').replace('_', '').lower()


def channel_name_to_dir(channel_name: str) -> str:
    """Convert channel name to directory format (Station_kHz).
    
    The canonical format is STATION_KILOHERTZ (e.g., SHARED_10000, CHU_3330).
    This function passes through canonical format unchanged.
    
    Examples:
        "SHARED_10000" -> "SHARED_10000" (pass-through)
        "CHU_3330"     -> "CHU_3330" (pass-through)
    """
    # Already in canonical STATION_KILOHERTZ format - pass through
    if '_' in channel_name:
        parts = channel_name.split('_')
        if len(parts) == 2 and parts[0] in ('SHARED', 'WWV', 'CHU') and parts[1].isdigit():
            return channel_name
    
    # Fallback: replace spaces with underscores
    return channel_name.replace(' ', '_')


def dir_to_channel_name(dir_name: str) -> str:
    """Return directory name unchanged (canonical format is STATION_KILOHERTZ).
    
    Args:
        dir_name: Directory name (e.g., "SHARED_10000", "CHU_3330")
    
    Returns:
        Same as input - canonical format is used throughout
    """
    return dir_name


def channel_to_display_name(channel_name: str) -> str:
    """Convert canonical channel name to human-readable display format.
    
    This is ONLY for UI display purposes. All internal operations use
    the canonical STATION_KILOHERTZ format.
    
    Args:
        channel_name: Canonical format (e.g., "SHARED_10000", "CHU_3330")
    
    Returns:
        Display format (e.g., "SHARED 10 MHz", "CHU 3.33 MHz")
    """
    parts = channel_name.split('_')
    if len(parts) == 2 and parts[1].isdigit():
        station = parts[0]
        khz = int(parts[1])
        mhz = khz / 1000
        # Format: integer if whole number, otherwise show decimals
        if mhz == int(mhz):
            mhz_str = str(int(mhz))
        else:
            mhz_str = f"{mhz:.2f}".rstrip('0').rstrip('.')
        return f"{station} {mhz_str} MHz"
    
    return channel_name.replace('_', ' ')


class TimeStdPaths:
    """Central path manager for TimeStd two-phase pipeline.
    
    Usage:
        from hf_timestd.paths import TimeStdPaths
        
        paths = TimeStdPaths('/tmp/timestd-test')
        
        # Phase 1: Raw buffer (binary complex64 + JSON sidecars)
        raw_dir = paths.get_raw_buffer_dir('WWV 10 MHz')
        
        # Phase 2: Analytical engine outputs
        clock_dir = paths.get_clock_offset_dir('WWV 10 MHz')
    """
    
    def __init__(self, data_root: str | Path):
        """Initialize path manager.
        
        Args:
            data_root: Root data directory (e.g., /tmp/timestd-test)
        """
        self.data_root = Path(data_root)


    # ========================================================================
    # PHASE 1: RAW BUFFER (Binary complex64 + JSON sidecars)
    # ========================================================================
    
    def get_raw_buffer_root(self) -> Path:
        """Get raw buffer root directory.
        
        Returns: {data_root}/raw_buffer/
        """
        return self.data_root / 'raw_buffer'
    
    def get_raw_buffer_dir(self, channel_name: str) -> Path:
        """Get raw buffer directory for a channel.
        
        Returns: {data_root}/raw_buffer/{CHANNEL}/
        """
        channel_dir = channel_name_to_dir(channel_name)
        return self.get_raw_buffer_root() / channel_dir
    
    def get_raw_buffer_date_dir(self, channel_name: str, date: str) -> Path:
        """Get raw archive date directory.
        
        Args:
            channel_name: Channel name
            date: Date in YYYYMMDD format
        
        Returns: {data_root}/raw_buffer/{CHANNEL}/{YYYYMMDD}/
        """
        return self.get_raw_buffer_dir(channel_name) / date
    
    def get_raw_buffer_metadata_dir(self, channel_name: str, date: str) -> Path:
        """Get raw archive metadata directory.
        
        Returns: {data_root}/raw_buffer/{CHANNEL}/{YYYYMMDD}/metadata/
        """
        return self.get_raw_buffer_date_dir(channel_name, date) / 'metadata'
    
    # ========================================================================
    # PHASE 2: ANALYTICAL ENGINE
    # ========================================================================
    
    def get_phase2_root(self) -> Path:
        """Get Phase 2 root directory.
        
        Returns: {data_root}/phase2/
        """
        return self.data_root / 'phase2'
    
    def get_phase2_dir(self, channel_name: str) -> Path:
        """Get Phase 2 directory for a channel.
        
        Returns: {data_root}/phase2/{CHANNEL}/
        """
        channel_dir = channel_name_to_dir(channel_name)
        return self.get_phase2_root() / channel_dir
    
    def get_clock_offset_dir(self, channel_name: str) -> Path:
        """Get clock offset series directory.
        
        Contains D_clock(t) time series - the primary Phase 2 output.
        
        Returns: {data_root}/phase2/{CHANNEL}/clock_offset/
        """
        return self.get_phase2_dir(channel_name) / 'clock_offset'
    
    def get_carrier_analysis_dir(self, channel_name: str) -> Path:
        """Get carrier analysis directory.
        
        Contains amplitude, phase, and Doppler measurements.
        
        Returns: {data_root}/phase2/{CHANNEL}/carrier_analysis/
        """
        return self.get_phase2_dir(channel_name) / 'carrier_analysis'
    
    def get_channel_quality_dir(self, channel_name: str) -> Path:
        """Get channel quality metrics directory.
        
        Contains delay spread, coherence time, spreading factor.
        
        Returns: {data_root}/phase2/{CHANNEL}/channel_quality/
        """
        return self.get_phase2_dir(channel_name) / 'channel_quality'
    
    def get_discrimination_dir(self, channel_name: str) -> Path:
        """Get WWV/WWVH discrimination directory.
        
        Contains per-minute station identification results.
        
        Returns: {data_root}/phase2/{CHANNEL}/discrimination/
        """
        return self.get_phase2_dir(channel_name) / 'discrimination'
    
    def get_bcd_correlation_dir(self, channel_name: str) -> Path:
        """Get BCD correlation directory.
        
        Contains 100 Hz subcarrier cross-correlation results.
        
        Returns: {data_root}/phase2/{CHANNEL}/bcd_correlation/
        """
        return self.get_phase2_dir(channel_name) / 'bcd_correlation'
    
    def get_tone_detections_dir(self, channel_name: str) -> Path:
        """Get tone detections directory.
        
        Contains 1000/1200 Hz minute marker detection results.
        
        Returns: {data_root}/phase2/{CHANNEL}/tone_detections/
        """
        return self.get_phase2_dir(channel_name) / 'tone_detections'
    
    def get_ground_truth_dir(self, channel_name: str) -> Path:
        """Get ground truth directory.
        
        Contains 440/500/600 Hz exclusive tone detections.
        
        Returns: {data_root}/phase2/{CHANNEL}/ground_truth/
        """
        return self.get_phase2_dir(channel_name) / 'ground_truth'
    
    def get_doppler_dir(self, channel_name: str) -> Path:
        """Get Doppler estimation directory.
        
        Contains per-tick phase tracking and coherence estimates.
        
        Returns: {data_root}/phase2/{CHANNEL}/doppler/
        """
        return self.get_phase2_dir(channel_name) / 'doppler'
    
    def get_phase2_state_dir(self, channel_name: str) -> Path:
        """Get Phase 2 processing state directory.
        
        Returns: {data_root}/phase2/{CHANNEL}/state/
        """
        return self.get_phase2_dir(channel_name) / 'state'
    
    # ========================================================================
    # LEGACY COMPATIBILITY (deprecated - use phase-specific methods)
    # ========================================================================
    
    def get_archive_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Get legacy NPZ archive directory.
        
        Use get_raw_buffer_dir() for new code.
        
        Returns: {data_root}/archives/{CHANNEL}/
        """
        channel_dir = channel_name_to_dir(channel_name)
        return self.data_root / 'archives' / channel_dir
    
    def get_archive_file(self, channel_name: str, timestamp: str, frequency_hz: int) -> Path:
        """DEPRECATED: Get path for legacy NPZ archive file.
        
        Returns: {data_root}/archives/{CHANNEL}/{timestamp}_{freq}_iq.npz
        """
        archive_dir = self.get_archive_dir(channel_name)
        return archive_dir / f"{timestamp}_{frequency_hz}_iq.npz"
    
    def get_analytics_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Get legacy analytics directory.
        
        Use get_phase2_dir() for new code.
        
        Returns: {data_root}/analytics/{CHANNEL}/
        """
        channel_dir = channel_name_to_dir(channel_name)
        return self.data_root / 'analytics' / channel_dir
    
    def get_digital_rf_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Get legacy Digital RF directory.
        
        Use get_decimated_dir() for Phase 3 decimated DRF.
        
        Returns: {data_root}/analytics/{CHANNEL}/digital_rf/
        """
        return self.get_analytics_dir(channel_name) / 'digital_rf'
    
    # Legacy analytics subdirectories (for backward compatibility)
    
    def get_tick_windows_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_doppler_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'tick_windows'
    
    def get_station_id_440hz_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_ground_truth_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'station_id_440hz'
    
    def get_bcd_discrimination_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_bcd_correlation_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'bcd_discrimination'
    
    def get_test_signal_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_ground_truth_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'test_signal'
    
    def get_timing_dir(self, channel_name: str) -> Path:
        """Get Phase 2 timing results directory.
        
        Returns: {data_root}/phase2/{CHANNEL}/timing/
        """
        return self.get_phase2_dir(channel_name) / 'timing'
    
    def get_quality_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_channel_quality_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'quality'
    
    def get_analytics_logs_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Get analytics logs directory."""
        return self.get_analytics_dir(channel_name) / 'logs'
    
    def get_analytics_status_dir(self, channel_name: str) -> Path:
        """DEPRECATED: Use get_phase2_state_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'status'
    
    # ========================================================================
    # Discovery Methods
    # ========================================================================
    
    # Directories that are not channels (exclude from discovery)
    _EXCLUDE_DIRS = {'status', 'metadata', 'state', 'logs', 'fusion', 'upload'}
    
    def discover_channels(self) -> list[str]:
        """Discover all channels from any available data source.
        
        Checks Phase 1 (raw_buffer) and Phase 2 (phase2) to find channels.
        
        This now matches the JavaScript implementation in timestd-paths.js.
        
        Returns:
            List of channel names (human-readable format)
        """
        channels = set()
        
        # Check Phase 1: raw_buffer/
        raw_dir = self.get_raw_buffer_root()
        if raw_dir.exists():
            for channel_dir in raw_dir.iterdir():
                if channel_dir.is_dir() and channel_dir.name not in self._EXCLUDE_DIRS:
                    channels.add(dir_to_channel_name(channel_dir.name))
        
        # Check Phase 2: phase2/
        phase2_dir = self.get_phase2_root()
        if phase2_dir.exists():
            for channel_dir in phase2_dir.iterdir():
                if channel_dir.is_dir() and channel_dir.name not in self._EXCLUDE_DIRS:
                    channels.add(dir_to_channel_name(channel_dir.name))
        
        # Fall back to legacy archives directory if nothing found
        if not channels:
            archives_dir = self.data_root / 'archives'
            if archives_dir.exists():
                for channel_dir in archives_dir.iterdir():
                    if channel_dir.is_dir() and channel_dir.name not in self._EXCLUDE_DIRS:
                        channels.add(dir_to_channel_name(channel_dir.name))
        
        return sorted(channels)
    
    def discover_phase2_channels(self) -> list[str]:
        """Discover channels with Phase 2 analytical results.
        
        Returns:
            List of channel names with Phase 2 data
        """
        phase2_dir = self.get_phase2_root()
        if not phase2_dir.exists():
            return []
        
        channels = []
        for channel_dir in phase2_dir.iterdir():
            if channel_dir.is_dir() and channel_dir.name not in self._EXCLUDE_DIRS:
                channels.append(dir_to_channel_name(channel_dir.name))
        
        return sorted(channels)
    
    def discover_products_channels(self) -> list[str]:
        """Discover channels with Phase 3 derived products."""
        return []


def load_paths_from_config(config_path: Optional[str | Path] = None) -> TimeStdPaths:
    """Load TimeStdPaths from configuration file.
    
    Args:
        config_path: Path to timestd-config.toml (default: ./config/timestd-config.toml)
    
    Returns:
        TimeStdPaths instance configured from TOML
    
    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config is invalid
    """
    if config_path is None:
        # Default location
        config_path = Path(__file__).parent.parent.parent / 'config' / 'timestd-config.toml'
    
    config_path = Path(config_path)
    
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        config = toml.load(f)
    
    # Determine data root based on mode
    mode = config.get('recorder', {}).get('mode', 'test')
    
    if mode == 'production':
        data_root = config.get('recorder', {}).get('production_data_root', '/var/lib/timestd')
    else:
        data_root = config.get('recorder', {}).get('test_data_root', '/tmp/timestd-test')
    
    return TimeStdPaths(data_root)


# Convenience function for scripts
def get_paths(data_root: Optional[str | Path] = None, 
              config_path: Optional[str | Path] = None) -> TimeStdPaths:
    """Get TimeStdPaths instance.
    
    Args:
        data_root: Explicit data root (overrides config)
        config_path: Path to config file (if using config)
    
    Returns:
        TimeStdPaths instance
    
    Usage:
        # Use explicit data root
        paths = get_paths('/tmp/timestd-test')
        
        # Use config file
        paths = get_paths(config_path='config/timestd-config.toml')
        
        # Use default config
        paths = get_paths()
    """
    if data_root is not None:
        return TimeStdPaths(data_root)
    
    return load_paths_from_config(config_path)
