#!/usr/bin/env python3
import time
import sys
from ka9q import discover_channels, RadiodControl

STATUS_IP = "239.192.152.141"

def main():
    print(f"Connecting to radiod at {STATUS_IP}...")
    control = RadiodControl(STATUS_IP)
    
    print("Discovering channels...")
    channels = discover_channels(STATUS_IP, listen_duration=2.0)
    
    if not channels:
        print("No channels found.")
        return

    print(f"Found {len(channels)} channels.")
    by_freq = {}
    
    for ssrc, ch in channels.items():
        if ch.frequency not in by_freq:
            by_freq[ch.frequency] = []
        by_freq[ch.frequency].append(ch)

    deleted_count = 0
    for freq, ch_list in by_freq.items():
        if len(ch_list) > 1:
            print(f"Frequency {freq/1e6:.3f} MHz has {len(ch_list)} duplicates. Cleaning up all...")
            for ch in ch_list:
                try:
                    control.remove_channel(ch.ssrc)
                    print(f"  Deleted SSRC {ch.ssrc:x}")
                    deleted_count += 1
                except Exception as e:
                    print(f"  Failed to delete {ch.ssrc:x}: {e}")
        else:
            # Optional: Delete even singletons if they are stale (Enc=2)?
            # User wants to fix "redundant" receivers. Safe to leave singletons?
            # Better to clean EVERYTHING on these frequencies to force fresh recreation.
            pass
            
    print(f"Cleanup complete. Deleted {deleted_count} channels.")
    
    # Verify
    time.sleep(1)
    remaining = discover_channels(STATUS_IP, listen_duration=1.0)
    print(f"Remaining channels: {len(remaining)}")

if __name__ == "__main__":
    main()
