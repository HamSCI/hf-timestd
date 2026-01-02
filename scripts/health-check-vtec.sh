#!/bin/bash
# Health check for timestd-vtec: Verify GNSS VTEC data is being produced

set -e

DATA_ROOT="/var/lib/timestd"
# VTEC data depends on satellite visibility. 
# While usually continuous (1Hz), we allow 5 minutes gap for difficult conditions.
MAX_AGE_SECONDS=300 

if [ ! -d "$DATA_ROOT" ]; then
    # Fallback for test mode
    DATA_ROOT="/tmp/timestd-test"
fi

# Path based on default config (data/gnss_vtec) relative to working dir (DATA_ROOT)
VTEC_DIR="$DATA_ROOT/data/gnss_vtec"
TODAY=$(date +%Y%m%d)

# Check if VTEC directory exists
if [ ! -d "$VTEC_DIR" ]; then
    echo "ERROR: VTEC data directory not found: $VTEC_DIR"
    echo "       (Service may not be running or is disabled)"
    exit 1
fi

# Find most recent HDF5 file
# Note: live_vtec.py rotates files daily or by size, named gnss_vtec_YYYYMMDD_*.h5
LATEST_HDF5=$(find "$VTEC_DIR" -name "gnss_vtec_${TODAY}*.h5" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$LATEST_HDF5" ]; then
    # Fallback to check CSV if HDF5 disabled? 
    # Default is HDF5 enabled.
    # Check for CSV just in case
    LATEST_CSV=$(find "$DATA_ROOT/data" -name "gnss_vtec.csv" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    
    if [ -n "$LATEST_CSV" ]; then
        LATEST_FILE="$LATEST_CSV"
        echo "Found CSV but no HDF5 for today."
    else
        echo "ERROR: No VTEC files found for today in $VTEC_DIR"
        exit 1
    fi
else
    LATEST_FILE="$LATEST_HDF5"
fi

# Check file age
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest VTEC file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    exit 1
fi

echo "OK: VTEC service producing data (latest file: $FILE_AGE seconds old)"
exit 0
