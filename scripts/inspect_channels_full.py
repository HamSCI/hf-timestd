#!/usr/bin/env python3
from ka9q import discover_channels, Encoding

print("Discovering channels...")
chs = discover_channels('239.192.152.141', listen_duration=1.0)
print(f"Found {len(chs)} channels")
print("\nSSRC       Freq (MHz)   Dest            Encoding  Rate")
print("-" * 60)

for ssrc, info in chs.items():
    enc = getattr(info, 'encoding', 'N/A')
    freq = info.frequency / 1e6
    dest = getattr(info, 'multicast_address', 'N/A')
    rate = getattr(info, 'sample_rate', 'N/A')
    print(f"{ssrc:<10} {freq:<12.3f} {dest:<15} {enc:<8} {rate}")
