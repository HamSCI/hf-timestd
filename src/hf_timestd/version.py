"""
HF Time Standard Analysis - Version Information

Centralized version constants for hf-timestd.
All modules should import version from here rather than defining their own.

Provides standardized timestamp utilities that always use UTC with explicit
timezone markers.
"""

from datetime import datetime, timezone
from typing import Optional

# =============================================================================
# VERSION CONSTANTS
# =============================================================================

# Main hf-timestd version - follows semantic versioning
TIMESTD_VERSION = "3.0.0"

# Component versions (for tracking algorithm changes)
COMPONENT_VERSIONS = {
    'core_recorder': '2.0',
    'phase2_analytics': '2.0',
    'phase2_temporal_engine': '2.1.0',
    'clock_convergence': '2.0',  # Kalman filter version
    'multi_broadcast_fusion': '1.1',  # Per-broadcast calibration
}

# State file version - increment when state schema changes
# Older state files will be discarded on load
STATE_FILE_VERSION = 2

# Data contract version - for CSV/JSON schema tracking
DATA_CONTRACT_VERSION = "2025-12-08-v1"


# =============================================================================
# TIMESTAMP UTILITIES (Issue 3.2 Fix)
# =============================================================================

def utc_now() -> datetime:
    """Get current time in UTC with timezone info.
    
    Always use this instead of datetime.now() for consistency.
    """
    return datetime.now(timezone.utc)


def utc_timestamp() -> float:
    """Get current Unix timestamp (seconds since epoch)."""
    return utc_now().timestamp()


def utc_isoformat() -> str:
    """Get current time as ISO 8601 string with 'Z' suffix.
    
    Example: '2025-12-08T21:05:00Z'
    
    All hf-timestd status files should use this format for timestamps.
    """
    return utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')


def utc_isoformat_ms() -> str:
    """Get current time as ISO 8601 string with milliseconds and 'Z' suffix.
    
    Example: '2025-12-08T21:05:00.123Z'
    """
    return utc_now().strftime('%Y-%m-%dT%H:%M:%S.') + \
           f"{utc_now().microsecond // 1000:03d}Z"


def parse_utc_isoformat(timestamp: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string to datetime (UTC).
    
    Handles both 'Z' suffix and '+00:00' timezone notation.
    
    Args:
        timestamp: ISO 8601 string (e.g., '2025-12-08T21:05:00Z')
        
    Returns:
        datetime with UTC timezone, or None if parsing fails
    """
    if not timestamp:
        return None
    
    try:
        # Handle 'Z' suffix
        if timestamp.endswith('Z'):
            timestamp = timestamp[:-1] + '+00:00'
        return datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None


# =============================================================================
# STATUS FILE UTILITIES
# =============================================================================

def create_status_header(service_name: str, extra_fields: dict = None) -> dict:
    """Create standard status file header.
    
    All hf-timestd status files should start with this header for consistency.
    
    Args:
        service_name: Name of the service (e.g., 'core_recorder', 'phase2_analytics')
        extra_fields: Additional fields to include
        
    Returns:
        Dict with standard header fields
    """
    header = {
        'service': service_name,
        'version': COMPONENT_VERSIONS.get(service_name, TIMESTD_VERSION),
        'timestd_version': TIMESTD_VERSION,
        'timestamp': utc_isoformat(),
        'data_contract_version': DATA_CONTRACT_VERSION,
    }
    if extra_fields:
        header.update(extra_fields)
    return header


# =============================================================================
# VERSION INFO FOR LOGGING
# =============================================================================

def get_version_string() -> str:
    """Get formatted version string for logging."""
    return f"hf-timestd v{TIMESTD_VERSION}"


def log_version_info(logger) -> None:
    """Log version information at startup.
    
    Args:
        logger: Logger instance to use
    """
    logger.info(f"📡 {get_version_string()}")
    logger.info(f"   Data contract: {DATA_CONTRACT_VERSION}")
    logger.info(f"   State file version: {STATE_FILE_VERSION}")
