#!/usr/bin/env python3
import time
import sys
from ka9q import discover_channels, RadiodControl

STATUS_IP = "239.192.152.141" # Default, or can be passed as arg

def main():
    print(f"Discovering channels on {STATUS_IP}...")
    channels = discover_channels(STATUS_IP, listen_duration=2.0)
    
    if not channels:
        print("No channels found.")
        return

    print(f"Found {len(channels)} channels:")
    by_freq = {}
    
    for ssrc, ch in channels.items():
        print(f"  SSRC {ssrc:x}: {ch.frequency/1e6:.3f} MHz, {ch.preset}, "
              f"Rate={ch.sample_rate}, SNR={ch.snr:.1f}, "
              f"Enc={ch.encoding}, Dest={ch.multicast_address}:{ch.port}")
        
        if ch.frequency not in by_freq:
            by_freq[ch.frequency] = []
        by_freq[ch.frequency].append(ch)

    print("\nDuplicate Analysis:")
    duplicates_found = False
    for freq, ch_list in by_freq.items():
        if len(ch_list) > 1:
            duplicates_found = True
            print(f"!! Frequency {freq/1e6:.3f} MHz has {len(ch_list)} channels:")
            for ch in ch_list:
                print(f"    SSRC {ch.ssrc:x}, Pres={ch.preset}, Rate={ch.sample_rate}, Enc={ch.encoding}")
    
    if not duplicates_found:
        print("No duplicates found by frequency.")
    else:
        print("Duplicates detected.")

if __name__ == "__main__":
    main()
