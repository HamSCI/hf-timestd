#!/usr/bin/env python3
"""
Add Kalman filter fields to L2 timing measurements schema
"""

import json
from pathlib import Path

# Schema file
schema_file = Path("/home/mjh/git/hf-timestd/src/hf_timestd/schemas/l2_timing_measurements_v1.json")

# Read schema
with open(schema_file, 'r') as f:
    schema = json.load(f)

# Kalman filter fields to add
kalman_fields = [
    {
        "name": "tof_kalman_ms",
        "type": "float",
        "required": False,
        "allow_empty": True,
        "allow_nan": True,
        "description": "Kalman-filtered Time of Flight (ionospheric path delay)",
        "units": "milliseconds",
        "note": "Science-First Architecture v5.0: Per-broadcast Kalman filter state tracking ionospheric path dynamics"
    },
    {
        "name": "tof_uncertainty_ms",
        "type": "float",
        "required": False,
        "allow_empty": True,
        "allow_nan": True,
        "description": "Kalman filter uncertainty for ToF estimate",
        "units": "milliseconds"
    },
    {
        "name": "doppler_ms_per_min",
        "type": "float",
        "required": False,
        "allow_empty": True,
        "allow_nan": True,
        "description": "Rate of change of ToF (tracks ionospheric layer movement)",
        "units": "milliseconds per minute",
        "note": "Doppler in the ionospheric sense (layer movement), not carrier Doppler"
    },
    {
        "name": "gpsdo_consistent",
        "type": "boolean",
        "required": False,
        "allow_empty": True,
        "description": "GPSDO temporal continuity check: measurement consistent with previous minute (residual < 1ms)"
    }
]

# Find insertion point (after doppler_hz)
doppler_hz_index = None
for i, field in enumerate(schema['fields']):
    if field['name'] == 'doppler_hz':
        doppler_hz_index = i
        break

if doppler_hz_index is None:
    print("ERROR: Could not find doppler_hz field")
    exit(1)

# Insert Kalman fields after doppler_hz
for i, kalman_field in enumerate(kalman_fields):
    schema['fields'].insert(doppler_hz_index + 1 + i, kalman_field)

# Update schema version
schema['schema_version'] = '1.3.0'

# Write back
with open(schema_file, 'w') as f:
    json.dump(schema, f, indent=4)

print(f"✅ Updated schema to v1.3.0 with {len(kalman_fields)} Kalman fields")
print(f"   Schema file: {schema_file}")
print(f"\nAdded fields:")
for field in kalman_fields:
    print(f"   - {field['name']}: {field['description']}")
