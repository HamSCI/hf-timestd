"""
Chrony Shared Memory (SHM) Refclock Driver

This module writes timing data to a System V Shared Memory segment 
compatible with Chrony's 'refclock SHM' driver.

Chronyd Configuration:
----------------------
Add to /etc/chrony/chrony.conf:

    # HF Time Transfer via time-manager
    refclock SHM 0 refid TMGR poll 6 precision 1e-4 offset 0.0 delay 0.2
    
Restart: sudo systemctl restart chronyd

Structure Layout (struct shmTime on 64-bit Linux):
-------------------------------------------------
    int    mode;                 // 0-3   (Mode 1 = use count locking)
    int    count;                // 4-7   (Sequence counter)
    time_t clockTimeStampSec;    // 8-15  (System time seconds)
    int    clockTimeStampUSec;   // 16-19 (System time microseconds)
    [pad]                        // 20-23 (Alignment padding)
    time_t receiveTimeStampSec;  // 24-31 (Reference time seconds)
    int    receiveTimeStampUSec; // 32-35 (Reference time microseconds)
    [pad]                        // 36-39 (Alignment padding)
    int    leap;                 // 40-43 (Leap second indicator)
    int    precision;            // 44-47 (log2 precision)
    int    nsamples;             // 48-51 (Number of samples)
    int    valid;                // 52-55 (Data is valid)

NOTE: Chrony creates the SHM segment. We attach to it.
      If running as non-root, ensure permissions allow access.

Reference:
- https://chrony.tuxfamily.org/doc/4.0/chrony.conf.html#refclock
"""

import logging
import mmap
import os
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)


# SHM segment key base (NTP convention)
# Key = 0x4e545030 + unit number (0-3)
# 0x4e545030 = "NTP0" in ASCII
SHM_KEY_BASE = 0x4e545030

# SHM segment size (must match chronyd expectation)
SHM_SIZE = 96  # bytes


