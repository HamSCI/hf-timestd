#!/usr/bin/env python3
"""
Chrony SHM Diagnostic Tool

This script reads the Chrony SHM segment and displays its contents to help
diagnose why Chrony is not accepting updates from the fusion service.

Usage:
    sudo python scripts/verify_chrony_shm.py [--unit 0] [--monitor]
"""

import argparse
import struct
import time
import sys
from datetime import datetime, timezone

# SHM segment key base (NTP convention)
SHM_KEY_BASE = 0x4e545030
SHM_SIZE = 96


def read_shm_segment(unit: int = 0):
    """Read and parse the Chrony SHM segment."""
    key = SHM_KEY_BASE + unit
    
    try:
        import sysv_ipc
        shm = sysv_ipc.SharedMemory(key, flags=0, size=SHM_SIZE)
        data = shm.read(SHM_SIZE, 0)
        shm.detach()
    except ImportError:
        print("ERROR: sysv_ipc module not available")
        print("Install with: pip install sysv_ipc")
        return None
    except sysv_ipc.ExistentialError:
        print(f"ERROR: SHM segment does not exist (key=0x{key:08x})")
        print("The fusion service may not be running or SHM not initialized")
        return None
    except PermissionError:
        print(f"ERROR: Permission denied accessing SHM segment (key=0x{key:08x})")
        print("Try running with sudo")
        return None
    
    # Parse the SHM structure
    # Format: @ii q i 4x q i 4x iiii II iiiiiiii
    try:
        unpacked = struct.unpack('@ii q i 4x q i 4x iiii II iiiiiiii', data)
        
        mode = unpacked[0]
        count = unpacked[1]
        clock_sec = unpacked[2]
        clock_usec = unpacked[3]
        recv_sec = unpacked[4]
        recv_usec = unpacked[5]
        leap = unpacked[6]
        precision = unpacked[7]
        nsamples = unpacked[8]
        valid = unpacked[9]
        clock_nsec = unpacked[10]
        recv_nsec = unpacked[11]
        
        return {
            'mode': mode,
            'count': count,
            'clock_sec': clock_sec,
            'clock_usec': clock_usec,
            'recv_sec': recv_sec,
            'recv_usec': recv_usec,
            'leap': leap,
            'precision': precision,
            'nsamples': nsamples,
            'valid': valid,
            'clock_nsec': clock_nsec,
            'recv_nsec': recv_nsec,
        }
    except struct.error as e:
        print(f"ERROR: Failed to unpack SHM data: {e}")
        print(f"Data length: {len(data)} bytes (expected {SHM_SIZE})")
        return None


def format_timestamp(sec, usec):
    """Format a timestamp for display."""
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{usec:06d} UTC"
    except (ValueError, OSError):
        return f"INVALID ({sec}.{usec:06d})"


