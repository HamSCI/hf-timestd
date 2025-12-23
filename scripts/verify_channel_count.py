#!/usr/bin/env python3
"""
Verification script for receiver proliferation fix.

Tests that channel count remains stable across service restarts.
"""

import sys
import time
from ka9q import discover_channels

def check_channel_count(status_address='radiod.local', expected_count=9):
    """Check current channel count."""
    channels = discover_channels(status_address, listen_duration=2.0)
    
    print(f"\n{'='*60}")
    print(f"Channel Count Verification - {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print(f"Total channels: {len(channels)}")
    print(f"Expected: {expected_count}")
    print(f"Status: {'✓ PASS' if len(channels) == expected_count else '✗ FAIL - DUPLICATES DETECTED'}")
    print(f"\nChannel Details:")
    
    # Group by frequency to detect duplicates
    by_freq = {}
    for ssrc, info in channels.items():
        freq = int(info.frequency)
        if freq not in by_freq:
            by_freq[freq] = []
        by_freq[freq].append((ssrc, info))
    
    for freq in sorted(by_freq.keys()):
        channels_at_freq = by_freq[freq]
        dup_marker = " ⚠️  DUPLICATE" if len(channels_at_freq) > 1 else ""
        print(f"\n  {freq/1e6:.3f} MHz: {len(channels_at_freq)} channel(s){dup_marker}")
        for ssrc, info in channels_at_freq:
            dest = getattr(info, 'multicast_address', 'N/A')
            enc = getattr(info, 'encoding', 'N/A')
            print(f"    SSRC={ssrc:08x}, preset={info.preset}, "
                  f"rate={info.sample_rate}, dest={dest}, enc={enc}")
    
    print(f"{'='*60}\n")
    
    return len(channels) == expected_count

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Verify channel count stability')
    parser.add_argument('--status-address', default='radiod.local',
                       help='Radiod status address')
    parser.add_argument('--expected', type=int, default=9,
                       help='Expected channel count')
    parser.add_argument('--watch', action='store_true',
                       help='Watch mode: check every 60 seconds')
    
    args = parser.parse_args()
    
    if args.watch:
        print("Watch mode: checking channel count every 60 seconds (Ctrl+C to stop)")
        try:
            while True:
                success = check_channel_count(args.status_address, args.expected)
                if not success:
                    print("⚠️  WARNING: Channel count mismatch detected!")
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nStopped watching")
    else:
        success = check_channel_count(args.status_address, args.expected)
        sys.exit(0 if success else 1)
