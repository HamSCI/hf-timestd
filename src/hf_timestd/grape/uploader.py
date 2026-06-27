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


# ---------------------------------------------------------------------------
# Verification helpers (used by SFTPUpload.verify; module-level for testing).
# ---------------------------------------------------------------------------

def _build_remote_manifest(local_path: Path,
                           dataset_name: str) -> List[Tuple[str, int]]:
    """Walk a freshly-uploaded local dataset and return the list of
    ``(remote_relative_path, byte_size)`` pairs that the SFTP server
    should now hold under the operator's home directory.

    The walk is fully deterministic — directories are visited in
    sorted order, files are sorted within each directory.  Order
    matters because ``verify()`` zips the returned list against the
    SFTP ``ls -l`` reply order; if either side reordered, a swap of
    two same-size files between paths would slip through.
    """
    manifest: List[Tuple[str, int]] = []
    for root, dirs, files in os.walk(local_path):
        # Sort dirs in-place so os.walk descends in the same order
        # every run, on every filesystem.
        dirs.sort()
        rel = os.path.relpath(root, local_path)
        prefix = dataset_name if rel == '.' else f"{dataset_name}/{rel}"
        for fname in sorted(files):
            local_file = os.path.join(root, fname)
            try:
                size = os.path.getsize(local_file)
            except OSError:
                continue
            manifest.append((f"{prefix}/{fname}", size))
    return manifest


# Matches a single sftp "ls -l" line.  sftp emits a Unix-like long
# listing; the size column is the 5th whitespace-separated field,
# preceded by the mode, link count, owner, and group.  We don't try
# to parse the trailing date or filename — only the size.
#
# The nlink/owner/group fields are matched as \S+ rather than \d+: the
# PSWS server (pswsnetwork.eng.ua.edu) returns "?" for the link-count
# column (it doesn't expose nlink over SFTP), e.g.
#   -rwxr-xr-x    ? 9032 stations 1283 Jun 27 06:16 gap_summary.json
# A strict \d+ for nlink matched zero lines there, so verify() always
# saw "0 ls replies" and reported false "upload truncation" even though
# every file had landed.  Only the size column is required to be numeric.
_SFTP_LS_LINE_RE = re.compile(
    r"^[\-dlpcsb][rwxXstST\-]{9}\S*\s+\S+\s+\S+\s+\S+\s+(\d+)\s+"
)


def _parse_sftp_ls_sizes(stdout: str) -> List[int]:
    """Extract byte sizes from sftp ``ls -l`` lines, preserving order.

    Lines that look like a long-listing entry contribute one size;
    every other line (the trigger-dir ``ls -d``, blank lines, banners)
    is ignored, so the trigger-dir line falls out of the size list
    naturally and is checked separately by the caller.
    """
    sizes: List[int] = []
    for line in (stdout or "").splitlines():
        m = _SFTP_LS_LINE_RE.match(line)
        if m:
            try:
                sizes.append(int(m.group(1)))
            except ValueError:
                continue
    return sizes


@dataclass
class UploadTask:
    """Represents an upload task in the queue"""
    dataset_path: str
    remote_path: str
    metadata: Dict
    status: str = "pending"  # pending, uploading, completed, failed
    attempts: int = 0
    last_attempt: Optional[str] = None
    created_at: str = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc).isoformat()
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'UploadTask':
        """Create from dictionary"""
        return cls(**data)


class UploadProtocol(ABC):
    """Base class for upload protocols"""
    
    @abstractmethod
    def upload(self, local_path: Path, remote_path: str, metadata: Dict) -> bool:
        """
        Upload dataset
        
        Args:
            local_path: Local path to dataset
            remote_path: Remote path (protocol-specific)
            metadata: Additional metadata
            
        Returns:
            True if upload successful
        """
        pass
    
    @abstractmethod
    def verify(self, remote_path: str) -> bool:
        """
        Verify upload completed successfully
        
        Args:
            remote_path: Remote path to verify
            
        Returns:
            True if verified
        """
        pass


