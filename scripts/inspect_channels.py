#!/usr/bin/env python3
from ka9q import discover_channels, Encoding

print("Discovering channels...")
# Use configured status address
chs = discover_channels("bee1-hf-status.local", listen_duration=2.0)
print(f"Found {len(chs)} channels")
print("\nSSRC       Freq (MHz)   Dest            Encoding")
print("-" * 50)

for ssrc, info in chs.items():
    enc = getattr(info, 'encoding', 'N/A')
    freq = info.frequency / 1e6
    dest = getattr(info, 'multicast_address', 'N/A')
    print(f"{ssrc:<10} {freq:<12.3f} {dest:<15} {enc}")
