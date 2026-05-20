"""
Chrony Shared Memory (SHM) Refclock Driver

This module writes timing data to a System V Shared Memory segment 
compatible with Chrony's 'refclock SHM' driver.

Chronyd Configuration:
----------------------
Add to /etc/chrony/chrony.conf:

    # HF Time Transfer via time-manager
    refclock SHM 0 refid HF poll 6 precision 1e-4 offset 0.0 delay 0.2
    
Restart: sudo systemctl restart chronyd

Structure Layout (struct shmTime, x86-64 Linux — the chrony/ntpd/gpsd SHM
refclock ABI; native alignment, time_t = int64):
--------------------------------------------------------------------------
    int      mode;                 // 0-3   (Mode 1 = count-locking protocol)
    int      count;                // 4-7   (sequence-lock counter)
    time_t   clockTimeStampSec;    // 8-15  (reference clock time, seconds)
    int      clockTimeStampUSec;   // 16-19 (reference clock time, microseconds)
    [pad]                          // 20-23 (8-byte alignment for the time_t below)
    time_t   receiveTimeStampSec;  // 24-31 (system/receive time, seconds)
    int      receiveTimeStampUSec; // 32-35 (system/receive time, microseconds)
    int      leap;                 // 36-39 (leap-second indicator — NO pad here)
    int      precision;            // 40-43 (log2 precision)
    int      nsamples;             // 44-47 (number of samples)
    int      valid;                // 48-51 (data-is-valid flag)
    unsigned clockTimeStampNSec;   // 52-55 (reference clock time, nanoseconds)
    unsigned receiveTimeStampNSec; // 56-59 (system/receive time, nanoseconds)
    int      dummy[8];             // 60-91 (reserved — chrony ignores it)
    [pad]                          // 92-95 (C struct trailing padding)

Total: 96 bytes. The packing format and the segment size both come from the
single SHM_STRUCT_FORMAT constant, so they cannot drift (see SHM_SIZE).

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

# struct shmTime packing format — see the module docstring for the layout.
# The trailing `4x` is the C struct's trailing padding (time_t forces 8-byte
# struct alignment); `iiiiiiii` is dummy[8], a reserved field chrony ignores.
SHM_STRUCT_FORMAT = '@ii q i 4x q i iiii II iiiiiiii 4x'

# SHM segment size — derived from the format so the two cannot drift (M-H21).
# chronyd's `struct shmTime` is 96 bytes on x86-64.
SHM_SIZE = struct.calcsize(SHM_STRUCT_FORMAT)  # 96 bytes


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

        # M-M17: consecutive-update-failure tracking.  A failed update()
        # now also clears `connected` so the caller's reconnect path
        # (see `multi_broadcast_fusion._loop`'s rate-limited reconnect
        # block) fires on the next cycle.  After this many back-to-back
        # failures we escalate the log level — silent failures here
        # mean chrony has no refclock samples and the system clock
        # silently degrades.
        self._consecutive_update_failures = 0
        self._UPDATE_FAILURE_ESCALATE_AT = 5

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
        """Connect using System V IPC shared memory.

        Handles the chrony-creates-first race condition: if chrony starts
        before fusion, it creates SHM segments as root:0600.  Fusion
        (running as timestd) can't write to them.  We detect that and
        fix the permissions **in place** via `shmctl(IPC_SET)` (sysv_ipc
        exposes this as the writable ``self.shm.mode`` property — needs
        CAP_IPC_OWNER, granted via AmbientCapabilities in the systemd
        unit, or running as root).

        M-M16 history: the previous code "recovered" from bad permissions
        by detaching, ``self.shm.remove()`` (= shmctl IPC_RMID), and
        recreating the segment.  shmctl IPC_RMID only **marks** a SysV
        segment for deletion — the segment lives on until every attached
        process detaches.  Since chronyd was still attached, our newly-
        created segment had a different shmid; chronyd kept reading the
        orphaned old one forever and our writes vanished into a segment
        nothing was listening to.  The same anti-pattern lived in the
        "can't even attach" path (ipcrm subprocess).  Both paths now
        either fix permissions in place or raise — failing loudly is
        the right answer because the operator's only safe recovery is
        to stop chronyd, ipcrm the segment, and restart both services.
        """
        try:
            # Try to CREATE segment first (for fresh installs).  This
            # ensures we create it with group-writable permissions; if
            # chronyd hasn't started yet, we create it and chronyd will
            # attach later.
            self.shm = sysv_ipc.SharedMemory(
                self.key,
                flags=sysv_ipc.IPC_CREAT | sysv_ipc.IPC_EXCL,
                size=SHM_SIZE,
                mode=0o666  # World-readable for cross-platform chronyd compatibility
            )
            logger.info("Created new Chrony SHM segment with world-readable permissions (0666)")
            self._use_sysv = True
            return

        except sysv_ipc.ExistentialError:
            pass  # Falls through to attach-and-fix path below

        # Segment exists — try to attach and (if needed) fix permissions
        # in place.
        try:
            self.shm = sysv_ipc.SharedMemory(
                self.key,
                flags=0,  # Attach to existing
                size=SHM_SIZE,
            )
        except (PermissionError, sysv_ipc.PermissionsError) as e:
            # We can't even attach.  The previous code's ipcrm-and-
            # recreate "recovery" silently orphaned chronyd (see method
            # docstring); refuse rather than do that.
            raise PermissionError(
                f"Cannot attach to Chrony SHM (key=0x{self.key:08x}): "
                f"permission denied (running as uid={os.getuid()}). "
                f"Operator fix: stop chronyd, "
                f"`sudo ipcrm -M 0x{self.key:08x}`, restart this service, "
                f"then start chronyd."
            ) from e

        current_mode = self.shm.mode
        current_uid = self.shm.uid
        my_uid = os.getuid()
        if current_mode & 0o666 == 0o666:
            logger.info(
                f"Attached to existing Chrony SHM segment "
                f"(mode={oct(current_mode)}, owner_uid={current_uid})"
            )
            self._use_sysv = True
            return

        # Permissions need widening.  Try to do it in place via shmctl
        # IPC_SET (sysv_ipc exposes this as a writable `mode` property).
        try:
            self.shm.mode = 0o666
            logger.warning(
                f"Fixed Chrony SHM permissions in place "
                f"(key=0x{self.key:08x}, mode={oct(current_mode)} → 0o666, "
                f"owner_uid={current_uid}, my_uid={my_uid})"
            )
        except (PermissionError, sysv_ipc.PermissionsError, OSError) as e:
            # In-place change failed — we lack CAP_IPC_OWNER and we're
            # not the owner.  Refuse rather than silently orphan chronyd
            # by remove-and-recreate.
            try:
                self.shm.detach()
            finally:
                self.shm = None
            raise PermissionError(
                f"Chrony SHM (key=0x{self.key:08x}) has restrictive "
                f"permissions (mode={oct(current_mode)}, "
                f"owner_uid={current_uid}, my_uid={my_uid}) and we lack "
                f"CAP_IPC_OWNER to fix them in place.  Refusing to "
                f"remove + recreate — that would silently orphan "
                f"chronyd (which is still attached to the old segment). "
                f"Operator fix: stop chronyd, "
                f"`sudo ipcrm -M 0x{self.key:08x}`, restart this service, "
                f"then start chronyd."
            ) from e

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
        try:
            self.shm_map = mmap.mmap(fd, SHM_SIZE)
        finally:
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
            # Mode 1 sequence-lock protocol:
            #   odd count  = write in progress (chrony must retry if count changes)
            #   even count = write complete    (chrony can safely use data)
            # We increment twice: once before packing (odd) and once after writing
            # (even), so chrony never reads a partially-updated struct.
            #
            # Normalize to an even base first: a prior mid-update failure may
            # have left self.count odd. Without this, `count += 1` would make
            # it even ("write complete") while a write is in progress —
            # inverting the protocol so chronyd ignores the refclock forever.
            if self.count % 2 == 1:
                self.count += 1
            self.count += 1  # now odd: write in progress

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

            # Pack the SHM structure to match chrony's `struct shmTime`
            # exactly (refclock_shm.c). This is the NTP shmTime layout
            # used by chrony, ntpd, and gpsd — 96 bytes on x86_64 Linux
            # (92 bytes of fields + 4 bytes of C struct trailing padding).
            #
            # Layout (native C alignment, time_t = int64):
            #   0-3:   int mode
            #   4-7:   int count                    (sequence-lock counter)
            #   8-15:  time_t clockTimeStampSec
            #   16-19: int clockTimeStampUSec
            #   20-23: padding (q field below needs 8-byte alignment)
            #   24-31: time_t receiveTimeStampSec
            #   32-35: int receiveTimeStampUSec
            #   36-39: int leap                     (NO padding here — `int`
            #                                        is 4-byte aligned and 36
            #                                        is already 4-aligned)
            #   40-43: int precision
            #   44-47: int nsamples
            #   48-51: int valid
            #   52-55: unsigned clockTimeStampNSec
            #   56-59: unsigned receiveTimeStampNSec
            #   60-91: int dummy[8]  (reserved — chrony ignores it)
            #   92-95: C struct trailing padding (`4x`)
            #
            # 2026-05-06 fix: a previous version of this struct format
            # inserted an extra `4x` between recv_usec and leap, claiming
            # "alignment for leap". That was wrong — `int leap` is already
            # 4-aligned at offset 36. The bogus padding shifted leap onwards
            # by 4 bytes against chrony's actual layout, so chrony read:
            #   - writer's `nsamples` (offset 48) as `valid`             — usually OK (nsamples=1)
            #   - writer's `valid` (offset 52) as `clockTimeStampNSec`   — read as 1ns
            #   - writer's `clock_nsec` (offset 56) as `receiveTimeStampNSec` — wrong field
            #   - writer's `recv_nsec` (offset 60) as `dummy[0]`         — silently dropped
            # TSL1/TSL2 mostly worked anyway because their `ref_time` is
            # a fractional UTC second, so writer's `clock_nsec` was a
            # plausible sub-second NSec that chrony tolerated. TSL3
            # rounds `ref_time` to integer GPS seconds, making clock_nsec
            # = 0 and exposing the bug fully — chrony saw recv_time off
            # by ~1 second and excluded TSL3 as an outlier (#x).
            data = struct.pack(
                SHM_STRUCT_FORMAT,
                1,              # mode = 1 (count-locking protocol)
                self.count,     # count (odd = write in progress)
                clock_sec,      # clockTimeStampSec (8-15)
                clock_usec,     # clockTimeStampUSec (16-19)
                # 4x padding (20-23) for q below
                recv_sec,       # receiveTimeStampSec (24-31)
                recv_usec,      # receiveTimeStampUSec (32-35)
                leap,           # leap (36-39)
                precision,      # precision (40-43)
                1,              # nsamples (44-47)
                1,              # valid (48-51)
                clock_nsec,     # clockTimeStampNSec (52-55)
                recv_nsec,      # receiveTimeStampNSec (56-59)
                0, 0, 0, 0, 0, 0, 0, 0  # dummy[8] (60-91)
            )

            # Write to SHM
            if self._use_sysv:
                self.shm.write(data, 0)
            else:
                self.shm_map.seek(0)
                self.shm_map.write(data)
                self.shm_map.flush()

            # Finalize sequence lock: advance count to next even value and
            # patch bytes 4-7 in the SHM so chrony sees an even count.
            self.count += 1  # now even: write complete
            count_bytes = struct.pack('@i', self.count)
            if self._use_sysv:
                self.shm.write(count_bytes, 4)
            else:
                self.shm_map.seek(4)
                self.shm_map.write(count_bytes)
                self.shm_map.flush()

            if self.count % 60 == 0:
                logger.debug(
                    f"ChronySHM write #{self.count}: "
                    f"mode=1, valid=1, precision={precision}, "
                    f"offset={(system_time - reference_time)*1000:+.3f}ms"
                )

            # M-M17: a successful update resets the consecutive-failure
            # counter so a single recovery cleans the slate.
            self._consecutive_update_failures = 0
            return True

        except Exception as e:
            logger.error(f"Failed to update Chrony SHM: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Restore the sequence-lock invariant: when update() returns, the
            # count MUST be even (write-complete) in both memory and the SHM
            # segment. A mid-update exception can leave it odd; if the segment
            # is left odd, chronyd treats every later sample as "write in
            # progress" and ignores the refclock until the next clean write.
            # The struct-body write is a single memcpy-class operation, so
            # forcing the count even exposes either the new sample or the
            # previous valid one — never a torn struct.
            try:
                if self.count % 2 == 1:
                    self.count += 1
                even_count = struct.pack('@i', self.count)
                if self._use_sysv:
                    self.shm.write(even_count, 4)
                else:
                    self.shm_map.seek(4)
                    self.shm_map.write(even_count)
                    self.shm_map.flush()
            except Exception as repair_err:
                logger.error(f"ChronySHM sequence-count repair failed: {repair_err}")

            # M-M17: mark disconnected so the caller's reconnect path
            # fires on the next cycle.  The previous code only returned
            # False — `self.connected` stayed True forever, and the
            # caller's `if not chrony_shm.connected: reconnect()` loop
            # never triggered.  Result: a transient SHM error
            # (e.g. mmap got closed, chronyd recreated the segment)
            # produced silent failures until the daemon restart.
            self.connected = False
            self._consecutive_update_failures += 1
            if self._consecutive_update_failures >= self._UPDATE_FAILURE_ESCALATE_AT:
                logger.critical(
                    f"ChronySHM: {self._consecutive_update_failures} "
                    f"consecutive update failures on unit {self.unit} "
                    f"(key=0x{self.key:08x}).  chronyd is receiving no "
                    f"refclock samples — system clock discipline depends "
                    f"on resolving this."
                )
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