class ChronySHM:
    """
    Chrony SHM refclock driver.
    
    Writes time samples to a shared memory segment that chronyd reads
    to discipline the system clock.
    
    Usage:
        shm = ChronySHM(unit=0)
        if shm.connect():
            # When you have a valid time measurement:
            shm.update(
                reference_time=utc_from_wwv,  # UTC timestamp
                system_time=time.time(),      # When measurement taken
                precision=-10                 # ~1ms precision
            )
    """
    
    def __init__(self, unit: int = 0):
        """
        Initialize Chrony SHM driver.
        
        Args:
            unit: SHM unit number (0-3). Corresponds to "refclock SHM N"
                  in chrony.conf. Default is 0.
        """
        self.unit = unit
        self.key = SHM_KEY_BASE + unit
        self.shm_id: Optional[int] = None
        self.shm_map: Optional[mmap.mmap] = None
        self.count = 0
        self.connected = False
        
        logger.info(f"ChronySHM initialized: unit={unit}, key=0x{self.key:08x}")
    
    def connect(self) -> bool:
        """
        Connect to (or create) the SHM segment.
        
        Returns:
            True if connected successfully
        """
        try:
            # Use sysv_ipc if available, otherwise fall back to file-based
            try:
                import sysv_ipc
                self._connect_sysv(sysv_ipc)
            except ImportError:
                logger.warning("sysv_ipc not available, using file-based SHM")
                self._connect_file()
            
            self.connected = True
            logger.info(f"ChronySHM connected: unit={self.unit}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to Chrony SHM: {e}")
            return False
    
    def _connect_sysv(self, sysv_ipc):
        """Connect using System V IPC shared memory."""
        try:
            # Try to CREATE segment first (for fresh installs)
            # This ensures we create it with group-writable permissions
            # If chronyd hasn't started yet, we create it and chronyd will attach
            self.shm = sysv_ipc.SharedMemory(
                self.key,
                flags=sysv_ipc.IPC_CREAT | sysv_ipc.IPC_EXCL,
                size=SHM_SIZE,
                mode=0o666  # World-readable for cross-platform chronyd compatibility
            )
            logger.info("Created new Chrony SHM segment with world-readable permissions (0666)")
        except sysv_ipc.ExistentialError:
            # Segment already exists, try to attach
            try:
                self.shm = sysv_ipc.SharedMemory(
                    self.key,
                    flags=0,  # Attach to existing
                    size=SHM_SIZE
                )
                logger.info("Attached to existing Chrony SHM segment")
            except PermissionError as e:
                # SHM segment exists but we don't have permission
                # This usually means chronyd created it with restricted permissions (0600)
                logger.error(
                    f"Permission denied accessing Chrony SHM (key=0x{self.key:08x}). "
                    f"The segment exists but has restrictive permissions. "
                    f"Solution: Stop chronyd, remove SHM, start timestd-analytics (creates SHM), then start chronyd. "
                    f"Commands: sudo systemctl stop chronyd && sudo ipcrm -M 0x{self.key:08x} && "
                    f"sudo systemctl restart timestd-analytics && sudo systemctl start chronyd"
                )
                raise
        
        self._use_sysv = True
    
    def _connect_file(self):
        """Connect using file-based shared memory (fallback)."""
        # File-based approach for systems without sysv_ipc
        shm_path = f"/dev/shm/chrony_shm_{self.unit}"
        
        # Create or open the file
        if not os.path.exists(shm_path):
            with open(shm_path, 'wb') as f:
                f.write(b'\x00' * SHM_SIZE)
            os.chmod(shm_path, 0o666)
        
        # Memory-map the file
        fd = os.open(shm_path, os.O_RDWR)
        self.shm_map = mmap.mmap(fd, SHM_SIZE)
        os.close(fd)
        
        self._use_sysv = False
        logger.info(f"Using file-based SHM: {shm_path}")
    
    def update(
        self,
        reference_time: float,
        system_time: Optional[float] = None,
        precision: int = -10,
        leap: int = 0
    ) -> bool:
        """
        Update the SHM segment with a new time sample.
        
        This should be called when you have a valid time measurement.
        For WWV timing, call this when a tone is detected and D_clock
        is computed.
        
        Args:
            reference_time: UTC timestamp from the time reference (WWV tones)
            system_time: System clock time when measurement was taken
                         (default: current time)
            precision: Log2 of precision in seconds. -10 = ~1ms, -13 = ~122μs
            leap: Leap second indicator (0=none, 1=insert, 2=delete)
        
        Returns:
            True if update successful
        """
        if not self.connected:
            logger.warning("ChronySHM not connected")
            return False
        
        if system_time is None:
            system_time = time.time()
        
        try:
            # Increment sequence counter
            self.count += 1
            
            # Split timestamps into seconds and microseconds
            # NOTE: Chrony SHM convention (opposite of NTP):
            # clockTimeStamp = reference time (true UTC)
            # receiveTimeStamp = system time (when measurement taken)
            # Chrony calculates: offset = receiveTimeStamp - clockTimeStamp
            # So: offset = system_time - reference_time = D_clock ✓
            clock_sec = int(reference_time)
            clock_usec = int((reference_time - clock_sec) * 1_000_000)
            
            recv_sec = int(system_time)
            recv_usec = int((system_time - recv_sec) * 1_000_000)
            
            # Nanoseconds for extended precision
            clock_nsec = int((reference_time - clock_sec) * 1_000_000_000) % 1_000_000_000
            recv_nsec = int((system_time - recv_sec) * 1_000_000_000) % 1_000_000_000
            
            # Pack the SHM structure for 64-bit Linux (96 bytes total)
            # Format: @iiqiiqiiiiii8i with proper alignment
            # 
            # Layout (with native alignment):
            #   0-3:   int mode
            #   4-7:   int count
            #   8-15:  time_t clockTimeStampSec (64-bit)
            #   16-19: int clockTimeStampUSec
            #   20-23: (padding for 8-byte alignment)
            #   24-31: time_t receiveTimeStampSec (64-bit)
            #   32-35: int receiveTimeStampUSec
            #   36-39: (padding for 8-byte alignment)  
            #   40-43: int leap
            #   44-47: int precision
            #   48-51: int nsamples
            #   52-55: int valid
            #   56-59: unsigned clockTimeStampNSec
            #   60-63: unsigned receiveTimeStampNSec
            #   64-95: int dummy[8]
            
            data = struct.pack(
                '@ii q i 4x q i 4x iiii II iiiiiiii',
                1,              # mode = 1 (use count locking)
                self.count,     # count (sequence number)
                clock_sec,      # clockTimeStampSec (system time)
                clock_usec,     # clockTimeStampUSec
                # 4x padding for 8-byte alignment
                recv_sec,       # receiveTimeStampSec (reference/true time)
                recv_usec,      # receiveTimeStampUSec
                # 4x padding for 4-byte alignment
                leap,           # leap
                precision,      # precision
                1,              # nsamples
                1,              # valid
                clock_nsec,     # clockTimeStampNSec
                recv_nsec,      # receiveTimeStampNSec
                0, 0, 0, 0, 0, 0, 0, 0  # dummy[8]
            )
            
            # Write to SHM
            if self._use_sysv:
                self.shm.write(data, 0)
            else:
                self.shm_map.seek(0)
                self.shm_map.write(data)
                self.shm_map.flush()
            
            if self.count % 60 == 0:
                logger.debug(
                    f"ChronySHM update #{self.count}: "
                    f"ref={reference_time:.6f}, sys={system_time:.6f}, "
                    f"offset={(system_time - reference_time)*1000:+.2f}ms"
                )
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to update Chrony SHM: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from SHM segment."""
        try:
            if self._use_sysv and hasattr(self, 'shm'):
                self.shm.detach()
            elif self.shm_map:
                self.shm_map.close()
            
            self.connected = False
            logger.info("ChronySHM disconnected")
            
        except Exception as e:
            logger.warning(f"Error disconnecting ChronySHM: {e}")


def install_chrony_config(unit: int = 0) -> str:
    """
    Generate chrony.conf snippet for HF time transfer.
    
    Args:
        unit: SHM unit number
        
    Returns:
        Configuration snippet to add to /etc/chrony/chrony.conf
    """
    return f"""