class SSHRsyncUpload(UploadProtocol):
    """Upload via SSH/rsync (for HamSCI PSWS)"""
    
    def __init__(self, config: Dict):
        """
        Initialize SSH/rsync uploader
        
        Args:
            config: Upload configuration
        """
        self.host = config['host']
        self.user = config['user']
        self.base_path = config.get('base_path', '/data/uploads')
        self.ssh_key = config.get('ssh', {}).get('key_file')
        self.bandwidth_limit = config.get('bandwidth_limit')  # KB/s
        self.timeout = config.get('timeout', 3600)  # seconds
    
    def upload(self, local_path: Path, remote_path: str, metadata: Dict) -> bool:
        """
        Upload using rsync over SSH
        
        Args:
            local_path: Local path to upload
            remote_path: Remote path relative to base_path
            metadata: Additional metadata
            
        Returns:
            True if successful
        """
        full_remote_path = f"{self.user}@{self.host}:{self.base_path}/{remote_path}"
        
        logger.info(f"Uploading {local_path} to {full_remote_path}")
        
        # Build rsync command
        cmd = ["rsync", "-avz", "--progress"]
        
        # Add SSH key if specified
        if self.ssh_key:
            cmd.extend(["-e", f"ssh -i {self.ssh_key}"])
        
        # Add bandwidth limit if specified
        if self.bandwidth_limit:
            cmd.extend(["--bwlimit", str(self.bandwidth_limit)])
        
        # Add timeout
        cmd.extend(["--timeout", str(self.timeout)])
        
        # Add source and destination
        # If directory, add trailing slash
        source = str(local_path)
        if local_path.is_dir():
            source += "/"
        
        cmd.extend([source, full_remote_path])
        
        logger.debug(f"Running: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout
            )
            
            logger.info(f"Upload successful: {local_path}")
            logger.debug(f"rsync output: {result.stdout}")
            return True
        
        except subprocess.CalledProcessError as e:
            logger.error(f"rsync failed: {e.stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"rsync timeout after {self.timeout} seconds")
            return False
        except Exception as e:
            logger.error(f"Upload error: {e}")
            return False
    
    def verify(self, remote_path: str) -> bool:
        """
        Verify upload by checking if remote path exists
        
        Args:
            remote_path: Remote path to verify
            
        Returns:
            True if exists
        """
        full_remote_path = f"{self.base_path}/{remote_path}"
        
        # Build SSH command to test if path exists
        cmd = ["ssh"]
        
        if self.ssh_key:
            cmd.extend(["-i", self.ssh_key])
        
        cmd.extend([
            f"{self.user}@{self.host}",
            "test", "-e", full_remote_path
        ])
        
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Verification error: {e}")
            return False


class SFTPUpload(UploadProtocol):
    """
    Upload via SFTP (wsprdaemon-compatible for HamSCI PSWS)
    
    Uses SFTP with bandwidth limiting and creates trigger directories
    for PSWS processing, matching wsprdaemon's upload behavior.
    """
    
    def __init__(self, config: Dict):
        """
        Initialize SFTP uploader

        Args:
            config: Upload configuration with keys:
                - host: PSWS server hostname
                - user: PSWS station ID (e.g., 'S000171')
                - ssh.key_file: Path to SSH private key
                - bandwidth_limit_kbps: Upload bandwidth limit (default: 100)
                - psws_server_url: PSWS server URL (default from config['host'])
                - verify_timeout_sec: SFTP verify-call timeout (default: 600)
        """
        self.host = config['host']
        self.user = config['user']  # PSWS station ID
        self.ssh_key = config.get('ssh', {}).get('key_file')
        self.bandwidth_limit_kbps = config.get('bandwidth_limit_kbps', 100)
        self.psws_server_url = config.get('psws_server_url', self.host)
        self.verify_timeout_sec = int(config.get('verify_timeout_sec', 600))

        # State carried from upload() to verify() so that verify can ask
        # the remote server about the exact files we just sent.  Set by
        # upload() on success; verify() refuses to confirm without it.
        self._last_upload: Optional[Dict] = None
    
    def upload(self, local_path: Path, remote_path: str, metadata: Dict) -> bool:
        """
        Upload using SFTP (wsprdaemon-compatible)
        
        Process:
        1. cd to parent of dataset
        2. SFTP: put -r {dataset}
        3. SFTP: mkdir {trigger_directory}
        
        Args:
            local_path: Local path to dataset (OBS directory)
            remote_path: Not used - SFTP uploads to home directory
            metadata: Metadata including instrument_id
            
        Returns:
            True if successful
        """
        logger.info(f"Uploading {local_path} via SFTP to {self.user}@{self.psws_server_url}")
        
        # Extract dataset name and instrument ID
        dataset_name = local_path.name  # e.g., OBS2025-10-27T00-00
        instrument_id = metadata.get('instrument_id', '172')
        
        # Create trigger directory name (wsprdaemon format)
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M')
        trigger_dir = f"c{dataset_name}_#{instrument_id}_#{timestamp}"
        
        logger.info(f"Trigger directory: {trigger_dir}")
        
        try:
            # Build sftp batch commands for recursive upload
            # PSWS server is sftp-only (no scp/ssh shell)
            batch_cmds = self._build_sftp_put_commands(local_path, dataset_name)
            batch_cmds.append(f"mkdir \"{trigger_dir}\"")
            batch_cmds.append("quit")
            batch_input = "\n".join(batch_cmds) + "\n"
            
            sftp_cmd = [
                "sftp", "-q",
                "-i", str(self.ssh_key),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                f"{self.user}@{self.psws_server_url}"
            ]
            
            logger.info(f"Running sftp upload to {self.user}@{self.psws_server_url} "
                        f"({len(batch_cmds)} commands)")
            
            result = subprocess.run(
                sftp_cmd,
                input=batch_input,
                capture_output=True,
                text=True,
                timeout=3600
            )
            
            if result.returncode != 0:
                logger.error(f"sftp failed (exit {result.returncode}): {result.stderr}")
                return False

            # Stash the context verify() needs to confirm the remote state.
            self._last_upload = {
                'local_path':   local_path,
                'dataset_name': dataset_name,
                'trigger_dir':  trigger_dir,
            }
            logger.info(f"Upload successful: {local_path}")
            return True
        
        except subprocess.TimeoutExpired:
            logger.error(f"Upload timeout after 1 hour")
            return False
        except Exception as e:
            logger.error(f"Upload error: {e}")
            return False
    
    def _build_sftp_put_commands(self, local_path: Path, remote_name: str) -> List[str]:
        """
        Build sftp batch commands to recursively upload a directory.
        
        Args:
            local_path: Local directory to upload
            remote_name: Remote directory name (top-level)
            
        Returns:
            List of sftp commands (mkdir, put, cd, lcd)
        """
        cmds = []
        cmds.append(f"mkdir {remote_name}")
        
        for root, dirs, files in os.walk(local_path):
            rel = os.path.relpath(root, local_path)
            if rel == '.':
                remote_dir = remote_name
            else:
                remote_dir = f"{remote_name}/{rel}"
            
            # Create subdirectories
            for d in sorted(dirs):
                cmds.append(f"mkdir {remote_dir}/{d}")
            
            # Upload files
            for f in sorted(files):
                local_file = os.path.join(root, f)
                cmds.append(f"put {local_file} {remote_dir}/{f}")
        
        return cmds
    
    def _upload_sftp_fallback(self, local_path: Path, trigger_dir: str, sftp_cmds_file: str) -> bool:
        """Fallback SFTP upload using rsync over SSH"""
        logger.info(f"Using rsync fallback for {local_path}")
        
        # Use rsync which is more reliable than sftp batch mode
        ssh_cmd = f"ssh -i {self.ssh_key} -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        
        cmd = [
            "rsync", "-avz", "--progress",
            "-e", ssh_cmd,
            str(local_path),
            f"{self.user}@{self.psws_server_url}:"
        ]
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600
            )
            
            if result.returncode != 0:
                logger.error(f"rsync failed: {result.stderr}")
                return False
            
            # Create trigger directory via ssh
            trigger_cmd = [
                "ssh", "-i", str(self.ssh_key),
                "-o", "BatchMode=yes",
                f"{self.user}@{self.psws_server_url}",
                f"mkdir -p {trigger_dir}"
            ]
            
            subprocess.run(trigger_cmd, capture_output=True, timeout=30)
            
            logger.info(f"rsync upload successful: {local_path}")
            return True
            
        except Exception as e:
            logger.error(f"rsync fallback failed: {e}")
            return False
    
    def verify(self, remote_path: str) -> bool:
        """Verify the upload actually arrived intact.

        Walks the local dataset, asks the PSWS SFTP server for an
        ``ls -l`` of every uploaded leaf file (in submission order),
        and confirms each remote entry's byte size matches local.
        Also checks that the trigger directory was created.

        Returns True only when every file matches; any missing file,
        size mismatch, or sftp error returns False so the
        ``UploadManager`` re-queues the task instead of allowing the
        post-upload cleanup branch in ``cli.py grape daily`` to delete
        local data that may not have actually arrived.

        Args:
            remote_path: kept for the ``UploadProtocol`` interface;
                actual paths are taken from the context stashed by
                ``upload()`` so verify always asks about exactly what
                upload() just sent.
        """
        if not self._last_upload:
            logger.error("verify: no upload context — refusing to confirm")
            return False

        local_path: Path = self._last_upload['local_path']
        dataset_name: str = self._last_upload['dataset_name']
        trigger_dir: str = self._last_upload['trigger_dir']

        manifest = _build_remote_manifest(local_path, dataset_name)
        if not manifest:
            logger.error(f"verify: local dataset {local_path} has no files")
            return False

        # Build a single sftp batch: one `ls -l` per leaf file plus the
        # trigger dir.  sftp doesn't support recursive ls, so we walk
        # the manifest explicitly.  Order is preserved in the response,
        # which is what _parse_sftp_ls_sizes relies on.
        batch_cmds: List[str] = []
        for remote_rel, _ in manifest:
            batch_cmds.append(f'ls -l "{remote_rel}"')
        batch_cmds.append(f'ls -d "{trigger_dir}"')
        batch_cmds.append("quit")
        batch_input = "\n".join(batch_cmds) + "\n"

        sftp_cmd = [
            "sftp", "-q", "-b", "-",
            "-i", str(self.ssh_key),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            f"{self.user}@{self.psws_server_url}",
        ]

        try:
            result = subprocess.run(
                sftp_cmd, input=batch_input,
                capture_output=True, text=True,
                timeout=self.verify_timeout_sec,
            )
        except subprocess.TimeoutExpired:
            logger.error(
                f"verify: sftp ls timed out after "
                f"{self.verify_timeout_sec}s ({len(manifest)} files)"
            )
            return False
        except OSError as exc:
            logger.error(f"verify: failed to spawn sftp: {exc}")
            return False

        # Any "not found" / "Can't ls" in stderr is a missing file.
        # sftp prints to stderr per failed command but doesn't
        # necessarily set a non-zero exit code, so we have to look.
        stderr = (result.stderr or "")
        for marker in ("Can't ls", "No such file", "Couldn't stat",
                       "not found"):
            if marker in stderr:
                logger.error(
                    f"verify: remote ls reported missing files; "
                    f"sftp stderr:\n{stderr.strip()}"
                )
                return False

        remote_sizes = _parse_sftp_ls_sizes(result.stdout)
        if len(remote_sizes) < len(manifest):
            logger.error(
                f"verify: expected {len(manifest)} ls -l replies, "
                f"got {len(remote_sizes)} — possible upload truncation"
            )
            return False

        for (remote_rel, expected_size), actual_size in zip(
                manifest, remote_sizes[:len(manifest)]):
            if actual_size != expected_size:
                logger.error(
                    f"verify: size mismatch on {remote_rel}: "
                    f"local={expected_size} remote={actual_size}"
                )
                return False

        # Final ls was the trigger directory; verify it appeared.
        if trigger_dir not in result.stdout:
            logger.error(
                f"verify: trigger directory {trigger_dir} not present "
                f"in remote ls output"
            )
            return False

        logger.info(
            f"verify OK: {len(manifest)} files match by size, "
            f"trigger dir present"
        )
        return True


