#!/usr/bin/env python3
from ka9q import RadiodControl, discover_channels

STATUS_ADDR = '239.192.152.141'
control = RadiodControl(STATUS_ADDR)
channels = discover_channels(STATUS_ADDR, 1.0)
print(f"Found {len(channels)} channels. Removing all...")
for ssrc in channels:
    try:
        control.remove_channel(ssrc)
        print(f"Removed {ssrc}")
    except Exception as e:
        print(f"Error removing {ssrc}: {e}")
