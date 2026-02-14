#!/bin/bash
# Health check for timestd-physics: Verify L3 physics data is being produced

set -e

DATA_ROOT="/var/lib/timestd"
# Physics fusion runs every minute, so we allow up to 5 minutes before alerting
MAX_AGE_SECONDS=300 

if [ ! -d "$DATA_ROOT" ]; then
    # Fallback for test mode
    DATA_ROOT="/tmp/timestd-test"
fi

TEC_DIR="$DATA_ROOT/phase2/science/tec"
TODAY=$(date +%Y%m%d)

# Check if TEC directory exists
if [ ! -d "$TEC_DIR" ]; then
    echo "ERROR: TEC directory not found: $TEC_DIR"
    exit 1
fi

# Find most recent TEC product file (HDF5)
LATEST_HDF5=$(find "$TEC_DIR" -name "*tec_${TODAY}*.h5" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

LATEST_FILE=""
if [ -n "$LATEST_HDF5" ]; then
    LATEST_FILE="$LATEST_HDF5"
fi

if [ -z "$LATEST_FILE" ]; then
    echo "ERROR: No physics files found for today in $TEC_DIR"
    exit 1
fi

# Check file age
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest TEC file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    exit 1
fi

echo "OK: Physics fusion producing data (latest file: $FILE_AGE seconds old)"
exit 0