class UploadManager:
    """Manages upload queue and retry logic"""
    
    def __init__(
        self, 
        config: Dict, 
        storage_manager: Optional[Any] = None,
        on_success_callback: Optional[callable] = None
    ):
        """
        Initialize upload manager
        
        Args:
            config: Uploader configuration
            storage_manager: Optional storage manager for status updates
            on_success_callback: Optional callback(task) to run after successful upload
        """
        self.config = config
        self.storage_manager = storage_manager
        self.on_success_callback = on_success_callback
        
        # Retry settings
        self.max_retries = config.get('max_retries', 3)
        self.retry_backoff_base = config.get('retry_backoff_base', 2)
        self.queue_file = Path(config.get('queue_file', '/var/lib/signal-recorder/upload_queue.json'))
        self.queue: List[UploadTask] = []
        
        # Init protocol
        self.protocol = self._create_protocol()
        
        # Load queue from disk
        self._load_queue()
    
    def _create_protocol(self) -> UploadProtocol:
        """Create upload protocol instance"""
        protocol_type = self.config.get('protocol', 'sftp')  # Default to SFTP for PSWS
        
        if protocol_type == 'ssh_rsync':
            return SSHRsyncUpload(self.config)
        elif protocol_type == 'sftp':
            return SFTPUpload(self.config)
        else:
            raise ValueError(f"Unknown upload protocol: {protocol_type}")
    
    def _should_upload_date(self, date: datetime.date) -> bool:
        """
        Check if date is ready for upload (wsprdaemon-compatible).
        Only upload data from completed days (yesterday or earlier).
        
        Args:
            date: Date to check
            
        Returns:
            True if date is ready for upload
        """
        today_utc = datetime.now(timezone.utc).date()
        return date < today_utc
    
    def _is_already_uploaded(self, dataset_path: Path) -> bool:
        """
        Check if dataset has already been uploaded successfully.
        Looks for .upload_complete marker file (wsprdaemon-compatible).
        
        Args:
            dataset_path: Path to dataset directory
            
        Returns:
            True if already uploaded
        """
        marker_file = dataset_path.parent / ".upload_complete"
        return marker_file.exists()
    
    def _mark_upload_complete(self, dataset_path: Path):
        """
        Mark dataset as successfully uploaded (wsprdaemon-compatible).
        Creates .upload_complete marker file in parent directory.
        
        Args:
            dataset_path: Path to dataset directory
        """
        marker_file = dataset_path.parent / ".upload_complete"
        try:
            marker_file.touch()
            logger.info(f"Created upload completion marker: {marker_file}")
        except Exception as e:
            logger.error(f"Failed to create completion marker: {e}")
    
    def _validate_digital_rf(self, dataset_path: Path) -> bool:
        """
        Validate Digital RF dataset before upload.
        Checks if dataset is readable and has valid structure.
        
        Args:
            dataset_path: Path to OBS dataset directory (contains ch0, ch1, etc.)
            
        Returns:
            True if valid
        """
        if not HAS_DIGITAL_RF:
            logger.warning("digital_rf not available, skipping validation")
            return True
        
        try:
            # Find channel directories
            channels = [d for d in dataset_path.iterdir() 
                       if d.is_dir() and not d.name.startswith('.')]
            
            if not channels:
                logger.error(f"No channels found in {dataset_path}")
                return False
            
            logger.info(f"Validating {len(channels)} channels in {dataset_path}")
            
            # DigitalRFReader expects the parent directory containing channel dirs
            # So we pass dataset_path (OBS dir), not individual channel dirs
            try:
                reader = drf.DigitalRFReader(str(dataset_path))
                channel_names = reader.get_channels()
                
                if not channel_names:
                    logger.error(f"No readable channels in {dataset_path}")
                    return False
                
                for ch_name in channel_names:
                    bounds = reader.get_bounds(ch_name)
                    if bounds[0] is None or bounds[1] is None:
                        logger.error(f"Channel {ch_name}: No data found")
                        return False
                    
                    sample_count = bounds[1] - bounds[0]
                    logger.info(f"Channel {ch_name}: {sample_count} samples valid")
                
            except Exception as e:
                logger.error(f"Digital RF validation failed: {e}")
                return False
            
            logger.info(f"✅ Digital RF validation passed for {dataset_path}")
            return True
            
        except Exception as e:
            logger.error(f"Digital RF validation error: {e}")
            return False
    
    def _load_queue(self):
        """Load upload queue from disk"""
        if self.queue_file.exists():
            try:
                with open(self.queue_file, 'r') as f:
                    data = json.load(f)
                self.queue = [UploadTask.from_dict(item) for item in data]
                logger.info(f"Loaded {len(self.queue)} tasks from queue")
            except Exception as e:
                logger.error(f"Error loading queue: {e}")
                self.queue = []
        else:
            self.queue = []
    
    def _save_queue(self):
        """Save upload queue to disk"""
        try:
            self.queue_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.queue_file, 'w') as f:
                data = [task.to_dict() for task in self.queue]
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving queue: {e}")
    
    def enqueue(self, dataset_path: Path, metadata: Dict):
        """
        Add dataset to upload queue (wsprdaemon-compatible)
        
        Performs validation before enqueuing:
        1. Check date is from previous day or earlier
        2. Check if already uploaded (.upload_complete marker)
        3. Validate Digital RF structure
        
        Args:
            dataset_path: Path to dataset (OBS directory)
            metadata: Metadata dict with keys:
                - date: Date string (YYYY-MM-DD) or datetime.date
                - callsign: Station callsign
                - grid_square: Maidenhead grid
                - station_id: PSWS station ID
                - instrument_id: PSWS instrument ID
        """
        # Parse date
        if isinstance(metadata.get('date'), str):
            try:
                date = datetime.strptime(metadata['date'], '%Y-%m-%d').date()
            except ValueError:
                logger.error(f"Invalid date format: {metadata['date']}")
                return
        else:
            date = metadata.get('date')
        
        if not date:
            logger.error("No date in metadata")
            return
        
        # Check 1: Only upload previous days (wsprdaemon behavior)
        if not self._should_upload_date(date):
            today_utc = datetime.now(timezone.utc).date()
            logger.info(f"Skipping {dataset_path}: date {date} is not before today ({today_utc})")
            return
        
        # Check 2: Already uploaded?
        if self._is_already_uploaded(dataset_path):
            logger.info(f"Skipping {dataset_path}: already uploaded (.upload_complete marker exists)")
            return
        
        # Check 3: Validate Digital RF
        if not self._validate_digital_rf(dataset_path):
            logger.error(f"Skipping {dataset_path}: Digital RF validation failed")
            return
        
        # Construct remote path (SFTP uploads to home dir, so just use dataset name)
        remote_path = dataset_path.name  # e.g., OBS2025-10-27T00-00
        
        # Create task
        task = UploadTask(
            dataset_path=str(dataset_path),
            remote_path=remote_path,
            metadata=metadata
        )
        
        # Check if already in queue
        for existing in self.queue:
            if existing.dataset_path == task.dataset_path:
                logger.warning(f"Dataset already in queue: {dataset_path}")
                return
        
        self.queue.append(task)
        self._save_queue()
        
        logger.info(f"✅ Enqueued upload: {dataset_path}")
        logger.info(f"   Date: {date}")
        logger.info(f"   Remote: {remote_path}")
    
    def process_queue(self):
        """Process upload queue with retry logic"""
        if not self.queue:
            logger.debug("Upload queue is empty")
            return
        
        logger.info(f"Processing upload queue ({len(self.queue)} tasks)")
        
        for task in self.queue[:]:  # Iterate over copy
            if task.status == "completed":
                continue
            
            # Check if we should retry
            if task.attempts >= self.max_retries:
                logger.error(f"Max retries exceeded for {task.dataset_path}")
                task.status = "failed"
                self._save_queue()
                continue
            
            # Exponential backoff
            if task.last_attempt:
                last_attempt_time = datetime.fromisoformat(task.last_attempt)
                wait_time = self.retry_backoff_base ** task.attempts * 60  # minutes
                elapsed = (datetime.now(timezone.utc) - last_attempt_time).total_seconds()
                
                if elapsed < wait_time:
                    logger.debug(f"Waiting {wait_time - elapsed:.0f}s before retry for {task.dataset_path}")
                    continue
            
            # Attempt upload
            self._attempt_upload(task)
            self._save_queue()
    
    def _attempt_upload(self, task: UploadTask):
        """
        Attempt to upload a task
        
        Args:
            task: UploadTask to upload
        """
        task.status = "uploading"
        task.attempts += 1
        task.last_attempt = datetime.now(timezone.utc).isoformat()
        
        logger.info(f"Upload attempt {task.attempts}/{self.max_retries}: {task.dataset_path}")
        
        try:
            dataset_path = Path(task.dataset_path)
            
            if not dataset_path.exists():
                logger.error(f"Dataset not found: {dataset_path}")
                task.status = "failed"
                task.error_message = "Dataset not found"
                return
            
            # Perform upload
            success = self.protocol.upload(dataset_path, task.remote_path, task.metadata)
            
            if success:
                # Verify upload
                if self.protocol.verify(task.remote_path):
                    logger.info(f"✅ Upload verified: {task.dataset_path}")
                    task.status = "completed"
                    task.completed_at = datetime.now(timezone.utc).isoformat()
                    
                    # Create .upload_complete marker (wsprdaemon-compatible)
                    self._mark_upload_complete(dataset_path)
                    
                    # Mark in storage manager (if available)
                    if hasattr(self.storage_manager, 'mark_upload_complete'):
                        if 'date' in task.metadata and 'band' in task.metadata:
                            self.storage_manager.mark_upload_complete(
                                task.metadata['date'],
                                task.metadata['band']
                            )
                    
                    # Run success callback (e.g., for cleanup)
                    if self.on_success_callback:
                        try:
                            logger.info(f"Running success callback for {task.dataset_path}")
                            self.on_success_callback(task)
                        except Exception as cb_err:
                            logger.error(f"Success callback failed: {cb_err}", exc_info=True)
                            # Note: We still consider the upload "completed" even if cleanup failed
                            
                else:
                    logger.warning(f"Upload verification failed: {task.dataset_path}")
                    task.status = "pending"
                    task.error_message = "Verification failed"
            else:
                logger.error(f"Upload failed: {task.dataset_path}")
                task.status = "pending"
                task.error_message = "Upload failed"
            
            # Mark upload attempt in storage manager (if available)
            if hasattr(self.storage_manager, 'mark_upload_attempted'):
                if 'date' in task.metadata and 'band' in task.metadata:
                    self.storage_manager.mark_upload_attempted(
                        task.metadata['date'],
                        task.metadata['band']
                    )
        
        except Exception as e:
            logger.error(f"Upload error: {e}", exc_info=True)
            task.status = "pending"
            task.error_message = str(e)
    
    def get_status(self) -> Dict:
        """Get upload queue status"""
        status = {
            'total': len(self.queue),
            'pending': 0,
            'uploading': 0,
            'completed': 0,
            'failed': 0
        }
        
        for task in self.queue:
            status[task.status] += 1
        
        return status
    
    def clear_completed(self):
        """Remove completed tasks from queue"""
        original_count = len(self.queue)
        self.queue = [task for task in self.queue if task.status != "completed"]
        removed = original_count - len(self.queue)
        
        if removed > 0:
            logger.info(f"Removed {removed} completed tasks from queue")
            self._save_queue()
    
    def write_upload_report(self, report_dir: Optional[Path] = None) -> Path:
        """
        Write a daily upload report summarizing upload status.
        
        Args:
            report_dir: Directory for reports (default: queue_file parent / reports)
            
        Returns:
            Path to the report file
        """
        if report_dir is None:
            report_dir = self.queue_file.parent / 'reports'
        
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate report
        now = datetime.now(timezone.utc)
        status = self.get_status()
        
        report = {
            'generated_at': now.isoformat(),
            'summary': status,
            'tasks': []
        }
        
        for task in self.queue:
            report['tasks'].append({
                'dataset': task.dataset_path,
                'status': task.status,
                'attempts': task.attempts,
                'created': task.created_at,
                'completed': task.completed_at,
                'error': task.error_message
            })
        
        # Write daily report
        report_file = report_dir / f"upload_report_{now.strftime('%Y%m%d')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Upload report written: {report_file}")
        
        # Also append to running log
        log_file = report_dir / 'upload_history.log'
        with open(log_file, 'a') as f:
            f.write(f"{now.isoformat()} | "
                   f"completed={status['completed']} "
                   f"pending={status['pending']} "
                   f"failed={status['failed']}\n")
        
        return report_file
    
    def get_upload_history(self, days: int = 7) -> List[Dict]:
        """
        Get upload history for the last N days.
        
        Args:
            days: Number of days to look back
            
        Returns:
            List of daily summaries
        """
        report_dir = self.queue_file.parent / 'reports'
        history = []
        
        for i in range(days):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            report_file = report_dir / f"upload_report_{date.strftime('%Y%m%d')}.json"
            
            if report_file.exists():
                try:
                    with open(report_file, 'r') as f:
                        report = json.load(f)
                    history.append({
                        'date': date.strftime('%Y-%m-%d'),
                        'summary': report.get('summary', {}),
                        'task_count': len(report.get('tasks', []))
                    })
                except Exception as e:
                    logger.warning(f"Failed to read report {report_file}: {e}")
        
        return history