def display_shm_data(data, verbose=False):
    """Display SHM segment contents in human-readable format."""
    if data is None:
        return
    
    print("\n" + "="*70)
    print("CHRONY SHM SEGMENT CONTENTS")
    print("="*70)
    
    # Basic fields
    print(f"\nMode:           {data['mode']}")
    print(f"Count:          {data['count']}")
    print(f"Valid:          {data['valid']} {'✓ VALID' if data['valid'] else '✗ INVALID'}")
    print(f"Leap:           {data['leap']}")
    print(f"Precision:      {data['precision']} (2^{data['precision']} seconds = {2**data['precision']:.6f}s)")
    print(f"Nsamples:       {data['nsamples']}")
    
    # Timestamps
    print(f"\nClock Timestamp (Reference/True Time):")
    clock_time = data['clock_sec'] + data['clock_usec'] / 1_000_000.0
    print(f"  Seconds:      {data['clock_sec']}")
    print(f"  Microseconds: {data['clock_usec']}")
    print(f"  Nanoseconds:  {data['clock_nsec']}")
    print(f"  Formatted:    {format_timestamp(data['clock_sec'], data['clock_usec'])}")
    print(f"  Unix time:    {clock_time:.6f}")
    
    print(f"\nReceive Timestamp (System Time):")
    recv_time = data['recv_sec'] + data['recv_usec'] / 1_000_000.0
    print(f"  Seconds:      {data['recv_sec']}")
    print(f"  Microseconds: {data['recv_usec']}")
    print(f"  Nanoseconds:  {data['recv_nsec']}")
    print(f"  Formatted:    {format_timestamp(data['recv_sec'], data['recv_usec'])}")
    print(f"  Unix time:    {recv_time:.6f}")
    
    # Calculated offset
    offset_sec = recv_time - clock_time
    offset_ms = offset_sec * 1000.0
    print(f"\nCalculated Offset (Receive - Clock):")
    print(f"  {offset_ms:+.3f} ms")
    
    # Age of data
    now = time.time()
    age_sec = now - recv_time
    print(f"\nData Age:")
    print(f"  {age_sec:.1f} seconds ago")
    if age_sec > 30:
        print(f"  ⚠️  WARNING: Data is stale (>{age_sec:.0f}s old)")
    
    # Validation checks
    print(f"\n" + "-"*70)
    print("VALIDATION CHECKS")
    print("-"*70)
    
    checks_passed = 0
    checks_total = 0
    
    # Check 1: Mode
    checks_total += 1
    if data['mode'] == 1:
        print("✓ Mode is 1 (count locking)")
        checks_passed += 1
    else:
        print(f"✗ Mode is {data['mode']} (expected 1)")
    
    # Check 2: Valid flag
    checks_total += 1
    if data['valid'] == 1:
        print("✓ Valid flag is set")
        checks_passed += 1
    else:
        print("✗ Valid flag is NOT set")
    
    # Check 3: Timestamps are reasonable
    checks_total += 1
    if 1700000000 < data['clock_sec'] < 2000000000:
        print("✓ Clock timestamp is in reasonable range")
        checks_passed += 1
    else:
        print(f"✗ Clock timestamp is out of range: {data['clock_sec']}")
    
    checks_total += 1
    if 1700000000 < data['recv_sec'] < 2000000000:
        print("✓ Receive timestamp is in reasonable range")
        checks_passed += 1
    else:
        print(f"✗ Receive timestamp is out of range: {data['recv_sec']}")
    
    # Check 4: Precision is negative
    checks_total += 1
    if data['precision'] < 0:
        print(f"✓ Precision is negative ({data['precision']})")
        checks_passed += 1
    else:
        print(f"✗ Precision should be negative, got {data['precision']}")
    
    # Check 5: Offset is reasonable
    checks_total += 1
    if abs(offset_ms) < 100:
        print(f"✓ Offset is reasonable ({offset_ms:+.3f} ms)")
        checks_passed += 1
    else:
        print(f"⚠️  Offset is large: {offset_ms:+.3f} ms")
        checks_passed += 0.5
    
    # Check 6: Data is fresh
    checks_total += 1
    if age_sec < 30:
        print(f"✓ Data is fresh ({age_sec:.1f}s old)")
        checks_passed += 1
    else:
        print(f"✗ Data is stale ({age_sec:.1f}s old)")
    
    print(f"\n{'='*70}")
    print(f"VALIDATION SUMMARY: {checks_passed}/{checks_total} checks passed")
    print(f"{'='*70}\n")
    
    if checks_passed == checks_total:
        print("✓ SHM segment appears to be correctly formatted and up-to-date")
        print("  If Chrony is not reading it, the issue may be with Chrony configuration")
    else:
        print("✗ SHM segment has issues that may prevent Chrony from accepting updates")


def monitor_shm(unit: int = 0, interval: float = 1.0):
    """Monitor SHM segment in real-time."""
    print(f"Monitoring SHM unit {unit} (press Ctrl+C to stop)...")
    print(f"Update interval: {interval}s\n")
    
    last_count = None
    
    try:
        while True:
            data = read_shm_segment(unit)
            if data:
                # Clear screen (optional)
                # print("\033[2J\033[H", end="")
                
                now = time.time()
                recv_time = data['recv_sec'] + data['recv_usec'] / 1_000_000.0
                clock_time = data['clock_sec'] + data['clock_usec'] / 1_000_000.0
                offset_ms = (recv_time - clock_time) * 1000.0
                age_sec = now - recv_time
                
                # Detect updates
                update_indicator = ""
                if last_count is not None and data['count'] != last_count:
                    update_indicator = " ← NEW UPDATE"
                last_count = data['count']
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Count={data['count']:4d} "
                      f"Valid={data['valid']} "
                      f"Offset={offset_ms:+7.3f}ms "
                      f"Age={age_sec:5.1f}s"
                      f"{update_indicator}")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Could not read SHM")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Chrony SHM Diagnostic Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Display SHM contents once
  sudo python scripts/verify_chrony_shm.py
  
  # Monitor SHM updates in real-time
  sudo python scripts/verify_chrony_shm.py --monitor
  
  # Monitor with faster updates
  sudo python scripts/verify_chrony_shm.py --monitor --interval 0.5
        """
    )
    parser.add_argument('--unit', type=int, default=0,
                        help='SHM unit number (default: 0)')
    parser.add_argument('--monitor', action='store_true',
                        help='Monitor SHM updates in real-time')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='Monitor update interval in seconds (default: 1.0)')
    parser.add_argument('--verbose', action='store_true',
                        help='Verbose output')
    
    args = parser.parse_args()
    
    if args.monitor:
        monitor_shm(args.unit, args.interval)
    else:
        data = read_shm_segment(args.unit)
        display_shm_data(data, args.verbose)
        
        if data:
            sys.exit(0)
        else:
            sys.exit(1)


if __name__ == '__main__':
    main()
