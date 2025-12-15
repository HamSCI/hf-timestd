"""
Configuration utilities for standardized path management

Provides centralized path resolution with backward compatibility
and support for environment variable expansion.

DEPRECATION NOTICE (Issue 2.3 - 2025-12-08):
    PathResolver is DEPRECATED. Use TimeStdPaths from hf_timestd.paths instead.
    
    PathResolver has issues:
    - Uses different path structure than TimeStdPaths (adds /data suffix)
    - Has separate development_mode flag that can conflict with config mode
    - Not synchronized with web-ui JavaScript paths
    
    Migration:
        # Old (deprecated):
        from hf_timestd.config_utils import PathResolver
        resolver = PathResolver(config)
        data_dir = resolver.get_data_root()
        
        # New (preferred):
        from hf_timestd.paths import get_paths
        paths = get_paths(config_path='config/timestd-config.toml')
        data_dir = paths.data_root
"""

import os
import logging
import warnings
from pathlib import Path
from typing import Dict, Optional
from string import Template

logger = logging.getLogger(__name__)


class PathResolver:
    """
    Resolves configuration paths with support for:
    - Environment variable expansion
    - Template variable substitution (${paths.xyz})
    - Backward compatibility with old config structure
    - Fallback defaults for systemd deployment
    """
    
    # Default paths for systemd deployment (FHS compliant)
    DEFAULTS = {
        'base_dir': '/var/lib/signal-recorder',
        'data_dir': '/var/lib/signal-recorder/data',
        'analytics_dir': '/var/lib/signal-recorder/analytics',
        'upload_state_dir': '/var/lib/signal-recorder/upload',
        'status_dir': '/var/lib/signal-recorder/status',
        'config_dir': '/etc/signal-recorder',
        'credentials_dir': '/etc/signal-recorder/credentials',
        'log_dir': '/var/log/signal-recorder',
        'web_ui_data_dir': '/var/lib/signal-recorder-web',
    }
    
    # Development fallbacks (if running without installation)
    DEV_DEFAULTS = {
        'base_dir': './data',
        'data_dir': './data/recordings',
        'analytics_dir': './data/analytics',
        'upload_state_dir': './data/upload',
        'status_dir': './data/status',
        'config_dir': './config',
        'credentials_dir': './config/credentials',
        'log_dir': './logs',
        'web_ui_data_dir': './web-ui/data',
    }
    
    def __init__(self, config: Dict, development_mode: bool = False):
        """
        Initialize path resolver
        
        Args:
            config: Parsed TOML configuration
            development_mode: Use development paths instead of system paths
        
        .. deprecated:: 2025-12-08
            Use :class:`hf_timestd.paths.TimeStdPaths` instead.
        """
        warnings.warn(
            "PathResolver is deprecated. Use TimeStdPaths from hf_timestd.paths instead. "
            "See config_utils.py module docstring for migration guide.",
            DeprecationWarning,
            stacklevel=2
        )
        self.config = config
        self.development_mode = development_mode
        self._resolved_paths = {}
        
        # Determine if we're in development or production
        if development_mode or not self._is_production_environment():
            self.defaults = self.DEV_DEFAULTS
            logger.info("Using development path defaults")
        else:
            self.defaults = self.DEFAULTS
            logger.info("Using production path defaults (FHS compliant)")
    
    def _is_production_environment(self) -> bool:
        """Check if we're running in a production environment"""
        # Check for systemd service context
        if os.getenv('INVOCATION_ID'):  # Set by systemd
            return True
        # Check if running as root or dedicated user
        if os.getuid() == 0 or os.getenv('USER') == 'signal-recorder':
            return True
        # Check if FHS directories exist
        if Path('/etc/signal-recorder').exists():
            return True
        return False
    
    def get_paths_config(self) -> Dict[str, str]:
        """
        Get the paths configuration section with defaults
        
        Returns:
            Dictionary of path name -> resolved path
        """
        if self._resolved_paths:
            return self._resolved_paths
        
        # Start with defaults
        paths = self.defaults.copy()
        
        # Override with config values if present
        config_paths = self.config.get('paths', {})
        for key, value in config_paths.items():
            if value:
                paths[key] = value
        
        # Resolve environment variables
        for key in paths:
            paths[key] = os.path.expandvars(paths[key])
            paths[key] = os.path.expanduser(paths[key])
        
        # Resolve template variables (${paths.xyz})
        # Do multiple passes to handle nested references
        for _ in range(3):
            for key in paths:
                try:
                    template = Template(paths[key])
                    paths[key] = template.safe_substitute(paths=paths)
                except Exception as e:
                    logger.warning(f"Failed to resolve template in {key}: {e}")
        
        self._resolved_paths = paths
        return paths
    
    def get_path(self, key: str, create: bool = True) -> Path:
        """
        Get a resolved path by key
        
        Args:
            key: Path key (e.g., 'data_dir', 'analytics_dir')
            create: Create directory if it doesn't exist
            
        Returns:
            Resolved Path object
        """
        paths = self.get_paths_config()
        path_str = paths.get(key, self.defaults.get(key, './data'))
        path = Path(path_str)
        
        if create and not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created directory: {path}")
            except Exception as e:
                logger.error(f"Failed to create directory {path}: {e}")
        
        return path
    
    def get_data_dir(self) -> Path:
        """Get the main data directory for RTP recordings"""
        # Support backward compatibility with old config
        recorder_config = self.config.get('recorder', {})
        
        # NEW: Use development_mode to determine which data_root to use
        # This allows audit tools to test both modes regardless of config setting
        if self.development_mode and 'test_data_root' in recorder_config:
            base_root = Path(recorder_config['test_data_root'])
            return base_root / 'data'
        elif not self.development_mode and 'production_data_root' in recorder_config:
            base_root = Path(recorder_config['production_data_root'])
            return base_root / 'data'
        
        # Priority: recorder.archive_dir > recorder.data_dir > paths.data_dir > default
        if 'archive_dir' in recorder_config:
            return Path(recorder_config['archive_dir'])
        elif 'data_dir' in recorder_config:
            return Path(recorder_config['data_dir'])
        else:
            return self.get_path('data_dir')
    
    def get_analytics_dir(self) -> Path:
        """Get the analytics directory"""
        recorder_config = self.config.get('recorder', {})
        
        # NEW: Use development_mode to determine which data_root to use
        # This allows audit tools to test both modes regardless of config setting
        if self.development_mode and 'test_data_root' in recorder_config:
            base_root = Path(recorder_config['test_data_root'])
            return base_root / 'analytics'
        elif not self.development_mode and 'production_data_root' in recorder_config:
            base_root = Path(recorder_config['production_data_root'])
            return base_root / 'analytics'
        
        # Check for explicit analytics_dir in recorder config
        if 'analytics_dir' in recorder_config:
            return Path(recorder_config['analytics_dir'])
        
        # Check for quality_metrics_dir (backward compat)
        if 'quality_metrics_dir' in recorder_config:
            return Path(recorder_config['quality_metrics_dir']).parent
        
        return self.get_path('analytics_dir')
    
    def get_quality_metrics_dir(self) -> Path:
        """Get the quality metrics directory"""
        recorder_config = self.config.get('recorder', {})
        
        if 'quality_metrics_dir' in recorder_config:
            path_str = recorder_config['quality_metrics_dir']
            # Resolve template variables
            paths = self.get_paths_config()
            path_str = Template(path_str).safe_substitute(paths=paths)
            return Path(path_str)
        
        return self.get_analytics_dir() / 'quality'
    
    def get_wwv_timing_csv(self) -> Path:
        """Get the WWV timing CSV file path"""
        recorder_config = self.config.get('recorder', {})
        
        if 'wwv_timing_csv' in recorder_config:
            path_str = recorder_config['wwv_timing_csv']
            # Resolve template variables
            paths = self.get_paths_config()
            path_str = Template(path_str).safe_substitute(paths=paths)
            return Path(path_str)
        
        return self.get_analytics_dir() / 'timing' / 'wwv_timing.csv'
    
    def get_discontinuity_export_dir(self) -> Path:
        """Get directory for discontinuity CSV exports"""
        recorder_config = self.config.get('recorder', {})
        
        if 'discontinuity_export_dir' in recorder_config:
            path_str = recorder_config['discontinuity_export_dir']
            paths = self.get_paths_config()
            path_str = Template(path_str).safe_substitute(paths=paths)
            return Path(path_str)
        
        return self.get_quality_metrics_dir()
    
    def get_daily_reports_dir(self) -> Path:
        """Get directory for daily reports"""
        recorder_config = self.config.get('recorder', {})
        
        if 'daily_reports_dir' in recorder_config:
            path_str = recorder_config['daily_reports_dir']
            paths = self.get_paths_config()
            path_str = Template(path_str).safe_substitute(paths=paths)
            return Path(path_str)
        
        return self.get_analytics_dir() / 'reports'
    
    def get_upload_state_dir(self) -> Path:
        """Get upload state directory"""
        uploader_config = self.config.get('uploader', {})
        recorder_config = self.config.get('recorder', {})
        
        # Check for explicit queue_file (backward compat)
        if 'queue_file' in uploader_config:
            queue_file = Path(uploader_config['queue_file'])
            return queue_file.parent
        
        # Check for queue_dir (old config)
        if 'queue_dir' in uploader_config:
            return Path(uploader_config['queue_dir'])
        
        # NEW: Use mode-specific data_root for upload state
        # This keeps upload queues separate for test vs production
        if self.development_mode and 'test_data_root' in recorder_config:
            base_root = Path(recorder_config['test_data_root'])
            return base_root / 'upload'
        elif not self.development_mode and 'production_data_root' in recorder_config:
            base_root = Path(recorder_config['production_data_root'])
            return base_root / 'upload'
        
        return self.get_path('upload_state_dir')
    
    def get_upload_queue_file(self) -> Path:
        """Get upload queue JSON file path"""
        uploader_config = self.config.get('uploader', {})
        
        if 'queue_file' in uploader_config:
            return Path(uploader_config['queue_file'])
        
        return self.get_upload_state_dir() / 'queue.json'
    
    def get_status_dir(self) -> Path:
        """Get runtime status directory"""
        recorder_config = self.config.get('recorder', {})
        
        # Use mode-specific data_root for status files
        if self.development_mode and 'test_data_root' in recorder_config:
            base_root = Path(recorder_config['test_data_root'])
            return base_root / 'status'
        elif not self.development_mode and 'production_data_root' in recorder_config:
            base_root = Path(recorder_config['production_data_root'])
            return base_root / 'status'
        
        return self.get_path('status_dir')
    
    def get_status_file(self) -> Path:
        """Get recording stats JSON file path"""
        monitoring_config = self.config.get('monitoring', {})
        
        if 'status_file' in monitoring_config:
            return Path(monitoring_config['status_file'])
        
        return self.get_status_dir() / 'core-recorder-status.json'
    
    def get_credentials_dir(self) -> Path:
        """Get credentials directory (restrictive permissions)"""
        return self.get_path('credentials_dir')
    
    def get_ssh_key_path(self) -> Optional[Path]:
        """Get SSH key path for PSWS uploads"""
        uploader_config = self.config.get('uploader', {})
        rsync_config = uploader_config.get('rsync', {})
        
        if 'ssh_key' not in rsync_config:
            return None
        
        ssh_key = rsync_config['ssh_key']
        
        # Resolve template variables
        paths = self.get_paths_config()
        ssh_key = Template(ssh_key).safe_substitute(paths=paths)
        
        # Expand env vars and user home
        ssh_key = os.path.expandvars(ssh_key)
        ssh_key = os.path.expanduser(ssh_key)
        
        return Path(ssh_key)
    
    def get_jwt_secret_file(self) -> Path:
        """Get JWT secret file path"""
        web_ui_config = self.config.get('web_ui', {})
        
        if 'jwt_secret_file' in web_ui_config:
            path_str = web_ui_config['jwt_secret_file']
            paths = self.get_paths_config()
            path_str = Template(path_str).safe_substitute(paths=paths)
            return Path(path_str)
        
        return self.get_credentials_dir() / 'jwt_secret.txt'
    
    def get_web_ui_data_dir(self) -> Path:
        """Get web UI data directory (users, configs, channels)"""
        web_ui_config = self.config.get('web_ui', {})
        
        if 'data_dir' in web_ui_config:
            return Path(web_ui_config['data_dir'])
        
        return self.get_path('web_ui_data_dir')
    
    def get_log_dir(self) -> Path:
        """Get log directory"""
        logging_config = self.config.get('logging', {})
        recorder_config = self.config.get('recorder', {})
        
        if 'log_dir' in logging_config:
            return Path(logging_config['log_dir'])
        
        # Use mode-specific data_root for logs
        if self.development_mode and 'test_data_root' in recorder_config:
            base_root = Path(recorder_config['test_data_root'])
            return base_root / 'logs'
        elif not self.development_mode and 'production_data_root' in recorder_config:
            base_root = Path(recorder_config['production_data_root'])
            return base_root / 'logs'
        
        return self.get_path('log_dir')
    
    def ensure_directories(self):
        """Create all necessary directories with appropriate permissions"""
        directories = [
            ('data_dir', 0o755),
            ('analytics_dir', 0o755),
            ('upload_state_dir', 0o755),
            ('status_dir', 0o755),
            ('log_dir', 0o755),
            ('credentials_dir', 0o700),  # Restrictive permissions
            ('web_ui_data_dir', 0o755),
        ]
        
        for dir_key, mode in directories:
            path = self.get_path(dir_key, create=True)
            
            # Set permissions (only if we created it)
            if path.exists():
                try:
                    path.chmod(mode)
                    if mode == 0o700:
                        logger.info(f"Set restrictive permissions on {path}")
                except Exception as e:
                    logger.warning(f"Failed to set permissions on {path}: {e}")
        
        # Create subdirectories
        self.get_quality_metrics_dir().mkdir(parents=True, exist_ok=True)
        self.get_wwv_timing_csv().parent.mkdir(parents=True, exist_ok=True)
        self.get_daily_reports_dir().mkdir(parents=True, exist_ok=True)
    
    def print_summary(self):
        """Print a summary of resolved paths"""
        paths = self.get_paths_config()
        
        print("\n" + "="*70)
        print("SIGNAL RECORDER PATH CONFIGURATION")
        print("="*70)
        print(f"Mode: {'Development' if self.development_mode else 'Production'}")
        print()
        
        print("RTP Data and Analytics (SAFE TO DELETE):")
        print(f"  Data Directory:       {self.get_data_dir()}")
        print(f"  Analytics Directory:  {self.get_analytics_dir()}")
        print(f"  Upload State:         {self.get_upload_state_dir()}")
        print(f"  Runtime Status:       {self.get_status_dir()}")
        print()
        
        print("Site Management (DO NOT DELETE):")
        print(f"  Configuration:        {paths.get('config_dir')}")
        print(f"  Credentials:          {self.get_credentials_dir()}")
        print(f"  Web UI Data:          {self.get_web_ui_data_dir()}")
        print()
        
        print("Logs:")
        print(f"  Log Directory:        {self.get_log_dir()}")
        print()
        
        print("Specific Files:")
        print(f"  Recording Stats:      {self.get_status_file()}")
        print(f"  Upload Queue:         {self.get_upload_queue_file()}")
        print(f"  WWV Timing CSV:       {self.get_wwv_timing_csv()}")
        
        ssh_key = self.get_ssh_key_path()
        if ssh_key:
            print(f"  SSH Key:              {ssh_key}")
        
        print("="*70 + "\n")


def load_config_with_paths(config_file: Path, development_mode: bool = False) -> tuple[Dict, PathResolver]:
    """
    Load configuration and create path resolver
    
    Args:
        config_file: Path to TOML configuration file
        development_mode: Use development paths
        
    Returns:
        Tuple of (config dict, PathResolver)
    """
    import toml
    
    with open(config_file, 'r') as f:
        config = toml.load(f)
    
    resolver = PathResolver(config, development_mode=development_mode)
    resolver.ensure_directories()
    
    return config, resolver
