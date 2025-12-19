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
        channel_name: Human-readable name (e.g., "WWV 10 MHz", "CHU 3.33 MHz")
    
    Returns:
        Key format: "wwv10", "chu3.33", etc.
    
    Examples:
        >>> channel_name_to_key("WWV 10 MHz")
        'wwv10'
        >>> channel_name_to_key("WWV 2.5 MHz")
        'wwv2.5'
        >>> channel_name_to_key("CHU 3.33 MHz")
        'chu3.33'
    """
    parts = channel_name.split()
    if len(parts) < 2:
        # Fallback: underscored lowercase
        return channel_name.replace(' ', '_').lower()
    
    station = parts[0].lower()  # wwv, chu
    freq = parts[1]             # 10, 2.5, 3.33
    
    return f"{station}{freq}"


def channel_name_to_dir(channel_name: str) -> str:
    """Convert channel name to directory format (Station_kHz).
    
    Examples:
        "WWV 10 MHz"  -> "SHARED_10000"
        "CHU 3.33 MHz" -> "CHU_3330"
    """
    parts = channel_name.split()
    if len(parts) < 2:
        return channel_name.replace(' ', '_')
    
    station = parts[0].upper().replace('/', '')
    try:
        freq_mhz = float(parts[1])
        khz = int(round(freq_mhz * 1000))
    except (ValueError, IndexError):
        return channel_name.replace(' ', '_')

    # SHARED frequencies (WWV/WWVH/BPM)
    if khz in {2500, 5000, 10000, 15000}:
        return f"SHARED_{khz}"
    
    return f"{station}_{khz}"


def dir_to_channel_name(dir_name: str) -> str:
    """Convert directory name back to human-readable format.
    
    Args:
        dir_name: Directory name (e.g., "SHARED_10000")
    
    Returns:
        Human-readable approximation (best effort)
    """
    if dir_name.startswith('SHARED_'):
        khz = dir_name.split('_')[1]
        mhz = float(khz) / 1000
        return f"SHARED {mhz:g} MHz"
    
    parts = dir_name.split('_')
    if len(parts) == 2:
        station, khz = parts
        try:
            mhz = float(khz) / 1000
            return f"{station} {mhz:g} MHz"
        except ValueError:
            pass
            
    return dir_name.replace('_', ' ')


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
        """DEPRECATED: Use get_clock_offset_dir() instead."""
        return self.get_analytics_dir(channel_name) / 'timing'
    
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
