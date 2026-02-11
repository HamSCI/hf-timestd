"""
Bootstrap State File Management

Provides inter-process communication for bootstrap lock state using a simple
JSON state file with inotify-based watching.

Architecture:
- MetrologyEngine writes bootstrap_state.json when FusionTimingState achieves lock
- Fusion service watches for this file and waits until locked
- State file includes lock tier, D_clock estimate, and uncertainty

This avoids polling and provides immediate notification when bootstrap locks.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# Default state file location
# NOTE: This must match the file written by bootstrap_service.py
DEFAULT_STATE_FILE = Path('/var/lib/timestd/state/bootstrap_timing_reference.json')


@dataclass
class BootstrapState:
    """Bootstrap lock state."""
    locked: bool = False
    lock_tier: str = 'NONE'  # NONE, PROVISIONAL, REFINED
    d_clock_ms: Optional[float] = None
    uncertainty_ms: Optional[float] = None
    lock_time: Optional[str] = None  # ISO timestamp
    sample_rate: int = 24000
    # Reference point for RTP-to-UTC(NIST) conversion (derived from tones, NOT NTP)
    # UTC = reference_utc + (RTP - reference_rtp) / sample_rate
    reference_rtp: Optional[int] = None      # RTP at a known minute boundary
    reference_utc: Optional[float] = None    # UTC timestamp of that minute boundary
    # Legacy fields (may be removed)
    rtp_to_utc_offset_samples: Optional[int] = None
    rtp_to_utc_offset_sec: Optional[float] = None
    ntp_correction_ms: Optional[float] = None
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(asdict(self), indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'BootstrapState':
        """Deserialize from JSON.
        
        Handles both the old bootstrap_state.json format and the new
        bootstrap_timing_reference.json format written by bootstrap_service.py.
        """
        data = json.loads(json_str)
        
        # Extract only the fields that BootstrapState expects
        # The timing reference file has additional fields we don't need
        return cls(
            locked=data.get('locked', False),
            lock_tier=data.get('lock_tier', 'NONE'),
            d_clock_ms=data.get('d_clock_ms'),
            uncertainty_ms=data.get('uncertainty_ms'),
            lock_time=data.get('lock_time'),
            sample_rate=data.get('sample_rate', 24000),
            reference_rtp=data.get('reference_rtp'),
            reference_utc=data.get('reference_utc'),
            rtp_to_utc_offset_samples=data.get('rtp_to_utc_offset_samples'),
            rtp_to_utc_offset_sec=data.get('rtp_to_utc_offset_sec'),
            ntp_correction_ms=data.get('ntp_correction_ms')
        )
    
    @classmethod
    def from_file(cls, path: Path) -> Optional['BootstrapState']:
        """Load state from file."""
        try:
            if path.exists():
                with open(path, 'r') as f:
                    return cls.from_json(f.read())
        except Exception as e:
            logger.warning(f"Failed to read bootstrap state: {e}")
        return None


class BootstrapStateWriter:
    """
    Writes bootstrap state to a file for other services to read.
    
    Used by MetrologyEngine to signal bootstrap lock to fusion service.
    """
    
    def __init__(self, state_file: Path = DEFAULT_STATE_FILE):
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def write_locked(
        self,
        lock_tier: str,
        d_clock_ms: float,
        uncertainty_ms: float,
        sample_rate: int = 24000,
        reference_rtp: Optional[int] = None,
        reference_utc: Optional[float] = None
    ):
        """Write locked state to file.
        
        Args:
            lock_tier: Lock tier (PROVISIONAL or REFINED)
            d_clock_ms: Estimated D_clock (system clock offset from UTC)
            uncertainty_ms: Uncertainty in D_clock estimate
            sample_rate: Sample rate in Hz
            reference_rtp: RTP value at a known minute boundary (from tones)
            reference_utc: UTC timestamp of that minute boundary (from tones + station ID)
        """
        state = BootstrapState(
            locked=True,
            lock_tier=lock_tier,
            d_clock_ms=d_clock_ms,
            uncertainty_ms=uncertainty_ms,
            lock_time=datetime.now(timezone.utc).isoformat(),
            sample_rate=sample_rate,
            reference_rtp=reference_rtp,
            reference_utc=reference_utc
        )
        self._write(state)
        if reference_utc:
            logger.info(f"[BOOTSTRAP] Wrote lock state: {lock_tier}, D_clock={d_clock_ms:+.1f}ms, "
                       f"ref_rtp={reference_rtp}, ref_utc={reference_utc:.3f}")
        else:
            logger.info(f"[BOOTSTRAP] Wrote lock state: {lock_tier}, D_clock={d_clock_ms:+.1f}ms")
    
    def write_unlocked(self):
        """Write unlocked state to file."""
        state = BootstrapState(locked=False, lock_tier='NONE')
        self._write(state)
        logger.info("[BOOTSTRAP] Wrote unlocked state")
    
    def _write(self, state: BootstrapState):
        """Atomic write to state file."""
        temp_file = self.state_file.with_suffix('.tmp')
        try:
            with open(temp_file, 'w') as f:
                f.write(state.to_json())
                f.flush()
                os.fsync(f.fileno())
            temp_file.replace(self.state_file)
        except Exception as e:
            logger.error(f"Failed to write bootstrap state: {e}")
            if temp_file.exists():
                temp_file.unlink()


class BootstrapStateWatcher:
    """
    Watches for bootstrap state changes using inotify.
    
    Used by fusion service to wait for bootstrap lock before processing.
    """
    
    def __init__(
        self,
        state_file: Path = DEFAULT_STATE_FILE,
        on_lock: Optional[Callable[[BootstrapState], None]] = None,
        on_unlock: Optional[Callable[[], None]] = None
    ):
        self.state_file = state_file
        self.on_lock = on_lock
        self.on_unlock = on_unlock
        self._current_state: Optional[BootstrapState] = None
        self._observer = None
        self._running = False
        self._lock_event = threading.Event()
    
    def get_state(self) -> Optional[BootstrapState]:
        """Get current bootstrap state."""
        return BootstrapState.from_file(self.state_file)
    
    def is_locked(self) -> bool:
        """Check if bootstrap is currently locked."""
        state = self.get_state()
        return state is not None and state.locked
    
    def wait_for_lock(self, timeout: Optional[float] = None) -> bool:
        """
        Block until bootstrap achieves lock.
        
        Args:
            timeout: Maximum seconds to wait (None = forever)
            
        Returns:
            True if locked, False if timeout
        """
        # Check if already locked
        if self.is_locked():
            logger.info("[BOOTSTRAP] Already locked")
            return True
        
        # Try to use watchdog for efficient waiting
        try:
            return self._wait_with_watchdog(timeout)
        except ImportError:
            logger.warning("watchdog not available, falling back to polling")
            return self._wait_with_polling(timeout)
    
    def _wait_with_watchdog(self, timeout: Optional[float]) -> bool:
        """Wait using inotify via watchdog."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
        
        class StateFileHandler(FileSystemEventHandler):
            def __init__(self, watcher: 'BootstrapStateWatcher'):
                self.watcher = watcher
            
            def on_modified(self, event):
                if event.src_path == str(self.watcher.state_file):
                    self._check_lock()
            
            def on_created(self, event):
                if event.src_path == str(self.watcher.state_file):
                    self._check_lock()
            
            def _check_lock(self):
                state = self.watcher.get_state()
                if state and state.locked:
                    self.watcher._current_state = state
                    self.watcher._lock_event.set()
                    if self.watcher.on_lock:
                        self.watcher.on_lock(state)
        
        # Ensure parent directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Start watching
        handler = StateFileHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self.state_file.parent), recursive=False)
        observer.start()
        
        try:
            logger.info(f"[BOOTSTRAP] Waiting for lock (watching {self.state_file})...")
            
            # Check once immediately in case file was created before we started watching
            if self.is_locked():
                self._lock_event.set()
            
            # Wait for lock event
            locked = self._lock_event.wait(timeout=timeout)
            
            if locked:
                state = self.get_state()
                logger.info(
                    f"[BOOTSTRAP] Lock detected: {state.lock_tier}, "
                    f"D_clock={state.d_clock_ms:+.1f}ms ± {state.uncertainty_ms:.1f}ms"
                )
            else:
                logger.warning(f"[BOOTSTRAP] Timeout waiting for lock ({timeout}s)")
            
            return locked
            
        finally:
            observer.stop()
            observer.join(timeout=1.0)
    
    def _wait_with_polling(self, timeout: Optional[float]) -> bool:
        """Fallback polling-based wait."""
        start = time.time()
        poll_interval = 1.0  # Check every second
        
        while True:
            if self.is_locked():
                state = self.get_state()
                logger.info(
                    f"[BOOTSTRAP] Lock detected: {state.lock_tier}, "
                    f"D_clock={state.d_clock_ms:+.1f}ms"
                )
                return True
            
            if timeout is not None and (time.time() - start) >= timeout:
                logger.warning(f"[BOOTSTRAP] Timeout waiting for lock ({timeout}s)")
                return False
            
            time.sleep(poll_interval)
