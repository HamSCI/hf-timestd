#!/usr/bin/env python3
"""
List active channels from radiod.
"""
import time
from ka9q import RadiodControl
from ka9q.discovery import discover_channels, ChannelInfo

def main():
    print("Discovering channels (1.0s)...")
    # Use the address from timestd config
    channels = discover_channels("bee1-hf-status.local", listen_duration=2.0)
    
    if not channels:
        print("No channels found.")
        return

    print(f"Found {len(channels)} channels:")
    print(f"{'SSRC':>10} {'Freq (MHz)':>10} {'Mode':>6} {'Rate':>8} {'Dest':>20}")
    print("-" * 60)
    
    for ssrc, ch in sorted(channels.items()):
        print(f"{ssrc:10d} {ch.frequency/1e6:10.3f} {ch.preset:>6} {ch.sample_rate:8d} {ch.multicast_address or '':>20}")

if __name__ == "__main__":
    main()
