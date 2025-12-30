#!/bin/bash
# Health check for core-recorder: Verify raw data is being written

set -e

DATA_ROOT="/var/lib/timestd"
MAX_AGE_SECONDS=120  # Alert if no new files in 2 minutes

# Find most recent .bin file across all channels
LATEST_FILE=$(find "$DATA_ROOT/raw_buffer" -name "*.bin" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

if [ -z "$LATEST_FILE" ]; then
    echo "ERROR: No raw buffer files found in $DATA_ROOT/raw_buffer"
    exit 1
fi

# Check file age
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$LATEST_FILE") ))

if [ "$FILE_AGE" -gt "$MAX_AGE_SECONDS" ]; then
    echo "WARNING: Latest raw buffer file is $FILE_AGE seconds old (max: $MAX_AGE_SECONDS)"
    echo "File: $LATEST_FILE"
    exit 1
fi

echo "OK: Core recorder writing data (latest file: $FILE_AGE seconds old)"
exit 0
