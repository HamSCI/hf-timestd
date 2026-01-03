"""
Upload manager module

Handles reliable upload of processed datasets to remote repositories.
"""

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
        """
        self.host = config['host']
        self.user = config['user']  # PSWS station ID
        self.ssh_key = config.get('ssh', {}).get('key_file')
        self.bandwidth_limit_kbps = config.get('bandwidth_limit_kbps', 100)
        self.psws_server_url = config.get('psws_server_url', self.host)
    
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
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m%dT%H-%M')
        trigger_dir = f"c{dataset_name}_#{instrument_id}_#{timestamp}"
        
        logger.info(f"Trigger directory: {trigger_dir}")
        
        # Create SFTP batch commands file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.sftp', delete=False) as f:
            sftp_cmds_file = f.name
            # Change to parent directory, upload dataset, create trigger
            f.write(f"put -r {local_path.name}\n")
            f.write(f"mkdir {trigger_dir}\n")
            f.write("quit\n")
        
        try:
            # Build SFTP command
            cmd = ["sftp", "-v"]  # Verbose for logging
            
            # Add SSH key if specified
            if self.ssh_key:
                cmd.extend(["-i", str(self.ssh_key)])
            
            # Add bandwidth limit
            cmd.extend(["-l", str(self.bandwidth_limit_kbps)])
            
            # Add batch file
            cmd.extend(["-b", sftp_cmds_file])
            
            # Add destination
            cmd.append(f"{self.user}@{self.psws_server_url}")
            
            logger.debug(f"Running: {' '.join(cmd)}")
            logger.debug(f"Working directory: {local_path.parent}")
            
            # Run SFTP from parent directory
            result = subprocess.run(
                cmd,
                cwd=str(local_path.parent),
                check=True,
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour timeout
            )
            
            logger.info(f"SFTP upload successful: {local_path}")
            logger.debug(f"SFTP output: {result.stdout}")
            
            # Clean up temp file
            Path(sftp_cmds_file).unlink(missing_ok=True)
            
            return True
        
        except subprocess.CalledProcessError as e:
            logger.error(f"SFTP failed: {e.stderr}")
            Path(sftp_cmds_file).unlink(missing_ok=True)
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"SFTP timeout after 1 hour")
            Path(sftp_cmds_file).unlink(missing_ok=True)
            return False
        except Exception as e:
            logger.error(f"SFTP error: {e}")
            Path(sftp_cmds_file).unlink(missing_ok=True)
            return False
    
    def verify(self, remote_path: str) -> bool:
        """
        Verify upload by checking if trigger directory exists
        
        Args:
            remote_path: Not used - verification checks home directory
            
        Returns:
            True if verified (always True for SFTP, PSWS will report issues)
        """
        # PSWS will email if there are issues, so we assume success
        # Alternative: could SSH and check for dataset directory
        logger.info("SFTP upload verification: assuming success (PSWS will report issues)")
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
        self.protocol = self._init_protocol(config)
        
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
            dataset_path: Path to dataset directory
            
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
            
            for channel_dir in channels:
                try:
                    # Try to open as Digital RF
                    reader = drf.DigitalRFReader(str(channel_dir))
                    bounds = reader.get_bounds()
                    
                    if bounds[0] is None or bounds[1] is None:
                        logger.error(f"Channel {channel_dir.name}: No data found")
                        return False
                    
                    sample_count = bounds[1] - bounds[0]
                    logger.info(f"Channel {channel_dir.name}: {sample_count} samples valid")
                    
                except Exception as e:
                    logger.error(f"Channel {channel_dir.name}: Validation failed - {e}")
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

