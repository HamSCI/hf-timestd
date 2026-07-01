"""
Upload Queue Interface (Function 6)

Defines the contract for uploading Digital RF files to remote repository.
Decouples file creation from network operations.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict, Callable
from .data_models import UploadTask, UploadStatus, FileMetadata


class UploadQueue(ABC):
    """
    Interface for Function 6: Repository Upload
    
    Manages upload of Digital RF files to remote repository.
    Handles queuing, retry logic, bandwidth limiting, and status tracking.
    
    Design principle:
        Producer (Function 5) doesn't care about SSH, rsync, bandwidth limits,
        or retry logic. Just queues files and monitors status.
        
    Key features:
    - Persistent queue (survives restarts)
    - Automatic retry on failure
    - Bandwidth limiting
    - Progress tracking
    - Priority support
    """
    
    @abstractmethod
    def queue_file(
        self,
        local_path: Path,
        metadata: FileMetadata,
        priority: int = 0
    ) -> str:
        """
        Queue a file for upload.
        
        File is added to persistent queue and will be uploaded
        when network is available. Returns immediately.
        
        Args:
            local_path: Full path to Digital RF file/directory
            metadata: File metadata (channel, time range, quality)
            priority: Upload priority (higher = uploaded first)
                     Default 0 = normal priority
                     
        Returns:
            task_id: Unique identifier for tracking this upload
            
        Thread safety:
            Thread-safe. Multiple producers can queue files concurrently.
            
        Example:
            # Function 5 queues completed file
            completed_file = writer.write_decimated(...)
            if completed_file:
                task_id = upload_queue.queue_file(
                    local_path=completed_file,
                    metadata=FileMetadata(
                        channel_name="WWV 5.0 MHz",
                        frequency_hz=5e6,
                        start_time=1699876800.0,
                        end_time=1699880400.0,
                        sample_rate=10,
                        sample_count=36000,
                        file_format='digital_rf',
                        quality_summary={'completeness': 99.8},
                        time_snap_used=time_snap_dict
                    ),
                    priority=0
                )
                logger.info(f"Queued for upload: {task_id}")
        """
        pass
    
    @abstractmethod
    def get_task_status(self, task_id: str) -> Optional[UploadTask]:
        """
        Get current status of upload task.
        
        Args:
            task_id: Task identifier from queue_file()
            
        Returns:
            UploadTask with current status, progress, attempts, etc.
            None if task_id not found
            
        Example:
            task = upload_queue.get_task_status(task_id)
            if task:
                print(f"Status: {task.status.value}")
                print(f"Progress: {task.progress_pct():.1f}%")
                if task.status == UploadStatus.FAILED:
                    print(f"Error: {task.last_error}")
        """
        pass
    
    @abstractmethod
    def get_pending_count(self) -> int:
        """
        Get number of files waiting to upload.
        
        Returns:
            Count of tasks with status PENDING or UPLOADING
            
        Usage:
            Monitoring, alerting:
                pending = upload_queue.get_pending_count()
                if pending > 100:
                    alert("Upload queue backlog: {pending} files")
        """
        pass
    
    @abstractmethod
    def get_queue_status(self) -> Dict[str, int]:
        """
        Get queue statistics by status.
        
        Returns:
            dict with counts by status:
            - 'pending': int
            - 'uploading': int
            - 'completed': int
            - 'failed': int
            - 'cancelled': int
            
        Usage:
            Status dashboard:
                stats = upload_queue.get_queue_status()
                print(f"Pending: {stats['pending']}")
                print(f"Failed: {stats['failed']}")
        """
        pass
    
    @abstractmethod
    def cancel_task(self, task_id: str) -> bool:
        """
        Cancel a pending or in-progress upload.
        
        Args:
            task_id: Task to cancel
            
        Returns:
            True if cancelled, False if already completed or not found
            
        Usage:
            # Cancel upload of corrupt file
            if not verify_integrity(file):
                upload_queue.cancel_task(task_id)
        """
        pass
    
    @abstractmethod
    def retry_failed_tasks(self) -> int:
        """
        Retry all failed uploads.
        
        Resets failed tasks to pending status for retry.
        Useful after network outage resolved.
        
        Returns:
            Number of tasks reset for retry
            
        Usage:
            # After network restored
            retried = upload_queue.retry_failed_tasks()
            logger.info(f"Retrying {retried} failed uploads")
        """
        pass
    
    @abstractmethod
    def set_bandwidth_limit(self, kbps: int) -> None:
        """
        Set upload bandwidth limit.
        
        Args:
            kbps: Kilobytes per second (0 = unlimited)
            
        Usage:
            # Limit during business hours
            if is_business_hours():
                upload_queue.set_bandwidth_limit(100)  # 100 KB/s
            else:
                upload_queue.set_bandwidth_limit(0)    # Unlimited
        """
        pass
    
    @abstractmethod
    def pause_uploads(self) -> None:
        """
        Pause all uploads.
        
        Current upload will complete, but no new uploads will start.
        Queue remains intact.
        
        Usage:
            # Pause during critical data collection
            upload_queue.pause_uploads()
        """
        pass
    
    @abstractmethod
    def resume_uploads(self) -> None:
        """
        Resume paused uploads.
        
        Uploads will continue from where paused.
        
        Usage:
            # Resume after critical period
            upload_queue.resume_uploads()
        """
        pass
    
    @abstractmethod
    def register_progress_callback(
        self,
        callback: Callable[[str, float], None]
    ) -> None:
        """
        Register callback for upload progress updates.
        
        Args:
            callback: Function(task_id: str, progress_pct: float)
                     Called periodically during upload
                     
        Usage:
            def on_progress(task_id, progress):
                print(f"{task_id}: {progress:.1f}%")
                
            upload_queue.register_progress_callback(on_progress)
        """
        pass
    
    @abstractmethod
    def register_completion_callback(
        self,
        callback: Callable[[str, bool], None]
    ) -> None:
        """
        Register callback for upload completion.
        
        Args:
            callback: Function(task_id: str, success: bool)
                     Called when upload completes (success or failure)
                     
        Usage:
            def on_complete(task_id, success):
                if success:
                    # Delete local file to free space
                    task = upload_queue.get_task_status(task_id)
                    os.remove(task.local_path)
                else:
                    logger.error(f"Upload failed: {task_id}")
                    
            upload_queue.register_completion_callback(on_complete)
        """
        pass
    
    @abstractmethod
    def get_total_bytes_uploaded(self) -> int:
        """
        Get total bytes uploaded across all completed tasks.
        
        Returns:
            Total bytes successfully uploaded
            
        Usage:
            total_mb = upload_queue.get_total_bytes_uploaded() / 1e6
            print(f"Total uploaded: {total_mb:.1f} MB")
        """
        pass
    
    @abstractmethod
    def get_upload_rate(self) -> float:
        """
        Get current upload rate.
        
        Returns:
            Upload rate in bytes per second (averaged over recent uploads)
            
        Usage:
            rate_kbps = upload_queue.get_upload_rate() / 1024
            print(f"Upload rate: {rate_kbps:.1f} KB/s")
        """
        pass
    
    @abstractmethod
    def cleanup_completed_tasks(self, older_than_days: int = 7) -> int:
        """
        Remove old completed tasks from queue.
        
        Frees memory and disk space by removing successfully
        uploaded tasks older than specified age.
        
        Args:
            older_than_days: Remove tasks completed more than this many days ago
            
        Returns:
            Number of tasks removed
            
        Usage:
            # Weekly cleanup
            removed = upload_queue.cleanup_completed_tasks(older_than_days=7)
            logger.info(f"Cleaned up {removed} old tasks")
        """
        pass


class UploadProtocol(ABC):
    """
    Interface for upload protocol implementation.
    
    Separate interface to allow different upload mechanisms
    (rsync, sftp, S3, etc.) without changing UploadQueue interface.
    """
    
    @abstractmethod
    def upload_file(
        self,
        local_path: Path,
        remote_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None
    ) -> bool:
        """
        Upload a file to remote repository.
        
        Args:
            local_path: Local file/directory to upload
            remote_path: Remote destination path
            progress_callback: Optional callback(bytes_uploaded, total_bytes)
            
        Returns:
            True if upload successful, False otherwise
            
        Raises:
            ConnectionError: Network issue
            AuthenticationError: Authentication failed
            PermissionError: No write permission on remote
        """
        pass
    
    @abstractmethod
    def verify_upload(
        self,
        local_path: Path,
        remote_path: str
    ) -> bool:
        """
        Verify uploaded file matches local file.
        
        Args:
            local_path: Local file
            remote_path: Remote file
            
        Returns:
            True if remote file matches local (size, checksum, etc.)

        NOTE (2026-07-01): this interface's concrete implementers were removed
        when GRAPE uploads folded onto ``hs_uploader`` (see grape/hs_upload.py
        and the 2026-06-30 note in grape/uploader.py); the live path does NOT
        post-verify.  If you re-implement this, do NOT re-``ls`` the GRAPE
        trigger directory to confirm success — the PSWS server *consumes* that
        directory on ingest, so that check produced false "Verification failed"
        negatives.  Confirm via the OBS* dataset dirs or the watermark, which is
        what ``hf-timestd grape status`` now reads.
        """
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """
        Test connection to remote repository.
        
        Returns:
            True if can connect and authenticate
            
        Usage:
            if not protocol.test_connection():
                logger.error("Cannot connect to repository")
        """
        pass
    
    @abstractmethod
    def get_protocol_name(self) -> str:
        """
        Get protocol identifier.
        
        Returns:
            Protocol name ('rsync', 'sftp', 's3', etc.)
        """
        pass
