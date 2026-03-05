"""
NASA Earthdata / CDDIS Authentication Helper

Centralizes credential management for all NASA CDDIS downloads (IONEX, DCB).

Credential Lookup Order:
1. NETRC environment variable (set by systemd EnvironmentFile)
2. /etc/hf-timestd/earthdata-netrc (system-wide, preferred for services)
3. ~/.netrc (user home directory, fallback for interactive use)

The netrc file must contain:
    machine urs.earthdata.nasa.gov
    login YOUR_USERNAME
    password YOUR_PASSWORD

File permissions must be 0600 (owner read/write only).
"""

import logging
import netrc
import os
import stat
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Default system-wide credential path
SYSTEM_NETRC = Path('/etc/hf-timestd/earthdata-netrc')
EARTHDATA_HOST = 'urs.earthdata.nasa.gov'


def find_netrc_path() -> Optional[Path]:
    """
    Locate the netrc file containing CDDIS credentials.

    Returns:
        Path to netrc file, or None if not found.
    """
    # 1. NETRC env var (highest priority, set by systemd unit)
    env_netrc = os.environ.get('NETRC')
    if env_netrc:
        p = Path(env_netrc)
        if p.is_file():
            return p
        logger.warning(f"NETRC env points to non-existent file: {env_netrc}")

    # 2. System-wide location
    if SYSTEM_NETRC.is_file():
        return SYSTEM_NETRC

    # 3. User home ~/.netrc
    home_netrc = Path.home() / '.netrc'
    if home_netrc.is_file():
        return home_netrc

    return None


def validate_netrc(path: Path) -> Tuple[bool, str]:
    """
    Validate that a netrc file exists, has correct permissions, and contains
    Earthdata credentials.

    Returns:
        (ok, message) tuple.
    """
    if not path.exists():
        return False, f"Netrc file not found: {path}"

    # Check permissions (must be 0600)
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        return False, (
            f"Netrc file {path} has permissions {oct(mode)} (must be 0600). "
            f"Fix with: chmod 600 {path}"
        )

    # Check it contains Earthdata credentials
    try:
        nrc = netrc.netrc(str(path))
        auth = nrc.authenticators(EARTHDATA_HOST)
        if auth is None:
            return False, (
                f"No entry for '{EARTHDATA_HOST}' in {path}. "
                f"Add:\n  machine {EARTHDATA_HOST}\n  login YOUR_USERNAME\n  password YOUR_PASSWORD"
            )
        login, _, password = auth
        if not login or not password:
            return False, f"Incomplete credentials for '{EARTHDATA_HOST}' in {path}"
    except netrc.NetrcParseError as e:
        return False, f"Failed to parse {path}: {e}"

    return True, f"Earthdata credentials OK in {path}"


def get_cddis_session() -> requests.Session:
    """
    Create a requests.Session pre-configured with NASA Earthdata credentials.

    The session uses HTTP Basic Auth loaded from the netrc file.
    CDDIS redirects through urs.earthdata.nasa.gov for OAuth, and
    requests handles the redirect chain automatically when auth is set.

    Raises:
        FileNotFoundError: If no netrc file is found.
        ValueError: If the netrc file is invalid or missing credentials.
    """
    netrc_path = find_netrc_path()
    if netrc_path is None:
        raise FileNotFoundError(
            "No NASA Earthdata credentials found. Create /etc/hf-timestd/earthdata-netrc with:\n"
            f"  machine {EARTHDATA_HOST}\n"
            "  login YOUR_USERNAME\n"
            "  password YOUR_PASSWORD\n"
            "Then: chmod 600 /etc/hf-timestd/earthdata-netrc && chown timestd:timestd /etc/hf-timestd/earthdata-netrc"
        )

    ok, msg = validate_netrc(netrc_path)
    if not ok:
        raise ValueError(msg)

    logger.debug(f"Using Earthdata credentials from {netrc_path}")

    nrc = netrc.netrc(str(netrc_path))
    login, _, password = nrc.authenticators(EARTHDATA_HOST)

    session = requests.Session()
    session.auth = (login, password)
    return session


def check_earthdata_credentials() -> Tuple[bool, str]:
    """
    Startup check: verify Earthdata credentials are available and valid.
    Suitable for calling at service init to fail loudly if misconfigured.

    Returns:
        (ok, message) tuple.
    """
    netrc_path = find_netrc_path()
    if netrc_path is None:
        return False, (
            "NASA Earthdata credentials not found. "
            "IONEX/DCB downloads will fail. "
            "See docs/NASA_EARTHDATA_SETUP.md for setup instructions."
        )
    return validate_netrc(netrc_path)