# =============================================================================
# HF Time Transfer via time-manager
# =============================================================================
# This refclock receives UTC from WWV/WWVH/CHU time broadcasts, providing
# ~1ms accuracy. It can be used as a backup to GPS or as primary reference.

refclock SHM {unit} refid HF poll 3 precision 1e-3

# Explanation:
#   SHM {unit}     - Shared memory unit {unit} (key 0x{SHM_KEY_BASE + unit:08x})
#   refid HF       - Reference ID shown in 'chronyc sources' (HF = High Frequency)
#   poll 3         - Poll interval 2^3 = 8 seconds
#   precision 1e-3 - 1 millisecond precision

# To verify: run 'chronyc sources -v' and look for 'HF' reference
# =============================================================================
"""


if __name__ == "__main__":
    # Test the ChronySHM driver
    logging.basicConfig(level=logging.DEBUG)
    
    print("Chrony SHM Driver Test")
    print("=" * 60)
    
    shm = ChronySHM(unit=0)
    
    if shm.connect():
        print(f"Connected to SHM unit 0 (key 0x{shm.key:08x})")
        
        # Simulate time updates
        for i in range(5):
            now = time.time()
            # Simulate WWV time (current UTC with ~5ms propagation delay)
            wwv_time = now - 0.005
            
            shm.update(
                reference_time=wwv_time,
                system_time=now,
                precision=-10  # ~1ms
            )
            
            print(f"Update {i+1}: offset={(now - wwv_time)*1000:.2f}ms")
            time.sleep(1)
        
        shm.disconnect()
        print("Test complete")
    else:
        print("Failed to connect to SHM")
    
    print("\nChrony configuration snippet:")
    print(install_chrony_config(unit=0))
