#!/usr/bin/env python3
"""
Fix calibration_offsets bug in phase2_temporal_engine.py
"""

from pathlib import Path

file_path = Path("/home/mjh/git/hf-timestd/src/hf_timestd/core/phase2_temporal_engine.py")

# Read file
with open(file_path, 'r') as f:
    lines = f.readlines()

# Fix 1: Add parameter to method signature (line 1928, 0-indexed 1927)
# Find the line with "forced_station: Optional[str] = None"
for i, line in enumerate(lines):
    if i >= 1920 and i <= 1930 and "forced_station: Optional[str] = None" in line:
        # Add calibration_offsets parameter after forced_station
        lines[i] = line.rstrip() + ",\n"
        lines.insert(i+1, "        calibration_offsets: Optional[Dict[str, float]] = None\n")
        print(f"✓ Added calibration_offsets parameter at line {i+1}")
        break

# Fix 2: Pass parameter in method call (around line 2644, now shifted by 1)
for i, line in enumerate(lines):
    if i >= 2640 and i <= 2650 and "forced_station=station" in line and "_step3_transmission_time_solution" in lines[i-5:i+1]:
        # Add calibration_offsets parameter
        lines[i] = line.rstrip() + ",\n"
        lines.insert(i+1, "                        calibration_offsets=calibration_offsets\n")
        print(f"✓ Added calibration_offsets argument at line {i+1}")
        break

# Write back
with open(file_path, 'w') as f:
    f.writelines(lines)

print(f"\n✅ Fixed calibration_offsets bug in {file_path}")
print("\nChanges:")
print("1. Added 'calibration_offsets' parameter to _step3_transmission_time_solution signature")
print("2. Passed 'calibration_offsets' when calling the method")
