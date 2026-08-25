"""
FastAPI Web UI Configuration

Loads configuration from timestd-config.toml and provides
data paths for the web API.
"""

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python < 3.11

from pathlib import Path
from typing import Dict, Any, List
import logging
import os

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_DATA_ROOT = Path("/var/lib/timestd")

# Repo-relative config, kept LAST: it exists only if someone created it
# inside the checkout, and the repo ships just the .template.  A deployed
# web-api reads /etc like every other component.
_REPO_CONFIG_PATH = Path(__file__).parent.parent / "config" / "timestd-config.toml"


def candidate_config_paths():
    """Where to look for the station config, in order.

    Mirrors ``hf_timestd.cli``: $TIMESTD_CONFIG, then the production
    location under /etc, then the repo copy for development.
    """
    paths = []
    env = os.environ.get("TIMESTD_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(Path("/etc/hf-timestd/timestd-config.toml"))
    paths.append(_REPO_CONFIG_PATH)
    return paths


def resolve_config_path():
    """First candidate that exists; else the first candidate (for the message)."""
    candidates = candidate_config_paths()
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


DEFAULT_CONFIG_PATH = _REPO_CONFIG_PATH   # back-compat for explicit callers


class Config:
    """Application configuration."""
    
    def __init__(self, config_path: Path = None):
        """
        Load configuration from TOML file.
        
        Args:
            config_path: Path to timestd-config.toml
        """
        self.config_path = Path(config_path) if config_path is not None \
            else resolve_config_path()
        self._load_config()
    
    def _load_config(self):
        """Load configuration from TOML file.

        A missing file is NOT fatal at import: this module builds a
        singleton on import, and raising there means a clean checkout (or
        an unconfigured host) cannot even import the web API to ask it
        what is wrong.  Missing config yields an empty one and a warning;
        callers that need a value still fail where the value is used.
        """
        if not self.config_path.exists():
            logger.warning(
                "No station config found at %s (looked in: %s) — continuing "
                "with empty configuration",
                self.config_path,
                ", ".join(str(p) for p in candidate_config_paths()),
            )
            self.config = {}
            self._apply_config()
            return
        
        try:
            with open(self.config_path, 'rb') as f:
                self.config = tomllib.load(f)
            logger.info(f"Loaded configuration from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            raise
        self._apply_config()

    def _apply_config(self):
        """Derive the fields the app reads from ``self.config``.

        Split out of :meth:`_load_config` so the no-config path produces a
        usable object with empty sections and default paths, instead of a
        half-built one.
        """
        # Extract key configuration
        self.station = self.config.get('station', {})
        self.recorder = self.config.get('recorder', {})
        self.web_ui = self.config.get('web_ui', {})
        self.gnss_vtec = self.config.get('gnss_vtec', {})
        # [storage] backend selection (HDF5→SQLite migration) — passed to
        # make_data_product_reader so web-api reads follow read_sqlite.
        self.storage = self.config.get('storage', {})
        
        # Determine data root based on mode
        mode = self.recorder.get('mode', 'production')
        if mode == 'test':
            self.data_root = Path(self.recorder.get('test_data_root', '/tmp/timestd-test'))
        else:
            self.data_root = Path(self.recorder.get('production_data_root', '/var/lib/timestd'))
        
        # Data paths
        self.phase2_dir = self.data_root / 'phase2'
        self.fusion_dir = self.phase2_dir / 'fusion'
        self.science_dir = self.phase2_dir / 'science'
        self.tec_dir = self.science_dir / 'tec'
        self.propagation_dir = self.science_dir / 'propagation'
        self.gnss_vtec_dir = self.data_root / 'data' / 'gnss_vtec'
        self.status_dir = self.data_root / 'data' / 'status'
        
        # Channels from config
        self.channels = self._parse_channels()
    
    def _parse_channels(self) -> List[Dict[str, Any]]:
        """
        Parse channel configuration from recorder.channel_group.*.channels.
        
        Returns:
            List of channel dictionaries with frequency and description
        """
        channels = []
        channel_groups = self.recorder.get('channel_group', {})
        for group_name, group_config in channel_groups.items():
            for channel_config in group_config.get('channels', []):
                freq_hz = channel_config.get('frequency_hz')
                description = channel_config.get('description', '')
                if freq_hz:
                    channels.append({
                        'frequency_hz': freq_hz,
                        'frequency_mhz': freq_hz / 1e6,
                        'description': description,
                        'channel_name': description
                    })
        return channels
    
    def get_channel_dir(self, channel_name: str) -> Path:
        """
        Get data directory for a specific channel.
        
        Args:
            channel_name: Channel name (e.g., 'WWV_10000', 'SHARED_5000')
            
        Returns:
            Path to channel directory
        """
        return self.phase2_dir / channel_name
    
    @property
    def station_metadata(self) -> Dict[str, Any]:
        """
        Get station metadata.
        
        Returns:
            Dictionary with station information
        """
        return {
            'callsign': self.station.get('callsign', 'UNKNOWN'),
            'grid_square': self.station.get('grid_square', ''),
            'station_id': self.station.get('id', ''),
            'instrument_id': self.station.get('instrument_id', ''),
            'description': self.station.get('description', ''),
            'latitude': self.station.get('latitude', 0.0),
            'longitude': self.station.get('longitude', 0.0),
            'mode': self.recorder.get('mode', 'production')
        }


# Global config instance
config = Config()
