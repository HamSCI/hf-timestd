#!/usr/bin/env python3
"""
Apply Kalman filter fields to L2TimingMeasurement instantiation
"""

import re
from pathlib import Path

# Target file
target_file = Path("/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_analytics_service.py")

# Read the file
with open(target_file, 'r') as f:
    content = f.read()

# Find and replace the snr_db line to add Kalman fields after it
pattern = r'(                        snr_db=float\(snr_db\) if snr_db is not None else None,\n)(                        utc_verified=)'

replacement = r'''\1                        
                        # Per-Broadcast Kalman Filter State (Science-First v5.0)
                        tof_kalman_ms=tof_kalman_ms,
                        tof_uncertainty_ms=tof_uncertainty_ms,
                        doppler_ms_per_min=doppler_ms_per_min,
                        gpsdo_consistent=gpsdo_consistent,
                        
\2'''

# Apply the replacement
new_content = re.sub(pattern, replacement, content)

if new_content == content:
    print("ERROR: Pattern not found - file may have changed")
    print("Looking for snr_db line...")
    if 'snr_db=float(snr_db)' in content:
        print("Found snr_db line")
    if 'utc_verified=' in content:
        print("Found utc_verified line")
    exit(1)

# Write back
with open(target_file, 'w') as f:
    f.write(new_content)

print(f"✅ Successfully added Kalman fields to L2TimingMeasurement in {target_file}")
print("\n🎉 Analytics service integration complete!")
print("\nNext steps:")
print("1. Test the integration")
print("2. Restart analytics service")
print("3. Verify Kalman state files are created")
