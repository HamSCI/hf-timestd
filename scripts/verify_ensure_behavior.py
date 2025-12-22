#!/usr/bin/env python3
from ka9q import RadiodControl
import time

CTL = RadiodControl('239.192.152.141')

# 1. Create explicit channel
print("Creating channel A...")
ssrc1 = CTL.create_channel(frequency_hz=1000000, encoding=4) # F32
print(f"Channel A created: {ssrc1}")

time.sleep(1.0) # wait for registry

# 2. Ensure channel with NO destination specified
print("Ensuring channel (dest=None)...")
info = CTL.ensure_channel(frequency_hz=1000000, encoding=4, destination=None)

print(f"Ensure returned SSRC: {info.ssrc}")

if info.ssrc == ssrc1:
    print("MATCH: ensure_channel(dest=None) matches existing channel.")
else:
    print("MISMATCH: ensure_channel(dest=None) created NEW channel.")

# Cleanup
CTL.remove_channel(ssrc1)
if info.ssrc != ssrc1:
    CTL.remove_channel(info.ssrc)
