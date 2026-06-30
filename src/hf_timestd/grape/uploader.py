"""
Upload manager module

Handles reliable upload of processed datasets to remote repositories.
"""

import os
import re
import subprocess
import logging
import time
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import json

# Optional imports
try:
    import digital_rf as drf
    HAS_DIGITAL_RF = True
except ImportError:
    HAS_DIGITAL_RF = False

logger = logging.getLogger(__name__)


def load_upload_config_from_toml(toml_config: Dict, path_resolver=None) -> Dict:
    """
    Convert TOML configuration to UploadManager format.
    
    Args:
        toml_config: Parsed TOML configuration dict
        path_resolver: Optional PathResolver for standardized paths
        
    Returns:
        Dict suitable for UploadManager initialization
    """
    uploader = toml_config.get('uploader', {})
    if uploader is None:
        uploader = {}
        
    station = toml_config.get('station', {})
    if station is None:
        station = {}
    
    # Determine protocol
    protocol = uploader.get('protocol', 'sftp')
    
    # Get protocol-specific config
    if protocol == 'sftp':
        proto_config = uploader.get('sftp', {})
    else:
        proto_config = uploader.get('rsync', {})
    
    # Get queue file path
    if path_resolver:
        queue_file = path_resolver.get_upload_queue_file()
    elif 'queue_file' in uploader:
        queue_file = Path(uploader['queue_file'])
    elif 'queue_dir' in uploader:
        queue_file = Path(uploader['queue_dir']) / 'queue.json'
    else:
        queue_file = Path('/var/lib/signal-recorder/upload/queue.json')
    
    # Get SSH key path
    ssh_key = proto_config.get('ssh_key', '')
    if path_resolver and ssh_key:
        ssh_key_path = path_resolver.get_ssh_key_path()
        if ssh_key_path:
            ssh_key = str(ssh_key_path)
    
    # Build unified config
    config = {
        'protocol': protocol,
        'host': proto_config.get('host', 'pswsnetwork.eng.ua.edu'),
        'user': proto_config.get('user', station.get('id', '')),
        'ssh': {
            'key_file': ssh_key
        },
        'bandwidth_limit_kbps': proto_config.get('bandwidth_limit_kbps', 
                                                 proto_config.get('bandwidth_limit', 100)),
        'max_retries': uploader.get('max_retries', 5),
        'retry_backoff_base': 2 if uploader.get('exponential_backoff', True) else 1,
        'queue_file': queue_file
    }
    
    return config


def test_psws_connectivity(toml_config: Dict) -> bool:
    """Test PSWS SFTP connectivity and SSH key authentication.

    Three-step preflight check (mirrors wsprdaemon wd-sftp-psws):
      1. TCP connectivity to PSWS server port 22
      2. SSH private key file exists and has correct permissions
      3. SFTP autologin (connect + disconnect with empty batch)

    Args:
        toml_config: Parsed TOML configuration dict

    Returns:
        True if all checks pass
    """
    station = toml_config.get('station', {}) or {}
    uploader = toml_config.get('uploader', {}) or {}
    sftp_config = uploader.get('sftp', {}) or {}

    host = sftp_config.get('host', 'pswsnetwork.eng.ua.edu')
    user = sftp_config.get('user', station.get('id', ''))
    ssh_key = sftp_config.get('ssh_key', '')
    if ssh_key:
        ssh_key = os.path.expanduser(ssh_key)

    print(f"PSWS Upload Preflight Check")
    print(f"  Host:    {host}")
    print(f"  User:    {user}")
    print(f"  SSH key: {ssh_key}")
    print()

    all_ok = True

    # --- Check 1: TCP connectivity ---
    print(f"[1/3] TCP connectivity to {host}:22 ...", end=" ", flush=True)
    try:
        import socket
        t0 = time.time()
        sock = socket.create_connection((host, 22), timeout=5)
        sock.close()
        elapsed = time.time() - t0
        print(f"OK ({elapsed:.1f}s)")
    except Exception as e:
        print(f"FAILED ({e})")
        print(f"      Cannot reach {host}:22 — check network/firewall")
        return False

    # --- Check 2: SSH key file ---
    print(f"[2/3] SSH key at {ssh_key} ...", end=" ", flush=True)
    if not ssh_key:
        print("FAILED (no ssh_key configured in [uploader.sftp])")
        all_ok = False
    else:
        key_path = Path(ssh_key)
        try:
            if not key_path.exists():
                print(f"FAILED (file not found)")
                print(f"      Expected: {ssh_key}")
                all_ok = False
            else:
                mode = key_path.stat().st_mode & 0o777
                if mode & 0o077:
                    print(f"WARNING (permissions {oct(mode)} too open, should be 0600)")
                    print(f"      Run: chmod 600 {ssh_key}")
                    # Not fatal — SSH may still work
                else:
                    print("OK")

                # Check for corresponding .pub
                pub_path = Path(str(key_path) + '.pub')
                try:
                    if pub_path.exists():
                        with open(pub_path, 'r') as f:
                            pub_contents = f.read().strip()
                        print(f"      Public key: {pub_contents[:80]}...")
                    else:
                        print(f"      (no .pub file at {pub_path})")
                except PermissionError:
                    print(f"      (cannot read .pub — run as timestd user)")
        except PermissionError:
            print(f"OK (file exists, owned by another user)")
            print(f"      Cannot check permissions — run as timestd user for full check")
            print(f"      Hint: sudo -u timestd /opt/hf-timestd/venv/bin/hf-timestd grape test-upload")

    if not all_ok:
        return False

    if not user:
        print(f"\n[3/3] SFTP autologin ... SKIPPED (no user/station ID configured)")
        print(f"      Set [station].id or [uploader.sftp].user in config")
        return False

    # --- Check 3: SFTP autologin ---
    print(f"[3/3] SFTP autologin as {user}@{host} ...", end=" ", flush=True)
    try:
        t0 = time.time()
        result = subprocess.run(
            [
                "sftp", "-q",
                "-i", ssh_key,
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "UserKnownHostsFile=/dev/null",
                "-b", "/dev/null",
                f"{user}@{host}"
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        elapsed = time.time() - t0
        if result.returncode == 0:
            print(f"OK ({elapsed:.1f}s)")
        else:
            print(f"FAILED (exit {result.returncode}, {elapsed:.1f}s)")
            if result.stderr.strip():
                for line in result.stderr.strip().splitlines():
                    print(f"      {line}")
            print(f"      Verify that {user}'s public key is registered with PSWS server")
            return False
    except subprocess.TimeoutExpired:
        print("FAILED (timeout after 30s)")
        return False
    except FileNotFoundError:
        print("FAILED (sftp command not found — install openssh-client)")
        return False

    print(f"\nAll checks passed — PSWS upload should work.")
    return True

# NOTE (2026-06-30): the upload-execution classes (UploadTask, UploadProtocol,
# SSHRsyncUpload, SFTPUpload, UploadManager) and the queue.json machinery were
# removed when GRAPE uploads were folded onto hs_uploader.transports.
# psws_magnetometer.PswsDatasetSftp (see grape/hs_upload.py).  Only the
# config loader and the connectivity preflight remain.
