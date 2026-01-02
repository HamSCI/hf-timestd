#!/bin/bash
# Health check for science-aggregator: Verify TEC data is being produced

set -e

DATA_ROOT="/var/lib/timestd"
# Aggregator runs every 5 minutes (300s), so we allow up to 15 minutes (3 cycles) before alerting
MAX_AGE_SECONDS=900 

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

# Find most recent TEC file (HDF5 or CSV)
LATEST_HDF5=$(find "$TEC_DIR" -name "tec_${TODAY}*.h5" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
LATEST_CSV=$(find "$TEC_DIR" -name "tec_${TODAY}*.csv" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

LATEST_FILE=""
if [ -n "$LATEST_HDF5" ]; then
    LATEST_FILE="$LATEST_HDF5"
elif [ -n "$LATEST_CSV" ]; then
    LATEST_FILE="$LATEST_CSV"
fi

if [ -z "$LATEST_FILE" ]; then
    echo "ERROR: No TEC files found for today in $TEC_DIR"
    exit 1
fi

# Check file age
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest TEC file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    exit 1
fi

echo "OK: Science aggregator producing data (latest file: $FILE_AGE seconds old)"
exit 0
