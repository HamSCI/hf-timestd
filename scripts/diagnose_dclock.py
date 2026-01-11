#!/usr/bin/env python3
"""
Diagnostic script to capture D_clock calculation details.
Run this to see exactly where negative D_clock values come from.
"""

import sys
sys.path.insert(0, '/opt/hf-timestd/src')

import time
import json
from pathlib import Path
from datetime import datetime

print("D_clock Diagnostic Monitor")
print("=" * 80)
print("Monitoring state files for D_clock calculations...")
print("This will show the actual values used in the calculation.")
print()

# Monitor these channels
channels = [
    ('WWV_25000', '/var/lib/timestd/state/phase2-wwv25.json'),
    ('CHU_7850', '/var/lib/timestd/state/phase2-chu7.85.json'),
    ('SHARED_10000', '/var/lib/timestd/state/phase2-shared10.json'),
]

# Track last seen values
last_seen = {}

print(f"Waiting for next minute boundary...")
now = time.time()
next_minute = (int(now / 60) + 1) * 60
time.sleep(next_minute - now + 2)  # Wait 2 seconds into next minute

print(f"\nCapturing at {datetime.utcfromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print("-" * 80)

for channel_name, state_file in channels:
    path = Path(state_file)
    if not path.exists():
        print(f"\n{channel_name}: State file not found")
        continue
    
    try:
        with open(path) as f:
            data = json.load(f)
        
        # Look for timing calibrator state
        if 'timing_calibrator' not in data:
            print(f"\n{channel_name}: No timing_calibrator state")
            continue
        
        tc = data['timing_calibrator']
        
        print(f"\n{channel_name}:")
        print(f"  Phase: {tc.get('phase', 'unknown')}")
        print(f"  Global RTP offset: {tc.get('global_rtp_offset', 'None')} samples")
        
        if tc.get('global_rtp_offset') is not None:
            offset_ms = tc['global_rtp_offset'] / 20000.0 * 1000.0
            print(f"                     = {offset_ms:.3f} ms")
        
        # Check for recent detections
        if 'bootstrap_detections' in tc and tc['bootstrap_detections']:
            recent = tc['bootstrap_detections'][-1]
            print(f"  Last detection:")
            print(f"    Station: {recent.get('station', 'unknown')}")
            print(f"    RTP offset: {recent.get('rtp_offset', 'None')} samples")
            print(f"    Confidence: {recent.get('confidence', 0.0):.2f}")
        
        # Check for RTP calibration
        if 'rtp_calibration' in tc and channel_name in tc['rtp_calibration']:
            cal = tc['rtp_calibration'][channel_name]
            print(f"  RTP calibration:")
            print(f"    Offset: {cal.get('rtp_offset_samples', 'None')} samples")
            print(f"    Confirmations: {cal.get('n_confirmations', 0)}")
    
    except Exception as e:
        print(f"\n{channel_name}: Error reading state - {e}")

print("\n" + "=" * 80)
print("\nKEY INSIGHT:")
print("The global_rtp_offset is added to rtp_timestamp to get expected_second_rtp.")
print("If this offset is wrong, all D_clock calculations will be systematically offset.")
print()
print("Expected behavior:")
print("  expected_second_rtp = rtp_timestamp + global_rtp_offset")
print("  emission_rtp = arrival_rtp - propagation_samples")
print("  D_clock = (emission_rtp - expected_second_rtp) / sample_rate * 1000")
print()
print("If D_clock is negative, then emission_rtp < expected_second_rtp")
print("This means: arrival_rtp - propagation < expected_second_rtp")
print("Or: arrival_rtp < expected_second_rtp + propagation")
print()
print("Since propagation is small (~2-5ms = 40-100 samples),")
print("arrival_rtp should be very close to expected_second_rtp.")
print()
print("If global_rtp_offset is too large, expected_second_rtp will be too large,")
print("making D_clock negative.")
